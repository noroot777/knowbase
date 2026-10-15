# -*- coding: utf-8 -*-
"""「从日常浏览器导入 X 登录态」那三组用例共用的摆盘。

**单拆一个文件只因为 700 行上限**(`test_x_login_import.py` 顶到过 717)。边界按
职责切:这一份只管「假的外部世界长什么样」—— 两个假浏览器、一条 cookie 的形状、
`Network.getCookies` 的筛法、默认拒绝一切真浏览器的 `ImportBase`;三个
`test_x_login_*.py` 各自只管它那一族判据。**摆盘共用一份**,各写一份的话几边的
前提会各漂各的(`ChannelBase` 当初从 `test_x_channels.py` 共用也是这个理由)。
"""

from __future__ import annotations

from unittest.mock import patch

from kb.feeds import cdp_attach, x_browser, x_channels, x_cdp, x_login_import
from tests.test_x_channels import ChannelBase

BROWSER, ATTACH = x_channels.BROWSER, x_channels.ATTACH


def cookie(name, value='v', domain='.x.com', **extra):
    """一条 `Storage.getCookies` 形状的 cookie(多给的字段照它真实的样子带上)。"""
    return {'name': name, 'value': value, 'domain': domain, 'path': '/',
            'expires': 1900000000.0, 'size': 42, 'httpOnly': True, 'secure': True,
            'session': False, 'sameSite': 'None', 'priority': 'Medium'} | extra


def applicable(cookies, urls):
    """按 `Network.getCookies` 的语义筛:只交出对这几个地址适用的 cookie。

    判**域**和**路径**两道。路径这道不是凑数:`Network.getCookies` 交的是「对这几个
    地址适用的」,**不是**「这个域下所有路径的」—— `ct0@/home` 在根 URL 下压根不出现
    (真浏览器实测如此)。夹具少了这道的话,「验证时用的那一份整个落在快照之外」这件
    事在这里永远摆不出来,而那正是第 5 轮审查拦下的那条。
    `secure` 那道没判(这一组全是 https 地址,判不判结果一样)。
    **`urls` 没给就按协议语义返回全部** —— 那一档正是我们不许生产走的那条。
    """
    if not urls:
        return list(cookies)
    out = []
    for c in cookies:
        dom = (c.get('domain') or '').lstrip('.').lower()
        cpath = c.get('path') or '/'
        for u in urls:
            host, _, rest = u.split('://', 1)[-1].partition('/')
            upath = '/' + rest
            if not (host.lower() == dom or host.lower().endswith('.' + dom)):
                continue
            # RFC 6265 的 path-match:相等、或者是以 `/` 收尾的前缀
            if upath == cpath or upath.startswith(
                    cpath if cpath.endswith('/') else cpath + '/'):
                out.append(c)
                break
    return out


def as_cookie(param: dict) -> dict:
    """此刻交出去的一条 `CookieParam` → 浏览器随后会把它报成什么样的 `Cookie`。

    夹具要能分开说两件事:**我们交进去的**、和**浏览器认下来的**。这两者实测并不
    相同(Chrome 把有效期削到 400 天、站点在验证期间自己换发 cookie),而判据只许
    建在后者身上 —— 夹具要是只会按前者答,这一组用例就永远碰不到那个分岔。
    `session`/`expires` 这一对由 `expires` 在不在决定,跟真浏览器一致(实测:
    持久 cookie 两边逐位相同,会话 cookie 报 `expires=-1, session=true`)。
    """
    out = dict(param)
    out.setdefault('size', 42)
    out.setdefault('priority', 'Medium')
    if isinstance(param.get('expires'), (int, float)) and param['expires'] > 0:
        out['session'] = False
    else:
        out['session'], out['expires'] = True, -1
    return out


def logged_in(**extra):
    """一份够用的 X 登录态。"""
    return [cookie('auth_token'), cookie('ct0'), *extra.get('more', [])]


def _no_real_browser(*_a, **_k):
    """`setUp` 里顶替 `Attached` 的哨兵 —— 被真调用到就是这个用例漏摆桩了。"""
    raise AssertionError('这条用例没有摆 wire(),差一点连上真的浏览器')


