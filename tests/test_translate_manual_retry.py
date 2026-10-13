# -*- coding: utf-8 -*-
"""来源面板上「重新翻译」那颗按钮的浏览器回归 + 端点那一侧的口径。

它解除的是一个原来**只能靠手改 feeds.db 才解除得了**的终态:试满
`translate.MAX_TRIES` 的条目按设计离开翻译队列、按原文导出,而 `queued_sql` 的
`translate_tries<MAX_TRIES` 让面板上的「同步」和「重试失败项」**都选不到它们**
—— 中转站一轮 503 就能造出上百条(2026-10-13 实测 rss 42 条 + social 66 条),
那批英文已经被索引、被打了标签,面板上却一个字都不提它们存在。

页面那一侧由 tests/translate_manual_retry_probe.mjs 真跑一遍(自带 mock 后端,CDP 直连
chrome-headless-shell,不装 playwright、不连网、始终无头、临时 profile);
队列口径那一族(`QueueTests`)也在本文件里,脚手架借
`test_feed_translate.FeedCase`(临时 registry + 临时 vault + 固定模型)——
import 进来的 `FeedCase` 自己不带 test 方法,被 loader 收走也是空跑。
**付费边界那一族(射程收窄、点名标记的寿命、补抓和翻译在同一轮里先后跑)在
tests/test_translate_manual_scope.py** —— 分家只因为 700 行上限,摆盘共用本文件
那个 `ManualRetryCase`。
**别和 tests/test_translate_retry.py 搞混** —— 那一份钉的是自动重试那条路
(「失败过的优先」取数顺序 + 导出阶段把本轮失败和此前失败分开报),这一份钉的是
**用户手动点那颗按钮**那条路。
这里钉住**端点自己的几道闸** —— 它们各自对应一种「点下去什么也不会发生,而且
不报错」的形态。

跑:  python -m unittest tests.test_translate_manual_retry -v
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import llm  # noqa: E402
from kb.feeds import registry as reg, translate  # noqa: E402
from tests.headless import probe_env, requires_headless, run_probe  # noqa: E402
from tests.test_feed_translate import FeedCase  # noqa: E402

_PROBE = Path(__file__).resolve().parent / "translate_manual_retry_probe.mjs"


def _stuck(failed, shipped, queued=0, reason='', exhausted=None, sig='sig-now'):
    """`translate.stuck` 的回包形状。端点只读它,不写库 —— 见 `translate_retry`。

    `exhausted` 默认跟 `shipped` 一致(那是常态:试满额度的下一步就是按原文导出),
    两者分开摆是为了钉住「说的数字和动的那批对不对得上」。
    """
    return {'failed': failed, 'shipped': shipped, 'queued': queued, 'reason': reason,
            'exhausted': shipped if exhausted is None else exhausted, 'sig': sig}


class EndpointTests(unittest.TestCase):
    """端点那几道闸:每一道都对应「点了没反应又不报错」的一种形态。"""

    def setUp(self):
        from fastapi.testclient import TestClient

        from kb.server import app
        self.client = TestClient(app)

    def test_without_a_model_the_request_is_refused_with_the_next_step(self):
        """没配 key 时 `translate_pending` 整个跳过、一次额度都不计。

        放过去的话:额度被清零(写进了库)、管道跑一轮、一个字也没翻,而面板上
        是一句「已开始重新翻译」。拒掉并说清去哪儿配才是这一档该有的样子。
        """
        from kb.feeds import translate
        with patch.object(translate, 'available', lambda: False):
            r = self.client.post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(400, r.status_code)
        self.assertIn('模型', r.json()['detail'])

    def test_an_unknown_subscription_is_refused(self):
        """射程是「这条订阅」时,那条订阅得真的存在 —— 否则 requeue 会静默
        命中 0 行,返回一句「已开始」而什么都没放回。"""
        from kb.feeds import registry as reg
        from kb.feeds import translate
        # 订阅清单打桩:这一条验的是「id 不在清单里就拒」,不该顺带去开生产库
        with patch.object(translate, 'available', lambda: True), \
             patch.object(reg, 'subscriptions', lambda kind: [{'id': 7}]):
            r = self.client.post('/api/intake/rss/translate-retry',
                                 json={'source_id': 9999999})
        self.assertEqual(404, r.status_code)

    def test_an_unknown_kind_is_refused(self):
        r = self.client.post('/api/intake/not-a-kind/translate-retry', json={})
        self.assertEqual(404, r.status_code)

    def test_the_pipeline_plan_never_includes_crawl(self):
        """**不许重新采集。** 要重译的内容早就在库里了,重跑 crawl 既慢、又可能
        因为源站退避什么都拿不到,而翻译挂在 export 阶段上(见
        `kb/collectors/feeds.py` 的 `materialize`)。

        判据落在**真传给 `start_pipeline` 的那份计划**上,不是读一遍源码:
        计划里混进 crawl 的话,用户点「重新翻译」会触发一轮完整采集,而页面上
        的提示只说了翻译。
        """
        from kb import pipeline
        from kb.feeds import translate
        seen = {}

        def fake_start(stages=None, crawl_filter=None):
            seen['stages'] = list(stages or [])
            seen['filter'] = dict(crawl_filter or {})
            return 'run-test'

        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck', lambda *a, **k: _stuck(5, 0, queued=7)), \
             patch.object(pipeline, 'start_pipeline', fake_start):
            r = self.client.post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(200, r.status_code, r.text)
        self.assertNotIn('crawl', seen['stages'])
        # 译文落盘之后那几篇 md 换了内容:不重建索引的话检索到的还是英文那一份
        self.assertEqual(['export', 'index', 'extract'], seen['stages'])
        self.assertEqual('rss', seen['filter'].get('channel'))

    def test_an_empty_queue_is_refused_instead_of_starting_a_no_op_run(self):
        """队列空了就别起管道:那一轮什么也不翻,而提示说的是「已开始重新翻译」。

        判据是「现在队列里有没有东西」而不是「放回了几条」—— 正常积压(一轮只译
        MAX_ITEMS 条,剩下的额度一次没动过)同样该被这颗按钮消化,而它们一条都
        不该被 `requeue_failed` 碰到。
        """
        from kb import pipeline
        from kb.feeds import translate
        started = []
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck', lambda *a, **k: _stuck(0, 0)), \
             patch.object(pipeline, 'start_pipeline',
                          lambda *a, **k: started.append(1) or 'run-x'):
            r = self.client.post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(400, r.status_code)
        self.assertEqual([], started)

    def test_an_exhausted_only_scope_is_not_refused(self):
        """**反过来钉一边:队列是空的、但有试满额度的,绝不许拒。**

        那恰恰是这颗按钮唯一救得回来的形态 —— `queued` 按定义选不到它们
        (`translate_tries<MAX_TRIES`),拒掉就等于把入口关上,而面板上那句话还
        写着「108 条翻译失败」。

        判据也不许写成「没失败过就拒」:一条都没失败、只是还没轮到的积压是另一
        档,`test_an_ordinary_backlog_can_be_started_by_hand` 钉它。
        """
        from kb import pipeline
        from kb.feeds import translate
        started = []
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck',
                          lambda *a, **k: _stuck(108, 66, queued=0)), \
             patch.object(pipeline, 'start_pipeline',
                          lambda *a, **k: started.append(1) or 'run-e'):
            r = self.client.post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual([1], started)
        self.assertEqual(66, r.json()['pending'])

    def test_an_ordinary_backlog_is_not_refused_either(self):
        """反向钉另一边:**一条都没失败过的积压同样不许拒。**

        定时同步默认是关的,一轮又只译 `MAX_ITEMS` 条 —— 这批内容在系统里没有
        别的办法动起来,而原来面板上那句「还在等翻译」读完无事可做(用户原话:
        "脱离了 ai 我都没法重新翻译他们")。

        这一档和上一条用例互为反边:判据写成「没失败过就拒」时上一条绿、这一条
        红;写成「队列空了就拒」时正好相反。两条都在才钉得住那个 `or`
        (第一版只有上一条,注入「没失败过就拒」红 0 条)。
        """
        from kb import pipeline
        from kb.feeds import translate
        started = []
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck', lambda *a, **k: _stuck(0, 0, queued=18)), \
             patch.object(pipeline, 'start_pipeline',
                          lambda *a, **k: started.append(1) or 'run-p'):
            r = self.client.post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual([1], started)
        self.assertEqual(18, r.json()['pending'])

    def test_a_queue_longer_than_one_round_says_so(self):
        """一轮上限是 `translate.MAX_ITEMS`,队列比它长时要如实说清只译得完一截。

        不说的话用户点一次、看到数字只掉了 40,会以为按钮漏了东西 —— 而那正是
        这颗按钮最常见的使用场景(积压几百条)。
        """
        from kb import pipeline
        from kb.feeds import translate
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck',
                          lambda *a, **k: _stuck(108, 60, exhausted=66, queued=180)), \
             patch.object(pipeline, 'start_pipeline', lambda *a, **k: 'run-y'):
            detail = self.client.post('/api/intake/rss/translate-retry',
                                      json={}).json()['detail']
        # 说的总量是「这个范围里还没译的」= 还排着队的 180 + 已经离开队列的 66,
        # **不是「失败了几条」(108)** —— 后者只是其中一段,照它说就把这一次要花
        # 的钱说小了(第 1 轮审查:按钮射程比提示里那个数字宽)。
        # **也不是「真落了盘的」(60)**:没落盘的那 6 条照样会被放回队列,
        # `shipped` 和 `exhausted` 故意摆成不同的数就是为了钉住这一处用对了哪个
        # (见 `translate.stuck`;两个一样的话这条注入红 0 条)。
        self.assertIn('246', detail)
        self.assertNotIn('240', detail)
        self.assertIn(str(translate.MAX_ITEMS), detail)
        self.assertIn('下一轮', detail)

    def test_a_short_queue_does_not_promise_a_second_round(self):
        """反过来钉一边:一轮译得完的时候不许说「其余留到下一轮」。"""
        from kb import pipeline
        from kb.feeds import translate
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck', lambda *a, **k: _stuck(3, 0, queued=3)), \
             patch.object(pipeline, 'start_pipeline', lambda *a, **k: 'run-z'):
            detail = self.client.post('/api/intake/rss/translate-retry',
                                      json={}).json()['detail']
        self.assertNotIn('下一轮', detail)

    def test_a_stale_confirmation_is_refused_before_anything_starts(self):
        """**确认期间这个范围里的内容变了 —— 连管道都不占。**

        范围(来源 + 哪条订阅)由前端在打开确认框之前定死,可**范围里的那批内容**
        还会变:判据里有订阅自己的「补抓原文正文」开关,确认框开着的时候把它关掉,
        等正文的那几条当场变成可译的(2026-10-14 第 5 轮审查实测:确认 1 条、实际
        译了 2 条,而订阅没换、`source_id` 也没丢)。

        判据落在**后果**上:409 + 说清为什么,而且 `start_pipeline` 一次都没被调
        —— 占了管道再拒绝的话,这一轮什么也没干却把别人的同步挡在外面。
        """
        from kb import pipeline
        start = Mock()
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck', lambda *a, **k: _stuck(1, 0, queued=1)), \
             patch.object(translate, 'scope_signature', lambda *a, **k: '2:bbbbbbbb'), \
             patch.object(pipeline, 'start_pipeline', start):
            r = self.client.post('/api/intake/rss/translate-retry',
                                 json={'scope_sig': '1:aaaaaaaa'})
        self.assertEqual(409, r.status_code)
        self.assertIn('变过了', r.json()['detail'])
        self.assertIn('补抓原文正文', r.json()['detail'])     # 说清了最常见的那个原因
        start.assert_not_called()

    def test_a_matching_confirmation_is_not_refused(self):
        """反过来钉一边:指纹相符的那一次照旧放行。

        只钉「不符就拒」的话,一个**一律拒绝**的实现也能全绿,而那时这颗按钮
        压根点不动。
        """
        from kb import pipeline
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck', lambda *a, **k: _stuck(1, 0, queued=1)), \
             patch.object(translate, 'scope_signature', lambda *a, **k: '1:aaaaaaaa'), \
             patch.object(pipeline, 'start_pipeline', lambda *a, **k: 'run-1'):
            r = self.client.post('/api/intake/rss/translate-retry',
                                 json={'scope_sig': '1:aaaaaaaa'})
        self.assertEqual(200, r.status_code, r.text)

    def test_a_caller_without_a_confirmation_snapshot_is_still_served(self):
        """空指纹那一档放行:curl / 脚本直调没有确认框,也就没有承诺过的数字。

        钉它是因为**这是所有既有调用路径的那一档** —— 写成「必填」的话这颗按钮
        之外的每一处调用都变成 409,而它们从来没有被承诺过任何数字。
        """
        from kb import pipeline
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck', lambda *a, **k: _stuck(1, 0, queued=1)), \
             patch.object(translate, 'scope_signature',
                          Mock(side_effect=AssertionError('空指纹不该去算这一步'))), \
             patch.object(pipeline, 'start_pipeline', lambda *a, **k: 'run-1'):
            r = self.client.post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(200, r.status_code, r.text)

    def test_the_endpoint_takes_its_numbers_from_one_snapshot(self):
        """端点那两个数字只许来自 `stuck()` 那一份快照,不许自己另开一次读。

        `stuck()` 把两个数、那句原因和那份指纹锁在一个读事务里给出来;端点再调
        一次 `pending()` 就又是另一个时点,于是回包里能出现「这一轮 1 条」配着
        「那两条的指纹」—— 而两道指纹校验都会正确匹配那两条、把这个错误的确认
        当成有效授权(2026-10-14 第 6 轮审查实测)。

        判据落在**「那次读压根没发生」**上:打桩成抛异常,比断言某个数字相等
        更钉得住 —— 后者在两个时点恰好一样时(测试里几乎总是)是绿的。
        """
        from kb import pipeline
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck', lambda *a, **k: _stuck(1, 0, queued=1)), \
             patch.object(translate, 'pending',
                          Mock(side_effect=AssertionError('数字只能来自 stuck 那一份快照'))), \
             patch.object(pipeline, 'start_pipeline', lambda *a, **k: 'run-1'):
            r = self.client.post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(200, r.status_code, r.text)

    def test_a_busy_pipeline_is_reported_as_a_conflict(self):
        from kb import pipeline
        from kb.feeds import translate
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'stuck', lambda *a, **k: _stuck(1, 0, queued=1)), \
             patch.object(pipeline, 'start_pipeline', lambda *a, **k: ''):
            r = self.client.post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(409, r.status_code)


@requires_headless
class BrowserTests(unittest.TestCase):
    """真页面:那句话说出三件事、按钮发出带射程的请求、三条反向边。"""

    def test_the_retry_entry_works_on_the_real_panel(self):
        r = run_probe([shutil.which("node"), str(_PROBE)],
                      timeout=300, env=probe_env())
        out = r.stdout + r.stderr
        self.assertEqual(0, r.returncode, out)
        # **退出码 0 不代表它真跑过。** 探针在起浏览器之前就退出(找不到无头壳、
        # 顶层那道孤儿检查判定没有调用方、mock 端口起不来)时同样是 exit 0,而
        # 那时这条用例报的绿是假的 —— 它什么都没验。所以判据落在**产出**上:
        # 那份收尾摘要在不在,以及它说的失败数是不是 0。
        self.assertRegex(out, r'\d+ ok, 0 failed',
                         '探针没有产出收尾摘要,它很可能一个检查都没跑:\n' + out[-2000:])
        self.assertGreaterEqual(int(out.rsplit(' ok, ', 1)[0].rsplit('\n', 1)[-1]), 20, out[-400:])



class ManualRetryCase(FeedCase):
    """这两族共用的摆盘。射程那一族搬去了 tests/test_translate_manual_scope.py
    (只因为 700 行上限),两边摆的是同一种库,所以摆盘待在这里。

    它自己不带 test 方法,被 loader 收走也是空跑 —— 同 `FeedCase`。
    """

    def exhaust(self, title, body='Some English body.'):
        """造一条「试满额度、已按原文入库」的条目:中转站 503 那一轮的产物。"""
        self.put(title, body)
        key = [r for r in reg.items('rss')['items'] if r['title'] == title][0]['key']
        with reg.db() as c:
            c.execute("UPDATE feed_items SET translate_tries=?, translate_error=?,"
                      " exported_hash='stale-hash' WHERE key=?",
                      (translate.MAX_TRIES, 'Claude 调用失败: 503 no available account', key))
        return key


    def _with_fulltext(self, sub):
        """给一条订阅打开正文补抓(`subscriptions.fulltext` 是独立一列)。"""
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET fulltext=1 WHERE id=?', (sub['id'],))


class QueueTests(ManualRetryCase):
    """面板上那颗「重新翻译」按钮:把译不动的条目放回队列**队首**并真的译掉。

    为什么需要它:试满 `translate.MAX_TRIES` 的条目按设计离开队列、按原文导出,
    而 `queued_sql` 的 `translate_tries<MAX_TRIES` 让「同步」和「重试失败项」
    **都选不到它们** —— 中转站一轮 503 就能造出上百条,原来只能靠手改 feeds.db
    才解除得了(2026-10-13 实测 rss 42 条 + social 66 条)。

    这里的判据一律落在**后果**上:「库里那条有没有译文」「下一轮取数取到了它
    吗」。断言 `translate_manual` 这一列等于几是没用的 —— 一个没改 ORDER BY 的
    实现照样能把那个标记写对(见 `test_a_requeued_item_beats_a_full_queue`,那条
    用例是这一族里唯一能钉住排序的)。
    """

    def test_an_exhausted_item_is_invisible_to_the_normal_queue(self):
        # 这条用例钉的是**前提**:不先确认它真的选不到,后面那几条就证明不了
        # 这颗按钮解除了什么。
        self.exhaust('A new agent runtime')
        self.assertEqual(0, translate.pending('rss'))
        self.assertEqual(0, reg.items('rss')['translating'])

    def test_retrying_puts_an_exhausted_item_back_and_translates_it(self):
        key = self.exhaust('A new agent runtime')
        self.assertEqual(1, translate.requeue_failed('rss'))
        self.assertEqual(1, translate.pending('rss'))        # 回到队列里了
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n全新的智能体运行时\n'
                                            '###BODY###\n这个运行时调度工具。')):
            self.assertEqual(1, translate.translate_pending('rss')['done'])
        with reg.db() as c:
            row = c.execute('SELECT * FROM feed_items WHERE key=?', (key,)).fetchone()
        # 后果:库里那条有了中文,而且译完就离开队列、不再排在队首
        self.assertEqual('全新的智能体运行时', row['title_zh'])
        self.assertEqual(0, translate.pending('rss'))
        self.assertEqual(0, row['translate_manual'])
        # 译文要重新导出(旧的英文那份还在 vault 里),所以闸门必须重新成立
        self.assertEqual('', row['exported_hash'])

    def test_a_requeued_item_beats_a_full_queue(self):
        """**这一族里唯一钉得住排序的用例。**

        取数是「失败过的优先」(`translate_tries DESC`),而放回队列要把额度清零
        —— 清零恰好把这一批变成「从没试过」、排到队尾。队列积压超过一轮上限时,
        用户点完按钮、等完一整轮同步,库里一个字都不会变,而且什么都不报错
        (2026-10-13 实测:点下那一刻队列 246 条,恢复的 108 条排在第 139 位往后)。

        所以这里把一轮上限压到 1 条,让「刚放回的那条」和「队列里本来就有的、
        失败过一次的那条」抢同一个名额:取到的必须是用户点过的那一条。
        """
        key = self.exhaust('A new agent runtime')
        # 另一条订阅上有一条失败过两次、还有额度的条目:按「失败过的优先」它
        # 本来排在队首。分属两条订阅是**为了让它不被这次重试碰到** —— 同一条
        # 订阅下 `requeue_failed` 会把它一起放回、额度一起清零,那时两条的排序
        # 键逐字相同,这条用例就什么也证明不了了(第一版实测如此)。
        other_sub = reg.save_subscription('rss', {'name': 'Other', 'url': 'https://o.org/f.xml'})
        reg.put_items(other_sub, [{'url': 'https://o.org/x', 'title': 'Another English post',
                                   'body': 'More English text.', 'published': '2026-09-28 09:00'}])
        with reg.db() as c:
            c.execute("UPDATE feed_items SET translate_tries=2"
                      " WHERE title='Another English post'")
        other = [r for r in reg.items('rss')['items']
                 if r['title'] == 'Another English post'][0]['key']
        translate.requeue_failed('rss', self.source['id'])
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n全新的智能体运行时\n'
                                            '###BODY###\n这个运行时调度工具。')):
            summary = translate.translate_pending('rss', limit=1)
        self.assertEqual(1, summary['done'])
        with reg.db() as c:
            rows = {r['key']: r['title_zh'] for r in
                    c.execute('SELECT key,title_zh FROM feed_items')}
        self.assertEqual('全新的智能体运行时', rows[key])   # 用户点过的那条译了
        self.assertEqual('', rows[other])                   # 另一条留到下一轮

    def test_an_untried_item_is_never_called_a_failure(self):
        """**「还没轮到」不许被说成「翻译失败」。**

        两个数字出自同一个 `stuck`,射程却必须不同:`failed` 回答"试过了没成功"
        (下一步是看那条原因、决定要不要再试),`queued` 回答"等着译的有几条"
        (下一步是等下一轮,或者现在就点那颗按钮)。混成一个的话面板会对一批
        刚抓回来、一次都没试过的内容说"翻译失败",而那句话后面还跟着一条
        失败原因 —— 那是凭空捏造的。

        **它会被 `requeue_failed` 放回去是有意的**(见
        `test_an_ordinary_backlog_can_be_started_by_hand`):那是修 4 要的射程。
        """
        self.put('A fresh English post', 'Never tried yet.')
        self.assertEqual(0, translate.stuck('rss')['failed'])
        self.assertEqual(1, translate.stuck('rss')['queued'])
        self.assertEqual('', translate.stuck('rss')['reason'])
        self.assertEqual(1, translate.pending('rss'))        # 照旧排着队

    def test_retrying_does_not_touch_a_finished_translation(self):
        # 反向钉边之二:已经译完的条目被放回队列就是重复买一次模型调用,而且
        # `_save` 会把它的 exported_hash 再清一次、整篇重新导出。
        self.put('A new agent runtime', 'This runtime schedules tools.')
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n运行时\n'
                                            '###BODY###\n调度工具。')):
            translate.translate_pending('rss')
        with reg.db() as c:   # 译完的那条也带着失败历史(中途失败过一次)
            c.execute('UPDATE feed_items SET translate_tries=2')
        self.assertEqual(0, translate.requeue_failed('rss'))
        self.assertEqual(0, translate.stuck('rss')['failed'])

    def test_the_panel_separates_shipped_from_still_queued(self):
        """面板上那两档的下一步完全相反,所以必须分开数。

        `shipped` 那半**不在**「还在等翻译」的计数里(它们已经离开队列、按英文
        写进了知识库),不单独说出来的话面板上没有一个字提到它们存在。
        """
        self.exhaust('A new agent runtime')
        self.put('Another English post', 'Still queued.')
        with reg.db() as c:          # 失败过一次、还有额度:算 failed 不算 shipped
            # `fetched_at` 要显式摆晚一点:`registry.now()` 只精确到秒,一批摆进去
            # 的条目时间戳一模一样,那时「最近那条」由 key 定序决定 —— 不摆的话
            # 这条断言钉的是扫描顺序,不是「取最近一次的原因」那个判据。
            c.execute("UPDATE feed_items SET translate_tries=1, translate_error='boom',"
                      " fetched_at='2099-01-01 00:00:00'"
                      " WHERE title='Another English post'")
        stuck = translate.stuck('rss')
        self.assertEqual(2, stuck['failed'])
        self.assertEqual(1, stuck['shipped'])
        self.assertIn('boom', stuck['reason'])          # 最近抓回来的那条的原因
        # 顶上那句「还在等翻译」只数还没入库、还在队列里的那一档
        self.assertEqual(1, reg.items('rss')['translating'])

    def test_the_reason_survives_a_cleared_error_column(self):
        # 判据是「试过至少一次」而不是「有错误信息」:`translate_error` 被
        # `_save` 成功时清空过,库里也有 tries 满而错误是空串的存量条目
        # (2026-10-13 实测 social 2 条)。按错误信息筛会把它们永久留在库外。
        key = self.exhaust('A new agent runtime')
        with reg.db() as c:
            c.execute("UPDATE feed_items SET translate_error='' WHERE key=?", (key,))
        self.assertEqual(1, translate.stuck('rss')['failed'])
        self.assertEqual('', translate.stuck('rss')['reason'])
        self.assertEqual(1, translate.requeue_failed('rss'))

    def test_an_ordinary_backlog_can_be_started_by_hand(self):
        """**没失败过的积压也要能被这颗按钮带走。**

        射程原来是 `RETRY_SQL`(`translate_tries>0`),于是「从没试过、只是还没
        排到」的那批一条都选不进来 —— 而定时同步默认是关的,它们在系统里没有
        别的办法动起来(用户原话:"脱离了 ai 我都没法重新翻译他们")。

        反过来钉一边在 `test_retrying_does_not_touch_a_finished_translation`:
        译完的那些照旧不许被放回去。
        """
        self.put('A fresh English post', 'Never tried yet.')
        self.assertEqual(0, translate.stuck('rss')['failed'])    # 确实没失败过
        self.assertEqual(1, translate.stuck('rss')['queued'])    # 但排着队
        self.assertEqual(1, translate.requeue_failed('rss'))
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n一篇新文章\n'
                                            '###BODY###\n还没试过。')):
            self.assertEqual(1, translate.translate_pending('rss', only_manual=True)['done'])

    def test_an_unexported_exhausted_item_is_not_called_shipped(self):
        """**「不再自动重试」不等于「已经写进知识库」。**

        面板上那句「其中 N 条已按原文写进知识库」用的是 `shipped`,它必须真的
        落过盘:最后一次翻译失败之后、导出之前那个窗口里,条目的额度已经满了而
        `exported_hash` 还是空的,vault 里一篇文章都没有 —— 按额度数就是对一篇
        压根不在知识库里的文章说它已经入库了(第 1 轮审查实测)。

        **反过来钉一边:射程照旧要算上它。** 那一档会被 `requeue_failed` 放回去
        (`manual_sql` 不看导出状态),射程改用 `shipped` 的话就又变成「说 3 条、
        动 4 条」—— 正是这一轮要修的那类缺陷。
        """
        self.put('A new agent runtime', 'This runtime schedules tools.')
        key = [r for r in reg.items('rss')['items']][0]['key']
        with reg.db() as c:     # 试满额度,但还没导出
            c.execute("UPDATE feed_items SET translate_tries=?, translate_error='boom',"
                      " exported_hash='' WHERE key=?", (translate.MAX_TRIES, key))
        s = translate.stuck('rss')
        self.assertEqual(1, s['failed'])
        self.assertEqual(0, s['shipped'])        # 没落盘,不许说它入库了
        self.assertEqual(1, s['exhausted'])      # 但它确实离开了队列
        self.assertEqual(0, s['queued'])
        # 射程里有它:点下去真的会把它放回来
        self.assertEqual(1, translate.requeue_failed('rss'))
        self.assertEqual(1, translate.pending('rss', only_manual=True))

    def test_an_exported_exhausted_item_is_counted_as_shipped(self):
        """反过来钉另一边:真落了盘的那些照旧算 `shipped`。

        只验「没落盘的不算」会让一个**一律返回 0** 的实现全绿,而那句话正是面板上
        唯一提到这批内容存在的地方 —— 数成 0 等于把它们重新藏起来。
        """
        self.exhaust('A new agent runtime')      # 它把 exported_hash 摆成了非空
        s = translate.stuck('rss')
        self.assertEqual(1, s['shipped'])
        self.assertEqual(1, s['exhausted'])


if __name__ == "__main__":
    unittest.main()
