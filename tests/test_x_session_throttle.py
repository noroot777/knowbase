# -*- coding: utf-8 -*-
"""应用启动请求被限流时,这一个账号当场失败,整批照旧往下走。

**判据为什么落在「读了几次 / 花了多久 / 后面那个账号有没有被碰」**:这一档的
事故形态不是"报错文案难看",是**每个账号都把 `ACCOUNT_SECONDS` 用光**(2026-10-20
实测 24 个账号 × 50 秒正好吃掉整轮 1200 秒预算,余下 27 个连试都没试上,整轮
0 条)。所以断言必须落在"它多快结束""剩下的账号还抓不抓",光断言异常类型
是验过程不验后果 —— 判据写错成"等满超时再抛同一个异常"照样能让那种用例全绿。

**第一条必须从真正的 CDP 事件出发**(`EventSession` + `Network.responseReceived`),
不许手写一份带 `session_limited` 的快照:这次的缺陷恰恰在于 `Viewer` 压根不在
`Responses` 的监听名单里,证据一条都没留下。手写快照等于把那一步假设成已经做对,
真缺陷注入进去也不会红。

**「不停整批」要配一道显式冷却**,不然「少白等」就变成「更快地向同一个受限
接口连发」:识别得快了之后一个账号不到一秒就失败,整批会以这个速度把余下几十个
号全部试完、全部失败(离线模拟:限流 30 秒结束的一轮,51 次导航 15.3 秒试完、
新增 0 条)。而「同一轮里它自己会恢复」这个结论本来是在**有**那段白等的前提下
观察到的 —— 所以 `CooldownTests` 的判据落在「这一轮到底抓到几个号」。

**排在错误里最后一位不等于排在成功后面**:429 在一次导航里是粘的,所以「这次
导航见过一个 429」不能当成「应用没启动」。`BootedPagesSurviveTests` 钉的就是这
一边 —— 内容接口 200、页面已经有推文的那种页面要走 `ok`,不许被这一档否决、
也不许因此冻结水位。

**反过来钉的那几条**:启动请求 200 照旧什么结论都不产生;单次 429 不终局;
什么证据都没有时这一档照旧成立;安全验证和内容请求限流仍然优先、仍然整批中止
—— 它们和这一档的分界就是「要不要停掉余下的账号」,糊在一起的代价是把一轮本来
能抓到的内容整批丢掉。
"""
import contextlib
import inspect
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from kb.feeds import (x_article, x_browser, x_cooldown, x_page_state, x_post,
                     x_timeline_state)
from kb.feeds.fetching import FeedError
from kb.feeds.x_timeline_state import TimelineState, wait_state
from tests.test_x_collectors import tweet
from tests.test_x_page_state import EventSession, response
from tests.test_x_article import _FakeChrome, answering
from tests.test_x_article import entry as article_entry
from tests.test_x_article import snapshot as article_snapshot
from tests.test_x_post import OLD, Page, preview, snapshot


def throttled(operation='Viewer', status=429):
    """一条被限流的启动请求。"""
    return response(status, operation=operation)


class Clock:
    """虚拟时钟:`sleep` 直接推进时间。

    整模块换掉 `x_browser.time` / `x_cooldown.time` 这两个**名字**,不去 patch
    标准库 `time` 自己的属性 —— 后者是全进程生效的。
    """

    START = 1000.0

    def __init__(self):
        self.now = self.START

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    @property
    def elapsed(self):
        return self.now - self.START


