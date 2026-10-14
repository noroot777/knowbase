# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""「登记 content_hash 即声明已索引」—— 两条旁路也得证得出来(kb/doc_rewrite.py)。

被测的那件事:`documents.content_hash` 那一句写的是「这份文件已经索引过了」,
而索引判「要不要重建」只看它。签错这张收据的后果**不可恢复**:别人那份还没索引
的改动从此永远进不了 chunks —— 磁盘上读出来是新的、搜索里是旧的,点开文章是新的,
再同步、再索引都不会自己纠正。

两条旁路原来整段在锁外(wiki 日报的双链化、YouTube 导语回填),于是「写完文件 →
刷 hash」之间另一个进程的一次导出就够把那个末态造出来:`WHERE content_hash=expect`
只问「**库**里记的还是我动手之前那一份」,答不了「**磁盘**上还是我写的那一份」
(2026-10-14 审查用独立子进程在两条旁路上都稳定复现了)。

所以判据是三样,**各有射程、缺一不可**(`doc_rewrite.register_written`,
**所有登记都必经的那一个边界**):

  1. **登记的是本次变换自己写出去的那一份** —— 调用方交付写出快照,退出前再读
     一次磁盘比对。管的是「写回到登记之间磁盘被换掉」。**「刚读到」不能充当
     「刚写出」**:主回写原来就是事后重读磁盘来当凭据,于是这道证明在那条路上
     压根不成立 —— 审查用独立子进程只改一行 tag(或 url / published / source /
     小节标题)就让它盖了章。
  2. **这一份现在重新索引一次,库里不会变**(`index_projection_matches`)——
     比的是 `store.upsert_document` 真正写进库的每一样,不是模型输入那三样。
     管的是「变换自己越界改了索引看得见的东西」,那一档磁盘上确实是我写的那份,
     ① 对它是瞎的。
  3. **库里记的还是动手之前那个 hash** —— 条件 UPDATE 自己的 WHERE。

外加一层:整段在维护锁里,认这把锁的那几条路(导出、索引、清理)压根进不来。
**这一层和上面两道也要各自有红**:子进程认锁时取不到(窗口是关着的),子进程
不认锁时也不许被认证成已索引。

打桩只打在「什么时候插进来」这一个点上(`Rewriting.write` 刚写完那一瞬 —— 原缺陷的
窗口就在这里);读文件、变换、写回、核对、登记、索引、搜索全部真跑,插进来的那一下
是**真的独立子进程**。
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "wiki"))

from kb import doc_rewrite, maintenance, scanner          # noqa: E402

import backfill_youtube_summaries as summaries            # noqa: E402
import wiki_daily                                         # noqa: E402
import wikilib                                            # noqa: E402

from doc_rewrite_fixture import NEW_TOKEN, OLD_TOKEN, Vault   # noqa: E402

