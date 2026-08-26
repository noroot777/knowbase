from __future__ import annotations

import json
import stat
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from src.crawler.profile_ext import (
    ProfileCredentialUnavailableError,
    ProfileCredentialStore,
    parse_general_msg_list,
    parse_wxdown_item,
)


class ProfileCredentialTests(unittest.TestCase):
    def sample_item(self) -> dict:
        return {
            "url": (
                "https://mp.weixin.qq.com/s?__biz=MzExample%3D%3D"
                "&uin=123&key=secret-key&pass_ticket=pass-ticket"
            ),
            "set_cookie": (
                "wap_sid2=sid-value; Path=/; HttpOnly, "
                "appmsg_token=token%2Bvalue; Path=/; HttpOnly"
            ),
            "timestamp": int(time.time() * 1000),
            "name": "测试公众号",
        }

    def test_parse_wxdown_item(self) -> None:
        credential = parse_wxdown_item(self.sample_item())
        self.assertEqual(credential.biz, "MzExample==")
        self.assertEqual(credential.uin, "123")
        self.assertEqual(credential.pass_ticket, "pass-ticket")
        self.assertEqual(credential.appmsg_token, "token+value")
        self.assertIn("wap_sid2=sid-value", credential.cookie)
        self.assertNotIn("Path=", credential.cookie)

    def test_store_is_private_and_keeps_latest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            store = ProfileCredentialStore(path)
            saved, rejected = store.save_wxdown_items([self.sample_item()])
            self.assertEqual(len(saved), 1)
            self.assertEqual(rejected, [])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(store.get("MzExample==").is_valid())

            older = self.sample_item()
            older["timestamp"] = 1
            saved, rejected = store.save_wxdown_items([older])
            self.assertEqual(saved, [])
            self.assertEqual(rejected, [])
            self.assertGreater(store.get("MzExample==").captured_at, 1)

    def test_store_uses_server_invalidation_instead_of_fixed_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            store = ProfileCredentialStore(path)
            item = self.sample_item()
            item["timestamp"] = 1
            store.save_wxdown_items([item])

            self.assertTrue(store.get("MzExample==").is_valid())
            store.mark_invalid("MzExample==")
            with self.assertRaises(ProfileCredentialUnavailableError):
                store.get("MzExample==")
            self.assertFalse(
                store.get("MzExample==", allow_invalid=True).is_valid()
            )
            store.mark_valid("MzExample==")
            self.assertTrue(store.get("MzExample==").is_valid())

    def test_parse_general_msg_list_with_multi_article(self) -> None:
        timestamp = 1_787_000_000
        raw = json.dumps(
            {
                "list": [
                    {
                        "comm_msg_info": {"id": "100", "datetime": timestamp},
                        "app_msg_ext_info": {
                            "appmsgid": "100",
                            "title": "主文章",
                            "digest": "摘要",
                            "content_url": "https://mp.weixin.qq.com/s?mid=200&idx=1",
                            "cover": "https://example.com/1.jpg",
                            "multi_app_msg_item_list": [
                                {
                                    "title": "子文章",
                                    "content_url": "https://mp.weixin.qq.com/s?mid=200&idx=2",
                                    "cover": "https://example.com/2.jpg",
                                }
                            ],
                        },
                    }
                ]
            }
        )
        articles = parse_general_msg_list(raw)
        self.assertEqual([item["title"] for item in articles], ["主文章", "子文章"])
        self.assertNotEqual(articles[0]["id"], articles[1]["id"])
        self.assertEqual(
            articles[0]["create_time"],
            datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M"),
        )


if __name__ == "__main__":
    unittest.main()
