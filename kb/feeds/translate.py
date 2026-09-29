# -*- coding: utf-8 -*-
"""外文订阅条目的中文化:抓进来是英文,落进知识库之前先译成中文。

为什么放在导出之前、而不是阅读时现翻:vault 的 markdown 是事实来源,索引、
提取、检索、日报全都读它。阅读时现翻只有那一块屏幕是中文,搜"智能体"仍然
搜不到一篇讲 agent 的英文文章,日报也还是拿英文素材写。

判定不花钱(`registry.text_language` 只看字符构成),只有判成外文的条目才调
模型;一条译一次,结果落在 feed_items 的 title_zh/body_zh 上长期复用。

失败不挡路:译不动的条目按原文导出(`display()` 有译文用译文、没有用原文),
记下失败原因等下一轮重试,超过 `MAX_TRIES` 次就不再自动重试——它多半不是
偶发抖动,而是这条内容本身有问题,留着原文比一直烧钱强。
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
    """拆 ###TITLE###/###BODY### 两段。模型没按格式回时整段当正文,标题留原文
    ——宁可标题没译,也不要把一行格式标记当成标题写进 vault。"""
    if '###BODY###' not in out:
        return '', out.strip()
    head, _, body = out.partition('###BODY###')
    return head.replace('###TITLE###', '').strip(), body.strip()


def translate_item(item, model) -> tuple[str, str]:
    """返回 (标题译文, 正文译文)。标题译不出来时回空串,调用方保留原标题。"""
    body = (item['body'] or '')[:MAX_BODY]
    truncated = len(item['body'] or '') > MAX_BODY
    parts = _split(body) or ['']
    title_zh, out = _parse_titled(_call(
        _WITH_TITLE.format(title=item['title'] or '(无标题)', body=parts[0]), model))
    pieces = [out]
    for extra in parts[1:]:
        pieces.append(_call(_BODY_ONLY.format(body=extra), model))
    text = '\n\n'.join(p for p in pieces if p)
    if not text.strip():
        raise RuntimeError('模型返回了空译文')
    if truncated:
        text += _TRUNCATED.format(n=MAX_BODY)
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


def pending(kind: str) -> int:
    with reg.db() as c:
        return c.execute(
            "SELECT count(*) FROM feed_items WHERE kind=? AND lang='en'"
            " AND body_zh='' AND translate_tries<?", (kind, MAX_TRIES)).fetchone()[0]


def _label_unknown(kind: str) -> None:
    """老库里的条目没判过语种(lang='')。判定纯本地,一次把它们标完。"""
    with reg.db() as c:
        rows = c.execute("SELECT key,title,body FROM feed_items WHERE kind=? AND lang=''",
                         (kind,)).fetchall()
        for r in rows:
            c.execute('UPDATE feed_items SET lang=? WHERE key=?',
                      (reg.text_language(r['title'], r['body']), r['key']))


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
    summary = {'done': 0, 'failed': 0, 'left': 0, 'skipped': '', 'errors': []}
    _label_unknown(kind)
    with reg.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM feed_items WHERE kind=? AND lang='en' AND body_zh=''"
            ' AND translate_tries<? ORDER BY translate_tries, fetched_at DESC LIMIT ?',
            (kind, MAX_TRIES, max(1, limit or MAX_ITEMS))).fetchall()]
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
        text += f",{summary['failed']} 条翻译失败(按原文保留)"
    if summary['left']:
        text += f",{summary['left']} 条留待下次同步"
    return text
