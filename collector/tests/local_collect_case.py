"""本机绿泡泡采集那一族共用的夹具:临时库 + 打桩过的读取器 + 一次 `collect()`。

`push()` 也在这里 —— 它原来和夹具一起待在 `test_local_wechat.py` 里,而
`test_local_index` / `test_local_coverage` 从那边 import 它。那个模块写到 700 行
上限之后,「明细点得开、删得掉」那一族搬去了 `test_article_drop.py`,两边都要用
这份夹具,所以夹具连同 `push()` 一起落到这个中立模块里;`test_local_wechat` 仍然
re-export `push`,那两个既有的 import 一个字都不用改。
"""
from __future__ import annotations

import contextlib
import io
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


class LocalCollectCase(unittest.TestCase):
    """一个空库、一个打桩读取器、两个公众号(一个启用一个停用)。"""

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

    def _reset_library(self):
        """把库清回「刚建好」:四张表都空,下一段摆局不受上一段影响。"""
        for table in ("article_fetch_state", "article_bodies", "articles",
                      "deleted_articles"):
            self.repo.conn.execute(f"DELETE FROM {table}")
        self.repo.commit()

    def _count(self, table):
        return self.repo.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _drop_now(self, *extra):
        """以面板那颗按钮的形态删掉库里**当前**那一篇(真实 CLI,另一个连接)。"""
        import cli
        import json as _json

        row = self.repo.conn.execute("SELECT id, link FROM articles").fetchone()
        with contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["articles", "drop", "--id", row["id"], "--account",
                             "档案名", "--link", row["link"], "--json", *extra])
        self.assertEqual(cli.EXIT_OK, code)
        self.assertTrue(_json.loads(out.getvalue())["ok"])

    def collect(self, **kwargs):
        from src.collect.local_runner import collect

        return collect(lambda event: None, reader=self.reader, **kwargs)
