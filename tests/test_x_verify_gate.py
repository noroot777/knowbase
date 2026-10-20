# -*- coding: utf-8 -*-
"""撞上 X 人机验证页之后的那道熔断(x_browser 的识别 + 整批中止,sync 的跳过文案)。

硬约束、事故形态和实测证据在 `docs/decisions/x-channels.md`。

**怎么写这几条**:

- **一个真浏览器都不起。** `_STATE_JS` 那段判据抠出来丢给 node 求值(真代码,不是
  在 Python 里复述一遍那几条正则 —— 复述版改坏了正则也全绿);Python 那一侧用一个
  假 page 驱动**真的** `harvest`,打桩的边界只有 `browser_session` 一处。
- **判据落在后果上**:剩余账号**被碰了几个**、它们**进不进「重试失败项」的名单**、
  库里**有没有给它们记退避**、面板那行字**说的是哪个原因** —— 而不是"有没有打上
  某个标志"。一个把标志传下去却没人认它的实现要能红。
- **「立刻抛」不拿墙上时间当判据**,数的是 `_STATE_JS` 被求值了几次:没有这道判据
  时 `_wait_state` 会每 0.3 秒问一次、问满 `ACCOUNT_SECONDS`(约 167 次),所以
  「正好 1 次」既钉住了"认出来了",也钉住了"没有白等"。
- **每条都反过来钉一边**:认得出验证页的同时,正常那一批照旧一个不少地抓完、
  一条都不许被熔断带走。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import maintenance, pipeline_state
from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_channels
from tests.x_page_state import page_state

FRESH = datetime.now(timezone.utc) - timedelta(hours=3)
OLD = datetime.now(timezone.utc) - timedelta(days=9)
FLOOR = datetime.now(timezone.utc) - timedelta(days=4)
FLOOR_ISO = FLOOR.isoformat()

_VERIFY_STATE = page_state(url='https://x.com/account/access', verify=True)


def _tweet(handle, id_, when, context=''):
    return {'id': id_, 'author': handle, 'name': f'{handle} Person',
            'datetime': when.strftime('%Y-%m-%dT%H:%M:%SZ'), 'text': f'thought {id_}',
            'context': context, 'url': f'https://x.com/{handle}/status/{id_}',
            'article': ''}


class FakePage:
    """驱动真 `harvest` 的假页面 —— 只认 `x_browser` 那四段脚本。

    正常账号给「1 条新推 + 3 条连续老原创推」:新的那条该入库,后面三条让
    `_old_enough` 当场命中,于是这个账号一屏就读完、不会滚满 `MAX_SCROLLS`。
    """

    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.visited: list[str] = []
        self.state_evals: Counter = Counter()
        self.handle = ''

    def open_page(self, url):
        pass

    def goto(self, url):
        self.handle = url.rstrip('/').rsplit('/', 1)[-1]
        self.visited.append(self.handle)

    def _items(self):
        h = self.handle
        return [_tweet(h, '9', FRESH),
                _tweet(h, '3', OLD - timedelta(minutes=1)),
                _tweet(h, '2', OLD - timedelta(minutes=2)),
                _tweet(h, '1', OLD - timedelta(minutes=3))]

    def eval(self, js):
        if js == x_browser._STATE_JS:
            self.state_evals[self.handle] += 1
            if self.verdicts.get(self.handle) == 'verify':
                return json.dumps(_VERIFY_STATE)
            return json.dumps(page_state(url=f'https://x.com/{self.handle}',
                                         articles=4))
        if js == x_browser._COLLECT_JS:
            items = self._items()
            return json.dumps({'articles': len(items), 'parsed': len(items), 'items': items})
        if js == x_browser._SCROLL_JS:
            return json.dumps({'bottom': True})
        return 'true'


@contextmanager
def _session(page):
    yield page


def _harvest(verdicts, handles, floor=FLOOR_ISO):
    page = FakePage(verdicts)
    with patch.object(x_browser, 'browser_session', lambda *a, **k: _session(page)):
        got = x_browser.harvest([(h, floor) for h in handles],
                                profile='/tmp/unused', budget=600)
    return page, got


class HarvestGateTests(unittest.TestCase):
    """harvest 这一层:认出来、立刻抛、整批就此中止、剩下的记"跳过"不记"失败"。"""

    def test_the_whole_batch_stops_at_the_first_verification_page(self):
        """**后果是"剩下的账号一次都没被碰"**,不是"有没有记上标志"。"""
        page, got = _harvest({'a': 'verify'}, ['a', 'b', 'c'])
        self.assertEqual(['a'], page.visited)
        self.assertEqual([], [h for h in ('b', 'c') if h in page.visited])
        for rest in ('b', 'c'):
            self.assertEqual('', got[rest]['error'])
            self.assertEqual(x_browser.VERIFY_SKIPPED, got[rest]['skipped'])
            self.assertEqual([], got[rest]['entries'])

    def test_the_account_that_hit_it_is_the_only_failure(self):
        """命中的那一个照旧记失败 —— 它是这一轮唯一的证据,面板要显示它。"""
        _, got = _harvest({'a': 'verify'}, ['a', 'b', 'c'])
        self.assertIn('人机验证', got['a']['error'])
        self.assertIn('x.com/account/access', got['a']['error'])
        self.assertNotIn('skipped', got['a'])
        self.assertEqual(1, len([h for h, r in got.items() if r.get('error')]))

    def test_it_does_not_wait_out_the_account_budget(self):
        """判据是**问了几次状态**:没有这道判据时要问满 `ACCOUNT_SECONDS`。"""
        page, _ = _harvest({'a': 'verify'}, ['a', 'b'])
        self.assertEqual(1, page.state_evals['a'])

    def test_a_clean_batch_is_never_cut_short(self):
        """反过来钉一边:没有验证页时一个账号都不许被熔断带走。"""
        page, got = _harvest({}, ['a', 'b', 'c'])
        self.assertEqual(['a', 'b', 'c'], page.visited)
        for h in ('a', 'b', 'c'):
            self.assertEqual('', got[h]['error'])
            self.assertNotIn('skipped', got[h])
            self.assertEqual([f'https://x.com/{h}/status/9'],
                             [e['url'] for e in got[h]['entries']])

    def test_a_verification_page_in_the_middle_keeps_what_came_before(self):
        """熔断只掐**后面**那一段:已经抓到手的照旧交出去。"""
        page, got = _harvest({'c': 'verify'}, ['a', 'b', 'c', 'd', 'e'])
        self.assertEqual(['a', 'b', 'c'], page.visited)
        self.assertEqual(1, len(got['a']['entries']))
        self.assertEqual(1, len(got['b']['entries']))
        self.assertIn('人机验证', got['c']['error'])
        for rest in ('d', 'e'):
            self.assertEqual(x_browser.VERIFY_SKIPPED, got[rest]['skipped'])


class RetryListTests(unittest.TestCase):
    """真正要防的那件事:被熔断掐掉的账号**不许进「重试失败项」的名单**。

    进了名单换来的就是下一轮整批重撞 —— 而那正是这道熔断要防的。口径见
    `docs/decisions/retry-scope.md` 那条「预算跳过和退避中不进名单」。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(x_channels, 'BROWSER_PROFILE', self.root / 'profile')):
            p.start(); self.addCleanup(p.stop)
        self.state = deepcopy(pipeline_state._state)
        pipeline_state._state.update(running=False, stop_requested=False)
        self.addCleanup(lambda: pipeline_state._state.update(self.state))
        x_channels.reset(); self.addCleanup(x_channels.reset)
        with reg.db() as c:
            c.execute("DELETE FROM subscriptions WHERE kind='social'")
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True); cookies.write_bytes(b'x')
        reg.save_settings('social', {'enabled': False, 'interval_minutes': 1440,
                                     'x_channel': x_channels.BROWSER})
        self.ids = {}
        for h in ('Aaa', 'Bbb', 'Ccc'):
            self.ids[h.lower()] = reg.save_subscription(
                'social', {'name': h, 'url': f'https://x.com/{h}'})['id']

    def _collect(self, verdicts):
        page = FakePage(verdicts)
        with patch.object(x_browser, 'browser_session', lambda *a, **k: _session(page)):
            return page, sync.collect('social', FetchScope())

    def test_the_skipped_accounts_stay_out_of_the_retry_list(self):
        page, summary = self._collect({'aaa': 'verify'})
        self.assertEqual(['aaa'], page.visited)
        self.assertEqual([str(self.ids['aaa'])], [str(i) for i in summary['failed_ids']])
        self.assertEqual(2, summary['skipped'])
        self.assertEqual(1, len(summary['errors']))

    def test_the_skipped_accounts_are_not_put_into_backoff(self):
        """**后果落在库里那一行**:没被尝试过的不许记失败、不许记退避 ——
        否则下一轮常规同步也会把它们当成"等待重试"跳过。"""
        self._collect({'aaa': 'verify'})
        with reg.db() as c:
            rows = {r['name']: dict(r) for r in c.execute(
                "SELECT name,last_error,failures,retry_at FROM subscriptions "
                "WHERE kind='social'")}
        self.assertIn('人机验证', rows['Aaa']['last_error'])
        for name in ('Bbb', 'Ccc'):
            self.assertEqual('', rows[name]['last_error'])
            self.assertEqual(0, rows[name]['failures'])
            self.assertEqual(0, rows[name]['retry_at'])

    def test_the_panel_blames_verification_not_the_time_budget(self):
        """一律写"预算不足"会把人引去调大预算,而这一轮和预算没有关系。"""
        _, summary = self._collect({'aaa': 'verify'})
        self.assertEqual(x_browser.VERIFY_SKIPPED, summary['message'])
        self.assertNotIn('预算', summary['message'])

    def test_a_clean_round_leaves_an_empty_retry_list(self):
        """反过来钉一边:没有验证页时三条都抓完、名单是空的、文案一个字都不写。"""
        page, summary = self._collect({})
        self.assertEqual(['aaa', 'bbb', 'ccc'], page.visited)
        self.assertEqual([], list(summary['failed_ids']))
        self.assertEqual(0, summary['skipped'])
        self.assertEqual('', summary['message'])
        self.assertEqual(3, summary['checked'])
        self.assertEqual(3, summary['added'])


