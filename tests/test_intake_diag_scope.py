# -*- coding: utf-8 -*-
"""**「立即执行」的射程必须说实话**(`kb/intake_diag/actions.py` 的 `scope_of`),
以及**来源级那两类问题的措辞**(`kb/intake_diag/sources.py`)。

为什么这一份比别的更要紧:确认框里那段话是人决定「要不要花这笔钱」的全部信息。
管道那三条真相摆在那儿 —— `crawl_filter` 只有 `export` 这一段认;`index` 是整个
vault 的增量比对、要拿维护锁;`extract` 走 `kb.extract.pending_docs(conn)`,
**压根没有来源参数**,按全库取数、花模型的钱。所以「只执行这一条」做不到,按钮上
也不许这么写。

钉五件事:
① **两块面板跑同一份阶段清单**(`RUN_STAGES` 与 `browser_capture_api.CONTINUE_STAGES`)。
   各自演进的话,同一句「条目化提取不在这一步」会在一个页面上成真、在另一个页面
   上变谎。这是结构红,行为用例证明不了它。
② **那段话由服务端给**,而且如实写着整类来源、本轮上限、会联网、要花钱、提取不在
   这一轮。
③ **混着选时拒绝,而不是挑一条路跑**:翻译那一轮按整个范围放回队列,正文那一轮按
   来源的补抓队列取 —— 一次按两份射程跑,确认框里写不出一句不说谎的话。
④ **翻译那一档交回既有端点**,指纹原样取 `translate.stuck` 那一份:另开一次读就能
   出现「确认 1 条、带着那两条的指纹」。
⑤ **X 那两类绝不许说成掉登录**,覆盖未完成那颗按钮**不许承诺断点续传**。
⑥ **确认框里那份计划带一张凭证,提交时按现态重签一张比一比**:等待期间条目换了
   档、来源改了指、选集里某一条被清理重收 —— 用户同意的是一份计划,跑掉的会是另
   一份,**而那一份可能要花模型的钱**(条目那三档在 `PlanTicketTests`,来源那一档在
   `SourceTicketTests`:它绑的是**这条订阅的定义改到第几版**,不是它的编号)。
⑦ **本面板任何一条路都不许带 `extract`**,这一条是结构红:三条路的阶段清单逐个
   看过,外加「真写进去了就当场炸」那道守卫(`NoExtractAnywhereTests`)。

只有 `pipeline.start_pipeline` 和 `translate.stuck` 打桩(前者会真起一轮同步,后者
用来摆「范围里一条都没有」那两种末态),判据和文案一个字都没打。
"""
from __future__ import annotations

from unittest.mock import patch

from kb.feeds import fulltext, registry as reg, translate, x_page_state as xstate
from kb.intake_diag import actions, lanes, sources
from kb.web import browser_capture_api
from tests.intake_diag_case import DiagCase, key_of

QUEUED = 'https://news.example.org/queued'
TRANSLATE = 'https://solo.example.org/translate-queued'


