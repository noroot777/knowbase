# -*- coding: utf-8 -*-
"""补抓来的全文落盘之后,知识库里那份按旧壳算出来的提取结果要作废。

被测的那件事:一条 HN Buzzing 条目的正文从「标题 + 域名 + ↑ 108 HN Points」
变成整篇文章之后,摘要、主题和实体必须跟着重做。

**不作废的后果不是"晚一点更新"。** `documents.extracted_at` 非空意味着这一篇
**永远**不进 `extract.pending_docs`,而 `sync._kept_meta` 又把旧 `tags` 原样写
回 frontmatter、索引照它重建 `doc_tags` —— 于是摘要、主题、实体永久停在那条
链接上,正文里的项目一个都不会出现在检索、图鉴和日报里(2026-10-09 审查实测:
新中文正文确实进了 chunk,而 summary 还是「OLD SHELL: only a link and HN
points.」、tags 还是 `topic/link-only`,`pending_docs` 是空的)。

**时点是「新正文真的落了盘」,不是「网络抓到了正文」**(见
`fulltext.retire_stale`)。按形状单独一个文件:它要摆的盘比别的用例重一截
(真把 md 索引进 kb.db、真跑一遍 save/writeback),挤进 `test_feed_fulltext.py`
会越过 700 行上限。脚手架复用那边那一份,两边的前提才不会各漂各的。
"""
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from kb import config, extract, llm, scanner, store
from kb.chunker import split_chunks
from kb.collectors.feeds import FeedCollector
from kb.config import Source
from kb.feeds import fulltext, registry as reg, sync
from kb.scanner import parse_frontmatter
from tests.test_feed_fulltext import PAGE, FullTextCase, page

ZH = '###TITLE###\n是的，而且\n###BODY###\n这个运行时为编码智能体调度工具，并保留完整记录。'
OLD = {'entities': [{'name': 'htmx', 'kind': 'project'}], 'topics': ['link-only'],
       'summary': '只有一条链接和 HN 得分。'}


class StaleCase(FullTextCase):
    """脚手架:临时 kb.db + 一条已经按壳导出、索引、提取过的旧条目。"""

    def setUp(self):
        super().setUp()
        vault = self.root / 'vault'
        for p in (patch.object(scanner, 'VAULT_ROOT', vault),
                  patch.object(extract, 'VAULT_ROOT', vault)):
            p.start(); self.addCleanup(p.stop)
        self.src = Source('rss', vault / 'source' / 'rss')

    def kb(self):
        # config.DB_PATH 由 FullTextCase 指到 tmp;`store.connect` 的默认参数在
        # 函数定义时就绑死了,所以这里和被测代码一样显式现读它。
        return closing(store.connect(config.DB_PATH))

    def index(self, rel: str):
        """按索引阶段那条真路径把这篇 md 扫进 kb.db。"""
        doc = scanner._doc_from_file(self.root / 'vault' / rel, self.src)
        with self.kb() as c:
            store.upsert_document(c, doc, split_chunks(doc.body), reg.now())
            c.commit()
        return doc

    def state(self, rel: str) -> dict:
        with self.kb() as c:
            row = c.execute('SELECT extracted_at,summary FROM documents WHERE path=?',
                            (rel,)).fetchone()
            return dict(
                extracted_at=row and row['extracted_at'], summary=row and row['summary'],
                tags=sorted(r[0] for r in c.execute('SELECT tag FROM doc_tags WHERE path=?', (rel,))),
                entities=sorted(r[0] for r in c.execute('SELECT entity FROM entities WHERE path=?', (rel,))),
                pending=[r['path'] for r in extract.pending_docs(c, 100)])

    def md_tags(self, rel: str) -> list[str]:
        meta, _ = parse_frontmatter((self.root / 'vault' / rel).read_text())
        return sorted(meta.get('tags') or [])

    def shell_in_the_vault(self, extra_tags: list[str] | None = None) -> tuple[str, str]:
        """壳先按原样走完导出 + 索引 + 提取 —— 打开开关时库里就是这个状态。"""
        reg.save_subscription('rss', {'fulltext': False}, self.source['id'])
        key = self.put()
        sync.export('rss')
        reg.save_subscription('rss', {'fulltext': True}, self.source['id'])
        rel = f'source/rss/{key}.md'
        self.index(rel)
        with self.kb() as c:
            stale = extract.save_extraction(c, rel, OLD)
            c.commit()
            extract.writeback_extraction(c, rel, OLD, stale)
        if extra_tags:
            extract.writeback_tags(rel, extra_tags)
        return key, rel

    def fetch_and_translate(self, reply: str = ZH):
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))), \
                patch.object(llm, 'chat', Mock(return_value=reply)):
            FeedCollector('rss').materialize()


