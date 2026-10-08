# -*- coding: utf-8 -*-
"""登录态通道的**存盘契约**:水位什么时候推进、区间补抓用谁的边界。

和 `test_x_channels.py` 按形状分开(那边是通道的选择、整批预取和它跟 HTTP 那
一侧的分派):这里每一条都要真跑一轮 `sync.collect` 把结果落进 feeds.db,看的是
"写盘失败那一轮水位动没动"这类末态 —— 挤进那个文件会越过仓库那条 700 行上限。

被打桩的只有"起一个浏览器抓一批"那一下(`x_browser.harvest`),存盘、事务、
水位判定一行不打桩。摆盘共用 `test_x_channels.ChannelBase`。

跑:  python -m unittest tests.test_x_watermark -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_channels
from tests.test_x_channels import FRESH_LOCAL, ChannelBase


class WatermarkCommitTests(ChannelBase):
    """**水位和条目一个事务**:存盘失败那一轮水位不许动。"""

    def entry(self, url='https://x.com/Example/status/1'):
        return {'url': url, 'title': 'a thought', 'body': 'a thought',
                'author': 'Example Person', 'published': FRESH_LOCAL, 'content_kind': 'post'}

    def setUp(self):
        super().setUp()
        # 浏览器通道的"就绪"判据是 profile 里有过 Cookies;不摆上的话整轮会被
        # 记成"待配置"、一条订阅都不取,而这几条用例要看的正是存盘那一步。
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True, exist_ok=True)
        cookies.write_bytes(b'x')
        self.settings(x_channels.BROWSER)

    def collect(self, scope=None, **by_handle):
        """真跑一轮 `sync.collect`,只把"起浏览器抓一批"那一下替掉。

        **不能用 `harvested()` 摆盘**:`collect` 自己会先调 `_prefetch_x`,那一步
        `reset()` + 真预取,摆好的缓存当场被清掉(而且会去起一个真 Chrome)。
        """
        with patch.object(x_browser, 'harvest', lambda jobs, **kw: dict(by_handle)):
            return sync.collect('social', scope or FetchScope(force=True))

    def test_a_failed_save_does_not_advance_the_watermark(self):
        """原来 `take()` 在工作线程里当场存一次水位,而条目要等主线程的
        `_save_result` 才写进库 —— 两个独立事务没有共同的成功/失败边界。

        审查实测:注入一次写盘失败之后 `stored_items=0`、而水位已经推进到那条
        没入库的推文,重试取回 0 条 —— 那不是提示不准,是丢数据。
        """
        s = self.sub()
        with patch.object(reg, 'put_items', side_effect=OSError('写盘失败')):
            summary = self.collect(example={'entries': [self.entry()], 'error': '',
                                            'watermark': '2026-10-07T00:00:00+00:00'})
        self.assertEqual(1, len(summary['errors']))
        self.assertEqual('', x_channels.watermark(reg.subscription('social', s['id']),
                                                  x_channels.BROWSER))
        self.assertEqual(0, reg.items('social', s['id'])['total'])

    def test_a_successful_save_advances_it_in_the_same_transaction(self):
        s = self.sub()
        summary = self.collect(example={'entries': [self.entry()], 'error': '',
                                        'watermark': '2026-10-07T00:00:00+00:00'})
        self.assertEqual(1, summary['added'])
        self.assertEqual('2026-10-07T00:00:00+00:00',
                         x_channels.watermark(reg.subscription('social', s['id']),
                                              x_channels.BROWSER))

    def test_saving_the_watermark_keeps_the_other_channels_value(self):
        s = self.sub()
        x_channels.save_watermark(s['id'], x_channels.FEEDGRAB, '2026-10-05')
        self.collect(example={'entries': [self.entry()], 'error': '',
                              'watermark': '2026-10-07T00:00:00+00:00'})
        s = reg.subscription('social', s['id'])
        self.assertEqual('2026-10-05', x_channels.watermark(s, x_channels.FEEDGRAB))
        self.assertEqual('2026-10-07T00:00:00+00:00',
                         x_channels.watermark(s, x_channels.BROWSER))

    def test_an_incomplete_round_is_recorded_without_calling_it_a_failure(self):
        s = self.sub()
        summary = self.collect(example={
            'entries': [self.entry()], 'error': '', 'watermark': '',
            'incomplete': '滚动次数用完,没有读到上次的位置'})
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, len(summary['partial']))
        row = reg.subscription('social', s['id'])
        self.assertIn('滚动次数', row['last_note'])
        self.assertEqual('', row['last_error'])
        self.assertTrue(row['last_success'])
        self.assertEqual(0, row['failures'])          # 不进退避重试

    def test_a_later_complete_round_clears_the_note(self):
        s = self.sub()
        self.collect(example={'entries': [self.entry()], 'error': '', 'watermark': '',
                              'incomplete': '滚动次数用完'})
        self.collect(example={'entries': [self.entry('https://x.com/Example/status/2')],
                              'error': '', 'watermark': ''})
        self.assertEqual('', reg.subscription('social', s['id'])['last_note'])


class RangedScopeTests(ChannelBase):
    """区间补抓:用用户点的那段日期当下界,而且不许动日常增量水位。"""

    def test_the_prefetch_gets_the_requested_date_not_the_daily_watermark(self):
        """审查实测:传给采集器的仍是日常水位 2026-10-07,于是用户点名要的
        10-05 一条也翻不到(`added=0`),而日常水位还被进一步推进了。"""
        s = self.sub()
        x_channels.save_watermark(s['id'], x_channels.BROWSER, '2026-10-07T00:00:00+00:00')
        seen, scope = {}, FetchScope(since='2026-10-05', until='2026-10-06')

        def fake(jobs, **kw):
            seen.update(dict(jobs))
            return {h: {'entries': [], 'error': '',
                        'watermark': '2026-10-08T00:00:00+00:00'} for h, _ in jobs}

        with patch.object(x_browser, 'harvest', fake):
            sync._prefetch_x('social', reg.subscriptions('social'),
                             self.settings(x_channels.BROWSER), scope)
        self.assertEqual({'example': '2026-10-05'}, seen)

    def test_a_ranged_round_never_moves_the_daily_watermark(self):
        s = self.sub()
        x_channels.save_watermark(s['id'], x_channels.BROWSER, '2026-10-07T00:00:00+00:00')
        scope = FetchScope(since='2026-10-05', until='2026-10-06')

        def fake(jobs, **kw):
            return {h: {'entries': [], 'error': '',
                        'watermark': '2026-10-08T00:00:00+00:00'} for h, _ in jobs}

        with patch.object(x_browser, 'harvest', fake):
            sync._prefetch_x('social', reg.subscriptions('social'),
                             self.settings(x_channels.BROWSER), scope)
            _, meta = x_channels.take(reg.subscription('social', s['id']),
                                      self.settings(x_channels.BROWSER))
        self.assertNotIn('config_patch', meta)
        self.assertEqual('2026-10-07T00:00:00+00:00',
                         x_channels.watermark(reg.subscription('social', s['id']),
                                              x_channels.BROWSER))

    def test_a_ranged_round_does_not_clear_the_incomplete_note(self):
        """它不改日常增量的任何状态,这条提示也算 —— 清掉等于替日常同步宣布
        "读完了",而那一边的进度一点没动。"""
        s = self.sub()
        self.settings(x_channels.BROWSER)
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True, exist_ok=True)
        cookies.write_bytes(b'x')
        with patch.object(x_browser, 'harvest', lambda jobs, **kw: {
                'example': {'entries': [], 'error': '', 'watermark': '',
                            'incomplete': '滚动次数用完'}}):
            sync.collect('social', FetchScope(force=True))
        self.assertIn('滚动次数', reg.subscription('social', s['id'])['last_note'])
        with patch.object(x_browser, 'harvest', lambda jobs, **kw: {
                'example': {'entries': [], 'error': ''}}):
            sync.collect('social', FetchScope(since='2026-10-05', until='2026-10-06'))
        self.assertIn('滚动次数', reg.subscription('social', s['id'])['last_note'])

    def test_a_daily_round_still_passes_each_accounts_own_watermark(self):
        a = self.sub('Alpha')
        x_channels.save_watermark(a['id'], x_channels.BROWSER, '2026-10-01T00:00:00+00:00')
        self.sub('Beta')
        seen = {}

        def fake(jobs, **kw):
            seen.update(dict(jobs))
            return {h: {'entries': [], 'error': ''} for h, _ in jobs}

        with patch.object(x_browser, 'harvest', fake):
            sync._prefetch_x('social', reg.subscriptions('social'),
                             self.settings(x_channels.BROWSER), FetchScope())
        self.assertEqual({'alpha': '2026-10-01T00:00:00+00:00', 'beta': ''}, seen)


if __name__ == '__main__':
    unittest.main()
