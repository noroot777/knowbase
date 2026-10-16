# -*- coding: utf-8 -*-
"""抓取成果的**归属**:哪几篇算这一次抓到的。

「抓了多少」一度是拿旁观者的视角算出来的,两版各错一次:先是索引阶段数"库里头
一回出现这个路径"的文档数(空抓取碰上 3 篇历史未索引的旧文报成「+3 条」,反过来
真抓到 2 篇而这一轮不索引则报 0、之后单独索引也补不回来),再是物化前后取全 vault
路径差集(物化那段时间里别的进程落下的文件被算进这一轮)。差集分不出"谁写的"。

所以现在只认生产者自己交回来的回执(`SourceCollector.materialize()` 的返回值)。
这一组钉的就是它:回执说了算、回执说不出来就报「不可确认」、回执里不算新内容的
路径由账本按取材范围筛掉,以及成果归属不受下游阶段(索引、条目化)左右。

回执里那一条**属于哪一次业务抓取**(新抓到 / 补导 / 说不清)在
tests/test_fetch_identity.py;账本本体(几次、几条、故障时说什么)和它的恢复协议
在 tests/test_fetch_rounds.py;前端那几种说法在 tests/daily_probe.mjs。
"""
import json
import sqlite3
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pathlib import Path

from kb.collectors import Fetched, wechat_local
from kb import config, embedder, fetch_rounds, pipeline_segments as segments
from kb import pipeline_stages, scanner, store
from kb import pipeline_state as ps
from tests.fetch_rounds_fixture import BOOK, T, _Base, _Fake


class ReceiptIsTheOnlyEvidenceTests(_Base):
    """**这一组是问题 2 的要害**:只认生产者交回来的回执,不拿旁观者的差集猜。"""

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

    def test_a_run_that_vanished_before_the_stage_finished_is_a_lower_bound(self):
        """真实 `_run_export` 跑到一半进程没了(这里在逐来源循环之后定点抛
        `KeyboardInterrupt`,等价于第 1 轮审查用的 `os._exit(23)`):vault 里真多
        了 1 篇,账本只能说「至少 +1 条」。

        旧实现这里报 `{'n':1,'added':0,'partial':False}` —— 进门那张空回执已经把
        「不可确认」解除了,所以是个**确定**的 0 条。"""
        fake = _Fake(self, 'rss', writes=['source/rss/a.md'])
        real = pipeline_stages._per_channel

        def _die(stage, flt, fn):
            real(stage, flt, fn)
            if stage == 'export':
                raise KeyboardInterrupt('进程没了')

        with patch.object(pipeline_stages, 'crawl_channels',
                          Mock(return_value=[{'channel': 'rss', 'accounts': []}])), \
                patch.object(pipeline_stages, 'active_collector',
                             Mock(return_value=fake)), \
                patch.object(pipeline_stages, '_per_channel', _die):
            pipeline_stages._run_crawl()
            with self.assertRaises(KeyboardInterrupt):
                pipeline_stages._run_export()
        self.assertTrue((self.vault / 'source/rss/a.md').exists())
        self.assertEqual({'n': 1, 'added': 1, 'partial': True}, self._got())

    def test_a_second_sub_round_that_vanished_is_a_lower_bound(self):
        """**第 2 轮审查实测的那一条**,整条管道走真的:一轮日报里每个来源各跑
        一次 `FETCH_STAGES`、共用一个业务批次身份,所以这里是**同一个 key 的两个
        子轮**(各有自己的 `run_id`)。第一个子轮正常收尾,第二个子轮写出第二篇
        之后进程没了 —— 两篇都在 vault 里,账本只能说「至少 +2 条」。

        旧实现这里报 `{'n':1,'added':2,'partial':False}`:第一个子轮那条结束行
        冒充了整批交完。注意 `added` 这时**正好**等于真实值,所以只看数字看不出
        问题 —— 判据必须是"这个数确定不确定",用户照着它决定要不要再花一次模型钱。
        """
        self._run(_Fake(self, 'rss', writes=['source/rss/a.md']))
        self.assertEqual({'n': 1, 'added': 1, 'partial': False}, self._got())
        real = pipeline_stages._per_channel

        def _die(stage, flt, fn):
            real(stage, flt, fn)
            if stage == 'export':
                raise KeyboardInterrupt('第二个子轮的进程没了')

        ps._state['run_id'] = 'run-probe-2'          # 第二个来源那次运行
        with patch.object(pipeline_stages, '_per_channel', _die),                 self.assertRaises(KeyboardInterrupt):
            self._run(_Fake(self, 'rss', writes=['source/rss/b.md']))
        self.assertTrue((self.vault / 'source/rss/b.md').exists())
        self.assertEqual({'n': 1, 'added': 2, 'partial': True}, self._got())

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
        fetch_rounds.note_end('batch-dup')        # 物化这一段明确走完了
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
        # 结束行照旧落下:这一轮的 `partial` 只可能来自那一条读不懂的行
        fetch_rounds.note_end('batch-torn')
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
        fetch_rounds.note_end('batch-nopath')     # 同上:缺口是唯一的来源
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
        fetch_rounds.note_end('batch-scope')
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
        fetch_rounds.note_end('batch-legacy')
        self.assertEqual(BOOK(1, 2, '09:00', fresh=0, unsure=2),
                         fetch_rounds.today(day))


