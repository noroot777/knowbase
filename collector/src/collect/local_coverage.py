"""区分没有可用文章与推送不可读；名称仅能产生明确标注的不确定诊断。"""

from collections import defaultdict


def discovery_issues(conn, accounts, snapshots, active, covered):
    active = set(active)
    source_biz = defaultdict(set)
    for row in conn.execute("SELECT DISTINCT source_key,biz FROM local_wechat_articles"):
        if row["source_key"] in active:
            source_biz[row["source_key"]].add(row["biz"])
    session_names = {s["source_key"]: s.get("session_name") for s in snapshots}
    per_account = defaultdict(dict)
    matched = set()
    rows = conn.execute(
        "SELECT i.source_key,i.local_id,b.biz FROM local_wechat_message_issues i "
        "LEFT JOIN local_wechat_issue_biz b USING (source_key,local_id)")
    for row in rows:
        source = row["source_key"]
        if source not in active:
            continue
        known = {row["biz"]} if row["biz"] else source_biz[source]
        key = source, row["local_id"]
        for account in accounts:
            identified = account["fakeid"] in known
            same_name = (not known and session_names.get(source)
                         and session_names[source] in {account["name"], account.get("display_name")})
            if identified or same_name:
                per_account[account["name"]][key] = bool(identified)
                matched.add(key)
    return {
        "unreadable_messages": len(matched),
        "unreadable_accounts": [
            {"name": a["name"], "messages": len(per_account[a["name"]]),
             "identity_confirmed": all(per_account[a["name"]].values())}
            for a in accounts if a["name"] not in covered and per_account[a["name"]]
        ],
    }
