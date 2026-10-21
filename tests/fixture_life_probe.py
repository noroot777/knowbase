# -*- coding: utf-8 -*-
"""`tests/test_fixture_life*.py` 两份共用的小工具 —— 起夹具、等端口、造凭据、复位。

拆出来只是因为那两份加起来过了 700 行的上限。**这里不放任何断言**:判据和后果各自
留在自己那份里,这边只有「怎么起一个、怎么看它还在不在」这类机械活。
"""

from __future__ import annotations

import contextlib
import os
import resource
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


def _hold_high_fds(case, count: int = 1100) -> list[int]:
    """把低号占满,好让下一个 `os.pipe()` 落到 1024 以上。回收由 `case` 负责。

    **一成功一登记,不许「全开完了才登记」。** 列表推导里某一次 `os.open` 抛出来时,
    那个列表压根还没赋给变量,前面已经开成功的几百个号就没人收了 —— 整数被丢掉不会关掉
    内核里的 fd。实测:把软上限压到 128 跑这条用例,清理全跑完之后进程里还剩 125 个开着
    的号,接下来一次普通 `open(os.devnull)` 就 `EMFILE`,**后面每一条用例都跟着坏**。
    准备阶段失败只该让这一条跳过,不该污染整轮。

    **环境上限是这条用例的前提,要显式抬高、抬不动就明说跳过。** 默认软上限在不少机器上
    只有 256,够不上 1100;硬上限不够时这个场景在这台机器上构造不出来 —— 那就跳过,不要
    让它以一个看不出原因的 `EMFILE` 红在半路。
    """
    hold: list[int] = []
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    need = count + 200              # 留出余量给 unittest 自己和那根凭据管道
    if soft < need:
        if hard != resource.RLIM_INFINITY and hard < need:
            case.skipTest(f"这台机器打开文件的硬上限只有 {hard},构造不出高位凭据")
        # 先登记还原、再登记关 fd:清理是后进先出,fd 得在还原上限之前关掉
        case.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        resource.setrlimit(resource.RLIMIT_NOFILE, (need, hard))
    case.addCleanup(lambda: _shut_fds(*hold))
    for _ in range(count):
        hold.append(os.open(os.devnull, os.O_RDONLY))
    return hold


def _identity(pid: int) -> str:
    """一个进程的**不可变**身份:它的启动时刻。

    **只认不可变的那部分,而且把 pid 绑进去。** pid 会被回收,所以光比 pid 不够;而
    命令行**会变** ——
    `execv` 一跑就换一副样子,上一版把它也算进身份里,结果记在 exec 之前的身份跟它后来
    的样子对不上,回收时一个字都不敢碰(实测稳定漏下一个手起的夹具)。启动时刻 exec
    前后不变,又足以把号回收后的另一个进程区分开:要让同一个 pid 落在同一秒里,得在一
    秒内把整个 pid 空间转一圈(macOS 近十万、Linux 默认三万二),不是真实情形。

    **端口和脚本名都不是所有权凭据。** 「是这一种服务、正在用这个端口」在另一轮的同型
    夹具接手了这个端口之后一样成立 —— 实测:本轮的 A 退出、另一轮的 B 接手同一个端口,
    按「名字对上、端口对上」清理把活着的 B 杀掉了。

    **「量不出来」和「它已经没了」是两件事。** `ps` 起不来、被权限或资源挡住的时候返回
    空串就等于告诉回收方「没什么要收的」,而那正是静默漏一个进程的形态。所以只有 `ps`
    明确说「没有这个进程」才回空串,量不出来一律抛 —— 测试里抛出去是**响的**,用例当场
    红,不会把一个活着的夹具留在后台。
    """
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                         capture_output=True, text=True)
    got = out.stdout.strip()
    if got:
        # **pid 要绑进这个串里。** 启动时刻只精确到秒,同一秒里起来的两个进程读出来一模
        # 一样 —— 而回收那两个出口只比这个串,pid 是另一个参数。实测:杀掉本轮的 A、紧接
        # 着起另一轮的 B,拿 A 的身份去收 B,串对上了,活着的 B 被杀掉。
        return f"{pid}@{got}"
    if out.returncode != 0 and not out.stderr.strip():
        return ""                    # `ps` 的「没有这个进程」
    raise AssertionError(
        f"量不出 pid {pid} 的启动时刻(rc={out.returncode},"
        f" stderr={out.stderr.strip()!r}) —— 不许当成「它已经没了」")


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
