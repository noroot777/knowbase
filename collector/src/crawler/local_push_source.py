"""经公共 Reader 的只读 SQL 批量读取公众号，游标在 SQL LIMIT 之前生效。"""

from __future__ import annotations

import hashlib
import re

from src.crawler.local_wechat import LocalReaderError, LocalPushContentError

PAGE_SIZE = 500
TABLE_BATCH = 80  # 避免 SQLite UNION / 系统命令行长度上限。


def _digest(value):
    return hashlib.sha256(str(value or "").encode()).hexdigest()


def _anchor(table, where="", order="ASC"):
    # 只在内存中比较边界消息；落盘的是摘要，不保存 XML。
    return (f"COALESCE((SELECT CAST(local_id AS TEXT) || ':' || "
            f"COALESCE(server_id,0) || ':' || COALESCE(create_time,0) || ':' || "
            f"hex(message_content) || ':' || hex(compress_content) FROM \"{table}\" "
            f"{where} ORDER BY local_id {order} LIMIT 1),'')")


class LocalPushSource:
    def __init__(self, reader):
        self.reader = reader

    def sql(self, filename, query, limit=PAGE_SIZE):
        rows = self.reader.call("sql", query, "--subdir", "message", "--file", filename,
                                "--limit", str(limit)).get("rows")
        if not isinstance(rows, list):
            raise LocalReaderError("微信读取器缺少只读 SQL 结果，请更新 wechat-cli")
        return rows

    def snapshots(self, checkpoints):
        # 只列公众号对应的 Msg 表，绝不读取私人或群聊内容。
        tables = {"Msg_" + hashlib.md5(s["username"].encode()).hexdigest(): s
                  for s in self.reader.accounts()
                  if re.fullmatch(r"gh_[A-Za-z0-9_]+", s.get("username", ""))}
        databases = self.reader.call("schema", "--subdir", "message").get("databases")
        if not isinstance(databases, list) or not databases:
            raise LocalReaderError("微信读取器未提供消息数据库结构，请更新 wechat-cli")
        for database in databases:
            filename = database["file"]
            # source_key 会进入只读 SQL 字面量，仅允许标准数据库文件名。
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
                raise LocalReaderError("微信消息数据库文件名不兼容")
            selected = sorted(t["name"] for t in database.get("tables", [])
                              if t["name"] in tables)
            for start in range(0, len(selected), TABLE_BATCH):
                queries = []
                for table in selected[start:start + TABLE_BATCH]:
                    key = filename + ":" + table
                    old = checkpoints.get(key, {})
                    cursor = max(0, int(old.get("max_id", 0)))
                    queries.append(
                        f"SELECT '{key}' AS source_key, COUNT(*) AS row_count, "
                        f"COALESCE(MIN(local_id),0) AS min_id, COALESCE(MAX(local_id),0) AS max_id, "
                        f"COUNT(CASE WHEN local_id>{cursor} THEN 1 END) AS new_count, "
                        f"{_anchor(table)} AS first_anchor, "
                        f"{_anchor(table, order='DESC')} AS last_anchor, "
                        f"{_anchor(table, f'WHERE local_id={cursor}')} AS boundary_anchor "
                        f'FROM "{table}"')
                rows = self.sql(filename, " UNION ALL ".join(queries), TABLE_BATCH)
                if len(rows) != len(queries):
                    raise LocalReaderError("微信消息统计不完整，未推进读取游标")
                for row in rows:
                    for field in ("first", "last", "boundary"):
                        row[field + "_hash"] = _digest(row.pop(field + "_anchor"))
                    row["file"] = filename
                    row["table"] = row["source_key"].split(":", 1)[1]
                    # 名称仅供覆盖诊断，不能作为采集文章的身份映射。
                    row["session_name"] = tables[row["table"]].get("display_name", "")
                    yield row

    def page(self, plans):
        queries = []
        for plan in plans:
            if "ids" in plan:
                where = "local_id IN (" + ",".join(str(int(i)) for i in plan["ids"]) + ")"
            else:
                if plan["after"] >= plan["max_id"]:
                    continue
                where = f"local_id>{int(plan['after'])} AND local_id<={int(plan['max_id'])}"
            queries.append(
                f"SELECT '{plan['source_key']}' AS source_key, local_id, create_time, "
                f"hex(message_content) AS content_hex, hex(compress_content) AS compressed_hex, "
                f'WCDB_CT_message_content AS compression FROM "{plan["table"]}" '
                f"WHERE {where}")
        if not queries:
            return []
        return self.sql(plans[0]["file"], " UNION ALL ".join(queries)
                        + f" ORDER BY source_key, local_id LIMIT {PAGE_SIZE}")


def decode_push(row):
    """WCDB 文本可能是 zstd BLOB；SQL hex 避免 JSON 对 bytes 的有损转换。"""
    try:
        raw = bytes.fromhex(row.get("content_hex") or row.get("compressed_hex") or "")
        if raw and (row.get("compression") == 4 or raw.startswith(b"\x28\xb5\x2f\xfd")):
            import zstandard

            raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=16 * 1024 * 1024)
        return {"text": raw.decode("utf-8"), "create_time": int(row.get("create_time") or 0)}
    except ImportError:
        raise LocalReaderError("缺少 zstandard 依赖，请在 collector 运行 uv sync") from None
    except Exception:
        raise LocalPushContentError("公众号推送内容解码失败") from None
