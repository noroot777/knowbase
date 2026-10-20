# -*- coding: utf-8 -*-
"""「从日常浏览器导入 X 登录态」那条链(`kb/feeds/x_login_import.py`)。

**为什么另拆一个文件**:`tests/test_x_channels.py` 627 行、`test_x_attach_channel.py`
293 行都贴着 700 上限,理由同那次按形状拆分。摆盘共用 `tests/x_login_fixture.py`
(它自己也是顶到 717 行之后拆出去的)—— 各写一份的话几边的前提会各漂各的。

这里钉的是这条链**最贵的那几个错**:
- 射程放宽一格,就是把别的站点的登录态一起搬走(银行、邮箱都在同一个浏览器里);
- 缺了关键 cookie 还往 profile 里写,就是用一份残缺的登录态**盖掉**原来那份好的,
  而 `readiness` 看到 Cookies 文件照旧报「登录过」;
- 把「写进去了」当成「可以用了」,下一个发现真相的时刻是第二天早上那一轮同步;
- cookie 值落进明文 JSON,等于把这个功能防的事自己做一遍。

`probe` 那一组钉的是另一件事:`verify_timeline` 是从它里面抽出来的,**抽完每一档文案
一个字都不许变**(元规则「搬代码要逐符号对过原文」)—— 所以那一组驱动的是真的
`verify_timeline`,不是打桩的。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from kb.feeds import cdp_attach, x_browser, x_channels, x_login_import
from kb.feeds.x_cdp import CdpError
# 摆盘在 `x_login_fixture`(三组用例共用,单拆只因为 700 行上限)。
from tests.x_login_fixture import (ATTACH, BROWSER, ImportBase, cookie, logged_in)
from tests.x_page_state import page_state


class ScopeTests(unittest.TestCase):
    """射程。**这一组是这个模块存在的前提** —— 放宽一格就是搬走别的站点的登录态。"""

    def test_it_takes_the_authorised_domain_and_its_subdomains(self):
        for domain in ('x.com', '.x.com', 'api.x.com', '.api.x.com', 'X.COM'):
            self.assertTrue(x_login_import.in_scope(domain), domain)

    def test_a_lookalike_suffix_is_not_in_scope(self):
        """**反过来钉一边。** `endswith('x.com')` 会把这几个一起圈进来,而它们是
        别人的站点 —— 这正是这条判据存在的全部理由。
        """
        for domain in ('notx.com', 'evilx.com', '.myx.com', 'x.com.evil.com',
                       'example.org', '', 'xx.com'):
            self.assertFalse(x_login_import.in_scope(domain), domain)

    def test_the_scope_is_one_authorised_domain_only(self):
        """射程写死在一处,而且只有 x.com。扩域要用户点名(见模块注释)。"""
        self.assertEqual(('x.com',), x_login_import.SCOPE)


class ReadTests(ImportBase):
    def test_only_cookies_in_scope_leave_the_daily_browser(self):
        """别的站点那几条**在读的这一步就被摘掉**,压根不进后面任何一步。"""
        browser, _ = self.wire(cookies=[
            cookie('auth_token'), cookie('ct0'),
            cookie('SESSION', domain='.bank.example'),
            cookie('sid', domain='mail.example.org'),
            cookie('spoof', domain='evilx.com')])
        got = x_login_import.read_from_daily_browser()
        self.assertEqual({'auth_token', 'ct0'}, {c['name'] for c in got})

    def test_the_browser_is_asked_only_for_the_authorised_site(self):
        """**范围由浏览器卡住,不由我们过滤。** 这一条是这个模块最贵的那个保证。

        原来这儿读的是 browser 级的 `Storage.getCookies` —— 它**没有**任何按站点
        筛选的参数(唯一的可选参数是 `browserContextId`,按 profile 分),所以那一
        条命令会把整个浏览器里所有站点已经解密好的 cookie 交到我们进程里:银行、
        邮箱、公司后台都在内。之后 `in_scope` 再怎么过滤,也只能证明「我们没有
        **保存**授权外的数据」,证不了「没有**读取**」—— 而页面上对用户说的是
        「只读 x.com」。所以读取改成页面级 `Network.getCookies` + 显式 `urls`,
        让浏览器在交出来之前就把范围卡掉。

        **反过来钉一边**:同一个夹具在 `urls` 没给的时候照真浏览器的语义把整杯
        端出来,所以这一条不是在验夹具听话,是在验我们确实带上了 `urls`。
        """
        browser, _ = self.wire(cookies=[
            cookie('auth_token'), cookie('ct0'),
            cookie('SESSION', domain='.bank.example'),
            cookie('sid', domain='mail.example.org')])
        got = x_login_import.read_from_daily_browser()
        self.assertEqual({'auth_token', 'ct0'}, {c['name'] for c in got})
        self.assertIn('Network.getCookies', browser.methods)
        self.assertNotIn('Storage.getCookies', browser.methods)
        urls = dict(browser.calls)['Network.getCookies']['urls']
        self.assertTrue(urls and all('x.com' in u for u in urls), urls)
        self.assertEqual(1, browser.exited)       # 连接照旧关掉

    def test_it_opens_exactly_one_blank_tab_and_touches_nothing_else(self):
        """在别人的浏览器里干活的卫生规矩。**范围那条约束赢了「一张标签页都不开」
        那条**(那是我们自己定的,而数据范围是用户定的),但余下的规矩一条不松:
        只开一张、只开 `about:blank`(一个网络请求都不发)、不列不碰他已有的标签页、
        绝不 `Browser.close`(那是他正在用的浏览器)。
        """
        browser, _ = self.wire()
        x_login_import.read_from_daily_browser()
        self.assertEqual(['about:blank'], browser.pages)
        for forbidden in ('Target.getTargets', 'Target.setDiscoverTargets',
                          'Browser.close', 'Page.navigate'):
            self.assertNotIn(forbidden, browser.methods)

    def test_a_browser_without_any_cookie_reads_as_empty_not_as_an_error(self):
        """合法的空:那个浏览器里一条 x.com cookie 都没有。空集照旧返回空集
        (口径同元规则那条「反过来钉一边:合法的空源照旧返回空集」)。
        """
        self.wire(cookies=[])
        self.assertEqual([], x_login_import.read_from_daily_browser())


class WriteShapeTests(ImportBase):
    def test_it_hands_chrome_only_valid_cookie_params(self):
        """`getCookies` 多给的那几个字段不是 `CookieParam` 的合法字段,原样回灌会让
        这条命令整条失败 —— 而那一下的表现只是一句「导入失败」。
        """
        _, chrome = self.wire()
        x_login_import.install(logged_in(), self.profile)
        for written in chrome.written:
            self.assertNotIn('size', written)
            self.assertNotIn('session', written)
            self.assertNotIn('priority', written)
            self.assertEqual({'name', 'value', 'domain', 'path', 'secure',
                              'httpOnly', 'sameSite', 'expires'}, set(written))

    def test_a_partitioned_cookie_keeps_its_partition(self):
        """**`partitionKey` 必须带过去。** 它在 `Cookie` 和 `CookieParam` 里是同一个
        类型,所以这不是「多给了一个响应字段」而是**改写语义**:省掉它等于把一条
        分区 cookie 写成非分区的,而同名、分属不同分区的两条还会被压到同一个键上
        (可能连普通那条一起盖掉)。
        """
        key = {'topLevelSite': 'https://x.com', 'hasCrossSiteAncestor': False}
        _, chrome = self.wire(cookies=[cookie('auth_token', partitionKey=key),
                                       cookie('ct0')])
        x_login_import.install([cookie('auth_token', partitionKey=key),
                                cookie('ct0')], self.profile)
        written = {c['name']: c for c in chrome.written}
        self.assertEqual(key, written['auth_token']['partitionKey'])
        self.assertNotIn('partitionKey', written['ct0'])   # 没有就不编一个

    def test_a_session_cookie_goes_over_without_an_expiry(self):
        """会话 cookie 的 `expires` 是 -1。原样交回去等于说「1969 年就过期了」,
        Chrome 会当场把它丢掉 —— 省掉这个字段才是「关掉浏览器就失效」那个语义。
        """
        _, chrome = self.wire()
        x_login_import.install([cookie('ct0', expires=-1)], self.profile)
        self.assertNotIn('expires', chrome.written[0])

    def test_cookies_land_before_the_first_navigation(self):
        """先灌再开页:第一次导航就带着它们,随后那次验证读到的才是最终状态。

        **两次开机各导航一次**:第一次是当场验,第二次是重开之后再真验一次(那一道
        管「快照压根看不见的那部分」,见 `install`)。两次打的都是同一个地址 ——
        它就是 `VERIFY_URL`,快照的读取范围也按它算,**一处定义**。
        """
        _, chrome = self.wire()
        x_login_import.install(logged_in(), self.profile)
        self.assertEqual('Storage.setCookies', chrome.calls[0][0])
        self.assertEqual([x_login_import.VERIFY_URL] * 2, chrome.goes)
        self.assertIn(x_login_import.VERIFY_URL, x_login_import._SCOPE_URLS)

    def test_it_opens_the_profile_three_times_and_closes_each(self):
        """**三次开机各有各的问题要答。** ①写 + 当场验;②重开、读盘、**再真验一次**
        (那才是「以后定时同步不用再点弹窗」这句承诺的内容);③**只读盘、不导航** ——
        因为②那次导航本身就是个写者,它换发出来的那一份才是明早要用的,而那一份
        落没落盘只有再开一次才答得出(见 `install` 的提交边界)。
        每一次都要收尾走完:`Browser.close` 才是让 profile 存盘的那一步,被 kill 掉
        的那一次写进去的东西可能一个都没落盘。
        """
        _, chrome = self.wire()
        x_login_import.install(logged_in(), self.profile)
        self.assertEqual(3, chrome.entered)
        self.assertEqual(3, chrome.exited)
        self.assertEqual([self.profile] * 3, chrome.opens)
        # **第三次一步都不许导航** —— 再导航就又多一个写者,同一个缺口只是被推到
        # 下一次关闭之后,永远证不完。
        self.assertEqual(2, len(chrome.goes), chrome.goes)


class RefusalTests(ImportBase):
    def test_without_the_essential_cookies_nothing_is_written(self):
        """**最贵的那个错。** 残缺的登录态一旦写进去,就盖掉了这个 profile 里原来
        那份可能好着的 —— 而 `readiness` 看到 Cookies 文件照旧报「登录过」,真相
        要等到真抓的时候。所以这一档一个字都不写。
        """
        _, chrome = self.wire(cookies=[cookie('guest_id')])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual(0, chrome.entered)                 # Chrome 压根没起
        self.assertIsNone(chrome.written)
        self.assertFalse(self.ledger.exists())              # 也没留痕
        self.assertIn('auth_token', detail)
        self.assertIn('登录一次', detail)

    def test_it_refuses_before_touching_anything_when_no_debug_port_is_open(self):
        """没有调试开关 → 连 profile 都不碰,而且要给出怎么开那个开关。"""
        browser, chrome = self.wire(discovered=False)
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual(0, browser.entered)
        self.assertEqual(0, chrome.entered)
        self.assertIn('chrome://inspect', detail)

    def test_a_busy_profile_refuses_before_the_authorisation_popup(self):
        """**顺序要紧。** profile 被占着的时候先说,别等用户点完授权弹窗才发现
        写不进去 —— 那一下白让他点一次。
        """
        browser, chrome = self.wire(busy=True)
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual(0, browser.entered)
        self.assertEqual(0, chrome.entered)
        self.assertIn('占着', detail)

    def test_the_authorisation_timeout_comes_back_as_its_own_words(self):
        """等不到那一下「允许」时,`cdp_attach` 那句成品文案要原样透出来。"""
        self.wire(attach_fail=CdpError('连不上 Chrome:浏览器没有在预期时间内响应。'
                                       '如果浏览器弹了授权弹窗,点一下「允许」再试'))
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('允许', detail)


class VerdictTests(ImportBase):
    def test_a_verified_import_says_so_and_says_what_it_bought(self):
        self.wire()
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok)
        self.assertIn('2 个 x.com cookie', detail)
        self.assertIn('7 条推文', detail)
        self.assertIn('还留在磁盘上、X 也还认这份登录态', detail)     # 不是「当场能用」就算成
        self.assertIn('不再需要点授权弹窗', detail)

    def test_cookies_x_rejects_are_not_reported_as_usable(self):
        """**把「写进去了」当成「可以用了」是这条链第二贵的错。** 搬过去的 cookie
        换了浏览器实例之后 X 认不认,是这条路唯一真正的未知 —— 不认的时候要当场
        说清,并且指向那条确定能走的路(在那个 profile 里直接登录一次)。
        """
        self.wire(verdict='login')
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('判成未登录', detail)
        self.assertIn('打开登录窗口', detail)

    def test_each_unverified_verdict_keeps_its_own_words(self):
        """四档没验成的原因各说各的话,不许折叠成一句「导入失败」。

        循环里重新 `wire` 一次就够:`patch.object` 后起的那次盖住前一次,而留痕那
        份每轮原地覆盖 —— 不必(也不该)重跑 `setUp`,那会把 `self.ledger` 指到
        另一个临时目录,后面读的就不是刚写的那份了。
        """
        wanted = {'login': '判成未登录', 'limited': '限流',
                  'not-loaded': '没加载完', 'no-tweets': '一条推文都没读到'}
        for verdict, phrase in wanted.items():
            with self.subTest(verdict=verdict):
                self.wire(verdict=verdict)
                ok, detail = x_login_import.import_login_state(self.profile)
                self.assertFalse(ok)
                self.assertIn('已经搬进', detail)     # 实话:cookie 确实写了
                self.assertIn(phrase, detail)

    def test_a_rejected_import_still_leaves_a_trace(self):
        """没验成的那几档**最需要**留痕:cookie 已经落进 profile 了,`readiness`
        从此会报「登录过」—— 不留痕的话那份不可用的登录态就成了一句看不出来的
        假话。提示而不代劳。
        """
        self.wire(verdict='login')
        x_login_import.import_login_state(self.profile)
        self.assertTrue(self.ledger.exists())
        self.assertIn('判成未登录', x_login_import.summary())


class LedgerTests(ImportBase):
    def test_the_trace_records_names_but_never_values(self):
        """**cookie 值就是登录凭证本身。** 它只该待在那个 profile 的加密库里;
        落进一份明文 JSON 等于把这个功能防的事自己做一遍。

        **这条用例的射程是调用处,不是 `record()` 内部** —— 而那是刻意的:
        `record()` 压根不接收值(只收名字和条数),所以从它里面注不出泄密来
        (反向验证实测:往它的 payload 里塞 `names` 照旧绿,因为那本来就是名字)。
        唯一能泄密的写法是调用处把整条 cookie 交过去,那一注这条就红。
        想「加强」它之前先想清楚这件事:保证是**结构性**的,不是靠断言盯出来的。
        """
        self.wire(cookies=[cookie('auth_token', value='SECRET-TOKEN'),
                           cookie('ct0', value='SECRET-CSRF')])
        x_login_import.import_login_state(self.profile)
        raw = self.ledger.read_text('utf-8')
        self.assertNotIn('SECRET-TOKEN', raw)
        self.assertNotIn('SECRET-CSRF', raw)
        self.assertIn('auth_token', raw)                  # 名字不是秘密,留着好诊断

    def test_never_imported_is_an_empty_line_not_a_guess(self):
        self.assertEqual('', x_login_import.summary())
        self.assertEqual({'known': True, 'at': '', 'verdict': '', 'cookies': 0},
                         x_login_import.state())

    def test_an_unreadable_trace_says_so_instead_of_never_imported(self):
        """**读不出来的正确答案是「不知道」。** 拿它冒充「从没导过」是在报一个
        没查到的结论(口径同 `config.read_config_file`)。
        """
        self.ledger.write_text('{坏掉的 JSON', encoding='utf-8')
        got = x_login_import.state()
        self.assertFalse(got['known'])
        self.assertIn('登录态导入记录', x_login_import.summary())

    def test_if_the_round_cannot_be_registered_the_profile_is_not_touched(self):
        """**账本一个字都写不进去的时候,正确的做法是什么都别改。**

        这是「账本永远不比 profile 旧」那条不变量在磁盘坏掉时唯一还能成立的做法:
        什么都没动,那么账本上原来那条记录照旧是准确的。反过来(先改 profile、
        记不上就算了)的后果是面板继续展示一句关于**已经不存在**的那份登录态的
        「当场验证通过」—— 而用户没有任何办法看出来。
        """
        _, chrome = self.wire()
        with patch.object(x_login_import, 'write_config_file',
                          side_effect=OSError('磁盘满了')):
            ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual(0, chrome.entered)              # profile 压根没碰
        self.assertIsNone(chrome.written)
        self.assertIn('没写进磁盘', detail)
        self.assertIn('不动 profile', detail)

    def test_only_the_closing_entry_failing_still_reports_the_real_outcome(self):
        """**登记成了、落账失败**是另一档:profile 真的改了,而账本停在
        `writing` —— 那正是此刻的实情(「这一轮没走完」),面板不会冒充成功。
        结论照旧如实返回,原因跟着说出去(不许一声不吭)。
        """
        self.wire()
        real = x_login_import.write_config_file
        calls = []

        def once_then_fail(path, data):
            calls.append(data['verdict'])
            if len(calls) == 1:
                return real(path, data)
            raise OSError('磁盘满了')

        with patch.object(x_login_import, 'write_config_file', once_then_fail):
            ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok)                              # 东西确实搬到位了
        self.assertIn('没写进磁盘', detail)
        self.assertEqual(['writing', 'ok'], calls)
        # 盘上留着的是「没走完」,而不是上一轮的结论
        self.assertEqual('writing', json.loads(self.ledger.read_text('utf-8'))['verdict'])

    def test_importing_again_overwrites_the_previous_trace(self):
        """cookie 会过期,所以这件事要能反复做。再点一次就是重做一遍。"""
        self.wire()
        x_login_import.import_login_state(self.profile)
        self.assertEqual(2, json.loads(self.ledger.read_text('utf-8'))['cookies'])
        self.wire(cookies=[cookie('auth_token'), cookie('ct0'), cookie('twid')])
        x_login_import.import_login_state(self.profile)
        self.assertEqual(3, json.loads(self.ledger.read_text('utf-8'))['cookies'])


class EndpointTests(ImportBase):
    def test_the_panel_shows_when_the_state_was_imported(self):
        self.wire()
        x_login_import.import_login_state(self.profile)
        self.settings(BROWSER)
        got = self.client.get('/api/intake/social/x-channel').json()
        self.assertIn('上次从日常浏览器导入', got['login_state'])

    def test_the_button_refuses_when_the_saved_channel_is_another_one(self):
        """门槛和「打开登录窗口」同一道:页面上那个下拉是草稿,后端读存下来的那份。
        拒绝文案要说清**现在是哪一档**和**下一步**。
        """
        self.settings(ATTACH)
        r = self.x_act('/api/intake/social/x-channel/import-login')
        self.assertEqual(400, r.status_code)
        detail = r.json()['detail']
        self.assertIn(x_channels.LABELS[ATTACH], detail)
        self.assertIn('保存设置', detail)

    def test_a_successful_import_comes_back_with_the_refreshed_trace(self):
        """面板上那行字要跟着这一次更新,否则用户得重开整页才看得见自己刚做的事。"""
        self.wire()
        self.settings(BROWSER)
        got = self.x_act('/api/intake/social/x-channel/import-login').json()
        self.assertTrue(got['ok'])
        self.assertIn('上次从日常浏览器导入', got['login_state'])

    def test_this_kind_of_source_has_no_channels_at_all(self):
        r = self.client.post('/api/intake/rss/x-channel/import-login')
        self.assertEqual(404, r.status_code)


class FakePage:
    """假的页面会话:`eval` 按预设回答 `_STATE_JS`。"""

    def __init__(self, *states):
        self.states, self.seen = list(states), 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def open_page(self, url='about:blank'):
        return 'sid'

    def goto(self, url):
        return {}

    def eval(self, _expr, timeout=0.0):
        state = self.states[min(self.seen, len(self.states) - 1)]
        self.seen += 1
        return json.dumps(page_state(url='https://x.com/home') | state)


class ProbeWordingTests(unittest.TestCase):
    """`verify_timeline` 是从 `probe` 里抽出来的 —— **抽完每一档文案一个字都不许变。**

    所以这一组驱动的是**真的** `verify_timeline`(只打桩浏览器会话),而不是把它
    也打掉:打掉的话这一组就只在验打桩本身,而「搬代码要逐符号对过原文」那条元规则
    正是要防这个(广域回归可能全绿,因为新函数压根没进过执行路径)。
    """

    def run_probe(self, *states, attach=False, budget=0.4):
        page = FakePage(*states)
        for p in (patch.object(x_browser, 'browser_session',
                               lambda *_a, **_k: page),
                  patch.object(x_browser, 'profile_busy', lambda *_a: False),
                  patch.object(x_browser, 'ACCOUNT_SECONDS', budget),
                  patch.object(x_browser, '_SETTLE_STEP', 0.01),
                  patch.object(cdp_attach, 'discover',
                               lambda **_k: cdp_attach.Endpoint('127.0.0.1', 1, '/x', 'x'))):
            p.start(); self.addCleanup(p.stop)
        return x_browser.probe('/tmp/profile', attach=attach)

    def test_a_working_login_reports_the_tweet_count(self):
        ok, detail = self.run_probe({'articles': 5})
        self.assertTrue(ok)
        self.assertEqual('登录态有效,首页读到 5 条推文', detail)

    def test_the_independent_profile_is_told_about_both_of_its_buttons(self):
        """**这一档有两条自救路,都要说。** 登录态是「从日常浏览器导入」来的那些人,
        只被指去点「打开登录窗口」等于被推着做他本来想免掉的那次登录 —— 而他要的
        只是再导一次(cookie 过期是常态)。
        """
        ok, detail = self.run_probe({'login': True})
        self.assertFalse(ok)
        self.assertEqual('X 登录态已失效:点「打开登录窗口」重新登录一次,'
                         '或者再点一次「从日常浏览器导入登录态」', detail)

    def test_the_daily_browser_is_told_to_log_in_there(self):
        """两条路的下一步不一样 —— 指错地方等于让人白跑一趟。"""
        ok, detail = self.run_probe({'login': True}, attach=True)
        self.assertFalse(ok)
        self.assertEqual('这个浏览器里的 X 登录态已失效,'
                         '请在它里面打开 x.com 登录一次,再回来检查', detail)

    def test_rate_limiting_keeps_its_own_sentence(self):
        ok, detail = self.run_probe({'limited': True})
        self.assertFalse(ok)
        self.assertEqual('X 暂时限制了访问频率,稍后再试', detail)

    def test_a_page_that_never_loads_points_at_the_network(self):
        ok, detail = self.run_probe({'ready': 'loading'})
        self.assertFalse(ok)
        self.assertEqual('X 首页没有加载完 —— 网络不通,或者这台机器访问 X 要走代理',
                         detail)

    def test_a_loaded_page_with_no_tweets_keeps_the_other_sentence(self):
        ok, detail = self.run_probe({'articles': 0})
        self.assertFalse(ok)
        self.assertEqual('X 首页打开了,但一条推文都没读到。登录态可能刚刚失效', detail)


if __name__ == '__main__':
    unittest.main()
