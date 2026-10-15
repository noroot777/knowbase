# -*- coding: utf-8 -*-
"""抓取成果的**归属**:哪几篇算这一次抓到的。

「抓了多少」一度是拿旁观者的视角算出来的,两版各错一次:先是索引阶段数"库里头
一回出现这个路径"的文档数(空抓取碰上 3 篇历史未索引的旧文报成「+3 条」,反过来
真抓到 2 篇而这一轮不索引则报 0、之后单独索引也补不回来),再是物化前后取全 vault
路径差集(物化那段时间里别的进程落下的文件被算进这一轮)。差集分不出"谁写的"。

所以现在只认生产者自己交回来的回执(`SourceCollector.materialize()` 的返回值)。
这一组钉的就是它:回执说了算、回执说不出来就报「不可确认」、回执里不算新内容的
路径由账本按取材范围筛掉,外加真 `sync.export` 自己那条「新落下」的判据。

账本本体(几次、几条、故障时说什么)和它的恢复协议在 tests/test_fetch_rounds.py;
前端那几种说法在 tests/daily_probe.mjs。
"""
import json
import sqlite3
import subprocess
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kb.collectors import Fetched, wechat_local
from kb import embedder, fetch_rounds, pipeline_segments as segments
from kb import pipeline_stages, scanner, store
from kb import pipeline_state as ps
from fetch_rounds_fixture import BOOK, T, _Base, _Fake


