# -*- coding: utf-8 -*-
"""`tests/test_fixture_life*.py` 两份共用的小工具 —— 起夹具、等端口、造凭据、复位。

拆出来只是因为那两份加起来过了 700 行的上限。**这里不放任何断言**:判据和后果各自
留在自己那份里,这边只有「怎么起一个、怎么看它还在不在」这类机械活。
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import json
import os
import resource
import struct
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


class IdentityUnknown(AssertionError):
    """**量不出这个号的身份** —— 跟「它已经没了」是两件事,见 `_identity`。

    继承 `AssertionError`:测试里抛出去是**响的**,用例当场红。单独给它一个类型,是为了
    让回收那边那条「量不出来就换另一条证明」**只认它**:
    - 认「凡 `AssertionError`」不够 —— 取样那一步真正的失败来自 `ps` 这个**子进程起不来**
      (fd 不够时建管道/fork 就失败),内核给的是 `OSError`,它不是 `AssertionError`。实测
      把软上限压到 64 占满 fd 之后跑整轮清理:`OSError(24)` 从判据里漏出去,holder 那条还
      能用的证明压根没机会用,清理全跑完而那个夹具还在、页面照样 200。
    - 认「凡 `OSError`」更不行:那是按异常类型分类,口径同 `fixture_life._is_stale` 那条
      ——「这个号坏了」和「我们这边没资源了」得分开。

    所以统一在 `_identity` 这一层把三种结果定清楚:**量到了**(非空串)、**确实没有这个
    进程**(空串)、**判不出来**(抛这个)。上面那两种宽判都会把第三种吞成第二种。
    """


def _sysctl_probe():
    """内核那条**不用 fork、不用 fd** 的问法 —— 问不出来就返回 None,由调用方降级。

    为什么非要它:回收一个跨会话的后代时,判据得「届时还问得出来」。原先只有 `ps` 一条
    路,而那是起一个子进程 —— fd 紧张时建管道/fork 当场失败,于是**已经创建好的那棵树在
    最需要收的时候反而判不出归属**(实测:真把软上限压到 64,账本在、能读,外层也收掉了,
    内层四个进程和页面照旧活着)。`sysctl` 是一次纯系统调用,不申请 fd、不建进程。
    """
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        return lib.sysctl
    except Exception:
        return None


_SYSCTL = _sysctl_probe() if sys.platform == "darwin" else None
_PROC_STAT = sys.platform.startswith("linux")
_CTL_KERN, _KERN_PROC, _KERN_PROC_PID = 1, 14, 1
_KINFO_CAP = 4096                  # 实测一条 `kinfo_proc` 是 648 字节


def _starttime_from_stat(blob: str, pid: int) -> int:
    """从 `/proc/<pid>/stat` 的一行里取第 22 个字段(starttime,单位是时钟节拍)。

    **进程名在第 2 个字段里、而且带括号,里面什么字符都可能有**(空格、右括号都见过)。
    所以不能按空格切整行 —— 从**最后一个** `)` 之后开始数,那之后的字段才是定长的。
    第 1 个字段也要核:它必须等于问的那个号,对不上说明这一行不是它(抛,口径同主路的
    回声自校验)。
    """
    cut = blob.rfind(")")
    if cut < 0 or not blob[:cut].startswith(f"{pid} ("):
        raise IdentityUnknown(f"/proc/{pid}/stat 这一行认不出来:{blob[:60]!r}")
    rest = blob[cut + 2:].split()
    if len(rest) < 20:
        raise IdentityUnknown(f"/proc/{pid}/stat 字段不够({len(rest)} 个)")
    try:
        return int(rest[19])             # 从 state(第 3 个字段)起数到第 22 个
    except ValueError as exc:
        raise IdentityUnknown(f"/proc/{pid}/stat 的 starttime 不是数:{exc}") from exc


def _identity_proc(pid: int) -> str:
    """Linux 那条**不 fork** 的路:读 `/proc/<pid>/stat`。

    比起 `ps` 便宜一个量级(一次 open/read,不建管道也不建进程),所以 fd 紧张时它还站得
    住的概率高得多 —— 但它**仍然要一个 fd**,真的一个都不剩时照旧判不出来(那一刻会落到
    `ps` 后路、两条都不成就抛,仍然是响的)。**这条路没有在 Linux 实机上验过**:这里只钉
    住它的解析和三种结果的形状,平台覆盖如实记在决策文档里。
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            blob = f.read()
    except FileNotFoundError:
        return ""                        # `/proc` 的「没有这个进程」
    except OSError as exc:
        raise IdentityUnknown(f"读不了 /proc/{pid}/stat:{exc!r}") from exc
    return f"{pid}@{_starttime_from_stat(blob, pid)}"


