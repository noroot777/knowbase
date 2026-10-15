# -*- coding: utf-8 -*-
"""当日抓取账本:今天第几次抓取、那一次往库里新加了多少篇。

这一组钉的是**「抓了多少」和「这一次生成能选进去几条」是两件事**。它们曾经是
一个数:按钮上报的是日报取材的结果,于是

- 第二次同步真抓回来 9 条,名额只剩 3 个 ⇒ 按钮写「3 条」;
- 抓回来那条发布得比窗口起点早、抢不到补录席位 ⇒ 按钮写「没有新内容」;
- 一条都没再抓,只是重新生成了一次日报 ⇒ 按钮从「9 条」变成「0 条」。

三种都是在用取材结果回答抓取的问题,而用户正是靠这个数决定要不要为这一批内容
再花一次模型钱。所以断言一律落在**后果**上:面板上那个数是什么、它在生成日报
前后变没变、重启之后还在不在 —— 只验"账本写了没有"的话,一个把取材数抄进去的
实现也能全绿。

两个方向都钉住:该算一次的算一次(多来源、多阶段共用批次身份),不该算的不算
(只勾索引的那种运行、衍生树的条目)。

日报取材口径本身在 tests/test_daily_carried.py 和 tests/test_daily_report.py。
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from kb import daily, daily_carry, embedder, fetch_rounds, llm, llm_stages
from kb import pipeline_batch, pipeline_stages, scanner, store
from kb import pipeline_state as ps


def T(text):
    return datetime.fromisoformat(text)


class _Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for p in (patch.object(fetch_rounds, 'DATA_DIR', self.root),):
            p.start(); self.addCleanup(p.stop)

    def book(self):
        raw = (self.root / fetch_rounds.FILE_NAME)
        return json.loads(raw.read_text(encoding='utf-8')) if raw.exists() else {}


class RoundBookTests(_Base):
    def test_a_second_sync_the_same_day_is_a_second_round(self):
        """当天两轮:第二轮是「第 2 次」,报的是它自己抓了多少,不是两轮的和。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_added('batch-a', 9, day)
        self.assertEqual({'n': 1, 'added': 9, 'at': '09:00'},
                         fetch_rounds.today(day))
        later = T('2026-09-29 15:30')
        fetch_rounds.open_round('batch-b', later)
        fetch_rounds.note_added('batch-b', 2, later)
        self.assertEqual({'n': 2, 'added': 2, 'at': '15:30'},
                         fetch_rounds.today(later))

    def test_one_business_fetch_across_several_sources_is_one_round(self):
        """一轮日报 = N+1 次管道运行,但只算**一次**抓取。

        它们共用一个批次身份,所以反复 open_round 是空操作、note_added 是累加 ——
        按运行记的话面板会写「今天第 7 次抓取」而用户只点过一次。
        """
        day = T('2026-09-29 09:00')
        for _ in range(3):                       # 三个来源各跑一遍
            fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_added('batch-a', 4, day)
        fetch_rounds.note_added('batch-a', 5, day)      # 编排里索引跑了两趟
        self.assertEqual({'n': 1, 'added': 9, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_a_run_that_did_not_fetch_is_not_a_round(self):
        """只勾了索引/提取的那种运行不是抓取 —— 它往库里写进去的东西(上一轮
        抓回来还没索引的)不该凭空变出一个「第 N 次抓取」。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.note_added('run-x', 7, day)        # 没 open 过
        self.assertEqual({'n': 0, 'added': 0, 'at': ''}, fetch_rounds.today(day))
        self.assertEqual([], self.book().get('rounds', []))

    def test_an_empty_fetch_still_counts_as_a_fetch(self):
        """什么都没抓到的那一次也是一次抓取 —— 反过来钉一边:有轮次、0 条,
        和「今天还没有抓取」是两句不同的话。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        self.assertEqual({'n': 1, 'added': 0, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_yesterdays_rounds_do_not_count_as_today(self):
        """跨自然日自己归零:昨天抓过三轮不该让今天从「第 4 次」开始。"""
        fetch_rounds.open_round('batch-a', T('2026-09-28 22:00'))
        fetch_rounds.note_added('batch-a', 30, T('2026-09-28 22:00'))
        self.assertEqual({'n': 0, 'added': 0, 'at': ''},
                         fetch_rounds.today(T('2026-09-29 07:00')))
        today = T('2026-09-29 08:00')
        fetch_rounds.open_round('batch-b', today)
        self.assertEqual(1, fetch_rounds.today(today)['n'])
        # 归零是**写**的时候落下的,昨天那一轮不留在盘上占位
        self.assertEqual(['batch-b'], [r['key'] for r in self.book()['rounds']])

    def test_the_count_survives_a_restart(self):
        """轮次记在盘上,不是进程内计数器 —— 重启之后接着往下数。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_added('batch-a', 5, day)
        # 新进程:模块没有任何内存状态,只有这份文件
        later = T('2026-09-29 11:00')
        fetch_rounds.open_round('batch-new', later)
        fetch_rounds.note_added('batch-new', 1, later)
        self.assertEqual({'n': 2, 'added': 1, 'at': '11:00'},
                         fetch_rounds.today(later))

    def test_a_restarted_process_does_not_reuse_a_batch_identity(self):
        """批次身份跨进程唯一。进程内自增的话,重启后发出的 `batch-1` 会撞上
        重启前那一轮,于是今天第二次抓取被并进第一次、面板永远写「第 1 次」。"""
        with patch.object(ps, '_RUN_EPOCH', 'aaaa'):
            first = pipeline_batch.open_batch()
            pipeline_batch.close_batch()
        with patch.object(ps, '_RUN_EPOCH', 'bbbb'), \
             patch.object(pipeline_batch, '_batch_seq', 0):
            second = pipeline_batch.open_batch()
            pipeline_batch.close_batch()
        self.assertNotEqual(first, second)
        day = T('2026-09-29 09:00')
        for key, n in ((first, 5), (second, 2)):
            fetch_rounds.open_round(key, day)
            fetch_rounds.note_added(key, n, day)
        self.assertEqual(2, fetch_rounds.today(day)['n'])

    def test_an_unreadable_book_reads_as_no_record_instead_of_raising(self):
        """读不出来按"今天还没有记录"算,不往抓取那条路上抛 —— 它只是个展示。"""
        (self.root / fetch_rounds.FILE_NAME).write_text('{半截 JSON',
                                                        encoding='utf-8')
        self.assertEqual({'n': 0, 'added': 0, 'at': ''},
                         fetch_rounds.today(T('2026-09-29 09:00')))


class ScopeTests(_Base):
    def test_only_the_sources_the_report_reads_count_as_new(self):
        """条目化衍生树和原文是同一批内容,日报取材本来就把它们排除在外;
        算成「抓到的新内容」的话,一轮条目化就能让按钮写出三位数而日报那边
        一条新的都没有。口径和 `daily._scope` 共用 PRIMARY_SOURCE_NAMES。"""
        self.assertTrue(fetch_rounds.counts_as_new('rss'))
        self.assertTrue(fetch_rounds.counts_as_new('dr-gzh'))
        self.assertFalse(fetch_rounds.counts_as_new('dr-gzh-projects'))
        self.assertFalse(fetch_rounds.counts_as_new('dr-video-projects'))


class IndexCountsTests(_Base):
    """索引阶段数进账本的必须是**真·新增**,不是"要重建索引的"。"""

    def setUp(self):
        super().setUp()
        self.db = self.root / 'kb.db'
        connect = store.connect
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        p = patch.object(store, 'connect', lambda *a, **k: connect(self.db))
        p.start(); self.addCleanup(p.stop)

    def test_a_rewritten_old_article_is_not_a_new_catch(self):
        """老文章改了内容(正文补抓、回写译文、重新导出)会要求重建索引,但它
        不是这一轮抓到的 —— 算进去的话同一篇会连着好几天被报成"刚抓到"。"""
        self.conn.execute(
            'INSERT INTO documents(path,source,title,account,published,url,'
            'content_hash,indexed_at) VALUES(?,?,?,?,?,?,?,?)',
            ('source/rss/a.md', 'rss', '甲', '某号', '2026-09-28 09:00', 'u',
             'h1', '2026-09-28 10:00'))
        self.conn.commit()
        self.assertTrue(store.doc_exists(self.conn, 'source/rss/a.md'))
        # 内容变了 ⇒ 要重建,但不是新的
        self.assertTrue(store.doc_needs_reindex(self.conn, 'source/rss/a.md', 'h2'))
        self.assertFalse(store.doc_exists(self.conn, 'source/rss/zzz.md'))


class StageWiringTests(_Base):
    """两个钩子真的接在管道阶段上 —— 账本本体全绿而阶段没调它的话,面板上那个
    数永远是「今天还没有抓取」,而单元测试一条都不红。"""

    def setUp(self):
        super().setUp()
        self.db = self.root / 'kb.db'
        connect = store.connect
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        for p in (patch.object(store, 'connect', lambda *a, **k: connect(self.db)),
                  # 抓取本体和向量编码都不是这一条要验的,留着会去连网/加载模型
                  patch.object(pipeline_stages, '_per_channel', Mock()),
                  patch.object(embedder, 'embed_pending_chunks', Mock(return_value=0))):
            p.start(); self.addCleanup(p.stop)
        ps._state.update({'batch': 'batch-probe', 'run_id': 'run-probe',
                          'stages': {s: {'status': '', 'detail': ''}
                                     for s in ps.STAGES}, 'log': []})

    def _scan(self, *docs):
        return patch.object(scanner, 'scan_all', Mock(return_value=list(docs)))

    def _doc(self, path, source='rss', body='正文', h='h1'):
        return scanner.Doc(path=path, source=source, title=path, account='某号',
                           published='2026-09-28 09:00', url=f'https://e/{path}',
                           body=body, content_hash=h)

    def test_a_real_crawl_then_index_lands_in_the_book(self):
        """跑一遍真的抓取阶段 + 真的索引阶段,账本上就该出现「第 1 次,2 条」。"""
        pipeline_stages._run_crawl()
        with self._scan(self._doc('source/rss/a.md'), self._doc('source/rss/b.md')):
            pipeline_stages._index_pass()
        got = fetch_rounds.today()
        self.assertEqual(1, got['n'])
        self.assertEqual(2, got['added'])

    def test_reindexing_the_same_articles_adds_nothing(self):
        """第二次抓取只是把同样两篇重新导出(内容变了要重建索引)—— 它们不是
        新抓到的,第二轮该是「第 2 次,0 条」而不是又报 2 条。"""
        pipeline_stages._run_crawl()
        with self._scan(self._doc('source/rss/a.md'), self._doc('source/rss/b.md')):
            pipeline_stages._index_pass()
        ps._state['batch'] = 'batch-probe-2'
        pipeline_stages._run_crawl()
        with self._scan(self._doc('source/rss/a.md', body='改过', h='h2'),
                        self._doc('source/rss/b.md', body='也改过', h='h2')):
            pipeline_stages._index_pass()
        self.assertEqual({'n': 2, 'added': 0},
                         {k: fetch_rounds.today()[k] for k in ('n', 'added')})

    def test_indexing_without_a_crawl_is_not_a_fetch(self):
        """只勾了索引的那种运行:库里确实多了两篇(上一轮抓回来还没索引的),
        但今天**没有**抓取过,账本上一轮都不该有。"""
        with self._scan(self._doc('source/rss/a.md'), self._doc('source/rss/b.md')):
            pipeline_stages._index_pass()
        self.assertEqual({'n': 0, 'added': 0, 'at': ''}, fetch_rounds.today())


class FetchFactIsNotReportFactTests(_Base):
    """抓取事实不随日报变 —— 这是整件事的要害。"""

    def setUp(self):
        super().setUp()
        self.db = self.root / 'kb.db'
        connect = store.connect
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        for p in (patch.object(daily, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(store, 'connect', lambda *a, **k: connect(self.db)),
                  patch.object(llm_stages, 'stage_model',
                               Mock(return_value=llm.Model('openai', 'm'))),
                  patch.object(daily_carry, 'DATA_DIR', self.root),
                  patch.object(llm, 'chat',
                               Mock(return_value='## 今日要点\n\n- **有事**:说明 [1]'))):
            p.start(); self.addCleanup(p.stop)

    def _doc(self, path, published):
        self.conn.execute(
            'INSERT INTO documents(path,source,title,account,published,url,summary,'
            'indexed_at,extracted_at,promo_level) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (path, 'rss', path, '某号', published, '', '摘要',
             '2026-09-28 09:30', '2026-09-28 09:30', ''))
        self.conn.commit()

    def test_generating_a_report_does_not_change_what_was_fetched(self):
        """生成日报不会把「今天抓了 2 条」抹成 0 —— 旧实现正是这么丢的:
        用户同步完看到数字,生成一次之后那个数就没了,第二次同步抓回来的东西
        再也没有地方显示。"""
        day = T('2026-09-29 08:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_added('batch-a', 2, day)
        self._doc('source/rss/a.md', '2026-09-28 09:00')
        self._doc('source/rss/b.md', '2026-09-28 10:00')
        before = fetch_rounds.today(day)
        info = daily.pending(first_days=1, now=day)
        daily.generate(since=info['since'], until=info['until'])
        self.assertEqual(before, fetch_rounds.today(day))
        self.assertEqual(2, fetch_rounds.today(day)['added'])

    def test_a_failed_ledger_write_cannot_invent_new_content(self):
        """日报文件写好了、记账没写上 —— 这是**受支持**的降级终态(那一档故意
        倒向"多报一遍"而不是永久写死)。按钮上那个数来自抓取账本,所以它一个字
        都不受影响;拿 `daily_carried` 去反推"今天这份里有什么"的实现会在这里
        凭空报出新增(审查实测 `next_button_gain=2` 而真实新增 0)。"""
        day = T('2026-09-29 08:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_added('batch-a', 2, day)
        self._doc('source/rss/a.md', '2026-09-28 09:00')
        self._doc('source/rss/b.md', '2026-09-28 10:00')
        info = daily.pending(first_days=1, now=day)
        # `daily` 是 `from ... import mark_carried` 进来的,所以要打它那一份名字
        with patch.object(daily, 'mark_carried',
                          Mock(side_effect=sqlite3.OperationalError('disk I/O error'))):
            daily.generate(since=info['since'], until=info['until'])
        self.assertEqual(set(), {r[0] for r in self.conn.execute(
            "SELECT path FROM daily_carried WHERE day<>'#seed'")})  # 账本确实空着
        self.assertEqual({'n': 1, 'added': 2, 'at': '08:00'},
                         fetch_rounds.today(day))


if __name__ == '__main__':
    unittest.main()