class TimelineSessionThrottleTests(unittest.TestCase):
    """采集那一侧:判据认不认得出来,以及它多快结束。"""

    def wait(self, session, handle='Example', budget=5.0):
        x_page_state.navigate(session, f'https://x.com/{handle}')
        return wait_state(session, handle, time.monotonic() + budget, x_browser._STATE_JS)

    def test_throttled_boot_request_fails_this_account_in_two_reads_not_a_whole_budget(self):
        session = EventSession([throttled()])
        started = time.monotonic()
        with patch.object(x_timeline_state.time, 'sleep'), \
             self.assertRaises(x_page_state.XSessionThrottled) as caught:
            self.wait(session, budget=x_browser.ACCOUNT_SECONDS)
        self.assertEqual(2, session.reads)
        self.assertLess(time.monotonic() - started, 1)
        self.assertNotIsInstance(caught.exception, x_page_state.XRateLimited)
        self.assertNotIsInstance(caught.exception, x_page_state.XAccessBlocked)

    def test_the_message_names_throttling_and_does_not_send_anyone_to_log_in_again(self):
        session = EventSession([throttled()])
        with patch.object(x_timeline_state.time, 'sleep'), \
             self.assertRaises(x_page_state.XSessionThrottled) as caught:
            self.wait(session)
        message = str(caught.exception)
        self.assertIn('限制了访问频率', message)
        self.assertIn('不必重新登录', message)
        self.assertNotIn('安全验证,本轮已停止', message)
        self.assertNotIn('未能判出原因', message)

    def test_a_successful_boot_request_produces_no_verdict_at_all(self):
        for status in [200, 204]:
            with self.subTest(status=status):
                state = x_page_state.Responses('owned')
                state.reset('https://x.com/Example')
                state.loader = 'current'
                state.receive(throttled(status=status))
                snap = state.apply(dict(ready='complete', articles=0))
                self.assertEqual({'Viewer': status}, snap.get('http_session'))
                self.assertNotIn('session_limited', snap)
                self.assertEqual('', x_page_state.problem(snap))
                self.assertEqual('', x_page_state.terminal(snap, ''))

    def test_a_single_observation_is_not_terminal_and_an_unfinished_load_is_not_either(self):
        snap = x_page_state.Responses('owned')
        snap.reset('https://x.com/Example')
        snap.loader = 'current'
        snap.receive(throttled())
        limited = snap.apply(dict(ready='complete', articles=0))
        self.assertEqual('session_limited', x_page_state.problem(limited))
        self.assertEqual('', x_page_state.terminal(limited, ''),
                         'one observation must not be terminal')
        loading = snap.apply(dict(ready='loading', articles=0))
        self.assertEqual('', x_page_state.problem(loading),
                         'a boot request still in flight is not a verdict')

    def test_boot_throttling_never_outranks_a_gate_or_a_throttled_content_request(self):
        for extra, expected, error in [
                (dict(challenge=True), 'challenge', x_page_state.XAccessBlocked),
                (dict(http_content_limited=True, limited=True), 'limited',
                 x_page_state.XRateLimited)]:
            with self.subTest(expected=expected):
                state = x_page_state.Responses('owned')
                state.reset('https://x.com/Example')
                state.loader = 'current'
                state.receive(throttled())
                snap = state.apply(dict(ready='complete', articles=0) | extra)
                self.assertEqual(expected, x_page_state.problem(snap))
                raised = x_page_state.escalate(snap, expected, RuntimeError('unused'))
                self.assertIsInstance(raised, error)


class BatchContinuesTests(unittest.TestCase):
    """批次那一侧:这一档和安全验证/限流的分界就是「停不停余下的账号」。"""

    def harvest(self, error, jobs=(('First', OLD), ('Second', OLD)), **kwargs):
        page = Page(snapshot(''))
        clock = Clock()
        calls = []

        def one(_page, handle, _floor):
            calls.append(handle)
            if handle == 'First':
                raise error
            return [], ''

        with patch.object(x_browser, 'time', clock), \
             patch.object(x_cooldown, 'time', clock), \
             patch.object(x_browser, '_harvest_one', side_effect=one), \
             patch.object(x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(page)):
            return calls, clock, x_browser.harvest(list(jobs), profile='', **kwargs)

    def test_a_throttled_boot_fails_only_its_own_account_and_the_batch_keeps_going(self):
        calls, clock, results = self.harvest(x_page_state.session_throttled())
        self.assertEqual(['First', 'Second', 'First'], calls,
                         'the next account must still be attempted, and the throttled '
                         'one must get one more try at the back of the queue')
        self.assertGreaterEqual(clock.elapsed, x_cooldown.FIRST_WAIT,
                                'the next navigation must not start immediately')
        self.assertIn('限制了访问频率', results['First']['error'])
        self.assertEqual([], results['First']['entries'])
        self.assertNotIn('watermark', results['First'],
                         'a throttled account must not advance its watermark')
        self.assertEqual('', results['Second']['error'])

    def test_a_verified_gate_still_stops_the_batch_so_the_two_stay_distinguishable(self):
        calls, _clock, results = self.harvest(x_page_state.access_blocked())
        self.assertEqual(['First'], calls, 'a gate must not touch later accounts')
        for handle in ['First', 'Second']:
            self.assertIn('安全验证', results[handle]['error'])
            self.assertNotIn('watermark', results[handle])