class ReceiptIsTheOnlyEvidenceTests(_Base):
    """**这一组是问题 2 的要害**:只认生产者交回来的回执,不拿旁观者的差集猜。"""

    def setUp(self):
        super().setUp()
        ps._state.update({'batch': 'batch-probe', 'run_id': 'run-probe',
                          'stages': {s: {'status': '', 'detail': ''}
                                     for s in ps.STAGES}, 'log': []})
        self.addCleanup(segments.clear_segments)
        segments.clear_segments()

    def _run(self, *fakes):
        """装上这几个采集器跑一轮抓取 + 物化,`_per_channel` 走真的。"""
        by_name = {f.name: f for f in fakes}
        with patch.object(pipeline_stages, 'crawl_channels',
                          Mock(return_value=[{'channel': f.name, 'accounts': []}
                                             for f in fakes])), \
                patch.object(pipeline_stages, 'active_collector',
                             Mock(side_effect=lambda ch=None: by_name[ch])):
            pipeline_stages._run_crawl()
            pipeline_stages._run_export()

    def _got(self):
        return {k: fetch_rounds.today()[k] for k in ('n', 'added', 'partial')}

    def test_a_file_another_process_wrote_meanwhile_is_not_this_catch(self):
        """**第 3 轮审查实测的那一条**:这一轮确定空抓(回执是空集),而物化期间
        另一个进程手工放进一篇 `source/rss/manual.md` —— 按钮该写「0 条」。

        旧实现拿物化前后的全 vault 差集当成果,这里报「+1 条」:差集是旁观者的
        视角,它分不出"谁写的"。那一篇确实会进索引、会进日报,但它不属于**这一次
        抓取**,而按钮上那行字说的正是"这一次抓了多少"。"""
        self._run(_Fake(self, 'rss', aside=['source/rss/manual.md']))
        self.assertEqual({'n': 1, 'added': 0, 'partial': False}, self._got())
        self.assertTrue((self.vault / 'source/rss/manual.md').exists())

    def test_what_the_collector_reports_is_what_gets_counted(self):
        """反过来钉一边:采集器说写了这两篇,就算这两篇(不多不少)。"""
        self._run(_Fake(self, 'rss', writes=['source/rss/a.md', 'source/rss/b.md'],
                        aside=['source/rss/manual.md']))
        self.assertEqual({'n': 1, 'added': 2, 'partial': False}, self._got())

    def test_a_collector_that_cannot_say_makes_the_count_unknown(self):
        """采集器给不出清单(回执 `None`):这一轮的条数是「不可确认」,不是 0。

        公众号那条通道真会走到这里 —— 导出超时后降级 `--no-images` 重跑,第一趟
        已经写下去的文章在第二趟里算「已存在」被跳过,清单是个少记的下界。"""
        self._run(_Fake(self, 'wechat_local', writes=['source/rss/a.md'],
                        receipt=False))
        self.assertEqual({'n': 1, 'added': 0, 'partial': True}, self._got())

    def test_one_source_that_cannot_say_does_not_fake_a_total(self):
        """勾了两个来源:前一个交了 2 篇的回执,后一个说不出来 —— 总数只能是
        「至少 2 条」。

        少了这一笔的后果是面板拿 2 当**确定**的总数,而后一个很可能已经往 vault
        落了文章;用户照着一个偏小的数决定不重新生成日报。"""
        self._run(_Fake(self, 'rss', writes=['source/rss/a.md', 'source/rss/b.md']),
                  _Fake(self, 'wechat_local', writes=['source/rss/c.md'],
                        receipt=False))
        self.assertEqual({'n': 1, 'added': 2, 'partial': True}, self._got())

    def test_a_source_that_blows_up_does_not_erase_what_the_others_wrote(self):
        """勾了两个来源,后一个炸在半路:前一个那篇已经在 vault 里、下一轮索引
        就会进库,它的回执已经落盘,所以那个数留得住 —— 只是总数标成下界。

        这是「每个来源各记一次、记在自己的 `finally` 里」那条的后果面。"""
        self._run(_Fake(self, 'rss', writes=['source/rss/a.md']),
                  _Fake(self, 'wechat_local', writes=['source/rss/b.md'], boom=True))
        self.assertEqual({'n': 1, 'added': 1, 'partial': True}, self._got())

    def test_two_sources_that_both_report_add_up_exactly(self):
        """反过来钉一边:两个来源都交得出回执时,总数是确定的并集。"""
        self._run(_Fake(self, 'rss', writes=['source/rss/a.md']),
                  _Fake(self, 'wechat_local', writes=['source/rss/b.md']))
        self.assertEqual({'n': 1, 'added': 2, 'partial': False}, self._got())

    def test_the_same_content_under_two_names_counts_once(self):
        """同一条内容在一轮里被报了两次,两次的**文件名不一样**:算 1 条。

        公众号那边的文件名是「号-标题」,标题一改名字就变;一次中途失败后的重跑
        会把它按新标题再写一遍,于是同一篇出现两个路径。按路径取并集的话这里报
        2 条 —— 而内容只有一条,它靠 `articles.id` 这个稳定身份认出来。"""
        fetch_rounds.open_round('batch-dup', T('2026-09-29 09:00'))
        fetch_rounds.note_new('batch-dup', {
            Fetched('wechat_local:2247', 'batch-dup', 'source/rss/旧标题.md'),
            Fetched('wechat_local:2247', 'batch-dup', 'source/rss/新标题.md')})
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_a_receipt_row_that_cannot_be_read_makes_the_round_a_lower_bound(self):
        """回执里有一条读不懂:**读得懂的照旧记下,这一轮报下界**。

        静默跳过(旧写法)的后果和「挑着收一份少记的清单」一样:面板给出一个
        偏小、**看着确定**的数,用户照它决定不重新生成日报,而真落进库的比它多。
        所以认得出的那一条照旧记,同时落一笔 partial —— 前端据此说「至少 +1 条」。
        """
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-torn', day)
        fetch_rounds.note_new('batch-torn', [
            Fetched('rss:a', 'batch-torn', 'source/rss/a.md'),
            ('只有两个字段',),                      # 形状读不出来
        ])
        self.assertEqual(BOOK(1, 1, '09:00', partial=True),
                         fetch_rounds.today(day))

    def test_a_row_with_no_path_is_a_gap_not_a_silent_skip(self):
        """答不出路径的那一条也算缺口:范围按路径判,答不出就判不了。

        不记这一笔的话它和「这一轮确实只有 1 篇」在面板上长得一样。
        """
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-nopath', day)
        fetch_rounds.note_new('batch-nopath', [
            Fetched('rss:a', 'batch-nopath', 'source/rss/a.md'),
            Fetched('rss:b', 'batch-nopath', ''),
        ])
        self.assertEqual(BOOK(1, 1, '09:00', partial=True),
                         fetch_rounds.today(day))

    def test_a_row_outside_the_scope_is_a_normal_skip_not_a_gap(self):
        """反过来钉一边:**不在取材范围内的那几条是正常筛掉,不是缺口**。

        衍生树、停用的树、`skip_dirs` 天天都会被筛掉几条;把它们也算成缺口的话
        每一轮都写「至少 +N 条」,那三个字从此再也不代表任何事情。
        """
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-scope', day)
        fetch_rounds.note_new('batch-scope', [
            Fetched('rss:a', 'batch-scope', 'source/rss/a.md'),
            Fetched('gz:b', 'batch-scope', 'source/dr-gzh-projects/b.md'),
        ])
        self.assertEqual(BOOK(1, 1, '09:00'), fetch_rounds.today(day))

    def test_a_collector_still_on_the_old_contract_is_counted_not_refused(self):
        """还按旧契约交**裸路径集合**的采集器:条数照旧确定,只有分解说不清。

        拒掉它等于把真落进库的那几篇静默少记成一个确定值;收下来只损失那句
        分解 —— `unsure` 如实说「说不清是哪一次抓到的」,而不是冒充「不是本次」。
        """
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-legacy', day)
        fetch_rounds.note_new('batch-legacy',
                              ['source/rss/a.md', 'source/rss/b.md'])
        self.assertEqual(BOOK(1, 2, '09:00', fresh=0, unsure=2),
                         fetch_rounds.today(day))