class LockWaitTests(Vault):
    """新取的这把锁要守这把锁自己的规矩:等的时候出声,等不到就如实收场。"""

    def setUp(self):
        super().setUp()
        # 等不到就立刻抛 —— 生产上限是 15 分钟,用例不等那么久
        p = mock.patch.object(maintenance, "WAIT_SECONDS", 0)
        p.start()
        self.addCleanup(p.stop)

    def test_the_entry_speaks_up_while_it_waits_and_does_not_swallow_the_timeout(self):
        """协调入口等锁时出声,等不到就把 `LockBusy` 原样抛出去。

        不出声的话,一轮正在跑的索引会让这两条控制台脚本看起来就是挂住了(上限
        15 分钟);吞掉 `LockBusy` 更糟 —— 调用方会以为「这一篇处理过了」。
        """
        rel = self.seed("gzh")
        before, said = self.disk(rel), []
        with self.someone_else_holds_the_lock():
            with self.assertRaises(maintenance.LockBusy):
                with doc_rewrite.rewriting(self.conn, rel,
                                           on_wait=said.append):
                    self.fail("锁还在别人手里,却进了协调区间")
        self.assertTrue(said, "等锁的时候一声不出")
        self.assertEqual(before, self.disk(rel), "没拿到锁却动了这篇 md")

    def test_the_entry_speaks_up_by_default(self):
        """**默认就出声**,静默得显式要 —— 新调用方漏传不该变成「安静挂住」。"""
        rel = self.seed("gzh")
        out = io.StringIO()
        with self.someone_else_holds_the_lock(), \
             contextlib.redirect_stdout(out), \
             self.assertRaises(maintenance.LockBusy):
            with doc_rewrite.rewriting(self.conn, rel):
                pass
        self.assertIn("等维护锁", out.getvalue())

    def test_a_lock_timeout_does_not_take_down_the_rest_of_the_wiki_stage(self):
        """linkify 等不到锁:这一步到此为止,**后面几步照跑**,退出码还是 0。

        双链化按 GRAPH_MARK 幂等,剩下那些下一轮原样补上;而实体页/主题页/index/
        图谱压根不写文章文件,没有理由跟着停。让 `LockBusy` 冒上去的话,一次锁
        超时就把整个 wiki 阶段后面几步一起带崩,面板上那张卡片直接标红。
        """
        rel = self.seed("gzh")
        roster = self.universe()
        out = io.StringIO()
        with self.someone_else_holds_the_lock(), \
             contextlib.redirect_stdout(out):
            self.assertEqual(0, wiki_daily.step_linkify(roster))
        self.assertIn("等不到维护锁", out.getvalue())
        self.assertNotIn(wikilib.GRAPH_MARK, self.disk(rel),
                         "锁在别人手里,这篇 md 却被改了")
        # 锁放开之后下一轮原样补上 —— 「到此为止」不是「永久跳过」
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, wiki_daily.step_linkify(self.universe()))
        self.assertIn(wikilib.GRAPH_MARK, self.disk(rel))

    def test_the_summary_backfill_reports_a_lock_timeout_in_its_exit_code(self):
        """回填脚本等不到锁:退 2(同 `kb index` 那条口径),而且一个字节没改。"""
        rel = self.seed("youtube")
        out = io.StringIO()
        with self.someone_else_holds_the_lock(), \
             contextlib.redirect_stdout(out), \
             mock.patch.object(sys, "argv", ["backfill_youtube_summaries.py"]):
            self.assertEqual(2, summaries.main())
        self.assertIn("等不到维护锁", out.getvalue())
        self.assertNotIn("summary:", self.disk(rel))
        self.assertEqual("", self.summary(rel))


class LockWindowTests(Vault):
    """认这把锁的那种写入,在协调区间里压根插不进来。"""

    def assert_the_window_is_shut(self, kind: str):
        """跑那条旁路,把子进程安排在「已写回、还没登记」那一瞬,断末态 + 过程。"""
        rel = self.seed("youtube" if kind == "youtube" else "gzh")
        with self.rival_slips_in_right_after_the_write(rel, lock=True) as seen:
            self.run_bypass(kind)
        # 末态先断:磁盘上就是旁路自己写出去的那一份,库里登记的也是它 ——
        # 没有任何「磁盘和 chunks 各一版」的分裂。
        disk = self.disk(rel)
        self.assertNotIn(NEW_TOKEN, disk, "子进程居然改掉了这篇 md")
        self.assertIn(OLD_TOKEN, disk)
        self.assertFalse(self.needs_index(rel), "旁路自己写出去的那一份没登记上")
        self.assertEqual([rel], self.hits(OLD_TOKEN))
        # 过程证据断在后面:它才是「窗口关着」的那张票。
        child = seen.get("child")
        self.assertIsNotNone(child, "压根没走到登记那一步")
        self.assertNotEqual(0, child.returncode,
                            f"子进程在协调区间里拿到了维护锁:{child.stdout}")
        self.assertIn("LockBusy", child.stderr,
                      f"子进程不是因为等不到锁才失败的:{child.stderr[-800:]}")

    def test_the_summary_backfill_holds_the_lock_across_its_window(self):
        self.assert_the_window_is_shut("youtube")

    def test_the_linkify_step_holds_the_lock_across_its_window(self):
        self.assert_the_window_is_shut("wiki")

    def test_the_rival_really_can_take_the_lock_when_nobody_holds_it(self):
        """反过来钉一边:没人持锁时那个子进程是**取得到**锁的。

        不钉的话上面那条用例对「协调区间有没有持锁」不敏感 —— 子进程因为导入
        失败、路径写错而非零退出,看起来和「窗口关着」一模一样,而 `LockBusy`
        那句断言也可能恰好来自别的地方。
        """
        rel = self.seed("gzh")
        child = self.rival(rel, lock=True)
        self.assertEqual(0, child.returncode,
                         f"子进程连空闲的锁都取不到:{child.stderr[-800:]}")
        self.assertIn("rival-wrote-the-file", child.stdout)
        self.assertIn(NEW_TOKEN, self.disk(rel))