class RunStageTests(DiagCase):
    def test_both_panels_continue_with_exactly_the_same_two_stages(self):
        """①:结构红。两处的清单必须逐字相等。"""
        self.assertEqual(browser_capture_api.CONTINUE_STAGES, actions.RUN_STAGES)
        self.assertEqual(('export', 'index'), actions.RUN_STAGES)
        self.assertNotIn('extract', actions.RUN_STAGES)
        self.assertNotIn('crawl', actions.RUN_STAGES)

    def test_run_starts_those_stages_for_this_kind_and_nothing_else(self):
        self.seed()
        seen = {}

        def fake(stages, **kwargs):
            seen.update(stages=stages, **kwargs)
            return {'id': 7}

        with patch.object(actions.pipeline, 'start_pipeline', fake):
            self.assertEqual({'run': {'id': 7}}, actions.run('rss'))
        self.assertEqual(['export', 'index'], seen['stages'])
        self.assertEqual({'channel': 'rss'}, seen['crawl_filter'])
        # 不采集,所以不许替一轮运行声明「人按的、跳过退避」。
        self.assertNotIn('force', seen)

    def test_the_picked_items_ride_along_inside_that_one_atomic_state(self):
        """人勾的那几条跟着 `crawl_filter` 走(审查第 1 轮 F02)。

        **意图只放在 `start_pipeline` 原子写进去的那份状态里**:占不到管道 = 这份
        状态压根没写 = 库里一个字节都没动。先改库再占管道的写法在 409 那一支会把
        挂起和额度提前改掉,而人看到的是一句「没有开始」—— 下面那条用例钉这一面。
        """
        self.seed()
        picked = [key_of('rss', 'https://news.example.org/held'),
                  key_of('rss', 'https://news.example.org/exhausted')]
        seen = {}

        def fake(stages, **kwargs):
            seen.update(stages=stages, **kwargs)
            return {'id': 7}

        with patch.object(actions.pipeline, 'start_pipeline', fake):
            actions.run('rss', picked)
        self.assertEqual({'channel': 'rss',
                          'fulltext_retry': {
                              'channel': 'rss', 'keys': picked,
                              # 那几条各自**此刻这一世**也在同一份状态里:这份意图
                              # 要等占住管道、跑到导出阶段才被消费,中间这条内容
                              # 可能被清理删掉又从同一个链接收回来 —— key 一个字
                              # 没变(口径在 `kb.intake_diag.warrant`)。
                              'lives': {key: self.row(key)['life'] for key in picked}}},
                         seen['crawl_filter'])

    def test_a_refused_round_leaves_the_picked_items_exactly_as_they_were(self):
        """409 那一支:**库里一个字节都不许动**。

        人看到的是一句「没有开始」,而挂起已经解掉、额度已经还回去的话,下一轮
        常规同步会去抓他以为没开始的那几条 —— 而那一轮的失败会记在他头上。
        """
        self.seed()
        held = key_of('rss', 'https://news.example.org/held')
        exhausted = key_of('rss', 'https://news.example.org/exhausted')
        before = (self.row(held), self.row(exhausted))

        def refuse(stages, **kwargs):
            kwargs['refused'].append('每日日报那一轮正在跑，等它结束再来')
            return None

        with patch.object(actions.pipeline, 'start_pipeline', refuse):
            with self.assertRaises(RuntimeError):
                actions.run('rss', [held, exhausted])
        self.assertEqual(before, (self.row(held), self.row(exhausted)))

    def test_a_busy_pipeline_comes_back_with_its_own_reason(self):
        self.seed()

        def refuse(stages, **kwargs):
            kwargs['refused'].append('每日日报那一轮正在跑，等它结束再来')
            return None

        with patch.object(actions.pipeline, 'start_pipeline', refuse):
            with self.assertRaises(RuntimeError) as caught:
                actions.run('rss')
        self.assertIn('每日日报那一轮正在跑', str(caught.exception))

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            actions.run('nope')