class BootedPagesSurviveTests(unittest.TestCase):
    """启动请求 429 不许否决**已经可用**的页面。

    429 在一次导航里是粘的(`statuses` 整段保留到下一次 `reset`),所以「这次导航
    见过一个 429」不等于「应用没启动」—— 同一次导航里启动请求被限一次、而内容
    接口 200 且页面已经渲染出推文,是能同时成立的。

    **排在错误里最后一位不等于排在成功后面**:`terminal` 的成功分支要求
    `not problem(state)`,漏了这一道的话这种页面永远到不了 `ok`,「检查连接」
    报失败、同步还会把这个账号的内容扔掉并冻结水位。
    """

    def state(self, url='https://x.com/Example', content='UserTweets', **snap):
        responses = x_page_state.Responses('owned')
        responses.reset(url)
        responses.loader = 'current'
        responses.receive(throttled())
        if content:
            responses.receive(throttled(operation=content, status=200))
        return responses.apply(dict(ready='complete', **snap))

    def test_a_timeline_that_rendered_tweets_reads_ok_instead_of_being_thrown_away(self):
        session = EventSession([throttled(), throttled(operation='UserTweets', status=200)],
                               available=True)
        x_page_state.navigate(session, 'https://x.com/Example')
        last = wait_state(session, 'Example', time.monotonic() + 5, x_browser._STATE_JS)
        self.assertTrue(last['articles'])
        self.assertEqual({'Viewer': 429}, last['http_session'],
                         'the refusal stays on record, it just is not decisive')
        self.assertEqual('', x_page_state.problem(last))

    def test_the_connection_check_still_passes_when_the_home_timeline_came_back(self):
        session = EventSession([throttled(), throttled(operation='HomeTimeline', status=200)],
                               available=True)
        x_page_state.navigate(session, 'https://x.com/home')
        verdict, _ = x_browser.verify_timeline(session, timeout=5.0)
        self.assertEqual('ok', verdict)

    def test_each_kind_of_proof_that_the_app_started_lifts_the_verdict_on_its_own(self):
        """四种证据各自单独成立 —— 合成一条的话漏掉哪一种都看不出来。"""
        for snap in [dict(articles=2), dict(empty=True), dict(text='正文')]:
            with self.subTest(**snap):
                state = self.state(content='', **snap)
                self.assertTrue(state['session_limited'])
                self.assertTrue(x_page_state.booted(state))
                self.assertEqual('', x_page_state.problem(state))
        # 这条路线自己的内容请求答复过也算:到得了这一档的地方它一定是 2xx
        # (429 和 4xx/5xx 在 `problem` 里更早就返回了)。
        answered = self.state(articles=0)
        self.assertEqual({'UserTweets': 200}, answered['http_content'])
        self.assertEqual('', x_page_state.problem(answered))

    def test_with_nothing_to_show_the_app_ever_started_the_verdict_still_stands(self):
        """反过来钉住:**真**卡在启动闪屏的那一种不许被这道闸放过去。"""
        bare = self.state(content='', articles=0)
        self.assertFalse(x_page_state.booted(bare))
        self.assertEqual('session_limited', x_page_state.problem(bare))
        self.assertEqual('session_limited', x_page_state.terminal(bare, 'session_limited'))

    def test_a_late_boot_429_does_not_freeze_the_watermark_of_a_finished_account(self):
        """迟到响应那条路(`drain`)同走一份判据。

        **后果是水位**:冻结之后这一段要等下一轮才补,而把它误报成「没读完」
        的页面其实读完了。判据落在 `result` 里那两个键上,不是异常类型。
        """
        responses = x_page_state.Responses('owned')
        responses.reset('https://x.com/Example')
        responses.loader = 'current'
        responses.receive(throttled(operation='UserTweets', status=200))
        responses.receive(throttled())
        timeline = TimelineState(Page(), 'Example', dict(articles=2, ready='complete'),
                                 x_browser._STATE_JS)
        timeline.result = dict(entries=[dict(url='u')], error='', incomplete='', watermark='new')
        timeline.watermark = OLD
        timeline.drain(responses)
        self.assertEqual('', timeline.result['incomplete'])
        self.assertEqual('new', timeline.result['watermark'])


