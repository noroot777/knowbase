# -*- coding: utf-8 -*-
"""X 采集通道:选择、水位、整批预取、以及它和 HTTP 那一侧的分工。

这套用例一个真浏览器、一个真 feedgrab 都不起 —— 起一次 Chrome 两三秒,而且
它要联网登录 X,在 CI 和别人的机器上都不成立。被打桩的只有两个边界:
`x_browser.harvest` / `x_feedgrab.harvest`(整批预取那一下)和它们各自的
子进程入口;判定逻辑一行不打桩。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import maintenance, pipeline_state
from kb.collectors import FetchScope
from kb.feeds import fetching, registry as reg, sync, x_browser, x_channels, x_feedgrab
from kb.web import intake_api
from tests.headless import probe_env, requires_headless

# 页面那一侧:probe 自带 mock 后端,CDP 直连 chrome-headless-shell
# (不装 playwright、不连网、始终无头、临时 profile)。见 tests/headless.py。
_PROBE = Path(__file__).resolve().parent / 'x_channel_probe.mjs'

SHANGHAI = ZoneInfo('Asia/Shanghai')
# 样本时间跟着今天走:新订阅只收最近 `reg.HISTORY_DAYS` 天(见 sync._within_history),
# 写死一个日期的话过几天红的是一堆跟通道毫无关系的用例。
FRESH = datetime.now(timezone.utc) - timedelta(hours=6)
FRESH_ISO = FRESH.strftime('%Y-%m-%dT%H:%M:%SZ')
FRESH_LOCAL = FRESH.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M')


def tweet(id_='1', handle='Example', when=None, text='a thought', context=''):
    return {'id': id_, 'author': handle, 'name': 'Example Person',
            'datetime': when or FRESH_ISO, 'text': text, 'context': context,
            'url': f'https://x.com/{handle}/status/{id_}'}


class ChannelBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(x_channels, 'BROWSER_PROFILE', self.root / 'profile'),
                  patch.object(x_channels, 'FEEDGRAB_HOME', self.root / 'feedgrab')):
            p.start(); self.addCleanup(p.stop)
        self.state = deepcopy(pipeline_state._state)
        pipeline_state._state.update(running=False, stop_requested=False)
        self.addCleanup(lambda: pipeline_state._state.update(self.state))
        x_channels.reset(); self.addCleanup(x_channels.reset)
        app = FastAPI(); app.include_router(intake_api.router)
        self.client = TestClient(app)
        # 新库自带 51 个默认大V订阅。这套用例关心的是"通道挑中了谁",
        # 留着它们每一处枚举都要先在 51 条里找自己那几条。
        with reg.db() as c:
            c.execute("DELETE FROM subscriptions WHERE kind='social'")

    def sub(self, handle='Example', **extra):
        body = {'name': handle, 'url': f'https://x.com/{handle}'} | extra
        return reg.save_subscription('social', body)

    def settings(self, channel, **extra):
        return reg.save_settings('social', {'enabled': False, 'interval_minutes': 1440,
                                            'x_channel': channel} | extra)

    def harvested(self, **by_handle):
        """直接摆一份预取结果,相当于"这一轮已经抓完了"。"""
        with x_channels._lock:
            x_channels._harvest.clear()
            x_channels._harvest.update(by_handle)


class ChannelChoiceTests(ChannelBase):
    def test_the_default_is_the_bridge_so_existing_installs_behave_the_same(self):
        s = reg.get_settings('social')
        self.assertEqual(x_channels.BRIDGE, s['x_channel'])
        self.assertEqual('', s['x_feedgrab_bin'])
        self.assertEqual('', s['x_chrome_path'])
        self.assertNotIn('x_channel', reg.get_settings('rss'))

    def test_an_unknown_channel_name_falls_back_to_the_default(self):
        self.assertEqual(x_channels.BRIDGE, x_channels.resolve('no-such-thing'))
        self.assertEqual(x_channels.BRIDGE, x_channels.resolve(''))
        self.assertEqual(x_channels.BROWSER, x_channels.resolve('browser'))

    def test_a_retired_name_is_translated_rather_than_dropped(self):
        """退役名认不出来 = 在用户没动过设置的情况下静默换掉他选的取法。

        所以别名表是读设置这条路上的唯一口径,`label()` 也要翻得出退役名 ——
        历史运行记录里写着的可能正是它,面板上那行不该显示成一个裸 id。
        """
        with patch.dict(x_channels._RETIRED, {'x_cookie': x_channels.FEEDGRAB}, clear=False):
            self.assertEqual(x_channels.FEEDGRAB, x_channels.resolve('x_cookie'))
            self.assertEqual(x_channels.LABELS[x_channels.FEEDGRAB],
                             x_channels.label('x_cookie'))
            with reg.db() as c:
                c.execute('INSERT OR REPLACE INTO feed_settings VALUES(?,?)',
                          ('social', json.dumps({'enabled': False, 'interval_minutes': 1440,
                                                 'bridges': [], 'x_channel': 'x_cookie'})))
            self.assertEqual(x_channels.FEEDGRAB, reg.get_settings('social')['x_channel'])

    def test_saving_rejects_a_relative_binary_path(self):
        with self.assertRaises(ValueError):
            self.settings(x_channels.FEEDGRAB, x_feedgrab_bin='feedgrab')
        with self.assertRaises(ValueError):
            self.settings(x_channels.BROWSER, x_chrome_path='chrome')
        self.settings(x_channels.BROWSER, x_chrome_path='')     # 留空 = 用默认位置

    def test_only_social_has_channels(self):
        self.assertTrue(x_channels.supports_channels('social'))
        self.assertFalse(x_channels.supports_channels('rss'))
        self.assertEqual(404, self.client.get('/api/intake/rss/x-channel').status_code)
        self.assertEqual(404, self.client.get('/api/intake/wechat/x-channel').status_code)

    def test_channel_fields_are_dropped_for_kinds_without_channels(self):
        """rss 的设置里不该留下一份谁也不读、也没校验过的通道配置。"""
        r = self.client.put('/api/intake/rss/settings', json={
            'enabled': False, 'interval_minutes': 360, 'x_channel': 'browser',
            'x_chrome_path': 'relative/is/not/even/valid'})
        self.assertEqual(200, r.status_code)
        self.assertNotIn('x_channel', reg.get_settings('rss'))
        self.assertNotIn('x_chrome_path', reg.get_settings('rss'))


class TargetTests(ChannelBase):
    def test_the_bridge_channel_takes_nobody(self):
        self.sub()
        self.assertEqual([], x_channels.targets(reg.subscriptions('social'),
                                                self.settings(x_channels.BRIDGE)))

    def test_a_dedicated_feed_url_still_wins_over_any_channel(self):
        """专用订阅链接是桥接全挂时唯一还能采的那批,通道不许把它接管过去。"""
        self.sub('Mine', feed_url='https://example.org/mine.xml')
        self.sub('Shared')
        picked = x_channels.targets(reg.subscriptions('social'),
                                    self.settings(x_channels.BROWSER))
        self.assertEqual(['Shared'], [s['name'] for s in picked])

    def test_a_non_x_social_subscription_is_left_on_http(self):
        self.sub('Bluesky', url='https://bsky.app/profile/someone')
        self.assertEqual([], x_channels.targets(reg.subscriptions('social'),
                                                self.settings(x_channels.BROWSER)))


class WatermarkTests(ChannelBase):
    def test_each_channel_keeps_its_own_watermark(self):
        """两条通道的精度不同(浏览器到分钟、feedgrab 到天),混用一个键的话
        切过去那一轮会拿带时分的值去填 `X_USER_TWEETS_SINCE`(它只认日期)。"""
        s = self.sub()
        x_channels.save_watermark(s['id'], x_channels.BROWSER, '2026-10-07T01:02:03+00:00')
        x_channels.save_watermark(s['id'], x_channels.FEEDGRAB, '2026-10-05')
        s = reg.subscription('social', s['id'])
        self.assertEqual('2026-10-07T01:02:03+00:00',
                         x_channels.watermark(s, x_channels.BROWSER))
        self.assertEqual('2026-10-05', x_channels.watermark(s, x_channels.FEEDGRAB))

    def test_saving_one_watermark_keeps_the_other_keys(self):
        s = self.sub()
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET config=? WHERE id=?',
                      (json.dumps({'someone_elses': 'value'}), s['id']))
        x_channels.save_watermark(s['id'], x_channels.BROWSER, '2026-10-07T00:00:00+00:00')
        cfg = json.loads(reg.subscription('social', s['id'])['config'])
        self.assertEqual('value', cfg['someone_elses'])

    def test_a_broken_config_blob_reads_as_empty_rather_than_raising(self):
        s = self.sub()
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET config=? WHERE id=?', ('not json', s['id']))
        s = reg.subscription('social', s['id'])
        self.assertEqual('', x_channels.watermark(s, x_channels.BROWSER))
        x_channels.save_watermark(s['id'], x_channels.BROWSER, '2026-10-07T00:00:00+00:00')
        self.assertEqual('2026-10-07T00:00:00+00:00',
                         x_channels.watermark(reg.subscription('social', s['id']),
                                              x_channels.BROWSER))


class TakeTests(ChannelBase):
    def entry(self, url='https://x.com/Example/status/1'):
        return {'url': url, 'title': 'a thought', 'body': 'a thought',
                'author': 'Example Person', 'published': FRESH_LOCAL, 'content_kind': 'post'}

    def test_a_handle_the_prefetch_never_reached_raises_rather_than_reading_empty(self):
        """静默返回空会被记成"这个号今天没发推" —— 那是在报一个没做过的结论。"""
        s = self.sub()
        settings = self.settings(x_channels.BROWSER)
        with self.assertRaises(LookupError):
            x_channels.take(reg.subscription('social', s['id']), settings)

    def test_a_per_account_failure_becomes_this_subscriptions_failure(self):
        s = self.sub()
        self.harvested(example={'entries': [], 'error': '@Example 这个账号在 X 上打不开'})
        with self.assertRaises(ValueError) as caught:
            x_channels.take(reg.subscription('social', s['id']), self.settings(x_channels.BROWSER))
        self.assertIn('打不开', str(caught.exception))

    def test_an_account_the_budget_did_not_reach_is_deferred_not_failed(self):
        s = self.sub()
        self.harvested(example={'entries': [], 'error': '', 'skipped': '本轮时间预算用完'})
        with self.assertRaises(fetching.CollectionBudgetExceeded):
            x_channels.take(reg.subscription('social', s['id']), self.settings(x_channels.BROWSER))

    def test_taking_keeps_the_http_validators_and_hands_back_the_watermark(self):
        """这条路没有 etag 可言,但写回空串会把之前走 HTTP 攒下的缓存抹掉 ——
        切回桥接通道时就要白拉一次全量。

        **水位只是作为补丁交回去,这一步一个字节都不写盘**:存盘要和条目在同一个
        事务里(见 `WatermarkCommitTests`)。
        """
        s = self.sub()
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET etag=?, modified=?, resolved_url=? WHERE id=?",
                      ('"abc"', 'Mon, 01 Jan 2026 00:00:00 GMT',
                       'https://bridge.example/Example', s['id']))
        self.harvested(example={'entries': [self.entry()], 'error': '',
                                'watermark': '2026-10-07T00:00:00+00:00'})
        entries, meta = x_channels.take(reg.subscription('social', s['id']),
                                        self.settings(x_channels.BROWSER))
        self.assertEqual(1, len(entries))
        self.assertEqual('"abc"', meta['etag'])
        self.assertEqual('https://bridge.example/Example', meta['resolved_url'])
        self.assertEqual({'x_watermark_browser': '2026-10-07T00:00:00+00:00'},
                         meta.get('config_patch'))
        self.assertEqual('', x_channels.watermark(reg.subscription('social', s['id']),
                                                  x_channels.BROWSER))

    def test_an_incomplete_round_comes_back_as_a_note_not_a_failure(self):
        """抓到的照旧入库,但"停在时间线半中间"要在面板上看得见。"""
        s = self.sub()
        self.harvested(example={'entries': [self.entry()], 'error': '',
                                'incomplete': '滚动次数用完,没有读到上次的位置',
                                'watermark': ''})
        entries, meta = x_channels.take(reg.subscription('social', s['id']),
                                        self.settings(x_channels.BROWSER))
        self.assertEqual(1, len(entries))
        self.assertIn('滚动次数', meta['note'])
        self.assertNotIn('config_patch', meta)


class DispatchTests(ChannelBase):
    """`fetch_source` 的第一道分派:谁走通道、谁留在 HTTP 上。"""

    def test_a_login_channel_short_circuits_before_any_http_request(self):
        s = self.sub()
        self.harvested(example={'entries': [], 'error': '', 'watermark': ''})
        with patch.object(fetching, 'request',
                          side_effect=AssertionError('不该发 HTTP 请求')):
            entries, _ = fetching.fetch_source(reg.subscription('social', s['id']),
                                               self.settings(x_channels.BROWSER))
        self.assertEqual([], entries)

    def test_the_bridge_channel_still_goes_through_http(self):
        s = self.sub()
        settings = self.settings(x_channels.BRIDGE,
                                 bridges=['https://bridge.example/{handle}/rss'])
        with patch.object(fetching, 'request', side_effect=RuntimeError('到 HTTP 了')):
            with self.assertRaises(Exception) as caught:
                fetching.fetch_source(reg.subscription('social', s['id']), settings)
        self.assertIn('到 HTTP', str(caught.exception))

    def test_a_subscription_with_its_own_feed_url_is_never_short_circuited(self):
        s = self.sub('Mine', feed_url='https://example.org/mine.xml')
        self.harvested(mine={'entries': [], 'error': '', 'watermark': ''})
        with patch.object(fetching, 'request', side_effect=RuntimeError('到 HTTP 了')):
            with self.assertRaises(Exception) as caught:
                fetching.fetch_source(reg.subscription('social', s['id']),
                                      self.settings(x_channels.BROWSER))
        self.assertIn('到 HTTP', str(caught.exception))

    def test_rss_sources_are_untouched_by_the_channel_setting(self):
        s = reg.save_subscription('rss', {'name': 'Blog', 'url': 'https://example.org/f.xml'})
        with patch.object(fetching, 'request', side_effect=RuntimeError('到 HTTP 了')):
            with self.assertRaises(Exception):
                fetching.fetch_source(reg.subscription('rss', s['id']),
                                      reg.get_settings('rss'))


class PrefetchTests(ChannelBase):
    def test_the_prefetch_passes_each_accounts_own_watermark(self):
        a, b = self.sub('Alpha'), self.sub('Beta')
        x_channels.save_watermark(a['id'], x_channels.BROWSER, '2026-10-01T00:00:00+00:00')
        seen = {}

        def fake(jobs, **kw):
            seen.update(dict(jobs))
            return {h: {'entries': [], 'error': '', 'watermark': w} for h, w in jobs}

        with patch.object(x_browser, 'harvest', fake):
            x_channels.prefetch(reg.subscriptions('social'), self.settings(x_channels.BROWSER))
        self.assertEqual({'alpha': '2026-10-01T00:00:00+00:00', 'beta': ''}, seen)
        self.assertEqual(b['name'], 'Beta')

    def test_a_stop_request_during_the_prefetch_defers_the_rest(self):
        """这一批要跑十几分钟。不认停止的话,按下去要等满整轮预算。"""
        self.sub('Alpha'); self.sub('Beta')
        pipeline_state._state['stop_requested'] = True
        with patch.object(x_browser, 'Chrome', side_effect=AssertionError('不该起浏览器')):
            sync._prefetch_x('social', reg.subscriptions('social'),
                             self.settings(x_channels.BROWSER), FetchScope())
        with x_channels._lock:
            got = dict(x_channels._harvest)
        self.assertEqual({'alpha', 'beta'}, set(got))
        self.assertTrue(all(g['skipped'] for g in got.values()))

    def test_the_bridge_channel_never_starts_a_browser(self):
        self.sub('Alpha')
        with patch.object(x_browser, 'harvest', side_effect=AssertionError('不该预取')), \
                patch.object(x_feedgrab, 'harvest', side_effect=AssertionError('不该预取')):
            sync._prefetch_x('social', reg.subscriptions('social'),
                             self.settings(x_channels.BRIDGE), FetchScope())

    def test_a_stale_harvest_from_a_previous_round_is_cleared(self):
        """上一轮的结果留着 = 这一轮一条都没抓到时却"取"得出东西来。"""
        self.sub('Alpha')
        self.harvested(alpha={'entries': [self.__class__ and {'url': 'x'}], 'error': ''})
        with patch.object(x_browser, 'harvest', lambda jobs, **kw: {}):
            x_channels.prefetch(reg.subscriptions('social'), self.settings(x_channels.BROWSER))
        with x_channels._lock:
            self.assertEqual({}, dict(x_channels._harvest))



class ConfigurationErrorTests(ChannelBase):
    """「这条订阅能不能开工」要问**当前**通道,不是永远问桥接。"""

    def test_the_bridge_channel_still_asks_for_a_template(self):
        s = self.sub()
        settings = self.settings(x_channels.BRIDGE)
        self.assertIn('桥接', reg.configuration_error(reg.subscription('social', s['id']),
                                                    settings))
        settings = self.settings(x_channels.BRIDGE,
                                 bridges=['https://bridge.example/{handle}/rss'])
        self.assertEqual('', reg.configuration_error(reg.subscription('social', s['id']),
                                                     settings))

    def test_the_browser_channel_asks_for_a_login_not_for_a_bridge(self):
        """选了浏览器通道的人看到一片"请填写 X 订阅桥接",说的是跟他这一轮采集
        毫无关系的一个设置。"""
        s = self.sub()
        settings = self.settings(x_channels.BROWSER)
        why = reg.configuration_error(reg.subscription('social', s['id']), settings)
        self.assertIn('登录', why)
        self.assertNotIn('桥接', why)
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True); cookies.write_bytes(b'x')
        self.assertEqual('', reg.configuration_error(reg.subscription('social', s['id']),
                                                     settings))

    def test_the_feedgrab_channel_asks_for_the_binary_then_for_the_login(self):
        s = self.sub()
        settings = self.settings(x_channels.FEEDGRAB)
        with patch('shutil.which', return_value=None):
            self.assertIn('feedgrab',
                          reg.configuration_error(reg.subscription('social', s['id']), settings))
        with patch('shutil.which', return_value='/usr/local/bin/feedgrab'):
            why = reg.configuration_error(reg.subscription('social', s['id']), settings)
            self.assertIn('登录', why)
            sessions = x_channels.FEEDGRAB_HOME / 'sessions'
            sessions.mkdir(parents=True); (sessions / 'twitter.json').write_text('{}')
            self.assertEqual('', reg.configuration_error(
                reg.subscription('social', s['id']), settings))

    def test_a_dedicated_feed_url_is_never_blocked_by_a_channel_problem(self):
        s = self.sub('Mine', feed_url='https://example.org/mine.xml')
        self.assertEqual('', reg.configuration_error(reg.subscription('social', s['id']),
                                                     self.settings(x_channels.BROWSER)))


class ChannelApiTests(ChannelBase):
    def test_the_panel_gets_the_channel_list_the_aliases_and_a_cheap_verdict(self):
        self.settings(x_channels.BROWSER)
        data = self.client.get('/api/intake/social/x-channel').json()
        self.assertEqual('browser', data['current'])
        self.assertEqual(list(x_channels.CHANNELS), [c['id'] for c in data['channels']])
        self.assertTrue(all(c['label'] and c['hint'] for c in data['channels']))
        self.assertFalse(data['ready'])
        self.assertTrue(data['needs_login_window'])
        self.assertEqual('', data['login_command'])

    def test_the_feedgrab_channel_hands_back_the_command_to_run(self):
        self.settings(x_channels.FEEDGRAB)
        data = self.client.get('/api/intake/social/x-channel').json()
        self.assertIn('feedgrab login twitter', data['login_command'])
        self.assertIn(str(x_channels.FEEDGRAB_HOME / 'sessions'), data['login_command'])
        self.assertFalse(data['needs_login_window'])

    def test_opening_a_login_window_is_refused_on_the_other_channels(self):
        self.settings(x_channels.FEEDGRAB)
        with patch.object(x_browser, 'open_login_window',
                          side_effect=AssertionError('不该开窗口')):
            r = self.client.post('/api/intake/social/x-channel/login')
        self.assertEqual(400, r.status_code)

    def test_opening_a_login_window_reports_what_to_do_next(self):
        self.settings(x_channels.BROWSER)
        with patch.object(x_browser, 'open_login_window', return_value='已打开登录窗口。'):
            r = self.client.post('/api/intake/social/x-channel/login')
        self.assertEqual(200, r.status_code)
        self.assertIn('已打开', r.json()['detail'])

    def test_a_failed_login_window_is_a_message_not_a_traceback(self):
        self.settings(x_channels.BROWSER)
        with patch.object(x_browser, 'open_login_window',
                          side_effect=x_browser.XBrowserError('登录窗口已经开着了')):
            r = self.client.post('/api/intake/social/x-channel/login')
        self.assertEqual(400, r.status_code)
        self.assertIn('已经开着', r.json()['detail'])

    def test_the_two_action_endpoints_only_answer_post(self):
        """页面上那两颗按钮打的是动作端点,不是读取 —— 这一条和下一条合起来钉住
        「前端发什么方法」和「后端收什么方法」是同一个。"""
        for path in ('/api/intake/social/x-channel/check',
                     '/api/intake/social/x-channel/login'):
            self.assertEqual(405, self.client.get(path).status_code, path)

    def test_the_panel_sends_those_two_with_post(self):
        """`pipeJson(path, {}, 120000)` 发的是 **GET**(选项里没有 method,fetch
        默认 GET),于是「检查连接」在真页面上永远是 405 Method Not Allowed,
        状态行还把它当成结论贴出来(2026-10-08 审查实测)。

        单测发对了方法、探针那个 mock 又只按 URL 匹配 —— 两边都看不见这个缺口,
        所以这里直接读那份前端代码:两个动作端点必须经 `pipePost`。
        """
        js = (Path(intake_api.__file__).parents[2] / 'kb' / 'static' / 'js'
              / 'x_channel.js').read_text(encoding='utf-8')
        for action in ('check', 'login'):
            calls = [line.strip() for line in js.splitlines()
                     if f"x-channel/{action}`" in line]
            self.assertTrue(calls, action)
            for line in calls:
                self.assertIn('pipePost(', line, line)
                self.assertNotIn('pipeJson(', line, line)

    def test_checking_the_browser_channel_really_opens_one(self):
        self.settings(x_channels.BROWSER)
        with patch.object(x_browser, 'probe', return_value=(True, '登录态有效')) as probe:
            r = self.client.post('/api/intake/social/x-channel/check')
        self.assertEqual((True, '登录态有效'), (r.json()['ready'], r.json()['detail']))
        self.assertEqual(str(x_channels.BROWSER_PROFILE), probe.call_args[0][0])

    def test_checking_the_feedgrab_channel_runs_its_doctor(self):
        self.settings(x_channels.FEEDGRAB)
        with patch.object(x_feedgrab, 'doctor', return_value=(False, '还没有 X 登录态')):
            r = self.client.post('/api/intake/social/x-channel/check')
        self.assertFalse(r.json()['ready'])
        self.assertIn('登录态', r.json()['detail'])

    def test_checking_the_bridge_channel_says_what_it_cannot_know(self):
        """桥接没有"连一次"可言。谎称已就绪会让人以为公共实例是好的。"""
        self.settings(x_channels.BRIDGE)
        self.assertEqual(400, self.client.post('/api/intake/social/x-channel/check').status_code)
        self.settings(x_channels.BRIDGE, bridges=['https://bridge.example/{handle}/rss'])
        detail = self.client.post('/api/intake/social/x-channel/check').json()['detail']
        self.assertIn('真抓一次', detail)

    def test_saving_a_channel_through_the_settings_endpoint(self):
        r = self.client.put('/api/intake/social/settings', json={
            'enabled': False, 'interval_minutes': 1440, 'x_channel': 'browser',
            'x_chrome_path': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'})
        self.assertEqual(200, r.status_code)
        self.assertEqual('browser', reg.get_settings('social')['x_channel'])
        bad = self.client.put('/api/intake/social/settings', json={
            'enabled': False, 'interval_minutes': 1440, 'x_channel': 'browser',
            'x_chrome_path': 'chrome'})
        self.assertEqual(400, bad.status_code)
        self.assertEqual('browser', reg.get_settings('social')['x_channel'])


class CollectIntegrationTests(ChannelBase):
    """整轮采集:预取 → 逐条取走 → 入库。"""

    def harvest_of(self, *handles, delay=0.0):
        def fake(jobs, **kw):
            if delay:
                time.sleep(delay)
            return {h: {'entries': [x_browser._entry(tweet('1', handle=h))],
                        'error': '', 'watermark': x_browser._newest(
                            [x_browser._entry(tweet('1', handle=h))], w)}
                    for h, w in jobs}
        return fake

    def test_a_browser_round_lands_entries_and_a_watermark(self):
        s = self.sub('Example')
        settings = self.settings(x_channels.BROWSER)
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True); cookies.write_bytes(b'x')
        with patch.object(x_browser, 'harvest', self.harvest_of()):
            summary = sync.collect('social', FetchScope())
        self.assertEqual(1, summary['added'])
        self.assertEqual(0, len(summary['errors']))
        self.assertEqual(0, summary['unconfigured'])
        with reg.db() as c:
            rows = c.execute('SELECT url, published FROM feed_items').fetchall()
        self.assertEqual(['https://x.com/example/status/1'], [r['url'] for r in rows])
        self.assertEqual(FRESH_LOCAL, rows[0]['published'])
        self.assertTrue(x_channels.watermark(reg.subscription('social', s['id']),
                                             x_channels.BROWSER))
        del settings

    def test_the_prefetch_time_is_not_charged_to_the_http_budget(self):
        """预取跑十几分钟是常态。采集预算算在它**前面**的话,预取一跑完每条订阅
        都已经超预算,整轮一条也取不走 —— 而东西明明已经抓在手里了。"""
        self.sub('Example')
        self.settings(x_channels.BROWSER)
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True); cookies.write_bytes(b'x')
        with patch.object(sync, 'COLLECT_SECONDS', 1), \
                patch.object(sync, 'SOURCE_SECONDS', 0.2), \
                patch.object(x_browser, 'harvest', self.harvest_of(delay=1.2)):
            summary = sync.collect('social', FetchScope())
        self.assertEqual(0, summary['skipped'])
        self.assertEqual(1, summary['added'])

    def test_a_login_channel_failure_never_trips_the_bridge_breaker(self):
        """熔断防的是"公共实例挂了,别再拿 50 个账号去撞"。登录态通道的内容已经
        在预取里抓完,断开只会白扔掉已经付过代价的结果。"""
        for i in range(8):
            self.sub(f'Acct{i}')
        self.settings(x_channels.BROWSER)
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True); cookies.write_bytes(b'x')

        def fake(jobs, **kw):
            return {h: ({'entries': [], 'error': 'X 登录态已失效'} if i < 5 else
                        {'entries': [x_browser._entry(tweet('1', handle=h))],
                         'error': '', 'watermark': ''})
                    for i, (h, w) in enumerate(jobs)}

        with patch.object(x_browser, 'harvest', fake):
            summary = sync.collect('social', FetchScope())
        self.assertEqual(8, summary['checked'])
        self.assertEqual(0, summary['skipped'])
        self.assertEqual('', summary['message'])
        self.assertEqual(3, summary['added'])

    def test_the_unconfigured_note_names_the_current_channels_problem(self):
        self.sub('Example')
        self.settings(x_channels.BROWSER)
        stages = []
        with patch.object(sync, '_stage', lambda *a, **k: stages.append(a)), \
                patch.object(x_browser, 'harvest',
                             side_effect=AssertionError('不该预取,这条订阅待配置')):
            summary = sync.collect('social', FetchScope())
        self.assertEqual(1, summary['unconfigured'])
        self.assertIn('登录', stages[-1][2])
        self.assertNotIn('桥接', stages[-1][2])


@requires_headless
class BrowserTests(unittest.TestCase):
    """面板上那一块:三条通道各要填什么互斥显示、改了选择不冒充当前事实、
    保存真把三个字段发出去、两个按钮各打各的端点、待配置横幅按当前通道说话。"""

    def test_the_channel_picker_works_on_a_real_page(self):
        r = subprocess.run([shutil.which('node'), str(_PROBE)], capture_output=True,
                           text=True, timeout=300, env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
