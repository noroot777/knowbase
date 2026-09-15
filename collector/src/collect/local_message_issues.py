"""隔离坏推送；后续只核对这些行，内容改变后自动重新解析。"""

import hashlib
import json

from src.crawler.local_push_source import TABLE_BATCH, decode_push
from src.crawler.local_wechat import LocalPushContentError, LocalReaderError, parse_push

# 解析器升级需要重新尝试旧异常内容时递增此版本。
PARSER_VERSION = 1


class MessageIssues:
    def __init__(self, conn):
        self.conn = conn
        self.checked = set()

    def parse(self, row):
        key = row["source_key"], int(row["local_id"])
        self.checked.add(key)
        fingerprint = hashlib.sha256(json.dumps([
            PARSER_VERSION, row.get("content_hex"), row.get("compressed_hex"),
            row.get("compression"), row.get("create_time"),
        ]).encode()).hexdigest()
        previous = self.conn.execute(
            "SELECT content_hash FROM local_wechat_message_issues WHERE source_key=? AND local_id=?",
            key).fetchone()
        if previous and previous[0] == fingerprint:
            return []
        try:
            articles = parse_push(decode_push(row))
        except LocalPushContentError as exc:
            self.conn.execute("INSERT OR REPLACE INTO local_wechat_message_issues VALUES (?,?,?,?)",
                              (*key, fingerprint, str(exc)))
            return []
        self.conn.execute("DELETE FROM local_wechat_message_issues WHERE source_key=? AND local_id=?", key)
        return articles

    def recheck(self, source, snapshots, failed_sources, accept, on_error):
        by_key = {s["source_key"]: s for s in snapshots}
        pending = [dict(r) for r in self.conn.execute(
            "SELECT source_key, local_id FROM local_wechat_message_issues")
            if (r["source_key"], r["local_id"]) not in self.checked
            and r["source_key"] in by_key and r["source_key"] not in failed_sources]
        for filename in dict.fromkeys(by_key[r["source_key"]]["file"] for r in pending):
            group = [r for r in pending if by_key[r["source_key"]]["file"] == filename]
            for offset in range(0, len(group), TABLE_BATCH):
                batch = group[offset:offset + TABLE_BATCH]
                plans = [{**by_key[r["source_key"]], "ids": [r["local_id"]]} for r in batch]
                try:
                    found = set()
                    for row in source.page(plans):
                        found.add((row["source_key"], row["local_id"]))
                        accept(row, self.parse(row))
                    # 被微信移除的异常消息不再保留待核对记录。
                    self.conn.executemany(
                        "DELETE FROM local_wechat_message_issues WHERE source_key=? AND local_id=?",
                        [(r["source_key"], r["local_id"]) for r in batch
                         if (r["source_key"], r["local_id"]) not in found])
                    self.conn.commit()
                except LocalReaderError as exc:
                    failed_sources.update(r["source_key"] for r in batch)
                    on_error(str(exc))

    def count(self, sources):
        if not sources:
            return 0
        return self.conn.execute(
            "SELECT COUNT(*) FROM local_wechat_message_issues WHERE source_key IN ("
            + ",".join("?" for _ in sources) + ")", list(sources)).fetchone()[0]
