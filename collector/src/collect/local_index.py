"""公众号文章发现索引：先增量入索引，再用发布时间和 biz 查询下载范围。"""

from __future__ import annotations

import json

from src.crawler.local_push_source import LocalPushSource, TABLE_BATCH, decode_push
from src.crawler.local_wechat import LocalReaderError, parse_push


def _checkpoint(snapshot):
    return {k: snapshot[k] for k in ("row_count", "min_id", "max_id", "first_hash", "last_hash")}


def _start(snapshot, old):
    # local_id 是每张 Msg 表的 AUTOINCREMENT 主键，与发布时间无关。
    # 同名库重建、旧 ID 回填、删行或边界内容改变时，重扫该表，避免误用旧游标。
    if (old and snapshot["min_id"] == old["min_id"]
            and snapshot["first_hash"] == old["first_hash"]
            and snapshot["boundary_hash"] == old["last_hash"]
            and snapshot["row_count"] == old["row_count"] + snapshot["new_count"]):
        return old["max_id"]
    return 0


class ArticleIndex:
    def __init__(self, repo):
        self.conn = repo.conn

    def refresh(self, reader, progress, on_error):
        checkpoints = {r["source_key"]: json.loads(r["checkpoint"]) for r in self.conn.execute(
            "SELECT source_key, checkpoint FROM local_wechat_sources")}
        source = LocalPushSource(reader)
        stats = {"phase": "discovery", "scanned_messages": 0, "message_total": 0,
                 "indexed_articles": 0, "initial_index": not checkpoints}
        progress(dict(stats))
        snapshots = list(source.snapshots(checkpoints))
        active = [s["source_key"] for s in snapshots]
        plans = []
        for snapshot in snapshots:
            old = checkpoints.get(snapshot["source_key"])
            if old == _checkpoint(snapshot):
                continue
            start = _start(snapshot, old)
            count = snapshot["new_count"] if start else snapshot["row_count"]
            plans.append({**snapshot, "after": start, "expected": count, "read": 0,
                          "failed": False, "reset": not start, "seen": set()})
            stats["message_total"] += count
        progress(dict(stats))
        # 批量 SQL 跨公众号读增量；不逐号启动正文抓取，也不输出无关公众号名称。
        for offset in range(0, len(plans), TABLE_BATCH):
            # 一次 SQL 只能访问一个已配置文件，因此再次按分片分组。
            batch = plans[offset:offset + TABLE_BATCH]
            for filename in dict.fromkeys(p["file"] for p in batch):
                group = [p for p in batch if p["file"] == filename]
                self._read_group(source, group, stats, progress, on_error)
        return active

    def _read_group(self, source, plans, stats, progress, on_error):
        by_key = {p["source_key"]: p for p in plans}
        try:
            while any(p["after"] < p["max_id"] for p in plans):
                rows = source.page(plans)
                if not rows:
                    raise LocalReaderError("公众号推送分页不完整，未推进读取游标")
                for row in rows:
                    plan = by_key[row["source_key"]]
                    local_id = int(row["local_id"])
                    if not plan["after"] < local_id <= plan["max_id"]:
                        raise LocalReaderError("公众号推送分页游标无效")
                    plan["after"] = local_id
                    plan["read"] += 1
                    stats["scanned_messages"] += 1
                    try:
                        articles = parse_push(decode_push(row))
                    except LocalReaderError as exc:
                        plan["failed"] = True
                        on_error(str(exc))
                        continue
                    for article in articles:
                        plan["seen"].add(article["id"])
                        self.conn.execute(
                            "INSERT INTO local_wechat_articles "
                            "(source_key,id,biz,create_time,message_time,local_id,article_json) "
                            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_key,id) DO UPDATE SET "
                            "biz=excluded.biz, create_time=excluded.create_time, "
                            "message_time=excluded.message_time, local_id=excluded.local_id, "
                            "article_json=excluded.article_json",
                            (row["source_key"], article["id"], article["biz"], article["create_time"],
                             row.get("create_time") or 0, local_id, json.dumps(article, ensure_ascii=False)))
                        stats["indexed_articles"] += 1
                # 文章索引与正文失败分离。半途失败可留下有效元数据，但不推进失败来源。
                self.conn.commit()
                progress(dict(stats))
        except LocalReaderError as exc:
            for plan in plans:
                plan["failed"] = True
            on_error(str(exc))
        for plan in plans:
            if not plan["failed"] and plan["read"] == plan["expected"]:
                if plan["reset"]:
                    obsolete = [(plan["source_key"], row[0]) for row in self.conn.execute(
                        "SELECT id FROM local_wechat_articles WHERE source_key=?",
                        (plan["source_key"],)) if row[0] not in plan["seen"]]
                    self.conn.executemany("DELETE FROM local_wechat_articles WHERE source_key=? AND id=?",
                                          obsolete)
                self.conn.execute(
                    "INSERT OR REPLACE INTO local_wechat_sources VALUES (?,?)",
                    (plan["source_key"], json.dumps(_checkpoint(plan))))
            elif not plan["failed"]:
                on_error("公众号推送数量在读取期间变化，下一次同步将重新校验")
        self.conn.commit()

    def covered_accounts(self, accounts, active):
        if not active:
            return set()
        rows = self.conn.execute(
            "SELECT DISTINCT biz FROM local_wechat_articles WHERE source_key IN ("
            + ",".join("?" for _ in active) + ")", active)
        known = {r["biz"] for r in rows}
        return {a["name"] for a in accounts if a["fakeid"] in known}

    def query(self, accounts, active, since="", until=""):
        if not accounts or not active:
            return []
        biz_names = {a["fakeid"]: a["name"] for a in accounts}
        clauses = ["biz IN (" + ",".join("?" for _ in biz_names) + ")",
                   "source_key IN (" + ",".join("?" for _ in active) + ")"]
        params = [*biz_names, *active]
        if since:
            clauses.append("create_time >= ?")
            params.append(since)
        if until:
            clauses.append("create_time < date(?, '+1 day')")
            params.append(until)
        rows = self.conn.execute(
            "SELECT biz, article_json FROM local_wechat_articles WHERE " + " AND ".join(clauses)
            + " ORDER BY message_time DESC, local_id DESC, source_key", params)
        seen, result = set(), []
        for row in rows:
            article = json.loads(row["article_json"])
            if article["id"] not in seen:
                seen.add(article["id"])
                result.append((biz_names[row["biz"]], article))
        return result
