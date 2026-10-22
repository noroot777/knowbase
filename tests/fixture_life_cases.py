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
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

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


def _slurp(path: str) -> str:
    """读一个可能压根没写出来的文件 —— 超时那条路上内层正在写就被收掉了。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def 立账(path: str, port: int, **pids: int) -> None:
    """**内层把它那一棵树的号连身份一起交出来** —— 外层的兜底只认这一份。

    为什么要有这份账:`_run_nested` 的锚点必须 `setsid` 成自己那一组(否则按组收会打
    到调用方自己那一组),于是**外层那一组的 `killpg` 到不了它**。平时这不是问题——整
    棵树的所有权在 `_run_nested` 的 `finally` 里;可外层进程被 SIGKILL 的时候那条
    `finally` 一行都不跑,跨会话又没有「按会话杀」这种系统调用,只能逐个号来。而逐个
    号就要有归属证明,所以**号和身份必须在同一刻一起记下**(判据仍在 `_mine`)。

    身份串里有空格(`lstart` 的格式),所以这份账走 JSON 不走空格分隔;先落 `.part` 再
    改名,读到的一定是整份。`_identity` 量不出来照旧抛 —— 那会儿账还没立,读账那边看到
    的是「压根没有这份账」,用例当场红,不会变成静默漏一棵树。
    """
    rec = {"port": port, "pids": pids,
           "ids": {k: _identity(v) for k, v in pids.items()}}
    with open(path + ".part", "w", encoding="utf-8") as f:
        json.dump(rec, f)
    os.replace(path + ".part", path)


class 起与收(unittest.TestCase):
    """真起一个夹具,断言的是「那个进程还在不在」。本身不含用例。"""

    def run(self, result=None):
        """**取消也是一个验证出口,它一样要走完清理。**

        `unittest` 在这条路上不跑 `doCleanups()`:`_Outcome.testPartExecutor` 对
        `KeyboardInterrupt` 一路往上抛,而 `TestCase.run()` 里那行 `self.doCleanups()`
        排在那几个 `testPartExecutor` 块**之后** —— 压根执行不到。于是「进程一建出来就
        `addCleanup`」这条口径在取消这个终态上一个字都不算。实测:直接跑一条真用例再给
        它 SIGINT,运行器 rc=-2,而它起的那个夹具还在、页面照样 200;那一档正是
        `idle='0'` 又没有 owner,三道自退防线全关着,它会一直在后台服务下去。

        所以在这一层补上,**所有创建点一起管**:`_spawn`、`_spawn_via_middle` 的中间进
        程、`_claim_pid` 认领的孙进程、`_outer` 起的独立会话,它们的回收全登记在同一个
        清理栈上,这里跑一次就都收了。

        `doCleanups` 自己抛什么都不许盖掉取消那个异常 —— 它只是收场,这一轮真正的结论
        是「被取消了」。而 `KeyboardInterrupt` 不是 `Exception` 的子类,所以第二次
        Ctrl-C 照旧打得断这段收场。
        """
        try:
            return super().run(result)
        except KeyboardInterrupt:
            with contextlib.suppress(Exception):
                self.doCleanups()
            raise

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

    #: 那个**锚点**:它是新会话的组首,只做一件事 —— 替我们把「收尸」这件事接过去。
    #: 它起内层运行器(于是内层和它的全部后代都落在这个组里),等内层退出,把退出码**原子
    #: 地**落到一个文件上,然后自己退掉。它的尸只由我们收,所以那个 pgid 一直被它占着。
    _ANCHOR = "\n".join([
        "import os, subprocess, sys",
        "out, err, rc, root = sys.argv[1:5]",
        "with open(out, 'w') as o, open(err, 'w') as e:",
        "    code = subprocess.call(sys.argv[5:], cwd=root, stdout=o, stderr=e)",
        "open(rc + '.part', 'w').write(str(code))",
        "os.replace(rc + '.part', rc)",      # 先写再改名:我们不会读到半份
    ])

    def _run_nested(self, argv: list[str], *, timeout: float,
                    env: dict[str, str] | None = None,
                    ) -> subprocess.CompletedProcess:
        """起一个**内层验证运行器**,整棵树的最终回收责任留在我们这一层。

        几条用例要在子进程里再跑一轮 unittest(低上限、资源紧张、带空格的 `TMPDIR`),
        而内层那一轮自己还会起中间进程和真夹具。**`subprocess.run(..., timeout=...)` 在
        这儿不够**:它超时只 SIGKILL 直接那个子进程,被强杀的运行器一条 `addCleanup` 都
        跑不了 —— 它起的那两层就没人收了。实测把一个内层运行器在起好夹具之后按住到外层
        deadline:运行器没了,中间进程和夹具都还活着、页面 200、那份根也还在;夹具的
        owner 写端攥在**活着的**中间进程手里,所以连 EOF 那道防线都不会响,而
        `idle='0'` 那一档连空闲防线都没有。

        **「等到它退出」和「可以不管它的后代了」是两件事。** 上一版把内层直接起成组首,
        于是拿退出码这件事本身就把尸收了 —— 而那一刻 pgid 的归属证明跟着消失,后代却可能
        还活着:内层被外部杀掉时它压根没跑自己的清理。实测两条出口各留下一个在服务的夹具
        (中间进程也在,页面 200),而外层一切正常、清理全跑完。所以中间加一个**锚点**:
        内层是它的子进程、同组,内层的退出码由锚点写到文件上,**锚点的尸只由我们收** ——
        于是从起跑到我们自己放手,那个 pgid 一直被它占着,按组动手的证明始终成立。

        另外两件事:给内层一份**隔离的 `TMPDIR`**(里面留下的根由我们删,不按名字去猜别人
        的目录;调用方给了 `TMPDIR` 就建在它底下,所以「那个目录底下什么都不剩」这类断言
        照旧成立),以及**所有终态走同一条收场路** —— 正常退、超时、内层被外部杀掉、外层
        自己被 `KeyboardInterrupt` 打断,都从这个函数的 `finally` 过一遍。**不能只靠
        `addCleanup`**:`unittest` 遇到 `KeyboardInterrupt` 会把它一路往上抛,`doCleanups()`
        那一行压根不执行。

        超时返回的 `returncode` 是 `None`。
        """
        env = dict(os.environ if env is None else env)
        box = tempfile.mkdtemp(prefix="nested-", dir=env.get("TMPDIR") or None)
        self.addCleanup(shutil.rmtree, box, ignore_errors=True)
        env["TMPDIR"] = box
        work = tempfile.mkdtemp(prefix="nested-work-")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        paths = {k: os.path.join(work, k) for k in ("out", "err", "rc", "anchor")}
        with open(paths["anchor"], "w") as anchor_err:
            proc = subprocess.Popen(
                [sys.executable, "-c", self._ANCHOR, paths["out"], paths["err"],
                 paths["rc"], str(ROOT), *argv],
                cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=anchor_err,
                start_new_session=True)
        self.addCleanup(self._sweep_session, proc)   # 冗余带:正常路径上已经是空操作
        out = err = ""
        rc: int | None = None
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not os.path.exists(paths["rc"]):
                time.sleep(0.05)
        finally:
            # **所有终态同一条路**(正常退、超时、内层被外部杀掉、外层自己被取消):先按组
            # 收(锚点钉着那个 pgid),再收尸,**读完那几份输出**,最后两份目录都删掉。
            # 顺序不许反:根删在前面的话,服务还活着时它的工作目录就没了;读排在删后面的
            # 话什么都读不到。**两份都得在这儿删** —— 取消那条路上 `doCleanups()` 压根不跑,
            # 只挂在那上面的目录就留在盘上了(实测每次取消留一份)。
            self._sweep_session(proc)
            out = _slurp(paths["out"])
            err = _slurp(paths["err"]) + _slurp(paths["anchor"])
            raw = _slurp(paths["rc"]).strip()
            if raw:
                with contextlib.suppress(ValueError):   # 在 finally 里不许抛
                    rc = int(raw)
            shutil.rmtree(box, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)
        # `rc` 为 `None` = 没拿到内层的退出码,也就是超时那条路(锚点是 `os.replace` 原子
        # 写的,读到半份写不出来;真写坏了的话诊断在 `err` 里)。
        return subprocess.CompletedProcess(argv, rc, out, err)

    def _sweep_session(self, proc: subprocess.Popen) -> None:
        """把那棵树**整组**收掉 —— `proc` 是那个锚点。

        **归属证明是「那个锚点还没被收尸」**:组里只要还有成员(**僵尸也算**),内核就不会
        把这个 pgid 分给别人。锚点的尸只在这个函数里收,所以从它起跑到这一刻,证明一直
        成立。`returncode` 一旦有值就什么都不做 —— 那是**这个函数自己**收过尸了,再对那个
        号动手,杀掉的可能是别人那一轮的验证。口径同 `_mine` 的 holder 那条:证明用不上就
        不动手。

        **拿到内层的退出码不等于可以放手**:内层退了,它起的中间进程和夹具可能还在。所以
        收场这一下在每条路上都要走,不是只在超时那条。
        """
        if proc.returncode is not None:
            return
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        # **「调用过 `killpg`」不等于「整棵树已经退出」。** 信号是异步投递的,而调用方
        # 回来之后下一件事就是读输出、删那两份目录 —— 删早了等于把一个还在服务的夹具的
        # cwd 抽掉。所以在这儿等那一组里除了组首之外都走掉。**探测必须排在收尸之前**:
        # 组首的尸还占着这个 pgid,内核就不会把它分给别人,这一轮探测问的才是我们那一组。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self._group_left(proc.pid, proc.pid):
            time.sleep(0.05)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)

    def _开账(self) -> Path:
        """要一个账本路径,**顺手把按账的兜底和那份工作目录一起登记上**。

        路径和登记从同一个出口出来,调用点就没有「拿了路径、忘了登记兜底」这种漏法 ——
        这一类漏不会在任何一条用例上报红,只会在后台留下一棵还在服务的树。

        登记顺序也在这儿定死:先登记删目录、再登记按账收,于是**按账收先跑、删目录后跑**
        (清理是后进先出)。反了的话删掉的是一个还在服务的夹具的 cwd。
        """
        work = Path(tempfile.mkdtemp(prefix="book-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        self.addCleanup(self._reap_book, str(work / "book"))
        return work / "book"

    def _reap_book(self, book) -> None:
        """**另一个会话里那棵树的兜底:按账本里记下的身份逐个收。**

        这条路专治「拥有它的那个进程自己没走到收场」:实测让一条用例在它的断言之前先红
        (普通 `AssertionError`,登记的清理**全部正常执行**),被收掉的只有外层那一组,
        而锚点、内层运行器、中间进程、夹具四个全活着、页面照样 200 —— 外层被杀掉就跑不
        了 `_run_nested` 的 `finally`,而那四个不在外层那一组里。

        没有这份账就什么都不做:那是「内层还没起到有东西要收」,不是漏。判据和动手都复
        用 `_reap_pid`,所以外层已经自己收干净的正常路径上,这里量到的身份是空串(`ps`
        说没有这个号)→ 一个字都不碰。
        """
        if not os.path.exists(book):
            return
        with open(book, encoding="utf-8") as f:
            rec = json.load(f)
        for name, pid in rec["pids"].items():
            self._reap_pid(pid, rec["ids"][name])

    def _group_left(self, pgid: int, skip: int) -> list[int]:
        """那一组里除了 `skip` 还剩哪些进程。量不出来一律当「还剩着」。

        用 `ps -eo pid=,pgid=` 自己过滤,不用 `ps -g`:后者在各家 BSD/Linux 上的语义
        不一样(有的按会话首解释)。`ps` 起不来或者输出读不成时返回一个占位的号 ——
        **判不出来不许当成「都走了」**,那正是「等了 0 毫秒就去删目录」的形态;等满上限
        顶多慢 5 秒。
        """
        try:
            out = subprocess.run(["ps", "-eo", "pid=,pgid="],
                                 capture_output=True, text=True)
        except OSError:
            return [-1]                                   # 判不出来 → 当还剩着
        if out.returncode != 0:
            return [-1]
        left = []
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                pid, got = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if got == pgid and pid != skip:
                left.append(pid)
        return left

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