class BodyScopeTests(DiagCase):
    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def test_the_confirm_text_states_the_real_reach(self):
        scope = actions.scope_of('rss', [key_of('rss', QUEUED)])
        self.assertEqual(['export', 'index'], scope['stages'])
        self.assertEqual(1, scope['picked'])
        detail = scope['detail']
        for said in ('范围是整类', '不只是你点的 1 条', str(fulltext.MAX_ITEMS),
                     '会联网', '要花钱', '条目化提取不在这一步',
                     '索引会检查整个知识库'):
            with self.subTest(said=said):
                self.assertIn(said, detail)
        # 队列里此刻真的有几条,也要如实写出来(元规则:计数从库里查)。
        self.assertIn(f'此刻队列里 {fulltext.pending("rss")} 条', detail)

    def test_the_header_button_does_not_talk_about_items_you_picked(self):
        """演示页顶上那颗叫「执行待办」、单条那颗叫「只执行这一条」,而后者在真实
        管道里做不到 —— 所以这里只在文案上去掉选集那半句,阶段清单一字不动。
        """
        head = actions.kind_scope('rss')
        self.assertEqual(0, head['picked'])
        self.assertNotIn('不只是你点的', head['detail'])
        self.assertIn('范围是整类', head['detail'])
        self.assertEqual(actions.scope_of('rss', [key_of('rss', QUEUED)])['stages'],
                         head['stages'])

    def test_an_empty_or_settled_batch_is_refused(self):
        with self.assertRaises(ValueError):
            actions.scope_of('rss', [])
        done = key_of('rss', 'https://news.example.org/recent')
        with self.assertRaises(ValueError):
            actions.scope_of('rss', [done])

    def test_mixing_translation_and_fulltext_is_refused_rather_than_half_run(self):
        both = [key_of('rss', QUEUED), key_of('rss', TRANSLATE)]
        self.assertEqual({'body_queued', 'translate_queued'},
                         set(lanes.split(both, 'rss')))
        with self.assertRaises(ValueError) as caught:
            actions.scope_of('rss', both)
        self.assertIn('请分开执行', str(caught.exception))


class TranslateScopeTests(DiagCase):
    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', TRANSLATE)

    def test_the_translation_route_hands_off_to_the_existing_endpoint(self):
        scope = actions.scope_of('rss', [self.key])
        self.assertEqual('/api/intake/rss/translate-retry', scope['endpoint'])
        stuck = translate.stuck('rss')
        self.assertEqual(stuck['sig'], scope['scope_sig'])
        # 射程是整个范围(还排着的 + 已经试满额度离开队列的),不是你点的那一条。
        self.assertEqual(stuck['queued'] + stuck['exhausted'], scope['picked'])
        self.assertEqual(2, scope['picked'])
        self.assertIsNone(scope['source_id'])
        # 「条目化提取不在这一轮」:译完之后那几篇的旧标签只被标成待重做,提取等
        # 下一次常规同步 —— 和补抓那条路上那句「不在这一步」同一个口径
        # (`actions` 里那两段文案各自逐字写着自己那一轮真会跑的事)。
        for said in ('整类', '不只是你点的那一条', '要花钱',
                     '条目化提取不在这一轮', '整批拒绝'):
            with self.subTest(said=said):
                self.assertIn(said, scope['detail'])

    def test_nothing_left_to_translate_is_refused_with_the_endpoints_own_wording(self):
        """确认框不许承诺一轮必然被 400 退回的执行 —— 否则人照着那段话点下去,
        拿到的是一句跟确认框矛盾的拒绝。两句文案逐字照搬那个端点。
        """
        empty = {'queued': 0, 'exhausted': 0, 'failed': 0, 'shipped': 0,
                 'sig': 'x', 'reason': ''}
        with patch.object(translate, 'stuck', lambda *a, **k: empty):
            with self.assertRaises(ValueError) as caught:
                actions.scope_of('rss', [self.key])
        self.assertEqual('这个范围里没有等待翻译的内容了。', str(caught.exception))
        with patch.object(translate, 'stuck', lambda *a, **k: {**empty, 'failed': 3}):
            with self.assertRaises(ValueError) as caught:
                actions.scope_of('rss', [self.key])
        self.assertIn('都在等正文补抓', str(caught.exception))


