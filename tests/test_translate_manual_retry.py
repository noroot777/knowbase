# -*- coding: utf-8 -*-
"""来源面板上「重新翻译」那颗按钮的浏览器回归 + 端点那一侧的口径。

它解除的是一个原来**只能靠手改 feeds.db 才解除得了**的终态:试满
`translate.MAX_TRIES` 的条目按设计离开翻译队列、按原文导出,而 `queued_sql` 的
`translate_tries<MAX_TRIES` 让面板上的「同步」和「重试失败项」**都选不到它们**
—— 中转站一轮 503 就能造出上百条(2026-10-13 实测 rss 42 条 + social 66 条),
那批英文已经被索引、被打了标签,面板上却一个字都不提它们存在。

页面那一侧由 tests/translate_manual_retry_probe.mjs 真跑一遍(自带 mock 后端,CDP 直连
chrome-headless-shell,不装 playwright、不连网、始终无头、临时 profile);
队列口径那一族(`ManualRetryTests`)也在本文件里,脚手架借
`test_feed_translate.FeedCase`(临时 registry + 临时 vault + 固定模型)——
import 进来的 `FeedCase` 自己不带 test 方法,被 loader 收走也是空跑。
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
             patch.object(translate, 'requeue_failed', lambda *a, **k: 5), \
             patch.object(translate, 'pending', lambda *a, **k: 7), \
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
             patch.object(translate, 'requeue_failed', lambda *a, **k: 0), \
             patch.object(translate, 'pending', lambda *a, **k: 0), \
             patch.object(pipeline, 'start_pipeline',
                          lambda *a, **k: started.append(1) or 'run-x'):
            r = self.client.post('/api/intake/rss/translate-retry', json={})
        self.assertEqual(400, r.status_code)
        self.assertEqual([], started)

    def test_a_queue_longer_than_one_round_says_so(self):
        """一轮上限是 `translate.MAX_ITEMS`,队列比它长时要如实说清只译得完一截。

        不说的话用户点一次、看到数字只掉了 40,会以为按钮漏了东西 —— 而那正是
        这颗按钮最常见的使用场景(积压几百条)。
        """
        from kb import pipeline
        from kb.feeds import translate
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'requeue_failed', lambda *a, **k: 108), \
             patch.object(translate, 'pending', lambda *a, **k: 246), \
             patch.object(pipeline, 'start_pipeline', lambda *a, **k: 'run-y'):
            detail = self.client.post('/api/intake/rss/translate-retry',
                                      json={}).json()['detail']
        self.assertIn('108', detail)
        self.assertIn('246', detail)
        self.assertIn(str(translate.MAX_ITEMS), detail)

    def test_a_short_queue_does_not_promise_a_second_round(self):
        """反过来钉一边:一轮译得完的时候不许说「其余留到下一轮」。"""
        from kb import pipeline
        from kb.feeds import translate
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'requeue_failed', lambda *a, **k: 3), \
             patch.object(translate, 'pending', lambda *a, **k: 3), \
             patch.object(pipeline, 'start_pipeline', lambda *a, **k: 'run-z'):
            detail = self.client.post('/api/intake/rss/translate-retry',
                                      json={}).json()['detail']
        self.assertNotIn('下一轮', detail)

    def test_a_busy_pipeline_is_reported_as_a_conflict(self):
        from kb import pipeline
        from kb.feeds import translate
        with patch.object(translate, 'available', lambda: True), \
             patch.object(translate, 'requeue_failed', lambda *a, **k: 1), \
             patch.object(translate, 'pending', lambda *a, **k: 1), \
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


class ManualRetryTests(FeedCase):
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

    def exhaust(self, title, body='Some English body.'):
        """造一条「试满额度、已按原文入库」的条目:中转站 503 那一轮的产物。"""
        self.put(title, body)
        key = [r for r in reg.items('rss')['items'] if r['title'] == title][0]['key']
        with reg.db() as c:
            c.execute("UPDATE feed_items SET translate_tries=?, translate_error=?,"
                      " exported_hash='stale-hash' WHERE key=?",
                      (translate.MAX_TRIES, 'Claude 调用失败: 503 no available account', key))
        return key

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

    def test_retrying_does_not_touch_items_that_were_never_tried(self):
        # 反过来钉一边:正常积压(一轮只译 MAX_ITEMS 条,剩下的额度一次没动过)
        # 本来下一轮就会被取走,放回队列是空操作 —— 把它们也算进去的话,面板上
        # 那个数字会把「还没轮到」说成「翻译失败」。
        self.put('A fresh English post', 'Never tried yet.')
        self.assertEqual(0, translate.requeue_failed('rss'))
        self.assertEqual(0, translate.stuck('rss')['failed'])
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

    def test_retrying_one_subscription_leaves_the_others_alone(self):
        # 那行提示跟着选中的订阅走,按钮的射程也得跟着收:否则用户看着「1 条」
        # 点下去,放回队列的是整个来源的全部条目,代价是他没点过的那一大笔
        # 模型调用。
        other = reg.save_subscription('rss', {'name': 'Other', 'url': 'https://o.org/f.xml'})
        self.exhaust('A new agent runtime')
        reg.put_items(other, [{'url': 'https://o.org/x', 'title': 'Other English post',
                               'body': 'Body here.', 'published': '2026-09-28 09:00'}])
        with reg.db() as c:
            c.execute("UPDATE feed_items SET translate_tries=? WHERE title='Other English post'",
                      (translate.MAX_TRIES,))
        self.assertEqual(1, translate.stuck('rss', self.source['id'])['failed'])
        self.assertEqual(1, translate.requeue_failed('rss', self.source['id']))
        with reg.db() as c:
            rows = {r['title']: r['translate_tries'] for r in
                    c.execute('SELECT title,translate_tries FROM feed_items')}
        self.assertEqual(0, rows['A new agent runtime'])
        self.assertEqual(translate.MAX_TRIES, rows['Other English post'])


if __name__ == "__main__":
    unittest.main()