class UncertifiedWriteTests(Vault):
    """不认这把锁的那种写入插进来了 —— 那就**保留待索引**,不许认证。"""

    def assert_the_new_body_is_not_lost(self, kind: str):
        """不认锁的那个进程插进来之后:这一篇继续待索引,一趟索引就把新正文补上。

        这是审查那条断言的反面:被审那一版在这里登记了子进程那份未索引新文件的
        hash,于是连跑两趟 `kb index` 都报「未变」,新正文一个字都搜不到,再走
        一轮同步的索引+提取也不恢复。
        """
        rel = self.seed("youtube" if kind == "youtube" else "gzh")
        v0 = self.version(rel)
        with self.rival_slips_in_right_after_the_write(rel, lock=False) as seen:
            self.run_bypass(kind)
        self.assertEqual(0, seen["child"].returncode, seen["child"].stderr)
        # 子进程那份新正文确实在磁盘上、确实还没进索引
        self.assertIn(NEW_TOKEN, self.disk(rel))
        self.assertEqual(v0, self.version(rel),
                         "把另一个进程那份未索引的改动认证成「已索引」了")
        self.assertTrue(self.needs_index(rel), "这一篇不再带着「待索引」")
        self.assertEqual([], self.hits(NEW_TOKEN), "摆盘没摆出「还没索引」")
        # 后果:一趟索引就补上了,搜得到新正文、搜不到旧正文
        self.reindex()
        self.assertEqual([rel], self.hits(NEW_TOKEN),
                         "新正文永久搜不到 —— 索引被那张假收据跳过了")
        self.assertEqual([], self.hits(OLD_TOKEN))
        self.assertFalse(self.needs_index(rel))

    def test_the_summary_backfill_does_not_certify_someone_elses_write(self):
        self.assert_the_new_body_is_not_lost("youtube")

    def test_the_linkify_step_does_not_certify_someone_elses_write(self):
        self.assert_the_new_body_is_not_lost("wiki")

    def test_a_transform_that_moves_the_index_input_is_not_certified(self):
        """第二道判据自己的红:磁盘上确实是我写的那份,但它改了索引看得见的东西。

        ① 那道(「登记的是我写出去的那一份」)对这一档是瞎的 —— 文件分毫不差就是
        变换的产物。没有这一道,一个越界的变换(往正文里塞字、改标题)会被登记成
        「已索引」,它改出来的内容从此永远进不了 chunks。
        """
        rel = self.seed("gzh")
        v0 = self.version(rel)
        with doc_rewrite.rewriting(self.conn, rel) as doc:
            doc.write(doc.text.replace(OLD_TOKEN, "越界改动的正文"))
        self.assertTrue(doc.changed)
        self.assertFalse(doc.indexed, "改了索引看得见的东西却被认证成「已索引」")
        self.assertIn("分块", doc.reason)
        self.assertEqual(v0, self.version(rel))
        self.assertTrue(self.needs_index(rel))

    def test_a_foreign_frontmatter_edit_is_not_certified_either(self):
        """「同一快照」那道判据自己的红:外来改动没动索引输入,也不许被认证。

        这一档里第二道判据(「索引输入还是索引里那一组」)是**瞎的** —— 别人只
        加了一行 tag,标题/账号/分块正文分毫不差。可索引写进库的不只那三样:
        `doc_tags`、`url`、`published` 都按 frontmatter 重建。认证下去,这一行手工
        加的 tag 就永远进不了 `doc_tags`,而按标签过滤的查询从此照旧捞不到它。
        所以登记的必须是**本次变换自己写出去的那一份**,不是「磁盘上那份看着也
        自洽」。
        """
        rel = self.seed("gzh")
        v0 = self.version(rel)
        foreign = self.md_text("gzh", f"{OLD_TOKEN} 旧版导语与旧版正文。").replace(
            f"  - source/{self.SOURCES['gzh']}\n",
            f"  - source/{self.SOURCES['gzh']}\n  - topic/手工加的\n")
        with self.rival_slips_in_right_after_the_write(rel, lock=False,
                                                      text=foreign):
            self.run_bypass("wiki")
        # 索引输入确实没动 —— 第二道判据对这一档没有意见
        self.assertTrue(doc_rewrite.index_input_matches(
            self.conn, rel, self.disk(rel)), "摆盘没摆出「索引输入没动」那一档")
        self.assertEqual(v0, self.version(rel),
                         "把别人那行手工 tag 认证成「已索引」了")
        self.assertTrue(self.needs_index(rel))
        self.reindex()
        self.assertIn("topic/手工加的", self.tags(rel),
                      "那行手工 tag 永久进不了 doc_tags")

    def test_a_display_only_transform_still_gets_certified(self):
        """反过来钉一边:只改展示层的那一次照旧登记得上。

        不钉的话「从此一次都不登记」也是全绿的 —— 而那会让每一轮索引把双链化过的
        整批文章白重建一遍(chunk 重切、向量重编码),代价按篇数线性涨。
        """
        rel = self.seed("gzh")
        v0 = self.version(rel)
        with doc_rewrite.rewriting(self.conn, rel) as doc:
            doc.write(doc.text.replace("tags:", "wiki_round: 2\ntags:"))
        self.assertEqual((True, True, ""), (doc.changed, doc.indexed, doc.reason))
        self.assertNotEqual(v0, self.version(rel))
        self.assertFalse(self.needs_index(rel))
        self.assertEqual([rel], self.hits(OLD_TOKEN), "正文被动了")

    def test_a_doc_the_cleanup_deleted_is_not_touched(self):
        """等锁期间清理把这一篇删掉了 —— 不给快照、不写文件,也不许抛。

        这里是「整份读改写」:写回会把 md **重新创建出来**,下一轮索引合法地把它
        收回库里,而 feeds 行已经没了,重跑清理只会说「无事可做」。

        **两半各自摆一次,因为挡住它们的不是同一道。** md 已经没了那半有两道:
        `_still_indexed` 和锁内那次读(读不出来 → 没有快照),所以把前者去掉它的
        **后果**那条断言照旧绿 —— 只有「原因说的是哪一档」会变。而 **md 还在、库里
        那行先没了**那半只有 `_still_indexed` 答得出来(清理正是先删库里那行),
        去掉它当场写坏那篇 md。所以第二半才是那道检查的后果式红。
        """
        for half, drop_file in (("md 和库里那行都没了", True),
                                ("库里那行先没了、md 还在", False)):
            with self.subTest(half):
                rel = self.seed("gzh")
                before = self.disk(rel)
                if drop_file:
                    (self.vault / rel).unlink()
                self.conn.execute("DELETE FROM documents WHERE path=?", (rel,))
                self.conn.commit()
                with doc_rewrite.rewriting(self.conn, rel) as doc:
                    self.assertIsNone(doc.text, "这一篇已经不在了,却还给出了快照")
                self.assertEqual((False, False), (doc.changed, doc.indexed))
                self.assertIn("不在", doc.reason)
                if drop_file:
                    self.assertFalse((self.vault / rel).exists(),
                                     "被清理删掉的 md 被写回来了")
                else:
                    self.assertEqual(before, self.disk(rel),
                                     "库里那行已经没了,这篇 md 还是被改了")

    def test_the_linkify_step_skips_a_doc_the_cleanup_took_after_its_roster(self):
        """清理在「名单取好」之后才动手 —— 真走 `step_linkify` 也不许把 md 写回来。

        上面那条钉的是协调入口这个原语,这一条钉的是**旁路自己**:`u.docs` 是
        一份快照,等锁的这段时间里清理完全可以把其中一篇从 feeds.db / vault /
        kb.db 三处删干净。写回会把 md 文件**重新创建出来**,下一轮索引合法地把它
        收回库里,而 feeds 行已经没了,重跑清理只会说「无事可做」—— 比索引那条路
        更糟,那一条下一轮 `remove_missing_documents` 还收得掉。
        """
        rel = self.seed("gzh")
        roster = self.universe()                 # 名单在清理之前取好
        self.assertIn(rel, roster.docs, "夹具没把这一篇放进 linkify 名单")
        (self.vault / rel).unlink()              # 清理:vault + kb.db 一起删
        self.conn.execute("DELETE FROM documents WHERE path=?", (rel,))
        self.conn.commit()
        self.run_bypass("wiki", universe=roster)
        self.assertFalse((self.vault / rel).exists(), "被清理删掉的 md 被写回来了")
        self.assertEqual(0, self.conn.execute(
            "SELECT count(*) FROM documents WHERE path=?", (rel,)).fetchone()[0])

    def test_one_coordination_window_only_writes_once(self):
        """一个协调区间只许写回一次 —— 第二次就有两份快照了。

        两份快照正是两条旁路原来的形状:算新文本读到的那一份和算 `before` 读到的
        那一份可以隔着别人的一次写入,而登记只证明得了最后那一份。
        """
        rel = self.seed("gzh")
        with self.assertRaises(RuntimeError):
            with doc_rewrite.rewriting(self.conn, rel) as doc:
                doc.write(doc.text + "\n甲\n")
                doc.write(doc.text + "\n乙\n")
        # 第一次那份照旧登记上(异常往外飞的时候已经改掉的文件也要登记)
        self.assertTrue(doc.changed)
        self.assertIn("甲", self.disk(rel))
        self.assertNotIn("乙", self.disk(rel))


