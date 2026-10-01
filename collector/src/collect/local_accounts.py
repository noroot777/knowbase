"""从本机微信的公众号会话里找订阅候选:加一个号不必再登录公众平台。

原来「添加公众号」只有一条路:扫码登录公众平台 → 调 search_biz 搜名字 →
拿它返回的 fakeid。那条路要维护一份全局扫码凭证,过期就谁也加不了号,而
本机微信里**已经关注的那些号**本来就躺在会话列表里。

归属(`__biz`,也就是库里的 fakeid)优先从发现索引 `local_wechat_articles`
里取:那张表是每轮本地采集顺手建的,覆盖本机**所有**公众号会话(不只订阅
的那几个),一条 SQL 就能拿到,一条推送都不用翻。实测 280 个会话里 258 个
直接解得出,会话与 biz 严格一对一。

索引里没有的(从没跑过本地同步、或者这个号本机只有服务通知、没有图文)才
现翻一次它的推送,而且只翻查询命中的那几个、还有上限——翻一个号是一次
读取器子进程调用,不设限的话一次「搜索」要几分钟。
"""

from __future__ import annotations

import hashlib

from src.crawler.local_wechat import LocalReader, LocalReaderError, session_biz

# 一次 discover 最多现场解析几个会话的归属。只在给了查询词时才会用到。
RESOLVE_LIMIT = 12
# 返回多少条候选。面板上总是带着查询词,这个上限只在终端里裸列时起作用。
MAX_ROWS = 200


def _table(username: str) -> str:
    """会话 → 它在 message 库里的 Msg 表名(与 LocalPushSource 同一个口径)。"""
    return "Msg_" + hashlib.md5(username.encode()).hexdigest()


def indexed_biz(conn) -> dict[str, str]:
    """发现索引里「Msg 表 → __biz」的映射。

    同一张表理论上可能对应多个 biz(改绑、库重建),实测为零。真出现时按
    文章条数多的那个算——升序遍历、后写覆盖。
    """
    rows = conn.execute(
        "SELECT source_key, biz, COUNT(*) AS n FROM local_wechat_articles "
        "GROUP BY source_key, biz ORDER BY n"
    ).fetchall()
    return {r["source_key"].split(":", 1)[1]: r["biz"] for r in rows}


def discover(repo, query: str = "", *, reader=None,
             resolve_limit: int = RESOLVE_LIMIT) -> list[dict]:
    """本机公众号会话 → 订阅候选,按最近有推送排在前面。

    每行:name(微信里显示的名字)、username(gh_ 开头的会话号)、fakeid
    (`__biz`,解不出来是空串)、subscribed(已经在订阅列表里)、stored_name
    (已订阅时库里存的档案键,可能是个旧名字)、last_at。
    """
    reader = reader or LocalReader()
    reader.check()
    sessions = reader.accounts()
    index = indexed_biz(repo.conn)
    subscribed = {r["fakeid"]: r["name"] for r in repo.conn.execute(
        "SELECT name, fakeid FROM accounts WHERE removed=0 AND fakeid<>''")}

    q = (query or "").strip().lower()
    rows = []
    for session in sessions:
        username = session.get("username") or ""
        name = (session.get("display_name") or "").strip()
        if q and q not in name.lower() and q not in username.lower():
            continue
        rows.append({"name": name, "username": username,
                     "fakeid": index.get(_table(username), ""),
                     "last_at": int(session.get("last_timestamp") or 0)})
    rows.sort(key=lambda r: -r["last_at"])
    del rows[MAX_ROWS:]

    if q:
        budget = resolve_limit
        for row in rows:
            if row["fakeid"] or budget <= 0:
                continue
            budget -= 1
            try:
                # 坏推送跳过继续往下找,别让一条解析不了的消息把这个号判死。
                row["fakeid"] = session_biz(reader, row["username"],
                                            lambda _message: None) or ""
            except LocalReaderError:
                # 单个会话读不动不该让整次搜索失败:它只是解不出归属,
                # 下面会如实标成「本机暂无可解析的图文推送」。
                pass

    for row in rows:
        row["subscribed"] = bool(row["fakeid"]) and row["fakeid"] in subscribed
        row["stored_name"] = subscribed.get(row["fakeid"], "")
    return rows
