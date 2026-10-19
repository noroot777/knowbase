"""收藏列表、行动卡和分诊共享文档/快照元数据口径。"""

import json
from pathlib import Path
import unittest
from unittest import mock

from kb import favbrain, store
from tests.fav_snippet_fixture import ApiFixture, DOC


class FavoriteBrainMetadataTests(ApiFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(favbrain, "VAULT_ROOT", self.vault))
        self.enterContext(mock.patch.object(
            favbrain, "_TRIAGE_PATH", Path(self.temp.name) / "triage.json"))
        self.enterContext(mock.patch.object(favbrain, "_state", {"running": False, "error": ""}))
        self.enterContext(mock.patch.object(favbrain.llm, "get_fav_model", return_value="stub"))
        self.chat = self.enterContext(mock.patch.object(favbrain.llm, "chat"))

    def save_document(self):
        self.client.post("/api/favorites/toggle", json={"path": DOC}).raise_for_status()
        return self.listed()["favorites"][0]

    def detach_document(self):
        item = self.save_document()
        store.remove_documents(self.conn, [DOC])
        self.conn.commit()
        return item

    def action_prompt(self, favorite_id):
        self.chat.reset_mock()
        self.chat.return_value = '{"name":"保留信息","what":"按收藏内容整理","kind":"read"}'
        action = favbrain.extract_action(favorite_id)
        self.assertEqual("保留信息", action["name"])
        stored = self.conn.execute(
            "SELECT action_json FROM favorites WHERE id=?", (favorite_id,)).fetchone()[0]
        self.assertEqual(action, json.loads(stored))
        self.chat.assert_called_once()
        return self.chat.call_args.args[0]

    def triage_item(self, favorite_id):
        self.chat.reset_mock()
        self.chat.return_value = json.dumps({"quick": [{"id": favorite_id}]})
        favbrain.run_triage()
        self.assertEqual("", favbrain._state["error"])
        self.chat.assert_called_once()
        prompt = self.chat.call_args.args[0]
        items, _tail = json.JSONDecoder().raw_decode(prompt[prompt.index("\n[") + 1:])
        self.assertIn(favorite_id, [item["id"] for item in items])
        self.assertTrue(favbrain.triage_status()["fresh"])
        return next(item for item in items if item["id"] == favorite_id)

    def assert_readers_agree(self, favorite_id):
        listed = next(item for item in self.listed()["favorites"] if item["id"] == favorite_id)
        row = next(item for item in favbrain._inbox_rows() if item["id"] == favorite_id)
        for column in ("title", "account", "published", "summary"):
            self.assertEqual(listed[column], row[column], column)

    def test_detached_action_prompt_uses_saved_source_date_and_summary(self):
        before = self.detach_document()
        prompt = self.action_prompt(before["id"])
        self.assertIn("文章标题:活着的那篇\n", prompt)
        self.assertIn("出处:某号 · 发布于 2026-10-01\n", prompt)
        self.assertIn("摘要:一句摘要\n", prompt)
        self.assertNotIn("文中一句在这儿", prompt)
        self.assertNotIn("出处:知识库文章", prompt)

    def test_detached_triage_prompt_uses_the_same_metadata_as_the_list(self):
        before = self.detach_document()
        item = self.triage_item(before["id"])
        for column in ("title", "account", "published", "summary"):
            self.assertEqual(before[column], item[column], column)
        self.assert_readers_agree(before["id"])

    def test_live_document_metadata_wins_even_when_its_fields_are_empty(self):
        favorite_id = self.save_document()["id"]
        self.conn.execute("UPDATE favorites SET origin_title='旧标题',origin_account='旧作者',"
                          " origin_published='2025-01-01',origin_summary='旧摘要'")
        for account, published, summary in (("新作者", "2026-10-19", "新摘要"), ("", "", "")):
            with self.subTest(account=account):
                self.conn.execute("UPDATE documents SET account=?,published=?,summary=? WHERE path=?",
                                  (account, published, summary, DOC))
                self.conn.commit()
                prompt = self.action_prompt(favorite_id)
                self.assertIn(f"出处:{account or '知识库文章'} · ", prompt)
                self.assertIn(f"摘要:{summary}\n", prompt)
                self.assertNotIn("旧作者", prompt)
                self.assertNotIn("旧摘要", prompt)
                item = self.triage_item(favorite_id)
                self.assertEqual((account, published, summary),
                                 tuple(item[column] for column in ("account", "published", "summary")))
                self.assert_readers_agree(favorite_id)

    def test_legacy_detached_metadata_is_not_invented_for_models(self):
        favorite_id = self.conn.execute(
            "INSERT INTO favorites(origin_title,created_at,updated_at)"
            " VALUES ('旧收藏','2025-02-03','2025-02-03')").lastrowid
        self.conn.commit()
        prompt = self.action_prompt(favorite_id)
        self.assertIn("出处:知识库文章 · 收藏于 2025-02-03\n", prompt)
        self.assertIn("摘要:\n", prompt)
        item = self.triage_item(favorite_id)
        self.assertEqual(("旧收藏", "", "", ""),
                         tuple(item[column] for column in ("title", "account", "published", "summary")))
        self.assert_readers_agree(favorite_id)

    def test_non_document_origins_keep_their_prompt_labels_and_quotes(self):
        for origin, label in (("daily", "每日日报"), ("chat", "知识问答"), ("feed", "订阅内容")):
            with self.subTest(origin=origin):
                favorite_id = self.conn.execute(
                    "INSERT INTO favorites(origin,origin_title,quote,created_at,updated_at)"
                    " VALUES (?,'片段标题','保存的引文','2026-10-01','2026-10-01')", (origin,)).lastrowid
                self.conn.commit()
                prompt = self.action_prompt(favorite_id)
                self.assertIn(f"出处:{label} · 收藏于 2026-10-01\n", prompt)
                self.assertIn("保存的引文", prompt)
                item = self.triage_item(favorite_id)
                self.assertEqual("保存的引文", item["quote"])
                self.assertEqual(("", "", ""),
                                 tuple(item[column] for column in ("account", "published", "summary")))
                self.assert_readers_agree(favorite_id)


if __name__ == "__main__":
    unittest.main()
