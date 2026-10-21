# -*- coding: utf-8 -*-
"""`tests/test_fixture_life*.py` 两份共用的小工具 —— 起夹具、等端口、造凭据、复位。

拆出来只是因为那两份加起来过了 700 行的上限。**这里不放任何断言**:判据和后果各自
留在自己那份里,这边只有「怎么起一个、怎么看它还在不在」这类机械活。
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import fixture_life  # noqa: E402


def _shut_fds(*fds: int) -> None:
    """收尾关掉几个裸 fd,已经关了的不吭声。"""
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


def _stat_of(pid: int) -> str:
    """`ps` 里那个状态字段(`T` = 被按住了)。"""
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                          capture_output=True, text=True).stdout


def _open_fds() -> int:
    """当前进程开着多少个 fd。`listdir` 自己那个在返回前已经关掉了。"""
    return len(os.listdir("/dev/fd"))


def _free_port() -> int:
    """挑一个空闲端口(口径同 `headless.fixture_server`:绑 0 让内核给)。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def _wait_gone(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _ping(port: int, path: str = "/", timeout: float = 2) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout).read()
        return True
    except urllib.error.HTTPError:
        return True                       # 有回应就算起来了,状态码不重要
    except (urllib.error.URLError, OSError):
        return False


def _wait_listening(port: int, proc: subprocess.Popen | None = None,
                    timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"夹具还没监听就退了(rc={proc.returncode})")
        if _ping(port, timeout=1):
            return
        time.sleep(0.1)
    raise AssertionError("夹具没有在限时内监听")


def _owner_pipe(token: str = "tok0123456789abcd") -> tuple[int, int, str]:
    """造一份跟 `fixture_server()` 同形的凭据:(读端, 写端, 环境变量值)。

    **裸 fd 一建出来就得登记回收**:`os.write` 也会抛(管道满、号已经坏了),抛在这里
    的话两个号就漏了,而调用方还没来得及 `addCleanup` —— 口径同 `fixture_server`。
    """
    r, w = os.pipe()
    try:
        os.write(w, token.encode())
    except BaseException:
        for fd in (r, w):
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    return r, w, f"{r}:{token}"


def _identity(pid: int) -> str:
    """一个进程的**不可混淆身份**:启动时刻 + 命令行。拿不到就返回空串。

    **端口和脚本名都不是所有权凭据。** 「是这一种服务、正在用这个端口」在另一轮的同型
    夹具复用了这个端口之后一样成立 —— 实测:本轮的 A 退出、另一轮的 B 接手同一个端口,
    按「名字对上、端口对上」清理把活着的 B 杀掉了。pid 也会被回收,所以要连启动时刻一起
    比:**同一个 pid 上的两个进程,启动时刻必然不同**。
    """
    out = subprocess.run(["ps", "-o", "lstart=,command=", "-p", str(pid)],
                         capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def _settled_identity(pid: int, needle: str, budget: float = 5.0) -> str:
    """等这个号上的命令行真的变成**我们起的那一个**,再把身份记下来。

    `subprocess.Popen` 一返回就能拿到 pid,但那一刻子进程可能还没 exec 完 —— `ps` 给出
    的命令行还是父进程的一份副本。把这个**转瞬即逝**的样子记成「这一轮的身份」,回收时
    一比就对不上,于是一个字都不敢碰(`_reap_pid` 的保守分支)。实测:整套一起跑时稳定
    漏下一个手起的夹具,而单条跑机器闲、exec 抢在 pid 那一行前面,一次都看不见。

    漏在手起那条用例上不是巧合:它的立意正是「没有 owner、空闲防线也关了,所以它不该
    自己退」—— **没有任何别的东西会来收它**,只要这一次回收不动手就是永久留着。

    `needle` 是「它变成了自己」的标志(脚本名加端口)。拿不到身份说明它已经没了,返回空;
    等不到也照旧返回当下这个 —— 它仍然是我们起的那个进程,收得动。
    """
    deadline = time.monotonic() + budget
    while True:
        got = _identity(pid)
        if not got or needle in got:
            return got
        if time.monotonic() >= deadline:
            return got
        time.sleep(0.05)


def _reset_state() -> None:
    """把那份共享状态复位 —— **每条用例开头都要**。

    `_decide` 判出该退就把 `_CLOSING` 永久落下(进程本来马上就要没了),放在一个
    长活的测试进程里就会毒到后面每一条:新请求一律不接纳,在途永远是 0。
    """
    with fixture_life._COUNT:
        fixture_life._INFLIGHT = 0
        fixture_life._CLOSING = False
        fixture_life._LAST = time.monotonic()
    fixture_life._OWNER_GONE = False


def _age(seconds: float) -> None:
    """把「上一个请求回完」的时刻往前拨 —— 别靠真的等。"""
    with fixture_life._COUNT:
        fixture_life._LAST = time.monotonic() - seconds


def _closed(fd: int) -> bool:
    try:
        os.fstat(fd)
        return False
    except OSError:
        return True