class VerifyTimelineTests(unittest.TestCase):
    """同一份判据的**另外两个消费口径**:「检查连接」和导入登录态那条链。

    不认 `verify` 的后果不是文案难看:它们会在验证页上**各自白等满超时**、然后
    报 `no-tweets`,而那一档的面板文案把人推去重新登录 —— 登录一万次也过不了那
    张页。所以判据只有一份,三个消费者都得认(元规则「口径只有一份」)。
    """

    class Page:
        def __init__(self, state):
            self.state, self.evals = state, 0

        def eval(self, js, **kw):
            self.evals += 1
            return json.dumps(self.state)

        def open_page(self, url):
            pass

        def goto(self, url):
            pass

    def test_it_reports_verification_instead_of_waiting_out_the_timeout(self):
        """**判据是问了几次**:不认它的话要问满 timeout 才返回 `no-tweets`。"""
        page = self.Page(_VERIFY_STATE)
        verdict, _ = x_browser.verify_timeline(page, timeout=30)
        self.assertEqual('verify', verdict)
        self.assertEqual(1, page.evals)

    def test_a_normal_timeline_is_still_ok(self):
        """反过来钉一边:正常首页照旧 `ok`,没被新判据抢走。"""
        page = self.Page(page_state(url='https://x.com/home', articles=5))
        self.assertEqual('ok', x_browser.verify_timeline(page, timeout=30)[0])

    def test_the_connection_check_does_not_blame_the_login(self):
        """「检查连接」那句话要指到过验证,不是指到重新登录。"""
        page = self.Page(_VERIFY_STATE)
        with patch.object(x_browser, 'profile_busy', lambda *a, **k: False), \
                patch.object(x_browser, 'browser_session',
                             lambda *a, **k: _session(page)):
            ok, detail = x_browser.probe('/tmp/unused')
        self.assertFalse(ok)
        self.assertIn('人机验证', detail)
        self.assertIn('x.com/account/access', detail)
        self.assertNotIn('打开登录窗口', detail)

    def test_the_import_wording_has_its_own_line_for_it(self):
        """导入登录态那条链的结论码 → 人话,也要有这一档。"""
        from kb.feeds import x_login_words
        tail = x_login_words._VERDICT_TAIL['verify']
        self.assertIn('人机验证', tail)
        self.assertNotIn('打开登录窗口', tail)
        # 反过来钉一边:原来那几档一个字都没被动。
        self.assertIn('打开登录窗口', x_login_words._VERDICT_TAIL['login'])


