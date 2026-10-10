# -*- coding: utf-8 -*-
"""订阅级的「要不要买提取」:feeds 来源的那道闸自己的零件。

判据本身留在 `kb.extract.pending_docs` —— 它是提取队列和面板那个「当前待处理
N 项」的**唯一口径**,搬走等于把口径拆成两份。这里只放它用到的几个零件:
跨库 ATTACH、两个库之间那条路径契约、feeds 来源的名字。**单纯因为 700 行上限**
(见 CLAUDE.md「代码文件行数上限」),没有别的理由。

搬过来的名字一律在 `kb.extract` 那边**原样再导出**,所以 `extract._FEED_DOC_PATH`
这类写法一个字都不用改。
"""

from __future__ import annotations

from contextlib import contextmanager, suppress

# feeds 那几类来源(rss/social/web/digest)的 vault 路径:`sync._export` 写的是
# `VAULT_ROOT/source/<kind>/<item_key>.md`,而 `documents.path` 存的是它相对
# VAULT_ROOT 的那一截。订阅开关在 feeds.db、文档在 kb.db,两边唯一能对上的
# 就是这个拼法 —— **它是一条隐式契约**:`_export` 哪天改了落盘路径,这里会
# 静默匹配不上,后果是那条订阅即使开着提取也一篇都排不进队列(方向上是安全
# 的,`extracted_at` 仍是 NULL,改回来就自动回队列)。回归钉在
# `tests/test_extract_optin.py::PathContractTests`。
FEED_DOC_PATH = "'source/' || s.kind || '/' || si.item_key || '.md'"
# ATTACH 的别名。取一个不会和 kb.db 自己的 schema 撞的名字。
FEEDS_ALIAS = "feedsreg"


def feed_kinds() -> set[str]:
    """feeds 那几类来源的名字。`documents.source` 对它们存的就是 kind
    (`config.SOURCES` 里 `Source(name=kind, ...)`),所以两边能直接比。"""
    from kb.source_types import feed_types
    return set(feed_types())


@contextmanager
def attached_feeds(conn):
    """把 feeds.db 以**只读**挂到这条 kb.db 连接上,出去时一定摘掉。

    两个库不合并是想清楚之后的选择(并行写窗口、恢复语义、备份体量,见
    `docs/decisions/extract-optin.md`)。这里只有一条硬约束:**ATTACH 只用于
    只读 JOIN,写入绝不跨库** —— WAL 下涉及多个库的事务不是原子的(SQLite
    官方行为,两个库现在都是 WAL),跨库写会在崩溃点留下半个结果。`mode=ro`
    把这件事从「约定」变成「物理上做不到」。

    进来之前先 DETACH 一次:上一次如果死在异常里没摘干净,同名 ATTACH 会报
    「already in use」,而那跟这次要回答的问题毫无关系。
    """
    from kb.feeds.registry import DB_PATH as FEEDS_DB
    with suppress(Exception):
        conn.execute(f"DETACH DATABASE {FEEDS_ALIAS}")
    # URI 之前先确认真实文件在:万一某个 SQLite 构建没开 URI 处理,这个字符串
    # 会被当成字面文件名,ATTACH 一个不存在的名字等于**凭空建一个空库**,
    # 接着查 subscriptions 报 no such table —— 抛出去是对的,但别留下垃圾文件。
    conn.execute(f"ATTACH DATABASE 'file:{FEEDS_DB}?mode=ro' AS {FEEDS_ALIAS}")
    try:
        yield
    finally:
        with suppress(Exception):
            conn.execute(f"DETACH DATABASE {FEEDS_ALIAS}")
