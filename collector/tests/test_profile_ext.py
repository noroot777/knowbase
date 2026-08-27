from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from src.crawler.profile_ext import (
    CRED_EXPIRED,
    CRED_STALE,
    CRED_VALID,
    ProfileCredentialUnavailableError,
    ProfileCredentialStore,
    credential_public_status,
    credential_state,
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


class CredentialFreshnessTests(unittest.TestCase):
    """凭证"放了多久"这一档:只影响上报,不影响放行。

    微信这套凭证撑不了多久(实测半小时上下开始零星失效),但它过期时不会
    通知谁——本地记录永远显示"有效",直到某次采集撞上 no session。
    没有这一档的话,昨天截的一批今天照样是绿点,面板上就是 44/44 就绪。
    """

    def _cred(self, age_seconds: int):
        item = {
            "url": ("https://mp.weixin.qq.com/s?__biz=MzExample%3D%3D"
                    "&uin=1&key=k&pass_ticket=p"),
            "set_cookie": "wap_sid2=s; Path=/",
            "timestamp": int(time.time()) - age_seconds,
        }
        return parse_wxdown_item(item)

    def test_state_by_age(self) -> None:
        self.assertEqual(CRED_VALID, credential_state(self._cred(60)))
        self.assertEqual(CRED_STALE, credential_state(self._cred(21 * 60)))

    def test_server_verdict_beats_age(self) -> None:
        """服务端否掉的就是 expired,哪怕刚截下来——别降格成"可能还行"。"""
        fresh = self._cred(5)
        fresh.invalidated_at = int(time.time())
        self.assertEqual(CRED_EXPIRED, credential_state(fresh))

    def test_threshold_is_tunable(self) -> None:
        with mock.patch.dict(os.environ, {"WXDOWN_CRED_FRESH_MINUTES": "60"}):
            self.assertEqual(CRED_VALID, credential_state(self._cred(45 * 60)))
        with mock.patch.dict(os.environ, {"WXDOWN_CRED_FRESH_MINUTES": "乱写"}):
            self.assertEqual(CRED_STALE, credential_state(self._cred(45 * 60)))

    def test_stale_credential_still_passes_the_gate(self) -> None:
        """放太久只是没把握,不是失效:get() 照样给,采集会拿它去试。"""
        old = self._cred(3 * 3600)
        self.assertTrue(old.is_valid())
        self.assertFalse(old.is_fresh())
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileCredentialStore(Path(directory) / "c.json")
            store.save_wxdown_items([{
                "url": ("https://mp.weixin.qq.com/s?__biz=MzExample%3D%3D"
                        "&uin=1&key=k&pass_ticket=p"),
                "set_cookie": "wap_sid2=s; Path=/",
                "timestamp": int(time.time()) - 3 * 3600,
            }])
            self.assertIsNotNone(store.get("MzExample=="))   # 不抛,就是放行

    def test_public_status_carries_state_and_age(self) -> None:
        status = credential_public_status(self._cred(25 * 60))
        self.assertEqual(CRED_STALE, status["state"])
        self.assertTrue(status["valid"])                     # 服务端还没否它
        self.assertGreaterEqual(status["age_seconds"], 25 * 60)


if __name__ == "__main__":
    unittest.main()
