# -*- coding: utf-8 -*-
"""起一个真夹具、再按身份收掉它 —— 两份后果用例共用的那些**机械活**。

拆出来只是因为 `tests/test_fixture_life_outcome.py` 过了 700 行的上限。这里只有「怎么起
一个、怎么收掉它」,**判据和断言各自留在用例那边**;跟 `tests/fixture_life_probe.py` 的
分工是:那份是纯函数,这份要用到 `addCleanup` / `skipTest`,所以做成基类。

`tests/test_probe_contract.py` 的「谁可以自己起沙箱服务」对两份后果用例开了例外,而且必
须开:其中一条验的正是**夹具自己**退出时删不删得掉那份根,所以不能给 `KB_FIXTURE_ROOT`
—— 走 `fixture_server` 的话根归父进程,它的 `finally` 会把结论盖掉,收场信号改错了也照样
全绿。
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
import unittest

from tests import fixture_life
from tests.fixture_life_probe import (
    _shut_fds, _free_port, _identity, _wait_listening, ROOT,
)


class 起与收(unittest.TestCase):
    """真起一个夹具,断言的是「那个进程还在不在」。本身不含用例。"""

    def _spawn(self, *, idle: str, upstream: str = "", stderr=subprocess.DEVNULL,
               stdout=subprocess.DEVNULL):
        """直接起一个 `serve_workspace.py`(手起这一档:没有 owner 凭据)。"""
        port = _free_port()
        env = {**os.environ, fixture_life.IDLE_ENV: idle}
        env.pop(fixture_life.OWNER_ENV, None)
        argv = [sys.executable, "tests/serve_workspace.py", "--port", str(port)]
        if upstream:
            argv += ["--upstream", upstream]
        proc = subprocess.Popen(argv, cwd=ROOT, env=env,
                                stdout=stdout, stderr=stderr)
        self.addCleanup(self._reap, proc)
        _wait_listening(port, proc)
        return port, proc

    def _spawn_via_middle(self, *, idle: str, owned: bool,
                          nonblocking: bool = False, highfd: bool = False):
        """让一个中间进程去起夹具,好让我们杀掉它来模拟调用方没了。

        `owned=True` 时中间进程按 `fixture_server()` 的协议给出所有权凭据。
        **孙进程的 pid 由中间进程交回**(stdout 第一行)—— 按端口猜所有权会误杀。
        """
        port = _free_port()
        code = "\n".join([
            "import os, subprocess, sys, time",
            "env = dict(os.environ)",
            f"key = {fixture_life.OWNER_ENV!r}",
            f"owned = {owned!r}",
            f"nonblocking = {nonblocking!r}",
            f"highfd = {highfd!r}",
            "hold = []",
            # 先把低号占满,凭据就落到 1024 以上。**环境上限是这个场景的前提**:默认软上
            # 限在不少机器上只有 256,所以显式抬高;硬上限不够就如实报一句让上面跳过,不
            # 要以一个看不出原因的 EMFILE 红在半路(口径同 `_hold_high_fds`)。
            "if highfd:",
            "    import resource",
            "    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)",
            "    need = 1300",
            "    if soft < need:",
            "        if hard != resource.RLIM_INFINITY and hard < need:",
            "            print('highfd-unavailable=%d' % hard, flush=True)",
            "            raise SystemExit(0)",
            "        resource.setrlimit(resource.RLIMIT_NOFILE, (need, hard))",
            "    for _ in range(1100):",
            "        hold.append(os.open(os.devnull, os.O_RDONLY))",
            "pass_fds = ()",
            "if owned:",
            "    r, w = os.pipe()",
            "    os.write(w, b'tok0123456789abcd')",
            "    if nonblocking:",
            "        os.set_blocking(r, False)",
            "    env[key] = '%d:tok0123456789abcd' % r",
            "    pass_fds = (r,)",
            "    print('ownerfd=%d' % r, flush=True)",
            "else:",
            "    env.pop(key, None)",
            f"p = subprocess.Popen([sys.executable, 'tests/serve_workspace.py',"
            f" '--port', '{port}'], env=env, pass_fds=pass_fds,"
            " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
            "print(p.pid, flush=True)",
            "time.sleep(600)",
        ])
        env = {**os.environ, fixture_life.IDLE_ENV: idle}
        env.pop(fixture_life.OWNER_ENV, None)
        middle = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self._reap, middle)
        self.addCleanup(middle.stdout.close)
        # 有凭据时中间进程先吐一行 `ownerfd=N`(好让用例断言它确实落在高位),再吐 pid
        self._owner_fd = -1
        while True:
            line = middle.stdout.readline().strip()
            self.assertTrue(line, "中间进程什么都没吐出来")
            if line.startswith("highfd-unavailable="):
                self.skipTest(f"这台机器打开文件的硬上限只有 "
                              f"{line.partition('=')[2]},构造不出高位凭据")
            if line.startswith("ownerfd="):
                self._owner_fd = int(line.partition("=")[2])
                continue
            grandchild = int(line)
            break
        # 身份就地记即可 —— 它是**不可变**的那部分(启动时刻),exec 换掉命令行也不变,
        # 所以不必等它「安定下来」,也不会留下一个空身份的登记(见 `_identity`)。
        self.addCleanup(self._reap_pid, grandchild, _identity(grandchild))
        _wait_listening(port, middle)
        return port, middle, grandchild

    def _port_open(self, port: int) -> bool:
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _reap(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    def _reap_pid(self, pid: int, started: str) -> None:
        """按**这一轮那个进程的身份**收 —— `started` 是起它的时候记下的 `_identity`。

        身份对不上就一个字都不碰:要么它已经退了,要么这个号上现在是别人。只比脚本名
        不够(另一轮的同型夹具也叫这个名字),只比 pid 不够(号会被回收)。
        """
        if not started or _identity(pid) != started:
            return
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    def _wait_dead(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = subprocess.run(["ps", "-o", "pid=", "-p", str(pid)],
                                 capture_output=True, text=True).stdout.strip()
            if not out:
                return True
            time.sleep(0.2)
        return False

    # ---- 空闲那一道 ----


    def _reap_stopped(self, pid: int, started: str) -> None:
        """被 SIGSTOP 按住的那个:**先核验身份,再 SIGCONT**。

        SIGCONT 也是信号。上一版为了「先放它走才收得动」把它发在核验之前,那等于对一个
        还没认过的 pid 动手 —— 号被回收之后唤醒的是别人的进程。发信号的每一条分支都只
        许作用在核验过的那个身份上,一条都不例外。
        """
        if not started or _identity(pid) != started:
            return
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGCONT)
            os.kill(pid, signal.SIGKILL)


    def _plug(self) -> tuple[int, int]:
        """造一根**已经灌满**的管道,交给子进程当 stdout。返回 (读端, 写端)。"""
        r, w = os.pipe()
        self.addCleanup(_shut_fds, r, w)
        filled = 0
        os.set_blocking(w, False)         # 只有灌的这一下用非阻塞:这是**我们自己**的 fd
        with contextlib.suppress(BlockingIOError):
            while filled < 1 << 20:
                filled += os.write(w, b"x" * 4096)
        os.set_blocking(w, True)          # 交给子进程之前还成阻塞,逼它走丢提示那条路
        self.assertGreater(filled, 0, "没把管道灌住,这条用例没验到东西")
        return r, w
