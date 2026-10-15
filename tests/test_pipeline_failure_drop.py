"""明细里那一条:点得开、删得掉,删完就不再出现。

管道面板上那份失败明细里有一类条目是「这一篇一直缺正文」 —— 它会被按退避时间
一轮一轮地重试,而其中确实有一些原文已经没有内容了。这一族钉的是三件事:

①**真正的删除在采集器那边,出口这一层只管显示**(`kb.pipeline_failures`);
②**顺序不许反**:采集器那一步(不可逆)成了才记账本,反过来会静默丢掉一个待处理项;
③**那几份运行记录一个字节都不动** —— 「两份落盘是同一个业务结果的两个投影」那条
  不变量在 `docs/decisions/pipeline-history.md` 里,撕开它的代价是恢复时两份各记
  着不同的东西。

摆盘沿用 `tests/pipeline_case.HistoryCase`(账本文件从 `_STATE_FILE` 派生,所以
那一处 patch 同时隔离了这一份)。
"""
import fcntl
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import pipeline_failures as pf, pipeline_state as state
from kb.collectors import CollectorError, item_dropper
from kb.web import pipeline_api
from tests.headless import probe_env, requires_headless, run_probe
from tests.pipeline_case import HistoryCase

_PROBE = Path(__file__).resolve().parent / 'pipe_drop_probe.mjs'


def _gone(url, title='缺正文的一篇', aid='wx-1'):
    """一条带身份的明细:面板据此点开原文、也据此把它删掉。"""
    return {'title': title, 'reason': '缺少正文，暂未导出', 'pending': True,
            'url': url, 'drop': {'channel': 'wechat_local', 'id': aid,
                                 'account': '某个号'}}


class FakeCollector:
    """实现了 `ItemDropping` 的采集器替身。"""

    name, label = 'wechat_local', '公众号'

    def __init__(self, fail=''):
        self.fail, self.calls = fail, []

    def drop_item(self, ref):
        self.calls.append(ref)
        if self.fail:
            raise CollectorError(self.fail)
        return f"已移除,以后不再采集:{ref['id']}"


class PlainCollector:
    """没有这个能力的采集器:面板上那颗按钮压根不该出现。"""

    name, label = 'rss', 'RSS'


