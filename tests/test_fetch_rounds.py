# -*- coding: utf-8 -*-
"""当日抓取账本:今天第几次抓取、那一次往 vault 新落了多少篇。

这一组钉三件事,三件都曾经错过,而且错法一次比一次隐蔽。

**① 「抓了多少」不是「这一次生成能选进去几条」。** 按钮上一度报的是日报取材
结果,于是:第二次同步真抓回来 9 条、名额只剩 3 个就写「3 条」;抓回来那条发布
得早、抢不到补录席位就写「没有新内容」;一条都没再抓、只是重新生成了一次日报,
那个数就从 9 变成 0。

**② 「抓了多少」也不是「库里头一回出现这个路径的文档数」。** 改成由索引阶段数
之后仍然是拿消费者的结果猜抓取:空抓取碰上 3 篇历史未索引的旧文会报成「+3 条」,
反过来真抓到 2 篇而这一轮不索引则报 0、之后单独索引也补不回来。成果的归属只能
由抓取自己定 —— 数的是**物化**那一步往 vault 新落下的 md。

**③ 「读不出来」不是「没抓到」。** 账本写失败被吞掉的话,25 篇文章已经入库而
按钮永久写 0 条;坏掉的账本被当成空的话,面板会笃定地说「今天还没有抓取」,
下一次开轮次还把它覆盖成「第 1 次」。

所以断言一律落在**后果**上:面板上那个数是什么、它在生成日报前后变没变、
重启之后还在不在、故障之后它说的是「0」还是「不可确认」—— 只验"账本写了没有"
的话,一个把取材数抄进去的实现也能全绿。

两个方向都钉住:该算一次的算一次(多来源、多阶段共用批次身份),不该算的不算
(只勾索引/只勾导出的那种运行、衍生树、停用的树、历史旧文件)。

日报取材口径本身在 tests/test_daily_carried.py 和 tests/test_daily_report.py;
前端那三种说法在 tests/daily_probe.mjs。
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from kb import config, daily, daily_carry, embedder, fetch_rounds, llm, llm_stages
from kb import pipeline_batch, pipeline_stages, scanner, store
from kb import pipeline_state as ps


def T(text):
    return datetime.fromisoformat(text)


class _Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault = self.root / 'vault'
        # `_unsaved` 是模块级的:不收干净的话,一条写失败用例会把后面每一条都
        # 拖成「不可确认」(同 tests/test_*.py 那条共用数据目录的教训)。
        fetch_rounds._unsaved = []
        self.addCleanup(setattr, fetch_rounds, '_unsaved', [])
        for p in (patch.object(fetch_rounds, 'DATA_DIR', self.root),
                  patch.object(fetch_rounds, 'VAULT_ROOT', self.vault),
                  patch.object(config, 'SOURCES', self._sources())):
            p.start(); self.addCleanup(p.stop)

    def _sources(self):
        """一棵正常树 + 一棵衍生树 + 一棵停用树 + 一个 skip_dirs。"""
        return [
            config.Source(name='rss', root=self.vault / 'source' / 'rss'),
            config.Source(name='dr-youtube-dig',
                          root=self.vault / 'source' / 'dr-youtube-dig',
                          skip_dirs=frozenset({'_graph'})),
            config.Source(name='dr-gzh-projects',
                          root=self.vault / 'source' / 'dr-gzh-projects',
                          derived=True),
            config.Source(name='dr', root=self.vault / 'source' / 'dr',
                          enabled=False),
        ]

    def write(self, *rels, body='正文'):
        """往 vault 落几篇 md(模拟物化那一步真写出来的文章)。"""
        for rel in rels:
            f = self.vault / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f'---\ntitle: {rel}\npublished: 2026-09-28 09:00\n'
                         f'---\n{body}', encoding='utf-8')

    def lines(self):
        raw = self.root / fetch_rounds.FILE_NAME
        return raw.read_text(encoding='utf-8').splitlines() if raw.exists() else []


class RoundBookTests(_Base):
    """账本本体:几次、几条、哪一天、故障时说什么。"""

    def test_a_second_sync_the_same_day_is_a_second_round(self):
        """当天两轮:第二轮是「第 2 次」,报的是它自己抓了多少,不是两轮的和。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', {f'p{i}' for i in range(9)})
        self.assertEqual({'n': 1, 'added': 9, 'at': '09:00'},
                         fetch_rounds.today(day))
        later = T('2026-09-29 15:30')
        fetch_rounds.open_round('batch-b', later)
        fetch_rounds.note_new('batch-b', {'q1', 'q2'})
        self.assertEqual({'n': 2, 'added': 2, 'at': '15:30'},
                         fetch_rounds.today(later))

    def test_one_business_fetch_across_several_sources_is_one_round(self):
        """勾了三个来源同步 = 一次抓取、三次管道运行。按运行记的话面板会写成
        「今天第 3 次抓取」而用户只点过一次。"""
        day = T('2026-09-29 09:00')
        for _ in range(3):
            fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', {'a', 'b', 'c', 'd'})
        self.assertEqual({'n': 1, 'added': 4, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_a_run_that_did_not_fetch_is_not_a_round(self):
        """没开过轮次的运行(只勾导出/索引)落下来的东西不该凭空变出一次抓取。"""
        fetch_rounds.note_new('run-x', {'a', 'b'})
        self.assertEqual({'n': 0, 'added': 0, 'at': ''},
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_an_empty_fetch_still_counts_as_a_fetch(self):
        """什么都没抓到也是抓过一次 —— 用户要能看出"同步跑了,确实没有新的"。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        self.assertEqual({'n': 1, 'added': 0, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_yesterdays_rounds_do_not_count_as_today(self):
        """跨自然日自己归零:昨天抓过 30 条,今天面板上是「今天还没有抓取」。"""
        fetch_rounds.open_round('batch-a', T('2026-09-28 22:00'))
        fetch_rounds.note_new('batch-a', {f'p{i}' for i in range(30)})
        self.assertEqual({'n': 0, 'added': 0, 'at': ''},
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_a_round_that_finishes_after_midnight_still_gets_its_count(self):
        """23:59 开的轮次,00:01 才物化完 —— 它那一笔得记得上(按**开工那天**
        归属)。按"现在是今天"去找轮次的话,这一轮永远停在 0 条。"""
        fetch_rounds.open_round('batch-a', T('2026-09-29 23:59'))
        fetch_rounds.note_new('batch-a', {'a', 'b', 'c'})
        self.assertEqual({'n': 1, 'added': 3, 'at': '23:59'},
                         fetch_rounds.today(T('2026-09-29 23:59')))
        # 过了零点:那一轮算在 29 日,30 日这边还没有抓取
        self.assertEqual({'n': 0, 'added': 0, 'at': ''},
                         fetch_rounds.today(T('2026-09-30 00:01')))

    def test_the_day_boundary_follows_the_project_timezone(self):
        """「今天」按项目时区(Asia/Shanghai),不按宿主机。UTC 机器上按宿主机
        算的话,北京时间 00:00~07:59 这八小时会被记进前一天。"""
        at = datetime(2026, 9, 29, 16, 30, tzinfo=timezone.utc)   # 沪 09-30 00:30
        fetch_rounds.open_round('batch-a', at)
        fetch_rounds.note_new('batch-a', {'a'})
        self.assertEqual({'n': 1, 'added': 1, 'at': '00:30'},
                         fetch_rounds.today(at))
        self.assertEqual({'n': 0, 'added': 0, 'at': ''},
                         fetch_rounds.today(T('2026-09-29 23:00')))
        # 反过来钉一边:不带时区的照旧当项目时区,不再按 UTC 解释一次
        fetch_rounds.open_round('batch-b', T('2026-09-30 09:00'))
        self.assertEqual(2, fetch_rounds.today(T('2026-09-30 09:00'))['n'])

    def test_the_round_number_does_not_stop_at_a_cap(self):
        """同一天抓很多次时次数照旧往上走 —— 旧实现只留最后 200 条明细而
        `n` 取明细条数,第 201 次之后次数就永久停在 200。"""
        day = T('2026-09-29 09:00')
        for i in range(205):
            fetch_rounds.open_round(f'batch-{i}', day)
        self.assertEqual(205, fetch_rounds.today(day)['n'])

    def test_the_count_survives_a_restart(self):
        """账本在数据目录里,重启 kb-web 之后那个数还在。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', {'a', 'b', 'c', 'd', 'e'})
        self.assertEqual(5, fetch_rounds.today(day)['added'])   # 每次都重新读文件

    def test_a_restarted_process_does_not_reuse_a_batch_identity(self):
        """重启之后批次号不许撞上今天已经记过的那一轮 —— 撞上的话第二次同步
        会被当成同一轮(面板停在「第 1 次」,新抓的直接累加进旧那一行)。"""
        day = T('2026-09-29 09:00')
        with patch.object(ps, '_RUN_EPOCH', 'aaaa'):
            first = pipeline_batch.open_batch()
            pipeline_batch.close_batch()
        with patch.object(ps, '_RUN_EPOCH', 'bbbb'), \
                patch.object(pipeline_batch, '_batch_seq', 0):
            second = pipeline_batch.open_batch()
            pipeline_batch.close_batch()
        self.assertNotEqual(first, second)
        for key, n in ((first, 2), (second, 3)):
            fetch_rounds.open_round(key, day)
            fetch_rounds.note_new(key, {f'{key}-{i}' for i in range(n)})
        self.assertEqual({'n': 2, 'added': 3}, {k: fetch_rounds.today(day)[k]
                                                for k in ('n', 'added')})

    def test_a_missing_book_is_a_certain_no_not_unknown(self):
        """反过来钉一边:账本还没建立 = 确定今天没抓过,不是「读不出来」。"""
        self.assertEqual({'n': 0, 'added': 0, 'at': ''},
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_an_unreadable_book_is_unknown_not_no_record(self):
        """账本坏了要说「不可确认」。当成空的后果有两重:面板笃定地说「今天还
        没有抓取」(而今天确实抓过 9 条),下一次开轮次还把它覆盖成「第 1 次」。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', {f'p{i}' for i in range(9)})
        (self.root / fetch_rounds.FILE_NAME).write_text('{坏了', encoding='utf-8')
        self.assertIsNone(fetch_rounds.today(day))
        before = (self.root / fetch_rounds.FILE_NAME).read_bytes()
        fetch_rounds.open_round('batch-b', day)
        # 读不全的文件一个字都不动(认得出来的那部分重写回去 = 悄悄删掉读不懂的)
        self.assertIn(before, (self.root / fetch_rounds.FILE_NAME).read_bytes())
        self.assertIsNone(fetch_rounds.today(day))

    def test_a_half_written_line_does_not_lose_the_confirmed_rounds(self):
        """追加而不是整份重写:断电留下一条半截行,**已经确认的那几轮还在文件里**
        (整份重写的话一次写坏就把当天全带走)。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', {'a', 'b'})
        good = self.lines()
        with open(self.root / fetch_rounds.FILE_NAME, 'a', encoding='utf-8') as fh:
            fh.write('{"key": "batch-b", "da')        # 写到一半断电
        self.assertIsNone(fetch_rounds.today(day))     # 不可确认
        self.assertEqual(good, self.lines()[:len(good)])

    def test_a_write_failure_says_unknown_and_is_retried_later(self):
        """写失败不许假装成功(旧实现吞掉 `OSError`:文章已入库而按钮永久写
        0 条)。这一刻说「不可确认」,下一次账本操作把漏掉的那几行补上去。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        with patch.object(fetch_rounds, '_append', Mock(return_value=False)):
            fetch_rounds.note_new('batch-a', {'a', 'b', 'c'})
            self.assertIsNone(fetch_rounds.today(day))         # 不是 0 条
        fetch_rounds.open_round('batch-a', day)                # 磁盘好了
        self.assertEqual({'n': 1, 'added': 3, 'at': '09:00'},
                         fetch_rounds.today(day))


class SnapshotScopeTests(_Base):
    """快照只收日报认的那些树 —— 口径和 `daily._scope` 同一份。"""

    def test_only_the_sources_the_report_reads_are_in_the_snapshot(self):
        self.write('source/rss/a.md',
                   'source/dr-youtube-dig/v.md',
                   'source/dr-youtube-dig/_graph/agg.md',   # 聚合页不是文档本体
                   'source/dr-gzh-projects/tool.md',        # 衍生树:和原文同一批内容
                   'source/dr/old.md')                      # 停用的树不再收新内容
        self.assertEqual({'source/rss/a.md', 'source/dr-youtube-dig/v.md'},
                         set(fetch_rounds.vault_snapshot()))


class FetchIsTheExportsOwnResultTests(_Base):
    """**这一组是问题 2 的要害**:数的是物化自己的产出,不是索引看到的增量。"""

    def setUp(self):
        super().setUp()
        self.db = self.root / 'kb.db'
        connect = store.connect
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        for p in (patch.object(store, 'connect', lambda *a, **k: connect(self.db)),
                  patch.object(embedder, 'embed_pending_chunks', Mock(return_value=0))):
            p.start(); self.addCleanup(p.stop)
        ps._state.update({'batch': 'batch-probe', 'run_id': 'run-probe',
                          'stages': {s: {'status': '', 'detail': ''}
                                     for s in ps.STAGES}, 'log': []})

    def _channels(self, *writes, body='正文', boom=False):
        """把 `_per_channel` 换成"物化真往 vault 写这几篇"。"""
        def run(stage, crawl_filter, fn):
            if stage != 'export':
                return
            self.write(*writes, body=body)
            if boom:
                raise RuntimeError('某个来源炸了')
        return patch.object(pipeline_stages, '_per_channel', Mock(side_effect=run))

    def _scan(self, *docs):
        return patch.object(scanner, 'scan_all', Mock(return_value=list(docs)))

    def _doc(self, path, source='rss', body='正文', h='h1'):
        return scanner.Doc(path=path, source=source, title=path, account='某号',
                           published='2026-09-28 09:00', url=f'https://e/{path}',
                           body=body, content_hash=h)

    def _got(self):
        return {k: fetch_rounds.today()[k] for k in ('n', 'added')}

    def test_an_empty_fetch_meeting_old_unindexed_files_adds_nothing(self):
        """**审查实测的那一条**:vault 里躺着 3 篇历史未索引的旧文,这一轮一个
        字节都没抓到 —— 按钮该写「第 1 次抓取 · 0 条」,而不是「+3 条」。"""
        self.write('source/rss/old1.md', 'source/rss/old2.md', 'source/rss/old3.md')
        with self._channels():                      # 物化没写出任何新文件
            pipeline_stages._run_crawl()
            pipeline_stages._run_export()
        self.assertEqual({'n': 1, 'added': 0}, self._got())

    def test_what_the_fetch_wrote_counts_even_if_this_run_does_not_index(self):
        """**审查实测的另一条**:真抓到 2 篇、这一轮不索引 —— 照样是 2 条。
        旧实现报 0,而且之后单独索引也补不回来。"""
        with self._channels('source/rss/a.md', 'source/rss/b.md'):
            pipeline_stages._run_crawl()
            pipeline_stages._run_export()
        self.assertEqual({'n': 1, 'added': 2}, self._got())

    def test_indexing_later_does_not_change_the_count(self):
        """隔一次运行再索引:库里多了 2 篇,但抓取事实没变(那不是新的一次抓取,
        也不该把已经记上的那 2 条再记一遍)。"""
        with self._channels('source/rss/a.md', 'source/rss/b.md'):
            pipeline_stages._run_crawl()
            pipeline_stages._run_export()
        ps._state.update({'batch': '', 'run_id': 'run-index-only'})
        with self._scan(self._doc('source/rss/a.md'), self._doc('source/rss/b.md')):
            pipeline_stages._index_pass()
        self.assertEqual({'n': 1, 'added': 2}, self._got())

    def test_a_partial_index_commit_cannot_lose_the_count(self):
        """**问题 3 实测的那一条**:索引分批提交到一半炸了(磁盘满)。抓取那 2 条
        已经是落定的事实 —— 旧实现把记账挂在索引全过之后,这里会永久写 0 条。"""
        with self._channels('source/rss/a.md', 'source/rss/b.md'):
            pipeline_stages._run_crawl()
            pipeline_stages._run_export()
        upsert = store.upsert_document
        calls = {'n': 0}

        def flaky(*a, **k):
            calls['n'] += 1
            if calls['n'] > 1:
                raise sqlite3.OperationalError('database or disk is full')
            return upsert(*a, **k)

        with self._scan(self._doc('source/rss/a.md'), self._doc('source/rss/b.md')), \
                patch.object(store, 'upsert_document', Mock(side_effect=flaky)):
            with self.assertRaises(sqlite3.OperationalError):
                pipeline_stages._index_pass()
        self.assertEqual({'n': 1, 'added': 2}, self._got())

    def test_a_rewritten_old_article_is_not_a_new_catch(self):
        """老文章重新导出(正文补抓、回写译文)会改写同一个路径 —— 它不是这一轮
        抓到的,算进去的话同一篇会连着好几天被报成"刚抓到"。"""
        self.write('source/rss/a.md')
        with self._channels('source/rss/a.md', body='补全了正文'):
            pipeline_stages._run_crawl()
            pipeline_stages._run_export()
        self.assertEqual({'n': 1, 'added': 0}, self._got())

    def test_exporting_without_a_crawl_is_not_a_fetch(self):
        """只勾了导出的那种运行:vault 里确实多了 2 篇(上一轮抓回来还没导的),
        但今天**没有**抓取过,账本上一轮都不该有。"""
        with self._channels('source/rss/a.md', 'source/rss/b.md'):
            pipeline_stages._run_export()
        self.assertEqual({'n': 0, 'added': 0, 'at': ''}, fetch_rounds.today())

    def test_a_source_that_blows_up_does_not_erase_what_the_others_wrote(self):
        """勾了好几个来源,后一个炸了:前面那篇已经在 vault 里、下一轮索引就会
        进库,所以它得算上(记在 `finally` 里的理由)。"""
        with self._channels('source/rss/a.md', boom=True):
            pipeline_stages._run_crawl()
            with self.assertRaises(RuntimeError):
                pipeline_stages._run_export()
        self.assertEqual({'n': 1, 'added': 1}, self._got())

    def test_a_round_of_item_extraction_is_not_a_catch(self):
        """条目化往衍生树写出一批条目:和原文是同一批内容,日报取材本来就排除
        它们。算进来的话一轮条目化就能让按钮写出三位数,而日报那边一条新的都没有。"""
        with self._channels('source/dr-gzh-projects/t1.md',
                            'source/dr-gzh-projects/t2.md'):
            pipeline_stages._run_crawl()
            pipeline_stages._run_export()
        self.assertEqual({'n': 1, 'added': 0}, self._got())


class FetchFactIsNotReportFactTests(_Base):
    """抓取事实不随日报变 —— 这是整件事的起点。"""

    def setUp(self):
        super().setUp()
        self.db = self.root / 'kb.db'
        connect = store.connect
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        for p in (patch.object(daily, 'VAULT_ROOT', self.vault),
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

    def _two_fetched(self, day):
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', {'source/rss/a.md', 'source/rss/b.md'})
        self._doc('source/rss/a.md', '2026-09-28 09:00')
        self._doc('source/rss/b.md', '2026-09-28 10:00')
        return daily.pending(first_days=1, now=day)

    def test_generating_a_report_does_not_change_what_was_fetched(self):
        """生成日报不会把「今天抓了 2 条」抹成 0 —— 旧实现正是这么丢的:
        用户同步完看到数字,生成一次之后那个数就没了,第二次同步抓回来的东西
        再也没有地方显示。"""
        day = T('2026-09-29 08:00')
        info = self._two_fetched(day)
        before = fetch_rounds.today(day)
        daily.generate(since=info['since'], until=info['until'])
        self.assertEqual(before, fetch_rounds.today(day))
        self.assertEqual(2, fetch_rounds.today(day)['added'])

    def test_a_failed_ledger_write_cannot_invent_new_content(self):
        """日报文件写好了、取材账本没写上 —— 这是**受支持**的降级终态(那一档
        故意倒向"多报一遍"而不是永久写死)。按钮上那个数来自抓取账本,所以它一个
        字都不受影响;拿 `daily_carried` 去反推"今天这份里有什么"的实现会在这里
        凭空报出新增(审查实测 `next_button_gain=2` 而真实新增 0)。"""
        day = T('2026-09-29 08:00')
        info = self._two_fetched(day)
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
