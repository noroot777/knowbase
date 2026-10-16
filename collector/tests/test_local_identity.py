"""导出回执与**首次来源身份**:这一轮抓到的、之前抓到这次才补导的、说不清的。

和采集本体、「明细点得开删得掉」那两族共用 `local_collect_case` 那份夹具。单拆出来
是因为这一组问的全是另一个问题:**一篇文章是哪一次抓到的**,以及那个事实在异常、
重试、读不懂的清单面前还站不站得住 —— 口径和事故形态在
`docs/decisions/daily.md` 的「『这一次抓到的』和『补导进来的』分不开」一节。
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db import connection as conn_mod
from src.crawler.local_wechat import parse_push
from src.utils.article_identity import stable_article_id

from tests.local_collect_case import LocalCollectCase, push


class LocalIdentityTests(LocalCollectCase):
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
    def test_export_hands_up_what_it_wrote_with_each_items_identity(self):
        """导出脚本逐篇打的那行清单,连同输出目录一起收进结构化事件。

        每条带 `id`(稳定内容身份)和 `round`(发现它的那一次业务抓取)——
        知识库拿它当成果回执,并据此分开「这一次新抓到的」和「补导之前抓到的」
        (口径在 kb/collectors/__init__.py 的 materialize 契约)。
        """
        from pathlib import Path

        self.reader.messages.return_value = [push()]
        self.fill.return_value = (["正文"], "<p>正文</p>", "")
        self.collect(round_key="batch-today")
        out = str(Path(self.tmp.name) / "export2")
        event = self._export_event(out)
        self.assertIn("  [ok] ", self.human)       # 给人看的那一行还在
        # 先判「交上来了没有」再判内容:缺键的话 KeyError 会把红变成 ERROR,
        # 而「清单压根没交」正是这一条要拦的那种改动。
        self.assertEqual({"out", "items"}, {"out", "items"} & set(event),
                         f"事件里没有成果回执:{sorted(event)}")
        self.assertEqual(out, event["out"])
        self.assertEqual(1, len(event["items"]))
        one = event["items"][0]
        self.assertTrue(one["name"].endswith(".md"))
        # 回执指的得是真落在盘上的那个文件,不是一句好看的文案
        self.assertTrue((Path(out) / one["name"]).is_file())
        self.assertTrue(one["id"])                             # 稳定内容身份
        self.assertEqual("batch-today", one["round"])          # 发现它的那一轮

    def test_the_round_that_found_an_article_is_not_the_round_that_exports_it(self):
        """**这一条是本任务的要害**:正文当天没抓到、隔一轮补上才导出。

        回执里记的仍是**发现**它的那一轮(`batch-day1`),不是导出它的这一轮 ——
        否则药丸会把一篇前几天就抓到的文章算成「这一次新抓到的」,而远端那天
        压根没发新文章。`INSERT OR IGNORE` 保证第二轮不改写它。
        """
        from pathlib import Path

        self.reader.messages.return_value = [push()]
        # 第一轮:发现了它,但正文一个字都没拿到 —— 导出会跳过它(无正文)
        self.fill.return_value = ([], "", "读取失败")
        self.collect(round_key="batch-day1")
        out = str(Path(self.tmp.name) / "export-carried")
        self.assertEqual([], self._export_event(out)["items"])

        # 第二轮:正文补上了,这一轮才真写进 vault。先清掉上一轮留下的退避状态,
        # 不然它会被当成「还没到重试时间」整轮跳过。
        with self.repo.conn:
            self.repo.conn.execute("DELETE FROM article_fetch_state")
        self.fill.return_value = (["补上的正文"], "<p>补上的正文</p>", "")
        self.collect(round_key="batch-day3")
        items = self._export_event(out)["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("batch-day1", items[0]["round"])

    def test_filling_in_a_body_does_not_create_a_first_origin(self):
        """**补正文不构成新的首次来源。** 库里早就有、但没正文的那一篇。

        这一篇可能是存量、手工导入、没带批次号跑的那次采集,也可能是冻结分支
        写进来的 —— 共同点是**没有谁能证明它是哪一次抓到的**。它没正文,所以
        照旧会进采集队列;等哪天正文补上了,这一次的批次号不许盖到它头上。

        盖上去的后果不是"数字差一点":远端今天一条新内容都没发,药丸会写
        「其中 1 篇是这一次新抓到的」。正确答案是 0 篇新抓到、1 篇说不清。
        """
        from pathlib import Path
        from src.utils.article_identity import stable_article_id

        article = parse_push(push())[0]
        aid = stable_article_id(article["link"])
        # 先造出「有文章、没正文、没来源」这个状态:不带批次号入一次元数据
        self.repo.upsert_article_from_blog("档案名", article)
        self.repo.commit()
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM article_fetch_rounds").fetchone()[0])

        self.fill.return_value = (["隔几天才补上的正文"], "<p>补上的</p>", "")
        self.collect(round_key="batch-today")
        self.assertTrue(self.repo.conn.execute(
            "SELECT 1 FROM article_bodies WHERE article_id=?", (aid,)).fetchone(),
            "正文该补上才对,这一条测的是补上之后来源有没有被改写")
        self.assertEqual([], self.repo.conn.execute(
            "SELECT round_key FROM article_fetch_rounds").fetchall(),
            "补正文那一轮把自己写成了这一篇的首次来源 —— 远端没发新内容,"
            "药丸却会说「这一次新抓到的」")
        # 后果钉到回执上:知识库那侧读到空串才会归到 unsure 那一档
        out = str(Path(self.tmp.name) / "export-prior")
        self.assertEqual("", self._export_event(out)["items"][0]["round"])

    def test_a_crash_after_the_body_cannot_leave_an_article_without_its_origin(self):
        """来源和首次元数据同一个事务:正文之后崩一次也分不开它们。

        原来来源是正文抓完之后补记的。入库单篇要走三段提交(元数据 → 正文 →
        字数),**正文之后那一段**崩一次(磁盘满、库被锁死)就留下一篇有正文、
        没来源的文章 —— 而它从此再也不进采集队列(`_download` 的
        `has_readable_body` 那道筛),来源永久丢掉,这一篇在药丸上永远算「说不清」。

        注入点必须在**正文已经提交之后**:抓正文本身那一步的异常在
        `_fetch_article_detail_text` 里就被接住了(退避重试),它崩不出这个形态。
        判据落在后果上:用一条**独立连接**(只看得见已提交的东西)去看,文章在
        库里就一定带着它的来源;再跑一次采集也确认它不会回到队列里来。
        """
        from src.db.repository import ArticleRepository

        self.fill.return_value = (["正文落库了"], "<p>正文落库了</p>", "")
        with patch.object(ArticleRepository, "set_word_count",
                          side_effect=OSError("写字数时磁盘满了")):
            result = self.collect(round_key="batch-crash")
        self.assertTrue(any("磁盘满" in e for e in result["errors"]),
                        f"注入点没崩,这一条就什么都没测到:{result['errors']}")

        seen = conn_mod.get_connection(readonly=True)
        try:
            rows = seen.execute("SELECT id FROM articles").fetchall()
            origins = dict(seen.execute(
                "SELECT article_id, round_key FROM article_fetch_rounds").fetchall())
            bodies = seen.execute("SELECT COUNT(*) FROM article_bodies").fetchone()[0]
        finally:
            seen.close()
        self.assertEqual(1, len(rows))
        self.assertEqual(1, bodies, "正文该已经提交 —— 这一条测的是正文之后崩掉")
        self.assertEqual({rows[0]["id"]: "batch-crash"}, origins,
                         "文章连正文都提交了、来源却没跟上 —— 它再也不进采集队列,"
                         "这一篇的来源永久丢了")
        # 「再也不进队列」不是推测:下一轮它被当成已有的那一篇直接筛掉
        again = self.collect(round_key="batch-next")
        self.assertEqual((1, 0), (again["existing_articles"], again["download_total"]))

    def test_a_failed_origin_write_takes_the_whole_article_back(self):
        """来源那一行写不进去:**这一篇整篇撤回**,不许留下半条。

        「文章和来源在同一个事务里」只保证它们不会被**别人**的提交劈开。劈开它们
        的是自己这一步抛出来的异常:文章那行已经写进事务、来源那行炸了,而
        `_download` 是逐篇接住异常接着跑下一篇的 —— 下一篇成功时那次 `commit()`
        就把这半条一起提交了。而这一篇从此有正文、没来源,重试时 `first` 为假、
        再也补不上(实测的末态:`articles=1 / article_fetch_rounds=0`,之后反复
        同步都只得到 `unsure=1`)。

        注入手段是 SQLite 的授权器,**只拒来源表的 INSERT** —— 不替换仓储的 SQL、
        不改提交代码,也不挑某个错误码。判据落在后果上:用独立连接看已提交的那份,
        这一篇一个字节都不该在;解除故障后下一轮才把它当真正的首次插入,记下
        **那一次**的批次(而不是凭空给存量补一个)。
        """
        import sqlite3

        from src.db import repository as repo_mod

        real, deny = repo_mod.get_connection, {"on": True}

        def guard(*a, **kw):
            conn = real(*a, **kw)

            def auth(action, arg1, arg2, dbname, source):
                if (deny["on"] and action == sqlite3.SQLITE_INSERT
                        and arg1 == "article_fetch_rounds"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            conn.set_authorizer(auth)
            return conn

        with patch.object(repo_mod, "get_connection", guard):
            first = self.collect(round_key="batch-first")
            self.assertTrue(any("not authorized" in e for e in first["errors"]),
                            f"注入点没拦住来源那一行:{first['errors']}")
            seen = real(readonly=True)
            try:
                counts = [seen.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in ("articles", "article_fetch_rounds", "article_bodies")]
            finally:
                seen.close()
            self.assertEqual([0, 0, 0], counts,
                             "来源写失败之后还是把这一篇提交了 —— 它的来源从此"
                             "永久丢了,重试也补不上")

            deny["on"] = False          # 故障解除:这一轮才是真正的首次插入
            again = self.collect(round_key="batch-retry")
        self.assertEqual((1, []), (again["new_articles"], again["errors"]))
        self.assertEqual(["batch-retry"], [r["round_key"] for r in self.repo.conn.execute(
            "SELECT round_key FROM article_fetch_rounds")])
        out = str(Path(self.tmp.name) / "export-after-rollback")
        self.assertEqual("batch-retry", self._export_event(out)["items"][0]["round"])

    def test_the_rollback_takes_back_this_article_not_the_callers_transaction(self):
        """反过来钉一边:撤的是**这一篇**,调用方同一个事务里别的写入原样留着。

        整份 `rollback()` 也能让这一篇不落盘,代价是把调用方那个事务里已经写好、
        还没提交的东西一起撤掉 —— 那是用另一种静默丢数据换掉这一种。所以用
        savepoint,而不是 `rollback()`。

        **这一条得自己摆出「事务里还有别人的写」这个前提**:眼下 `_download` 是每
        篇单独提交的,从 `collect()` 那一层注入的话事务里只有这一篇,两种做法看不
        出区别。而 `upsert_article_from_blog` 是公开方法,不能只在「调用方刚好没有
        别的未提交写入」时才正确。
        """
        import sqlite3

        article = parse_push(push())[0]
        # 调用方先在同一个事务里写一笔别的业务,还没提交
        self.repo.conn.execute("UPDATE accounts SET latest_crawl_at=?"
                               " WHERE name='档案名'", ("2026-10-15 09:00",))
        self.assertTrue(self.repo.conn.in_transaction)

        def auth(action, arg1, arg2, dbname, source):
            if action == sqlite3.SQLITE_INSERT and arg1 == "article_fetch_rounds":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.repo.conn.set_authorizer(auth)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.repo.upsert_article_from_blog("档案名", article,
                                                   fetch_round="batch-x")
        finally:
            self.repo.conn.set_authorizer(None)
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM articles").fetchone()[0])
        self.assertEqual("2026-10-15 09:00", self.repo.conn.execute(
            "SELECT latest_crawl_at FROM accounts WHERE name='档案名'").fetchone()[0],
            "调用方那一笔被连带撤掉了 —— 撤回的边界是这一篇,不是整个事务")
        self.repo.commit()
        seen = conn_mod.get_connection(readonly=True)
        try:
            self.assertEqual("2026-10-15 09:00", seen.execute(
                "SELECT latest_crawl_at FROM accounts"
                " WHERE name='档案名'").fetchone()[0])
        finally:
            seen.close()

    def test_a_collect_without_a_round_leaves_the_identity_blank(self):
        """在终端里手动跑一次采集(没有业务抓取身份):不许编一个。

        空串是「说不清」,知识库那一侧归到 unsure 那一档;硬写个值等于凭空造
        「这一篇是哪一次抓到的」这个事实。
        """
        from pathlib import Path

        self.reader.messages.return_value = [push()]
        self.fill.return_value = (["正文"], "<p>正文</p>", "")
        self.collect()                                  # 不传 round_key
        out = str(Path(self.tmp.name) / "export-anon")
        self.assertEqual("", self._export_event(out)["items"][0]["round"])

    def test_an_item_field_that_is_not_text_voids_the_whole_list(self):
        """`[item]` 行里某个字段不是字符串:整份作废,和写到一半那种同档。

        这一端和知识库那一端(`_receipt` / `fetch_identity.text_field`)必须同口径 ——
        只在一端拦的话,另一端就成了把缺口洗成确定答案的那个地方。不许 `str()` 一下
        糊过去:`str(None)` / `str({...})` 都是**非空**字符串,当抓取身份交上去会被
        归进「确定不是这一轮抓的」。
        """
        import cli

        good = ('[export] 输出目录: /tmp/whatever\n'
                '  [item] {"id": "a1", "round": "batch-x", "name": "2026-10-15/a.md"}\n'
                '[export] 完成: 导出 1, 跳过(已存在) 0, 空正文 0\n')
        for bad in ('{"id": "a2", "round": {"not": "an id"}, "name": "b.md"}',
                    '{"id": "a2", "round": 7, "name": "b.md"}',
                    '{"id": ["a2"], "round": "batch-x", "name": "b.md"}',
                    '{"id": "a2", "round": "batch-x", "name": null}'):
            raw = good.replace('[export] 完成',
                               '  [item] ' + bad + '\n[export] 完成')
            with self.subTest(line=bad):
                with patch("cli.subprocess.run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout=raw, stderr="")), \
                     contextlib.redirect_stdout(io.StringIO()) as event_output, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.EXIT_OK, cli.main(["export", "--no-images"]))
                self.assertNotIn("items", json.loads(event_output.getvalue()))

    def test_a_torn_item_line_voids_the_whole_list(self):
        """清单里有一行读不懂:**整份作废,不挑着收**。

        挑着收的后果是交上去一份少记的清单,而上层把它当确定值用(「这一次抓了
        N 篇」)—— 少记是静默丢数据,报「不可确认」顶多让人多同步一次。
        判据落在后果上:事件里**没有** `items` 这个键,知识库那侧据此报不可确认。
        """
        import cli

        raw = ("[export] 输出目录: /tmp/whatever\n"
               '  [item] {"id": "a1", "round": "batch-x", "name": "2026-10-15/a.md"}\n'
               "  [item] {\"id\": \"a2\", \"round\"\n"        # 写到一半
               "[export] 完成: 导出 2, 跳过(已存在) 0, 空正文 0\n")
        with patch("cli.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=raw, stderr="")), \
             contextlib.redirect_stdout(io.StringIO()) as event_output, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(["export", "--no-images"]))
        event = json.loads(event_output.getvalue())
        self.assertNotIn("items", event)
        # 反过来钉一边:整份清单都读得懂时照旧给键(不是一出错就永久不给了)。
        good = raw.replace("  [item] {\"id\": \"a2\", \"round\"\n", "")
        with patch("cli.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=good, stderr="")), \
             contextlib.redirect_stdout(io.StringIO()) as event_output, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(["export", "--no-images"]))
        self.assertEqual([{"id": "a1", "round": "batch-x",
                           "name": "2026-10-15/a.md"}],
                         json.loads(event_output.getvalue())["items"])


if __name__ == "__main__":
    unittest.main()
