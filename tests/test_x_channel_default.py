# -*- coding: utf-8 -*-
"""设置里没存过 `x_channel` 的存量库落哪一档(`x_channels.unsaved_default`、`registry._settle_x_channel`)。

默认换成不登录之后,默认还是独立 profile 那时**登录成功过、却从没存过通道**的库照旧走独立 profile
(面板上点「登录 X」在下拉没改过时不写设置,这类库是常态)。判据落在后果上:
- 独立 profile 的 cookie 库里**真有 X 的会话 cookie** 才算登录过;只起过一次 Chrome(文件在、一条
  cookie 都没有)、只开过登录窗口没登成的,落到不登录;
- 判出来就**存进设置**:之后 profile 里的东西来了又走,通道都不变;
- cookie 库读不了、上级目录进不去、写入方正写到一半(读不到一致快照)都不固化,这一次照旧按独立
  profile 读,下一次再判;
- 存过的值照存着的读,不看 profile 脸色。

cookie 库照 Chromium 的真实表结构摆(`cookies` 表、`host_key`/`name` 两列,值是加密的、这里不碰),
不是 touch 一个空文件 —— 那正是要分开的两种情形。

跑:  python -m unittest tests.test_x_channel_default -v
"""
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import registry as reg, x_channels
from tests.test_x_channels import ChannelBase


