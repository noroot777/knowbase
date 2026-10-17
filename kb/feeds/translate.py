# -*- coding: utf-8 -*-
"""外文订阅条目的中文化:抓进来是英文,落进知识库之前先译成中文。

**硬约束在 `docs/decisions/translate.md` 第一节 —— 改这块之前先读那一份。**

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

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor

from kb.feeds import registry as reg
# 「哪些条目算在这一轮里」的判据全在那个模块里(搬出去只因为 700 行上限)。
# **一律原样再导出**:外面照旧 `translate.queued_sql()` / `translate.stuck()`,
# 测试照旧 `patch.object(translate, 'pending')` —— 这几个名字在本模块的全局里,
# 打桩改的就是这一份,而 `translate_pending` 查的也是这一份。
from kb.feeds.translate_scope import (            # noqa: F401 - 再导出
    MAX_TRIES, RETRY_SQL, ScopeChanged, clear_manual, manual_sql, pending,
    queued_sql, requeue_failed, scope_signature, stuck, _retry_where,
)
from kb.pipeline_state import _state

# 一条正文最多译多少字:超出部分不译,末尾注明并指向原文。RSS 全文条目偶尔
# 上万字,整篇译一遍的钱和时间都不划算,而知识库要的是能被检索到的中文正文。
MAX_BODY = int(os.environ.get('KB_TRANSLATE_MAX_BODY', '12000'))
CHUNK = 3000            # 单次请求的正文上限,按段落切,不切断句子
WORKERS = 3             # 并发度:中转站限流下 3 条并行已经够快,再高容易 429

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
                # **判据带上算它时那一版的内容**(口径同 `_save` 那两句):这一趟的
                # SELECT 不开事务、不持锁(legacy `isolation_level`),中间那一行可能
                # 被换掉 —— 手动收录那颗「重新抓一次」跑在 HTTP 请求线程里、不持
                # 任何同步的维护锁,而且它自己也写 `lang`(`grab.replace`)。无条件
                # 按 key 写的后果是把**按旧正文算出来的**语种盖到新正文上:一篇
                # 中文新正文被标成英文、白买一次翻译。`rowcount == 0` 不是失败
                # (元规则),是「那一版已经不在了」。
                c.execute('UPDATE feed_items SET lang=? WHERE key=? AND title=? AND body=?',
                          (lang, r['key'], r['title'], r['body']))


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


def input_version(item) -> tuple[str, str]:
    """这一次翻译请求所依据的那份输入:标题 + 正文。写回时拿它当判据。

    **口径只有这一份。** 取快照那头和写回那头各写一遍的话,哪天多比一列、
    少比一列,两处就各漂各的 —— 而这道判据漏一次就是让一份作废的译文重新
    成为知识库内容。

    标题也要比,不是只比正文:半成品那条路(见 `PARTIAL_SQL`)的请求输入就
    只有标题,而正文换掉时它手里那份 `body_zh` 同样是旧正文的译文。
    """
    return (item['title'] or '', item['body'] or '')


def _save(key: str, title_zh: str, body_zh: str, seen: tuple[str, str]) -> bool:
    """写回译文。返回**写进去了没有** —— 0 行 = 这期间原文换过了。

    **判据是「这一条还是我发请求时那份标题和正文吗」,而且待在 UPDATE 自己的
    WHERE 里,不许在 Python 这边先 SELECT 一次比对。** 先查后写在两个线程/
    进程之间压根不成立(翻译跑在 3 个工作线程上,而换正文的那两个写入方 ——
    `fulltext._save` 和 `scripts/feed_fulltext_recheck` —— 可能在另一个进程里),
    而 SQLite 把写入串行化:判据待在写语句里才和那次写入同生共死。口径同
    `scripts/feed_fulltext_recheck._downgrade`、`sync._claim_write`、
    `store.mark_no_body` 和 collector 那条 UPSERT。

    **不这么写就是让作废的旧译文重新成为知识库内容,而且不可恢复**:模型请求
    要几十秒到几分钟,这期间复查(或者一次成功补抓)完全可能把这一条的正文整
    份换掉并清空译文 —— 旧回包随后按 key 盖回去,`translated_at` 跟着重新设上,
    于是这一条**再也不进翻译队列**,导出和阅读又优先用 `body_zh`:正文是新的、
    中文却是旧内容的译文,后面每一轮同步都把这段文字重新写进 vault
    (2026-10-09 第 4 轮审查三个隔离库稳定复现:复查把一段版权声明换成了正确
    的标题与链接,而迟到的旧回包把「版权所有,网站使用条款和隐私声明。」写了
    回去,再同步三轮也清不掉)。

    **0 行什么都不动,这正是我们要的末态**:`translated_at` 没写上,这一条还
    待在队列里(它此刻多半在补抓队列,等正文定下来再译);`exported_hash` 也
    没清,vault 里那份不会被这份过期译文重写。
    """
    with reg.db() as c:
        # exported_hash 清空 = 让导出阶段重写这篇 md。译完不重导的话,vault 里
        # 留着的还是那份英文,索引/提取/日报读到的也还是英文,等于没译。
        cur = c.execute("""UPDATE feed_items SET title_zh=?,body_zh=?,translated_at=?,
            translate_error='',translate_tries=0,translate_manual=0,exported_hash=''
            WHERE key=? AND title=? AND body=?""",
            (title_zh, body_zh, reg.now(), key, *seen))
        return bool(cur.rowcount)


def _fail(key: str, error: Exception, seen: tuple[str, str]) -> bool:
    """记一次失败。返回**记上了没有** —— 判据同 `_save`,理由是另一半:

    一份基于旧正文的请求失败了,那是**旧内容**的毛病,不该记在换过来的新内容
    头上。记上去就是拿别人的错误推进这一条的重试计数(`MAX_TRIES` 到顶就按
    原文导出,等于让新正文少几次翻译机会),面板上那行「补译失败」说的也是一
    件跟当前内容无关的事。
    """
    with reg.db() as c:
        cur = c.execute('UPDATE feed_items SET translate_error=?,'
                        'translate_tries=translate_tries+1'
                        ' WHERE key=? AND title=? AND body=?',
                        (str(error)[:600] or type(error).__name__, key, *seen))
        return bool(cur.rowcount)


def translate_pending(kind: str, limit: int | None = None, progress=None,
                      only_manual: bool = False,
                      source_id: int | None = None) -> dict:
    """把这个来源里待翻译的条目译成中文。返回一份成品摘要给调用方汇报。

    默认处理当前范围内全部待译条目，不设每轮条数上限。只有显式传入 `limit`
    的调用才限量；失败条目每轮只尝试一次，不在同一轮里反复重试。

    没配 key / 没有可用模型时直接跳过(skipped 带原因),不报错也不重试计数
    ——那是配置问题,不是这些条目有问题,按失败记会把重试次数白白耗光。

    **`only_manual` 和 `source_id` 一起才是付费边界,缺一不可。** 面板上那颗
    「重新翻译」按钮的射程跟着当前选中的订阅走,而这两个参数是那个射程唯一跟得到
    取数这一步的东西:收窄只到 `requeue_failed`(它按 `source_id` 点名)的话,
    这里照旧按整个 kind 取数,于是用户确认「这条订阅的 1 条」却为
    另一条订阅的内容也付了钱(2026-10-13 第 1 轮审查实测)。`crawl_filter`
    到不了这里 —— `crawl_channels` 只留 `channel`/`accounts` 两个键。

    **`source_id` 这一道是为了不把旧标记当成本次授权。** `translate_manual`
    是落在库里的持久标记,而清它那一步**不保证跑得到**:同一轮里补抓阶段抛异常、
    进程被 kill、机器断电,标记就留在那儿。下一次重试筛着另一条订阅点下去,
    只按标记取数就会把上一轮残留的那些一起译掉 —— 用户确认 1 条、实际译 2 条,
    多出来的那条属于他这次压根没选的订阅(2026-10-14 第 2 轮审查实测:在补抓
    的正文保存处注入 `OSError`,随后筛另一条订阅重试,两条都译成了中文)。
    判据因此是**本次点的那个范围**,不是「上一轮应该清掉了」。

    **常规同步两个都不传**:那时照旧按「点名的 → 失败过的 → 新的」排序取数,
    只是顺序上让残留的点名条目先走,不把别的内容关在门外。
    """
    # stale:这一轮的回包作废了几条(请求发出之后原文被换掉了,见 `_save`)。
    # 它既不算成功也不算失败 —— 什么都没坏,只是这份译文不是当前内容的译文;
    # 统计要如实反映**实际写回结果**,按"调了几次模型"报的话这一轮会说"已翻译
    # N 条"而库里一个字都没改。
    summary = {'done': 0, 'failed': 0, 'left': 0, 'stale': 0, 'skipped': '',
               'errors': [], 'failed_keys': []}
    _label_unknown(kind)
    _requeue_partial(kind)
    cond, cargs = queued_sql()          # 队列判据只有这一份,别在这里另写一遍
    if only_manual:
        cond += ' AND translate_manual=1'
    # 本次点名的那个范围。**和标记是两道,不是一道** —— 见上面 docstring。
    scope, sargs = _retry_where(source_id)
    cond += scope
    cargs = [*cargs, *sargs]
    with reg.db() as c:
        # **失败过的优先**。原来是 `ORDER BY translate_tries`(升序),于是译
        # 不动的那些永远排在"从没试过"的后面:实测队列里 833 条 tries=0 压着
        # 63 条 tries=1,按一轮 40 条算要 21 轮同步、三周才轮得到重试一次,而
        # 这期间每一轮导出都把这 63 条原样报成「补译失败」并判红 —— 一个三周
        # 不变的红点,说的还不是这一轮发生的事(2026-10-08 实测)。
        # 失败优先不会让它们赖着:`translate_tries<MAX_TRIES` 是硬出口,试满
        # 就按原文导出、离开队列。从没试过的条目在同一轮接着处理。
        sql = (
            f'SELECT * FROM feed_items WHERE kind=? AND ({cond})'
            # **手动重试排在最前面**,压过「失败过的优先」:那颗按钮是用户的
            # 显式指令,而第二段排序恰好把刚放回队列的那批排到队尾(额度清零
            # = 从没试过)。显式限量调用时也要先取点名的条目，不能挤到后面
            # 导致一条都没译 —— 见 `requeue_failed`。
            # `key` 收尾是为了定序:`fetched_at` 只精确到秒(`registry.now`),
            # 一批抓回来的几百条时间戳一模一样,前几段排完仍然没有定序 —— 调用
            # 顺序就交给 SQLite 的扫描顺序了,不可复现也没法测。
            ' ORDER BY translate_manual DESC, translate_tries DESC,'
            ' fetched_at DESC, key')
        args = (kind, *cargs)
        if limit is not None:
            sql += ' LIMIT ?'
            args += (max(1, limit),)
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    # 剩几条要按**这一轮的射程**数:只译点名条目时拿整个来源的队列长度去减,
    # 报出来的「留到下一轮」里混着压根不在这一轮射程里的内容。
    summary['left'] = max(0, pending(kind, source_id,
                                     only_manual=only_manual) - len(rows))
    if not rows:
        return summary
    try:
        model = _model()
    except Exception as e:
        summary['skipped'] = str(e)
        summary['left'] = pending(kind, source_id, only_manual=only_manual)
        return summary

    def one(item):
        """调模型。失败就地记错(时机和写回判据都跟着这一条自己走)。

        第四个值是「这一条的写回落下了吗」:失败那一侧由 `_fail` 现场回答,
        成功那一侧的实际写入方是主循环里的 `_save`,所以这里给 True。
        """
        if _state.get('stop_requested'):
            return item, None, None, True
        try:
            return item, *translate_item(item, model), True
        except Exception as e:                       # noqa: BLE001 - 逐条隔离
            return item, None, e, _fail(item['key'], e, input_version(item))

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='translate') as pool:
        for item, title_zh, result, recorded in pool.map(one, rows):
            if isinstance(result, Exception) and recorded:
                summary['failed'] += 1
                # key 也记下来:导出那头要靠它把"本轮新失败"和"此前失败、还排着
                # 队"分开报,拿 failed 这个计数是分不出来的(见 `sync._export`)。
                summary['failed_keys'].append(item['key'])
                summary['errors'].append({'title': item['title'][:120], 'reason': str(result)[:300]})
            elif isinstance(result, Exception):      # 原文已经换过,这次失败不算它的
                summary['stale'] += 1
            elif result is None:                     # 收到停止请求,剩下的留给下一轮
                summary['left'] += 1
            elif _save(item['key'], title_zh, result, input_version(item)):
                summary['done'] += 1
            else:
                summary['stale'] += 1                # 作废,不写成功标记也不重导出
            if progress:
                progress(summary['done'] + summary['failed'], len(rows))
    return summary


def summarize(summary: dict) -> str:
    if summary['skipped']:
        return f"英文内容未翻译({summary['skipped']})"
    if not (summary['done'] or summary['failed'] or summary['left']
            or summary.get('stale')):
        return ''
    text = f"已翻译 {summary['done']} 条英文内容"
    if summary['failed']:
        text += f",{summary['failed']} 条翻译失败(仍在重试,暂不入库)"
    if summary.get('stale'):
        # 不混进 failed:那会让面板判红,而这不是故障。也不混进 left:那句话
        # 说的是"这一轮没轮到",而这一条是译过了、结果作废了。
        text += f",{summary['stale']} 条的原文在这期间换过、旧译文作废(下一轮按新正文重译)"
    if summary['left']:
        text += f",{summary['left']} 条留到下一轮译完再入库"
    return text