class SourceProblemTests(DiagCase):
    """⑤:X 那两类问题。**绝不许说成掉登录,也不许承诺断点续传。**"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def problem(self, kind: str, name: str) -> dict:
        return next(p for p in sources.problems(kind) if p['problem'] == name)

    def test_throttling_is_never_dressed_up_as_a_logout(self):
        boot = self.problem('social', 'x_boot')
        self.assertEqual('页面应用没能启动（限流，等冷却）', boot['label'])
        self.assertIn('不要反复登录', boot['explanation'])
        detail = actions.scope_of('social', [], problem=boot)['detail']
        said = ' '.join([boot['label'], boot['explanation'], boot['advice'], detail])
        # **只有否定式允许出现**:「不需要重新登录」正是要说的那句话,而祈使式
        # 一句都不许有 —— 把限流说成掉登录,人就会一遍遍去登,而登了也没用。
        for banned in ('请重新登录', '去重新登录', '重新登录一次', '重新登录后',
                       '先登录一次', '重新登陆', '登录已过期', '登录态已失效'):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, said)
        self.assertIn('不是登录过期', boot['explanation'])
        self.assertIn('不需要重新登录', detail)
        # 冷却由后端统一给,也不保证这一次就读得到。
        self.assertIn('45', said)
        self.assertIn('180', said)
        self.assertIn('不保证', said)
        # `x_browser` 本来就会等冷却并排到队尾重试一次 —— 不许写成「完全没有自动重试」。
        self.assertIn('排到队尾重试一次', boot['advice'])

    def test_the_generic_bucket_also_says_nothing_about_logging_in(self):
        """判不准时多说一句「去登录」,就是把限流说成掉登录 —— 那正是要防的那件事。"""
        connect = self.problem('rss', 'connect')
        said = connect['explanation'] + connect['advice']
        self.assertNotIn('登录', said)
        self.assertIn('无法解析主机名', connect['evidence'])

    def test_the_coverage_button_never_promises_to_resume_scrolling(self):
        cover = self.problem('social', 'coverage')
        scope = actions.scope_of('social', [], problem=cover)
        self.assertEqual('重试此范围', scope['title'])
        self.assertIn('不是从上次停下的位置接着滚', cover['advice'])
        self.assertIn('**不是**从上次停下的位置接着滚', scope['detail'])
        self.assertIn('仍然可能读不完', scope['detail'])
        # 保留的同步下界要说出来 —— 它正是「重试此范围」里那个「范围」。
        self.assertIn(cover['since'], scope['detail'])
        self.assertTrue(cover['since'])

    def test_a_note_quoting_the_throttling_wording_is_still_a_coverage_problem(self):
        """实测形态:某账号的 `last_note` 里**原样嵌着**限流那段文案。拿关键词去搜
        它会把一个「成功但没读完」判成「启动限流」,于是屏幕上说「本轮没读到这个
        账号」,而它其实读到了一堆内容。按列分流就没有这个问题。
        """
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET last_note=? WHERE id=?',
                      (f'滚动次数用完，没有读到上次的位置；有 67 条折叠推文暂缓补全'
                       f'（{xstate.session_throttled()}），留到下次同步再试',
                       self.ids['cover']))
        found = [p['problem'] for p in sources.problems('social')
                 if p['source_id'] == self.ids['cover']]
        self.assertEqual(['coverage'], found)

    def test_a_blocked_channel_is_surfaced_instead_of_promising_a_retry(self):
        """通道没配好时 `/{kind}/sync` 以 400 拒掉。不先说出来的话,确认框照常承诺
        「冷却后重试这一个账号」,点下去却弹一句「浏览器通道还没有登录过 X」——
        而这块面板存在的理由之一恰恰是**不要**把限流引到「再登一次」上去。
        """
        boot = self.problem('social', 'x_boot')
        self.assertTrue(boot['blocked'])
        self.assertFalse(boot['actionable'])
        self.assertTrue(boot['enabled'])
        # **反过来钉一边**:不走 X 通道的那一条照旧能点。
        page = self.problem('web', 'coverage')
        self.assertEqual('', page['blocked'])
        self.assertTrue(page['actionable'])

    def test_a_disabled_source_is_listed_but_offers_no_retry(self):
        dead = self.problem('rss', 'connect')
        self.assertFalse(dead['enabled'])
        self.assertFalse(dead['actionable'])

    def test_a_source_problem_is_not_an_article(self):
        """来源连接问题走自己的四步,**不套文章那四步**,也没有「不再收录」。"""
        self.assertNotEqual(lanes.ITEM_STEPS, sources.SOURCE_STEPS)
        self.assertEqual(4, len(sources.SOURCE_STEPS))
        for name, shape in sources.PROBLEMS.items():
            with self.subTest(problem=name):
                self.assertNotIn('exits', shape)
                self.assertLess(shape['step'], len(sources.SOURCE_STEPS))
        scope = actions.scope_of('web', [], problem=self.problem('web', 'coverage'))
        self.assertEqual('/api/intake/web/sync', scope['endpoint'])
        self.assertEqual(1, scope['picked'])
        self.assertIn('只点名', scope['detail'])

    def test_counting_source_problems_never_raises_for_a_quiet_source(self):
        """合法的空集照旧是空集(元规则:反过来钉一边)。"""
        counts = sources.counts()
        self.assertEqual(2, counts['social'])
        self.assertEqual(1, counts['web'])
        self.assertEqual(1, counts['rss'])
        self.assertEqual(0, counts['youtube'])

class PlanTicketTests(DiagCase):
    """⑥:确认框那张凭证。**它绑整份计划,但不绑展示数字。**

    不绑凭证的后果实测过(审查第 1 轮 F03):`/scope` 和 `/run` 各按「此刻这几条
    在哪一档」重新选路,于是确认框里写着「条目化提取不在这一步」、而等待期间那一
    条补抓完成、变成待翻译,提交时就改走了另一条路、另一份射程 —— 用户同意的是一
    份计划,跑掉的是另一份,**而那一份要花模型的钱**。
    """

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', QUEUED)

    def ticket(self, keys=None, kind='rss') -> str:
        return actions.scope_of(kind, keys or [self.key])['plan']

    def test_an_unchanged_plan_signs_the_same_ticket_twice(self):
        """**反过来钉一边**:没变过的计划不许要求人重看一遍。

        每次重签都变的凭证等于「永远拒绝」—— 那样这颗按钮在正常情况下也点不动,
        而人会学会无脑重试,确认框从此不被阅读。
        """
        self.assertEqual(self.ticket(), self.ticket())

    def test_the_queue_depth_in_the_confirm_text_does_not_invalidate_it(self):
        """展示数字不绑进去:「此刻队列里 N 条」每一轮同步都在变。

        绑进去的话人每次点都要重看一遍一句**没变过**的话。
        """
        first = actions.scope_of('rss', [self.key])
        agg = reg.subscription('rss', self.ids['agg'])
        for i in range(3):
            self.put(agg, f'https://news.example.org/more-{i}',
                     body='标题壳 (\nexample.org\n)\n10-08\n108 HN Points',
                     title=f'又多出来的第 {i} 条')
        second = actions.scope_of('rss', [self.key])
        self.assertNotEqual(first['detail'], second['detail'])   # 那句话真的变了
        self.assertEqual(first['plan'], second['plan'])          # 凭证没变

    def test_an_item_that_changed_lane_while_waiting_signs_a_different_ticket(self):
        """那次事故的主形态:等待期间补抓完成,这一条从正文档跳到翻译档。

        两条路的射程和花的钱都不一样(翻译那一轮按整个范围放回队列、调模型),
        所以凭证必须变 —— 提交那一下会被 409 退回,人重看一次新的那段话。
        """
        key = key_of('rss', 'https://news.example.org/en-shell')
        agg = reg.subscription('rss', self.ids['agg'])
        self.put(agg, 'https://news.example.org/en-shell',
                 body='标题壳 (\nexample.org\n)\n10-08\n108 HN Points',
                 title='An English shell', lang='en')
        before = actions.scope_of('rss', [key])
        self.assertEqual({'body_queued': [key]}, lanes.split([key], 'rss'))
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fulltext_at=?,content_kind='content'"
                      ' WHERE key=?', (reg.now(), key))
        after = actions.scope_of('rss', [key])
        self.assertEqual({'translate_queued': [key]}, lanes.split([key], 'rss'))
        self.assertNotEqual(before['plan'], after['plan'])

    def test_a_recollected_item_signs_a_different_ticket_even_at_the_same_key(self):
        """`item_key` 按网址算:等待期间被清理删掉又从同一个链接收回来的那一条,
        key 一个字都没变而内容是另一份(见 `lanes.identities`)。
        """
        before = self.ticket()
        with reg.db() as c:
            c.execute('DELETE FROM subscription_items WHERE item_key=?', (self.key,))
            c.execute('DELETE FROM feed_items WHERE key=?', (self.key,))
        reg.put_items(reg.subscription('rss', self.ids['agg']),
                      [{'url': QUEUED, 'title': '又收回来的同一个链接',
                        'body': '标题壳 (\nexample.org\n)\n10-08\n108 HN Points',
                        'published': '2026-10-08 09:00', 'content_kind': 'summary'}])
        self.assertNotEqual(before, self.ticket())

    def test_a_new_content_revision_signs_a_different_ticket(self):
        """同一世里内容换过一版也算计划变了 —— 人看过的那段话说的是**那一版**。"""
        before = self.ticket()
        with reg.db() as c:
            c.execute('UPDATE feed_items SET content_rev=content_rev+1 WHERE key=?',
                      (self.key,))
        self.assertNotEqual(before, self.ticket())

    def test_adding_or_dropping_one_item_signs_a_different_ticket(self):
        one = self.ticket()
        two = self.ticket([self.key, key_of('rss', 'https://news.example.org/held')])
        self.assertNotEqual(one, two)
        # 集合相同、顺序不同的算同一份计划(前端勾选顺序不是计划的一部分)。
        pair = [self.key, key_of('rss', 'https://news.example.org/held')]
        self.assertEqual(two, self.ticket(list(reversed(pair))))

    def test_the_route_name_alone_already_separates_two_tickets(self):
        """路由名签在最前面:**别的每一样都一模一样**时两条路也是两张票。

        这里直接按维度构造,不靠两条真路由顺带差出来的别的字段 —— 那样「路由名
        压根没签进去」的实现也是绿的。
        """
        same = ('rss', actions.RUN_STAGES)
        self.assertNotEqual(actions._ticket('body', *same, picked=1),
                            actions._ticket('/api/intake/rss/translate-retry', *same,
                                            picked=1))
        # 两条真路由也确实拿不到对方那张票。
        body = actions.scope_of('rss', [self.key])
        translate_plan = actions.scope_of('rss', [key_of('rss', TRANSLATE)])
        self.assertEqual(body['stages'], translate_plan['stages'])
        self.assertNotEqual(body['plan'], translate_plan['plan'])

    def test_a_wider_paid_range_signs_a_different_ticket(self):
        """翻译那一轮的射程是**整个范围**,不是你点的那一条。

        等待期间范围里多出几条 = 这一轮要花的钱变了,凭证跟着变 ——
        指纹原样取既有端点那一份(`translate.stuck` 的 `sig`)。
        """
        key = key_of('rss', TRANSLATE)
        narrow = {'queued': 1, 'exhausted': 0, 'failed': 0, 'sig': 'sig-1',
                  'shipped': 0}
        wide = {**narrow, 'queued': 9, 'sig': 'sig-2'}
        with patch.object(translate, 'stuck', lambda kind: narrow):
            first = actions.scope_of('rss', [key])
        with patch.object(translate, 'stuck', lambda kind: wide):
            second = actions.scope_of('rss', [key])
        self.assertNotEqual(first['plan'], second['plan'])
        self.assertIn('9 条', second['detail'])

    def test_a_key_from_another_source_is_refused_before_any_ticket_is_signed(self):
        """错 kind 不给票,而且**不是「签一张空票」**:那一批在那一类来源里压根
        不存在,照着跑一轮等于按一个空选集起跑。
        """
        with self.assertRaises(ValueError):
            actions.scope_of('web', [self.key])


class SourceTicketTests(DiagCase):
    """⑥的后半:**来源那一档的凭证绑的是「这条订阅的定义改到第几版」,不是它的编号。**

    序列(审查第 2 轮 R3 实测):确认框弹出时写着「重试来源 A」→ 人在停留期间把这
    条订阅改指到 B → B 也没读完 → 回到那个还开着的确认框点「开始」。`source_id`
    没变、问题类型没变、`problem_id` 逐字相同,于是票对得上,实际重试的是 B,还带着
    「人按的、跳过退避」。

    所以这一组每一条都**把别的每一样都摆成一模一样的**,只让一个维度动 —— 那样
    「顺带靠别的字段差出来」的实现证明不了自己。
    """

    NOTE = '这一轮只读到前 2 页，不在时间线底部；水位没有前移'

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.sid = self.ids['page']

    def problem(self, kind='web', source_id=None) -> dict:
        return next(p for p in sources.problems(kind)
                    if p['source_id'] == (source_id or self.sid))

    def ticket(self, kind='web', source_id=None) -> str:
        return actions.scope_of(kind, [],
                                problem=self.problem(kind, source_id))['plan']

    def edit(self, **data):
        """改这条订阅,并把**同一句**「没读完」重新写回去。

        换链接会连带清掉 `last_note`(`save_subscription` 的 `reset`),而真实序列是
        **改指之后新来源也没读完**:于是 `problem_id`、问题档位、证据原话、承诺的
        下界全都一模一样,票却必须换。
        """
        reg.save_subscription('web', {'name': '网页专栏（覆盖未完成样例）',
                                      'url': 'https://column.example.org/', **data},
                              self.sid)
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET last_note=? WHERE id=?',
                      (self.NOTE, self.sid))

    def test_an_unchanged_source_signs_the_same_ticket_twice(self):
        """**反过来钉一边**:没动过的那一条不许要求人重看一遍。

        每次重签都变的凭证等于「永远拒绝」,而人会学会无脑重试。
        """
        self.assertEqual(self.ticket(), self.ticket())

    def test_a_repointed_source_cannot_reuse_the_old_confirmation(self):
        first, before = self.ticket(), self.problem()
        self.edit(url='https://another.example.org/')
        after = self.problem()
        # 旧那张票认得出的每一样都没变 —— 编号、问题档、证据原话、承诺的下界。
        self.assertEqual(before['id'], after['id'])
        self.assertEqual(before['evidence'], after['evidence'])
        self.assertEqual(before['since'], after['since'])
        self.assertNotEqual(first, self.ticket())

    def test_an_address_changed_back_and_forth_still_signs_a_new_ticket(self):
        """**可复原的输入值不能充当身份**(同 `fulltext.work_version` 那条)。

        地址 A→B→A 之后那个字符串逐字复原 —— 签 `url` 的实现在这里又绿了,而中间
        那一版确实存在过:它清过采集进度、删过 `subscription_items`。代次只往前走。
        """
        first = self.ticket()
        self.edit(url='https://another.example.org/')
        self.edit(url='https://column.example.org/')
        self.assertEqual('https://column.example.org/',
                         reg.subscription('web', self.sid)['url'])
        self.assertNotEqual(first, self.ticket())

    def test_renaming_alone_already_signs_a_new_ticket(self):
        """确认框里念的是名字:「重试来源 A」底下换成了 B,那句话就不是原来那句。"""
        first = self.ticket()
        self.edit(name='改指到另一个专栏')
        self.assertNotEqual(first, self.ticket())

    def test_disabling_it_signs_a_new_ticket_and_takes_the_retry_exit_away(self):
        """停用 = 这一下压根跑不起来(端点 400)。两头一起钉:票换了,出口也没了。"""
        first = self.ticket()
        self.edit(enabled=False)
        self.assertFalse(self.problem()['actionable'])
        self.assertNotEqual(first, self.ticket())

    def test_the_promised_watermark_moving_signs_a_new_ticket(self):
        """**下界由采集进度改,不经 `save_subscription`、因此不动 `rev`。**

        确认框里那句「保留的同步下界是 X」承诺的就是它:只签 `rev` 的实现在这里
        是绿的,而人同意的是「从那个时刻起重读」。
        """
        cover = self.ids['cover']
        first = self.ticket('social', cover)
        was = self.problem('social', cover)
        reg.merge_config(cover, {'x_watermark_browser': '2026-10-20 08:00'})
        now = self.problem('social', cover)
        self.assertEqual(was['rev'], now['rev'])          # 定义没改过一版
        self.assertNotEqual(was['since'], now['since'])   # 承诺的下界真的挪了
        self.assertEqual('2026-10-20 08:00', now['since'])
        self.assertNotEqual(first, self.ticket('social', cover))

    def test_this_rounds_failure_wording_does_not_invalidate_it(self):
        """**刻意不签那一轮的失败原话**:同一条订阅每一轮失败都会重写它。

        签进去等于让人每一轮都重看一句**没变过**的话(口径同 `_ticket` 那条
        「不绑展示数字」)。问题**档位**变了由 `problem_id` 认得出,它已经在路由名里。
        """
        first, before = self.ticket(), self.problem()
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET last_note=? WHERE id=?',
                      ('这一轮只读到前 1 页，不在时间线底部；水位没有前移', self.sid))
        self.assertNotEqual(before['evidence'], self.problem()['evidence'])
        self.assertEqual(first, self.ticket())


class NoExtractAnywhereTests(DiagCase):
    """⑦:结构红 —— 本面板**任何**一条路都不许带 `extract`。

    `extract` 走 `kb.extract.pending_docs(conn)`:**压根没有来源参数**,按全库取
    数、花模型的钱。而这块面板的三条路里有两条是交回既有端点的,那两个端点原本的
    阶段清单里就带着它(审查第 1 轮 F04)—— 照原样交出去,确认框里那句「条目化提
    取不在这一轮」当场变成谎话,而人是照着那句话决定点下去的。

    行为用例证明不了这一条:它要的是「**所有**路都没有」。
    """

    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def test_none_of_the_three_routes_carries_it(self):
        plans = [actions.kind_scope('rss'),
                 actions.scope_of('rss', [key_of('rss', QUEUED)]),
                 actions.scope_of('rss', [key_of('rss', TRANSLATE)]),
                 actions.scope_of('web', [], problem=self.problem())]
        self.assertEqual(4, len(plans))
        for plan in plans:
            with self.subTest(title=plan['title']):
                self.assertEqual([], [one for one in plan['stages']
                                      if one in actions.FORBIDDEN_STAGES])
                self.assertIn('条目化提取不在这一', plan['detail'])
        # 来源那一条多跑一段 `crawl`(它要的就是「再去这个来源读一次」),其余相同。
        self.assertEqual(['crawl', 'export', 'index'], plans[-1]['stages'])

    def test_a_forbidden_stage_blows_up_instead_of_quietly_spending_money(self):
        """真写进去了就当场炸 —— 这是结构错误,不是用户输入错误。"""
        with patch.object(actions, 'RUN_STAGES', ('export', 'index', 'extract')):
            with self.assertRaises(AssertionError) as caught:
                actions.kind_scope('rss')
        self.assertIn('extract', str(caught.exception))

    def problem(self) -> dict:
        found = [one for one in sources.problems('web') if one['actionable']]
        self.assertTrue(found)
        return found[0]
