# -*- coding: utf-8 -*-
"""回执里的那一条**属于哪一次业务抓取**:这一次新抓到的 / 补导的 / 说不清。

tooltip 上那句分解靠的就是这三档。它们在每天都走到的那条路上必然不等:正文抓取当天
失败、`body_retry` 隔两天补上才导出,那几篇是前几天抓到的 —— 远端今天一条新内容
都没发,按钮却照旧写「+3 条」,而用户看着那个数决定要不要再花一次模型钱。

**三档不是两档**:空串是「没记过」,并进「不是这一次」就等于把不知道说成确定不是,
而那一档在存量库上是绝大多数。

回执本身(谁交的、交不出来怎么办、取材范围怎么筛)在 tests/test_fetch_receipt.py;
账本本体与恢复协议在 tests/test_fetch_rounds.py;前端那几种说法在
tests/daily_probe.mjs。
"""
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from kb.collectors import Fetched
from kb import fetch_rounds, pipeline_segments as segments
from kb import pipeline_stages
from kb import pipeline_state as ps
from tests.fetch_rounds_fixture import BOOK, T, _Base, _Fake


class WhichRoundActuallyCaughtItTests(_Base):
    """**这一组是本任务的要害**:「这一次新抓到的」和「补导之前抓到的」分得开。

    两个数在补导场景下必然不等,而 tooltip 上那个 `added` 报的是前者加后者
    (「这次同步之后日报能多拿到多少」,仓库主人定的口径)。分解不是装饰:远端
    今天一条新内容都没发、按钮却写「+3 条」时,用户得能看出那 3 条是前几天抓到、
    今天正文才补齐的,而不是又值一次模型钱。

    第三档「说不清」单列,不许往补导里并:抓取身份是空串时我们只知道「没记过」,
    不知道「不是这一次」(存量内容、手工导入的链接都长这样)。
    """

    def setUp(self):
        super().setUp()
        # `running` 要一起装上:**光留着 `batch` 不等于"这一轮在跑"** —— 收尾时
        # 那两个字段是故意留下的,所以 `round_key()` 认 `running`(见它的说明)。
        ps._state.update({'running': True,
                          'batch': 'batch-probe', 'run_id': 'run-probe',
                          'stages': {s: {'status': '', 'detail': ''}
                                     for s in ps.STAGES}, 'log': []})
        self.addCleanup(segments.clear_segments)
        segments.clear_segments()

    def _run(self, *fakes):
        by_name = {f.name: f for f in fakes}
        with patch.object(pipeline_stages, 'crawl_channels',
                          Mock(return_value=[{'channel': f.name, 'accounts': []}
                                             for f in fakes])), \
                patch.object(pipeline_stages, 'active_collector',
                             Mock(side_effect=lambda ch=None: by_name[ch])):
            pipeline_stages._run_crawl()
            pipeline_stages._run_export()

    def _split(self):
        return {k: fetch_rounds.today()[k]
                for k in ('added', 'fresh', 'carried', 'unsure')}

    def test_what_this_round_caught_is_told_apart_from_what_it_carried(self):
        """这一轮写下去 3 篇:1 篇是今天抓到的,2 篇是前几天抓到、今天才补导的。"""
        self._run(_Fake(self, 'rss', writes=['source/rss/new.md'],
                        carried=['source/rss/old1.md', 'source/rss/old2.md']))
        self.assertEqual({'added': 3, 'fresh': 1, 'carried': 2, 'unsure': 0},
                         self._split())

    def test_a_round_that_only_carried_old_items_caught_nothing_new(self):
        """远端一条新内容都没发,导出把前几天积压的正文补齐了 —— `added` 照实
        报 2(日报这一刻确实多拿到 2 条),但新抓到的是 0。"""
        self._run(_Fake(self, 'rss',
                        carried=['source/rss/old1.md', 'source/rss/old2.md']))
        self.assertEqual({'added': 2, 'fresh': 0, 'carried': 2, 'unsure': 0},
                         self._split())

    def test_items_with_no_recorded_round_are_unsure_not_carried(self):
        """存量内容交不出抓取身份:归「说不清」,不许算成补导的。"""
        self._run(_Fake(self, 'rss', writes=['source/rss/new.md'],
                        nameless=['source/rss/stock.md']))
        self.assertEqual({'added': 2, 'fresh': 1, 'carried': 0, 'unsure': 1},
                         self._split())

    def test_the_breakdown_survives_a_replay_of_the_same_receipt(self):
        """同一份回执记两遍(fsync 失败后重试、两条通道各报一次):分解不翻倍。

        并集按内容身份取,抓取身份跟着那一条走 —— 存成三个计数器的话这里会
        变成 2/2/0。"""
        self._run(_Fake(self, 'rss', writes=['source/rss/new.md'],
                        carried=['source/rss/old.md']))
        fetch_rounds.note_new('batch-probe', frozenset({
            Fetched('rss:source/rss/new.md', 'batch-probe', 'source/rss/new.md'),
            Fetched('rss:source/rss/old.md', 'batch-older', 'source/rss/old.md')}))
        self.assertEqual({'added': 2, 'fresh': 1, 'carried': 1, 'unsure': 0},
                         self._split())

    def test_a_known_round_is_not_overwritten_by_a_later_blank_one(self):
        """同一条内容第二次被报上来时身份是空的:留住已知的那个,别退回「说不清」。

        反过来也要成立 —— 先空后有就补上。否则一次降级上报会把已经知道的事实
        又丢回去。"""
        fetch_rounds.open_round('batch-k', T('2026-09-29 09:00'))
        one = 'source/rss/a.md'
        fetch_rounds.note_new('batch-k', {Fetched('rss:a', 'batch-older', one)})
        fetch_rounds.note_new('batch-k', {Fetched('rss:a', '', one)})
        fetch_rounds.note_end('batch-k')          # 物化这一段明确走完了
        self.assertEqual(BOOK(1, 1, '09:00', fresh=0, carried=1),
                         fetch_rounds.today(T('2026-09-29 09:00')))
        fetch_rounds.note_new('batch-k', {Fetched('rss:b', '', 'source/rss/b.md')})
        fetch_rounds.note_new('batch-k', {Fetched('rss:b', 'batch-k', 'source/rss/b.md')})
        self.assertEqual(BOOK(1, 2, '09:00', fresh=1, carried=1),
                         fetch_rounds.today(T('2026-09-29 09:00')))


