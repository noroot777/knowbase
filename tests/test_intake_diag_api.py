# -*- coding: utf-8 -*-
"""「问题与待办」的接口(`/api/intake-care/*`)——**哪一颗按钮真的走了哪一条路**。

这一份钉的是**分派**和**先拒后承诺**那两件事,别处验不到:

① **立即执行有三条路,射程各不相同,由服务端决定走哪一条。** 翻译走既有的
   `/{kind}/translate-retry`(它自己带付费边界和射程指纹)、来源走既有的
   `/{kind}/sync`,只有正文/入库那一档用 `actions.run`。走错一条的后果不是报错,
   是**在确认框承诺的射程之外跑掉一轮**。
② **跑不了的来源问题要先拒,不要先给一段承诺。** `/{kind}/sync` 对停用的订阅和
   没配好的通道各有一句 400;先承诺再被退回,人看到的是两句互相矛盾的话 ——
   尤其 X 限流那一档,那句退回正是「还没有登录过 X」,恰好把人引到这块面板**最
   不该**引他去的地方。
③ **处理记录按实际写入结果记**(元规则):拿别的来源的 key 来搁置,写进去 0 条,
   账上也必须是 0 条。
④ **「正文已补入」不算「已入库」**(状态契约):它只是换到了下一档。

`intake_api.start_sync` / `start_translate_retry` / `pipeline.start_pipeline` 三处
打桩 —— 它们真跑会起一轮同步。**打的是函数体那一层,不是路由壳**:路由只负责
把那条路原本的阶段清单填进去,而本面板要传自己那一份(见 `intake_api` 的拆分)。
别的一行没打:判据、文案、记账全真跑。
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.feeds import registry as reg
from kb.intake_diag import actions, lanes, ledger, warrant
from kb.web import intake_api, intake_diag_api
from tests.intake_diag_case import DiagCase, key_of

QUEUED = 'https://news.example.org/queued'
TRANSLATE = 'https://solo.example.org/translate-queued'
SNOOZED = 'https://news.example.org/snoozed'
PASTED = '\n\n'.join(
    '人手粘进来的正文第 %d 段，它和别的段说的不是同一件事，整页加起来要过得了按行'
    '去重之后那道字数门槛，而且不能是同一段在复读。' % i for i in range(1, 12))


class CareApiCase(DiagCase):
    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        app = FastAPI()
        app.include_router(intake_diag_api.router)
        self.client = TestClient(app)

    def planned(self, payload: dict) -> dict:
        """先问一次射程、把凭证带上 —— 面板上那两步真的是这么走的。

        `/run` 不带凭证一律 400,所以每条用例都得过这一步(见 `PlanTicketRouteTests`)。
        """
        r = self.client.post('/api/intake-care/scope', json=payload)
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()['plan'])
        return {**payload, 'plan': r.json()['plan']}

    def start(self, payload: dict):
        return self.client.post('/api/intake-care/run', json=self.planned(payload))

    def problem_id(self, kind: str, name: str) -> str:
        found = self.client.get('/api/intake-care', params={'kind': kind}).json()
        return next(p['id'] for p in found['sources'] if p['problem'] == name)


class OverviewTests(CareApiCase):
    def test_the_numbers_come_from_the_database_and_name_the_other_screen(self):
        over = self.client.get('/api/intake-care').json()
        tally = lanes.counts()
        for lane in lanes.LANES:
            with self.subTest(lane=lane.id):
                self.assertEqual(sum(tally[k].get(lane.id, 0) for k in tally),
                                 over['lane_totals'][lane.id])
        # 已搁置那一组走它自己那条取数,和列表接口同一个 WHERE。
        self.assertEqual(lanes.shelved()['total'], over['groups'][lanes.ARCHIVED])
        self.assertEqual(2, over['groups'][lanes.ARCHIVED])
        # **来源级问题单独报一个数,不混进那几组的「条」数**(审查第 1 轮 B08):
        # `groups` 的量词是「条内容」,而一条来源问题不是一篇文章。
        self.assertEqual(over['lane_totals']['body_held']
                         + over['lane_totals']['body_exhausted']
                         + over['lane_totals']['translate_exhausted'],
                         over['groups'][lanes.ATTENTION])
        self.assertEqual(len(over['sources']), over['source_problems'])
        self.assertTrue(over['source_problems'])
        # 页头那一句就是「两屏不打架」那条约束的落点。
        self.assertIn('同步中心', over['note'])
        self.assertIn('条内容', over['note'])
        self.assertIn('步', over['note'])
        self.assertIn('来源连接问题单独计数', over['note'])

    def test_one_subscription_can_contribute_two_source_problems_and_no_items(self):
        """同一条订阅同时有 `last_error` 和 `last_note` 时**贡献两条来源问题**。

        把它们加进「条内容」那个数,页头就会说「还有 N 条内容在等」而其中两条压根
        不是内容 —— 点进去也找不到任何一篇文章(审查第 1 轮 B08)。
        """
        before = self.client.get('/api/intake-care', params={'kind': 'web'}).json()
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET last_error=?,failures=2 WHERE id=?',
                      ('连接失败：读取超时', self.ids['page']))
        after = self.client.get('/api/intake-care', params={'kind': 'web'}).json()
        self.assertEqual(before['source_problems'] + 1, after['source_problems'])
        self.assertEqual(len(after['sources']), after['source_problems'])
        # 「条内容」那几组一个字都没动。
        self.assertEqual(before['groups'], after['groups'])
        self.assertEqual(before['lane_totals'], after['lane_totals'])

    def test_an_unknown_kind_is_refused(self):
        self.assertEqual(404, self.client.get('/api/intake-care',
                                              params={'kind': 'nope'}).status_code)

    def test_the_three_fulltext_lanes_arrive_as_three(self):
        over = self.client.get('/api/intake-care').json()
        shown = {lane['id']: lane['state'] for lane in over['lanes']
                 if lane['id'].startswith('body_') and lane['id'] != 'body_recent'}
        self.assertEqual(3, len(set(shown.values())))
        for state in shown.values():
            self.assertNotIn('抓取中', state)


class RunRoutingTests(CareApiCase):
    """①:三条路各走各的。"""

    def test_a_fulltext_batch_runs_export_and_index_only(self):
        seen = {}

        def fake(stages, **kwargs):
            seen.update(stages=stages, **kwargs)
            return {'id': 3}

        with patch.object(actions.pipeline, 'start_pipeline', fake):
            r = self.start({'kind': 'rss', 'keys': [key_of('rss', QUEUED)]})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(['export', 'index'], seen['stages'])
        # 点名那几条跟着同一份 `crawl_filter` 走(见 `actions.run`),**各自那一世
        # 也一起下去**:这份意图要等占住管道、跑到导出阶段才被消费,中间这条内容
        # 可能被清理删掉又从同一个链接收回来(口径在 `kb.intake_diag.warrant`)。
        key = key_of('rss', QUEUED)
        self.assertEqual({'channel': 'rss',
                          'fulltext_retry': {'channel': 'rss', 'keys': [key],
                                             'lives': {key: self.row(key)['life']}}},
                         seen['crawl_filter'])
        said = r.json()
        self.assertIn('条目化提取不在这一步', said['detail'])
        self.assertEqual(['export', 'index'], said['scope']['stages'])
        self.assertEqual(['run_body'], [row['action'] for row in ledger.recent('rss')])

    def test_a_translation_batch_is_handed_to_the_existing_endpoint(self):
        seen = {}

        def fake(kind, body, stages):
            seen.update(kind=kind, source_id=body.source_id, sig=body.scope_sig,
                        stages=list(stages))
            return {'ok': True}

        with patch.object(intake_api, 'start_translate_retry', fake):
            r = self.start({'kind': 'rss', 'keys': [key_of('rss', TRANSLATE)]})
        self.assertEqual(200, r.status_code, r.text)
        # 指纹原样转过去 —— 它就是「确认期间范围变没变」那道闸。**取的是服务端
        # 那一份**(`translate.stuck` 同一个读事务里给的),不是请求体里带来的:
        # 前端带什么都不该影响这一轮的付费射程。
        self.assertEqual({'kind': 'rss', 'source_id': None,
                          'sig': r.json()['scope']['scope_sig'],
                          'stages': ['export', 'index']}, seen)
        self.assertTrue(seen['sig'])
        latest = ledger.recent('rss')[0]
        self.assertEqual('run_translate', latest['action'])
        self.assertEqual(r.json()['scope']['picked'], latest['affected'])

    def test_a_source_problem_is_handed_to_the_existing_sync_endpoint(self):
        """**确认时那份来源描述也要跟着交下去,而且是整份、不是一个编号。**

        只传订阅编号的话,起跑那一下只能按编号**重读一份新配置** —— 而人确认的是
        确认框里那一条。`expect` 让那一步用同一份描述再核一次,并由它一路交到执行
        器手里(那一侧在 `tests/test_intake_diag_warrant.py`)。这里钉的是交接本身:
        少传一个参数的话这条红不会亮,而缺口只有在改指之后才看得见。

        **比的是值不是有无**(元规则):描述要和 `warrant.source_sig` 现算的那一份
        逐字相同 —— 只问「带没带」时,「带错了哪一份」恒绿。
        """
        seen = {}

        def fake(kind, body, stages, expect=None):
            seen.update(kind=kind, source_ids=body.source_ids, stages=list(stages),
                        expect=expect)
            return {'ok': True}

        with patch.object(intake_api, 'start_sync', fake):
            r = self.start({'kind': 'web', 'keys': [],
                            'problem_id': self.problem_id('web', 'coverage')})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual({'kind': 'web', 'source_ids': [self.ids['page']],
                          'stages': ['crawl', 'export', 'index'],
                          'expect': {str(self.ids['page']): warrant.source_sig(
                              reg.subscription('web', self.ids['page']), 'web',
                              reg.get_settings('web'))}}, seen)
        self.assertEqual('重试此范围', r.json()['scope']['title'])
        self.assertEqual(['run_source'], [row['action'] for row in ledger.recent('web')])

    def test_an_old_source_confirmation_cannot_start_the_repointed_source(self):
        """确认框写着重试来源 A,停留期间这条订阅被改指到 B —— 旧票一律拒。

        形态:`problem_id` 是 `source:<id>:connect` 拼出来的,改指之后它一个字都
        没变,于是上一版那张只绑编号的票照旧对得上,实际重试的是 B、还带着「人按
        的、跳过退避」(审查第 2 轮 R3 实测)。**两头都钉**:这一条钉拒绝,下面那
        条钉「没改过的照旧放行」—— 只钉拒绝的话,一张永远对不上的票也是全绿。
        """
        sid = self.ids['page']
        planned = self.planned({'kind': 'web', 'keys': [],
                                'problem_id': self.problem_id('web', 'coverage')})
        reg.save_subscription('web', {'name': '改指到另一个专栏'}, sid)
        started = []
        with patch.object(intake_api, 'start_sync',
                          lambda *a, **k: started.append(a) or {'ok': True}):
            r = self.client.post('/api/intake-care/run', json=planned)
        self.assertEqual(409, r.status_code, r.text)
        self.assertEqual([], started)
        self.assertEqual([], [row['action'] for row in ledger.recent('web')])

    def test_a_source_confirmation_that_did_not_drift_still_goes_through(self):
        """**反过来钉一边。** 每次重签都变的票等于「永远拒绝」,而人会学会无脑重试。"""
        planned = self.planned({'kind': 'web', 'keys': [],
                                'problem_id': self.problem_id('web', 'coverage')})
        with patch.object(intake_api, 'start_sync',
                          lambda kind, body, stages, expect=None: {'ok': True}):
            r = self.client.post('/api/intake-care/run', json=planned)
        self.assertEqual(200, r.status_code, r.text)

    def test_the_whole_kind_button_does_not_need_a_selection(self):
        with patch.object(actions.pipeline, 'start_pipeline',
                          lambda stages, **kw: {'id': 9}):
            r = self.start({'kind': 'rss', 'keys': [], 'whole_kind': True})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(0, r.json()['scope']['picked'])
        self.assertNotIn('不只是你点的', r.json()['scope']['detail'])

    def test_a_busy_pipeline_comes_back_as_a_conflict(self):
        def refuse(stages, **kwargs):
            kwargs['refused'].append('每日日报那一轮正在跑')
            return None

        payload = self.planned({'kind': 'rss', 'keys': [key_of('rss', QUEUED)]})
        with patch.object(actions.pipeline, 'start_pipeline', refuse):
            r = self.client.post('/api/intake-care/run', json=payload)
        self.assertEqual(409, r.status_code)
        self.assertIn('每日日报那一轮正在跑', r.json()['detail'])
        # 占不到管道那一下什么也没记 —— 账上不许出现一笔没发生的执行。
        self.assertEqual([], ledger.recent('rss'))


class RefuseBeforePromisingTests(CareApiCase):
    """②:跑不了的来源问题先拒,不先给一段承诺。"""

    def test_a_channel_that_is_not_configured_is_refused_with_the_reason(self):
        problem_id = self.problem_id('social', 'x_boot')
        for path in ('/api/intake-care/scope', '/api/intake-care/run'):
            with self.subTest(path=path):
                r = self.client.post(path, json={'kind': 'social', 'keys': [],
                                                 'problem_id': problem_id,
                                                 'plan': 'whatever'})
                self.assertEqual(400, r.status_code)
                said = r.json()['detail']
                self.assertIn('待配置', said)
                self.assertIn('这跟它这一次为什么失败是两件事', said)

    def test_a_disabled_source_is_refused_and_points_at_the_sources_page(self):
        r = self.client.post('/api/intake-care/run',
                             json={'kind': 'rss', 'keys': [], 'plan': 'whatever',
                                   'problem_id': self.problem_id('rss', 'connect')})
        self.assertEqual(400, r.status_code)
        self.assertIn('我的订阅', r.json()['detail'])

    def test_a_problem_that_went_away_is_refused_rather_than_run_blind(self):
        """确认框里那段话是按旧问题写的 —— 问题已经变了就整批拒绝。"""
        r = self.client.post('/api/intake-care/run',
                             json={'kind': 'web', 'keys': [], 'plan': 'whatever',
                                   'problem_id': 'source:999:coverage'})
        self.assertEqual(409, r.status_code)
        self.assertIn('请刷新再看一次', r.json()['detail'])


class ShelfRouteTests(CareApiCase):
    """③:账上那个数就是真写进去的数。"""

    def test_a_key_from_another_source_is_reported_as_zero_not_as_done(self):
        r = self.client.post('/api/intake-care/shelf',
                             json={'kind': 'web', 'keys': [key_of('rss', QUEUED)],
                                   'decision': 'snooze', 'reason': '先放放'})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual({'shelved': 0, 'asked': 1}, {k: r.json()[k]
                                                      for k in ('shelved', 'asked')})
        self.assertEqual('暂缓 0 条', ledger.recent('web')[0]['summary'])
        self.assertEqual(0, ledger.recent('web')[0]['affected'])

    def decision_of(self, key: str) -> str:
        found = next(row for row in lanes.shelved()['items'] if row['key'] == key)
        return found['shelf_decision_id']

    def test_releasing_puts_it_back_and_says_how_many(self):
        key = key_of('rss', SNOOZED)
        r = self.client.post('/api/intake-care/shelf/release',
                             json={'kind': 'rss', 'keys': [key],
                                   'decisions': [self.decision_of(key)]})
        self.assertEqual({'released': 1, 'asked': 1}, {k: r.json()[k]
                                                       for k in ('released', 'asked')})
        self.assertEqual('shelf_release', ledger.recent('rss')[0]['action'])
        self.assertEqual(1, ledger.recent('rss')[0]['affected'])
        self.assertNotIn(key, [row['key'] for row in lanes.shelved()['items']])
        # 恢复之后它回到本来所在的那一档,而不是消失。
        self.assertEqual({'body_queued': [key]}, lanes.split([key], 'rss'))

    def test_an_empty_batch_is_refused_instead_of_logging_a_no_op(self):
        for path in ('/api/intake-care/shelf/release', '/api/intake-care/requeue'):
            with self.subTest(path=path):
                r = self.client.post(path, json={'kind': 'rss', 'keys': []})
                self.assertEqual(400, r.status_code)
        self.assertEqual([], ledger.recent('rss'))

    def test_an_unknown_decision_is_refused_by_the_schema(self):
        r = self.client.post('/api/intake-care/shelf',
                             json={'kind': 'rss', 'keys': [key_of('rss', QUEUED)],
                                   'decision': 'delete'})
        self.assertEqual(422, r.status_code)

    def test_more_keys_than_the_cap_are_refused(self):
        r = self.client.post('/api/intake-care/requeue',
                             json={'kind': 'rss',
                                   'keys': [f'{i:032x}'
                                            for i in range(intake_diag_api.MAX_KEYS + 1)]})
        self.assertEqual(422, r.status_code)


class PastedBodyTests(CareApiCase):
    """④:「正文已补入」不算「已入库」。"""

    def test_it_moves_to_the_next_lane_and_says_so(self):
        key = key_of('rss', QUEUED)
        r = self.client.post('/api/intake-care/body',
                             json={'key': key, 'body': PASTED})
        self.assertEqual(200, r.status_code, r.text)
        self.assertIn('还没有入库', r.json()['detail'])
        self.assertEqual({'ingest_queued': [key]}, lanes.split([key], 'rss'))
        self.assertEqual('paste_body', ledger.recent('rss')[0]['action'])
        self.assertEqual([key], ledger.recent('rss')[0]['item_keys'])

    def test_a_body_that_does_not_clear_the_bar_is_refused_and_nothing_is_logged(self):
        """准入判据和浏览器采集那条路**逐字同一份**(`browser_capture._admit_body`),
        这里只钉「被拒的那一下账上不许留痕」—— 账是人回头判断「我上次对这一条做过
        什么」的依据,记一笔没发生过的补入比不记更坏。
        """
        r = self.client.post('/api/intake-care/body',
                             json={'key': key_of('rss', QUEUED), 'body': '太短了。'})
        self.assertEqual(400, r.status_code)
        self.assertEqual([], ledger.recent('rss'))

    def test_an_unknown_item_is_refused_as_missing(self):
        r = self.client.post('/api/intake-care/body',
                             json={'key': '0' * 32, 'body': PASTED})
        self.assertEqual(404, r.status_code)
        self.assertEqual([], ledger.recent('rss'))

class PlanTicketRouteTests(CareApiCase):
    """⑤:**提交前按现态重签一次计划凭证**(审查第 1 轮 F03)。

    `/scope` 和 `/run` 原先各按「此刻这几条在哪一档」重新选路。确认框里写着
    「条目化提取不在这一步」,而等待期间那一条补抓完成、变成待翻译,提交时就改走
    了另一条路、另一份射程 —— 用户同意的是一份计划,跑掉的是另一份,**而那一份要
    花模型的钱**。

    三件事:没带凭证一律拒;凭证对不上整批拒绝、**什么都不许开始**;对得上照旧跑。
    """

    def test_a_submission_without_a_plan_is_refused_and_starts_nothing(self):
        called = []
        with patch.object(actions.pipeline, 'start_pipeline',
                          lambda stages, **kw: called.append(stages) or {'id': 1}):
            r = self.client.post('/api/intake-care/run',
                                 json={'kind': 'rss', 'keys': [key_of('rss', QUEUED)]})
        self.assertEqual(400, r.status_code)
        self.assertIn('没有带执行计划', r.json()['detail'])
        self.assertEqual([], called)
        self.assertEqual([], ledger.recent('rss'))

    def test_a_plan_that_drifted_while_you_were_reading_it_is_refused(self):
        """主形态:等待期间那一条从正文档跳到翻译档。

        不拦的话这一下会走翻译那条路 —— 按**整个范围**放回队列并调模型,而确认框
        里写的是「导出 + 索引」。
        """
        key = key_of('rss', 'https://news.example.org/en-shell')
        self.put(reg.subscription('rss', self.ids['agg']),
                 'https://news.example.org/en-shell',
                 body='标题壳 (\nexample.org\n)\n10-08\n108 HN Points',
                 title='An English shell', lang='en')
        payload = self.planned({'kind': 'rss', 'keys': [key]})
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fulltext_at=?,content_kind='content'"
                      ' WHERE key=?', (reg.now(), key))
        called = []
        with patch.object(actions.pipeline, 'start_pipeline',
                          lambda stages, **kw: called.append(stages) or {'id': 1}):
            with patch.object(intake_api, 'start_translate_retry',
                              lambda *a: called.append('translate')):
                r = self.client.post('/api/intake-care/run', json=payload)
        self.assertEqual(409, r.status_code)
        self.assertIn('什么都没有开始', r.json()['detail'])
        self.assertEqual([], called)
        self.assertEqual([], ledger.recent('rss'))

    def test_a_ticket_signed_for_another_kind_is_not_accepted_here(self):
        """同一份选集、同一份阶段清单,换个 `kind` 提交不许通过。"""
        payload = self.planned({'kind': 'rss', 'keys': [key_of('rss', QUEUED)]})
        r = self.client.post('/api/intake-care/run', json={**payload, 'kind': 'web'})
        self.assertIn(r.status_code, (400, 409))
        self.assertEqual([], ledger.recent('web'))

    def test_the_page_cannot_declare_its_own_paid_range(self):
        """旧页面不许扩大付费射程:请求体里带来的指纹**一个字都不读**。

        收下它等于让页面自己声明「这一轮要放回队列多少条」—— 而那正是这道闸要挡
        的事。三头都钉:①请求模型里压根没有这一格;②夹带了就**整份请求被拒**
        (`extra='forbid'`),一步都不开始;③**反过来**,同一份请求不夹带照旧跑得
        起来 —— 不然「这个端点全拒」也是绿的。
        """
        self.assertNotIn('scope_sig', intake_diag_api.RunRequest.model_fields)
        seen: list[str] = []

        def fake(kind, body, stages):
            seen.append(body.scope_sig)
            return {'ok': True}

        payload = self.planned({'kind': 'rss', 'keys': [key_of('rss', TRANSLATE)]})
        with patch.object(intake_api, 'start_translate_retry', fake):
            smuggled = self.client.post(
                '/api/intake-care/run',
                json={**payload, 'scope_sig': '我自己编的一个指纹'})
            self.assertEqual(422, smuggled.status_code, smuggled.text)
            self.assertIn('scope_sig', smuggled.text)
            self.assertEqual([], seen)
            self.assertEqual([], ledger.recent('rss'))
            clean = self.client.post('/api/intake-care/run', json=payload)
        self.assertEqual(200, clean.status_code, clean.text)
        # 转过去的是**服务端自己算的**那一份,不是页面给的那串字。
        self.assertEqual([clean.json()['scope']['scope_sig']], seen)
        self.assertNotIn('我自己编的一个指纹', seen)

    def test_a_translation_route_without_a_fingerprint_is_refused(self):
        """取不到指纹就不许开跑。

        那个端点把空串解释成「没有确认框、没有承诺过的数字」(curl / 脚本直调)
        而放行 —— 可这里恰恰是从确认框来的,放过去就是拿「脚本直调」的待遇跑一次
        人确认过的付费操作。
        """
        called = []
        payload = self.planned({'kind': 'rss', 'keys': [key_of('rss', TRANSLATE)]})
        with patch.object(actions, '_signed',
                          lambda plan, *a, **kw: {**plan, 'scope_sig': '',
                                                  'plan': payload['plan']}):
            with patch.object(intake_api, 'start_translate_retry',
                              lambda *a: called.append(a)):
                r = self.client.post('/api/intake-care/run', json=payload)
        self.assertEqual(409, r.status_code)
        self.assertIn('取不到指纹', r.json()['detail'])
        self.assertEqual([], called)

    def test_a_matching_ticket_goes_through(self):
        """**反过来钉一边**:没变过的计划照旧跑得起来。

        不钉这一面的话,「永远 409」也能让上面三条全绿 —— 而那样这颗按钮在正常
        情况下也点不动。
        """
        with patch.object(actions.pipeline, 'start_pipeline',
                          lambda stages, **kw: {'id': 11}):
            r = self.start({'kind': 'rss', 'keys': [key_of('rss', QUEUED)]})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(['run_body'], [row['action'] for row in ledger.recent('rss')])


class ItemPagingTests(CareApiCase):
    """⑥:往后翻用 `offset`,**不是把 `limit` 加大**(审查第 1 轮 F09)。

    `limit` 在服务端被夹到 `lanes.PAGE` 封顶 —— 上一版前端把它加大到 41 再请求,
    拿回来的还是最前 40 条:一个总数 41 的档位反复点「展开更多」永远看不到第 41
    条,而按钮一直在那里、每次点都"成功"。
    """

    def setUp(self):
        super().setUp()
        self.agg = reg.subscription('rss', self.ids['agg'])
        shell = '标题壳 (\nexample.org\n)\n10-08\n108 HN Points'
        # 凑到总数 > 一页:原本这一档有 1 条。
        for i in range(lanes.PAGE):
            self.put(self.agg, f'https://news.example.org/page-{i:03d}', body=shell,
                     title=f'积压里的第 {i} 条')

    def fetch(self, **params) -> dict:
        r = self.client.get('/api/intake-care/items',
                            params={'lane': 'body_queued', 'kind': 'rss', **params})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()['sections'][0]

    def test_a_bigger_limit_cannot_reach_past_the_first_page(self):
        first = self.fetch()
        self.assertEqual(lanes.PAGE + 1, first['total'])
        self.assertEqual(lanes.PAGE, len(first['items']))
        stretched = self.fetch(limit=lanes.PAGE + 1)
        self.assertEqual([row['key'] for row in first['items']],
                         [row['key'] for row in stretched['items']])

    def test_the_offset_does_reach_it_and_the_two_pages_do_not_overlap(self):
        first = self.fetch()
        second = self.fetch(offset=len(first['items']))
        self.assertEqual(1, len(second['items']))
        self.assertEqual(lanes.PAGE + 1, second['total'])     # 总数按同一个 WHERE
        got = [row['key'] for row in first['items']] + \
              [row['key'] for row in second['items']]
        self.assertEqual(lanes.PAGE + 1, len(set(got)))
        # 翻到底之后再往后要就是空集,不是报错、也不是从头再来一遍。
        self.assertEqual([], self.fetch(offset=lanes.PAGE + 1)['items'])

    def test_the_shelved_group_pages_the_same_way(self):
        """已搁置那一组走的是另一条取数(`lanes.shelved`),分页协议必须一样。"""
        keys = [row['key'] for row in lanes.rows('body_queued', 'rss',
                                                 '', 0, lanes.PAGE)['items']]
        self.shelve(keys, 'rss', 'snooze')
        r = self.client.get('/api/intake-care/items',
                            params={'group': lanes.ARCHIVED, 'kind': 'rss'})
        found = r.json()['sections'][0]
        self.assertEqual(len(keys) + 2, found['total'])       # 样例自带两条
        self.assertEqual(lanes.PAGE, len(found['items']))
        rest = self.client.get('/api/intake-care/items',
                               params={'group': lanes.ARCHIVED, 'kind': 'rss',
                                       'offset': lanes.PAGE}).json()['sections'][0]
        self.assertEqual(len(keys) + 2 - lanes.PAGE, len(rest['items']))