class FakeAttached:
    """假的 `cdp_attach.Attached`:记下收到的命令,按范围回答 `Network.getCookies`。

    **它按真浏览器的语义答:`urls` 之外的一条都不交出来。** 这不是摆设 ——
    生产那条路之所以改成页面级的 `Network.getCookies`,全部理由就是「范围由浏览器
    卡住」(见 `x_login_import.read_scoped`)。夹具要是无论给什么 `urls` 都把整杯
    端出来,这一组就退化成在验我们自己那道 `in_scope` 过滤,而那正是报告指出的
    那个不可靠判据。
    """

    #: 跟真 `cdp_attach.Attached` 一样:这是开在**用户自己**浏览器里的后台标签页。
    #: `x_login_import.read_all` 的运行时闸认的就是这一样(和「有没有 `profile`」),
    #: 夹具不带的话那道闸在这一组里永远摆不出来。
    _BACKGROUND = True

    def __init__(self, cookies, fail=None):
        self.cookies, self.fail = cookies, fail
        self.calls, self.entered, self.exited = [], 0, 0
        self.pages, self._sid = [], ''

    def __call__(self, endpoint=None, *, timeout=0.0):
        self.endpoint, self.timeout = endpoint, timeout
        return self

    def __enter__(self):
        self.entered += 1
        if self.fail:
            raise self.fail
        return self

    def __exit__(self, *_exc):
        self.exited += 1

    def call(self, method, params=None, sid='', timeout=0.0):
        self.calls.append((method, params or {}))
        if method == 'Target.createTarget':
            return {'targetId': 'tid'}
        if method == 'Target.attachToTarget':
            return {'sessionId': 'sid'}
        if method == 'Storage.getCookies':
            # 真浏览器会把整杯端出来 —— 这个夹具也照着答,好让「生产里压根不该
            # 走这条」那几条用例有东西可以红。
            return {'cookies': self.cookies}
        if method == 'Network.getCookies':
            return {'cookies': applicable(self.cookies, (params or {}).get('urls'))}
        return {}

    def open_page(self, url='about:blank'):
        self.pages.append(url)
        self._sid = 'sid'
        return self._sid

    @property
    def methods(self):
        return [m for m, _ in self.calls]


class FakeChrome:
    """假的 `x_cdp.Chrome`:记下写进去的 cookie、开过的页、收尾走没走完。

    **它要同时扮三次开机**:`install` 那一次写+当场验、`_reopened` 那一次重开核验、
    `_settled` 那一次只读盘。
    `persisted` 不给的时候按「写进去的都留下了」答(常态);给了就按它答。

    **`persisted` 收的是整条 cookie,不是名字。** 收名字的话这个夹具压根表达不了
    「名字一样、值是上一次那份旧的」那一档 —— 而那正是重导入最常见的形态
    (第 2 轮审查指出第 1 轮那条没修完,就是因为判据和夹具都只认名字)。
    给字符串当简写(= 同名同值,留下来了),给 dict 就逐字按它答。
    `clean` 是收尾那次 `Browser.close` 成没成(`closed_cleanly`)。

    **`live` 是「验证结束那一刻浏览器里是什么」**,跟 `persisted`(盘上是什么)分开
    给。不给就按「浏览器原样认下了我们交的那批」答(常态)。两者必须能分开说,
    因为真浏览器会改写我们交进去的东西 —— 见 `as_cookie`。

    **`live2` / `settled` 是后半程那两格**:`live2` =「重开那次**验完**浏览器里是
    什么」(那次导航自己也是写者,站点可以在那一下换发凭证),`settled` =「那次关掉
    之后盘上是什么」。缺省都按「这一程什么都没变」答(`live2` 跟 `persisted` 一样、
    `settled` 跟 `live2` 一样),所以没摆它们的用例行为跟从前一模一样。
    **这两格必须能分开说**:不然「最后那次验证用的是一条会话 cookie、关掉就没了」
    这个形态在夹具里压根摆不出来,而那正是提交边界那道闸要拦的。
    """

    def __init__(self, fail=None, persisted=None, clean=True, reopen_fail=None,
                 live=None, live2=None, settled=None, settle_fail=None):
        self.fail, self.clean, self.reopen_fail = fail, clean, reopen_fail
        self.persisted, self.live = persisted, live
        self.live2, self.settled, self.settle_fail = live2, settled, settle_fail
        self.calls, self.pages, self.goes = [], [], []
        self.entered, self.exited, self.opens = 0, 0, []
        self.closed_cleanly = None

    def __call__(self, profile, *, headless=True, chrome_path=''):
        self.profile, self.headless, self.chrome_path = profile, headless, chrome_path
        self.opens.append(profile)
        self._boot_reads = 0
        if self.settle_fail and len(self.opens) > 2:
            raise self.settle_fail
        if self.reopen_fail and len(self.opens) > 1:
            raise self.reopen_fail
        return self

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *_exc):
        self.exited += 1
        self.closed_cleanly = self.clean

    def call(self, method, params=None, sid='', timeout=0.0):
        self.calls.append((method, params or {}))
        if self.fail and method == 'Storage.setCookies':
            raise self.fail
        if method == 'Storage.getCookies':
            # 四次读各问各的,**答案不许混成一个**(混了的话提交边界那道闸就没有
            # 射程):①第一次开机 = 验证结束时浏览器里有什么;②重开、导航之前 =
            # 盘上有什么;③重开、验完之后 = 这次加载之后浏览器里有什么;④最后那次
            # 只读盘 = 这一程关掉之后盘上有什么。
            self._boot_reads += 1
            boot, nth = len(self.opens), self._boot_reads
            if boot <= 1:
                want = self.live
            elif boot == 2 and nth == 1:
                want = self.persisted
            elif boot == 2:
                want = self.live2 if self.live2 is not None else self.persisted
            elif self.settled is not None:
                want = self.settled
            else:
                want = self.live2 if self.live2 is not None else self.persisted
            if want is None:
                want = [as_cookie(c) for c in (self.written or [])]
            # **整杯:一条都不按路径筛**(真浏览器的 `Storage.getCookies` 就是这样,
            # 这也正是目标侧改用它的理由 —— 见 `x_login_import.read_all`)。
            return {'cookies': [c if isinstance(c, dict) else cookie(c) for c in want]}
        # **`Network.getCookies` 这一侧故意不答。** 目标 profile 上再出现按 URL 读
        # 的写法就是路径盲区又回来了,而那条路静默的形态正是「两侧同时漏掉」——
        # 这里回空,让它当场红在「cookie 一条都没留下」上。
        return {}

    def open_page(self, url='about:blank'):
        self.pages.append(url)
        self._sid = 'sid'
        return self._sid

    def goto(self, url):
        # **也记进 `calls`**:重开那一次「先读盘、后导航」的顺序要能在外面钉住,
        # 而顺序只有在同一条流水上才看得见(读和导航分两个列表的话,谁先谁后就
        # 摆不出来了)。
        self.calls.append(('Page.navigate', {'url': url}))
        self.goes.append(url)
        return {}

    @property
    def written(self):
        """交给 `Storage.setCookies` 的那一批。"""
        for method, params in self.calls:
            if method == 'Storage.setCookies':
                return params['cookies']
        return None


