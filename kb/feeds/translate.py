# -*- coding: utf-8 -*-
"""外文订阅条目的中文化:抓进来是英文,落进知识库之前先译成中文。

为什么放在导出之前、而不是阅读时现翻:vault 的 markdown 是事实来源,索引、
提取、检索、日报全都读它。阅读时现翻只有那一块屏幕是中文,搜"智能体"仍然
搜不到一篇讲 agent 的英文文章,日报也还是拿英文素材写。

判定不花钱(`registry.text_language` 只看字符构成),只有判成外文的条目才调
模型;一条译一次,结果落在 feed_items 的 title_zh/body_zh 上长期复用。

失败不挡路,但也不抢在前头:还排在翻译队列里的条目这一轮先不导出(见
`queued_sql`),等译完再落盘;重试 `MAX_TRIES` 次仍然译不动的才按原文导出
——那多半不是偶发抖动,而是这条内容本身有问题,留着原文比一直烧钱强,更比
把它挡在知识库外面强。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from kb.feeds import registry as reg
from kb.pipeline_state import _state

# 一轮同步最多译多少条:存量几千条英文的话,一次全译会把一次同步拖成几小时、
# 还一次性烧掉一大笔钱。分批消化,每轮同步啃一截。
MAX_ITEMS = int(os.environ.get('KB_TRANSLATE_MAX_ITEMS', '40'))
# 一条正文最多译多少字:超出部分不译,末尾注明并指向原文。RSS 全文条目偶尔
# 上万字,整篇译一遍的钱和时间都不划算,而知识库要的是能被检索到的中文正文。
MAX_BODY = int(os.environ.get('KB_TRANSLATE_MAX_BODY', '12000'))
CHUNK = 3000            # 单次请求的正文上限,按段落切,不切断句子
WORKERS = 3             # 并发度:中转站限流下 3 条并行已经够快,再高容易 429
MAX_TRIES = 3

_TRUNCATED = '\n\n> 原文较长，以上为前 {n} 字的中文翻译，完整内容请打开原文。'
NOTE = '> 本文由英文原文自动翻译，专有名词保留原文；如有歧义请对照原文。'

_RULES = """把下面的内容翻译成简体中文。

要求:
- 只输出译文本身,不要解释、不要前言、不要代码围栏。
- 保持原有的段落与换行结构,不要合并或拆分段落。
- 产品名、公司名、人名、代码、命令、URL、缩写保留原文,不要音译。
- 不增不减不总结:原文说什么就译什么。"""

_WITH_TITLE = _RULES + """

严格按下面的格式输出:
###TITLE###
(标题的译文)
###BODY###
(正文的译文)

标题:
{title}

正文:
{body}"""

_BODY_ONLY = _RULES + """

正文:
{body}"""

# 标题单独译:正文为空的条目(social 那类短动态)用它,正文回包里漏了标题
# 分隔标记时也用它补一次。
_TITLE_ONLY = _RULES + """