class CooldownTests(unittest.TestCase):
    """「不停整批」要配一道显式冷却,否则「少白等」变成「更快地连发」。

    **判据落在「这一轮到底抓到几个号」**,不是等了多少秒:识别得快了之后一个
    账号不到一秒就失败,不冷却的话整批会以这个速度全部试完、全部失败,而整轮
    预算还剩一大半(审查给的离线模拟:51 次导航 15.3 秒试完、新增 0 条)。
    """

    def test_each_hit_pushes_the_next_navigation_back_and_the_wait_grows_to_a_cap(self):
        clock = Clock()
        waits = []
        with patch.object(x_cooldown, 'time', clock):
            cool = x_cooldown.SessionCooldown(first=10.0, maximum=30.0)
            for _ in range(4):
                cool.throttled()
                before = clock.now
                cool.wait()
                waits.append(clock.now - before)
        self.assertEqual([10.0, 20.0, 30.0, 30.0], waits)

    def test_an_account_that_read_cleanly_resets_the_backoff(self):
        """不回退的话,早先那一次会把后面无关的一次限流直接罚成封顶那么久。"""
        clock = Clock()
        with patch.object(x_cooldown, 'time', clock):
            cool = x_cooldown.SessionCooldown(first=10.0, maximum=30.0)
            for _ in range(2):
                cool.throttled()
                cool.wait()
            cool.cleared()
            cool.throttled()
            before = clock.now
            cool.wait()
        self.assertEqual(10.0, clock.now - before)

    def test_a_stop_request_cuts_the_wait_short_instead_of_sleeping_it_out(self):
        clock = Clock()
        with patch.object(x_cooldown, 'time', clock):
            cool = x_cooldown.SessionCooldown(first=60.0)
            cool.throttled()
            cool.wait(should_stop=lambda: True)
        self.assertEqual(0.0, clock.elapsed, 'a pressed stop must not wait out the cooldown')

    def test_the_wait_never_runs_past_the_whole_round_budget(self):
        """等过预算换不来任何内容 —— 预算闸会把余下的号判成「留待下次同步」。"""
        clock = Clock()
        with patch.object(x_cooldown, 'time', clock):
            cool = x_cooldown.SessionCooldown(first=600.0)
            cool.throttled()
            cool.wait(deadline=clock.now + 5.0)
        self.assertAlmostEqual(5.0, clock.elapsed, places=6)

    def batch(self, clears_after, jobs=8, budget=1200.0):
        clock = Clock()
        page = Page(snapshot(''))
        attempts = []
        clears_at = clock.now + clears_after

        def one(_page, handle, _floor):
            attempts.append((handle, clock.now))
            if clock.now < clears_at:
                raise x_page_state.session_throttled()
            return [], ''

        with patch.object(x_browser, 'time', clock), \
             patch.object(x_cooldown, 'time', clock), \
             patch.object(x_browser, '_harvest_one', side_effect=one), \
             patch.object(x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(page)):
            results = x_browser.harvest([(f'A{n}', OLD) for n in range(jobs)],
                                        profile='', budget=budget)
        return attempts, clears_at, results

    def test_a_throttle_that_clears_mid_round_still_gets_the_whole_batch(self):
        attempts, clears_at, results = self.batch(clears_after=100.0)
        failed = sorted(h for h, r in results.items() if r.get('error'))
        self.assertEqual([], failed, '限流过去之后每个号都该读到,包括排到队尾重试的')
        blind = [handle for handle, when in attempts if when < clears_at]
        self.assertLess(len(blind), 8,
                        '不许在限流还没过去的时候就把整批试完')

    def test_a_throttle_that_never_clears_tries_each_account_at_most_twice(self):
        """重排有上限,而且**一个水位都不许推进**。"""
        attempts, _clears_at, results = self.batch(clears_after=10 ** 6, budget=0.0)
        tries = {handle: sum(1 for h, _ in attempts if h == handle) for handle, _ in
                 [(f'A{n}', OLD) for n in range(8)]}
        self.assertEqual({2}, set(tries.values()))
        for handle, result in results.items():
            self.assertIn('限制了访问频率', result['error'], handle)
            self.assertNotIn('watermark', result, handle)

    def test_an_account_that_read_cleanly_resets_the_backoff_inside_the_real_batch(self):
        """`cleared()` 要真的接在批循环里。

        **后果是「紧接着那一次撞上要等多久」**:中间那个号读干净了,下一次限流
        该按起点等,不该沿用上一次翻倍后的值。只钉 `SessionCooldown` 自己钉不住
        这一条 —— 类上有这个方法、调度层压根没调它,那一组照样全绿。
        """
        clock = Clock()
        page = Page(snapshot(''))
        attempts = []

        def one(_page, handle, _floor):
            attempts.append((handle, clock.now))
            if handle.startswith('Bad'):
                raise x_page_state.session_throttled()
            return [], ''

        with patch.object(x_browser, 'time', clock), \
             patch.object(x_cooldown, 'time', clock), \
             patch.object(x_browser, '_harvest_one', side_effect=one), \
             patch.object(x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(page)):
            x_browser.harvest([('Bad1', OLD), ('Good', OLD), ('Bad2', OLD)], profile='')
        self.assertEqual(['Bad1', 'Good', 'Bad2', 'Bad1', 'Bad2'],
                         [handle for handle, _ in attempts])
        self.assertEqual(x_cooldown.FIRST_WAIT, attempts[3][1] - attempts[2][1],
                         'the hit right after a clean read must wait from the floor again')

    def test_body_completion_throttling_also_holds_the_next_account_back(self):
        """正文补全那条路是**暂缓不抛**的,外层看不见异常。

        所以它必须自己把这一次记到会话上 —— 否则下一个号会不冷却就接着导航,
        而这正是「同类范围」里按代码推断的那一处。
        """
        session = Page()
        clock = Clock()
        with patch.object(x_cooldown, 'time', clock), \
             patch.object(x_post, 'fetch', side_effect=x_page_state.session_throttled()):
            x_post.fill([preview()], session=session)
            held = x_cooldown.of(session).wait()
        self.assertEqual(1, x_cooldown.of(session).hits)
        self.assertEqual(x_cooldown.FIRST_WAIT, clock.elapsed)
        self.assertIn('限制了访问频率', held)