class MainWritebackTests(Vault):
    """提取的 tag 回写那条主路 —— 和两条旁路同一道边界、同一组证明。

    它原来是另一个形状:`refresh_content_hash` 自己**再读一次磁盘**,把读到的
    那一份当成「我写出去的那一份」交给登记。于是「登记的是本次写出去的那一份」
    这道证明在这条路上压根不成立,而那三样模型输入(标题/账号/拼接正文)对下面
    这五种外来编辑**全都看不出来**:审查按字段逐个实测,五种都被盖了章,连跑
    两趟 `kb index` 都报「未变 1」——新标签查不到、小节标题搜不到、原文链接和
    发布日期一直是旧的,再同步也不恢复。

    这条路的消费方有四个(管道 `_extract_pass`、`kb extract --writeback`、
    `scripts/extract_parallel.py`、软广补判经 `sync_promo_tag` 进来),它们共用
    这一份,所以这里按**那一份**摆局,不逐个消费方重摆一遍。
    """

    def assert_a_foreign_edit_is_not_certified(self, **field):
        """外来编辑只动一样索引看得见的东西 —— 不许登记,下一轮重建要补上。"""
        rel = self.seed("gzh")
        v0 = self.version(rel)
        foreign = self.md_with("gzh", **field)
        with self.rival_slips_in_after_the_main_writeback(
                rel, lock=False, text=foreign) as seen:
            self.assertTrue(self.run_main_writeback(rel), "主回写没改文件")
        self.assertEqual(0, seen["child"].returncode, seen["child"].stderr)
        self.assertEqual(foreign, self.disk(rel), "摆盘没摆成「外来那一份在磁盘上」")
        self.assertEqual(v0, self.version(rel),
                         "把另一个进程那份未索引的改动认证成「已索引」了")
        self.assertTrue(self.needs_index(rel), "这一篇不再带着「待索引」")
        self.assertIn("没登记已索引", self.said, "没登记却一声不出")
        return rel

    def test_a_foreign_tag_edit_is_not_certified(self):
        rel = self.assert_a_foreign_edit_is_not_certified(
            tags=["source/dr-gzh", "topic/手工加的"])
        self.reindex()
        self.assertIn("topic/手工加的", self.tags(rel),
                      "那行手工 tag 永久进不了 doc_tags")

    def test_a_foreign_url_edit_is_not_certified(self):
        rel = self.assert_a_foreign_edit_is_not_certified(
            url="https://example.invalid/moved")
        self.reindex()
        self.assertEqual("https://example.invalid/moved", self.col(rel, "url"),
                         "原文链接永远停在旧地址")

    def test_a_foreign_published_edit_is_not_certified(self):
        rel = self.assert_a_foreign_edit_is_not_certified(published="2026-10-13")
        self.reindex()
        self.assertEqual("2026-10-13", self.col(rel, "published"),
                         "发布日期永远停在旧值,按日期筛选也跟着错")

    def test_a_foreign_source_edit_is_not_certified(self):
        rel = self.assert_a_foreign_edit_is_not_certified(source="rss")
        self.reindex()
        self.assertEqual("rss", self.col(rel, "source"), "来源永远停在旧值")

    def test_a_foreign_heading_edit_is_not_certified(self):
        """小节标题那一档:拼接正文一个字没动,只有分块的 heading 变了。

        `chunks.heading` 进 `chunks_fts` 的 text_tok,所以搜新标题该命中。判据
        比的是逐块的 (seq, heading, text) 而不是拼起来的那一串,正是为了这一档。
        """
        rel = self.assert_a_foreign_edit_is_not_certified(heading="新小节标题")
        self.assertEqual([], self.hits("新小节标题"), "摆盘没摆出「还没索引」")
        self.reindex()
        self.assertEqual([rel], self.hits("新小节标题"),
                         "新小节标题永久搜不到 —— 索引被那张假收据跳过了")

    def test_the_written_snapshot_is_what_gets_the_receipt(self):
        """**第一道自己的红**:外来编辑没动索引投影,收据也不许盖到它头上。

        这一档 ② 是瞎的 —— 别人改的是 `summary:`(或者正文里的双链语法、图谱
        尾区),`normalize_body` 会把它们剥掉、frontmatter 里那个字段索引压根
        不读,所以「重新索引一次库里会变吗」的答案确实是「不会」。两道判据的
        射程在这里分开:**只有「登记的必须是我写出去的那一份」答得出来。**

        要钉的不是「数据丢了」(这一档两种形状末态都安全,投影本来就一致),而是
        `content_hash` 这一列的**语义只有一个**:它记的是我们验过投影、并且亲手
        写出去的那一份。盖到别人那份上之后,这一篇从此说「已索引」,而谁都没有
        对磁盘上**那一份**做过任何证明 —— 下一轮重建会正式过一遍判据,代价只是
        白重建一次;反过来,② 哪天漏了一样(索引多写一列、判据没跟上)这一道就是
        仅剩的那一层。
        """
        rel = self.seed("gzh")
        v0 = self.version(rel)
        # 外来那份要带上**这一轮已经落库**的那个 tag,否则不一致的是它、
        # 摆出来的就不是「没动索引投影」那一档了
        before = self.disk(rel)
        # 外来那一份改的得是**索引压根不读的**一个 frontmatter 键。原来这里加的是
        # `summary:`,它现在进投影了(upsert 之后那一步会把它写进
        # `documents.summary`),② 对它有意见 —— 那就不是这一档要摆的局了。
        foreign = self.md_with(
            "gzh", tags=[f"source/{self.SOURCES['gzh']}", "topic/审查复现"]
        ).replace('account: "审查隔离号"',
                  'account: "审查隔离号"\nnote: "别人刚加的一行备注"')
        with self.rival_slips_in_after_the_main_writeback(
                rel, lock=False, text=foreign) as seen:
            self.assertTrue(self.run_main_writeback(rel))
        self.assertEqual(0, seen["child"].returncode, seen["child"].stderr)
        # 先钉住摆盘:这一档的 ② 确实没有意见
        self.assertEqual("", doc_rewrite.index_projection_matches(
            self.conn, rel, before, self.disk(rel)),
            "摆盘没摆出「外来编辑没动索引投影」那一档")
        self.assertEqual(v0, self.version(rel),
                         "收据盖到了别人那一份上(我写出去的那一份已经没了)")
        self.assertTrue(self.needs_index(rel),
                        "这一篇被当成已索引,而磁盘上那一份谁都没证过")

    def test_a_tag_that_has_not_landed_in_the_db_is_not_certified(self):
        """**第二道自己的红**:回写写进 md 的 tag 还没落库,就不许登记。

        这一档 ① 是瞎的 —— 磁盘上分毫不差就是主回写自己写出去的那一份。真实
        链路是「落库 → 提交 → 回写 md」,所以两边本该一致;这里把落库那一步
        去掉,摆的就是「md 领先于 doc_tags」。认证下去,那几个 topic/entity tag
        永远进不了 `doc_tags`,按标签过滤的枚举和 wiki 层从此照旧看不见它们。
        """
        from kb import extract
        rel = self.seed("gzh")
        v0 = self.version(rel)
        said = io.StringIO()
        with contextlib.redirect_stdout(said):      # 落库那一步故意不做
            self.assertTrue(extract.writeback_extraction(
                self.conn, rel, {"topics": ["没落库的主题"], "entities": []}))
        self.assertIn("topic/没落库的主题", self.disk(rel), "tag 没写进 md")
        # 拒绝的理由必须是投影那一条 —— ① 对这一档没有意见(磁盘上分毫不差
        # 就是主回写自己写出去的那一份),所以这条红只能由 ② 给出
        self.assertIn("标签还没进 doc_tags", said.getvalue())
        self.assertNotIn("不是本次写回的那一份", said.getvalue())
        self.assertEqual(v0, self.version(rel),
                         "md 上那组 tag 还没进 doc_tags,却被认证成「已索引」")
        self.assertTrue(self.needs_index(rel))
        self.assertIn("没登记已索引", said.getvalue())
        self.reindex()
        self.assertIn("topic/没落库的主题", self.tags(rel),
                      "那组 tag 永久进不了 doc_tags")

    def test_a_legitimate_writeback_still_gets_certified(self):
        """反过来钉一边:正常那次回写照旧登记得上。

        不钉的话「主回写从此一次都登记不上」也全绿,代价是每轮索引把回写过的
        整批文章白重建一遍(chunk 重切 + 向量重编码),而且外面看不出来。
        """
        rel = self.seed("gzh")
        v0 = self.version(rel)
        self.assertTrue(self.run_main_writeback(rel))
        self.assertIn("topic/审查复现", self.disk(rel), "tag 没写进 frontmatter")
        self.assertNotEqual(v0, self.version(rel), "正常回写没登记上")
        self.assertFalse(self.needs_index(rel), "正常回写之后还带着「待索引」")
        self.assertNotIn("没登记已索引", self.said)

    def test_a_lock_respecting_rival_cannot_get_in_mid_writeback(self):
        """认锁的那个进程在主回写的窗口里拿不到锁 —— 这条路整段也在锁里。"""
        rel = self.seed("gzh")
        with self.rival_slips_in_after_the_main_writeback(
                rel, lock=True) as seen:
            self.assertTrue(self.run_main_writeback(rel))
        child = seen["child"]
        self.assertNotIn(NEW_TOKEN, self.disk(rel), "认锁的进程却改到了这篇")
        self.assertNotEqual(0, child.returncode, "它居然拿到了锁")
        self.assertIn("LockBusy", child.stderr)
        self.assertFalse(self.needs_index(rel), "窗口关着,这一次该登记上")