def _identity_sysctl(pid: int) -> str:
    """`kinfo_proc` 开头那个 `struct timeval __p_starttime`,精度到微秒。

    **结构解对了没有,自己核一遍**:偏移 40 处是 `p_pid`,它必须等于我们问的那个号 ——
    对不上就说明这份偏移在当前内核上不成立,那是「判不出来」(抛,下游会降级到 `ps`),
    **不许当成身份**。靠一组猜出来的偏移去回收进程会杀掉别人。

    「没有这个进程」在这条路上有两种说法,都要认:`rc == 0` 而**一个字节都没返回**,或者
    明确的 `ESRCH`。其余 errno 一律是判不出来。
    """
    mib = (ctypes.c_int * 4)(_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid)
    size = ctypes.c_size_t(_KINFO_CAP)
    buf = ctypes.create_string_buffer(_KINFO_CAP)
    ctypes.set_errno(0)
    rc = _SYSCTL(mib, 4, buf, ctypes.byref(size), None, 0)
    if rc != 0:
        got = ctypes.get_errno()
        if got == errno.ESRCH:
            return ""
        raise IdentityUnknown(f"sysctl 问不出 pid {pid}(errno={got})")
    if size.value == 0:
        return ""                  # 内核的「没有这个进程」
    if size.value < 44:
        raise IdentityUnknown(f"sysctl 给的 kinfo_proc 太短({size.value} 字节)")
    sec, usec = struct.unpack_from("=qi", buf.raw, 0)
    echo, = struct.unpack_from("=i", buf.raw, 40)
    if echo != pid:
        raise IdentityUnknown(
            f"kinfo_proc 的偏移在这个内核上不成立(问 {pid},回声 {echo})")
    return f"{pid}@{sec}.{usec:06d}"


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
    明确说「没有这个进程」才回空串,量不出来一律抛 `IdentityUnknown` —— 测试里抛出去是
    **响的**,用例当场红,不会把一个活着的夹具留在后台。

    **失败契约只有一种形状。** 起 `ps` 这个子进程本身会抛 `OSError`(fd 不够时建管道/fork
    就失败),那也是「判不出来」,得在这里就转成 `IdentityUnknown` —— 让它按原样漏出去,
    下游那条「量不出来就换另一条证明」就认不出它(实测后果见 `IdentityUnknown`)。

    **「没有诊断文本」不等于「没有这个进程」。** 退回 absent 的判据要收到平台确实这么说的
    那一种:`ps` 查不到进程时 `rc == 1` 且两个流都空。拿「非零退出码 + stderr 是空的」当
    absent 太宽 —— `ps` 自己被信号打死(`rc` 是负数)、或者以别的码失败而什么都没说时也满足
    它,于是一个还活着的进程被判成「没什么要收的」。宁可判不出来:那一边有 holder 兜着,
    两条都没有时会抛,都是响的。
    """
    if _PROC_STAT:
        return _identity_proc(pid)
    if _SYSCTL is not None:
        # **一条路走到底,不混用。** 两条路量出来的串形状不同(内核给到微秒,`ps` 只到
        # 秒),记的时候用一条、收的时候用另一条就一定对不上。所以这个选择在模块加载时
        # 定下来,整轮(以及跨进程的内层/外层)都用同一条。
        return _identity_sysctl(pid)
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True)
    except OSError as exc:            # `ps` 这个子进程起不来:判不出来,不是「它没了」
        raise IdentityUnknown(f"起不来 `ps`(要量 pid {pid}):{exc!r}") from exc
    got = out.stdout.strip()
    if got:
        # **pid 要绑进这个串里。** 启动时刻只精确到秒,同一秒里起来的两个进程读出来一模
        # 一样 —— 而回收那两个出口只比这个串,pid 是另一个参数。实测:杀掉本轮的 A、紧接
        # 着起另一轮的 B,拿 A 的身份去收 B,串对上了,活着的 B 被杀掉。
        return f"{pid}@{got}"
    if out.returncode == 1 and not out.stderr.strip():
        return ""                    # `ps` 的「没有这个进程」
    raise IdentityUnknown(
        f"量不出 pid {pid} 的启动时刻(rc={out.returncode},"
        f" stderr={out.stderr.strip()!r}) —— 不许当成「它已经没了」")


def 开工(path: str) -> None:
    """**创建任何东西之前先立个标记。** 之后账上一笔都没有,也不等于后台没东西。

    上一版把「账本文件不存在」读成「它还没起到有东西要收」。那不成立:账是整棵树起齐之后
    才一次性发布的,于是从第一个进程跑起来到发布完成之间,**整棵树和页面都已经可用,而正式
    账还不存在**。实测在发布前取消:基类的取消收场确实跑了,`_reap_book` 因为文件不在直接
    返回,锚点、内层、中间进程、夹具四个全在、页面 200。

    所以判据换成这个标记:**有标记而账上一条都没有 = 判不出来,一律抛**(响的红),不许
    返回「确定没有」。口径同 `_identity` 的三种结果。
    """
    open(path + ".starting", "w").close()


def 记一笔(path: str, name: str, pid: int) -> None:
    """**一个号一拿到手就记一笔**,不等整棵树起齐。

    一行一条 JSON、`O_APPEND` 追加写:半截只可能出现在**最后一行**(写到一半就被掐掉),
    读的时候丢掉没有换行结尾的那一行就行 —— 不需要「先写 `.part` 再改名」那种整份发布,
    而整份发布正是上一版那个窗口的来源。

    号和身份仍然在**同一刻**一起记下(判据仍在 `_mine`)。身份这一问现在走内核那条不用
    fork、不用 fd 的路(见 `_identity`),所以这一笔的代价是几微秒,窗口小到只剩「进程刚
    建出来、这一行还没写完」。
    """
    try:
        ident = _identity(pid)
    except IdentityUnknown:
        # **身份量不出来也要把这个号记下来(`id` 置空)。** 这一笔要是干脆不记,外层只会
        # 读到「账上没有这个号」,于是一个还在服务的后代被静默漏掉;记成「有号、身份未
        # 知」的话,外层按它去收会在 `_mine` 那儿抛 —— 响的红,不是静默漏。口径同
        # `_identity` 的三种结果:判不准一律扣留,不许返回「确定没有」。
        ident = None
    line = json.dumps({"name": name, "pid": pid, "id": ident},
                      ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def 记目录(path: str, root) -> None:
    """**把一份目录也交给外层。** 建它的那个进程被强杀时自己删不掉它。

    记录和删除分在两个进程里:建它的那一侧只负责交出路径,真正确认全树退出之后由外层
    删。实测过它的反面 —— 原样跑一条用例(不注入任何故障),拥有者没走到自己的 `finally`
    时留下两份空的验证目录。
    """
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"dir": str(root)}) + "\n")


def 记端口(path: str, port: int) -> None:
    """把页面那个号也记在同一本账上(给断言用,不参与回收)。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"port": port}) + "\n")


def 读账(path: str) -> tuple[bool, list[dict]]:
    """返回(开工了没有, 记下来的那几笔)。**半截的最后一行丢掉。**"""
    started = os.path.exists(path + ".starting")
    rows: list[dict] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f.read().splitlines(keepends=True):
                if not line.endswith("\n"):
                    continue                      # 写到一半:这一行不算
                with contextlib.suppress(ValueError):
                    rows.append(json.loads(line))
    return started, rows



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