class DetailThrottleTests(unittest.TestCase):
    """正文补全撞上它时,已经抓到的时间线条目要留住。"""

    def test_throttling_during_body_completion_defers_instead_of_failing_the_account(self):
        """**后果是「这个账号其余的推文还在不在」**,不是异常类型。

        折叠推文本身照旧不入库 —— 正文不全就落库的话 `put_items` 的 DO NOTHING
        再也补不回来(见 `x_article`)。要钉的是它**没有穿到 `harvest` 去**:
        穿出去就是整个账号这一轮的收获连同水位一起作废。
        """
        short = x_browser._entry(tweet('124', text='short'))
        folded = preview()
        with patch.object(x_post, 'fetch', side_effect=x_page_state.session_throttled()):
            kept, note = x_post.fill([short, folded, folded], session=Page())
        self.assertEqual([short], kept, 'posts needing no completion must survive')
        self.assertIn('暂缓补全', note)
        self.assertIn('限制了访问频率', note)


class NavigationBoundaryTests(unittest.TestCase):
    """冷却要挡住**每一次新开页面**,不是只挡账号与账号之间那一次。

    一个账号内部还会导航很多次:折叠推文的详情页、每一篇长文的文章页。正文补全
    撞上限流之后是**暂缓不抛**的,`x_post.complete` 紧接着把余下条目交给
    `x_article.fill` —— 长文接口关掉、或者接口失败转浏览器时,那条后路直接
    `session.goto()`,既不看也不等刚刚登记下来的冷却。**账号边界不等于导航边界。**

    **判据是「有没有新开页面」**(`_FakeChrome.visited`),不是异常类型:报错文案
    换一个照样绕过去,而真实代价是在 X 刚说「慢一点」的时候继续连着敲它。

    **扣留而不是在这里睡**:睡下去花的是整轮预取的预算,换来的只是一篇下一轮就能
    免费补上的长文正文。扣留不推进水位,下一轮重叠重抓。
    """

    def setUp(self):
        _FakeChrome.reset(article_snapshot(blocks=['正文第一段。']))
        self.addCleanup(_FakeChrome.reset, article_snapshot(blocks=['正文第一段。']))
        self.session = _FakeChrome()

    def cooled(self):
        """把这个会话置成「刚撞上启动限流」。"""
        x_cooldown.of(self.session).throttled()
        return self.session

    def entries(self, count=3):
        return [article_entry(url=f'https://x.com/a/status/{i}') for i in range(count)]

    def test_the_long_form_fallback_opens_nothing_while_the_session_is_cooling(self):
        """接口整个关掉那条分支(`KB_X_ARTICLE_API=''`)。"""
        session = self.cooled()
        with patch.object(x_article, 'API', ''), \
             patch.object(x_article, 'PAGE_WAIT', 0.0):
            kept, note = x_article.fill(self.entries(), session=session)
        self.assertEqual([], _FakeChrome.visited, '冷却期间一个长文页都不许开')
        self.assertEqual([], kept, '补不到的那几条本轮不入库')
        self.assertIn('留到下次同步再试', note, '要让调用方把水位留在原处')

    def test_the_endpoint_failure_fallback_also_opens_nothing_while_cooling(self):
        """接口答不上来、转浏览器那条分支 —— 和上面那条是两处独立的调用点。"""
        session = self.cooled()
        request, calls = answering(*[FeedError('down')] * 3)
        with request, patch.object(x_article, 'PAGE_WAIT', 0.0):
            kept, note = x_article.fill(self.entries(), session=session)
        self.assertEqual([], _FakeChrome.visited, '冷却期间一个长文页都不许开')
        self.assertTrue(calls.urls, '第三方长文接口不归这道闸管,照旧要问')
        self.assertEqual([], kept)
        self.assertIn('留到下次同步再试', note)

    def test_the_whole_completion_chain_stops_navigating_once_the_body_step_is_throttled(self):
        """审查复现的那条真实链:`complete` → `fill` 登记限流 → `x_article.fill`。

        **后果是「登记之后还开了几张页」**。折叠推文那一条暂缓、长文那几条扣留,
        都不入库、都不推进水位 —— 而整个过程里一次新导航都不该发生。
        """
        session = self.cooled()
        folded = preview()
        longform = self.entries(2)
        with patch.object(x_article, 'API', ''), \
             patch.object(x_article, 'PAGE_WAIT', 0.0), \
             patch.object(x_post, 'fetch', side_effect=x_page_state.session_throttled()):
            kept, note = x_post.complete([folded, *longform], session=session)
        self.assertEqual([], _FakeChrome.visited,
                         'nothing may be opened after the throttle is on record')
        self.assertEqual([], kept)
        self.assertIn('暂缓补全', note)

    def test_once_the_cooldown_is_over_the_fallback_works_again(self):
        """**反过来钉一边**:闸只在冷却里关着,过去了照旧要把长文读回来 ——
        否则这道保护就成了「撞过一次限流就再也不补长文」。"""
        clock = Clock()
        with patch.object(x_cooldown, 'time', clock):
            session = self.cooled()
            x_cooldown.of(session).wait()
            self.assertEqual('', x_cooldown.cooling(session))
            with patch.object(x_article, 'API', ''), \
                 patch.object(x_article, 'PAGE_WAIT', 0.0):
                kept, note = x_article.fill(self.entries(1), session=session)
        self.assertEqual(1, len(_FakeChrome.visited), '冷却过去就该照常开页')
        self.assertEqual('', note)
        self.assertIn('正文第一段', kept[0]['body'])

    def test_a_session_that_never_hit_the_throttle_is_not_held_back(self):
        """没撞过的会话一点都不受影响(feedgrab 那条路连会话都没有)。"""
        self.assertEqual('', x_cooldown.cooling(_FakeChrome()))
        self.assertEqual('', x_cooldown.cooling(None))

    def test_asking_never_writes_anything_onto_the_session(self):
        """**查询不许改东西。**

        这一步会被形形色色的会话对象调到 —— `x_post_provenance` 那条存量修复链
        传下来的就是一个**裸 `object()`**,挂不住属性。顺手给它建一道闸会当场
        AttributeError 把整条修复链带掉(全量回归实测 6 条红),而本地那几个
        假会话都是普通类、挂得住,所以单看那几组是看不出来的。
        """
        bare = object()
        self.assertEqual('', x_cooldown.cooling(bare))
        self.assertFalse(hasattr(bare, '_x_cooldown'))
        fake = _FakeChrome()
        x_cooldown.cooling(fake)
        self.assertIsNone(getattr(fake, '_x_cooldown', None),
                          'a plain query must not create the gate either')