class TheRealExporterReportsOnlyFreshFilesTests(_Base):
    """回执由**真的** `sync.export` 交:它写了哪几篇、哪几篇算新落下的。

    这一组不装假采集器 —— 「新落下」这个判据长在导出那边(写之前问一句 vault 里
    有没有这个路径),只钉假采集器的话等于没钉:导出队列是
    `exported_hash<>content_hash`,正文补抓、译文回写都会让老文章重新写一遍同一个
    路径,它们不是这一轮抓到的。
    """

    def setUp(self):
        super().setUp()
        from kb import maintenance
        from kb.feeds import registry as reg
        from kb.feeds import sync as fsync
        self.reg, self.fsync = reg, fsync
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(fsync, 'VAULT_ROOT', self.vault)):
            p.start(); self.addCleanup(p.stop)
        self.source = reg.save_subscription('rss', {'name': 'T',
                                                    'url': 'https://e.org/f.xml'})

    def _entry(self, n=1):
        return [{'url': f'https://e.org/a{n}', 'title': f'文章{n}',
                 'body': '正文正文', 'published': '2026-09-28 09:00'}]

    @contextmanager
    def _round(self, key):
        """管道此刻正在跑哪一次业务抓取(`fetch_rounds.round_key()` 读的就是它)。

        只改这几个键、不整份换掉 `_state`:换掉的话 `stages` 一起没了,而
        `sync.export` 收尾那一下要往里写阶段状态。

        **「没在跑」要如实装成 `running=False`、而不是把 `batch` 清空。** 真实的
        空闲状态是「`running` 放下了、`batch` 还留着上一轮的」(收尾故意留的,见
        `pipeline_state._end_run`),清空 `batch` 模拟出来的那种状态在生产里压根
        不存在 —— 用它当桩,`round_key()` 读错判据也照样绿。
        """
        before = (ps._state.get('running'), ps._state.get('batch'),
                  ps._state.get('run_id'))
        ps._state['running'] = bool(key)
        ps._state['batch'], ps._state['run_id'] = key or before[1], ''
        try:
            yield
        finally:
            (ps._state['running'], ps._state['batch'],
             ps._state['run_id']) = before

    def test_the_first_export_reports_the_file_it_created(self):
        self.reg.put_items(self.source, self._entry())
        got = self.fsync.export('rss')
        self.assertEqual(1, len(got))
        one = next(iter(got))
        self.assertTrue((self.vault / one.path).exists())
        # 内容身份带来源前缀 + feed_items 的主键(稳定、跨改名不变)
        self.assertTrue(one.content_id.startswith('rss:'))

    def test_the_export_hands_back_the_round_that_found_the_item(self):
        """回执里的抓取身份就是 `put_items` 那一刻记下的那一轮。

        这是「本次新抓到」那一档的来源:入库时记,导出时交。按导出时刻反推的话,
        昨天抓到、今天才补导的那几篇会被算成今天新抓到的。"""
        with self._round('batch-found-it'):
            self.reg.put_items(self.source, self._entry())
        got = self.fsync.export('rss')
        self.assertEqual('batch-found-it', next(iter(got)).fetch_id)

    def test_an_item_from_an_earlier_round_is_carried_not_freshly_caught(self):
        """**这一条是本任务的要害**:昨天那一轮抓到的条目,今天这一轮才导出 ——
        账本该说「这 1 条是补导的」,而不是「今天新抓到 1 条」。

        旧实现压根分不出来:回执里只有路径,两种情形长得一模一样。远端今天一条
        新内容都没发,按钮却写「+1 条新抓到」,用户照着它再花一次模型钱。"""
        with self._round('batch-yesterday'):
            self.reg.put_items(self.source, self._entry())
        fetch_rounds.open_round('batch-today', T('2026-09-29 09:00'))
        with self._round('batch-today'):
            fetch_rounds.note_new('batch-today', self.fsync.export('rss'))
            fetch_rounds.note_end('batch-today')
        self.assertEqual(BOOK(1, 1, '09:00', fresh=0, carried=1),
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_an_item_found_by_this_very_round_is_a_fresh_catch(self):
        """反过来钉一边:同一轮里抓到又导出的,就是「这一次新抓到的」。"""
        fetch_rounds.open_round('batch-now', T('2026-09-29 09:00'))
        with self._round('batch-now'):
            self.reg.put_items(self.source, self._entry())
            fetch_rounds.note_new('batch-now', self.fsync.export('rss'))
            fetch_rounds.note_end('batch-now')
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_a_stock_item_with_no_round_is_counted_but_not_classified(self):
        """入库时压根没记过抓取身份(存量条目、在终端里跑的一次采集):条数照旧
        算进去,那句分解归到「说不清」—— 不许算成补导,也不许算成新抓到。"""
        with self._round(''):                            # 管道没在跑 = 空串
            self.reg.put_items(self.source, self._entry())
        fetch_rounds.open_round('batch-today', T('2026-09-29 09:00'))
        with self._round('batch-today'):
            fetch_rounds.note_new('batch-today', self.fsync.export('rss'))
            fetch_rounds.note_end('batch-today')
        self.assertEqual(BOOK(1, 1, '09:00', fresh=0, unsure=1),
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_an_idle_pipeline_hands_out_no_identity_even_though_one_is_left_over(self):
        """管道收尾之后 `batch` **还留在状态里** —— 那不是「这一轮在跑」。

        留着是故意的:晚一步来取结果的人要按 id 认领自己那一轮
        (`pipeline_state._end_run`)。拿它当当前身份的话,管道外直接入库的那一条
        (独立采集、面板上手点的维护动作)会被盖上一个跟它毫无关系的批次号 ——
        而 `put_items` 的 `DO NOTHING` 是**首次说了算**,盖错了救不回来,tooltip 从此
        把它算成「这一次新抓到的」。

        **这一条必须自己摆出"批次号还在"这个前提**:把 `batch` 清空再断言空串,
        等于换了个生产里不存在的状态去问,读错判据也照样绿。
        """
        before = (ps._state.get('running'), ps._state.get('batch'))
        self.addCleanup(lambda: ps._state.update(
            {'running': before[0], 'batch': before[1]}))
        ps._state.update({'running': False, 'batch': 'batch-finished'})
        self.assertTrue(ps._state['batch'])              # 前提:身份确实还躺着
        self.assertEqual('', fetch_rounds.round_key(),
                         '空闲时把上一轮的批次号当成当前身份了')
        self.reg.put_items(self.source, self._entry())
        with self.reg.db() as c:
            self.assertEqual([''], [r['fetch_round'] for r in c.execute(
                "SELECT fetch_round FROM feed_items WHERE kind='rss'")],
                '管道外入库的那一条被永久盖上了上一轮的批次号')
        # 反过来钉一边:真在跑的时候照旧答得出这一轮是哪一次
        ps._state['running'] = True
        self.assertEqual('batch-finished', fetch_rounds.round_key())

    def test_a_second_export_with_nothing_pending_reports_nothing(self):
        self.reg.put_items(self.source, self._entry())
        self.fsync.export('rss')
        self.assertEqual(frozenset(), self.fsync.export('rss'))

    def test_a_file_deleted_from_the_vault_is_not_a_fresh_catch_on_rewrite(self):
        """存量清理删掉过那篇 md,之后这一条重新入队导出 —— 它不是新抓到的。

        判据是**两道一起**(库里那一格 + 这个路径上此刻有没有文件),这一条钉的是
        库里那一格那一半:只按文件在不在判的话,这里会报「+1 条」,而那是一篇几天
        前就抓到的老文章。另一半由
        `test_a_landed_file_is_not_a_fresh_catch_again_in_the_next_round` 钉。"""
        self.reg.put_items(self.source, self._entry())
        first = next(iter(self.fsync.export('rss')))
        (self.vault / first.path).unlink()
        with self.reg.db() as c:
            c.execute("UPDATE feed_items SET content_hash='changed' WHERE kind='rss'")
        again = self.fsync.export('rss')
        self.assertEqual(frozenset(), again)
        self.assertTrue((self.vault / first.path).exists())  # 文件真又写回来了

    def test_rewriting_an_old_article_is_not_a_fresh_catch(self):
        """正文补抓/译文回写让同一条重新入队:md 确实又写了一遍,但回执是空的。

        算进去的话同一篇会连着好几天被报成"刚抓到",而用户看着那个数决定要不要
        再花一次模型钱重新生成日报。"""
        self.reg.put_items(self.source, self._entry())
        first = self.fsync.export('rss')
        with self.reg.db() as c:
            c.execute("UPDATE feed_items SET content_hash='changed' WHERE kind='rss'")
        again = self.fsync.export('rss')
        path = self.vault / next(iter(first)).path
        self.assertEqual(frozenset(), again)
        self.assertTrue(path.exists())          # 文件真又写了一遍,只是不算新抓到

    def test_a_file_that_landed_stays_in_the_receipt_when_the_confirm_fails(self):
        """md 已经替换进 vault、随后 `feed_items` 那次确认失败 —— 这一篇必须留在
        回执里(第 1 轮审查实测)。

        旧判据是「两道都要卡」(文件落了 + 入库确认也成了),于是确认那一下的异常
        被接成逐项错误、回执照旧返回一个看着**确定**的空集;而那时重导时它已经
        不算新文件,这一篇**永远**补不回账(实测:vault 多了 1 篇,tooltip 永久写
        「0 条」且自称确定,刷新和重试都纠不回来)。

        反过来钉一边:确认没写上 `path`,可那篇 md 已经在 vault 里了 —— 恢复之后
        重导**不许再报一次**它(判据的另一半,F1),而第一份回执里的那一条照旧留在
        账本里。
        """
        fetch_rounds.open_round('batch-confirm', T('2026-09-29 09:00'))
        with self._round('batch-confirm'):
            self.reg.put_items(self.source, self._entry())
            with self.reg.db() as c:
                c.execute("CREATE TRIGGER no_confirm BEFORE UPDATE OF exported_hash "
                          "ON feed_items BEGIN SELECT RAISE(ABORT, '确认失败'); END")
            got = self.fsync.export('rss')
            self.assertEqual(1, len(got))                            # 回执里有它
            self.assertTrue((self.vault / next(iter(got)).path).exists())
            fetch_rounds.note_new('batch-confirm', got)
            with self.reg.db() as c:
                self.assertEqual([''], [r[0] for r in c.execute(
                    "SELECT exported_hash FROM feed_items WHERE kind='rss'")])
                c.execute('DROP TRIGGER no_confirm')
            again = self.fsync.export('rss')
            self.assertEqual(frozenset(), again)         # 已经物化过了,不是新落的
            fetch_rounds.note_new('batch-confirm', again)
            fetch_rounds.note_end('batch-confirm')
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_a_landed_file_is_not_a_fresh_catch_again_in_the_next_round(self):
        """**审查实测的那一条(F1)**:第一次同步落了文件、随后那次入库确认
        失败;第二次同步远端一篇新的都没有 —— 它不许再被算成这一次的新增。

        实测的旧末态:判据只看 `feed_items.path`,而确认失败时那一格还空着,于是
        **换一个业务身份**重导同一篇又满足「头一回落进 vault」,账本的轮内并集管不到
        另一轮 —— tooltip 写「第 2 次抓取 · +1 条」,提示语还说这 1 篇是这一次才补导
        进来的,而 vault 里的文件集合和字节一个都没变。用户照着那个数再花一次模型钱。

        两头都钉:第一轮照旧报它(确认失败不许把已落地的成果从成果里抹掉),第二轮
        报 0 条**而且是确定的 0**(不是「不可确认」—— 那同样会让人去点一次重新生成)。
        """
        fetch_rounds.open_round('batch-first', T('2026-09-29 09:00'))
        with self._round('batch-first'):
            self.reg.put_items(self.source, self._entry())
            with self.reg.db() as c:
                c.execute("CREATE TRIGGER no_confirm BEFORE UPDATE OF exported_hash "
                          "ON feed_items BEGIN SELECT RAISE(ABORT, '确认失败'); END")
            fetch_rounds.note_new('batch-first', self.fsync.export('rss'))
            fetch_rounds.note_end('batch-first')
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(T('2026-09-29 09:00')))
        was = {q: q.read_bytes() for q in self.vault.rglob('*.md')}
        with self.reg.db() as c:
            c.execute('DROP TRIGGER no_confirm')         # 确认那一下好了
        fetch_rounds.open_round('batch-second', T('2026-09-29 10:00'))
        with self._round('batch-second'):
            self.reg.put_items(self.source, self._entry())   # 远端还是那一篇
            got = self.fsync.export('rss')
            fetch_rounds.note_new('batch-second', got)
            fetch_rounds.note_end('batch-second')
        self.assertEqual(frozenset(), got)               # 这一次一篇新的都没有
        self.assertEqual(was, {q: q.read_bytes() for q in self.vault.rglob('*.md')})
        self.assertEqual(BOOK(2, 0, '10:00'),
                         fetch_rounds.today(T('2026-09-29 10:00')))
