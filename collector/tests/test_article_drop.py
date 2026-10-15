"""明细里那一条:报告带得出身份、删得掉、删完真的不再抓。

从 `test_local_wechat.py` 搬过来的(那个模块到了 700 行上限)。三层判据:
①导出那份 `no_body` 明细要带**身份**,否则面板只能显示、点不开也删不掉;
②删除要幂等、归属对不上一律拒、**缺行不是排除凭据**;
③**排除提交之后后续写入不许复活这个身份** —— 判据在写语句自己的 WHERE 里,
发现阶段那次黑名单检查只是早一道。

硬约束、事故形态和反向验证红数在 `docs/decisions/failure-drop.md` 第一节。
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest.mock import patch

from src.crawler.local_wechat import parse_push
from src.utils.article_identity import article_identity, stable_article_id
from tests.local_collect_case import LocalCollectCase, push


class ArticleDropTests(LocalCollectCase):
    def test_export_keeps_old_empty_article_retryable(self):
        from scripts import export_obsidian
        import sys

        self.reader.messages.return_value = [push(published="2020-01-01 12:00")]
        self.fill.return_value = ("请求错误", None, "")
        self.collect()
        out = str(Path(self.tmp.name) / "export")
        with patch.object(sys, "argv", ["export", "--out", out, "--dedup-root", out, "--no-images"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, export_obsidian.main())
        self.assertIn("待补正文 1", output.getvalue())
        self.assertIn("无正文,跳过 档案名 · 测试文章", output.getvalue())
        self.assertNotIn("失败 0", output.getvalue())
        import cli
        import subprocess

        with patch("cli.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=output.getvalue(), stderr="")), \
             contextlib.redirect_stdout(io.StringIO()) as event_output, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(["export", "--no-images"]))
        import json

        # 这一段 patch 掉了 subprocess,结构化报告压根没写成 —— 走的正是降级路径:
        # 那一条**仍然列得出来**(漏报是静默丢数据),只是没有身份,面板据此把它
        # 显示成不可点、不可删。
        self.assertEqual([{"name": "档案名 · 测试文章", "id": "", "account": "",
                           "title": "", "link": ""}],
                         json.loads(event_output.getvalue())["no_body"])
        self.assertEqual(0, self.repo.conn.execute("SELECT is_wx_deleted FROM articles").fetchone()[0])
        self.repo.conn.execute("UPDATE article_fetch_state SET retry_at=0")
        self.repo.commit()
        self.fill.return_value = (["恢复正文"], "<p>恢复正文</p>", "")
        self.assertEqual([], self.collect()["errors"])
        self.assertEqual(1, self.repo.conn.execute("SELECT COUNT(*) FROM article_bodies").fetchone()[0])

    def test_no_body_detail_carries_identity_so_the_panel_can_open_and_drop_it(self):
        """面板上那条「缺少正文」要点得开、删得掉,靠的是这里的 id 和 link。

        `账号 · 标题` 那个字符串只够给人看:标题会重名、`·` 本身也可能出现在号名
        或标题里,拿它反查是哪一篇一定出错。所以导出脚本另写一份结构化报告,CLI
        优先读它。
        """
        import json
        import sys

        from scripts import export_obsidian

        self.reader.messages.return_value = [push()]
        self.fill.return_value = ("请求错误", None, "")
        self.collect()
        out = str(Path(self.tmp.name) / "export")
        report = Path(self.tmp.name) / "report.json"
        with patch.object(sys, "argv", ["export", "--out", out, "--dedup-root", out,
                                        "--no-images", "--report", str(report)]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, export_obsidian.main())
        items = json.loads(report.read_text(encoding="utf-8"))["no_body"]
        self.assertEqual(1, len(items))
        stored = self.repo.conn.execute("SELECT id, link FROM articles").fetchone()
        self.assertEqual(stored["id"], items[0]["id"])
        self.assertEqual(stored["link"], items[0]["link"])
        self.assertEqual(("档案名", "测试文章"), (items[0]["account"], items[0]["title"]))

        import cli

        with patch("cli.subprocess.run", side_effect=lambda argv, **kw: self._fake_export(argv)), \
             contextlib.redirect_stdout(io.StringIO()) as events, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(["export", "--no-images"]))
        got = json.loads(events.getvalue())["no_body"][0]
        self.assertEqual((stored["id"], "档案名 · 测试文章"), (got["id"], got["name"]))

    def _fake_export(self, argv):
        """替身导出进程:只把 `--report` 那份报告写出来,不真去扫 vault。"""
        import json
        import subprocess

        report = Path(argv[argv.index("--report") + 1])
        row = self.repo.conn.execute("SELECT id, link FROM articles").fetchone()
        report.write_text(json.dumps({"no_body": [
            {"id": row["id"], "account": "档案名", "title": "测试文章",
             "link": row["link"]}]}, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0,
                                           stdout="[export] 完成: 导出 0\n", stderr="")

    def test_dropping_one_article_stops_rediscovering_it_and_is_idempotent(self):
        """「以后不再抓了」= 删掉它 + 把链接身份拉黑,再发现同一篇也不入库。

        **幂等**:连点两次照样成功 —— 用户会连点,两个标签页也会各点一次,报红会
        让人以为没删掉。第二次走的是「行没了、但名单里已经有它」那一档(凭据在,
        所以兑现得了那句承诺);**没有凭据的缺行是另一回事**,见
        `test_a_missing_row_without_any_proof_of_exclusion_is_refused`。
        **归属对不上一律拒**:删下去是不可恢复的。
        """
        import cli
        import json

        self.collect()
        aid = self.repo.conn.execute("SELECT id FROM articles").fetchone()[0]
        with contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_ERROR, cli.main(
                ["articles", "drop", "--id", aid, "--account", "别的号", "--json"]))
        self.assertFalse(json.loads(out.getvalue())["ok"])
        self.assertEqual(1, self.repo.conn.execute(
            "SELECT COUNT(*) FROM articles").fetchone()[0])

        for _ in range(2):      # 连点两次:第二次库里已经没这一行了,照样成功
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.EXIT_OK, cli.main(
                    ["articles", "drop", "--id", aid, "--account", "档案名", "--json"]))
            self.assertTrue(json.loads(out.getvalue())["ok"])
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM articles").fetchone()[0])
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM article_bodies").fetchone()[0])

        # 反过来钉一边:下一轮同步再发现同一篇,不许又入库一次
        self.fill.reset_mock()
        self.assertEqual([], self.collect()["errors"])
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM articles").fetchone()[0])
        self.fill.assert_not_called()

    def test_an_excluded_identity_cannot_be_written_back_after_the_check_passed(self):
        """排除提交之后,**写入层**自己拦住这一篇 —— 不是只靠发现那一道早检查。

        `prepare_article` 查黑名单那一下和真正写入之间有真实间隔(采集一轮要抓
        正文、要更新显示名),用户完全可能正好在这中间按下「不再抓取」:查过的
        那一篇照旧 upsert 进去、照旧抓正文、照旧被导出写进 Vault,而面板上那一条
        已经不再显示,用户**再也看不到它**(第 1 轮审查实测
        `row_exists_after_drop_and_ingest=true`、`body_fetch_calls=1`、
        `export_candidates=1`)。所以判据搬进写语句自己的 WHERE 里。

        判据落在**库里那一行在不在、导出清单里有没有它**上,不落在「有没有调
        is_deleted_blacklist」上。
        """
        import cli
        import json as _json

        from scripts.export_obsidian import load_articles

        self.collect()
        row = self.repo.conn.execute("SELECT id, link FROM articles").fetchone()
        aid, link = row["id"], row["link"]
        article = parse_push(push())[0]         # 同一篇,采集那一侧已经准备好了
        with contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(
                ["articles", "drop", "--id", aid, "--account", "档案名", "--json"]))
        self.assertTrue(_json.loads(out.getvalue())["ok"])

        # 并发那一半:检查早就过了,现在才落到写入
        self.assertFalse(self.repo.upsert_article_from_blog("档案名", article),
                         "写入层要自己拒掉")
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM articles").fetchone()[0])
        self.assertFalse(self.repo.set_body_text(aid, ["正文"], link=link))
        self.assertFalse(self.repo.set_body_html(aid, "<p>正文</p>", link=link))
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM article_bodies").fetchone()[0])
        # 换一个旧档案 ID 也拦得住:判据是那一组键,不只是 id
        legacy = dict(article, id="legacy-id")
        self.assertFalse(self.repo.upsert_article_from_blog("档案名", legacy))
        self.assertFalse(self.repo.set_body_text("legacy-id", ["正文"], link=link))

        # 反过来钉一边:没被排除的那一篇照旧写得进、正文也写得进
        other = parse_push(push(mid="202"))[0]
        self.assertTrue(self.repo.upsert_article_from_blog("档案名", other))
        self.assertTrue(self.repo.set_body_text(other["id"], ["正文"],
                                                link=other["link"]))
        self.repo.commit()
        self.assertEqual([other["id"]],
                         [a["id"] for a in load_articles(self.repo, None)])

        # 存量那一档:行先在、身份后被拉黑(库被别的路径写过/回退过旧备份),
        # 导出那道冗余带也要挡住 —— 导出是写进 Vault,写出去不会自己消失。
        self.repo.exclude_identities(other["id"])
        self.repo.commit()
        self.assertEqual([], load_articles(self.repo, None))

    def test_removing_an_article_mid_sync_is_reported_as_excluded_not_failed(self):
        """同步跑着的时候按下「不再抓取」:**如实记成已排除,不记成下载失败。**

        队列是发现阶段排好的,发现到真正写入之间隔着抓正文。那一篇被写入层拒掉
        之后,原来会落进 `body_failures` 说一句「正文暂未获取,已安排自动重试」
        —— 它不会再被抓了,这是一句谎话;而且它还占着一个 `download_failed`,
        让一轮本来干净的同步看着像出了错。

        摆局:正文抓取那一步里按下按钮(真实时序正是这样 —— 那一步要几秒到
        几十秒)。判据落在**库里那一行在不在、报告里那几个数怎么说**上。
        """
        import cli

        def drop_during_fetch(*a, **kw):
            aid = self.repo.conn.execute("SELECT id FROM articles").fetchone()[0]
            link = parse_push(push())[0]["link"]
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                cli.main(["articles", "drop", "--id", aid, "--account", "档案名",
                          "--link", link, "--json"])
            return (["正文"], "<p>正文</p>", "")

        self.fill.side_effect = drop_during_fetch
        got = self.collect()
        self.assertEqual([], got["errors"])
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM articles").fetchone()[0], "一个字节都不许留下")
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM article_bodies").fetchone()[0])
        self.assertEqual(1, got["excluded_articles"])
        self.assertEqual((0, 0, 0), (got["download_failed"], got["download_succeeded"],
                                     got["new_articles"]),
                         "既不是失败、也不是成功、更不是新增")
        self.assertEqual([], got["body_failures"], "不许说「已安排自动重试」")

    def test_a_missing_row_without_any_proof_of_exclusion_is_refused(self):
        """**缺行不是排除凭据。** 拿不出凭据就拒,别报一个没做到的结论。

        同步记录和没关掉的旧页面会一直持有这一篇的身份,而采集库完全可能被清理、
        重建或回退到没有这一行的版本。那时照旧返回成功的话,主系统会把它从各轮
        明细里隐藏,而下一轮发现时它照样入库、照样抓正文(第 1 轮审查实测
        `identity_blacklisted=false`、`row_exists_after_drop_and_ingest=true`)。
        明细一旦隐藏,用户再也看不到这一条。

        三档:①名单里已经有它 → 幂等成功;②算得出身份(给了 `--link`)→ 当场
        补写进名单再成功;③两样都没有 → 拒。
        """
        import cli
        import json as _json

        def drop(*extra):
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                 contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["articles", "drop", *extra, "--json"])
            return code, _json.loads(out.getvalue())

        self.collect()
        row = self.repo.conn.execute("SELECT id, link FROM articles").fetchone()
        aid, link = row["id"], row["link"]
        # 摆局:行被别的路径清掉了,而且没有留下任何排除记录
        self.repo.conn.execute("DELETE FROM articles WHERE id=?", (aid,))
        self.repo.commit()
        self.assertFalse(self.repo.is_deleted_blacklist(aid), "前提:名单里没有它")

        # ③ 既没有凭据、也算不出身份 —— 拒,一个字都不许写
        code, got = drop("--id", aid)
        self.assertEqual(cli.EXIT_ERROR, code)
        self.assertFalse(got["ok"])
        self.assertIn("没法承诺", got["detail"])
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM deleted_articles").fetchone()[0])

        # ② 给了链接:当场把那组身份补进名单,再报成功
        code, got = drop("--id", aid, "--link", link)
        self.assertEqual(cli.EXIT_OK, code)
        self.assertTrue(got["ok"] and got["excluded"])
        self.assertTrue(self.repo.is_deleted_blacklist(article_identity(link)))
        self.assertTrue(self.repo.is_deleted_blacklist(stable_article_id(link)))

        # 后果:下一轮发现同一篇不许入库、不许抓正文
        self.fill.reset_mock()
        self.assertEqual([], self.collect()["errors"])
        self.assertEqual(0, self.repo.conn.execute(
            "SELECT COUNT(*) FROM articles").fetchone()[0])
        self.fill.assert_not_called()

        # ① 名单里已经有它:连点一次照样成功,而且不用再给链接
        code, got = drop("--id", aid)
        self.assertEqual(cli.EXIT_OK, code)
        self.assertTrue(got["ok"] and got["excluded"])
