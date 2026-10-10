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

    地址是 `db_uri` 给的规范 URI,**承重的是这一道**:路径里的 `#`/`?` 会被 URI
    解析当成 fragment / query —— 那不是报错,是**打开了一个名字更短的别的库**,
    顺带在那儿建一个文件,`mode=ro` 也一起丢了(2026-10-11 审查实测,数据目录叫
    `reader#data` / `reader?data` 时各是一种)。`conn` 自己要带 SQLITE_OPEN_URI
    才认这个地址,那是 `store.connect` 的 `uri=True` 负责的事。

    **参数绑定今天是冗余带,如实记着**:`as_uri()` 会把单引号一并编码成 `%27`,
    所以把地址拼进 SQL 字符串这个注入**红 0 条**(实测)。留着是构造上的 ——
    「地址怎么来的」和「地址怎么进 SQL」是两件事,不该让后者押在前者一定会
    编码上;而审查里那一档 `reader's data` 的 `near "s": syntax error` 正是
    两道都没有时的样子。

    进来之前先 DETACH 一次:上一次如果死在异常里没摘干净,同名 ATTACH 会报
    「already in use」,而那跟这次要回答的问题毫无关系。
    """
    from kb.feeds import registry as reg
    from kb.store import db_uri
    with suppress(Exception):
        conn.execute(f"DETACH DATABASE {FEEDS_ALIAS}")
    # 文件不在就别往下走:下一句 `reg.db()` 会**把它建出来**(建表 + 补种默认
    # 订阅),于是「库被删了」这种该让人看一眼的状态被静默抹平成「一条开着
    # 提取的订阅都没有」,所有 feeds 文档从此不提取而面板一切正常。
    if not reg.DB_PATH.exists():
        raise FileNotFoundError(f"订阅库不在:{reg.DB_PATH}")
    # **补列迁移要先于这次只读 JOIN 发生。** `subscriptions.extract` 是 ALTER
    # 补出来的,而补列只在 `reg.db()` 里跑 —— 升级后第一条命令如果是
    # `kb extract`(或管道的并行存量提取),它压根没碰过注册库,JOIN 当场
    # `no such column: s.extract`,退出码 1;而先打开一次面板再跑同一条命令
    # 就好了(2026-10-11 审查实测)。这里借 `reg.db()` 跑一遍:它幂等,口径也
    # 只有那一份(建表 + 补列 + 按修订号补种都在它的写锁里)。
    with reg.db():
        pass
    conn.execute(f"ATTACH DATABASE ? AS {FEEDS_ALIAS}",
                 (db_uri(reg.DB_PATH) + "?mode=ro",))
    try:
        yield
    finally:
        with suppress(Exception):
            conn.execute(f"DETACH DATABASE {FEEDS_ALIAS}")