class DropLedgerTests(HistoryCase):
    def test_a_dropped_item_disappears_from_every_view_but_the_records_are_untouched(self):
        """删掉之后:状态、last_run、历史清单里都不再有它;**磁盘上那两份仍然有**。

        过滤待在出口一处,是因为「最新那一轮」在内存 `_state`、整批汇总和两份落盘
        里各有一份投影 —— 挨个做外科手术既漏得掉,又会让两份落盘不再是同一个业务
        结果的两个投影。
        """
        self.run_round('run-1', 'ok', '完成', failures=[
            _gone('https://mp.weixin.qq.com/s?x=1'),
            {'title': '另一条', 'reason': '翻译失败'}])
        pf.remember('wechat_local', 'wx-1')

        latest = state.last_result()
        self.assertEqual(2, len(latest['stages']['export']['failures']),
                         '内存里那一轮不许被改')
        disk = state.load_last_run()['stages']['export']['failures']
        self.assertEqual(2, len(disk), '磁盘状态文件不许被改')
        hist = state.load_history()[0]['stages']['export']['failures']
        self.assertEqual(2, len(hist), '历史文件不许被改')

        shown = state.load_last_run()
        pf.strip(shown)
        self.assertEqual(['另一条'],
                         [f['title'] for f in shown['stages']['export']['failures']],
                         '没有身份的那一条照旧留着(反过来钉一边)')

    def test_both_the_running_round_and_last_run_are_filtered(self):
        """**两处都要过。** 空闲时面板渲染的是 `last_run`,跑着的时候渲染的是顶层
        —— 漏掉哪一处,用户都会在某个时刻看见刚删掉的那几条又回来了。
        """
        self.run_round('run-1', 'ok', '完成', failures=[_gone('https://x/1')])
        pf.remember('wechat_local', 'wx-1')
        payload = {'stages': state.get_state()['stages'],
                   'last_run': state.last_result()}
        pf.strip_state(payload)
        self.assertEqual([], payload['stages']['export']['failures'])
        self.assertEqual([], payload['last_run']['stages']['export']['failures'])

    def test_the_total_follows_the_list_down(self):
        """被截断过的那一轮:总数要跟着减,**而且照旧比清单长**。

        面板照 `failures_total || failures.length` 念「明细 · N 项(仅列前 M 条)」。
        只删数组不改总数的话那个 N 一直不动,用户删了十条、标题还写着原来那个数;
        反过来把它压到清单长度以下,余下那几百条就连「还有」都不知道了
        (`_cap_failures` 留下这个字段正是为了这件事)。
        """
        entry = {'status': 'ok', 'detail': '完成',
                 'failures': [_gone('https://x/1'), _gone('https://x/2', aid='wx-2')],
                 'failures_total': 120}
        run = {'stages': {'export': entry}}
        pf.remember('wechat_local', 'wx-1')
        self.assertEqual(1, pf.strip(run))
        self.assertEqual(119, entry['failures_total'])
        pf.remember('wechat_local', 'wx-2')
        pf.strip(run)
        self.assertEqual(([], 118), (entry['failures'], entry['failures_total']),
                         '清单空了,可那一轮确实还有 118 条没列出来')

        # 反过来钉一边:没被截断过的那一轮压根没有这个字段,剔完也不许凭空长出来
        # —— 长出来的话面板会念「明细 · 1 项」而清单是空的。
        plain = {'status': 'ok', 'failures': [_gone('https://x/3', aid='wx-3')]}
        pf.remember('wechat_local', 'wx-3')
        pf.strip({'stages': {'export': plain}})
        self.assertEqual([], plain['failures'])
        self.assertNotIn('failures_total', plain)

    def test_an_unreadable_ledger_shows_the_items_again_rather_than_hiding_the_panel(self):
        """账本读不出来 = 那几条重新显示,**不是整份明细不显示**。

        重新显示只是多看见几行(采集那边早就不抓它们了,再点一次也只是重复一次
        幂等的删除);反过来把明细藏掉才是真的丢信息。
        """
        pf.remember('wechat_local', 'wx-1')
        pf._file().write_bytes(b'{ not json')
        self.assertEqual(set(), pf.dropped_keys())
        run = {'stages': {'export': {'failures': [_gone('https://x/1')]}}}
        self.assertEqual(0, pf.strip(run))
        self.assertEqual(1, len(run['stages']['export']['failures']))

    def test_the_ledger_is_capped_by_insertion_order(self):
        """封顶掉的是**最老的**那几条 —— 它们对应的轮次早被历史的上限挤出去了。"""
        with patch.object(pf, '_KEEP', 3):
            for n in range(5):
                pf.remember('wechat_local', f'wx-{n}')
        self.assertEqual({'wechat_local:wx-2', 'wechat_local:wx-3',
                          'wechat_local:wx-4'}, pf.dropped_keys())

    def test_two_concurrent_removals_both_stay_recorded(self):
        """两条请求同时记账,**两条都要留下来**。

        kb-web 是线程池,两个标签页各点一次完全正常。没有互斥的话两条都读到删除
        之前的旧列表、各自 append 一条、再整份替换 —— 最后落盘的只有后写的那一
        条,另一条已经不可逆地删掉了内容,却**再也不会被隐藏**,而两个响应都是
        `ok=true`(第 1 轮审查实测:账本里只剩 `wx-second`)。
        `_write_atomic` 只保证单次写入不是半截 JSON,保证不了这件事。

        竞态窗口靠「读完之后歇一下」摆出来:不加锁时两条必定都读到空账本。
        """
        real = pf._load_json

        def slow(path):
            got = real(path)
            time.sleep(0.05)
            return got

        done, started = [], threading.Barrier(2)

        def one(item_id):
            started.wait(5)
            pf.remember('wechat_local', item_id)
            done.append(item_id)

        with patch.object(pf, '_load_json', slow):
            threads = [threading.Thread(target=one, args=(i,))
                       for i in ('wx-first', 'wx-second')]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual({'wx-first', 'wx-second'}, set(done), '两条都该返回成功')
        self.assertEqual({'wechat_local:wx-first', 'wechat_local:wx-second'},
                         pf.dropped_keys())

    def test_a_held_lock_makes_the_ledger_write_fail_loudly(self):
        """锁被别的进程攥着:**抛出来,不许静默跳过**。

        调用方(端点)据此在文案里说一句「面板记录没写成」—— 不可逆的那一步已经
        成了,报红会让用户以为没删掉。反过来静默返回成功的话,账本里压根没有这
        一条,而面板显示得像删掉了,下一轮同步它又回来了。
        flock 认的是 open file description,所以同进程换一个 fd 就挡得住自己 ——
        这也正是为什么一把 flock 同时管住了线程池里那两条请求,不用再叠一把
        `threading.Lock`。
        """
        other = pf._lock_file().open('a+')
        self.addCleanup(other.close)
        fcntl.flock(other.fileno(), fcntl.LOCK_EX)
        with patch.object(pf, '_WAIT', 0.2), self.assertRaises(TimeoutError):
            pf.remember('wechat_local', 'wx-1')
        fcntl.flock(other.fileno(), fcntl.LOCK_UN)
        self.assertEqual(set(), pf.dropped_keys(), '没写成就是没写成')
        pf.remember('wechat_local', 'wx-1')
        self.assertEqual({'wechat_local:wx-1'}, pf.dropped_keys(), '让开之后照旧写得进')

    def test_the_lock_file_travels_with_the_ledger(self):
        """锁文件从账本那个路径派生 —— 测试隔离落盘时它自动跟着搬走。

        另立一个模块变量的话,每个走到这里的测试都得记着再 patch 一次,而漏掉的
        那个会去锁**生产**数据目录里的文件,漏没漏在测试输出上一个字都看不见。
        """
        self.assertEqual(pf._file().parent, pf._lock_file().parent)
        self.assertNotEqual(pf._file(), pf._lock_file())


