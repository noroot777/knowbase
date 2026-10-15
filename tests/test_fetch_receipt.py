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
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pathlib import Path

from kb.collectors import wechat_local
from kb import config, embedder, fetch_rounds, pipeline_segments as segments
from kb import pipeline_stages, scanner, store
from kb import pipeline_state as ps
from fetch_rounds_fixture import T, _Base, _Fake


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


class WeChatReceiptTests(_Base):
    """公众号那条通道:子进程打出来的清单怎么变成回执,什么时候只能说不出来。

    这是仓库主人的主力来源(库里八千多篇),所以它那一侧必须自己有红 —— 只钉
    假采集器的话,「超时降级之后清单是个少记的下界」这种只存在于真实现里的
    形态一条都发现不了。
    """

    def _proc(self, stdout, code=0):
        return SimpleNamespace(stdout=stdout, stderr='', returncode=code)

    def _event(self, out, written):
        return json.dumps({'stage': 'export', 'status': 'ok', 'detail': '完成',
                           'no_body': [], 'out': out, 'written': written},
                          ensure_ascii=False)

    def _run(self, proc_or_exc, problem=''):
        col = wechat_local.collector
        run = (Mock(side_effect=proc_or_exc) if isinstance(proc_or_exc, list)
               else Mock(return_value=proc_or_exc))
        with patch.object(wechat_local, '_backend_problem', Mock(return_value=problem)), \
                patch.object(wechat_local, '_run_cli', run):
            return col.materialize()

    def test_the_subprocess_list_becomes_vault_relative_paths(self):
        out = str(self.vault / 'source' / 'rss')          # 本夹具里的在册来源树
        got = self._run(self._proc(self._event(out, ['2026-10-15/某号-标题.md'])))
        self.assertEqual(frozenset({'source/rss/2026-10-15/某号-标题.md'}), got)

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
            fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual({'n': 1, 'added': 0, 'partial': True, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_a_tree_that_is_not_in_the_vault_only_skips_itself(self):
        """**反过来钉一边**:某棵来源树确定不在 vault 底下(配置指到别处)——
        那是「确定不算」,跳过它就行。把这一档也归成「不可确认」的话,存量配置里
        只要有一棵外挂树,每一轮抓取都永久写「条数读不出来」。"""
        outside = config.Source(name='elsewhere', root=self.root / 'outside')
        with patch.object(config, 'SOURCES', self._sources() + [outside]):
            day = T('2026-09-29 09:00')
            fetch_rounds.open_round('batch-a', day, seg='run-1')
            fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
            fetch_rounds.note_end('batch-a', 'run-1')
            self.assertEqual({'n': 1, 'added': 1, 'partial': False, 'at': '09:00'},
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

    def test_the_first_export_reports_the_file_it_created(self):
        self.reg.put_items(self.source, self._entry())
        got = self.fsync.export('rss')
        self.assertEqual(1, len(got))
        self.assertTrue((self.vault / next(iter(got))).exists())

    def test_a_second_export_with_nothing_pending_reports_nothing(self):
        self.reg.put_items(self.source, self._entry())
        self.fsync.export('rss')
        self.assertEqual(frozenset(), self.fsync.export('rss'))

    def test_rewriting_an_old_article_is_not_a_fresh_catch(self):
        """正文补抓/译文回写让同一条重新入队:md 确实又写了一遍,但回执是空的。

        算进去的话同一篇会连着好几天被报成"刚抓到",而用户看着那个数决定要不要
        再花一次模型钱重新生成日报。"""
        self.reg.put_items(self.source, self._entry())
        first = self.fsync.export('rss')
        with self.reg.db() as c:
            c.execute("UPDATE feed_items SET content_hash='changed' WHERE kind='rss'")
        again = self.fsync.export('rss')
        path = self.vault / next(iter(first))
        self.assertEqual(frozenset(), again)
        self.assertTrue(path.exists())          # 文件真又写了一遍,只是不算新抓到

    def test_a_file_that_landed_stays_in_the_receipt_when_the_confirm_fails(self):
        """**第 1 轮审查实测的那一条**:md 已经替换进 vault,随后 `feed_items` 的
        确认(`exported_hash`)失败 —— 这一篇必须留在回执里。

        旧判据是"两道都要卡"(文件落了 + 入库确认也成了),于是确认那一下的异常被
        接成逐项错误、回执照旧返回一个看着**确定**的空集;而重导时它已经不是新
        文件(`fresh=False`),这一篇**永远**补不回账(实测:vault 多了 1 篇,
        药丸永久写「0 条」且自称确定,刷新和重试都纠不回来)。

        反过来钉一边:它已经记过一次账了,确认恢复之后重导不许再算一次。"""
        self.reg.put_items(self.source, self._entry())
        with self.reg.db() as c:
            c.execute("CREATE TRIGGER no_confirm BEFORE UPDATE OF exported_hash "
                      "ON feed_items BEGIN SELECT RAISE(ABORT, '确认失败'); END")
        got = self.fsync.export('rss')
        self.assertEqual(1, len(got))                          # 回执里有它
        self.assertTrue((self.vault / next(iter(got))).exists())  # 文件真在 vault 里
        with self.reg.db() as c:
            self.assertEqual([''], [r[0] for r in c.execute(
                "SELECT exported_hash FROM feed_items WHERE kind='rss'")])
            c.execute('DROP TRIGGER no_confirm')
        self.assertEqual(frozenset(), self.fsync.export('rss'))


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
        self.assertEqual({'n': 0, 'added': 0, 'partial': False, 'at': ''}, fetch_rounds.today())

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