class WeChatReceiptTests(_Base):
    """公众号那条通道:子进程打出来的清单怎么变成回执,什么时候只能说不出来。

    这是仓库主人的主力来源(库里绝大多数文章),所以它那一侧必须自己有红 —— 只钉
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

        这是「补导」在公众号那条通道上的真实形态:身份只在**文章首次插入那个
        事务**里写一次(`upsert_article_from_blog`),补正文那一轮压根不碰它。
        记成这一轮的话药丸会把它算成「这一次新抓到的」,而远端那天压根没发新文章。"""
        out = str(self.vault / 'source' / 'rss')
        got = self._run(self._proc(self._event(
            out, ['2026-10-13/旧文.md'], ids={'2026-10-13/旧文.md': '1001'},
            rounds={'2026-10-13/旧文.md': 'batch-older'})))
        self.assertEqual('batch-older', next(iter(got)).fetch_id)

    def test_a_stock_article_without_a_round_is_unsure_not_someone_elses(self):
        """存量那一大批没有抓取身份:交空串「说不清」,不许编一个。

        编成这一轮 = 把补导的算成新抓到的;编成"别的轮次" = 把不知道写成确定
        不是本次。两种都是凭空造事实,所以空串原样交上去,由账本归到 unsure。"""
        out = str(self.vault / 'source' / 'rss')
        got = self._run(self._proc(self._event(
            out, ['2026-01-01/存量.md'], rounds={'2026-01-01/存量.md': ''})))
        self.assertEqual('', next(iter(got)).fetch_id)

    def test_a_row_with_no_file_name_voids_the_whole_list(self):
        """清单里有一行答不出文件名:**整份不可确认,不挑着收**。

        挑着收交上去的是一份少记的确定清单(「这一次抓了 N 篇」),而少记是静默
        丢数据:那几篇再也不会被算进任何一轮。报「不可确认」顶多让人多同步一次。
        `cmd_export` 那一端对空名字本来就是整份作废,两端得同一个口径 —— 不然
        中间这一道就成了把缺口洗成确定零条的地方。空白字符同样算答不出来。
        """
        out = str(self.vault / 'source' / 'rss')
        for bad in ('', '   ', None):
            raw = json.loads(self._event(out, ['a.md', 'b.md']))
            raw['items'][1]['name'] = bad
            with self.subTest(name=repr(bad)):
                self.assertIsNone(self._run(self._proc(json.dumps(
                    raw, ensure_ascii=False))))

    def test_a_field_that_is_not_text_voids_the_whole_list(self):
        """清单里某个字段不是字符串:**整份不可确认**,不许 `str()` 成一个身份。

        `str({'not': 'an identity'})` 是 `"{'not': 'an identity'}"` —— 非空,于是
        那一条被当成「确定不是这一轮抓到的」归进 carried(审查第 2 轮实测到的就是
        这个数:`carried=1, unsure=0, partial=false`)。把一行坏数据读成确定答案,
        比报不可确认坏得多。
        """
        out = str(self.vault / 'source' / 'rss')
        for key, bad in (('round', {'not': 'an identity'}), ('round', 7),
                         ('id', ['a']), ('name', 3)):
            raw = json.loads(self._event(out, ['a.md', 'b.md']))
            raw['items'][1][key] = bad
            with self.subTest(field=key, value=repr(bad)):
                self.assertIsNone(self._run(self._proc(json.dumps(
                    raw, ensure_ascii=False))))

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
        ps._state.update({'running': True,
                          'batch': 'b', 'run_id': 'r', 'planned_stages': None,
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

    def test_a_scope_that_cannot_be_read_is_not_an_empty_scope(self):
        """**第 2 轮审查那条静态项**:算范围时 `resolve()` 抛 `OSError`(权限、
        符号链接层数、目录临时不可用)—— 那是「这条路径算不算成果」**答不出来**,
        不是「确定不算」。旧口径把两者压成一种(跳过那棵树 / 返回空范围),于是
        一份真回执被筛成空集,再让结束行把这一轮定成**确定的 0 条**。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        real = Path.resolve

        def resolve(path, *a, **kw):
            if path == self.vault / 'source' / 'rss':
                raise OSError('这棵树暂时读不到')
            return real(path, *a, **kw)

        with patch.object(Path, 'resolve', resolve):
            fetch_rounds.note_new('batch-a', self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual(BOOK(1, 0, '09:00', True),
                         fetch_rounds.today(day))

    def test_a_symlink_loop_in_any_source_is_only_an_unreadable_scope(self):
        """**审查实测的 N5**:循环符号链接让 `resolve()` 抛的是 `RuntimeError`,
        不是 `OSError`。只接后者的话,这个异常会从 `note_new` 一路冒到导出阶段进门
        那张空回执登记上(`pipeline_stages._run_export` 的第一句),把整次导出连同
        其他正常来源一起带崩 —— 而出事的那棵树压根不是这一轮要导的那一个。

        用真的循环链接(指向自己),不打桩:这一条钉的就是「这一档到底抛什么」。
        后果和上一条同一句:**答不出来**归「不可确认」,不许筛成确定的 0 条。"""
        loop = self.vault / 'source' / 'dr-youtube-dig'
        loop.parent.mkdir(parents=True, exist_ok=True)
        loop.symlink_to(loop)
        with self.assertRaises(RuntimeError):            # 前提:这一档真抛它
            loop.resolve()
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        fetch_rounds.note_new('batch-a', ())             # 导出进门那张空回执
        fetch_rounds.note_new('batch-a', self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual(BOOK(1, 0, '09:00', True),
                         fetch_rounds.today(day))
        # **两处一起收口**,不是按某一个异常实例修一个调用点:vault 根自己是那个
        # 循环时(整棵 vault 挂在一条坏链接上)判据也只有「范围说不清」。
        with patch.object(config, 'VAULT_ROOT', loop):
            self.assertIsNone(fetch_rounds._scope_prefixes())

    def test_a_tree_that_is_not_in_the_vault_only_skips_itself(self):
        """**反过来钉一边**:某棵来源树确定不在 vault 底下(配置指到别处)——
        那是「确定不算」,跳过它就行。把这一档也归成「不可确认」的话,存量配置里
        只要有一棵外挂树,每一轮抓取都永久写「条数读不出来」。"""
        outside = config.Source(name='elsewhere', root=self.root / 'outside')
        with patch.object(config, 'SOURCES', self._sources() + [outside]):
            day = T('2026-09-29 09:00')
            fetch_rounds.open_round('batch-a', day, seg='run-1')
            fetch_rounds.note_new('batch-a',
                                  self.caught('batch-a', 'source/rss/a.md'))
            fetch_rounds.note_end('batch-a', 'run-1')
            self.assertEqual(BOOK(1, 1, '09:00'),
                             fetch_rounds.today(day))

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
        # `running` 要一起装上:**光留着 `batch` 不等于"这一轮在跑"** —— 收尾时
        # 那两个字段是故意留下的,所以 `round_key()` 认 `running`(见它的说明)。
        ps._state.update({'running': True,
                          'batch': 'batch-probe', 'run_id': 'run-probe',
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