只译下面这一行标题,直接输出译文本身,不要加标记:
{title}"""


def _model():
    from kb import llm, llm_stages
    m = llm_stages.stage_model('translate')
    if not llm.has_key(m.provider):
        raise RuntimeError(f'{m.provider} 还没配 API key,无法翻译外文内容')
    return m


def _call(prompt: str, model) -> str:
    from kb import llm
    out = llm.chat(prompt, model, max_tokens=8192, timeout=180).strip()
    if not out:
        raise RuntimeError('模型返回了空译文')
    return out


def _split(body: str) -> list[str]:
    """按段落切成不超过 CHUNK 的块。单段超长时硬切,总比整条译不动强。"""
    chunks: list[str] = []
    for para in body.split('\n\n'):
        while len(para) > CHUNK:
            chunks.append(para[:CHUNK])
            para = para[CHUNK:]
        if not chunks or len(chunks[-1]) + len(para) + 2 > CHUNK:
            chunks.append(para)
        else:
            chunks[-1] += '\n\n' + para
    return [c for c in chunks if c.strip()]


def _parse_titled(out: str) -> tuple[str, str]:
    """拆 ###TITLE###/###BODY### 两段。没有分隔标记时整段当正文、标题回空串
    ——一行格式标记不能当标题写进 vault。空标题不代表"这一条译完了",
    由调用方补问一次(见 `translate_item`)。"""
    if '###BODY###' not in out:
        return '', out.strip()
    head, _, body = out.partition('###BODY###')
    return head.replace('###TITLE###', '').strip(), body.strip()


def _title_only(title: str, model) -> str:
    """单独译一行标题。译不出来就抛错,让整条走失败重试。"""
    if not title.strip():
        return ''
    for line in _call(_TITLE_ONLY.format(title=title), model).splitlines():
        line = line.replace('###TITLE###', '').replace('###BODY###', '').strip()
        if line:
            return line
    raise RuntimeError('标题没有译出来')


def translate_item(item, model) -> tuple[str, str]:
    """返回 (标题译文, 正文译文)。**标题和正文都得有译文才算这一条译完了。**

    标题单独兜一道:模型偶尔漏掉 ###TITLE### 分隔标记,那时只解析得出正文。
    原来这种回包按"成功,标题留原文"存下来,而存下来就不再进翻译队列——页面
    上标着"已译为中文",标题却永久是英文(2026-09-30 复审实测)。所以缺了
    就补问一次;补不到就抛错,整条按失败计次、下一轮重试。

    正文为空的条目(social 那类只有一行标题的动态)不走带正文的那套格式:
    问它译一段空正文只会拿回空译文,白白耗掉三次重试额度。
    """
    original = item['title'] or ''
    if _has(item, 'body_zh') and not _has(item, 'title_zh') and original:
        # 存量半成品(见 `_requeue_partial`):正文已经是中文,只差标题那一行。
        # 整篇重译一遍是把同一份正文再买一次,而它已经在 vault 里了。
        return _title_only(original, model), item['body_zh']
    body = (item['body'] or '')[:MAX_BODY]
    truncated = len(item['body'] or '') > MAX_BODY
    parts = _split(body)
    if not parts:                                # 只有标题
        return _title_only(original, model), ''
    title_zh, out = _parse_titled(_call(
        _WITH_TITLE.format(title=original or '(无标题)', body=parts[0]), model))
    pieces = [out]
    for extra in parts[1:]:
        pieces.append(_call(_BODY_ONLY.format(body=extra), model))
    text = '\n\n'.join(p for p in pieces if p)
    if not text.strip():
        raise RuntimeError('模型返回了空译文')
    if truncated:
        text += _TRUNCATED.format(n=MAX_BODY)
    if original and not title_zh:
        title_zh = _title_only(original, model)
    return title_zh, text


def display(item) -> tuple[str, str]:
    """落盘/展示用的标题与正文:有译文用译文,没有就用原文。

    这是「译文优先」的唯一口径 —— 导出、面板、阅读器各判各的话,同一条内容
    在 vault 里是中文、在面板上是英文,用户根本不知道库里到底存了什么。
    """
    title = item['title_zh'] if _has(item, 'title_zh') else ''
    body = item['body_zh'] if _has(item, 'body_zh') else ''
    return (title or item['title']), (body or item['body'])


def _has(item, column: str) -> bool:
    try:
        return bool(item[column])
    except (IndexError, KeyError):      # 老 Row 对象(迁移前取出来的)没有这列
        return False


def queued_sql() -> tuple[str, list]:
    """"还排在翻译队列里"的判据,只有这一份。

    这些条目**不按英文导出**:一轮只译 MAX_ITEMS 条,一次进来 41 条英文的话
    第 41 条会先以英文写进 vault,而索引、提取、检索、日报读的全是 vault 里
    那份英文。等下一轮译完再落盘,晚一轮但是中文的。队列有出口——试满
    MAX_TRIES 次仍然失败的就不在这个集合里了,那时按原文导出,免得一条
    永远译不动的内容永远进不了知识库。

    判据是 `translated_at`(`_save` 只在整条译成时写它),不是"有没有 body_zh"
    ——只有标题的短动态译完之后 body_zh 仍然是空串,拿它判的话这一条译完了
    还赖在队列里,每轮重译一遍。

    **还排在正文补抓队列里的条目不算待翻译**(见 `kb.feeds.fulltext`):它们
    此刻手里那份正文是 feed 给的跳转壳,正文马上要被整条换掉。不排掉的话同一
    条内容买两次模型调用 —— 先按壳译一遍,补上正文再译一遍,而第一份译文随后
    就被 `fulltext._save` 丢掉。翻译是这条链上最贵的一步,顺序不能反。
    """
    from kb.feeds import fulltext      # 函数内 import:两个模块互相认识对方的口径
    cond, args = fulltext.queued_sql()
    return (f"lang='en' AND translated_at='' AND translate_tries<? AND NOT ({cond})",
            [MAX_TRIES, *args])


def available() -> bool:
    """翻译这一步现在能不能真的发生(配了 key、解析得出模型)。

    来源面板据此决定"没有译文"该怎么显示:能翻就是"还在等翻译,尚未入库",
    翻不了就是"这条只有原文",而后者的原文此刻已经按原样进了 vault
    (`materialize()` 传 `hold_untranslated=not skipped`)。
    """
    try:
        _model()
        return True
    except Exception:                      # noqa: BLE001 - 配置问题一律当"翻不了"
        return False


def pending(kind: str) -> int:
    cond, args = queued_sql()
    with reg.db() as c:
        return c.execute(f'SELECT count(*) FROM feed_items WHERE kind=? AND {cond}',
                         (kind, *args)).fetchone()[0]


# 复判的长度上限:判定规则收紧的只是"短文本"那一档,够长的条目在旧规则下
# 就走不到那一档,不必每轮重新数一遍字符。
_RECHECK_CHARS = 400


def _label_unknown(kind: str) -> None:
    """老库里的条目没判过语种(lang='')。判定纯本地,一次把它们标完。

    顺带复判一次短条目:2026-09-30 之前"字母少于 12 个一律算中文"把
    「Hello world」这种短外文永久挡在翻译队列外面,只改判定规则的话存量那批
    仍然一辈子是英文。判定不花钱,标签变了才写盘,所以这一趟稳定状态下是
    空转。
    """
    with reg.db() as c:
        rows = c.execute('SELECT key,title,body,lang FROM feed_items WHERE kind=? AND'
                         " (lang='' OR (lang='zh' AND length(title)+length(body)<?))",
                         (kind, _RECHECK_CHARS)).fetchall()
        for r in rows:
            lang = reg.text_language(r['title'], r['body'])
            if lang != (r['lang'] or ''):
                c.execute('UPDATE feed_items SET lang=? WHERE key=?', (lang, r['key']))


# 「半成品」的唯一口径:整条判成外文、原标题不空、正文有译文而标题没有。
# 这形态是 2026-09-30 之前那版解析器留下的——模型漏掉 ###TITLE### 分隔标记时,
# 整段当正文、空标题照样存下来。修了写入路径救不了**已经在库里的那批**:它们
# `translated_at` 非空,新的队列判据永远选不到,面板还按"任一字段有译文"标成
# 「已译为中文」,于是英文标题永久留着、再同步也不补(第 3 轮审查实测)。
PARTIAL_SQL = "lang='en' AND title<>'' AND title_zh='' AND body_zh<>''"


def _requeue_partial(kind: str) -> int:
    """把存量半成品清回待翻译状态,让下一趟只补一次标题。返回清回几条。

    幂等:清掉 `translated_at` 之后这一条就不再匹配 `PARTIAL_SQL` 里那个
    "已经完成过"的前提了(它进了队列,由正常流程收尾)。`translate_tries` 保持
    原样——补译标题失败照样计次,试满 `MAX_TRIES` 仍然拿不到标题的按原文留着,
    不会无限烧钱。
    """
    with reg.db() as c:
        cur = c.execute("UPDATE feed_items SET translated_at='' WHERE kind=? AND"
                        f" translated_at<>'' AND ({PARTIAL_SQL})", (kind,))
        return cur.rowcount or 0


def _save(key: str, title_zh: str, body_zh: str) -> None:
    with reg.db() as c:
        # exported_hash 清空 = 让导出阶段重写这篇 md。译完不重导的话,vault 里
        # 留着的还是那份英文,索引/提取/日报读到的也还是英文,等于没译。
        c.execute("""UPDATE feed_items SET title_zh=?,body_zh=?,translated_at=?,
            translate_error='',translate_tries=0,exported_hash='' WHERE key=?""",
            (title_zh, body_zh, reg.now(), key))


def _fail(key: str, error: Exception) -> None:
    with reg.db() as c:
        c.execute('UPDATE feed_items SET translate_error=?,translate_tries=translate_tries+1'
                  ' WHERE key=?', (str(error)[:600] or type(error).__name__, key))


def translate_pending(kind: str, limit: int | None = None, progress=None) -> dict:
    """把这个来源里待翻译的条目译成中文。返回一份成品摘要给调用方汇报。

    没配 key / 没有可用模型时直接跳过(skipped 带原因),不报错也不重试计数
    ——那是配置问题,不是这些条目有问题,按失败记会把重试次数白白耗光。
    """
    summary = {'done': 0, 'failed': 0, 'left': 0, 'skipped': '',
               'errors': [], 'failed_keys': []}
    _label_unknown(kind)
    _requeue_partial(kind)
    cond, cargs = queued_sql()          # 队列判据只有这一份,别在这里另写一遍
    with reg.db() as c:
        # **失败过的优先**。原来是 `ORDER BY translate_tries`(升序),于是译
        # 不动的那些永远排在"从没试过"的后面:实测队列里 833 条 tries=0 压着
        # 63 条 tries=1,按一轮 40 条算要 21 轮同步、三周才轮得到重试一次,而
        # 这期间每一轮导出都把这 63 条原样报成「补译失败」并判红 —— 一个三周
        # 不变的红点,说的还不是这一轮发生的事(2026-10-08 实测)。
        # 失败优先不会让它们赖着:`translate_tries<MAX_TRIES` 是硬出口,试满
        # 就按原文导出、离开队列。而 tries=0 的那些只是正常积压,晚一轮无损。
        rows = [dict(r) for r in c.execute(
            f'SELECT * FROM feed_items WHERE kind=? AND ({cond})'
            # `key` 收尾是为了定序:`fetched_at` 只精确到秒(`registry.now`),
            # 一批抓回来的几百条时间戳一模一样,前两段排完仍然没有定序 —— 取
            # 哪 40 条就交给 SQLite 的扫描顺序了,不可复现也没法测。
            ' ORDER BY translate_tries DESC, fetched_at DESC, key LIMIT ?',
            (kind, *cargs, max(1, limit or MAX_ITEMS))).fetchall()]
    summary['left'] = max(0, pending(kind) - len(rows))
    if not rows:
        return summary
    try:
        model = _model()
    except Exception as e:
        summary['skipped'] = str(e)
        summary['left'] = pending(kind)
        return summary

    def one(item):
        if _state.get('stop_requested'):
            return item, None, None
        try:
            return item, *translate_item(item, model)
        except Exception as e:                       # noqa: BLE001 - 逐条隔离
            _fail(item['key'], e)
            return item, None, e

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='translate') as pool:
        for item, title_zh, result in pool.map(one, rows):
            if isinstance(result, Exception):
                summary['failed'] += 1
                # key 也记下来:导出那头要靠它把"本轮新失败"和"此前失败、还排着
                # 队"分开报,拿 failed 这个计数是分不出来的(见 `sync._export`)。
                summary['failed_keys'].append(item['key'])
                summary['errors'].append({'title': item['title'][:120], 'reason': str(result)[:300]})
            elif result is None:                     # 收到停止请求,剩下的留给下一轮
                summary['left'] += 1
            else:
                _save(item['key'], title_zh, result)
                summary['done'] += 1
            if progress:
                progress(summary['done'] + summary['failed'], len(rows))
    return summary


def summarize(summary: dict) -> str:
    if summary['skipped']:
        return f"英文内容未翻译({summary['skipped']})"
    if not (summary['done'] or summary['failed'] or summary['left']):
        return ''
    text = f"已翻译 {summary['done']} 条英文内容"
    if summary['failed']:
        text += f",{summary['failed']} 条翻译失败(仍在重试,暂不入库)"
    if summary['left']:
        text += f",{summary['left']} 条留到下一轮译完再入库"
    return text
