"""Exercise the production read-only SQL against isolated synthetic WeChat shards."""

import hashlib
import sqlite3
from unittest.mock import Mock

from src.crawler.local_wechat import LocalReader


class SqlReader(LocalReader):
    def __init__(self):
        self.shards = {}
        self.sessions = []
        self.check = Mock()
        self.accounts = Mock(side_effect=lambda: self.sessions)
        self.call = Mock(side_effect=self._call)

    def add(self, username, messages, filename="biz_message_0.db"):
        db = self.shards.setdefault(filename, sqlite3.connect(":memory:"))
        db.row_factory = sqlite3.Row
        table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        db.execute(f'''CREATE TABLE IF NOT EXISTS "{table}" (
            local_id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER,
            create_time INTEGER, message_content TEXT, compress_content TEXT,
            WCDB_CT_message_content INTEGER)''')
        if username not in {s["username"] for s in self.sessions}:
            self.sessions.append({"username": username})
        for message in messages:
            db.execute(f'INSERT OR REPLACE INTO "{table}" VALUES (?,?,?,?,?,?)', (
                message.get("local_id"), message.get("server_id", 1),
                message.get("create_time", 1), message.get("text", ""),
                message.get("compressed", ""), message.get("compression")))
        db.commit()
        return db, table

    def _call(self, command, *args):
        if command == "schema":
            return {"databases": [{"file": filename, "tables": [dict(r) for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table'")]} for filename, db in self.shards.items()]}
        if command == "sql":
            filename = args[args.index("--file") + 1]
            limit = int(args[args.index("--limit") + 1])
            return {"rows": [dict(r) for r in self.shards[filename].execute(args[0]).fetchmany(limit)]}
        raise AssertionError(command)

    @property
    def content_queries(self):
        return [c for c in self.call.call_args_list if c.args[0] == "sql" and "content_hex" in c.args[1]]

    def close(self):
        for db in self.shards.values():
            db.close()


class TimelineFixture(SqlReader):
    """Keep old body/identity regressions' timeline fixtures on the new SQL path."""
    def __init__(self, reader):
        super().__init__()
        self.reader = reader

    def _call(self, command, *args):
        if command == "schema":
            self.close()
            self.shards = {}
            for session in self.reader.accounts():
                history = []
                for offset in range(0, 100000, 500):
                    page = self.reader.messages(session["username"], offset=offset)
                    history.extend(page)
                    if len(page) < 500:
                        break
                self.add(session["username"], list(reversed(history)))
        return super()._call(command, *args)
