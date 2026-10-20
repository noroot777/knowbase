# -*- coding: utf-8 -*-
"""独立 profile 那个浏览器的收尾(`x_cdp.Chrome.__exit__` + `profile_busy`)。

**单独一个文件**:`tests/test_cdp_attach.py` 已经 613 行、贴着 700 上限,而这一组
验的是另一条传输(我们自己 fork 出来的那个浏览器)的生命周期 —— 形状本来就不同。

要验的是一条链:关浏览器失败 → 残留进程 → 它攥着 profile 的锁 → 用户唯一能自救
的两个入口(「检查连接」「打开登录窗口」)一起被堵死。所以每一条都先断言**后果**
(进程真的没了 / 那两个入口真的放行),再断言契约。
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kb.feeds import x_browser, x_cdp
from kb.feeds.x_cdp import CdpError


def _stuff(fd: int) -> None:
    """把管道灌满:再写一个字节都要等人来读。灌的那一端必须是非阻塞的。"""
    while True:
        try:
            os.write(fd, b'x' * 4096)
        except BlockingIOError:
            return


def _closed(fds) -> bool:
    for fd in fds:
        try:
            os.fstat(fd)
            return False
        except OSError:
            pass
    return True


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _reap(proc: subprocess.Popen, fds=()) -> None:
    """用例自己的兜底清理:被审那一版会把进程和 fd 留下来,不能靠被测代码收。"""
    if proc.poll() is None:
        for sig in (signal.SIGCONT, signal.SIGKILL):
            try:
                os.kill(proc.pid, sig)
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:            # pragma: no cover
            pass
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _wired(profile: str, argv=None):
    """一个摆好了的 Chrome:真进程 + 真管道,**不起浏览器**。

    `__enter__` 里那条 `Browser.getVersion` 要等 30 秒,而这一组验的是收尾 ——
    所以按 `__enter__` 的样子把三个字段接上(口径同 `PipeWriteTests`:写端非阻塞
    那一句也要有,不然灌满这一步自己就挂住)。
    """
    page = x_cdp.Chrome(profile)
    proc = subprocess.Popen(argv or ['/bin/sh', '-c', 'exec sleep 30'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    r, w = os.pipe()
    os.set_blocking(w, False)
    page.proc, page._r, page._w = proc, r, w
    return page, proc, (r, w)


def _wait_for(path: Path, limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.02)


def _kill(pid: int) -> None:
    if not _dead(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:                                  # pragma: no cover
            pass


def _stub_browser(tmp: Path, pidfile: Path) -> str:
    """一个假「浏览器」:忽略所有参数,记下自己的 pid 然后赖着不走。"""
    path = tmp / 'fake-chrome'
    path.write_text(f'#!/bin/sh\necho $$ > {pidfile}\nexec sleep 30\n', 'utf-8')
    path.chmod(0o755)
    return str(path)


class ShutdownTests(unittest.TestCase):
    """收尾路径:`Browser.close` 怎么失败都不许把后面那几步跳过。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='kb-shutdown-'))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.tmp)], check=False))

    def test_a_browser_that_will_not_take_the_close_command_is_killed(self):
        """被审那一版红的就是这条:写不动的时候 `_post` 报的是 `CdpError`,而它
        原来不在 `__exit__` 的捕获里 —— 异常一冒出去,`kill`/`wait`、关那两个 fd、
        `proc` 复位一步都没做,留下一个用户看不见也关不掉的无头浏览器。"""
        page, proc, fds = _wired(str(self.tmp))
        self.addCleanup(_reap, proc, fds)
        _stuff(page._w)                                  # 浏览器不再读命令
        started = time.monotonic()
        with patch.object(x_cdp, 'WRITE_TIMEOUT', 0.3):
            page.__exit__(None, None, None)              # 不许抛
        self.assertLess(time.monotonic() - started, 5.0, '收尾没有上限')
        self.assertIsNotNone(proc.poll(), '卡住的浏览器没被结束掉')
        self.assertTrue(_closed(fds), '两个管道 fd 还开着')
        self.assertEqual((page._r, page._w, page.proc), (-1, -1, None), '状态没复位')

    def test_a_broken_pipe_on_the_close_command_is_handled_too(self):
        """另一种写失败:管道的另一头已经没了。它在 `_post` 里同样翻成
        `CdpError`(原来是裸 `OSError`,恰好在捕获里 —— 所以这一条是把那条路
        一起钉住,别让它跟着上面那条一块丢掉)。"""
        page, proc, fds = _wired(str(self.tmp))
        self.addCleanup(_reap, proc, fds)
        os.close(page._r)                                # 读端没了:写就是 EPIPE
        page.__exit__(None, None, None)
        self.assertIsNotNone(proc.poll(), '卡住的浏览器没被结束掉')
        self.assertEqual((page._r, page._w, page.proc), (-1, -1, None), '状态没复位')

    def test_a_browser_that_takes_the_close_but_never_exits_is_killed(self):
        """它收下了 `Browser.close` 却不退出 —— `wait` 抛 `TimeoutExpired`,
        这一档原来就有,别让它跟着改动丢掉。"""
        page, proc, fds = _wired(str(self.tmp))
        self.addCleanup(_reap, proc, fds)
        real_wait, calls = proc.wait, []

        def wait(timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired('chrome', timeout)
            return real_wait(timeout=timeout)

        with patch.object(proc, 'wait', side_effect=wait):
            page.__exit__(None, None, None)
        self.assertEqual(len(calls), 2, '没有在 kill 之后再等它')
        self.assertIsNotNone(proc.poll(), '不退出的浏览器没被 kill')
        self.assertTrue(_closed(fds))

    def test_a_browser_that_exits_on_its_own_is_not_killed(self):
        """反过来钉一边:好好退出的那一次不许被 kill —— `Browser.close` 存在的
        理由就是让它把 profile(登录态在里面)存盘,一律 kill 等于每次都放弃它。"""
        page, proc, fds = _wired(str(self.tmp), ['/bin/sh', '-c', 'exec sleep 0.3'])
        self.addCleanup(_reap, proc, fds)
        page.__exit__(None, None, None)
        self.assertEqual(proc.returncode, 0, f'被 kill 掉了:{proc.returncode}')
        self.assertTrue(_closed(fds))

    def test_a_launch_that_fails_cleans_up_and_keeps_its_own_reason(self):
        """`__enter__` 起不来时兜底走的也是这条路,所以同一个缺口在启动失败那一侧
        一模一样:收尾抛出来的异常会**顶掉启动失败那个原因**,而那个假浏览器连同
        两个 fd 一起留在系统里。"""
        pidfile = self.tmp / 'pid'
        stub = _stub_browser(self.tmp, pidfile)
        page = x_cdp.Chrome(str(self.tmp / 'profile'), chrome_path=stub)

        def no_answer(*_a, **_k):
            _wait_for(pidfile)             # 等它报上自己的 pid,再把失败注进来
            raise CdpError('它没有应答 CDP')

        with patch.object(x_cdp.Chrome, 'call', side_effect=no_answer), \
             patch.object(x_cdp.Chrome, '_post', side_effect=CdpError('写不动')):
            with self.assertRaises(CdpError) as caught:
                page.__enter__()
        pid = int(pidfile.read_text('utf-8').strip())
        self.addCleanup(_kill, pid)
        self.assertTrue(_dead(pid), '启动失败之后那个浏览器进程还活着')
        self.assertIn('浏览器启动失败', str(caught.exception), '启动失败的原因被收尾顶掉了')
        self.assertEqual((page._r, page._w, page.proc), (-1, -1, None), '状态没复位')


class ProfileBusyTests(unittest.TestCase):
    """「这个 profile 被占着吗」只有一份判据:`SingletonLock` 指着一个**活着的**
    pid。只看文件在不在的话,一次没能优雅关闭的收尾会把那把锁永久留在盘上。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='kb-busy-'))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.tmp)], check=False))

    def _lock(self, target: str) -> None:
        link = self.tmp / 'SingletonLock'
        if os.path.lexists(link):
            link.unlink()
        os.symlink(target, link)

    def test_no_lock_is_not_busy(self):
        self.assertFalse(x_cdp.profile_busy(str(self.tmp)))
        self.assertFalse(x_cdp.profile_busy(str(self.tmp / '不存在')))

    def test_a_live_pid_is_busy(self):
        self._lock(f'somehost-{os.getpid()}')
        self.assertTrue(x_cdp.profile_busy(str(self.tmp)))

    def test_a_dead_pid_is_not_busy(self):
        """残锁:Chrome 崩了或者被我们 kill 掉就留在原处,而 Chrome 自己下一次
        启动也是读这个 pid、发现死了就把锁抢过来。"""
        dead = subprocess.Popen(['/bin/sh', '-c', 'exit 0'])
        dead.wait(timeout=5)
        self._lock(f'somehost-{dead.pid}')
        self.assertTrue(os.path.lexists(self.tmp / 'SingletonLock'), '前提:锁还在')
        self.assertFalse(x_cdp.profile_busy(str(self.tmp)))

    def test_shapes_we_have_never_seen_are_not_evidence(self):
        """不是链接、或者链接内容看不懂,都不是「有人占着」的证据:没有证据的时候
        堵住用户唯一的两个自救入口,正是这条判据要改掉的毛病。"""
        (self.tmp / 'SingletonLock').write_text('x', 'utf-8')
        self.assertFalse(x_cdp.profile_busy(str(self.tmp)))
        (self.tmp / 'SingletonLock').unlink()
        self._lock('somehost-不是数字')
        self.assertFalse(x_cdp.profile_busy(str(self.tmp)))

    def test_the_connection_check_is_not_blocked_by_a_stale_lock(self):
        """后果一:「检查连接」放行到真去连浏览器那一步(这里用一个哨兵证明它
        走过了前置检查,不真起浏览器)。

        **前提里要摆上登录痕迹**:前置检查现在不止一道闸,「这个 profile 从没登录
        过」那一道在残锁之后(`has_login_footprint`)。不摆的话这一局会停在那一道上,
        于是"残锁放行了没有"这件事压根没被问到 —— 用例看着红,而它要钉的那条判据
        连执行路径都没进。
        """
        (self.tmp / 'Default').mkdir(parents=True, exist_ok=True)
        (self.tmp / 'Default' / 'Cookies').write_text('x', 'utf-8')
        self._lock('somehost-999999')
        with patch.object(x_browser, 'browser_session',
                          side_effect=AssertionError('前置检查放行了')):
            with self.assertRaises(AssertionError):
                x_browser.probe(str(self.tmp), '')

    def test_the_connection_check_still_stops_for_a_live_window(self):
        """反过来钉一边:真有一个活着的实例占着时,那道前置检查一个字都不许少
        —— 同一个 profile 目录同时只能开一个。"""
        self._lock(f'somehost-{os.getpid()}')
        with patch.object(x_browser, 'browser_session',
                          side_effect=AssertionError('不该去连')):
            ready, detail = x_browser.probe(str(self.tmp), '')
        self.assertFalse(ready)
        self.assertIn('正被另一个窗口占着', detail)

    def test_the_login_window_is_not_blocked_by_a_stale_lock(self):
        """后果二:「打开登录窗口」也放行 —— 原来这两处一起报「登录窗口还开着,
        关掉它再试」,而根本没有窗口可关,这条通道从此没有任何出路。"""
        self._lock('somehost-999999')
        binary = self.tmp / 'fake-chrome'
        binary.write_text('#!/bin/sh\nexit 0\n', 'utf-8')
        binary.chmod(0o755)
        # 这一档是**有头**窗口(用户要在里面输密码),所以只验那道闸放行,
        # 不真起浏览器 —— 回归里不开有头窗口。
        with patch.object(x_browser.subprocess, 'Popen') as popen:
            detail = x_browser.open_login_window(str(self.tmp / 'profile'), str(binary))
        self.assertTrue(popen.called, '那道闸把登录窗口拦住了')
        self.assertIn('已打开登录窗口', detail)


def _chrome() -> str:
    try:
        return x_cdp.find_chrome()
    except CdpError:
        return ''


_REAL_CHROME = _chrome()


@unittest.skipUnless(_REAL_CHROME, '本机没有 Google Chrome / Chromium')
class LiveShutdownTests(unittest.TestCase):
    """**真浏览器那一半。** 走的是 `x_cdp.Chrome` 自己那条路:独立临时 profile、
    `--headless=new`,不碰用户日常那个浏览器、不开有头窗口。

    为什么不用别的浏览器回归都在用的 chrome-headless-shell:那个二进制**压根
    不建 `SingletonLock`**(2026-10-09 实测),而这一组要验的后果之一正是那把锁
    —— 换个二进制就把唯一要验的东西换掉了。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='kb-live-shutdown-'))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.tmp)], check=False))

    def test_a_healthy_browser_exits_gracefully_and_frees_the_profile(self):
        """对照组:好好退出的那一次,进程是自己走的(不是被 kill 的)、fd 收了、
        锁也由浏览器自己清掉了。"""
        page = x_cdp.Chrome(str(self.tmp), chrome_path=_REAL_CHROME)
        with page:
            proc, fds = page.proc, (page._r, page._w)
            self.addCleanup(_reap, proc)
            page.open_page('about:blank')
            self.assertEqual(page.eval('1+1'), 2)
            self.assertTrue(x_cdp.profile_busy(str(self.tmp)),
                            '运行中的浏览器该算占着这个 profile')
        self.assertEqual(proc.returncode, 0, f'被 kill 掉了:{proc.returncode}')
        self.assertTrue(_closed(fds))
        self.assertEqual((page._r, page._w, page.proc), (-1, -1, None))
        self.assertFalse(x_cdp.profile_busy(str(self.tmp)))

    def test_a_wedged_browser_is_really_gone_and_the_profile_is_usable_again(self):
        """审查那份证据的反面,逐条对着来:`SIGSTOP` 停住它、灌满命令管道,
        `__exit__` 之后进程真的没了、两个 fd 收了、状态复位,而且**那把残锁不再
        挡住「检查连接」**(审查实测里它会永久报「登录窗口还开着,关掉它再试」)。
        """
        page = x_cdp.Chrome(str(self.tmp), chrome_path=_REAL_CHROME)
        page.__enter__()
        proc, fds = page.proc, (page._r, page._w)
        self.addCleanup(_reap, proc, fds)
        page.open_page('about:blank')
        os.kill(proc.pid, signal.SIGSTOP)              # 它从此不再读命令
        time.sleep(0.1)
        _stuff(page._w)
        started = time.monotonic()
        with patch.object(x_cdp, 'WRITE_TIMEOUT', 0.3):
            page.__exit__(None, None, None)            # 不许抛
        self.assertLess(time.monotonic() - started, 8.0, '收尾没有上限')
        self.assertIsNotNone(proc.poll(), '卡住的浏览器还活着(用户看不见也关不掉)')
        self.assertTrue(_closed(fds), '两个管道 fd 还开着')
        self.assertEqual((page._r, page._w, page.proc), (-1, -1, None), '状态没复位')
        self.assertTrue(os.path.lexists(self.tmp / 'SingletonLock'),
                        '前提:kill 之后那把锁确实留在原处 —— 所以判据不能只看它在不在')
        self.assertFalse(x_cdp.profile_busy(str(self.tmp)), '残锁被当成「有人占着」')
        with patch.object(x_browser, 'browser_session',
                          side_effect=AssertionError('前置检查放行了')):
            with self.assertRaises(AssertionError):
                x_browser.probe(str(self.tmp), '')


if __name__ == '__main__':
    unittest.main()
