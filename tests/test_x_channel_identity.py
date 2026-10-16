# -*- coding: utf-8 -*-
"""动作端点那道「跑的是不是你眼前这一份」的闸(`intake_api._x_act`)。

**为什么另拆一个文件**:`tests/test_x_channels.py` 已经 613 行,贴着 700 上限,
理由同 `test_x_attach_channel` 那次按形状拆分。摆盘(`ChannelBase`)仍共用那一份。

这里钉的是**服务端**那一半:请求要带上调用方眼前那一份配置,对不上就一个字都不
跑。前端那一半(先保存再执行、旧结论不许贴到新那一份旁边)在
`tests/x_channel_probe.mjs` 4.2~4.5b 几节。两份各有自己的回归,因为**任一侧单独
成立都不够**:页面那边的「已经保存了」只是它自己的缓存,证不了库里存着的还是它。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from kb.feeds import registry as reg, x_browser, x_channels, x_login_import
from tests.test_x_channels import ChannelBase

ACTIONS = ('/api/intake/social/x-channel/login',
           '/api/intake/social/x-channel/import-login',
           '/api/intake/social/x-channel/check')


class ConfigIdentityTests(ChannelBase):
    def setUp(self):
        super().setUp()
        # **三个动作全部摆成"跑起来就炸"**:这一组问的是"闸放没放行",所以放行
        # 就该看见那声炸,拦住就该一个字都没跑 —— 用返回成功的桩是分不开这两件事的。
        for p in (patch.object(x_browser, 'open_login_window',
                               side_effect=AssertionError('不该开窗口')),
                  patch.object(x_browser, 'probe',
                               side_effect=AssertionError('不该起浏览器')),
                  patch.object(x_login_import, 'import_login_state',
                               side_effect=AssertionError('不该搬 cookie'))):
            p.start(); self.addCleanup(p.stop)

    def post(self, path, **body):
        return self.client.post(path, json=body)

    def test_a_request_that_names_another_config_runs_nothing(self):
        """眼前那一份跟存着的对不上 —— 三个入口对称地一个字都不许跑。

        事故形态:第二个标签页把可执行文件位置改成一个不存在的文件并保存,第一页
        不刷新直接点「检查连接」。第一页手里的「已经保存了」是**它自己的缓存**,
        于是它既不知道、也不会重新 PUT,检的是第二页那条路径,而结论贴在第一页
        显示的那条可用路径旁边。
        """
        self.settings(x_channels.BROWSER, x_chrome_path='/opt/real/chrome')
        for path in ACTIONS:
            with self.subTest(path=path):
                r = self.post(path, x_channel='browser',
                              x_chrome_path='/opt/another-page/chrome')
                self.assertEqual(409, r.status_code)
                # 文案要说清**存着的是哪一档**和**下一步**(页面会重读一次)。
                self.assertIn(x_channels.LABELS[x_channels.BROWSER], r.json()['detail'])
                self.assertIn('重读', r.json()['detail'])

    def test_naming_only_the_channel_is_not_naming_the_config(self):
        """只对上取法名不算对上这一份 —— 那正是上一轮漏掉的那一半:同一档取法
        下换一条可执行文件位置,旧判据当场为真。"""
        self.settings(x_channels.FEEDGRAB, x_feedgrab_bin='/opt/real/feedgrab')
        r = self.post('/api/intake/social/x-channel/check', x_channel='feedgrab')
        self.assertEqual(409, r.status_code)

    def test_a_request_without_any_identity_runs_nothing(self):
        """没带身份和带了但对不上是同一件事的两种形态:两种都证不了「这一次说的
        是现在存着的这一份」,而动作会真起浏览器、真写那个 profile。"""
        self.settings(x_channels.BROWSER)
        for path in ACTIONS:
            with self.subTest(path=path):
                bare = self.client.post(path)                      # 压根没有 body
                self.assertEqual(409, bare.status_code, path)
                self.assertIn('带上你眼前那份设置', bare.json()['detail'])
                blank = self.post(path, x_channel='')              # 带了一份空的
                self.assertEqual(409, blank.status_code, path)

    # ── 反过来钉一边:对得上的那些照常放行 ────────────────────────────────
    def test_the_matching_config_is_let_through_and_comes_back_with_it(self):
        """放行那一半:这一次跑的是哪一份要**跟着结论回去** —— 回包落地时面板上
        那一份可能已经不是它了,前端靠它判「这条结论还贴得上吗」。"""
        self.settings(x_channels.BROWSER, x_chrome_path='/opt/real/chrome')
        with patch.object(x_browser, 'probe', return_value=(True, '登录态有效')):
            r = self.post('/api/intake/social/x-channel/check',
                          x_channel='browser', x_chrome_path='/opt/real/chrome')
        self.assertEqual(200, r.status_code)
        self.assertEqual({'x_channel': 'browser', 'x_feedgrab_bin': '',
                          'x_chrome_path': '/opt/real/chrome'}, r.json()['config'])

    def test_a_retired_alias_in_the_store_is_the_same_config_as_its_new_name(self):
        """存量里写着退役名(`bridge`)的和面板上的 `browser` 是**同一份**:两边都
        过 `resolve`:`reg.get_settings` 读出来那一下已经翻过一次,`config_of` 自己
        再翻一次(**两道都便宜,留着**:身份这个判据不该押在上游某一处记得翻)。
        不这么判的话,这些存量设置上三颗按钮会永远被拒,而用户什么都没做错。"""
        self.settings('bridge')
        self.assertEqual('browser', reg.get_settings('social')['x_channel'])
        with patch.object(x_browser, 'probe', return_value=(True, 'ok')):
            r = self.post('/api/intake/social/x-channel/check', x_channel='browser')
        self.assertEqual(200, r.status_code)

    def test_whitespace_around_a_path_is_not_a_different_config(self):
        """两边都 trim(面板发出去之前也 trim 过)。误拦的代价是用户对着一份看起来
        一模一样的设置被拒,看不出差在哪一步。"""
        self.settings(x_channels.FEEDGRAB, x_feedgrab_bin='/opt/real/feedgrab')
        with patch.object(x_channels, 'check', return_value=(True, 'ok')):
            r = self.post('/api/intake/social/x-channel/check', x_channel='feedgrab',
                          x_feedgrab_bin='  /opt/real/feedgrab  ')
        self.assertEqual(200, r.status_code)

    def test_settings_outside_the_identity_do_not_block_an_action(self):
        """身份只收**动作真正会用到**的那几项。把 `enabled`、同步间隔也算进去的
        话,用户在别处改个无关开关就会让这边的按钮开始拒绝 —— 而那些字段改了不
        影响这一次动作跑成什么样。"""
        self.settings(x_channels.FEEDGRAB, enabled=True, interval_minutes=60,
                      x_feedgrab_bin='/opt/real/feedgrab')
        with patch.object(x_channels, 'check', return_value=(True, 'ok')):
            r = self.post('/api/intake/social/x-channel/check', x_channel='feedgrab',
                          x_feedgrab_bin='/opt/real/feedgrab')
        self.assertEqual(200, r.status_code)

    def test_the_channel_refusals_still_speak_for_themselves(self):
        """闸排在「这一档不在这里登录」那些拒绝**之前**,但不许把它们盖掉:身份
        对得上、只是取法不对时,给的仍是那句说清哪一档和下一步的话(400)。"""
        self.settings(x_channels.FEEDGRAB, x_feedgrab_bin='/opt/real/feedgrab')
        r = self.post('/api/intake/social/x-channel/login', x_channel='feedgrab',
                      x_feedgrab_bin='/opt/real/feedgrab')
        self.assertEqual(400, r.status_code)
        self.assertIn('保存设置', r.json()['detail'])

    def test_the_panel_read_hands_back_the_whole_config(self):
        """GET 要给**完整那一份**。少了它前端只能拿上一次权威加载落下的路径去拼
        一份出来 —— 同一档取法在另一个标签页里换了路径时,拼出来的身份就是错的。
        """
        self.settings(x_channels.FEEDGRAB, x_feedgrab_bin='/opt/real/feedgrab')
        got = self.client.get('/api/intake/social/x-channel').json()
        self.assertEqual({'x_channel': 'feedgrab',
                          'x_feedgrab_bin': '/opt/real/feedgrab',
                          'x_chrome_path': ''}, got['config'])

    def test_the_panel_sends_its_own_config_with_every_action(self):
        """前端那一侧的结构性回归:三颗按钮都得把身份带上。漏一颗的后果不是报错,
        是那一颗从此永远被 409 拒绝 —— 而单测这边发什么都对不出来。"""
        from pathlib import Path

        from kb.web import intake_api
        src = (Path(intake_api.__file__).parents[2] / 'kb' / 'static' / 'js'
               / 'x_channel.js').read_text(encoding='utf-8')
        for action in ('login', 'import-login', 'check'):
            calls = [ln for ln in src.splitlines() if f'x-channel/{action}`' in ln]
            self.assertTrue(calls, action)
            # 请求体就在这一行、或者紧跟着的续行里(有两处换行写成了两行)。
            where = src.index(calls[0])
            self.assertIn('xChanExpect()', src[where:where + 240], action)


if __name__ == '__main__':
    unittest.main()