class SkipReasonTests(unittest.TestCase):
    """一轮里有几种跳过原因,面板那句话要**全说出来**,不是只剩最后写进去的那个。

    `skipped` 是各种原因之和,解释也得是:只留一个全局字符串的话,浏览器通道因
    验证中止、HTTP 那几条又耗尽预算的那一轮,给出的解释由线程池完成顺序决定。
    **这里直测 `_note_skip`**,因为端到端要让两种原因在同一轮里同时发生,得靠
    线程调度时序去卡预算边界 —— 那样的用例钉不住口径,只会偶发。原因到面板行文
    的那一段由 `RetryListTests` 端到端覆盖。
    """

    def _summary(self):
        return {'skipped': 0, 'message': '', 'skip_reasons': []}

    def test_both_reasons_survive(self):
        s = self._summary()
        sync._note_skip(s, x_browser.VERIFY_SKIPPED)
        sync._note_skip(s, sync.BUDGET_MESSAGE)
        self.assertIn(x_browser.VERIFY_SKIPPED, s['message'])
        self.assertIn(sync.BUDGET_MESSAGE, s['message'])
        self.assertEqual(2, len(s['skip_reasons']))

    def test_the_same_reason_is_said_once(self):
        """同一个原因跳过几十条,也只说一遍。"""
        s = self._summary()
        for _ in range(30):
            sync._note_skip(s, x_browser.VERIFY_SKIPPED)
        self.assertEqual([x_browser.VERIFY_SKIPPED], s['skip_reasons'])
        self.assertEqual(x_browser.VERIFY_SKIPPED, s['message'])

    def test_the_order_is_the_order_they_happened(self):
        s = self._summary()
        sync._note_skip(s, 'A')
        sync._note_skip(s, 'B')
        sync._note_skip(s, 'A')
        self.assertEqual('A；B', s['message'])


