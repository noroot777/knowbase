"""来源解除关联后的显示元数据、权威索引状态及存量快照迁移。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from kb import store, store_migrate
from tests.fav_snippet_fixture import ApiFixture, DOC, REAL_CONNECT


METADATA = ("source", "account", "published", "summary")
OLD_TRIGGER = """
CREATE TRIGGER favorites_document_snapshot BEFORE DELETE ON documents
BEGIN
  UPDATE favorites
  SET origin_title = CASE WHEN origin_title = '' THEN OLD.title ELSE origin_title END,
      origin_url = CASE WHEN origin_url = '' THEN OLD.url ELSE origin_url END
  WHERE path = OLD.path AND (origin_title = '' OR origin_url = '');
END;
"""


class FavoriteMetadataTests(ApiFixture):
    def test_document_removal_preserves_display_metadata_and_reports_availability(self):
        self.client.post("/api/favorites/toggle", json={"path": DOC}).raise_for_status()
        before = self.listed()["favorites"][0]
        self.assertIs(before["document_available"], True)
        store.remove_documents(self.conn, [DOC])
        self.conn.commit()
        after = self.listed()["favorites"][0]
        self.assertIs(after["document_available"], False)
        for column in METADATA:
            self.assertEqual(before[column], after[column], column)
        self.assertEqual(("dr-gzh", "某号", "2026-10-01", "一句摘要"),
                         tuple(after[column] for column in METADATA))
        self.conn.execute("INSERT INTO documents(path,source,title) VALUES (?, 'rss', '另一篇')", (DOC,))
        self.conn.commit()
        self.assertEqual(after, self.listed()["favorites"][0])

    def test_existing_snapshots_and_user_timestamps_are_not_overwritten(self):
        self.snip(path=DOC, quote="文中一句")
        self.conn.execute("UPDATE favorites SET origin_source='rss',origin_account='快照作者',"
                          " origin_published='2025-01-01',origin_summary='已有摘要'")
        self.conn.commit()
        before = dict(self.conn.execute("SELECT * FROM favorites").fetchone())
        self.assertEqual("某号", self.listed()["favorites"][0]["account"])
        store.remove_documents(self.conn, [DOC])
        self.conn.commit()
        after = dict(self.conn.execute("SELECT * FROM favorites").fetchone())
        for column in (*store_migrate._FAV_METADATA, "created_at", "updated_at"):
            self.assertEqual(before[column], after[column], column)
        self.assertEqual("快照作者", self.listed()["favorites"][0]["account"])

    def test_legacy_detached_rows_remain_honest_without_invented_metadata(self):
        self.conn.execute("INSERT INTO favorites(origin_title,created_at,updated_at)"
                          " VALUES ('旧收藏','x','x')")
        self.conn.commit()
        item = self.listed()["favorites"][0]
        self.assertIs(item["document_available"], False)
        self.assertIsNone(item["source"])
        self.assertEqual(("", "", ""), (item["account"], item["published"], item["summary"]))

    def test_non_document_origins_are_not_reported_as_indexed_documents(self):
        self.conn.execute("INSERT INTO favorites(origin,quote,created_at,updated_at)"
                          " VALUES ('daily','一段摘录','x','x')")
        self.conn.commit()
        item = self.listed()["favorites"][0]
        self.assertEqual("daily", item["origin"])
        self.assertIs(item["document_available"], False)


class FavoriteMetadataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="kb-fav-meta-")))
        self.db = self.root / "kb.db"

    def seed_v5(self):
        conn = REAL_CONNECT(self.db)
        conn.execute("INSERT INTO documents(path,source,title,account,published,summary)"
                     " VALUES ('a.md','rss','标题','作者','2026-10-01','摘要')")
        conn.execute("INSERT INTO favorites(id,path,note,created_at,updated_at)"
                     " VALUES (9,'a.md','笔记','created','updated')")
        conn.execute("INSERT INTO favorites(id,created_at,updated_at) VALUES (88,'x','x')")
        conn.execute("DELETE FROM favorites WHERE id=88")
        conn.execute("DROP TRIGGER favorites_document_snapshot")
        for column in store_migrate._FAV_METADATA:
            conn.execute(f"ALTER TABLE favorites DROP COLUMN {column}")
        conn.executescript(OLD_TRIGGER)
        conn.close()

    def test_v5_upgrade_preserves_rows_sequence_and_acquires_the_complete_snapshot(self):
        self.seed_v5()
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        before = dict(conn.execute("SELECT * FROM favorites").fetchone())
        self.assertEqual(88, conn.execute("SELECT seq FROM sqlite_sequence WHERE name='favorites'").fetchone()[0])
        store.remove_documents(conn, ["a.md"])
        after = dict(conn.execute("SELECT * FROM favorites").fetchone())
        self.assertEqual((9, "笔记", "created", "updated"),
                         tuple(after[column] for column in ("id", "note", "created_at", "updated_at")))
        self.assertEqual(("rss", "作者", "2026-10-01", "摘要"),
                         tuple(after[f"origin_{column}"] for column in METADATA))
        self.assertEqual("", before["origin_summary"])
        self.assertIsNone(after["path"])
        self.assertEqual([], list(conn.execute("PRAGMA foreign_key_check")))

    def test_metadata_columns_and_trigger_upgrade_roll_back_together(self):
        self.seed_v5()
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        before = conn.execute("SELECT sql FROM sqlite_master WHERE name='favorites_document_snapshot'").fetchone()[0]

        def deny_trigger(action, name, *_rest):
            return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TRIGGER
                    and name == "favorites_document_snapshot" else sqlite3.SQLITE_OK)

        conn.set_authorizer(deny_trigger)
        with self.assertRaises(sqlite3.DatabaseError):
            store_migrate.migrate_favorites(conn)
        conn.set_authorizer(None)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(favorites)")}
        self.assertFalse(set(store_migrate._FAV_METADATA) & columns)
        self.assertEqual(before, conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='favorites_document_snapshot'").fetchone()[0])
        self.assertEqual("笔记", conn.execute("SELECT note FROM favorites WHERE id=9").fetchone()[0])
        store_migrate.migrate_favorites(conn)
        self.assertTrue(set(store_migrate._FAV_METADATA).issubset(
            {row["name"] for row in conn.execute("PRAGMA table_info(favorites)")}))

    def test_existing_columns_do_not_hide_an_outdated_snapshot_trigger(self):
        self.seed_v5()
        conn = REAL_CONNECT(self.db)
        conn.execute("DROP TRIGGER favorites_document_snapshot")
        conn.executescript(OLD_TRIGGER)
        conn.close()
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        store.remove_documents(conn, ["a.md"])
        self.assertEqual("摘要", conn.execute("SELECT origin_summary FROM favorites WHERE id=9").fetchone()[0])

    def test_v5_metadata_first_connections_can_migrate_concurrently(self):
        self.seed_v5()

        def migrate(_index):
            conn = REAL_CONNECT(self.db)
            try:
                return conn.execute("SELECT origin_summary FROM favorites WHERE id=9").fetchone()[0]
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=12) as executor:
            self.assertEqual([""] * 12, list(executor.map(migrate, range(12))))

    def test_carried_columns_cover_the_complete_new_schema(self):
        conn = REAL_CONNECT(self.db)
        self.addCleanup(conn.close)
        self.assertEqual({"path", *store_migrate._FAV_CARRIED},
                         {row["name"] for row in conn.execute("PRAGMA table_info(favorites)")})


if __name__ == "__main__":
    unittest.main()