class BypassBehaviourTests(Vault):
    """两条旁路自己那点事:无人争抢时该做的事照旧做完。"""

    def test_the_summary_backfill_lands_the_same_snapshot_it_wrote_back(self):
        rel = self.seed("youtube")
        out = self.run_bypass("youtube")
        self.assertIn("补齐 1", out)
        disk = self.disk(rel)
        self.assertIn(f'summary: "{OLD_TOKEN}', disk, "md 里没写上导语")
        self.assertIn(OLD_TOKEN, self.summary(rel), "库里那一列没同步")
        # 库里那一列必须出自**写回去的那一份**,不是另一次读
        fm, _ = scanner.parse_frontmatter(disk)
        self.assertEqual(fm["summary"], self.summary(rel))
        self.assertFalse(self.needs_index(rel), "只改了 frontmatter 却没登记上")
        self.assertEqual([rel], self.hits(OLD_TOKEN))

    def test_the_linkify_step_decorates_and_registers(self):
        rel = self.seed("gzh")
        self.run_bypass("wiki")
        self.assertIn(wikilib.GRAPH_MARK, self.disk(rel), "没双链化")
        self.assertFalse(self.needs_index(rel), "双链化之后没登记上")
        self.assertEqual([rel], self.hits(OLD_TOKEN),
                         "图谱尾区进了 chunks(normalize_body 该把它剥掉)")
        # 幂等:再跑一轮不该再动它
        self.assertEqual(0, self._linkify_count())

    def _linkify_count(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return wiki_daily.step_linkify(self.universe())


if __name__ == "__main__":
    unittest.main()
