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

FRESH = datetime.now(timezone.utc) - timedelta(hours=3)
OLD = datetime.now(timezone.utc) - timedelta(days=9)
FLOOR = datetime.now(timezone.utc) - timedelta(days=4)
FLOOR_ISO = FLOOR.isoformat()

_VERIFY_STATE = {'url': 'https://x.com/account/access', 'ready': 'complete',
                 'articles': 0, 'login': False, 'verify': True,
                 'empty': False, 'limited': False}


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
            return json.dumps({'url': f'https://x.com/{self.handle}', 'ready': 'complete',
                               'articles': 4, 'login': False, 'verify': False,
                               'empty': False, 'limited': False})
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


class StatePageTests(unittest.TestCase):
    """`_STATE_JS` 那段判据本身:把**生产那个常量**丢给 node 求值。

    在 Python 里复述那几条正则是证明不了任何事的 —— 复述版和真代码改坏成不一样
    的两种,用例照样全绿。这里 import 来的 `x_browser._STATE_JS` 就是采集时真用
    的那一段,连同 `body` 和用例一起从 stdin 交给 node(避开 argv 的引号语义)。
    """

    CASES = [
        # 这一条的输入是 2026-10-20 真抓到的那张页(url + body 原文,见
        # `docs/decisions/x-channels.md` 那一节),不是照着判据反写的样本。
        ('实测那张账号受限页', '/account/access',
         'x.com\n正在进行安全验证\n\n本网站使用安全服务防护恶意自动程序。'
         '在验证您不是自动程序期间，将显示此页面。\n\nRay ID: a4d43d6e9b2ace7a\n'
         '由 Cloudflare 提供的性能和安全服务\n隐私', True),
        ('只凭地址也要认出来（挑战文案还没渲染出来那一瞬）',
         '/account/access', 'x.com\n请稍候…', True),
        ('Cloudflare 中文挑战页', '/karpathy',
         '正在进行安全验证\n本网站使用安全服务防护恶意自动程序。', True),
        ('Cloudflare 英文挑战页', '/karpathy',
         'Verifying you are human. This may take a few seconds.', True),
        ('连接复核那一句', '/karpathy',
         'x.com needs to review the security of your connection before proceeding.', True),
        ('正常时间线不许命中', '/karpathy',
         '首页\n探索\n通知\nAndrej Karpathy\n关注\n一条正经的推文,顺便提一句安全。', False),
        ('只是正文里提到"验证"也不许命中', '/karpathy',
         '我们给登录加了二步验证,安全多了。', False),
    ]

    NODE = """
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', d => { input += d; });
process.stdin.on('end', () => {
  const {body, cases} = JSON.parse(input);
  const out = cases.map(([name, pathname, text]) => {
    global.location = {pathname, href: 'https://x.com' + pathname};
    global.document = {readyState: 'complete', body: {innerText: text},
                       querySelector: () => null, querySelectorAll: () => []};
    return JSON.parse(eval(body)).verify;
  });
  process.stdout.write(JSON.stringify(out));
});
"""

    def test_the_verification_judgement_matches_only_the_challenge_pages(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('没有 node')
        payload = json.dumps({'body': x_browser._STATE_JS,
                              'cases': [[n, p, t] for n, p, t, _ in self.CASES]})
        r = subprocess.run([node, '-e', self.NODE], input=payload,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual(len(self.CASES), len(got))
        for (name, _, _, want), real in zip(self.CASES, got):
            self.assertEqual(want, real, name)


if __name__ == '__main__':
    unittest.main()
