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
    matched, unattributed, uncertain = set(), set(), set()
    rows = conn.execute(
        "SELECT i.source_key,i.local_id,b.biz FROM local_wechat_message_issues i "
        "LEFT JOIN local_wechat_issue_biz b USING (source_key,local_id)")
    for row in rows:
        source = row["source_key"]
        if source not in active:
            continue
        known = {row["biz"]} if row["biz"] else source_biz[source]
        key = source, row["local_id"]
        candidates = [a for a in accounts if a["fakeid"] in known] if known else [
            a for a in accounts if session_names.get(source)
            and session_names[source] in {a["name"], a.get("display_name")}]
        if known and not candidates:
            continue  # 能确认属于未选公众号，保持静默。
        if len(candidates) == 1 and len(known) <= 1:
            per_account[candidates[0]["name"]][key] = bool(known)
            matched.add(key)
        else:
            # 不把同一条异常重复分给同名账号，也不猜测改名/解码失败消息的归属。
            unattributed.add(key)
            # 没有关联线索的消息只留汇总，不能据此改变全部未覆盖账号的状态。
            uncertain.update(a["name"] for a in candidates)
    uncertain -= covered | per_account.keys()
    return {
        "unreadable_messages": len(matched),
        "unattributed_messages": len(unattributed),
        "coverage_unknown_accounts": [a["name"] for a in accounts if a["name"] in uncertain],
        "missing_accounts": [a["name"] for a in accounts
                             if a["name"] not in covered | per_account.keys() | uncertain],
        "unreadable_accounts": [
            {"name": a["name"], "messages": len(per_account[a["name"]]),
             "identity_confirmed": all(per_account[a["name"]].values())}
            for a in accounts if a["name"] in per_account
        ],
    }
