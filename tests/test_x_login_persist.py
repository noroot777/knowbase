# -*- coding: utf-8 -*-
"""导入登录态那条链上的**三条不变量**,每条都是一次审查拦下来的。

`tests/test_x_login_import.py` 钉的是射程、字段形状、拒绝和文案;这里钉的是另外
三件事 —— 它们的共同点是**都不能靠「流程走到了尾部」来证明**:

1. **读取范围由浏览器卡住,不由我们过滤。** 判据是生产代码里一次
   `Storage.getCookies` 都没有,外加那张标签页是隐藏的后台页。拿「最终保存/返回的
   数据过滤过」去证明「没有读取授权外的数据」是个不可靠判据 —— 过滤发生在数据
   已经解密、已经跨过浏览器边界进了我们进程之后。
2. **「当场能用」证不了「下次还在」。** 当场那次验证读的是浏览器进程内存里的
   状态;而这个功能承诺的是无人值守跨重启。判据只能是**重开同一个 profile**。
3. **账本永远不比 profile 旧。** 判据是「改之前先登记、每条退出路径都更新」,
   不是「正常走完时记一条」。

**「那份状态怎么投影」那一族在 `tests/test_x_login_state.py`** —— 比的是输入还是
浏览器快照、投影要带哪几维、完整的那一组在不在、验证失败和保存差异谁是主结论。
这里只钉上面那三条结构不变量。拆开只因为行数上限,摆盘都共用 `ImportBase`。
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from kb.feeds import cdp_attach, x_cdp, x_channels, x_login_import
from kb.feeds.x_cdp import CdpError
from tests.x_login_fixture import ImportBase, cookie

KB = Path(__file__).resolve().parent.parent / 'kb'


class _Spy(x_cdp.Session):
    """一个只记命令的 `Session`,用来驱动**真的** `open_page`。

    打桩 `open_page` 的话这一组就只在验打桩自己 —— 而要钉的恰恰是它拼出来的那几
    个参数(元规则「搬代码要逐符号对过原文」的同一个道理)。
    """

    _BACKGROUND = True
    _HIDDEN = True

    def __init__(self, reject_hidden: bool = False):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self.reject_hidden = reject_hidden

    def call(self, method, params=None, sid='', timeout=0.0):
        self.calls.append((method, params or {}))
        if method == 'Target.createTarget':
            if self.reject_hidden and 'hidden' in (params or {}):
                raise CdpError('Target.createTarget 失败:Invalid parameters hidden')
            return {'targetId': 'tid'}
        if method == 'Target.attachToTarget':
            return {'sessionId': 'sid'}
        return {}

    @property
    def created(self) -> list[dict]:
        return [p for m, p in self.calls if m == 'Target.createTarget']


class ScopeIsEnforcedByTheBrowserTests(unittest.TestCase):
    """不变量 ①。**这一组是结构性的** —— 它不盯某一次调用的参数,而是盯「这种
    写法在生产里压根不存在」,所以新写一处越权读取也会被它拦住。"""

    #: 整杯读只许出现在这一个函数里,只许从目标侧这几处调。
    JAR_HOME = 'read_all'
    JAR_CALLERS = ('install', '_reopened', '_settled')

    def test_the_whole_cookie_jar_is_only_read_from_our_own_profile(self):
        """`Storage.getCookies` 没有任何按站点筛选的参数,它一开口就是整杯。

        **用户那个浏览器上一次都不许发**(银行、邮箱、公司后台都在那一杯里);
        **我们自己那个 profile 上必须发** —— 按 URL 读卡不住**路径**,验证页用的
        可能正是 `ct0@/i/api`,两侧同时漏掉时「集合相等」什么都证明不了
        (实测与论证在 `x_login_import.read_all`)。所以这条规则不是「一次都不许
        有」,而是**两头各钉一道**:①整个 `kb/` 下这个调用形式只出现在 `read_all`
        的函数体里;②`read_all` 的调用点只有目标侧那三处。
        **按调用形式搜(带引号)而不是按名字搜**:注释和文档里要能照常讨论它。
        """
        home = KB / 'feeds' / 'x_login_import.py'
        offenders = []
        for path in sorted(KB.rglob('*.py')):
            text = path.read_text(encoding='utf-8')
            if not any(q in text for q in ("'Storage.getCookies'", '"Storage.getCookies"')):
                continue
            if path != home:
                offenders.append(str(path.relative_to(KB.parent)))
                continue
            tree = ast.parse(text)
            fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                       and n.name == self.JAR_HOME), None)
            self.assertIsNotNone(fn, '整杯读那个函数改名了,这条规则要跟着改')
            for i, line in enumerate(text.splitlines(), start=1):
                if any(q in line for q in ("'Storage.getCookies'", '"Storage.getCookies"')):
                    if not (fn.lineno <= i <= (fn.end_lineno or fn.lineno)):
                        offenders.append(f'{path.relative_to(KB.parent)}:{i}')
        self.assertEqual([], offenders)

    def test_the_whole_jar_read_is_only_called_from_the_target_side(self):
        """**反过来钉另一头:谁在调它。** 上一条只管「这个调用形式写在哪儿」,
        它拦不住有人把 `read_all(browser)` 写进读用户浏览器那条路里 —— 那一下整杯
        就真的跨过浏览器边界了,而且过滤之后看起来一切正常(元规则「漏报是静默丢
        数据」)。运行时那道闸在 `read_all` 自己身上,这一条是它的结构性那一半。
        """
        callers = []
        for path in sorted(KB.rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Call)
                            and isinstance(inner.func, ast.Name)
                            and inner.func.id == self.JAR_HOME):
                        callers.append(node.name)
        self.assertEqual(sorted(self.JAR_CALLERS), sorted(set(callers)))

    def test_the_whole_jar_read_refuses_the_users_own_browser(self):
        """运行时那道闸:**不是我们起的那个 profile,一条命令都不许发出去**。

        判据落在「发没发出去」而不是「返回了什么」—— 发出去了就已经解密、已经进
        了我们进程,之后再怎么过滤都晚了(口径同这一组的开头那段)。
        """
        from tests.x_login_fixture import FakeAttached
        browser = FakeAttached([cookie('auth_token')])
        with self.assertRaises(CdpError):
            x_login_import.read_all(browser)
        self.assertEqual([], browser.calls)

    def test_the_scoped_read_hands_the_browser_explicit_urls(self):
        """范围的执行点就是这几个 `urls`。少了它 `Network.getCookies` 的语义是
        「这张页面及其所有子框架适用的 cookie」—— 对 `about:blank` 来说是空,
        对别的页面来说是**那个站点**的,两种都不是我们要的。
        """
        urls = x_login_import._SCOPE_URLS
        self.assertTrue(urls)
        for url in urls:
            self.assertTrue(url.startswith('https://'), url)
            host = url.split('://', 1)[1].split('/', 1)[0]
            self.assertTrue(x_login_import.in_scope(host), host)

    def test_the_tab_in_the_users_browser_is_hidden_and_in_the_background(self):
        """**在用户自己的浏览器里开页,两个标志都要**:`background` 管「不抢前台」,
        `hidden` 管「不进标签栏」(它的生命周期还跟着这条连接,断开即消失)。
        协议规定 `hidden` 只能和 `background: true` 同时给。
        """
        self.assertTrue(cdp_attach.Attached._BACKGROUND)
        self.assertTrue(cdp_attach.Attached._HIDDEN)
        spy = _Spy()
        spy.open_page('about:blank')
        self.assertEqual([{'url': 'about:blank', 'background': True, 'hidden': True}],
                         spy.created)

    def test_an_old_browser_that_rejects_hidden_falls_back_to_background_only(self):
        """`hidden` 是比较新的参数。认不出来时退一档是可以的 —— 仍然不抢前台,
        只是标签栏里多一行。
        """
        spy = _Spy(reject_hidden=True)
        spy.open_page('about:blank')
        self.assertEqual([{'url': 'about:blank', 'background': True, 'hidden': True},
                          {'url': 'about:blank', 'background': True}], spy.created)

    def test_it_never_falls_back_to_a_foreground_tab(self):
        """**反过来钉一边。** 退档只许退到「后台标签页」,绝不退到前台:那是把
        用户正在看的东西顶掉,比标签栏多一行严重得多,宁可整件事失败。
        """
        spy = _Spy(reject_hidden=True)
        spy.open_page('about:blank')
        for params in spy.created:
            self.assertTrue(params.get('background'), params)
            self.assertNotIn('focus', params)
            self.assertNotIn('newWindow', params)

    def test_our_own_headless_profile_opens_a_plain_tab(self):
        """**反过来钉一边的另一半。** 隐藏标签页是为了不碰用户的标签栏;我们自己
        起的那个无头 Chrome 压根没有标签栏,不该跟着要这个比较新的参数(多要一个
        就多一种在老 Chrome 上失败的方式)。
        """
        self.assertFalse(x_cdp.Chrome._BACKGROUND)
        self.assertFalse(x_cdp.Chrome._HIDDEN)


class _FakeProc:
    """假的浏览器进程:只回答「还活着吗」和「被 kill 了吗」。"""

    def __init__(self, exited: bool = False):
        self.exited, self.killed = exited, False

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout=0.0):
        return 0

    def kill(self):
        self.killed = True


class PersistenceTests(ImportBase):
    """不变量 ②:对外承诺的是「下次还在」,所以判据是重开。"""

    def test_cookies_that_vanish_on_reopen_are_not_a_success(self):
        """**报告第 2 条。** 当场验证通过(X 认这份登录态、读到了推文),但重新
        打开这个 profile 之后关键 cookie 不在了 —— 这不是成功,它正是「以后定时
        同步不用再点弹窗」那句承诺反过来的样子。
        """
        self.wire(persisted=[])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('重新打开这个 profile 之后没有这一轮写的那一份', detail)
        self.assertEqual('unsaved',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])

    def test_one_missing_essential_is_enough_to_refuse(self):
        """`auth_token` 留下了、`ct0` 没留下 → 照旧不算成功:少了 CSRF 令牌,页面
        会在第一次 XHR 上掉回登录页,而 Cookies 文件已经躺在 profile 里了。
        """
        self.wire(persisted=['auth_token'])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('ct0', detail)
        self.assertIn('会话 cookie', detail)        # 正常关闭 → 多半是这个原因

    def test_a_forced_kill_says_it_could_not_save(self):
        """**两种原因的下一步不一样,所以要分开说。** 浏览器被 kill 掉(收尾那句
        `Browser.close` 没成)的时候,再导一次很可能就好了;而本来就是会话 cookie
        的话,再导一百次也一样。
        """
        self.wire(persisted=[], clean=False)
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('没能正常存盘', detail)
        self.assertNotIn('会话 cookie', detail)

    def test_a_profile_that_cannot_be_reopened_counts_as_not_saved(self):
        """**读不出来按「没留下」算。** 这一道是用来拦住一句承诺的,拦不住的代价
        是用户第二天早上才发现 X 那一段缺了;误拦的代价只是他再点一次那颗按钮。
        """
        self.wire(reopen_fail=CdpError('浏览器启动失败'))
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('没有这一轮写的那一份', detail)

    def test_a_real_success_says_it_checked_the_disk(self):
        """**反过来钉一边**:留下来了就照常报成功,而且那句话要说清它凭什么 ——
        「当场读到了推文」和「重开之后还在」是两件事,用户看到的得是后者。
        """
        self.wire()
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok)
        self.assertIn('重新打开之后它们还留在磁盘上、X 也还认这份登录态', detail)
        self.assertEqual('ok', json.loads(self.ledger.read_text('utf-8'))['verdict'])

    def test_the_clean_close_result_is_reported_not_swallowed(self):
        """`Chrome._shutdown` 一个字都不往外抛(它必须把 fd 和进程收干净),但
        「好好关了一次没有」这个事实要交到调用方手上 —— 原来它被整个吞掉,于是
        「被 kill、cookie 一个都没落盘」从外面看跟成功一模一样。

        **驱动的是真的 `_shutdown`**,只把那个进程和发送换成假的:要钉的正是它
        三条路各自留下什么结论,而不是某个标志位的初值。
        """
        cases = {True: None, False: CdpError('浏览器没有在预期时间内收下命令')}
        for clean, boom in cases.items():
            with self.subTest(clean=clean):
                chrome = x_cdp.Chrome('/tmp/x')
                self.assertIsNone(chrome.closed_cleanly)     # 还没收尾
                chrome.proc = _FakeProc()
                with patch.object(chrome, '_send',
                                  side_effect=boom or (lambda *a, **k: 1)):
                    chrome._shutdown()
                self.assertIs(clean, chrome.closed_cleanly)
                self.assertEqual(not clean, chrome.proc.killed)

    def test_a_browser_that_already_died_is_not_a_clean_close(self):
        """**反过来钉一边的那一半**:它自己先走了(崩溃、被别人杀掉)也不算
        「好好关了一次」—— 那一次 profile 里该存的东西可能一个都没存。
        """
        chrome = x_cdp.Chrome('/tmp/x')
        chrome.proc = _FakeProc(exited=True)
        chrome._shutdown()
        self.assertIs(False, chrome.closed_cleanly)


class TheReopenedBrowserIsTheJudgeTests(ImportBase):
    """不变量 ②的下半:盘上那几条对得上**不等于**这份登录态还成立。

    第 5 轮审查拦下的那条:`Network.getCookies` 交的是「对**这几个地址**适用的」,
    路径受限的凭证(实测 `ct0@/home`)在根 URL 下压根不出现 —— 而验证页用的恰好
    是那一份。所以成功要多过一道闸:**重开之后把真的那套登录验证再跑一次**。
    两道闸谁都替代不了谁:快照抓的是「当场能用、改天过期」(盘上是旧值/旧有效期),
    重验抓的是快照看不见的那一类。
    """

    def test_a_login_the_reopened_browser_rejects_is_not_a_success(self):
        """盘上一个不差,但重开之后 X 又把它判成未登录 —— 这不算成功。

        **这一档就是第 5 轮那条反例的形状**:缺的那一份快照根本没覆盖到,所以
        差异比对必然是空的,光靠它只能得出「存上了」。
        """
        self.wire(verdict_again='login')
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual('relogin',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])
        self.assertIn('又把它判成未登录', detail)
        self.assertNotIn('还留在磁盘上', detail)      # 不许拿上一道闸的结论充数
        self.assertNotIn('不再需要点授权弹窗', detail)

    def test_an_inconclusive_second_check_is_not_dressed_up_as_an_answer(self):
        """重验没跑成(限流、首页没加载完、一条推文都没读到)既不是成功、也不是
        「没存上」—— **判不准一律扣留**,并把下一步说成「再确认一次」。

        说成成功的代价是明早那一轮静默缺一段;说成「没存上」的代价是用户照着
        那句话一遍遍重导,而问题压根不在那儿。
        """
        for again in ('limited', 'not-loaded', 'no-tweets'):
            with self.subTest(again=again):
                self.wire(verdict_again=again)
                ok, detail = x_login_import.import_login_state(self.profile)
                self.assertFalse(ok)
                self.assertEqual(
                    'unconfirmed',
                    json.loads(self.ledger.read_text('utf-8'))['verdict'])
                self.assertIn('没跑成', detail)
                self.assertIn('检查连接', detail)
                self.assertNotIn('等于没搬成', detail)
                self.assertNotIn('不再需要点授权弹窗', detail)

    def test_the_second_check_runs_on_the_page_the_snapshot_covers(self):
        """**两道闸要对着同一个页面**:重验跑在 `VERIFY_URL` 上,而那个地址也在
        读取范围里 —— 否则验证页用的那一份仍然在快照之外,「盘上对得上」还是
        一句没有依据的话。
        """
        self.assertIn(x_login_import.VERIFY_URL, x_login_import._SCOPE_URLS)
        _b, chrome = self.wire()
        ok, _detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok)
        self.assertEqual([x_login_import.VERIFY_URL] * 2, chrome.goes)

    def test_the_disk_snapshot_is_taken_before_the_second_navigation(self):
        """重开那一次**先读盘、再导航、验完再读一次**;最后那次开机**只读盘**。

        读盘排在导航前面:反过来的话,那一次加载里站点自己轮换出的新凭证会被当成
        「盘上本来就有的」,于是「写进去的那一份到底存上没有」被顺手答成「存上了」。
        导航后面那一读是另一件事:它要的是「这次加载之后浏览器里是什么」—— 那才是
        明早采集要用的那一份,由最后那次只读盘的开机来对账。
        """
        _b, chrome = self.wire()
        x_login_import.import_login_state(self.profile)
        flow = [m for m, _p in chrome.calls
                if m in ('Storage.getCookies', 'Page.navigate')]
        self.assertEqual(['Page.navigate', 'Storage.getCookies',      # 当场验那次
                          'Storage.getCookies', 'Page.navigate',      # 重开:先读盘
                          'Storage.getCookies',                       # 重开:验完再读
                          'Storage.getCookies'],                      # 最后:只读盘
                         flow)

    def test_a_path_scoped_credential_takes_part_in_the_judgement(self):
        """路径受限的那一份也要进快照 —— 这是第 5 轮那条的另一半:只要读取范围
        覆盖到验证页,它就看得见,看得见就比得出来。

        摆的是「`/home` 下那条 `ct0` 盘上还是上一次那个旧值」:名字、根路径下那
        一份全都对得上,**只有这一条对不上**。
        """
        home_new = cookie('ct0', value='fresh', path='/home')
        home_old = cookie('ct0', value='stale-one', path='/home')
        self.wire(live=[cookie('auth_token'), cookie('ct0'), home_new],
                  persisted=[cookie('auth_token'), cookie('ct0'), home_old])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual('unsaved',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])
        self.assertIn('ct0', detail)

    def test_the_snapshot_gate_still_bites_when_the_second_check_passes(self):
        """**反过来钉一边之一**:重验通过**不许**顶掉快照那道闸。盘上那份 `ct0` 是
        上一次的旧值时,它这会儿当然还能用(还没过期)—— 而那恰好是用户最晚发现
        问题的走法:等它过期的那天,明早那一轮静默少一段。
        """
        self.wire(live=[cookie('auth_token'), cookie('ct0', value='fresh')],
                  persisted=[cookie('auth_token'), cookie('ct0', value='old')],
                  verdict_again='ok')
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual('unsaved',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])
        self.assertIn('当场也能用', detail)

    def test_a_first_check_that_failed_keeps_its_own_conclusion(self):
        """**反过来钉一边之二**:当场就没验过的时候,结论还是当场那一个,不许被
        重开那一次改写成 `relogin`/`unconfirmed` —— 已经查明的原因不许被后一道
        检查顶掉(口径同第 4 轮那条)。
        """
        self.wire(verdict='limited', verdict_again='login')
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual('limited',
                         json.loads(self.ledger.read_text('utf-8'))['verdict'])
        self.assertIn('在限流', detail)


class TheLastWriterHasToBeProvenTests(ImportBase):
    """提交边界:**被证明的那一份,必须就是最后留下的那一份。**

    重开那次验证本身也是个写者 —— 站点在那一下可以合法地下发、删除、轮换 cookie,
    而**明早采集要用的正是换来的那一份**。拿导航之前那份快照去宣布成功,证的是
    最后一个写者**之前**的状态;它过得去,不代表函数跑完之后盘上还有一份能用的。

    事故形态(审查用隔离 Chrome 确定性复现,`final-validation-reproduce.json`):
    第二次首页响应下发一条**没有有效期**的 `ct0`(= 会话 cookie),第二次验证照样
    读到推文、浏览器也**正常关闭**,那条 `ct0` 随进程一起没了 —— 前两道闸全过,
    产品报 `ok` 并承诺「以后不用点弹窗」,下一次打开同一个 profile 判 `login`。

    出路是最后再开一次、**只读盘不导航**:再导航就又多一个写者,同一个缺口只是被
    推到下一次关闭之后,永远证不完。
    """

    def _ledger(self):
        return json.loads(self.ledger.read_text('utf-8'))['verdict']

    def test_a_session_credential_minted_by_the_last_check_is_not_a_success(self):
        """**就是审查那条反例。** 两次验证都通过、两次都正常关闭、导航前的盘上快照
        一个字不差 —— 可最后那次验证用的 `ct0` 是那次加载里新发的会话 cookie,
        关掉就没了。这不算成功。
        """
        self.wire(live2=[cookie('auth_token'), cookie('ct0', value='minted')],
                  settled=[cookie('auth_token')])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual('unsaved', self._ledger())
        self.assertIn('会话 cookie', detail)
        self.assertIn('最后那次验证用的那一份', detail)
        # 不许把人指错方向:他写进去的那一份其实好好地留下了
        self.assertNotIn('没有这一轮写的那一份', detail)
        self.assertNotIn('不再需要点授权弹窗', detail)

    def test_a_credential_the_last_check_rotated_in_is_fine_when_it_persists(self):
        """**反过来钉一边,而且这一条最要紧。** 站点在最后那次加载里换发凭证是**常态**
        (`guest_id`、`__cf_bm`、`ct0` 都见过);只要换来的那一份真落了盘,就是一次
        完整的成功。这一道要是写成「最后那次验证之后不许有变化」,每一次正常导入都会
        被判死。
        """
        fresh = [cookie('auth_token'), cookie('ct0', value='rotated-in')]
        self.wire(live2=fresh, settled=fresh)
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok, detail)
        self.assertEqual('ok', self._ledger())

    def test_a_profile_that_cannot_be_opened_for_the_final_proof_is_refused(self):
        """最后那次开机起不来 = **证不出来**,按「没留下」算(口径同前两道:拦不住的
        代价是明早静默缺一段,误拦的代价只是再点一次那颗按钮)。
        """
        self.wire(settle_fail=CdpError('浏览器启动失败'))
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual('unsaved', self._ledger())
        self.assertNotIn('不再需要点授权弹窗', detail)

    def test_a_kill_on_the_verifying_browser_is_named_as_that(self):
        """**最后那次差异也要分得出两种病因。** 那个浏览器被 kill 掉(`Browser.close`
        没成)时,再导一次多半就好;本来就是会话 cookie 的话再导一百次也一样。
        """
        self.wire(live2=[cookie('auth_token'), cookie('ct0', value='minted')],
                  settled=[cookie('auth_token')], clean=False)
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('没能正常存盘', detail)
        self.assertNotIn('会话 cookie', detail)

    def test_the_final_proof_is_skipped_once_the_second_check_already_failed(self):
        """成功已经被拒了就不必再开第三次 —— 没有什么可证明的,而每开一次都是两三秒。
        **顺带钉住顺序**:要是把最后这道排在重验前面,`relogin` 会被一句「没存上」
        顶掉(口径同「已经查明的原因不许被后一道检查改写」)。
        """
        _b, chrome = self.wire(verdict_again='login',
                               live2=[cookie('auth_token')], settled=[])
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertEqual('relogin', self._ledger())
        self.assertEqual(2, len(chrome.opens), chrome.opens)
        self.assertIn('又把它判成未登录', detail)


class LedgerFollowsTheProfileTests(ImportBase):
    """不变量 ③:账本永远不比 profile 旧。"""

    def test_the_round_is_registered_before_the_first_write(self):
        """**顺序就是这条不变量本身。** 登记排在 `Storage.setCookies` 前面,所以
        从「profile 可能已经变了」那一刻起,盘上那条记录说的就是这一轮。
        """
        order = []
        real = x_login_import.write_config_file
        _, chrome = self.wire()

        def spy(path, data):
            order.append('ledger:' + data['verdict'])
            return real(path, data)

        with patch.object(x_login_import, 'write_config_file', spy):
            x_login_import.import_login_state(self.profile)
        self.assertEqual('ledger:writing', order[0])
        self.assertEqual('Storage.setCookies', chrome.calls[0][0])

    def test_an_exception_after_the_write_does_not_leave_yesterdays_success(self):
        """**报告第 3 条。** cookie 写进去了、随后导航/验证抛异常 —— profile 已经
        改了,而原来的做法是直接返回、跳过留痕,于是面板上继续挂着昨天那条「当场
        验证通过」,说的是一份**已经不存在**的登录态。
        """
        self.wire()
        x_login_import.import_login_state(self.profile)     # 昨天那一轮,ok
        self.assertIn('当场验证通过', x_login_import.summary())
        self.wire(chrome_fail=CdpError('injected navigation timeout'))
        ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('这一轮没走完', detail)
        got = json.loads(self.ledger.read_text('utf-8'))
        self.assertEqual('writing', got['verdict'])
        self.assertNotIn('当场验证通过', x_login_import.summary())
        self.assertIn('状态不确定', x_login_import.summary())

    def test_a_half_written_batch_is_never_reported_as_untouched(self):
        """`Storage.setCookies` 抛异常**不等于**一个 cookie 都没写进去,所以这一档
        不许说「原样没动」—— 账本留在 `writing`,文案说「状态不确定」。
        """
        self.wire(chrome_fail=CdpError('写到一半断了'))
        _, detail = x_login_import.import_login_state(self.profile)
        self.assertIn('状态不确定', detail)
        self.assertNotIn('没动', detail)

    def test_two_imports_cannot_interleave(self):
        """**`profile_busy` 是早一道,不是互斥**(元规则)。要串起来的是「写 → 验 →
        重开核验 → 落账」整段:profile 的 OS 独占锁在 `install` 返回那一刻就松了,
        而账还没落 —— 两轮叠起来时晚开工的那次可能先落账,面板上那行字从此说的是
        另一轮的事。
        """
        self.wire()
        with x_login_import._installing(self.profile) as blocked:
            self.assertEqual('', blocked)          # 空串 = 拿到了
            ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('另一次导入正在进行', detail)

    def test_the_lock_is_released_so_the_next_try_gets_through(self):
        """**反过来钉一边**:锁不许留着。一次失败的导入把这颗按钮永久变成「另一次
        正在进行」,比没有锁更糟 —— 而用户没有任何办法把它解开。
        """
        self.wire(chrome_fail=CdpError('第一次炸了'))
        x_login_import.import_login_state(self.profile)
        self.wire()
        ok, _ = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok)


class LockFailureTests(ImportBase):
    """锁**被占着**和锁**建不起来**是两件事,不许都说成「别人在导入」。"""

    def test_a_lock_that_cannot_be_created_says_what_is_actually_wrong(self):
        """数据目录只读 / 配额满 → 原来直接把 `OSError` 抛给接口,最终只落成一个
        500,连原因都看不到。
        """
        _, chrome = self.wire()
        with patch.object(x_login_import.os, 'open', side_effect=OSError('只读文件系统')):
            ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('建不了导入锁文件', detail)
        self.assertIn('只读文件系统', detail)
        self.assertNotIn('另一次导入', detail)     # 没有「别人」,别让他去关窗口
        self.assertEqual(0, chrome.entered)

    def test_a_filesystem_without_locks_is_not_reported_as_contention(self):
        """不支持 flock 的文件系统(网络盘)照旧不是并发。把它说成「别人在导入」
        的后果是用户去关一个压根不存在的窗口,然后反复点那颗按钮 —— 而每一次都
        白弹一个授权窗。
        """
        _, chrome = self.wire()
        with patch.object(x_login_import.fcntl, 'flock',
                          side_effect=OSError('Operation not supported')):
            ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('不支持文件锁', detail)
        self.assertNotIn('另一次导入', detail)
        self.assertEqual(0, chrome.entered)

    def test_real_contention_still_says_someone_else_is_importing(self):
        """**反过来钉一边**:`BlockingIOError` 那一档照旧是那句话。"""
        _, chrome = self.wire()
        with patch.object(x_login_import.fcntl, 'flock',
                          side_effect=BlockingIOError('已被占用')):
            ok, detail = x_login_import.import_login_state(self.profile)
        self.assertFalse(ok)
        self.assertIn('另一次导入正在进行', detail)
        self.assertEqual(0, chrome.entered)

    def test_a_failure_while_releasing_the_lock_does_not_eat_the_result(self):
        """**收尾自己抛错不许盖掉业务结果。** 解锁和关 fd 跑在 `finally` 里,而
        `finally` 里冒出来的异常会**替换掉**那条 `return` —— 一次已经成功的导入
        于是在接口上变成一个没有原因的 500,用户看到的是「导入没跑完」,而登录态
        其实已经搬好了:他会再点一次,再弹一个授权窗。
        """
        real = x_login_import.fcntl.flock

        def flaky(fd, op):
            real(fd, op)
            if op == x_login_import.fcntl.LOCK_UN:
                raise OSError('解锁失败')

        self.wire()
        with patch.object(x_login_import.fcntl, 'flock', flaky):
            ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok, detail)
        self.assertIn('还留在磁盘上、X 也还认这份登录态', detail)

    def test_a_failure_while_closing_the_descriptor_does_not_eat_the_result(self):
        """同一道的另一半:`os.close` 抛错(fd 已经被别处收走)照旧不许顶掉结论。

        **两份实现各有自己的回归** —— 这两步在两个嵌套的 `finally` 里,挡住一个
        不等于挡住另一个。
        """
        real = x_login_import.os.close

        def flaky(fd):
            real(fd)
            raise OSError('Bad file descriptor')

        self.wire()
        with patch.object(x_login_import.os, 'close', flaky):
            ok, detail = x_login_import.import_login_state(self.profile)
        self.assertTrue(ok, detail)

    def test_the_lock_is_really_released_on_the_way_out(self):
        """**反过来钉一边:咽掉异常不等于不解锁。** 两次连着导入都要能拿到锁 ——
        要是把那句 `flock(LOCK_UN)` 整个删掉换成 `pass`,这条会红(同一个进程里
        第二次 `LOCK_EX|LOCK_NB` 不会因为自己持有而失败,所以判据落在
        **文件没被占着**:换个 fd 去抢同一把锁)。
        """
        self.wire()
        for _ in range(2):
            ok, detail = x_login_import.import_login_state(self.profile)
            self.assertTrue(ok, detail)
        path = Path(str(self.ledger) + '.lock')
        fd = x_login_import.os.open(path, x_login_import.os.O_RDWR)
        try:
            x_login_import.fcntl.flock(
                fd, x_login_import.fcntl.LOCK_EX | x_login_import.fcntl.LOCK_NB)
            x_login_import.fcntl.flock(fd, x_login_import.fcntl.LOCK_UN)
        finally:
            x_login_import.os.close(fd)


class PanelWordingTests(unittest.TestCase):
    """面板文案不许带 markdown。

    这几条都是经 `textContent` 落到页面上的(`x_channel.js` 那三处),所以 `**粗体**`
    会原样显示成四个星号。本轮**两次**栽在这上面(一次是 `ENABLE_HINT`,一次是通道
    说明),所以把它钉成一条规则而不是改两处字 —— 反引号不在禁用之列:它在纯文本
    里读起来就是一对引号,不像星号那样一眼就是坏的。
    """

    def test_no_markdown_bold_in_anything_the_panel_prints(self):
        texts = {f'HINTS[{k}]': v for k, v in x_channels.HINTS.items()}
        texts |= {f'LABELS[{k}]': v for k, v in x_channels.LABELS.items()}
        texts |= {f'_VERDICT_TAIL[{k}]': v
                  for k, v in x_login_import._VERDICT_TAIL.items()}
        texts |= {'ENABLE_HINT': cdp_attach.ENABLE_HINT,
                  'ENDPOINT_HINT': cdp_attach.ENDPOINT_HINT}
        self.assertEqual([], [name for name, text in texts.items() if '**' in text])


class ImportRefreshesReadinessTests(ImportBase):
    """导入成功之后,就绪那一行不许还说导入之前的话(报告 A)。"""

    def test_the_import_response_carries_the_recomputed_readiness(self):
        """**同一份口径算两遍**:`x_channels.readiness`,跟 GET 那条路一字不差。
        不回新的 `ready/why`,页面上就同时挂着「当场验证通过」和「还没有登录过 X」。
        """
        self.wire()
        self.settings(x_channels.BROWSER)
        before = self.client.get('/api/intake/social/x-channel').json()
        self.assertFalse(before['ready'])
        got = self.x_act('/api/intake/social/x-channel/import-login').json()
        self.assertTrue(got['ok'])
        self.assertIn('ready', got)
        self.assertIn('why', got)
        fresh = self.client.get('/api/intake/social/x-channel').json()
        self.assertEqual(fresh['ready'], got['ready'])
        self.assertEqual(fresh['why'], got['why'])


if __name__ == '__main__':
    unittest.main()