def cookie_store(profile: Path, rows=(), layout=('Default', 'Network', 'Cookies')) -> Path:
    """一份 Chromium 形状的 cookie 库。`rows` 是 (host_key, name)。"""
    path = profile.joinpath(*layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE cookies (creation_utc INTEGER, host_key TEXT, name TEXT, '
                 'encrypted_value BLOB, path TEXT, expires_utc INTEGER)')
    conn.executemany("INSERT INTO cookies VALUES (0, ?, ?, x'00', '/', 0)", rows)
    conn.commit()
    conn.close()
    return path


class UnsavedChannelTests(ChannelBase):
    def setUp(self):
        super().setUp()
        with reg.db() as c:                      # 存量库:social 那一行压根没有(或者没有这个键)
            c.execute("DELETE FROM feed_settings WHERE kind='social'")
        self.profile = x_channels.BROWSER_PROFILE

    def legacy_row(self):
        """更早版本存下来的那种:有 social 这一行,没有 `x_channel` 这个键。"""
        with reg.db() as c:
            c.execute("INSERT OR REPLACE INTO feed_settings VALUES('social', ?)",
                      (json.dumps({'enabled': False, 'interval_minutes': 1440}),))

    def stored(self):
        with reg.db() as c:
            row = c.execute("SELECT value FROM feed_settings WHERE kind='social'").fetchone()
        return json.loads(row['value']).get('x_channel') if row else None

    def test_a_profile_that_really_logged_in_keeps_the_profile(self):
        for layout in (('Default', 'Network', 'Cookies'), ('Default', 'Cookies')):
            for legacy in (False, True):
                with self.subTest(layout='/'.join(layout), legacy_row=legacy):
                    with reg.db() as c:
                        c.execute("DELETE FROM feed_settings WHERE kind='social'")
                    if legacy:
                        self.legacy_row()
                    path = cookie_store(self.profile, [('.x.com', 'auth_token'), ('.x.com', 'ct0')], layout)
                    self.assertEqual(x_channels.BROWSER, reg.get_settings('social')['x_channel'])
                    self.assertEqual(x_channels.BROWSER, self.stored())
                    path.unlink()

    def test_a_profile_chrome_merely_opened_falls_to_no_login(self):
        """只起过 Chrome(空白页、没登成的登录窗口):文件在,X 的会话 cookie 没有。别的站的 cookie、
        X 上不是会话的那几条也都不算。"""
        # 实测:无头 Chrome 只开一次 about:blank,建出来的正是 `Default/Cookies`(两种布局都摆上)
        for layout in (('Default', 'Cookies'), ('Default', 'Network', 'Cookies')):
            cookie_store(self.profile, [('.example.org', 'auth_token'), ('.x.com', 'guest_id'),
                                        ('.notx.com', 'auth_token')], layout)
        self.legacy_row()
        self.assertEqual(x_channels.GUEST, reg.get_settings('social')['x_channel'])
        self.assertEqual(x_channels.GUEST, self.stored())
        self.assertEqual(1440, reg.get_settings('social')['interval_minutes'])   # 那一行别的键原样

    def test_no_profile_at_all_falls_to_no_login(self):
        self.assertEqual(x_channels.GUEST, reg.get_settings('social')['x_channel'])
        self.assertEqual(x_channels.GUEST, self.stored())

    def test_once_settled_the_profile_coming_and_going_changes_nothing(self):
        path = cookie_store(self.profile, [('.x.com', 'auth_token')])
        self.assertEqual(x_channels.BROWSER, reg.get_settings('social')['x_channel'])
        path.unlink()
        self.assertEqual(x_channels.BROWSER, reg.get_settings('social')['x_channel'])
        with reg.db() as c:
            c.execute("DELETE FROM feed_settings WHERE kind='social'")
        self.assertEqual(x_channels.GUEST, reg.get_settings('social')['x_channel'])
        cookie_store(self.profile, [('.x.com', 'auth_token')])
        self.assertEqual(x_channels.GUEST, reg.get_settings('social')['x_channel'])

    def test_an_unreadable_cookie_store_is_not_settled(self):
        """拿不准就别固化:这一次按升级前的口径(独立 profile)读,不存;读得了之后再判、再存。"""
        path = self.profile / 'Default' / 'Cookies'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'not a database')
        self.assertEqual(x_channels.BROWSER, reg.get_settings('social')['x_channel'])
        self.assertIsNone(self.stored())
        path.unlink()
        self.assertEqual(x_channels.GUEST, reg.get_settings('social')['x_channel'])
        self.assertEqual(x_channels.GUEST, self.stored())

    @unittest.skipIf(os.geteuid() == 0, 'root 不受目录权限约束')
    def test_a_store_behind_a_locked_directory_is_not_settled(self):
        """上级目录进不去:库在不在答不出来,不是「没有」。权限回来之后照真实内容判、再存。"""
        cookie_store(self.profile, [('.x.com', 'auth_token')])
        locked = self.profile / 'Default'
        locked.chmod(0)
        try:
            self.assertEqual(x_channels.BROWSER, reg.get_settings('social')['x_channel'])
            self.assertIsNone(self.stored())
        finally:
            locked.chmod(0o755)
        self.assertEqual(x_channels.BROWSER, reg.get_settings('social')['x_channel'])
        self.assertEqual(x_channels.BROWSER, self.stored())

    def test_a_store_mid_write_is_not_settled(self):
        """写入方(Chrome)删了会话 cookie 还没提交:那一刻读不到一致快照,不许当成「没有」存下来。
        删除要真溢出到库文件里(缓存压到一页、再改一批行),不然绕开锁直读文件也碰巧读到旧行。"""
        path = cookie_store(self.profile, [('.x.com', 'auth_token')]
                            + [('.example.org', f'pad{i}') for i in range(2000)])
        writer = sqlite3.connect(path, isolation_level=None)
        writer.execute('PRAGMA cache_size=1')
        writer.execute('BEGIN EXCLUSIVE')
        writer.execute("DELETE FROM cookies WHERE name='auth_token'")
        writer.execute("UPDATE cookies SET path='/spill' WHERE host_key='.example.org'")
        try:
            self.assertEqual(x_channels.BROWSER, reg.get_settings('social')['x_channel'])
            self.assertIsNone(self.stored())
        finally:
            writer.execute('ROLLBACK')
            writer.close()
        self.assertEqual(x_channels.BROWSER, reg.get_settings('social')['x_channel'])
        self.assertEqual(x_channels.BROWSER, self.stored())

    def test_a_saved_channel_is_read_as_saved_whatever_the_profile_holds(self):
        """反过来钉一边:存过 browser 的没登录也是 browser(那就是「待配置」,不许替他换成能跑的那档),
        存过 guest 的登录过也是 guest。"""
        self.settings(x_channels.BROWSER)
        self.assertEqual(x_channels.BROWSER, reg.get_settings('social')['x_channel'])
        cookie_store(self.profile, [('.x.com', 'auth_token')])
        self.settings(x_channels.GUEST)
        self.assertEqual(x_channels.GUEST, reg.get_settings('social')['x_channel'])


if __name__ == '__main__':
    unittest.main()