class StaleExtractionTests(StaleCase):

    def test_the_new_body_sends_the_article_back_for_extraction(self):
        key, rel = self.shell_in_the_vault()
        before = self.state(rel)
        self.fetch_and_translate()
        self.index(rel)                        # 索引阶段按新 md 重建
        after = self.state(rel)
        # 后果先断言:这一篇真的重新排进提取队列了 —— 只验"标记放下了没有"的话,
        # 一个什么都没作废的实现也能全绿。
        self.assertEqual([rel], after['pending'])
        self.assertIsNone(after['extracted_at'])
        self.assertEqual('', after['summary'])
        self.assertEqual(([], []), (after['tags'], after['entities']))
        self.assertEqual([], self.md_tags(rel))
        # 反过来钉一边:旧结论原来确实在,不然"清掉了"和"压根没写进去"同形。
        self.assertEqual(['entity/htmx', 'topic/link-only'], before['tags'])
        self.assertEqual([], before['pending'])
        # 作废的是结论,不是内容:新正文照旧在 vault 里。
        self.assertIn('这个运行时为编码智能体调度工具', (self.root / 'vault' / rel).read_text())
        self.assertEqual(0, self.row(key)['extract_stale'])

    def test_only_the_tags_computed_from_the_old_body_are_dropped(self):
        """人工 tag 和软广标记留着 —— 要作废的是"按旧链接算出来的结论"。"""
        _, rel = self.shell_in_the_vault(extra_tags=['flag/软广', '人工/待读'])
        self.assertEqual({'flag/软广', 'entity/htmx', 'topic/link-only', '人工/待读'},
                         set(self.md_tags(rel)))
        self.fetch_and_translate()
        self.assertEqual(['flag/软广', '人工/待读'], self.md_tags(rel))

    def test_nothing_is_retired_while_the_full_text_waits_for_translation(self):
        """vault 里还是那份壳时,旧提取一个字都不许动。

        补抓成功之后这一条要排队等翻译(一轮只译 `translate.MAX_ITEMS` 条,这里
        用一次模型故障造出同一个形状)。提前清掉 `extracted_at` 的话,提取阶段
        会拿**这份壳**再买一次提取、写回一个同样过时的结论,而 `extracted_at`
        跟着又非空了 —— 等正文真译完导出之后,摘要和主题照旧是壳的。
        """
        key, rel = self.shell_in_the_vault()
        self.fetch_and_translate(reply='')     # 回包空 = 翻译失败,这一条被扣在闸门外
        self.assertIn('HN Points', (self.root / 'vault' / rel).read_text())
        after = self.state(rel)
        self.assertIsNotNone(after['extracted_at'])
        self.assertEqual([], after['pending'])
        self.assertEqual(['entity/htmx', 'topic/link-only'], after['tags'])
        self.assertEqual(1, self.row(key)['extract_stale'])      # 待办留着

    def test_a_failed_retirement_keeps_the_todo_for_the_next_round(self):
        """这一步失败时标记留在原处,下一轮导出自己会再来一次。

        顺序反过来写(先放下标记再清)的话,这几篇会**永久**停在「摘要还是那个
        跳转壳」的状态:它们的 `exported_hash` 已经等于 `content_hash`、压根不
        在待导出集合里,没有任何路径回来重试。
        """
        key, rel = self.shell_in_the_vault()
        with patch.object(fulltext, '_drop_extraction', Mock(side_effect=OSError('kb.db 打不开'))):
            self.fetch_and_translate()
        # 导出没被带崩:新正文照旧落了盘,失败的只是"让旧摘要作废"这一步。
        self.assertIn('这个运行时为编码智能体调度工具', (self.root / 'vault' / rel).read_text())
        self.assertEqual(1, self.row(key)['extract_stale'])
        self.assertIsNotNone(self.state(rel)['extracted_at'])
        sync.export('rss')                     # 下一轮:content_hash 不用再变一次
        self.assertEqual(0, self.row(key)['extract_stale'])
        self.index(rel)
        self.assertEqual([rel], self.state(rel)['pending'])

    def test_a_plain_retranslation_still_keeps_the_old_tags(self):
        """反过来钉一边:没补抓过的条目重导出时,提取回写的 tag 照旧原样留着。

        那是 `sync._kept_meta` 原本的契约(见 `docs/decisions/translate.md` 的硬约束一节):同一篇
        换个语言,标签口径没变,再买一次提取只是重复付钱。摘 tag 只在正文被
        **整篇换掉**时发生 —— 一律摘的实现会在这里红。
        """
        key = self.put(self.plain, url='https://solo.example.org/post', title='Deep dive',
                       body='A long enough body for the exporter to take seriously.')
        sync.export('rss')
        rel = f'source/rss/{key}.md'
        self.index(rel)
        with self.kb() as c:
            stale = extract.save_extraction(c, rel, OLD)
            c.commit()
            extract.writeback_extraction(c, rel, OLD, stale)
        with reg.db() as c:                    # 译完了 → 重导出(translate._save 那条路)
            c.execute("UPDATE feed_items SET title_zh='深潜',body_zh='正文译文',"
                      "translated_at='2026-10-09 10:00',exported_hash='' WHERE key=?", (key,))
        sync.export('rss')
        self.assertIn('正文译文', (self.root / 'vault' / rel).read_text())
        self.assertEqual(['entity/htmx', 'topic/link-only'], self.md_tags(rel))
        self.assertIsNotNone(self.state(rel)['extracted_at'])

    def test_a_round_without_any_fetched_item_never_opens_the_index(self):
        """常态零开销:没有待作废的条目时一次 kb.db 连接都不开。"""
        self.put(self.plain, url='https://solo.example.org/post', title='Deep dive',
                 body='A long enough body for the exporter to take seriously.')
        with patch.object(fulltext, '_drop_extraction', Mock(side_effect=AssertionError)) as drop:
            sync.export('rss')
        self.assertEqual(0, drop.call_count)


if __name__ == '__main__':
    unittest.main()
