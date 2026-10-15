"""导出那一步:**发布是永久排除的最后一道,也是唯一一道真不变量。**

删除只约束采集库的新写入还不够 —— 导出拿的是几分钟前读出来的候选和正文快照,
而它写出去的 md 是**不可撤销的**:进了 Vault 之后,用户下一次同步不会让它自己消失,
而面板上那颗「不再抓取」承诺的是「以后不再采集」。三层判据:
①候选筛选按**完整身份**走,不只比档案 ID;
②发布那一刻在写事务里**再复核一次**(候选读出来之后被移除的,一个字节都不许写);
③`articles` 里按请求的 ID 查不到时,先按链接身份找出同一原文的**另一个档案 ID**
  —— 那一行还在,只把请求方给的键记进名单等于没删。

硬约束、事故形态和反向验证红数在 `docs/decisions/failure-drop.md` 第一节。
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest.mock import patch

from src.utils.article_identity import (
    article_identity,
    exclusion_keys,
    stable_article_id,
)
from tests.local_collect_case import LocalCollectCase


class ExportPublishTests(LocalCollectCase):
    def _export(self, *extra):
        """跑一趟真实导出,返回 `(stdout, 写出来的 md 列表)`。"""
        import sys

        from scripts import export_obsidian

        out_dir = Path(self.tmp.name) / "vault"
        with patch.object(sys, "argv", ["export", "--out", str(out_dir),
                                        "--dedup-root", str(out_dir),
                                        "--no-images", *extra]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, export_obsidian.main())
        return output.getvalue(), sorted(out_dir.rglob("*.md"))

    def _drop(self, *argv):
        import cli
        import json as _json

        with contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["articles", "drop", *argv, "--json"])
        return code, _json.loads(out.getvalue())

    def test_an_article_removed_after_the_candidates_were_read_is_not_published(self):
        """候选和正文都读出来了,这一刻用户按下「不再抓取」:**不许再写出去**。

        真实时序就是这样:一轮导出要下几百张图、写几百个文件,而旧页面上那个确认框
        可能在几分钟前就打开了。第 2 轮审查实测:导出读到正文之后完成移除,删除报
        `ok=true, removed=true`、库里 `remaining_articles=0`,导出照旧返回 0 并
        **实际写出 1 篇带正文的 md**。只把检查挪到循环末尾是同一类 TOCTOU。

        判据落在**磁盘上有没有那个文件**上,不落在「调了 publishable 没有」。
        """
        from scripts import export_obsidian

        self.collect()
        real = export_obsidian.load_articles

        def load_then_drop(repo, account):
            rows = real(repo, account)      # 候选 + 正文已经是快照了
            self._drop_now()                # 用户这一刻按下按钮
            return rows

        with patch.object(export_obsidian, "load_articles", load_then_drop):
            out, files = self._export()
        self.assertEqual([], files, "已经承诺永久移除的文章不许进 Vault")
        self.assertIn("已永久移除 1", out)
        self.assertIn("导出 0", out)

        # 反过来钉一边:没被移除的那一篇照旧发布,正文照旧写进去
        self._reset_library()
        self.collect()
        out, files = self._export()
        self.assertEqual(1, len(files), out)
        self.assertIn("真实正文测试内容", files[0].read_text(encoding="utf-8"))
        self.assertIn("已永久移除 0", out)

    def test_the_candidate_filter_uses_the_full_identity_not_just_the_archive_id(self):
        """候选筛选要按**完整身份**:行还在、而链接身份在名单里的,也不是候选。

        存量形态:库被别的路径写过、回退到旧备份、或者那次写入发生在判据落地之前。
        SQL 里那句只能比 `a.id`(链接身份是 Python 规范化出来的),只按 ID 筛会把
        这一篇当成正常候选。

        这一道是主路;发布那一刻的复核是后路,两边各有自己的红。
        """
        from scripts.export_obsidian import load_articles

        self.collect()
        row = self.repo.conn.execute("SELECT id, link FROM articles").fetchone()
        self.assertEqual([row["id"]],
                         [a["id"] for a in load_articles(self.repo, None)])

        # 只记链接身份,不碰档案 ID —— SQL 那句看不见它
        self.repo.exclude_identities(article_identity(row["link"]))
        self.repo.commit()
        self.assertEqual([], load_articles(self.repo, None),
                         "只按档案 ID 筛会漏掉同一原文的另一个身份")
        out, files = self._export()
        self.assertEqual([], files)
        self.assertIn("库内候选 0", out)

    def test_dropping_by_an_old_id_removes_the_row_saved_under_another_archive_id(self):
        """按请求的 ID 查不到时,先按链接身份找出同一原文的**另一个档案 ID**。

        同一篇文章换个通道、或者换一份采集库之后会以另一个档案 ID 保存
        (`prepare_article` 沿用旧 ID 就是这种情形的来源),而旧页面手里还是老那个。
        漏掉这一步的话:请求方给的那组键记进了名单、**库里那一行照旧在**,于是
        删除之后才启动的导出仍然把它写进知识库(第 2 轮审查实测:删除报
        `ok=true, excluded=true`、库里仍有 1 行、导出实际写出 1 篇 md)。

        判据落在**库里那一行在不在、Vault 里有没有那个文件**上。
        """
        self.collect()
        row = self.repo.conn.execute(
            "SELECT id, link, title, create_time FROM articles").fetchone()
        old_id, link = row["id"], row["link"]

        # 摆局:库被回退/换通道,同一原文以另一个历史档案 ID 保存,没留排除记录
        self.repo.conn.execute("DELETE FROM articles WHERE id=?", (old_id,))
        self.repo.commit()
        self.assertTrue(self.repo.upsert_article_from_blog("档案名", {
            "id": "legacy-id", "link": link, "title": row["title"],
            "create_time": row["create_time"]}))
        self.assertTrue(self.repo.set_body_text("legacy-id", ["正文"], link=link))
        self.repo.commit()
        self.assertFalse(self.repo.is_deleted_blacklist("legacy-id"),
                         "前提:名单里没有它")

        # 反过来钉一边:归属对不上一律拒 —— 按链接找到的那一行也要核对
        import cli

        code, got = self._drop("--id", old_id, "--link", link, "--account", "别的号")
        self.assertEqual(cli.EXIT_ERROR, code)
        self.assertFalse(got["ok"])
        self.assertEqual(1, self._count("articles"), "归属对不上时一个字都不许动")

        # 旧页面手里还是老那个 ID,链接是同一篇
        code, got = self._drop("--id", old_id, "--link", link, "--account", "档案名")
        self.assertEqual(cli.EXIT_OK, code)
        self.assertTrue(got["ok"] and got["removed"])
        self.assertEqual(0, self._count("articles"), "库里那一行必须真的删掉")
        self.assertEqual(0, self._count("article_bodies"))

        # 后果一:导出一个字节都不发布
        out, files = self._export()
        self.assertEqual([], files)
        self.assertIn("库内候选 0", out)
        # 后果二:名单里四个键都在 —— 换个通道再发现同一篇也挡得住
        for key in (*exclusion_keys(old_id, link), "legacy-id"):
            self.assertTrue(self.repo.is_deleted_blacklist(key), key)
        self.assertTrue(self.repo.is_deleted_blacklist(stable_article_id(link)))
        # 后果三:下一轮同步再发现同一篇,不入库也不抓正文
        self.fill.reset_mock()
        self.assertEqual([], self.collect()["errors"])
        self.assertEqual(0, self._count("articles"))
        self.fill.assert_not_called()