class DropEndpointTests(HistoryCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(pipeline_api.router)
        self.client = TestClient(app)
        self.fake = FakeCollector()
        self.patch = patch.object(pipeline_api, 'active_collector',
                                  side_effect=lambda name=None: self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def drop(self, **kw):
        body = {'channel': 'wechat_local', 'id': 'wx-1', 'account': '某个号', **kw}
        return self.client.post('/api/pipeline/failures/drop', json=body)

    def test_the_collector_runs_first_and_a_refusal_leaves_the_item_in_place(self):
        """采集器拒了就什么都没发生:账本不许写,那一条还在明细里。

        顺序反过来的后果是静默的 —— 面板上这条消失了,而它还在被一轮轮重试。
        """
        self.fake.fail = '采集器还没装好'
        r = self.drop()
        self.assertEqual(400, r.status_code)
        self.assertIn('采集器还没装好', r.json()['detail'])
        self.assertEqual(set(), pf.dropped_keys(), '拒了就不许记账')

    def test_a_successful_drop_is_recorded_and_repeatable(self):
        self.assertEqual(200, self.drop().status_code)
        self.assertEqual({'wechat_local:wx-1'}, pf.dropped_keys())
        self.assertEqual(200, self.drop().status_code, '连点两次照样成功')
        self.assertEqual({'wechat_local:wx-1'}, pf.dropped_keys())
        self.assertEqual([{'id': 'wx-1', 'account': '某个号', 'link': ''}] * 2,
                         self.fake.calls)

    def test_the_link_reaches_the_collector(self):
        """`link` 要原样送到采集器手上。

        库里已经没有那一行时,档案 ID 算不出链接身份,而永久排除要记的正是那个
        身份 —— 少了这个键,采集器只能拒掉这次移除(见 `cli._drop_missing`)。
        """
        link = 'https://mp.weixin.qq.com/s?__biz=b&mid=1&idx=1'
        self.assertEqual(200, self.drop(link=link).status_code)
        self.assertEqual([{'id': 'wx-1', 'account': '某个号', 'link': link}],
                         self.fake.calls)

    def test_a_ledger_write_failure_is_not_reported_as_a_failed_removal(self):
        """账本写不上**不许报红**:不可逆的那一步已经成了。

        报红会让用户以为没删掉而反复重试。如实在文案里说一句,那几条下一轮同步
        之后自然不再出现。
        """
        with patch.object(pf, '_write_atomic', side_effect=PermissionError('ro')):
            r = self.drop()
        self.assertEqual(200, r.status_code, r.text)
        self.assertIn('PermissionError', r.json()['detail'])
        self.assertIn('不再采集', r.json()['detail'], '成功那句话照旧要在')

    def test_a_collector_without_the_capability_is_refused_before_anything_happens(self):
        self.patch.stop()
        with patch.object(pipeline_api, 'active_collector',
                          side_effect=lambda name=None: PlainCollector()):
            r = self.drop(channel='rss')
        self.patch.start()
        self.assertEqual(400, r.status_code)
        self.assertIn('不支持', r.json()['detail'])
        self.assertEqual(set(), pf.dropped_keys())

    def test_an_item_without_an_identity_is_refused(self):
        r = self.drop(id='   ')
        self.assertEqual(400, r.status_code)
        self.assertEqual([], self.fake.calls, '没身份时压根不该碰采集器')

    def test_both_endpoints_stop_handing_the_item_over(self):
        """状态端点和历史清单**同一个口径**:处理掉的那条在哪一轮里都不再出现。"""
        self.run_round('run-1', 'ok', '完成', failures=[
            _gone('https://x/1'), {'title': '另一条', 'reason': '翻译失败'}])
        self.assertEqual(200, self.drop().status_code)

        got = self.client.get('/api/pipeline/status?source=false').json()
        self.assertEqual(['另一条'],
                         [f['title'] for f in got['last_run']['stages']['export']['failures']])
        runs = self.client.get('/api/pipeline/runs').json()['runs']
        self.assertEqual(['另一条'],
                         [f['title'] for f in runs[0]['stages']['export']['failures']])


class CollectorContractTests(HistoryCase):
    def test_the_wechat_collector_only_offers_the_two_actions_when_it_has_an_identity(self):
        """**没有身份就不给 url / drop。** 指着另一篇文章让人删是不可恢复的。

        旧版 CLI(以及它读不到结构化报告时的降级路径)给的是一个 `账号 · 标题`
        字符串 —— 那一条仍然要列得出来,只是不可点、不可删。
        """
        from kb.collectors.wechat_local import collector

        rich = collector._no_body_failure({
            'name': '某个号 · 标题', 'id': 'wx-9', 'account': '某个号',
            'title': '标题', 'link': 'https://mp.weixin.qq.com/s?x=9'})
        self.assertEqual('https://mp.weixin.qq.com/s?x=9', rich['url'])
        self.assertEqual({'channel': 'wechat_local', 'id': 'wx-9',
                          'account': '某个号',
                          'link': 'https://mp.weixin.qq.com/s?x=9'}, rich['drop'],
                         'link 也要带上:缺行时只有它算得出身份')

        # 降级那一档给的是**带空身份的字典**(CLI 读不到结构化报告时),更旧的
        # CLI 给的是一个裸字符串 —— 两种都要列得出来、两种都不许长出 url / drop。
        for plain in (collector._no_body_failure(
                          {'name': '某个号 · 标题', 'id': '', 'account': '',
                           'title': '', 'link': ''}),
                      collector._no_body_failure('某个号 · 标题')):
            self.assertEqual('某个号 · 标题', plain['title'])
            self.assertNotIn('url', plain)
            self.assertNotIn('drop', plain)
            self.assertTrue(plain['pending'], '照旧是「待补」而不是红')

        self.assertIsNotNone(item_dropper(collector),
                             '公众号采集器要声明得出这个能力')

    def test_the_cli_call_carries_the_account_and_the_link(self):
        """`drop_item` 要把 `account` 和 `link` 都交到 CLI 手上。

        `account` 是归属核对(删错的是另一个号的文章,不可恢复);`link` 是库里
        已经没有那一行时唯一算得出身份的东西 —— 少了它那边只能拒掉这次移除
        (见 `collector/cli.py` 的 `_drop_missing`)。给不出链接时**不许拼一个空的
        `--link`**:那等于让下游拿空串去算身份。
        """
        from kb.collectors import wechat_local as wl

        seen = []

        def fake(*argv, timeout):
            seen.append(argv)
            return SimpleNamespace(returncode=0, stdout='{"ok": true, "detail": "已移除"}',
                                   stderr='')

        with patch.object(wl, '_backend_problem', return_value=''), \
             patch.object(wl, '_run_cli', fake):
            wl.collector.drop_item({'id': 'wx-1', 'account': '某个号',
                                    'link': 'https://mp.weixin.qq.com/s?x=1'})
            wl.collector.drop_item({'id': 'wx-2'})
        self.assertEqual(('articles', 'drop', '--id', 'wx-1', '--account', '某个号',
                          '--link', 'https://mp.weixin.qq.com/s?x=1', '--json'),
                         seen[0])
        self.assertEqual(('articles', 'drop', '--id', 'wx-2', '--json'), seen[1],
                         '给不出的那两样一个都不许拼成空值')


@requires_headless
class PanelTests(HistoryCase):
    """面板上那两件事:标题点得开、按钮删得掉(真浏览器、真鼠标)。

    探针不许自己 spawn 浏览器、测试不许裸 `subprocess.run` 跑探针
    (见 `docs/decisions/probe-shell.md`)。
    """

    def test_a_body_less_item_can_be_opened_and_dropped_on_the_real_panel(self):
        r = run_probe([shutil.which('node'), str(_PROBE)],
                      timeout=300, env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
