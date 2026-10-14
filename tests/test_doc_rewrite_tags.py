# -*- coding: utf-8 -*-
"""投影判据里**标签**这一样 —— 它不是集合全等,分三段判。

这一份单列出来是因为三段各有自己的一套前提,和「写出快照 / 分块 / 那几列」
那几条无关:

- **文件上有的,库里得都有**(一个方向)。反方向不能按全等判:`kb extract`
  不带 `--writeback` 时提取结果只落 `doc_tags`、不回写文件,那是合法状态。
  按全等判它,拒绝登记会把这一篇交给下一轮重建,而重建 `doc_tags` 是
  `DELETE` 全部再只从文件重建 —— 那批标签当场永久没了。
- **文件上刚删掉的,库里得也没了**。这一段靠「索引消费的那一份原文」
  (`before`)才判得出来:末态上「库里有、文件没有」的两种来源长得一模一样,
  只有拿原文比才分得开上面那个合法状态和「外来编辑刚把那行删了」。
- **`flag/软广` 那一行按落库那份判定归一化**,规则和索引自己用的是同一份
  (`store.promo_tag_wanted`):还没判过时跟着文件,判过了按判定。

行为那几层在 `tests/test_doc_rewrite.py`、名单在
`tests/test_doc_rewrite_roster.py`、摆盘在 `tests/doc_rewrite_fixture.py`。
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "wiki"))

from kb import doc_rewrite, scanner, store   # noqa: E402

import wikilib                               # noqa: E402

from doc_rewrite_fixture import OLD_TOKEN, Vault   # noqa: E402


class DbOnlyTagTests(Vault):
    """提取结果只落库、不回写文件 —— 那是**合法状态**,纯展示改写不许害了它。

    `kb extract` 不带 `--writeback` 是明确支持的操作:`topic/*`、`entity/*` 进
    `doc_tags`,md 文件上压根没有这几行,`extracted_at` 非空。差异在**反方向**
    (库里有、文件没有),不是「磁盘上有还没索引的改动」。

    把它按集合全等判成不一致的代价**不是**「白重建一次」:`upsert_document`
    重建 `doc_tags` 是 `DELETE` 全部再**只从文件**重建,所以那次「保守地留待
    下一轮」恰好触发了把这批标签永久删掉的那一次重建 —— 而 `extracted_at`
    非空,这一篇不会被重新提取回来。审查实测:一次双链化(或一次软广回写)之后
    连跑两轮索引,库里只剩 `source/*`,按主题和实体过滤全空,再同步也不恢复。
    """

    def seed_extracted_but_not_written_back(self) -> str:
        rel = self.seed("gzh")
        self.extract_without_writeback(rel)
        self.assertIn("topic/只落库的主题", self.tags(rel), "夹具没摆出库里那组标签")
        self.assertNotIn("topic/只落库的主题", self.disk(rel),
                         "摆盘写回了文件 —— 那就不是这一档了")
        return rel

    def assert_the_db_only_tags_survive(self, rel: str):
        """两轮索引之后那批只落库的标签还在、还搜得到。"""
        self.assertFalse(self.needs_index(rel),
                         "纯展示改写没登记上 —— 下一轮重建会吞掉那批提取标签")
        first, second = self.reindex(), self.reindex()
        self.assertIn("topic/只落库的主题", self.tags(rel),
                      f"主题标签被重建吞掉了({first.strip()} / {second.strip()})")
        self.assertIn("entity/reviewtool", self.tags(rel), "实体标签被吞掉了")

    def test_linkify_does_not_cost_the_db_only_tags(self):
        rel = self.seed_extracted_but_not_written_back()
        self.run_bypass("wiki")
        self.assertIn(wikilib.GRAPH_MARK, self.disk(rel), "没双链化")
        self.assert_the_db_only_tags_survive(rel)

    def test_a_promo_only_writeback_does_not_cost_the_db_only_tags(self):
        """只补软广那行标签的那条路(`commit_promo` → 主回写)同样不许害它。

        走真实的 `commit_promo`:它才是完整的那条链(条件写判定 → 对齐
        `doc_tags` 那行 → 提交 → 回写 md 那行),光调 `sync_promo_tag` 的话
        判定和库里那行标签都还没落,判据拒绝它是对的。
        """
        from kb import extract, store
        rel = self.seed_extracted_but_not_written_back()
        snap = store.doc_input_snapshot(self.conn, rel)
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            why, synced = extract.commit_promo(
                self.conn, rel, {"level": "hard", "reason": "文末在卖课"},
                snap["version"],
                extract.input_fingerprint(snap["title"], snap["account"],
                                          snap["body"]))
        self.assertEqual(("", True), (why, synced), said.getvalue())
        self.assertTrue(self.flag_in_md(rel), "那行 flag 没写进 md")
        self.assertNotIn("没登记已索引", said.getvalue(),
                         f"合法的软广回写没登记上:{said.getvalue()}")
        self.assert_the_db_only_tags_survive(rel)

    def test_a_foreign_tag_added_to_the_file_is_still_refused(self):
        """反过来钉一边:单向判据不许把「文件上多了一行」也放过去。

        这一档才是真的「磁盘领先于索引」—— 取消整个标签比较就会漏掉它,而那行
        手工 tag 从此永远进不了 `doc_tags`。
        """
        rel = self.seed_extracted_but_not_written_back()
        foreign = self.md_with(
            "gzh", tags=[f"source/{self.SOURCES['gzh']}", "topic/手工加的"])
        with self.rival_slips_in_right_after_the_write(rel, lock=False,
                                                      text=foreign):
            self.run_bypass("wiki")
        self.assertTrue(self.needs_index(rel),
                        "文件上多出来的那行标签被认证成「已索引」了")
        self.reindex()
        self.assertIn("topic/手工加的", self.tags(rel))

    def test_a_full_writeback_still_gets_certified(self):
        """对照:正常全量回写(tag 同时落库又写进文件)照旧登记得上。"""
        rel = self.seed("gzh")
        self.assertTrue(self.run_main_writeback(rel))
        self.assertFalse(self.needs_index(rel), "全量回写那条路没登记上")


class DeletedFileTagTests(Vault):
    """md 上**刚被删掉**的那行标签:单向那一段放它过去,所以要第二段来拦。

    两段的分界就是「这个标签原来在不在文件上」,而末态上看不出来 —— 判据必须
    拿「索引消费的那一份原文」比。审查实测只有单向那一段时的后果:外来编辑删掉
    md 上一行标签,收据照签,连跑两轮 `kb index` 都报「未变 1」,按那个标签过滤
    照旧命中这一篇,再同步也不恢复(强制重建才纠正得了)。

    摆盘走 `seed_with_extraction()`:它连 upsert 之后那一步也真跑过,所以这一篇
    的 `doc_tags` 和 `entities` 都是真实索引写下来的。
    """

    def test_a_tag_deleted_from_the_file_is_not_certified(self):
        rel = self.seed_with_extraction()
        self.assertIn("topic/removed", self.tags(rel), "摆盘:库里该有这行标签")
        gone = self.disk(rel).replace("  - topic/removed\n", "")
        # 注入在**写入函数读文件之前**:那个删除被带进我们自己写出去的全文,
        # 于是第一样证明合法通过,只有投影这一道拦得住(见注入器的 docstring)。
        with self.rival_slips_in_before_the_main_writeback_reads(
                rel, lock=False, text=gone) as seen:
            self.assertTrue(self.run_main_writeback(rel, topic="stable"))
        self.assertEqual(0, seen["child"].returncode, seen["child"].stderr)
        self.assertNotIn("topic/removed", self.disk(rel), "注入没落地")
        self.assertIn("删掉的那行标签", self.said, f"收据签了:{self.said}")
        self.assertTrue(self.needs_index(rel), "这一篇没留着「待索引」")
        self.assertEqual([rel], self.hits_tagged(OLD_TOKEN, "topic/removed"),
                         "摆盘:重建之前按这个标签当然还筛得到")
        self.reindex()
        self.assertNotIn("topic/removed", self.tags(rel),
                         "下一轮重建没把删除收进索引")
        self.assertEqual([], self.hits_tagged(OLD_TOKEN, "topic/removed"),
                         "已从文件删掉的标签还筛得出这篇文章")

    def test_an_extraction_that_only_landed_in_the_db_does_not_block_the_receipt(self):
        """反过来钉一边:同一篇文章上**两种来源同时存在**时照旧签得下来。

        库里专有的那几行(提取没回写)不许被当成「刚删掉的」,而这一篇的 summary
        已经被模型那次提取盖掉、文件里仍写着旧的那句 —— 两样都不构成拒绝理由:
        文件自己这几句话一个字都没动。
        """
        rel = self.seed_with_extraction()
        self.extract_without_writeback(rel)
        self.assertIn("topic/只落库的主题", self.tags(rel), "摆盘:库里那组标签")
        self.assertEqual("审查复现", self.summary(rel),
                         "摆盘:模型那次提取该把 summary 盖掉")
        self.assertTrue(self.run_main_writeback(rel, topic="stable"))
        self.assertNotIn("没登记已索引", self.said, f"合法回写被拒了:{self.said}")
        self.assertFalse(self.needs_index(rel), "合法回写没登记上")
        first, second = self.reindex(), self.reindex()
        self.assertIn("topic/只落库的主题", self.tags(rel),
                      f"那批只落库的标签被吞了({first.strip()} / {second.strip()})")
        self.assertEqual("审查复现", self.summary(rel), "模型那句摘要被盖回去了")


class PromoFlagProjectionTests(Vault):
    """`flag/软广` 那一行:投影判据要和真实索引同一份规则。

    真实规则在 `store.promo_tag_wanted`:判过就按判定(hard 有 / none、soft 没
    有),**还没判过(`''`)就跟着文件** —— 那时 md 那行是提取阶段写进去的唯一
    记录,索引不去覆盖它。判据原来无条件把这一行从两边排除,于是还没判过的那
    一篇上,外来编辑增删它判据完全看不见:收据照签,而真实索引这时是跟着文件
    的,按这个标签筛选从此停在旧结果上,再同步也不恢复。
    """

    def seed_undecided(self, *, flagged: bool) -> str:
        """还没判过软广的一篇,md 上那行 flag 按 `flagged` 摆,库里和它一致。"""
        from kb.extract_promo import PROMO_TAG
        rel = self.seed("gzh")
        if flagged:
            text = self.md_with(
                "gzh", tags=[f"source/{self.SOURCES['gzh']}", PROMO_TAG])
            (self.vault / rel).write_text(text, encoding="utf-8")
            self.conn.execute("INSERT OR IGNORE INTO doc_tags (path, tag)"
                              " VALUES (?, ?)", (rel, PROMO_TAG))
            self.conn.execute("UPDATE documents SET content_hash=? WHERE path=?",
                              (scanner.file_hash(text), rel))
            self.conn.commit()
        self.assertEqual("", self.promo_level(rel), "夹具没摆成「还没判过」")
        self.assertEqual(flagged, self.flag_in_db(rel))
        self.assertEqual(flagged, self.flag_in_md(rel))
        return rel

    def assert_an_undecided_flag_edit_is_refused(self, *, flagged: bool):
        """外来编辑把那行 flag 增/删掉 —— 不许登记,一轮重建后库里跟着文件。

        **注入点在写入函数读文件之前**,所以主回写把那个改动带进了自己写出去
        的全文:①「磁盘上还是本次写出去的那一份」合法通过,这一档只有 ② 拦得住
        (注入点挪到写回之后的话,① 会先拦下来,这条用例对 ② 坏掉就不敏感了)。
        """
        from kb.extract_promo import PROMO_TAG
        rel = self.seed_undecided(flagged=flagged)
        tags = [f"source/{self.SOURCES['gzh']}"]
        if not flagged:                       # 原来没有 → 外来加上
            tags.append(PROMO_TAG)
        with self.rival_slips_in_before_the_main_writeback_reads(
                rel, lock=False, text=self.md_with("gzh", tags=tags)) as seen:
            self.run_main_writeback(rel)
        self.assertEqual(0, seen["child"].returncode, seen["child"].stderr)
        self.assertEqual(not flagged, self.flag_in_md(rel), "摆盘没摆成增/删")
        # 先钉住摆盘:① 对这一档确实没有意见(磁盘上就是主回写写出去的那一份)
        self.assertNotIn("不是本次写回的那一份", self.said)
        self.assertTrue(self.needs_index(rel),
                        "还没判过软广的那一篇,外来改那行 flag 被放过去了")
        self.reindex()
        self.assertEqual(not flagged, self.flag_in_db(rel),
                         "一轮重建之后库里那行 flag 该跟着文件")

    def test_an_added_flag_on_an_undecided_doc_is_refused(self):
        self.assert_an_undecided_flag_edit_is_refused(flagged=False)

    def test_a_removed_flag_on_an_undecided_doc_is_refused(self):
        self.assert_an_undecided_flag_edit_is_refused(flagged=True)

    def test_a_decided_verdict_ignores_the_file_flag(self):
        """控制:判过的那三档,md 上那行 flag 和库里不一致**不**构成拒绝理由
        —— 索引重建之后 `_align_promo_tag` 会按判定纠正它。"""
        from kb.extract_promo import PROMO_TAG
        for level, flagged_in_file in (("hard", False), ("none", True),
                                       ("soft", True)):
            with self.subTest(level=level):
                self.setUp()
                rel = self.seed_undecided(flagged=flagged_in_file)
                self.set_promo_level(rel, level)
                # 库里那行按判定摆好(= 上一次索引留下的末态)
                if level == "hard":
                    self.conn.execute("INSERT OR IGNORE INTO doc_tags"
                                      " (path, tag) VALUES (?, ?)",
                                      (rel, PROMO_TAG))
                else:
                    self.conn.execute("DELETE FROM doc_tags WHERE path=? AND"
                                      " tag=?", (rel, PROMO_TAG))
                self.conn.commit()
                # 谁都没改过这个文件,所以「动手之前那一份」就是它自己 ——
                # 要单独看的是软广那一段规则。
                self.assertEqual("", doc_rewrite.index_projection_matches(
                    self.conn, rel, self.disk(rel), self.disk(rel)),
                    "判定明确时,md 上那行 flag 不该构成「磁盘领先于索引」")

    def test_the_rule_matches_what_the_indexer_actually_does(self):
        """**两份实现的对账**:真跑 `upsert_document`,末态要和
        `store.promo_tag_wanted` 说的一样 —— 四档判定 × 文件有/无,八个组合。

        规则有两份实现(纯函数一份、`_align_promo_tag` 的两句 SQL 一份,后者
        必须把判据待在自己的 WHERE 里),所以要有一条回归把它们钉在一起:
        哪天一边改了另一边没跟上,这里红。
        """
        from kb.chunker import split_chunks
        from kb.extract_promo import PROMO_TAG
        for level in ("", "none", "soft", "hard"):
            for flagged in (False, True):
                with self.subTest(level=level or "未判过", in_file=flagged):
                    self.setUp()
                    rel = self.seed_undecided(flagged=flagged)
                    self.set_promo_level(rel, level)
                    doc = self.doc_of(rel)
                    store.upsert_document(self.conn, doc,
                                          split_chunks(doc.body), "对账")
                    self.conn.commit()
                    self.assertEqual(
                        store.promo_tag_wanted(level, flagged),
                        PROMO_TAG in self.tags(rel),
                        "索引真实末态和 `promo_tag_wanted` 对不上了")


if __name__ == "__main__":
    unittest.main()
