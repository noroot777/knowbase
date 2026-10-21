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
    IdentityUnknown, _shut_fds, _free_port, _identity, _wait_listening, ROOT,
)


def _mine(pid: int, started: str | None,
          holder: subprocess.Popen | None) -> bool:
    """这个号上现在还是不是**我们起的那个**进程?发信号的每一条分支都只经过这里。

    两条证明,哪条用得上用哪条:

    1. **身份**(`_identity`:pid + 启动时刻)。对不上就一个字都不碰 —— 要么它已经退
       了,要么这个号上现在是别人。只比脚本名不够(另一轮的同型夹具也叫这个名字),
       只比 pid 不够(号会被回收)。**「判不出来」只认 `IdentityUnknown` 这一种形状**:
       上一版写的是 `except AssertionError`,而真实失败来自起 `ps` 那个子进程本身
       (fd 不够时建管道/fork 就失败),内核给的是 `OSError` —— 它从判据里漏出去,下面
       那条还能用的证明压根没机会用。实测把软上限压到 64 占满 fd 再跑整轮清理:清理全
       部跑完、中间进程也收掉了,而那个夹具还在、页面照样 200。失败契约统一在
       `_identity` 那一层(见 `IdentityUnknown`),这里只消费它。
    2. **谁还握着这个号**。`holder` 是这个 pid 的父进程、而且永远不 `wait()`,所以它
       活着的时候这个号即使已经退了也还只是僵尸,不可能落到别人手上。这条**不用取
       样**,所以身份还没量出来(`_claim_pid` 把登记排在取样之前)、或者这会儿量不出
       来的时候,创建者靠它照样收得掉自己起的那个东西。

    两条都用不上就**抛**:凭一个号就动手会杀掉别人的进程(实测过),安静地放过它则是
    静默漏一个还在服务的夹具 —— 测试里抛出去是响的,那是这两者之外唯一诚实的出口。
    """
    if started is not None:
        if not started:
            return False             # 起它那会儿 `ps` 就说没有这个号 —— 本来没得收
        try:
            now = _identity(pid)
        except IdentityUnknown:
            pass                     # 量不出来 → 落到下面那条不靠取样的证明上
        else:
            return now == started    # 空串 = `ps` 说没有这个进程 → 不碰
    if holder is not None and holder.poll() is None:
        return True
    raise AssertionError(
        f"认不出 pid {pid} 还是不是我们起的那个(记下的身份 {started!r}),"
        "也没有别的所有权证明 —— 不许凭一个号就动手,也不许当成没事")


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
        # **拿到 pid 这一刻就立回收责任,身份随后再填**(见 `_claim_pid`):取样会抛,
        # 抛在登记前面的话这个刚起来的夹具就谁都不认领了。身份本身只记**不可变**的那部
        # 分(启动时刻),exec 换掉命令行也不变,所以不必等它「安定下来」。
        self._claim_pid(grandchild, middle)
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

    def _reap_pid(self, pid: int, started: str | None,
                  holder: subprocess.Popen | None = None) -> None:
        """按**这一轮那个进程的身份**收 —— `started` 是起它的时候记下的 `_identity`。

        判据只有一份,在 `_mine` 里(身份、或者「谁还握着这个号」);这里只负责动手。
        `holder` 给得出来就一定要给:回收时再取样一次也会抛,抛在这儿等于把一个还在服
        务的夹具丢在后台。
        """
        if not _mine(pid, started, holder):
            return
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    def _claim_pid(self, pid: int, holder: subprocess.Popen | None = None, *,
                   stopped: bool = False) -> str:
        """**一个裸 pid 一拿到手就立回收责任,不等身份量得出来。** 返回量到的身份。

        `addCleanup(self._reap_pid, pid, _identity(pid))` 是**先求实参、再登记**:
        `_identity` 量不出来时一律抛(它接口里就这么定的),抛在那一行登记就压根没发
        生 —— 用例当场红,而刚起来的那个夹具谁都不认领了。实测:在取样处定点注入它自
        己声明会抛的 `AssertionError`,已有清理(收中间进程、关 stdout)全部执行成功,
        留下的还是那个真夹具,ppid 已经是 1、页面照样 200。**让用例报红不等于把它创建
        的进程收回了。**

        所以顺序倒过来:先登记,再把身份填进那个盒子。身份还没填上就被收的那一刻,
        `_mine` 走的是 `holder` 那条不靠取样的证明。

        **`holder` 自己的清理要登记在这一行之前**(清理是后进先出,于是这一条排在它前
        面收,那会儿 holder 还活着)。反了的话 `_mine` 当场抛 —— 是个响的红,不会变成
        静默漏一个进程。
        """
        box: list[str | None] = [None]
        reaper = self._reap_stopped if stopped else self._reap_pid

        def _collect() -> None:
            reaper(pid, box[0], holder)

        self.addCleanup(_collect)
        box[0] = _identity(pid)      # 量不出来照旧抛 —— 但回收责任已经立好了
        return box[0]

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


    def _reap_stopped(self, pid: int, started: str | None,
                      holder: subprocess.Popen | None = None) -> None:
        """被 SIGSTOP 按住的那个:**先核验,再 SIGCONT**。

        SIGCONT 也是信号。上一版为了「先放它走才收得动」把它发在核验之前,那等于对一个
        还没认过的 pid 动手 —— 号被回收之后唤醒的是别人的进程。发信号的每一条分支都只
        许作用在核验过的那个号上,一条都不例外(判据同样在 `_mine`)。
        """
        if not _mine(pid, started, holder):
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
