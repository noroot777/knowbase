# -*- coding: utf-8 -*-
"""磁盘上这一份 md 读不出来时,旧结果仍然不许固化(`kb/extract._input_moved`)。

被测的那件事:那道共享判据有两道问,② 只读库(索引里那组输入还是我读到的那组
吗)、① 要读磁盘(索引扫到当前这份 md 了吗)。原来 ① 读不出来就**整个函数**
提前返回「没搬」—— 于是连只读库、压根不会失败的 ② 也一起被跳过了。

**后果是永久的,而且页面上一切正常。** 一篇文章刚更新成普通教程、索引也已经
跟上,此刻它恰好读不出来(没权限、正被别人换着写),旧稿那次 `hard` 就连同旧
摘要和 `extracted_at` 一起落了库:这一篇从普通搜索里消失(按广告隐藏)、点开
却是新内容,`kb index` + `kb extract` 连跑都不恢复(`extracted_at` 一设上它
再也不进 `pending_docs`),连「免费修复标签」那条路都会照着错的 `hard` 把广告
标签补到新版教程上(2026-10-14 审查三次稳定复现 + 两组页面截图)。

**「读不出来」和「真的不在了」分成两档**,所以这里正反都钉:
- 文件真被清理掉(`FileNotFoundError`)照旧**放行** —— 那一档由回写那道锁内
  复查 + 外键级联管,在判据里改判成作废会让「回写在锁里」那组回归对它不再
  敏感(把那把锁去掉也照样全绿)。
- 别的读取失败一律**拒**,连「索引里那组输入压根没变」时也拒:那一刻 ① 答不
  出话,而误报的代价只是下一轮重提一次。

打桩只打在模型回包上;读正文、落库、回写、索引、`chmod(0)` 全部真跑。
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from kb import cli, extract, scanner, store
from kb.chunker import split_chunks
from kb.config import Source
from tests.promo_delivery_fixture import OLD_BODY, PATH, _Vault

NEW_BODY = "新版普通教程 currenttutorialtoken。只讲本地部署,没有任何交易。"
OLD_DATA = {"entities": [], "topics": ["旧广告主题"],
            "summary": "旧资讯稿尾巴的代充值推广",
            "promo": {"level": "hard", "reason": "只针对旧稿的广告结论"}}


class UnreadableInputCase(_Vault):
    """一篇判过 `none` 的普通文章 + 一份旧稿留下的提取结果,随时可以交付。"""

    def setUp(self):
        super().setUp()
        self.seed("none", OLD_BODY, False)
        # 清掉 `extracted_at`:夹具里那个值会让它不进 `pending_docs`(判据是
        # `IS NULL`,不是空串),而「下一轮还补不补得回来」正是这组用例的后果
        # 断言。
        self.conn.execute("UPDATE documents SET extracted_at=NULL WHERE path=?",
                          (PATH,))
        self.conn.commit()
        self.old_seen = extract.model_input(self.conn, PATH)["seen"]

    def unreadable(self):
        """真的把它设成读不出来(不打桩 `read_text`)。"""
        self.md.chmod(0)
        self.addCleanup(self.md.chmod, 0o644)
        with self.assertRaises(PermissionError):
            self.md.read_text(encoding="utf-8")

    def rewrite_and_reindex(self, body: str = NEW_BODY):
        """把正文换掉,再按索引那条真路径把新版扫进库 —— ② 从此答得出话。"""
        self.md.write_text(self.md_text(body, False), encoding="utf-8")
        with mock.patch.object(scanner, "VAULT_ROOT", self.vault):
            doc = scanner._doc_from_file(
                self.md, Source(name="dr-gzh", root=self.vault / "source/dr-gzh"))
        store.upsert_document(self.conn, doc, split_chunks(doc.body), "new")
        self.conn.commit()

    def state(self) -> dict:
        row = self.conn.execute(
            "SELECT promo_level, summary, extracted_at FROM documents WHERE path=?",
            (PATH,)).fetchone()
        return dict(level=row["promo_level"], summary=row["summary"],
                    extracted_at=row["extracted_at"],
                    body=store.doc_body(self.conn, PATH),
                    pending=[r["path"] for r in extract.pending_docs(self.conn)])


class UnreadableVersionTests(UnreadableInputCase):

    def test_an_unreadable_file_cannot_certify_a_verdict_from_the_old_input(self):
        """换版 + 索引跟上 + 这一刻读不出来 → 旧 `hard` 不许落库。

        后果按「下一轮还补不补得回来」钉:判定还是新版那个 `none`、摘要没被旧
        稿的盖掉、`extracted_at` 还空着、这一篇还在待提取集合里。
        """
        self.rewrite_and_reindex()
        self.assertNotEqual(self.old_seen,
                            extract.model_input(self.conn, PATH)["seen"])
        self.unreadable()
        with self.env(), self.assertRaises(extract.StaleBody):
            extract.commit_extraction(self.conn, PATH, OLD_DATA, self.old_seen)
        after = self.state()
        self.assertEqual(after["level"], "none")
        self.assertNotEqual(after["summary"], OLD_DATA["summary"])
        self.assertIsNone(after["extracted_at"])
        self.assertIn("currenttutorialtoken", after["body"])
        self.assertEqual(after["pending"], [PATH])

    def test_a_readable_file_still_refuses_the_same_stale_result(self):
        """反过来钉一边:同样换了版、文件可读时本来就拒 —— 上一条不是靠
        「读不出来」才拒的,两条路的末态必须一样。"""
        self.rewrite_and_reindex()
        with self.env(), self.assertRaises(extract.StaleBody):
            extract.commit_extraction(self.conn, PATH, OLD_DATA, self.old_seen)
        self.assertEqual(self.state()["level"], "none")

    def test_an_unreadable_file_is_refused_even_when_the_indexed_input_matches(self):
        """读不出来、而索引里那组输入压根没变 → 照样拒。

        ① 这一刻答不出「索引扫到当前这份 md 了吗」,而「没读到」不等于「证明了
        没变」。误报的代价只是这一篇下一轮重提一次 —— 所以后果钉在「它还在待
        提取集合里」。
        """
        self.unreadable()
        with self.env(), self.assertRaises(extract.StaleBody):
            extract.commit_extraction(self.conn, PATH, OLD_DATA, self.old_seen)
        after = self.state()
        self.assertIsNone(after["extracted_at"])
        self.assertEqual(after["pending"], [PATH])

    def test_a_readable_unchanged_file_still_commits(self):
        """反过来钉一边:没人动过、读得出来的那一篇照旧落库成功。

        少了这条,把判据写成「一律拒」也全绿 —— 那会让每一轮提取都白跑。
        """
        with self.env():
            extract.commit_extraction(self.conn, PATH, OLD_DATA, self.old_seen,
                                      writeback=False)
        after = self.state()
        self.assertEqual(after["level"], "hard")
        self.assertEqual(after["summary"], OLD_DATA["summary"])
        self.assertIsNotNone(after["extracted_at"])
        self.assertEqual(after["pending"], [])

    def test_a_file_that_is_really_gone_is_not_called_a_version_mismatch(self):
        """文件真被清理掉那一档**放行** —— 它不是这道判据的问题。

        管它的是**锁内**那道「这一篇还在吗」(`_still_indexed`),而且管得更干净:
        一个字都没落库。在判据里改判成作废只是把这条路换个形状,代价却是回写
        压根不会被调到,**于是「回写在锁里、读也不许读」那组回归对它不再敏感**
        (把那把锁去掉也照样全绿)。

        两档的后果按**调用方看得见的东西**区分:版本不对是抛 `StaleBody`
        (CLI 记一次「跳过」、这一篇留在队列里),这一档是安静返回 False、末态
        干净,随后由外键级联和下一轮 `remove_missing_documents` 收尾。
        """
        self.md.unlink()
        # **判据这一层也要钉**:末态那两行单独立不住 —— 锁内那道「还在吗」会先
        # 一步 return False,判据就算在这里改判成作废,末态照旧是「没落库」,
        # 用例对它不敏感(实测:把这一档改成拒,只断言末态的那一版全绿)。
        self.assertEqual(
            extract._input_moved(self.conn, PATH, self.old_seen), "")
        with self.env():
            committed = extract.commit_extraction(
                self.conn, PATH, OLD_DATA, self.old_seen, writeback=False)
        self.assertFalse(committed)
        self.assertIsNone(self.state()["extracted_at"])

    def test_a_gone_file_cannot_hide_a_proven_input_change(self):
        """反过来钉上一条的边:文件不在了、但索引里的指纹**已经能证明**输入换
        了 —— 那个证明不因为读不到文件就失效。

        这一对是「② 问在 ① 前面」的全部差别:反过来排的话,① 的
        `FileNotFoundError` 会先一步放行,把一份已经拿到手的反证扔掉。

        **钉在判据自己身上,因为三个消费点都先被别的闸拦住了**:
        `commit_extraction`、`writeback_extraction`、`commit_promo` 进锁后第一
        件事都是 `_still_indexed`(「这一篇还在 vault 吗」),文件不在时压根走
        不到落库。所以这个组合在今天的调用图里没有可观测的末态差别 —— 钉的是
        这份共享口径的产物本身:它有三个消费点,下一个消费点未必也带着那道闸。
        """
        self.rewrite_and_reindex()
        self.md.unlink()
        self.assertIn("输入已更新",
                      extract._input_moved(self.conn, PATH, self.old_seen))

    def test_the_lightweight_recheck_keeps_its_own_reach(self):
        """软广补判关掉的是 ①,没关 ② —— 读不出来也不该影响它的射程。

        它要的是「这个判定针对的正文还是当前那一份吗」,磁盘有没有进索引由
        `extract_promo.disk_input_indexed` 更精确地回答。这里只能钉到判据这一
        层:`commit_promo` 那条路自己要读文件,读不出来会抛,末态由它那组用例
        钉着。
        """
        self.unreadable()
        self.assertEqual(
            extract._input_moved(self.conn, PATH, self.old_seen,
                                 file_must_match=False), "")
        self.md.chmod(0o644)        # 换版要写得进去;不可读那一段上面验完了
        self.rewrite_and_reindex()
        self.assertIn("输入已更新", extract._input_moved(
            self.conn, PATH, self.old_seen, file_must_match=False))


class RealCliTests(UnreadableInputCase):
    """报告实测的那条用户可见后果:真实 `kb extract --writeback` 不许报成功。"""

    def cli_with_swap(self):
        """模型回包**之前**换版 + 让文件读不出来 —— 和生产里那个交错同形状。"""
        def model(title, body, **kw):
            self.rewrite_and_reindex()
            self.unreadable()
            return dict(OLD_DATA)

        with self.env() as out, \
                mock.patch.object(extract, "extract_with_llm", model):
            code = cli.cmd_extract(SimpleNamespace(limit=100, writeback=True))
        return code, out.getvalue()

    def test_the_cli_does_not_report_success_for_a_stale_verdict(self):
        code, output = self.cli_with_swap()
        self.assertNotIn("成功 1", output)
        self.assertIn("跳过 1", output)
        after = self.state()
        self.assertEqual(after["level"], "none")
        self.assertIsNone(after["extracted_at"])
        self.assertEqual(after["pending"], [PATH])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