class ProgressShapeTests(unittest.TestCase):
    """整批里要报的不止「第几个账号」,还有几句话 —— 回调两种形状都得接。"""

    def callback(self):
        """把管道真正交给 `harvest` 的那个回调取出来。

        **判据必须取生产那一个,不能自己写一个宽松的桩** —— 这次的缺陷恰恰是
        它只接三参,而自己写的桩爱接几个接几个,注入进去也不会红。
        """
        from kb.feeds import sync
        taken = {}

        def prefetch(_sources, _settings, **kwargs):
            taken['on_progress'] = kwargs['on_progress']

        scope = SimpleNamespace(ranged=False, since='', until='')
        with patch.object(sync, '_stage'), \
             patch.object(sync.x_channels, 'reset'), \
             patch.object(sync.x_channels, 'supports_channels', return_value=True), \
             patch.object(sync.x_channels, 'targets', return_value=[{'id': 1}]), \
             patch.object(sync.x_channels, 'channel_of', return_value='browser'), \
             patch.object(sync.x_channels, 'label', return_value='独立 profile'), \
             patch.object(sync.x_channels, 'prefetch', side_effect=prefetch):
            sync._prefetch_x('social', [{'id': 1}], {}, scope)
        return taken['on_progress']

    def test_the_pipeline_callback_takes_a_bare_sentence_as_well_as_an_account_count(self):
        """冷却提示是单参的,直接调三参回调会 TypeError 把整条大V采集带掉。

        而面板上只看得到「采集失败」,指不到任何跟业务有关的东西 —— 同一个形状
        「关掉到点的登录窗口」那句话早就在用了。
        """
        progress = self.callback()
        progress(3, 51, 'karpathy')
        progress('X 限制了访问频率(本轮第 1 次),等 45 秒再试下一个账号')

    def test_the_reason_is_announced_before_the_wait_not_after_it(self):
        """**等完再说等于没说。**

        这一档要等 45~180 秒,而面板上那行字在这期间停在上一个账号的进度上 ——
        看起来正是用户来问的那句「又卡住了」。何况等完说的那一句紧接着就被下一个
        账号的进度盖掉,页面轮询基本抓不到。所以判据是**说那句话时表走到哪儿了**,
        不是「有没有说过」。
        """
        clock = Clock()
        said = []
        with patch.object(x_cooldown, 'time', clock):
            cool = x_cooldown.SessionCooldown(first=60.0)
            cool.throttled()
            cool.wait(announce=lambda note: said.append((clock.now, note)))
        self.assertEqual(1, len(said))
        when, note = said[0]
        self.assertEqual(Clock.START, when, 'the reason must be out before the first sleep')
        self.assertIn('限制了访问频率', note)
        self.assertEqual(60.0, clock.elapsed, 'and it must still wait afterwards')

    def test_nothing_is_announced_when_there_is_nothing_to_wait_for(self):
        """反过来钉一边:不用等的时候不许在面板上刷一句没来由的话。"""
        said = []
        x_cooldown.SessionCooldown().wait(announce=said.append)
        self.assertEqual([], said)

    def test_the_cooldown_note_reaches_that_callback_without_blowing_up(self):
        """反过来从 `harvest` 那一侧钉一次:提示真的是这么发出去的。"""
        progress = self.callback()
        clock = Clock()
        page = Page(snapshot(''))
        notes = []
        original = progress

        def spy(*args):
            notes.append(args)
            original(*args)

        def one(_page, handle, _floor):
            raise x_page_state.session_throttled()

        with patch.object(x_browser, 'time', clock), \
             patch.object(x_cooldown, 'time', clock), \
             patch.object(x_browser, '_harvest_one', side_effect=one), \
             patch.object(x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(page)):
            x_browser.harvest([('A', OLD), ('B', OLD)], profile='', on_progress=spy)
        self.assertTrue([n for n in notes if len(n) == 1 and '限制了访问频率' in n[0]],
                        'the cooldown must say so on the panel instead of looking hung')