class StatePageTests(unittest.TestCase):
    """`_STATE_JS` 那段判据本身:把**生产那个常量**丢给 node 求值。

    在 Python 里复述那几条正则是证明不了任何事的 —— 复述版和真代码改坏成不一样
    的两种,用例照样全绿。这里 import 来的 `x_browser._STATE_JS` 就是采集时真用
    的那一段,连同 `body`、页面结构和用例一起从 stdin 交给 node(避开 argv 的引号
    语义)。

    **每个输入都要给页面结构**(`app` = X 的应用骨架在不在、`tweets` = 有几张推文
    卡片),因为文案那条判据必须先过这道结构门 —— 审查第 1 轮实测:只凭正文搜索,
    一条**讨论** Cloudflare 的正常推文就能把整批采集停掉。所以这组用例里"文案命中
    但页面是正常的"那几条,红的方向和"挑战页认不出来"正好相反,两边都得钉住。
    """

    CHALLENGE = ('x.com\n正在进行安全验证\n\n本网站使用安全服务防护恶意自动程序。'
                 '在验证您不是自动程序期间，将显示此页面。\n\nRay ID: a4d43d6e9b2ace7a\n'
                 '由 Cloudflare 提供的性能和安全服务\n隐私')

    # (说明, pathname, 正文, 有 X 骨架吗, 几张推文卡片, 该不该判成"要验证")
    CASES = [
        # 这一条的输入是 2026-10-20 真抓到的那张页(url + body 原文,见
        # `docs/decisions/x-channels.md` 那一节),不是照着判据反写的样本。
        ('实测那张账号受限页', '/account/access', CHALLENGE, False, 0, True),
        ('只凭地址也要认出来（挑战文案还没渲染出来那一瞬）',
         '/account/access', 'x.com\n请稍候…', False, 0, True),
        ('挑战页落在别的路径上时,文案那条顶上',
         '/karpathy', 'Verifying you are human. This may take a few seconds.',
         False, 0, True),
        ('连接复核那一句同理', '/karpathy',
         'x.com needs to review the security of your connection before proceeding.',
         False, 0, True),
        # 下面这几条是**误报方向**:页面读得好好的,不许因为正文里有那几句就中止整批。
        ('正常推文原样引用挑战提示 —— 不许命中', '/simonw',
         '首页\n探索\nSimon Willison\n关注\n顺手记一下:Cloudflare 那张页上写的是'
         '“Verifying you are human”,挺眼熟。', True, 12, False),
        ('整段挑战文案被当成推文内容转述 —— 不许命中', '/karpathy',
         '有人给我发了这个:' + CHALLENGE, True, 8, False),
        ('一条推文都没有的号,简介里引用提示 —— 不许命中', '/freshacct',
         '首页\n探索\nFresh Account\n简介:本网站使用安全服务防护恶意自动程序。',
         True, 0, False),
        ('正常时间线本来就不该命中', '/karpathy',
         '首页\n探索\n通知\nAndrej Karpathy\n关注\n一条正经的推文,顺便提一句安全。',
         True, 20, False),
        ('只是正文里提到"二步验证"也不许命中', '/karpathy',
         '我们给登录加了二步验证,安全多了。', True, 5, False),
        # **结构门那两道各自成立,各自有自己的红。** 骨架选择器(`#react-root` /
        # `primaryColumn`)是 X 自己的 DOM 约定,它改名的那天 `!app` 会在**正常
        # 页面上**成立 —— 那时候"一张推文卡片都没有"是最后一道。反过来,页面没
        # 加载完时 `tweets` 为 0 而骨架已经在了,靠的是 `!app` 那一道。少任何一
        # 道,一条转述挑战文案的正常推文就能把整批采集停掉。
        ('骨架认不出来、但读到了推文 —— 不许命中', '/karpathy',
         '有人给我发了这个:' + CHALLENGE, False, 9, False),
        # 路由那条要有结尾边界:挡的是 `/account/access` 自己的**前缀延伸**
        # (`accessibility` 那类),不是 `/account/` 下面的别的页 —— 正则钉的是
        # `^/account/access`,`/account/settings` 在带不带边界两种写法下都不命中,
        # 拿它当反例是测不到边界的(本轮定点注入实测红 0,用例不敏感)。
        ('账号设置页不是受限页', '/account/settings',
         '首页\n账号设置\n修改密码', True, 0, False),
        ('受限页地址的前缀延伸页不是受限页', '/account/accessibility',
         '首页\n辅助功能\n减少动画', True, 3, False),
        ('带查询串的受限页照旧命中', '/account/access?lang=zh',
         'x.com\n请稍候…', False, 0, True),
    ]

    NODE = """
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', d => { input += d; });
process.stdin.on('end', () => {
  const {body, cases} = JSON.parse(input);
  const out = cases.map(([name, url, text, app, tweets]) => {
    const [pathname, search] = url.split('?');
    global.location = {pathname, search: search ? '?' + search : '',
                       href: 'https://x.com' + url};
    global.document = {
      readyState: 'complete',
      body: {innerText: text},
      // 只认得出两类选择器:X 的应用骨架、推文卡片。别的(登录按钮)一律没有。
      querySelector: sel => (app && /react-root|primaryColumn/.test(sel)) ? {} : null,
      querySelectorAll: sel => /article\[data-testid="tweet"\]/.test(sel)
                               ? new Array(tweets).fill({}) : [],
    };
    return JSON.parse(eval(body)).verify;
  });
  process.stdout.write(JSON.stringify(out));
});
"""

    def test_the_verification_judgement_needs_page_level_evidence(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('没有 node')
        payload = json.dumps({'body': x_browser._STATE_JS,
                              'cases': [[n, p, t, a, w]
                                        for n, p, t, a, w, _ in self.CASES]})
        r = subprocess.run([node, '-e', self.NODE], input=payload,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual(len(self.CASES), len(got))
        for (name, _, _, _, _, want), real in zip(self.CASES, got):
            self.assertEqual(want, real, name)


if __name__ == '__main__':
    unittest.main()
