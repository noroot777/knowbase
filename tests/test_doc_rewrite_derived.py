# -*- coding: utf-8 -*-
"""投影判据里 **upsert 之后那一步** 派生的两样:`documents.summary` 和实体 kind。

**索引不是 `upsert_document` 完就完了。** 三个真实索引入口(`cli._index_pass`、
`pipeline_stages._index_pass`、`pipeline_stages._ingest_pass`)都在它之后接着跑
`parse_pretagged` → `save_extraction`,把 frontmatter 里的 `summary` 和
`project`/`kind` 写进 `documents.summary` 和 `entities`。这两样同样在
`content_hash` 跳过的射程里,而且是用户查得到的东西(`aggregate` 的实体枚举按
`entities.kind` 筛、把 `documents.summary` 返回出去),不是展示层。

**这一层的判法和前面几样不同,所以单列一份。** 前面那些(五列、分块)库里的值
**就是**上一次索引从文件读出来的,直接和磁盘这一份比就对了;这两样库里的值可能
压根不是从文件来的 —— 走 `kb extract --writeback` 回写过的普通文章,md 里有
topic/entity 标签却没有 `summary:`/`project:`/`kind:` 那几行,而库里那两样是模型
给的。那种 md 重新索引一次,`parse_pretagged` 会拿 `""` 和 `other` 盖掉它们,所以
「库里和这一份重新派生的结果不等」在那一篇上是**常态**。拿它当拒绝理由等于给主
回写判死刑:拒绝 → 下一轮重建 → 模型摘要和 kind 当场没了,`extracted_at` 非空也
不会重提。**所以判的是「文件自己这句话动没动」**,两个方向各有一条用例钉着。

摆盘在 `tests/doc_rewrite_fixture.py`(`seed_with_extraction()` 连这一步也真跑
过)、标签那三段在 `tests/test_doc_rewrite_tags.py`、名单在
`tests/test_doc_rewrite_roster.py`。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "wiki"))

from kb import doc_rewrite             # noqa: E402

from doc_rewrite_fixture import Vault   # noqa: E402


class DerivedWriteTests(Vault):
    """外来编辑动了文件那两句话 —— 收据不许签,下一轮重建把它收进索引。"""

    def refuse_then_rebuild(self, transform) -> str:
        """注入一次外来编辑,断言「没登记 + 留着待索引」,返回这一篇。

        注入点在**写入函数读文件之前**:那个改动被带进我们自己写出去的全文,
        于是第一样证明(磁盘上还是本次写出去的那一份)合法通过,只有投影这一道
        拦得住。两道对大多数外来编辑互为备份,时点选错就是一道坏了也全绿
        (见注入器自己的 docstring)。
        """
        rel = self.seed_with_extraction()
        foreign = transform(self.disk(rel))
        self.assertNotEqual(foreign, self.disk(rel), "注入的变换什么都没改")
        with self.rival_slips_in_before_the_main_writeback_reads(
                rel, lock=False, text=foreign) as seen:
            self.assertTrue(self.run_main_writeback(rel, topic="stable"))
        self.assertEqual(0, seen["child"].returncode, seen["child"].stderr)
        self.assertTrue(self.needs_index(rel), "这一篇没留着「待索引」")
        return rel

    def assert_the_model_result_survives(self, metadata: str) -> str:
        """一篇带着这几行元数据、**还没有提取标签**的文章跑一次正常提取交付:
        收据必须签得下来,而且两轮普通索引之后模型那两样还在。

        **这是第一次回写补上标签的那一刻**:`parse_pretagged` 从「返回 None」
        变成「有了一整套」,而文件里那几行 frontmatter 一个字都没动。把那个变化
        当成「文件改了元数据」就会拒签 —— 一拒就重建,`save_extraction` 按文件
        把 `modelsummary` 盖成 `filesummary`(或空)、把 kind 盖成文件声明的那个,
        而 `extracted_at` 非空、这一篇再也不会被重提。
        """
        rel = self.seed_without_extraction_tags(metadata)
        changed, said = self.deliver_extraction(
            rel, {"summary": "modelsummary", "topics": ["generated"],
                  "entities": [{"name": "ReviewTool", "kind": "model"}]})
        self.assertTrue(changed, f"回写没改文件:{said}")
        self.assertEqual("modelsummary", self.summary(rel), "摆盘:交付没落库")
        self.assertEqual("model", self.entity_kind(rel, "reviewtool"),
                         "摆盘:模型给的 kind 没落库")
        self.assertNotIn("没登记已索引", said, f"正常交付被拒了:{said}")
        self.assertFalse(self.needs_index(rel), "正常交付没登记上")
        first, second = self.reindex(), self.reindex()
        self.assertEqual("modelsummary", self.summary(rel),
                         f"下一轮同步吞掉了模型摘要"
                         f"({first.strip()} / {second.strip()})")
        self.assertEqual("model", self.entity_kind(rel, "reviewtool"),
                         "模型给的实体类型被按文件重新派生的值覆盖了")
        return rel

    def test_a_first_writeback_keeps_the_model_summary(self):
        """文件早就写着 `summary:`、但一直没有提取标签的那一篇。"""
        self.assert_the_model_result_survives('summary: "filesummary"\n')

    def test_a_first_writeback_keeps_the_model_entity_kind(self):
        """文件早就写着 `project:`/`kind:`、但一直没有提取标签的那一篇。"""
        self.assert_the_model_result_survives(
            'project: "ReviewTool"\nkind: "tool"\n')

    def test_a_first_writeback_keeps_them_without_any_metadata(self):
        """对照:frontmatter 里压根没有那几行时也一样(摆盘自身的那一半)。"""
        self.assert_the_model_result_survives("")

    def test_a_file_entity_that_has_not_landed_in_entities_is_not_certified(self):
        """`doc_tags` 里有 `entity/*` **不是**「这个实体已入库」的证明。

        `save_extraction` 对 `doc_tags` 是 `INSERT OR IGNORE`(只增不删)、对
        `entities` 是先 `DELETE` 再重建,所以「历史实体标签还在、实体行早没了」
        是常规状态。那时外来编辑往文件里加同一个 `entity/*`,标签那三段全看不见
        —— 审查实测:收据照签,两轮普通索引都报「未变 1」,
        `aggregate.enumerate_entities` 一直是空,强制重建才出现。
        """
        rel = self.seed("gzh")
        (self.vault / rel).write_text(
            self.disk(rel).replace("tags:\n", "tags:\n  - topic/stable\n"),
            encoding="utf-8")
        self.reindex()
        # 第一次提取把 ReviewTool 落进 entities 和 doc_tags
        self.deliver_extraction(
            rel, {"summary": "firstsummary", "topics": ["stable"],
                  "entities": [{"name": "ReviewTool", "kind": "product"}]},
            writeback=False)
        self.assertIn("entity/reviewtool", self.tags(rel), "摆盘:标签该落库")
        foreign = self.disk(rel).replace(
            "tags:\n", "tags:\n  - entity/reviewtool\n")
        # 第二次提取不再返回这个实体 → `entities` 被清掉、那行标签还留着;
        # 同时外来进程把 `entity/reviewtool` 加进**文件**(注入在读文件之前,
        # 所以这个改动被带进我们自己写出去的全文)。
        with self.rival_slips_in_before_the_main_writeback_reads(
                rel, lock=False, text=foreign) as seen:
            changed, said = self.deliver_extraction(
                rel, {"summary": "secondsummary", "topics": ["new"],
                      "entities": []})
        self.assertTrue(changed, said)
        self.assertEqual(0, seen["child"].returncode, seen["child"].stderr)
        self.assertIn("entity/reviewtool", self.tags(rel),
                      "摆盘:那行历史标签该还在(所以标签那三段看不见这件事)")
        self.assertEqual([], self.entities(rel), "摆盘:实体行该已经被清掉")
        self.assertIn("实体", said, f"收据签了:{said}")
        self.assertTrue(self.needs_index(rel), "这一篇没留着「待索引」")
        self.assertEqual([], self.enumerated(), "摆盘:重建之前当然枚举不到")
        self.reindex()
        self.assertEqual(["reviewtool"], self.enumerated(),
                         "文件里的实体改了,一轮重建之后还枚举不到")

    def test_an_entity_dropped_from_the_file_but_left_in_entities_is_refused(self):
        """撤销那一侧 —— **标签那一段通常先拦住它**,所以这一条按单元钉。

        文件丢掉一行 `entity/*` 时,`doc_tags` 里那行必然还在(索引写进去的就是
        `before` 那一组标签,而 `save_extraction` 只增不删),于是「磁盘上删掉的
        那行标签还留在 doc_tags 里」先答了。把 `doc_tags` 那行也去掉之后,才只剩
        实体成员这一段回答得出来。**留着这一段**的理由不是「有端到端的红」,而是
        它不该押在标签那一段的排除集上:两段各读一张表,entities 和 doc_tags
        本来就会分离(先 `DELETE` 再重建 vs. `INSERT OR IGNORE`)。
        """
        rel = self.seed_with_extraction()
        before = self.disk(rel)
        moved = before.replace("  - entity/ReviewTool\n", "")
        self.assertNotEqual(before, moved, "摆盘:那行标签没去掉")
        self.conn.execute("DELETE FROM doc_tags WHERE path=? AND tag=?",
                          (rel, "entity/ReviewTool"))
        self.conn.commit()
        (self.vault / rel).write_text(moved, encoding="utf-8")
        self.assertEqual("磁盘上删掉的那个实体还留在 entities 里",
                         doc_rewrite.index_projection_matches(
                             self.conn, rel, before, moved),
                         "文件撤销了这个实体,entities 里那行还留着")

    def test_a_summary_edit_the_indexer_never_reads_is_certified(self):
        """没有 topic/entity 标签的那一篇:`summary:` 改了也不该拒。

        `parse_pretagged` 对它返回 None,索引那一趟压根不读这一行 —— 什么都不会
        变旧。按「改了就要求库里跟上」判的话,这一次拒签换来的是一轮**破坏性**
        重建(`doc_tags` 先删全部再只从文件重建,库里专有的提取标签当场没了),
        换的却是一个索引根本不消费的字段。
        """
        rel = self.seed_without_extraction_tags('summary: "filesummary"\n')
        self.extract_without_writeback(rel)        # 库里那句摘要是模型给的
        self.assertEqual("审查复现", self.summary(rel), "摆盘:模型那句摘要")
        before = self.disk(rel)
        moved = before.replace("filesummary", "newfilesummary")
        (self.vault / rel).write_text(moved, encoding="utf-8")
        self.assertEqual("", doc_rewrite.index_projection_matches(
            self.conn, rel, before, moved),
            "索引压根不读这一行,却按它拒了签")

    def test_a_project_change_pointing_at_no_file_entity_is_certified(self):
        """`project:` 指到一个文件压根没声明的实体 —— 改它也不该拒。

        索引不会给一个没有 `entity/*` 标签的实体写行,所以那句话改了也没有任何
        库里的值会变旧;按它拒签同样是白换一轮破坏性重建。这一条也按单元钉:
        去掉那道闸之后整组全绿(实测),因为改主实体那一档还被「旧的那个 norm
        现在该是 other」先拦住了。
        """
        rel = self.seed_without_extraction_tags(
            'project: "ReviewTool"\nkind: "tool"\n')
        (self.vault / rel).write_text(
            self.disk(rel).replace("tags:\n", "tags:\n  - topic/stable\n"),
            encoding="utf-8")
        self.reindex()
        before = self.disk(rel)
        self.assertEqual([], self.entities(rel), "摆盘:文件没声明任何实体")
        moved = before.replace('project: "ReviewTool"', 'project: "别的工具"')
        (self.vault / rel).write_text(moved, encoding="utf-8")
        self.assertEqual("", doc_rewrite.index_projection_matches(
            self.conn, rel, before, moved),
            "文件没有这个实体,索引不会给它写行,却按它拒了签")

    def test_an_edited_summary_is_not_certified(self):
        rel = self.refuse_then_rebuild(
            lambda t: t.replace("oldsummary", "newsummary"))
        self.assertIn("summary", self.said, f"收据签了:{self.said}")
        self.reindex()
        self.assertEqual("newsummary", self.summary(rel),
                         "两轮索引之后库里还是旧摘要")

    def test_a_withdrawn_summary_is_not_certified(self):
        """撤回也是动:文件原来说了一句、现在不说了,索引该跟着改成空。

        这一档是那句 `claims["summary"] or was_claims.get("summary")` 里**后半
        截**独有的射程 —— 只看「现在说了什么」的话,撤回之后新值是空串,一句
        假的「一致」就把旧摘要永久留在库里了。
        """
        rel = self.refuse_then_rebuild(
            lambda t: t.replace('summary: "oldsummary"\n', ""))
        self.assertIn("summary", self.said, f"收据签了:{self.said}")
        self.reindex()
        self.assertEqual("", self.summary(rel),
                         "文件已经不说那句了,库里还留着")

    def test_an_edited_entity_kind_is_not_certified(self):
        rel = self.refuse_then_rebuild(
            lambda t: t.replace('kind: "tool"', 'kind: "model"'))
        self.assertIn("project/kind", self.said, f"收据签了:{self.said}")
        self.reindex()
        self.assertEqual("model", self.entity_kind(rel, "reviewtool"),
                         "两轮索引之后主实体类型还是旧值")

    def test_a_withdrawn_entity_kind_is_not_certified(self):
        """撤回指派也是动:文件原来点名说「这个实体是 tool」,现在不说了。

        这一档是那个并集(`set(claims["kinds"]) | set(was_kinds)`)里**后半截**
        独有的射程 —— 只看「现在指派了谁」的话,撤回之后一个都不剩,循环压根
        不跑,库里那个旧 kind 就永久留着了(定点注入实测:只看现在的那一版,
        整组绿着)。重新索引会把它改成 `parse_pretagged` 的默认档。
        """
        rel = self.refuse_then_rebuild(lambda t: t.replace('kind: "tool"\n', ""))
        self.assertIn("project/kind", self.said, f"收据签了:{self.said}")
        self.reindex()
        self.assertEqual("other", self.entity_kind(rel, "reviewtool"),
                         "文件已经不指派它了,库里还留着旧类型")

    def test_a_writeback_that_says_nothing_about_them_still_gets_certified(self):
        """**反过来钉死的那一边,这一条比上面三条要紧**:md 里没写
        summary/project/kind 的那种文章(= `kb extract --writeback` 走的那条
        主路),库里那两样是模型给的,拿磁盘这一份重新派生一次去比永远对不上。

        那样判的末态不是「白重建一次」:拒绝 → 下一轮重建 → `parse_pretagged`
        用 `""` 和 `other` 把模型那两样盖掉,而 `extracted_at` 非空、这一篇不会
        被重新提取回来。所以这一档必须签得下来。
        """
        rel = self.seed("gzh")
        self.extract_without_writeback(rel)
        self.assertEqual("审查复现", self.summary(rel), "摆盘:模型那句摘要")
        self.assertEqual("product", self.entity_kind(rel, "reviewtool"),
                         "摆盘:模型给的那个 kind(tool 归一成 product)")
        self.assertNotIn("summary:", self.disk(rel), "摆盘:md 里不该有这几行")
        self.assertTrue(self.run_main_writeback(rel))
        self.assertNotIn("没登记已索引", self.said, f"合法回写被拒了:{self.said}")
        self.assertFalse(self.needs_index(rel), "合法回写没登记上")
        first, second = self.reindex(), self.reindex()
        self.assertEqual("审查复现", self.summary(rel),
                         f"模型那句摘要没了({first.strip()} / {second.strip()})")
        self.assertEqual("product", self.entity_kind(rel, "reviewtool"),
                         "模型给的 kind 被按文件重新派生的默认值盖了")

    def test_a_writeback_that_carries_an_entity_tag_still_gets_certified(self):
        """上一条的**实体那一半**:生产里 `--writeback` 连 `entity/*` 一起写。

        那一行写进 md 之后,重新索引时 `parse_pretagged` 会给这个实体派一个
        `other`(md 里没有 project/kind),而库里是模型给的 `product` —— 两边
        不等是**常态**。把「没指派就是 other」也算成「文件说过的话」,这一条
        就被拒,一拒就重建、模型那个 kind 当场没了。

        只写 topic 的摆盘看不出这件事(定点注入实测:那样改了判据,整组绿着),
        所以这一条专门把 `entity/*` 那行也回写出去。
        """
        rel = self.seed("gzh")
        self.extract_without_writeback(rel)      # 落库那一侧:kind=product
        self.assertEqual("product", self.entity_kind(rel, "reviewtool"),
                         "摆盘:模型给的那个 kind")
        self.assertTrue(self.run_main_writeback(rel, entity="ReviewTool"))
        self.assertIn("entity/reviewtool", self.disk(rel), "摆盘:md 那行没写上")
        self.assertNotIn("没登记已索引", self.said, f"合法回写被拒了:{self.said}")
        self.assertFalse(self.needs_index(rel), "合法回写没登记上")
        first, second = self.reindex(), self.reindex()
        self.assertEqual("product", self.entity_kind(rel, "reviewtool"),
                         f"模型给的 kind 被默认档盖了"
                         f"({first.strip()} / {second.strip()})")

    def test_a_bypass_that_moves_the_claim_and_lands_it_still_gets_certified(self):
        """**「动了」不等于「该拒」** —— 动了而且已经进库的那一档必须签得下来。

        摘要回填(`scripts/backfill_youtube_summaries.py`)就是这一档:它在同一个
        协调区间里把 `summary:` 写进 frontmatter **并且**更新 `documents.summary`,
        所以登记那一刻文件这句话动过、库里也已经跟上了。判据写成「动了就拒」的话
        这条旁路从此一次都登记不上,而症状只是「每轮索引都在重建这批文章」。

        摆盘得让这一篇的 md 带一个 `topic/*`:没有它 `parse_pretagged` 返回 None,
        索引压根不读那句 summary,这一档也就不成立了。
        """
        rel = self.seed("youtube")
        text = self.disk(rel).replace(
            f"  - source/{self.SOURCES['youtube']}\n",
            f"  - source/{self.SOURCES['youtube']}\n  - topic/回填\n")
        (self.vault / rel).write_text(text, encoding="utf-8")
        self.reindex()
        self.assertEqual("", self.summary(rel), "摆盘:库里这一列该还空着")
        said = self.run_bypass("youtube")
        self.assertIn("summary:", self.disk(rel), f"没写进 frontmatter:{said}")
        self.assertNotIn("没登记已索引", said, f"合法回填被拒了:{said}")
        self.assertFalse(self.needs_index(rel), "合法回填没登记上")
        self.assertEqual(
            "oldindexedtoken 旧版导语与旧版正文。", self.summary(rel),
            "库里那一列没跟着写(判据正是按它判「已经进库了」)")

    def test_a_kind_change_that_already_landed_is_certified(self):
        """kind 那一段「动了、可已经进库了」这半边 —— **直接对判据断言**。

        仓库里现在**没有**哪条路会合法地移动这句话(摘要回填动的是 summary,
        提取回写压根不写 project/kind),所以这半边拿不到端到端的红:把它换成
        「动了就拒」,整组 51 条全绿(实测)。**不删它**的理由不是「有人在用」,
        而是误报在这里不是安全侧 —— 一次拒绝就是一次破坏性重建(`DELETE` 掉
        库里专有的那批提取标签),和 summary 那一段同一个取舍。所以这里按单元
        的形状把它钉住,并如实记下「这一半目前没有消费方」。
        """
        rel = self.seed_with_extraction()
        before = self.disk(rel)
        moved = before.replace('kind: "tool"', 'kind: "model"')
        (self.vault / rel).write_text(moved, encoding="utf-8")
        self.assertEqual("磁盘上那句 project/kind 定的实体类型还没进 entities",
                         doc_rewrite.index_projection_matches(
                             self.conn, rel, before, moved),
                         "摆盘:库里还没跟上的时候该拒")
        self.conn.execute("UPDATE entities SET kind='model' WHERE path=? AND"
                          " norm='reviewtool'", (rel,))
        self.conn.commit()
        self.assertEqual("", doc_rewrite.index_projection_matches(
            self.conn, rel, before, moved),
            "库里已经跟上了还拒 —— 那就是一次破坏性重建")

    def test_the_claims_are_what_the_indexer_actually_writes(self):
        """**两份实现的对账**:`_extraction_claims` 读出来的那两句话,要和真实
        索引(`parse_pretagged` → `save_extraction`)写进库的末态逐字相同。

        判据只问「这句话动没动」,所以「这句话是什么」必须和索引的读法一致 ——
        各漂一点的话,拒绝之后那次重建写进去的是另一个值,而判据下一轮照旧说
        不一致:这一篇从此每轮索引都在重建,而症状只有「索引老在动」。

        **「文件没指派的那些实体一律 other」这一半一起钉住**:判据在「文件撤回
        了指派」那一档正是按 `_kind_for()` 算该有的末态,而那个 `other` 是
        `parse_pretagged` 里写死的字面量 —— 它哪天变了,这里红。
        **成员那一侧也一起对账**:索引重建之后 `entities` 的成员就该是文件声明
        的那一组,判据的「在不在」正是按它判的。
        """
        for label, transform in (
                ("原样", lambda t: t),
                ("改摘要", lambda t: t.replace("oldsummary", "newsummary")),
                ("撤回摘要", lambda t: t.replace('summary: "oldsummary"\n', "")),
                ("改类型", lambda t: t.replace('kind: "tool"', 'kind: "model"')),
                ("改主实体", lambda t: t.replace('project: "ReviewTool"',
                                               'project: "别的工具"')),
                ("撤回类型", lambda t: t.replace('kind: "tool"\n', "")),
        ):
            with self.subTest(label):
                self.setUp()
                rel = self.seed_with_extraction()
                (self.vault / rel).write_text(transform(self.disk(rel)),
                                              encoding="utf-8")
                claims = doc_rewrite._extraction_claims(self.doc_of(rel))
                self.assertTrue(claims["active"], "摆盘:这一份该触发那一步")
                self.reindex()          # 文件变了的那几档会真重建一次
                self.assertEqual(claims["summary"], self.summary(rel),
                                 "判据读到的 summary 和索引写进去的不是一个")
                landed = {norm: kind for _, kind, norm in self.entities(rel)}
                self.assertEqual(
                    {norm: doc_rewrite._kind_for(claims, norm)
                     for norm in claims["entities"]}, landed,
                    "判据读到的成员和 kind(含「没指派就是 other」)和索引写进去"
                    "的不是一套")


if __name__ == "__main__":
    unittest.main()