class WhichRoundActuallyCaughtItTests(_Base):
    """**这一组是本任务的要害**:「这一次新抓到的」和「补导之前抓到的」分得开。

    两个数在补导场景下必然不等,而药丸上那个 `added` 报的是前者加后者
    (「这次同步之后日报能多拿到多少」,仓库主人定的口径)。分解不是装饰:远端
    今天一条新内容都没发、按钮却写「+3 条」时,用户得能看出那 3 条是前几天抓到、
    今天正文才补齐的,而不是又值一次模型钱。

    第三档「说不清」单列,不许往补导里并:抓取身份是空串时我们只知道「没记过」,
    不知道「不是这一次」(存量内容、手工导入的链接都长这样)。
    """

    def setUp(self):
        super().setUp()
        ps._state.update({'batch': 'batch-probe', 'run_id': 'run-probe',
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
        self.assertEqual(BOOK(1, 1, '09:00', fresh=0, carried=1),
                         fetch_rounds.today(T('2026-09-29 09:00')))
        fetch_rounds.note_new('batch-k', {Fetched('rss:b', '', 'source/rss/b.md')})
        fetch_rounds.note_new('batch-k', {Fetched('rss:b', 'batch-k', 'source/rss/b.md')})
        self.assertEqual(BOOK(1, 2, '09:00', fresh=1, carried=1),
                         fetch_rounds.today(T('2026-09-29 09:00')))


class WeChatReceiptTests(_Base):
    """公众号那条通道:子进程打出来的清单怎么变成回执,什么时候只能说不出来。

    这是仓库主人的主力来源(库里八千多篇),所以它那一侧必须自己有红 —— 只钉
    假采集器的话,「超时降级之后清单是个少记的下界」这种只存在于真实现里的
    形态一条都发现不了。
    """

    def _proc(self, stdout, code=0):
        return SimpleNamespace(stdout=stdout, stderr='', returncode=code)

    def _event(self, out, names, ids=None, rounds=None):
        """子进程那份结构化事件。`items` 每条是 `{id, round, name}`。"""
        items = [{'id': (ids or {}).get(n, f'aid-{i}'),
                  'round': (rounds or {}).get(n, 'batch-probe'), 'name': n}
                 for i, n in enumerate(names)]
        return json.dumps({'stage': 'export', 'status': 'ok', 'detail': '完成',
                           'no_body': [], 'out': out, 'items': items},
                          ensure_ascii=False)

    def _run(self, proc_or_exc, problem=''):
        col = wechat_local.collector
        run = (Mock(side_effect=proc_or_exc) if isinstance(proc_or_exc, list)
               else Mock(return_value=proc_or_exc))
        with patch.object(wechat_local, '_backend_problem', Mock(return_value=problem)), \
                patch.object(wechat_local, '_run_cli', run):
            return col.materialize()

    def test_the_subprocess_list_becomes_identified_receipt_rows(self):
        """清单里那一条换算成 `(内容身份, 抓取身份, vault 相对路径)`。

        三样都要:路径定取材范围,内容身份让重放幂等,抓取身份让药丸分得开
        「这一次新抓到」和「补导之前抓到」。"""
        out = str(self.vault / 'source' / 'rss')          # 本夹具里的在册来源树
        got = self._run(self._proc(self._event(
            out, ['2026-10-15/某号-标题.md'], ids={'2026-10-15/某号-标题.md': '2247'},
            rounds={'2026-10-15/某号-标题.md': 'batch-7'})))
        self.assertEqual(frozenset({Fetched(
            'wechat_local:2247', 'batch-7',
            'source/rss/2026-10-15/某号-标题.md')}), got)

    def test_an_article_found_in_an_earlier_round_keeps_that_round(self):
        """抓取当天正文没拿到、隔两天补上才导出 —— 回执里记的仍是**发现**它的
        那一轮,不是导出它的这一轮。

        这是「补导」在公众号那条通道上的真实形态(`article_fetch_rounds` 靠
        `INSERT OR IGNORE` 把首次发现钉住)。记成这一轮的话药丸会把它算成
        「这一次新抓到的」,而远端那天压根没发新文章。"""
        out = str(self.vault / 'source' / 'rss')
        got = self._run(self._proc(self._event(
            out, ['2026-10-13/旧文.md'], ids={'2026-10-13/旧文.md': '1001'},
            rounds={'2026-10-13/旧文.md': 'batch-older'})))
        self.assertEqual('batch-older', next(iter(got)).fetch_id)

    def test_a_stock_article_without_a_round_is_unsure_not_someone_elses(self):
        """存量那八千多篇没有抓取身份:交空串「说不清」,不许编一个。

        编成这一轮 = 把补导的算成新抓到的;编成"别的轮次" = 把不知道写成确定
        不是本次。两种都是凭空造事实,所以空串原样交上去,由账本归到 unsure。"""
        out = str(self.vault / 'source' / 'rss')
        got = self._run(self._proc(self._event(
            out, ['2026-01-01/存量.md'], rounds={'2026-01-01/存量.md': ''})))
        self.assertEqual('', next(iter(got)).fetch_id)

    def test_an_export_root_outside_the_vault_cannot_be_a_receipt(self):
        """`out` 不在 vault 里:算出来的相对路径不是知识库里那个身份。

        记一批不存在的路径会让后面真落下来的同名文章被当成「已经数过」——
        账本正是靠路径去重的。宁可报「不可确认」。"""
        self.assertIsNone(self._run(self._proc(self._event('/tmp/别处', ['a.md']))))

    def test_an_old_collector_without_the_list_says_unknown(self):
        """`KB_COLLECTOR_DIR` 指着一份还不打清单的旧采集器 —— 不许当成 0 条。"""
        old = json.dumps({'stage': 'export', 'status': 'ok', 'detail': '完成',
                          'no_body': []}, ensure_ascii=False)
        self.assertIsNone(self._run(self._proc(old)))

    def test_a_list_row_that_cannot_be_read_makes_the_whole_round_unknown(self):
        """清单里混进一条读不懂的:整轮报「不可确认」,**不挑着收那几条认得的**。

        挑着收等于把一个少记的下界当成确定值 —— 读不懂的那几条很可能正是真写
        下去的文章。"""
        out = str(self.vault / 'source' / 'rss')
        raw = json.dumps({'stage': 'export', 'status': 'ok', 'detail': '完成',
                          'no_body': [], 'out': out,
                          'items': [{'id': 'a', 'round': 'r', 'name': 'x.md'},
                                    '这一条不是个对象']}, ensure_ascii=False)
        self.assertIsNone(self._run(self._proc(raw)))

    def test_a_degraded_rerun_after_a_timeout_says_unknown(self):
        """超时后降级 `--no-images` 重跑:第一趟已落盘的文章在第二趟算「已存在」
        被跳过,第二趟的清单是个少记的下界 —— 报它等于少报。"""
        out = str(self.vault / 'source' / 'rss')
        got = self._run([subprocess.TimeoutExpired('export', 1),
                         self._proc(self._event(out, ['2026-10-15/b.md']))])
        self.assertIsNone(got)

    def test_a_failed_subprocess_says_unknown_not_zero(self):
        out = str(self.vault / 'source' / 'rss')
        self.assertIsNone(self._run(self._proc(self._event(out, ['a.md']), code=1)))

    def test_a_collector_that_is_not_installed_wrote_nothing_for_certain(self):
        """反过来钉一边:采集器压根没装好 = 一个字节都没写,那是**确定**的空集。"""
        self.assertEqual(frozenset(), self._run(self._proc(''), problem='缺 .venv'))


class ABrokenLedgerMustNotBlockTheCaptureTests(_Base):
    """账本是个展示用的东西,它坏掉不许把整次抓取拦在采集器之前。

    实测形态:有人(或哪个工具)把 `fetch_rounds.jsonl` 写成了二进制,`_read` 把
    `UnicodeDecodeError` 一路抛给调用方,而第一个调用方是抓取阶段的 `open_round`
    —— 于是一份坏掉的展示账本让这一轮一个号都没抓(第 3 轮审查实测)。
    """

    def setUp(self):
        super().setUp()
        ps._state.update({'batch': 'b', 'run_id': 'r', 'planned_stages': None,
                          'stages': {s: {'status': '', 'detail': ''}
                                     for s in ps.STAGES}, 'log': []})

    def test_a_non_utf8_ledger_is_unknown_and_the_collector_still_runs(self):
        (self.root / fetch_rounds.FILE_NAME).write_bytes(b'\xff\xfe')
        called = Mock()
        with patch.object(pipeline_stages, '_per_channel', called):
            try:
                pipeline_stages._run_crawl()
            except Exception as e:                      # noqa: BLE001
                # 判据是后果,所以这里自己判红:异常穿出去 = 这一轮一个号都没抓。
                self.fail(f'坏掉的展示账本把抓取拦住了:{type(e).__name__}')
        self.assertTrue(called.called)                  # 采集器照样跑到了
        self.assertIsNone(fetch_rounds.today())         # 而且如实说不可确认


class ReceiptScopeTests(_Base):
    """回执里不算「抓到的新内容」的那些路径,账本自己筛掉(`in_scope`)。

    范围口径和日报取材认的那批树同一份:衍生树、停用的树、`skip_dirs` 都不算。
    交给每个采集器各判一遍的话迟早对不上 —— 对不上的表现是药丸报出一批压根
    不会进日报的文件。
    """

    def test_only_the_sources_the_report_reads_are_counted(self):
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', {
            'source/rss/a.md',
            'source/dr-youtube-dig/v.md',
            'source/dr-youtube-dig/_graph/agg.md',   # 聚合页不是文档本体
            'source/dr-gzh-projects/tool.md',        # 衍生树:和原文同一批内容
            'source/dr/old.md',                      # 停用的树不再收新内容
            'daily/2026-09-29.md',                   # 压根不在任何来源树里
        })
        self.assertEqual(2, fetch_rounds.today(day)['added'])


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

        只改这两个键、不整份换掉 `_state`:换掉的话 `stages` 一起没了,而
        `sync.export` 收尾那一下要往里写阶段状态。空串 = 管道没在跑。
        """
        before = (ps._state.get('batch'), ps._state.get('run_id'))
        ps._state['batch'], ps._state['run_id'] = key, ''
        try:
            yield
        finally:
            ps._state['batch'], ps._state['run_id'] = before

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
        self.assertEqual(BOOK(1, 1, '09:00', fresh=0, carried=1),
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_an_item_found_by_this_very_round_is_a_fresh_catch(self):
        """反过来钉一边:同一轮里抓到又导出的,就是「这一次新抓到的」。"""
        fetch_rounds.open_round('batch-now', T('2026-09-29 09:00'))
        with self._round('batch-now'):
            self.reg.put_items(self.source, self._entry())
            fetch_rounds.note_new('batch-now', self.fsync.export('rss'))
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
        self.assertEqual(BOOK(1, 1, '09:00', fresh=0, unsure=1),
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_a_second_export_with_nothing_pending_reports_nothing(self):
        self.reg.put_items(self.source, self._entry())
        self.fsync.export('rss')
        self.assertEqual(frozenset(), self.fsync.export('rss'))

    def test_a_file_deleted_from_the_vault_is_not_a_fresh_catch_on_rewrite(self):
        """存量清理删掉过那篇 md,之后这一条重新入队导出 —— 它不是新抓到的。

        判据从「磁盘上此刻有没有这个文件」换成「这一条进过 vault 没有」
        (`feed_items.path`)正是为这一刀:按文件在不在判的话,这里会报「+1 条」,
        而那是一篇几天前就抓到的老文章。"""
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


class FetchIsTheExportsOwnResultTests(_Base):
    """问题 2 的另一半:成果归属不受下游阶段(索引、条目化)左右。"""

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
        self.addCleanup(segments.clear_segments)
        segments.clear_segments()

    def _channels(self, *writes, body='正文', boom=False, receipt=True):
        """装一个按契约交回执的假采集器(见 `_Fake`),`_per_channel` 走真的。"""
        fake = _Fake(self, 'rss', writes=writes, body=body, boom=boom,
                     receipt=receipt)
        return patch.multiple(
            pipeline_stages,
            crawl_channels=Mock(return_value=[{'channel': 'rss', 'accounts': []}]),
            active_collector=Mock(return_value=fake))

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
        self.assertEqual(BOOK(0, 0, ''), fetch_rounds.today())

    def test_a_source_that_blew_up_mid_way_does_not_claim_a_certain_zero(self):
        """物化写了一篇然后炸了:它交不出回执,所以这一轮是「不可确认」。

        **不是笃定的 0 条** —— 那一篇真在 vault 里、下一轮索引就会进库,说 0 就是
        在少报。(勾了好几个来源时前面那几个的回执已经落盘,不受这一个影响,
        见 `ReceiptIsTheOnlyEvidenceTests` 那两条。)"""
        with self._channels('source/rss/a.md', boom=True):
            pipeline_stages._run_crawl()
            with self.assertRaises(RuntimeError):
                pipeline_stages._run_export()
        self.assertEqual({'n': 1, 'added': 0}, self._got())
        self.assertTrue(fetch_rounds.today()['partial'])

    def test_a_round_of_item_extraction_is_not_a_catch(self):
        """条目化往衍生树写出一批条目:和原文是同一批内容,日报取材本来就排除
        它们。算进来的话一轮条目化就能让按钮写出三位数,而日报那边一条新的都没有。"""
        with self._channels('source/dr-gzh-projects/t1.md',
                            'source/dr-gzh-projects/t2.md'):
            pipeline_stages._run_crawl()
            pipeline_stages._run_export()
        self.assertEqual({'n': 1, 'added': 0}, self._got())


if __name__ == '__main__':
    unittest.main()
