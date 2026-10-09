# -*- coding: utf-8 -*-
"""第四条 X 通道「我日常用的浏览器」(`attach`)接进 `x_channels` 的那一圈。

**为什么另拆一个文件**:`tests/test_x_channels.py` 已经 627 行,贴着 700 上限,
理由同 `test_x_feedgrab_resume` 那次按形状拆分。摆盘(`ChannelBase`)仍共用那一
份 —— 各写一份的话两边的前提会各漂各的。

传输和发现那一层在 `tests/test_cdp_attach.py`,这里只管**它作为一条通道**要守的
规矩:前端不用改一个字就能选它、它跟 `browser` 共用水位、它不在这里登录。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from kb.feeds import cdp_attach, registry as reg, x_browser, x_channels
from tests.test_x_channels import ChannelBase

ATTACH = x_channels.ATTACH
BROWSER = x_channels.BROWSER


class ChannelTableTests(ChannelBase):
    def test_the_channel_is_selectable_and_describes_its_own_cost(self):
        """**代价要写在选项上。** 开着远程调试的时候本机任何程序都能连上那个
        浏览器(连同里面所有站点的登录态),这不是一句可以留给文档的脚注 ——
        选这一档的人就是在接受它。
        """
        self.assertIn(ATTACH, x_channels.CHANNELS)
        self.assertTrue(x_channels.LABELS[ATTACH])
        hint = x_channels.HINTS[ATTACH]
        self.assertIn('登录态', hint)
        self.assertIn('本机任何程序', hint)

    def test_choosing_it_round_trips_through_the_settings(self):
        self.assertEqual(ATTACH, x_channels.resolve(ATTACH))
        self.assertEqual(ATTACH, self.settings(ATTACH)['x_channel'])
        self.assertEqual(ATTACH, x_channels.channel_of(reg.get_settings('social')))

    def test_it_needs_nothing_filled_in(self):
        """这一档没有要填的东西(没有路径、没有桥接地址)—— 保存它不该被校验拦下。"""
        self.settings(ATTACH, x_feedgrab_bin='', x_chrome_path='')
        self.assertEqual(ATTACH, reg.get_settings('social')['x_channel'])

    def test_it_takes_the_same_subscriptions_as_the_other_login_channels(self):
        self.sub('Mine', feed_url='https://example.org/mine.xml')
        self.sub('Shared')
        picked = x_channels.targets(reg.subscriptions('social'), self.settings(ATTACH))
        self.assertEqual(['Shared'], [s['name'] for s in picked])


class WatermarkSharingTests(ChannelBase):
    def test_it_shares_the_browser_watermark_key(self):
        """两条路读的是**同一个浏览器时间线**、精度都到分钟,语义逐字相同。
        各记一个键的话,从独立 profile 换到日常浏览器那一轮会按空水位把整个
        时间线重抓一遍 —— 而那段内容早就收过了。

        (对比 feedgrab:它的下界只到天,混用会拿带时分的值去填
        `X_USER_TWEETS_SINCE`,所以它**必须**另记一个键。)
        """
        s = self.sub()
        x_channels.save_watermark(s['id'], ATTACH, '2026-10-07T01:02:03+00:00')
        s = reg.subscription('social', s['id'])
        self.assertEqual('2026-10-07T01:02:03+00:00', x_channels.watermark(s, ATTACH))
        self.assertEqual('2026-10-07T01:02:03+00:00', x_channels.watermark(s, BROWSER))

    def test_it_does_not_disturb_the_feedgrab_watermark(self):
        s = self.sub()
        x_channels.save_watermark(s['id'], x_channels.FEEDGRAB, '2026-10-05')
        x_channels.save_watermark(s['id'], ATTACH, '2026-10-07T01:02:03+00:00')
        s = reg.subscription('social', s['id'])
        self.assertEqual('2026-10-05', x_channels.watermark(s, x_channels.FEEDGRAB))


class ReadinessTests(ChannelBase):
    def test_ready_when_a_browser_is_listening(self):
        found = cdp_attach.Endpoint('127.0.0.1', 9222, '/devtools/browser/x', 'Chrome')
        with patch.object(cdp_attach, 'discover', return_value=found):
            ready, why, hint = x_channels.readiness(self.settings(ATTACH))
        self.assertTrue(ready)
        self.assertEqual(('', ''), (why, hint))

    def test_not_ready_says_how_to_turn_the_switch_on(self):
        """「没就绪」要带上下一步,而且下一步只能是**用户自己去勾那个开关** ——
        我们不改他的启动参数、不写他的 Local State、也不去唤起那个页面。
        """
        with patch.object(cdp_attach, 'discover', return_value=None):
            ready, why, hint = x_channels.readiness(self.settings(ATTACH))
        self.assertFalse(ready)
        self.assertIn('远程调试', why)
        self.assertEqual(cdp_attach.ENABLE_HINT, hint)
        self.assertIn('chrome://inspect/#remote-debugging', hint)

    def test_the_banner_speaks_for_this_channel_not_for_the_bridge(self):
        """`configuration_error` 整句都由 `readiness` 说 —— 选了这一档的人看到
        「先填写 X 订阅桥接」说的是跟他这一轮采集毫无关系的一个设置。"""
        s = self.sub()
        settings = self.settings(ATTACH)
        with patch.object(cdp_attach, 'discover', return_value=None):
            problem = reg.configuration_error(reg.subscription('social', s['id']), settings)
        self.assertIn('远程调试', problem)
        self.assertNotIn('桥接', problem)

    def test_a_ready_browser_leaves_no_banner(self):
        s = self.sub()
        settings = self.settings(ATTACH)
        found = cdp_attach.Endpoint('127.0.0.1', 9222, '/devtools/browser/x', 'Chrome')
        with patch.object(cdp_attach, 'discover', return_value=found):
            self.assertEqual('', reg.configuration_error(
                reg.subscription('social', s['id']), settings))


class DispatchTests(ChannelBase):
    """「这一轮用谁的浏览器」只在一处决定,两档各验一边。"""

    def test_prefetch_asks_for_the_users_own_browser(self):
        self.sub()
        seen = {}

        def fake(jobs, **kw):
            seen.update(kw)
            return {}

        with patch.object(x_browser, 'harvest', fake):
            x_channels.prefetch(reg.subscriptions('social'), self.settings(ATTACH))
        self.assertIs(seen.get('attach'), True)

    def test_the_independent_profile_channel_still_launches_its_own(self):
        """反过来钉一边:把 `attach` 写死成 True 的实现会让这条红。"""
        self.sub()
        seen = {}

        def fake(jobs, **kw):
            seen.update(kw)
            return {}

        with patch.object(x_browser, 'harvest', fake):
            x_channels.prefetch(reg.subscriptions('social'), self.settings(BROWSER))
        self.assertIs(seen.get('attach'), False)

    def test_check_really_connects_through_the_same_path(self):
        seen = {}

        def fake(profile, chrome_path='', **kw):
            seen.update(kw)
            return True, '连上了'

        with patch.object(x_browser, 'probe', fake):
            ready, detail = x_channels.check(self.settings(ATTACH))
        self.assertIs(seen.get('attach'), True)
        self.assertEqual((True, '连上了'), (ready, detail))

    def test_probe_short_circuits_when_nothing_is_listening(self):
        """`readiness` 是便宜的判断,`check` 才真连一次;但真连之前先问一句
        「有没有浏览器开着」,否则用户点「检查连接」收到的是一句 WebSocket
        连不上的底层报错,看不出该去勾那个开关。"""
        with patch.object(cdp_attach, 'discover', return_value=None), \
             patch.object(cdp_attach, 'Attached',
                          side_effect=AssertionError('不该去连')):
            ready, detail = x_browser.probe('', '', attach=True)
        self.assertFalse(ready)
        self.assertIn(cdp_attach.ENABLE_HINT, detail)

    def test_this_channel_never_touches_the_independent_profile(self):
        """独立 profile 那条有一道「SingletonLock 还在 = 登录窗口开着」的前置
        检查。它对这一档毫无意义(我们压根不用那个 profile),拿它把这一档拦下
        就是让「登录窗口忘了关」把日常浏览器那条路一起堵掉。"""
        profile = x_channels.BROWSER_PROFILE
        profile.mkdir(parents=True, exist_ok=True)
        (profile / 'SingletonLock').write_text('x', 'utf-8')
        with patch.object(cdp_attach, 'discover', return_value=None):
            _ready, detail = x_browser.probe(str(profile), '', attach=True)
        self.assertNotIn('SingletonLock', detail)
        self.assertNotIn('登录窗口', detail)


class LoginEndpointTests(ChannelBase):
    def test_this_channel_does_not_log_in_here(self):
        """**拒绝的文案不许建议他换档。** 他要的登录态就在自己那个浏览器里,
        我们开一个独立 profile 的有头窗口对他毫无用处 —— 那句"先把取法改成
        「独立 profile 的登录态」"等于让他把刚选对的那一档改错。
        """
        self.settings(ATTACH)
        with patch.object(x_browser, 'open_login_window',
                          side_effect=AssertionError('不该开窗口')):
            r = self.client.post('/api/intake/social/x-channel/login')
        self.assertEqual(400, r.status_code)
        detail = r.json()['detail']
        self.assertIn('x.com', detail)                       # 下一步:在那儿登录
        self.assertIn(cdp_attach.ENABLE_HINT, detail)
        self.assertNotIn('保存设置', detail)                  # 不是"改档再来"
        self.assertNotIn(x_channels.LABELS[BROWSER], detail)

    def test_the_panel_does_not_offer_a_login_window_for_it(self):
        self.settings(ATTACH)
        data = self.client.get('/api/intake/social/x-channel').json()
        self.assertEqual(ATTACH, data['current'])
        self.assertFalse(data['needs_login_window'])
        self.assertEqual('', data['login_command'])

    def test_the_panel_lists_it_with_a_label_and_a_hint(self):
        """前端不许写死一张「哪条通道要填什么」的表,所以这一档的说明必须由
        后端发出去 —— 否则加一条通道就得改一次 JS。"""
        data = self.client.get('/api/intake/social/x-channel').json()
        row = next(c for c in data['channels'] if c['id'] == ATTACH)
        self.assertEqual(x_channels.LABELS[ATTACH], row['label'])
        self.assertEqual(x_channels.HINTS[ATTACH], row['hint'])

    def test_the_cheap_verdict_travels_with_the_listing(self):
        self.settings(ATTACH)
        with patch.object(cdp_attach, 'discover', return_value=None):
            data = self.client.get('/api/intake/social/x-channel').json()
        self.assertFalse(data['ready'])
        self.assertEqual(cdp_attach.ENABLE_HINT, data['hint'])


if __name__ == '__main__':
    unittest.main()
