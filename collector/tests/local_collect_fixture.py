"""本机采集那一组用例的共用夹具:临时库 + 真 `LocalReader` 桩 + 真导出脚本。

`test_local_wechat.py`(采集本体)和 `test_local_identity.py`(回执与首次来源身份)
共用它。**夹具只有一份**:两边各摆一次临时库和桩,两份迟早各漂各的,而这组用例
的判据全落在「库里那条是什么」上 —— 夹具一漂,断言就不再说同一件事。

`_export_event` 为什么要把 `items` 这个键在夹具里就断言掉,见它自己的 docstring。
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

from src.crawler.local_wechat import LocalReader
from src.db import connection as conn_mod


def push(biz="biz-a", mid="101", idx="1", published="2026-09-15 18:30", **extra):
    url = f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx={idx}&sn=signature&chksm=checksum&scene=123&xtrack=1"
    fields = {"title": "测试文章", "url": url, "digest": "摘要", "cover": "",
              "pub_time": str(int(datetime.strptime(published, "%Y-%m-%d %H:%M").timestamp())),
              **extra}
    return {"text": "<msg><appmsg><mmreader><category><item>"
            + "".join(f"<{k}>{escape(v)}</{k}>" for k, v in fields.items())
            + "</item></category></mmreader></appmsg></msg>",
            "create_time": 1}



class LocalCollectEnv(unittest.TestCase):
    """临时库、桩好的 reader、真导出脚本 —— 自己不带用例。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready)
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = root / "test.db", root, False
        from src.db.repository import ArticleRepository

        self.repo = ArticleRepository()
        self.repo.enable_crawl_account("档案名", "biz-a")
        self.repo.enable_crawl_account("停用号", "biz-off")
        self.repo.conn.execute("UPDATE accounts SET in_crawl_config=0 WHERE name='停用号'")
        self.repo.commit()
        self.reader = LocalReader()
        self.reader.check = Mock()
        self.reader.accounts = Mock(return_value=[{"username": "gh_test", "display_name": "档案名"}])
        self.reader.messages = Mock(return_value=[push()])
        from tests.local_source_fixture import TimelineFixture

        self.sql_fixture = TimelineFixture(self.reader)
        self.reader.call = self.sql_fixture.call

        self.fill = patch("src.utils.wechat_body.fetch_article_body_full", return_value=(
            ["真实正文测试内容"], "<p>真实正文测试内容</p>", "")).start()
        patch("src.utils.request_pacing.RequestPacer.wait").start()

    def tearDown(self):
        self.sql_fixture.close()
        patch.stopall()
        self.repo.close()
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = self.saved
        self.tmp.cleanup()

    def collect(self, **kwargs):
        from src.collect.local_runner import collect

        return collect(lambda event: None, reader=self.reader, **kwargs)


    def _export_event(self, out):
        """跑一次真导出脚本,再让 `cli.cmd_export` 把它的输出收成结构化事件。

        两段都走真的:清单是脚本自己逐篇打出来的,`out` 和每条的 `id`/`round`
        都由 cli 从那些行里解析。中间任何一段只留给人看的话,知识库那一侧就只能
        回去扫全库猜归属,而差集分不出"谁写的"。
        """
        import sys
        from scripts import export_obsidian

        with patch.object(sys, "argv",
                          ["export", "--out", out, "--dedup-root", out, "--no-images"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, export_obsidian.main())
        raw = output.getvalue()
        # 一篇都没导的时候也得有这一行:上层靠它把清单里的「天/文件名」还原成
        # vault 相对路径。
        self.assertIn(f"[export] 输出目录: {out}", raw)
        self.human = raw

        import cli
        with patch("cli.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=raw, stderr="")), \
             contextlib.redirect_stdout(io.StringIO()) as event_output, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(["export", "--no-images"]))
        event = json.loads(event_output.getvalue())
        # 这里就把 `items` 这个键钉住,别留给各条用例去下标 —— 键名漂了的话每条
        # 用例红成 KeyError(ERROR),而我们要的是一句说得出后果的 FAIL:
        # 知识库那一侧读不到清单就只能把整轮报成「不可确认」。
        self.assertIn("items", event,
                      "事件里没有 items 清单 —— 知识库那侧只能报「不可确认」")
        return event

