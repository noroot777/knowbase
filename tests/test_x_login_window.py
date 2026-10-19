# -*- coding: utf-8 -*-
"""登录窗口的生死:谁的窗口、什么时候关、关之后面板怎么说。

**判据一律落在后果上**,不是"标志打上没有":进程是不是真的没了(`os.kill(pid, 0)`)、
记录文件还在不在、面板那一行写的是哪句话。只验"调没调 release"的话,一个把窗口
记下来却从不动它的实现也能全绿。

**这里的"登录窗口"是真进程**,不是打桩的 pid:`subprocess.Popen` 起一个睡着的
`sleep`,再照 Chrome 的样子把 `SingletonLock` 链接成 `<主机名>-<pid>`。用一个假 pid
会让"它还活着吗"那道判据整条失效 —— 而那道正是"只关我们自己那一个"的安全边界。

反过来钉的那几边在 `ForeignWindowTests`:别人的窗口、坏记录、陈旧记录,一个都不许
碰。杀错进程比堵着一条采集通道严重得多。

跑:  python -m unittest tests.test_x_login_window -v
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from kb.feeds import x_cdp


def _sleeper() -> subprocess.Popen:
    """一个什么都不做、能被信号打断的真进程。"""
    return subprocess.Popen(['sleep', '300'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class LoginWindowBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = self._tmp.name
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for proc in self.procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:                            # noqa: BLE001 - 收尾
                pass
        self._tmp.cleanup()

    def _hold(self, pid: int | None = None) -> int:
        """照 Chrome 的样子占住这个 profile,返回占着它的 pid。"""
        if pid is None:
            proc = _sleeper()
            self.procs.append(proc)
            pid = proc.pid
        link = Path(self.profile) / 'SingletonLock'
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(f'testhost-{pid}')
        return pid

    def _note(self, pid: int, age: float = 0.0) -> None:
        (Path(self.profile) / x_cdp._LOGIN_NOTE).write_text(
            json.dumps({'pid': pid, 'at': time.time() - age}), encoding='utf-8')

    def _note_exists(self) -> bool:
        return (Path(self.profile) / x_cdp._LOGIN_NOTE).exists()


class ReleaseTests(LoginWindowBase):
    """到点的关掉,没到点的不动 —— 两边都钉。"""

    def test_window_past_its_ttl_is_really_killed(self):
        pid = self._hold()
        self._note(pid, age=20 * 60)
        note = x_cdp.release_login_window(self.profile, ttl=10 * 60)
        # 后果:那个进程真的没了,而不是"标记成该关"
        self.assertFalse(_alive(pid), '到点的登录窗口还活着')
        self.assertIn('自动关掉', note)
        self.assertFalse(self._note_exists(), '关掉之后记录该一起清掉')

    def test_window_within_its_ttl_is_left_alone(self):
        """反过来钉一边:用户可能正在输密码、过两步验证,那几分钟缩不短。"""
        pid = self._hold()
        self._note(pid, age=60)
        self.assertEqual(x_cdp.release_login_window(self.profile, ttl=10 * 60), '')
        self.assertTrue(_alive(pid), '没到点的窗口被关掉了')
        self.assertTrue(self._note_exists())

    def test_countdown_is_read_only(self):
        """面板每次刷新都调它 —— 它一个进程都不许碰、一个字节都不许写。"""
        pid = self._hold()
        self._note(pid, age=60)
        left = x_cdp.login_window_left(self.profile, ttl=10 * 60)
        self.assertTrue(530 < left <= 540, f'剩余时间不对:{left}')
        self.assertTrue(_alive(pid), 'login_window_left 把窗口关了')
        self.assertTrue(self._note_exists(), 'login_window_left 动了记录文件')

    def test_countdown_is_zero_once_past_ttl(self):
        """到点之后不再报"还剩多久" —— 报一个假的倒计时等于劝用户再等一会儿。"""
        pid = self._hold()
        self._note(pid, age=20 * 60)
        self.assertEqual(x_cdp.login_window_left(self.profile, ttl=10 * 60), 0.0)


class ForeignWindowTests(LoginWindowBase):
    """不是我们起的那些,一个都不许碰。杀错进程比堵着通道严重得多。"""

    def test_a_window_we_did_not_open_is_never_touched(self):
        """SingletonLock 指着别人:用户自己拿同一个 --user-data-dir 开的窗口。"""
        mine = _sleeper(); self.procs.append(mine)
        theirs = self._hold()                 # 占着目录的是另一个进程
        self._note(mine.pid, age=20 * 60)     # 我们记的是自己那个
        self.assertEqual(x_cdp.release_login_window(self.profile, ttl=1), '')
        self.assertTrue(_alive(theirs), '别人的窗口被我们关掉了')
        self.assertTrue(_alive(mine.pid), '连记录里那个无关进程也被杀了')
        self.assertEqual(x_cdp.login_window_pid(self.profile), 0)

    def test_a_broken_note_is_the_same_as_no_note(self):
        """坏记录等于没有记录 —— 两个方向(当成占着/当成该关)都会动错进程。"""
        held = self._hold()
        for payload in ('{', '{}', '{"pid": "x", "at": 1}', '{"pid": -1, "at": 1}', ''):
            (Path(self.profile) / x_cdp._LOGIN_NOTE).write_text(payload, encoding='utf-8')
            self.assertEqual(x_cdp.login_window_pid(self.profile), 0, payload)
            self.assertEqual(x_cdp.release_login_window(self.profile, ttl=0), '', payload)
            self.assertTrue(_alive(held), f'坏记录 {payload!r} 之下窗口被动了')

    def test_a_stale_note_is_dropped_so_the_pid_cannot_be_reused(self):
        """窗口早没了、记录还在:不清掉的话,系统把同一个 pid 分给别的进程时,
        「pid 对得上」那道会把一个无辜的进程认成我们的窗口。"""
        gone = _sleeper()
        gone.kill(); gone.wait(timeout=5)
        self._note(gone.pid, age=20 * 60)      # 没有 SingletonLock = 没占着
        self.assertEqual(x_cdp.release_login_window(self.profile, ttl=1), '')
        self.assertFalse(self._note_exists(), '陈旧记录没被清掉')

    def test_no_note_at_all_is_quiet(self):
        """profile 被占着、但不是我们开的(服务重启前的遗留也走这档)。"""
        held = self._hold()
        self.assertEqual(x_cdp.release_login_window(self.profile, ttl=0), '')
        self.assertTrue(_alive(held))

    def test_a_zombie_window_does_not_block_the_channel_forever(self):
        """窗口被强行结束、`SingletonLock` 来不及删:它在我们这儿是一格**僵尸**,
        而 `os.kill(pid, 0)` 对僵尸照样成功 —— 不收尸就是「`profile_busy` 永远为真、
        却没有窗口可关」,这条通道从此没有任何出路。

        判据是**结论**(这一轮不再被当成"窗口还开着")加**进程表**(那一格真的摘掉了),
        不是"调没调 waitpid"。"""
        proc = _sleeper()
        self.procs.append(proc)
        self._hold(proc.pid)                 # 锁留在原处,指着它
        self._note(proc.pid, age=60)         # 还没到点
        proc.kill()                          # 用户强退 / 崩溃:没人收尸
        time.sleep(0.2)
        self.assertTrue(_alive(proc.pid), '前提不成立:它该是一格僵尸')

        self.assertEqual(x_cdp.release_login_window(self.profile, ttl=10 * 60), '')
        self.assertFalse(_alive(proc.pid), '僵尸没被摘掉,这条通道会一直被判成占用')
        self.assertFalse(self._note_exists(), '窗口已经没了,记录该清掉')
        self.assertEqual(x_cdp.login_window_pid(self.profile), 0)


class PanelCopyTests(LoginWindowBase):
    """面板那一行:窗口开着时说的是"去关它",不是"还没有登录过 X"。"""

    def setUp(self):
        super().setUp()
        from kb.feeds import x_channels
        self.x_channels = x_channels
        self._orig = x_channels.BROWSER_PROFILE
        x_channels.BROWSER_PROFILE = Path(self.profile)
        self.addCleanup(lambda: setattr(x_channels, 'BROWSER_PROFILE', self._orig))

    def test_open_window_outranks_never_logged_in(self):
        """两档的下一步正好相反:还没登录要去开窗口,窗口开着要去关它。
        顺序反了,刚点开窗口的人收到的是一句正确但没用的话。"""
        pid = self._hold()
        self._note(pid, age=60)               # Cookies 不存在 = 从没登录过
        ready, why, nxt = self.x_channels.readiness({'x_channel': 'browser'})
        self.assertFalse(ready)
        self.assertIn('登录窗口', why)
        self.assertNotIn('还没有登录过', why)
        self.assertIn('自动关闭', nxt)

    def test_a_closed_window_restores_the_normal_verdict(self):
        """反过来钉一边:窗口关掉之后,面板立刻回到原来那套判断。"""
        (Path(self.profile) / 'Default').mkdir(parents=True)
        (Path(self.profile) / 'Default' / 'Cookies').write_text('x', encoding='utf-8')
        ready, why, _ = self.x_channels.readiness({'x_channel': 'browser'})
        self.assertTrue(ready, f'窗口没开着却判成不就绪:{why}')


if __name__ == '__main__':
    unittest.main()