class ImportBase(ChannelBase):
    def setUp(self):
        super().setUp()
        self.ledger = self.root / 'x-login-import.json'
        p = patch.object(x_login_import, 'LEDGER', self.ledger)
        p.start(); self.addCleanup(p.stop)
        self.profile = str(self.root / 'profile')
        # **默认拒绝:没摆桩的用例一步都不许走出这台机器。**
        # 这不是洁癖 —— 跑这套测试的机器上很可能正开着一个勾了远程调试的 Chrome
        # (那正是这个功能要用的东西)。漏写一次 `wire()` 的后果是用例**静默连上
        # 真浏览器**:弹一次授权弹窗、把真的 cookie 读出来写进临时 profile,然后
        # 因为条数和推文数跟预期不符才红 —— 而红的那一行看着像个普通的断言失败
        # (本轮实测:`import_login_state` 真跑通了,读回 13 个 cookie、9 条推文,
        # 整条用例跑了一分多钟)。所以这里先把发现那一步钉成「没有」:漏摆桩的
        # 用例会当场拿到「没有发现开着远程调试的浏览器」,一眼看得出是摆盘漏了。
        for p in (patch.object(cdp_attach, 'discover', lambda **_kw: None),
                  patch.object(cdp_attach, 'Attached', _no_real_browser)):
            p.start(); self.addCleanup(p.stop)

    def wire(self, *, cookies=None, verdict='ok', articles=7,
             discovered=True, busy=False, attach_fail=None, chrome_fail=None,
             persisted=None, clean=True, reopen_fail=None, live=None,
             verdict_again=None, live2=None, settled=None, settle_fail=None):
        """把四个外部依赖一次摆好,返回 (假浏览器, 假 Chrome)。

        `patch.object` 后起的那次盖住 `setUp` 里那道默认拒绝,所以同一个用例里
        连着 `wire()` 两次也成立(最后那次说了算)。

        **`verdict_again` 是重开之后那一次验证的结论**(缺省跟第一次一样)。两次
        必须能分开说 —— 「当场能用、重开就不认」正是那一道闸存在的理由,而它恰好
        是快照看不见的那一类(路径受限的凭证)。

        **`live2` / `settled` / `settle_fail` 管后半程**(见 `FakeChrome`):重开那次
        验完浏览器里是什么、那次关掉之后盘上是什么、最后那次开机起不起得来。
        """
        browser = FakeAttached(logged_in() if cookies is None else cookies, attach_fail)
        chrome = FakeChrome(chrome_fail, persisted, clean, reopen_fail, live,
                            live2, settled, settle_fail)
        said = []

        def verify(page, timeout=0.0):
            said.append(page)
            first = len(said) == 1
            return (verdict if first or verdict_again is None else verdict_again,
                    {'articles': articles})

        endpoint = cdp_attach.Endpoint('127.0.0.1', 9222, '/x', '测试') if discovered else None
        for p in (patch.object(cdp_attach, 'Attached', browser),
                  patch.object(cdp_attach, 'discover', lambda **_kw: endpoint),
                  patch.object(cdp_attach, 'explicit_problem', lambda *_a, **_k: ''),
                  patch.object(x_cdp, 'Chrome', chrome),
                  patch.object(x_cdp, 'profile_busy', lambda *_a: busy),
                  patch.object(x_browser, 'verify_timeline', verify)):
            p.start(); self.addCleanup(p.stop)
        return browser, chrome