class OtherConsumersTests(unittest.TestCase):
    """硬约束「不是只有采集那一侧要认」—— 检查连接和导入登录态都得认得出来。"""

    def test_the_connection_check_reports_throttling_instead_of_a_stale_login(self):
        session = EventSession([throttled()])
        x_page_state.navigate(session, 'https://x.com/home')
        with patch.object(x_browser.time, 'sleep'):
            verdict, _ = x_browser.verify_timeline(session, timeout=5.0)
        self.assertEqual('session_limited', verdict,
                         'the check must not fall through to the relogin wording')

    def test_every_verdict_the_timeline_can_return_has_wording_for_the_import_path(self):
        """**每个结论码都要在两处都有落点**,漏一个就退回含糊文案。

        两处是故意分开钉的:`_VERDICT_TAIL` 是那句话本身,而「重新打开之后那次
        验证的原因」另有一张白名单 —— 只补前者的话,结论码会落回 `unconfirmed`
        那句「X 请求出错、限流,或者首页没加载完」,把已经查明的原因重新说成三选一。
        """
        from kb.feeds import x_login_words
        for code in ['ok', 'login', 'limited', 'session_limited', 'challenge',
                     'failed', 'not-loaded', 'no-tweets']:
            with self.subTest(code=code):
                self.assertIn(code, x_login_words._VERDICT_TAIL)
        # 白名单装的是「**没得出登录态结论**的那几种」,所以 `login` 不在里面
        # (它得出了结论:X 判定未登录),`ok` 同理。启动限流和它旁边那几个同类。
        source = inspect.getsource(x_login_words._detail)
        for code in ['limited', 'session_limited', 'challenge', 'failed',
                     'not-loaded', 'no-tweets']:
            with self.subTest(code=code, place='reopened-failure roster'):
                self.assertIn(f"'{code}'", source,
                              f'{code} missing from the reopened-failure roster')

    def test_the_throttling_wording_does_not_send_anyone_to_log_in_or_to_the_gate(self):
        from kb.feeds.x_login_words import _VERDICT_TAIL
        tail = _VERDICT_TAIL['session_limited']
        self.assertIn('限制了访问频率', tail)
        self.assertNotIn('建议改用', tail, 'must not send anyone back to log in again')
        self.assertNotIn('account/access', tail, 'must not send anyone to the gate page')

    def test_the_throttling_wording_does_not_claim_the_login_state_is_fine(self):
        """这一档的意思恰恰是**这一次没验证成**,不能据此断言已有 cookie 还有效。

        口径同 `probe`:「这次无法验证,不必只因为这个就重新登录」。往前多说一句
        「登录态本身没问题」是没有依据的正面断言 —— 真失效了的人会照着这句话
        放过一个该重登的提示。
        """
        from kb.feeds.x_login_words import _VERDICT_TAIL
        tail = _VERDICT_TAIL['session_limited']
        self.assertIn('没验证成', tail, 'it must admit the verification did not complete')
        self.assertNotIn('没问题', tail, 'it must not assert the login state is still good')


if __name__ == '__main__':
    unittest.main()
