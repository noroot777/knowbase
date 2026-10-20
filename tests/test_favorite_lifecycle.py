"""收藏独立于来源文档:删除、重索引和升级都不许丢引文与用户记录。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from kb import scanner, store, store_migrate
from kb.config import Source
from tests.fav_snippet_fixture import ApiFixture, DOC, REAL_CONNECT
from tests.test_fav_snippet_migrate import _V2_DDL, _V3_DDL, _seed_old


class FavoriteLifecycleTests(ApiFixture):
    def test_all_document_removal_paths_keep_the_favorite_contents(self):
        for mode in ("direct", "explicit", "missing"):
            with self.subTest(mode=mode):
                self.conn.execute(
                    "INSERT OR IGNORE INTO documents(path, source, title, url)"
                    " VALUES (?, 'rss', '删除前的标题', 'https://e.org/deleted')", (DOC,))
                if mode == 'missing':
                    with mock.patch.object(scanner, 'VAULT_ROOT', self.vault):
                        doc = scanner._doc_from_file(
                            self.vault / DOC, Source('dr-gzh', (self.vault / DOC).parent))
                    store.upsert_document(self.conn, doc, [], '2026-10-19 12:00:00')
                self.conn.commit()
                response = self.snip(path=DOC, quote="文中一句", note="我的备注",
                                     prefix="前文", suffix="后文", quote_unique=1,
                                     anchor_exact=1, origin_title="收藏时的标题",
                                     origin_url="https://e.org/snapshot")
                self.assertEqual(200, response.status_code, response.text)
                favorite_id = response.json()["id"]
                self.conn.execute(
                    "UPDATE favorites SET action_json=?, status='done' WHERE id=?",
                    (json.dumps({"name": "行动卡"}), favorite_id))
                self.conn.commit()
                before = dict(self.conn.execute(
                    "SELECT * FROM favorites WHERE id=?", (favorite_id,)).fetchone())
                metadata = self.conn.execute(
                    "SELECT source,account,published,summary FROM documents WHERE path=?", (DOC,)).fetchone()
                if mode == "direct":
                    self.conn.execute("DELETE FROM documents WHERE path=?", (DOC,))
                elif mode == "explicit":
                    self.assertEqual(1, store.remove_documents(self.conn, [DOC]))
                else:
                    (self.vault / DOC).unlink()
                    report = scanner.ScanReport()
                    with mock.patch.object(scanner, 'VAULT_ROOT', self.vault), \
                         mock.patch.object(scanner, 'SOURCES', [
                             Source('dr-gzh', (self.vault / DOC).parent)]):
                        list(scanner.scan_all(report=report))
                    self.assertEqual(1, store.remove_missing_documents(self.conn, report))
                self.conn.commit()
                after = dict(self.conn.execute(
                    "SELECT * FROM favorites WHERE id=?", (favorite_id,)).fetchone())
                expected = {**before, "path": None}
                expected.update({f"origin_{column}": before[f"origin_{column}"] or metadata[column]
                                 for column in ("source", "account", "published", "summary")})
                self.assertEqual(expected, after)
                listed = self.listed("all")
                item = next(item for item in listed["favorites"] if item["id"] == favorite_id)
                self.assertEqual(("", "收藏时的标题", "https://e.org/snapshot", "我的备注"),
                                 (item["path"], item["title"], item["url"], item["note"]))
                self.assertEqual({"name": "行动卡"}, item["action"])
                self.assertEqual([], listed["doc_paths"])
                self.assertEqual(200, self.client.patch(
                    f"/api/favorites/{favorite_id}", json={"note": "删除原文后还能编辑"}).status_code)

    def test_whole_article_favorite_retains_the_last_known_metadata(self):
        response = self.client.post("/api/favorites/toggle", json={"path": DOC})
        self.assertEqual(200, response.status_code, response.text)
        favorite_id = self.listed()["favorites"][0]["id"]
        store.remove_documents(self.conn, [DOC])
        self.conn.commit()
        item = self.listed()["favorites"][0]
        self.assertEqual((favorite_id, "", "活着的那篇", "https://e.org/live"),
                         (item["id"], item["path"], item["title"], item["url"]))
        self.assertEqual([], self.listed()["doc_paths"])
        self.assertEqual(204, self.client.delete(f"/api/favorites/{favorite_id}").status_code)
        self.assertEqual([], self.listed()["favorites"])

    def test_reindexing_does_not_reattach_detached_favorites(self):
        self.snip(path=DOC, quote="旧正文的一句")
        store.remove_documents(self.conn, [DOC])
        self.conn.execute("INSERT INTO documents(path, source, title) VALUES (?, 'rss', '另一篇')",
                          (DOC,))
        self.conn.commit()
        item = self.listed()["favorites"][0]
        self.assertEqual(("", "活着的那篇"), (item["path"], item["title"]))
        self.assertEqual([], self.listed()["doc_paths"])

    def test_an_unrelated_document_stays_linked_and_invalid_paths_are_rejected(self):
        self.conn.execute("INSERT INTO documents(path, source) VALUES ('other.md', 'rss')")
        self.conn.execute("INSERT INTO favorites(path,created_at,updated_at)"
                          " VALUES ('other.md','x','x')")
        self.conn.commit()
        store.remove_documents(self.conn, [DOC])
        self.conn.commit()
        self.assertEqual(["other.md"], self.listed()["doc_paths"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO favorites(path,created_at,updated_at)"
                              " VALUES ('missing.md','x','x')")


class FavoriteLifecycleMigrationTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory(prefix="kb-fav-life-"))
        self.db = Path(self.root) / "kb.db"

    def seed_v4(self):
        ddl = _V3_DDL.split("CREATE TABLE favorites")[0] + store_migrate.FAVORITES_TABLE.replace(
            "ON DELETE SET NULL", "ON DELETE CASCADE")
        _seed_old(self.db, ddl)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE favorites SET origin_ref='原始身份', origin_title='标题快照',"
                " origin_url='https://e.org/snapshot', quote_unique=1, anchor_exact=1,"
                " doc_version='原始正文版本' WHERE id=7")
            conn.execute("INSERT INTO favorites(id,origin,origin_ref,origin_title,quote,"
                         " created_at,updated_at) VALUES (10,'daily','2026-10-18','日报',"
                         " '日报引文','x','x')")
            conn.execute("INSERT INTO favorites(id,created_at,updated_at) VALUES (100,'x','x')")
            conn.execute("DELETE FROM favorites WHERE id=100")

    def rows(self, conn):
        return {row["id"]: dict(row) for row in conn.execute("SELECT * FROM favorites")}

    def test_v4_migration_preserves_every_field_and_sequence(self):
        self.seed_v4()
        raw = sqlite3.connect(self.db)
        raw.row_factory = sqlite3.Row
        before = self.rows(raw)
        raw.close()
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        expected = {**before, 9: {**before[9], "path": None}}
        self.assertEqual(expected, self.rows(conn))
        self.assertEqual([], list(conn.execute("PRAGMA foreign_key_check")))
        metadata = conn.execute("SELECT source,account,published,summary FROM documents WHERE path=?",
                                (DOC,)).fetchone()
        conn.execute("DELETE FROM documents WHERE path=?", (DOC,))
        conn.commit()
        rows = self.rows(conn)
        self.assertEqual({7, 8, 9, 10}, set(rows))
        expected_row = {**before[7], "path": None}
        expected_row.update({f"origin_{column}": before[7][f"origin_{column}"] or metadata[column]
                             for column in ("source", "account", "published", "summary")})
        self.assertEqual(expected_row, rows[7])
        self.assertEqual((None, "活着的那篇", "https://e.org/live"),
                         (rows[8]["path"], rows[8]["origin_title"], rows[8]["origin_url"]))
        self.assertEqual(before[10], rows[10])
        next_id = conn.execute("INSERT INTO favorites(created_at,updated_at)"
                               " VALUES ('x','x')").lastrowid
        self.assertGreater(next_id, 100)

    def test_v1_v2_v3_also_acquire_the_retention_policy(self):
        v1 = _V3_DDL.split("CREATE TABLE favorites")[0] + """
        CREATE TABLE favorites (
          path TEXT PRIMARY KEY REFERENCES documents(path) ON DELETE CASCADE,
          created_at TEXT NOT NULL);
        INSERT INTO documents(path,title) VALUES ('old.md','老标题');
        INSERT INTO favorites(path,created_at) VALUES ('old.md','2026-01-01');
        """
        for index, ddl in enumerate((v1, _V2_DDL, _V3_DDL)):
            with self.subTest(version=index + 1):
                db = Path(self.root) / f"v{index + 1}.db"
                if index == 0:
                    with sqlite3.connect(db) as raw:
                        raw.executescript(ddl)
                else:
                    _seed_old(db, ddl)
                conn = REAL_CONNECT(db)
                try:
                    before = conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
                    conn.execute("DELETE FROM documents")
                    self.assertEqual(before, conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0])
                    self.assertEqual(0, conn.execute(
                        "SELECT COUNT(*) FROM favorites WHERE path IS NOT NULL").fetchone()[0])
                    self.assertEqual([], list(conn.execute("PRAGMA foreign_key_check")))
                finally:
                    conn.close()

    def test_v4_first_connections_can_migrate_concurrently(self):
        self.seed_v4()

        def connect_and_count(_index):
            conn = REAL_CONNECT(self.db)
            try:
                return conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=12) as executor:
            self.assertEqual([4] * 12, list(executor.map(connect_and_count, range(12))))

    def test_failed_rebuild_rolls_back_the_schema_rows_and_sequence(self):
        self.seed_v4()
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        before = self.rows(conn)

        def deny_drop(action, table, *_rest):
            return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE
                    and table == "favorites_v4" else sqlite3.SQLITE_OK)

        conn.set_authorizer(deny_drop)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                store_migrate.migrate_favorites(conn)
        finally:
            conn.set_authorizer(None)
        self.assertEqual(before, self.rows(conn))
        self.assertEqual("CASCADE", conn.execute("PRAGMA foreign_key_list(favorites)").fetchone()[6])
        self.assertEqual([], list(conn.execute("SELECT name FROM sqlite_master"
                                               " WHERE name='favorites_v4'")))
        self.assertEqual(100, conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='favorites'").fetchone()[0])
        store_migrate.migrate_favorites(conn)
        self.assertEqual("SET NULL", conn.execute("PRAGMA foreign_key_list(favorites)").fetchone()[6])

    def test_current_schema_needs_no_rebuild_or_write_lock(self):
        self.seed_v4()
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        before = self.rows(conn)
        statements = []
        conn.set_trace_callback(statements.append)
        store_migrate.migrate_favorites(conn)
        conn.set_trace_callback(None)
        self.assertEqual(before, self.rows(conn))
        self.assertFalse(any(statement.startswith(("BEGIN", "ALTER", "DROP", "INSERT"))
                             for statement in statements), statements)


if __name__ == "__main__":
    unittest.main()
