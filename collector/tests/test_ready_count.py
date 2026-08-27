from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path

from src.db import connection as conn_mod


class ReadyCountConsistencyTests(unittest.TestCase):
    """「几个凭证就绪」在三个地方必须是同一个口径。

    停用的号不参与采集,自然不该要求它有凭证——算进总数的话,停用一个号
    之后「全部就绪」就永远到不了。这件事已经在两处分别修过一次
    (`sources_api.list_targets` 和 `cli creds`),第三处 `cli accounts list`
    又漏了,于是同一件事三个地方三套账。这组测试就是把它们钉在一起。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._saved = (conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready)
        conn_mod.DEFAULT_DB_PATH = tmp / "test.db"
        conn_mod.DATA_DIR = tmp
        conn_mod._db_ready = False

        from src.crawler.profile_ext import profile_credential_store
        from src.db.repository import ArticleRepository

        self._store = profile_credential_store
        self._saved_cred_path = profile_credential_store.path
        profile_credential_store.path = tmp / "credentials.json"

        with ArticleRepository() as repo:
            repo.enable_crawl_account("启用号", "fake-on")
            repo.enable_crawl_account("停用号", "fake-off")
            repo.conn.execute(
                "UPDATE accounts SET in_crawl_config=0 WHERE name='停用号'")
            repo.commit()

        # 两个号都有一份刚截下来的凭证:停用号"有凭证"也不该被算进分母
        profile_credential_store.save_wxdown_items([
            {"url": f"https://mp.weixin.qq.com/s?__biz={biz}&uin=1&key=k&pass_ticket=p",
             "set_cookie": "wap_sid2=s; Path=/", "timestamp": int(time.time())}
            for biz in ("fake-on", "fake-off")
        ])

    def tearDown(self) -> None:
        self._store.path = self._saved_cred_path
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = self._saved
        self._tmp.cleanup()

    @staticmethod
    def _run(fn, args) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fn(args)
        return out.getvalue(), err.getvalue()

    def _creds_json(self) -> dict:
        import cli

        out, _ = self._run(cli.cmd_creds, Namespace(
            no_import=True, json=True, missing_only=False))
        return json.loads(out.strip().splitlines()[-1])

    def _accounts_summary(self) -> str:
        import cli

        _, err = self._run(cli.cmd_accounts, Namespace(
            action="list", json=False, name="", fakeid=""))
        return err.strip().splitlines()[0]

    def test_creds_counts_only_crawled_accounts(self) -> None:
        data = self._creds_json()
        self.assertEqual(1, data["total"])          # 停用号不进分母
        self.assertEqual(1, data["ready"])
        self.assertEqual(["启用号"], [a["name"] for a in data["accounts"]])

    def test_accounts_list_summary_matches_creds(self) -> None:
        data = self._creds_json()
        summary = self._accounts_summary()
        # 摘要里那个分数必须跟 creds 是同一个,否则用户对着命令表看会看到两套数
        self.assertIn(f"{data['ready']}/{data['total']} 个凭证就绪", summary)
        self.assertIn("共 2 个公众号", summary)      # 总数仍如实报,停用号没被藏起来

    def test_disabled_account_cannot_block_full_readiness(self) -> None:
        """把停用号的凭证删掉,「全部就绪」照样成立——它本来就不需要凭证。"""
        from src.crawler.profile_ext import ProfileCredential

        records = self._store.records()
        records.pop("fake-off")
        with self._store._lock:
            self._store._write_unlocked(records)
        self.assertIsInstance(records.get("fake-on"), ProfileCredential)

        data = self._creds_json()
        self.assertEqual((1, 1), (data["ready"], data["total"]))
        self.assertIn("1/1 个凭证就绪", self._accounts_summary())

    def test_disabled_account_is_still_listed(self) -> None:
        """不进计数 ≠ 不显示:停用号得能看见,否则没法重新启用。"""
        import cli

        _, err = self._run(cli.cmd_accounts, Namespace(
            action="list", json=False, name="", fakeid=""))
        self.assertIn("停用号", err)
        self.assertIn("启用号", err)


if __name__ == "__main__":
    unittest.main()
