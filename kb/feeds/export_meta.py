# -*- coding: utf-8 -*-
"""导出那一步的两个零件:**frontmatter 里哪些键归谁**、以及**本轮新失败怎么数**。

**单纯因为 700 行上限**(见 CLAUDE.md「代码文件行数上限」)从 `kb.feeds.sync` 里
搬出来的,口径一个字都没改 —— 做法同当初搬出 `history`。两个名字在 `sync` 那边
**原地再导出**,所以 `sync._kept_meta` / `sync._new_failures` 这些写法(以及全仓
注释、`docs/decisions/translate.md` 和回归里按名字的指向)照旧成立。

两个零件都**不读模块全局**:`_kept_meta` 收一个 path,`_new_failures` 收调用方的
连接和 SQL 片段 —— 所以搬家不会动到任何打桩点(`sync.VAULT_ROOT` 那一类)。
"""
from __future__ import annotations

from pathlib import Path

from kb.scanner import parse_frontmatter


# export 自己产出的 frontmatter 字段:每一轮都按 registry 里的当前事实重写。
# 这张表之外的键都是**别人**写进这篇 md 的(提取回写的 tags 是唯一的现役例子),
# 重导出时必须原样留下。
_OWNED_META = ('source', 'title', 'account', 'published', 'url', 'fetched_at',
               'content_kind', 'lang', 'translated_from', 'original_title')


def _kept_meta(path: Path) -> dict:
    """旧文件里那些不属于 export 的 frontmatter 字段。

    重导出是整份重写这篇 md。原来只写 export 自己那几个键,于是提取阶段回写的
    `tags`(topic/entity/flag)被一起抹掉 —— 而索引按 frontmatter 重建 doc_tags、
    `documents.extracted_at` 又还在,提取不会再做一遍:这篇文章从此按话题再也
    搜不到,以后每次同步都是同一个结果(2026-09-30 审查实测:一篇已提取的英文
    旧文补译后,按原话题检索当场从 1 条归零)。

    重导出原本少见(只有源内容变了才发生),翻译把它变成了常规路径:没配 key
    那阵子按英文入库的条目补上 key 之后要重写一遍,老解析器留下的半成品补译
    标题也要重写一遍。保留而不是重做提取,是因为标签口径没变 —— 内容还是同一
    篇,只是换了语言,再买一次提取只是重复付钱。
    """
    try:
        meta, _ = parse_frontmatter(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError):
        return {}                     # 还没导出过(或者读不了),没什么可留的
    return {k: v for k, v in meta.items() if k not in _OWNED_META}


def _new_failures(conn, queue: str, cond: str, args: list, extra: list,
                  keys: list[str], column: str = 'translate_error') -> tuple[int, int]:
    """这一轮刚失败、而且**还排在队列里**的那些,按"入没入过库"分两档。

    返回 `(未入库的, 已入库的)`。`keys` 是这一轮真失败的那几个条目;空列表
    (比如 `export` 被单独调用、这一轮压根没跑那一步)时两档都是 0 —— 没跑就没有
    "本轮新失败"可言,队列里那些错误全是此前留下的。

    **翻译和正文补抓共用这一份**(`column` 选哪一列的错误,`cond`/`extra` 给各自
    那个队列判据)。两边要回答的是同一个问题:"这一轮真失败的那几条,现在还被
    扣在队列里吗" —— 各写一遍的话,一边判红一边不判,而两处的出口规则
    (试满 MAX_TRIES 就离开队列、不该判红)一模一样。`column` 只在这个模块里被
    两个字面量调用,没有外部输入拼进 SQL。
    """
    if not keys:
        return 0, 0
    fresh = shipped = 0
    ph = ','.join('?' * len(keys))
    for t in conn.execute(
            f"SELECT exported_hash='' AS fresh, count(*) AS n {queue} AND ({cond})"
            f" AND {column}<>'' AND key IN ({ph}) GROUP BY fresh",
            (*args, *extra, *keys)):
        if t['fresh']:
            fresh = t['n']
        else:
            shipped = t['n']
    return fresh, shipped
