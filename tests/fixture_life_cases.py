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
from tests.fixture_life_probe import (   # noqa: F401 - 账本那几个由这儿转出去给内层用
    IdentityUnknown, _shut_fds, _free_port, _identity, _wait_listening, ROOT,
    开工, 记一笔, 记端口, 记目录, 读账,
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
        self.__dict__.setdefault("_未了", [])
        self._已让过 = False
        try:
            try:
                return super().run(result)
            except KeyboardInterrupt:
                # **先补完被打断那一项,再跑剩下的清理。** 反过来的话,剩下的清理里那些
                # 「释放依赖」的步骤(删目录、关 fd)会先跑掉,补跑就没有依据了。
                with contextlib.suppress(Exception):
                    self._补完()
                with contextlib.suppress(Exception):
                    self.doCleanups()
                raise
        finally:
            self._补完()

    _账: str | None = None           # 设了就表示「这一轮的创建要跨进程交接归属」

    def _记目录(self, root) -> None:
        """把一份目录交给外层(没设账本就什么都不做,口径同 `_记`)。"""
        if self._账:
            记目录(self._账, root)

    def _记(self, name: str, pid: int) -> None:
        """**一个号拿到手就交接,不等创建它的那个助手返回。**

        上一版是在 `_spawn_via_middle()` 返回之后才记 middle/kid 两笔。可那个助手**内部**
        就已经把中间进程和夹具起起来、还等了页面就绪 —— 那几百毫秒里树和页面都能用,账上
        却只有先记下的 anchor/inner。实测:在那一刻让外层普通地红一条,照常跑完全部清理,
        anchor/inner 被收掉而 middle/kid 仍在、页面 200。**「账上有几笔」不等于「这几笔
        覆盖了全部已创建的后代」**,所以交接要挪到创建点自己里面。

        没设账本就什么都不做 —— 本进程自己的 `_担上` 已经担着,跨进程那一份只有内层需要。
        """
        if self._账:
            记一笔(self._账, name, pid)

    def _担上(self, fn, *a, **kw) -> None:
        """登记一项回收,并且**在它真跑完之前不许从责任里消失**。

        `unittest` 执行一项 cleanup **之前**就把它从栈里弹掉了。于是第一次 SIGINT 正好落
        在这一项里面时,它既没跑完、也不在栈上 —— 再跑一次 `doCleanups()` 也不会把它找
        回来。实测:取消落在身份那一问上,外层已经退出,内层四个进程和页面照旧活着,而
        账本反而被剩下的清理删掉了。**「回调开始执行」不等于「回收已经完成」。**

        所以这儿留一份自己的账:进这一项之前记上,**跑完才划掉**,`run()` 收尾时把没划掉
        的再跑一遍。顺序不变 —— 这只是在原来那个清理栈上包了一层。
        """
        # **懒取,不靠 `run()` 先跑过。** 有一类用例会直接实例化一个裸壳、自己跑它的
        # `doCleanups()`(内层那本账交给外层的那条),那条路上 `run()` 压根不经过。
        pending = self.__dict__.setdefault("_未了", [])

        def _一项() -> None:
            token = (fn, a, kw)
            pending.append(token)
            fn(*a, **kw)                 # 抛出去就留在账上,收尾时再补
            pending.remove(token)

        self.addCleanup(_一项)

    def _补完(self) -> None:
        """把没划掉的再跑一遍 —— 它们要么压根没进去过,要么被打断在半路。

        第一次取消打断的那一项**原地再试一次**;**第二次取消照旧放手** —— 文档里那条
        「第二次 Ctrl-C 打得断这段收场」仍然成立,这里只是不把「被打断」算成「做完了」。
        """
        pending = self.__dict__.setdefault("_未了", [])
        tried: dict[int, int] = {}
        while pending:
            fn, a, kw = pending[0]
            try:
                fn(*a, **kw)
            except KeyboardInterrupt:
                if self._已让过:
                    raise
                self._已让过 = True
                continue
            except Exception as exc:                        # noqa: BLE001
                # **「已经试过一次」不等于可以放手。** 第一次失败多半是依赖还没就位
                # (证据被别人删了、holder 还活着),所以原地再试一次;两次都不成才划
                # 掉,并且把它说出来 —— 静静划掉就是把一个可能还在服务的后代丢在后台。
                n = tried.get(id(fn), 0) + 1
                tried[id(fn)] = n
                if n < 2:
                    continue
                print(f"[收场] {getattr(fn, '__name__', fn)} 补跑两次都没成:{exc}",
                      file=sys.stderr)
            pending.pop(0)

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
        self._担上(self._reap, proc)
        self._记("kid", proc.pid)        # 交接排在「等页面就绪」之前
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
        self._担上(self._reap, middle)
        self._记("middle", middle.pid)   # 同上:不等它把孙进程的号吐回来
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
        self._担上(self._sweep_session, proc)
        # 这两份目录是**这个进程**建的,它被上层强杀时自己删不掉 —— 所以也交给外层那本账
        # (实测的反面:原样跑一条用例,拥有者没走到自己的 `finally`,留下两份空目录)。
        self._记目录(box)
        self._记目录(work)       # 冗余带:正常路径上已经是空操作
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
            if getattr(self, "_未确认退出", None):
                print(f"[收场] 还有成员没确认退出,{box} / {work} 留着不删",
                      file=sys.stderr)
            else:
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
        left = self._group_left(proc.pid, proc.pid)
        while time.monotonic() < deadline and left:
            time.sleep(0.05)
            left = self._group_left(proc.pid, proc.pid)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        # **到期不等于确认退出。** 这一问是尽力而为的(`ps` 自己也可能慢),所以等满上限
        # 还有成员时要把这个事实说出来,不许静静往下走当成「整组都走了」—— 下一件事就是
        # 删那两份目录。只报不抛:抛在这儿会盖掉调用方真正的结论,而那份目录留着比删掉
        # 一个还在服务的夹具的 cwd 安全。
        if left:
            # **未确认退出就不许释放依赖。** 这个标志由收场那一处读:它还立着的时候那两
            # 份目录一个字都不动 —— 删早了抽掉的是一个可能还在服务的夹具的 cwd,而「已经
            # 尝试过按组收」不是安全释放的依据。
            self._未确认退出 = sorted(left)
            print(f"[收场] pgid {proc.pid} 等满 5 秒仍有成员 {sorted(left)} —— "
                  "未确认退出,那两份目录留着", file=sys.stderr)

    def _开账(self) -> Path:
        """要一个账本路径,**顺手把按账的兜底和那份工作目录一起登记上**。

        路径和登记从同一个出口出来,调用点就没有「拿了路径、忘了登记兜底」这种漏法 ——
        这一类漏不会在任何一条用例上报红,只会在后台留下一棵还在服务的树。

        登记顺序也在这儿定死:先登记删目录、再登记按账收,于是**按账收先跑、删目录后跑**
        (清理是后进先出)。反了的话删掉的是一个还在服务的夹具的 cwd。
        """
        work = Path(tempfile.mkdtemp(prefix="book-"))
        self._担上(self._收账与目录, work)
        self._账 = str(work / "book")
        return work / "book"

    def _收账与目录(self, work: Path) -> None:
        """**回收和它依赖的那份证据是同一个责任单元 —— 一起成,或者一起留着。**

        上一版把「删这份目录」单独登记在清理栈上。于是第一次取消打断按账回收之后,剩下
        的 `doCleanups()` 先把目录(连账本)删了,补跑那一下读到的是「没开工、没有账」,
        待办被划掉而整棵树留在后台 —— 实测:补跑时账本和开工标记都已经消失,四个号全在、
        页面 200。**账本是重试唯一的依据,不许排在重试前面被别人删掉。**

        所以删目录这一步挪进来,排在回收**之后**:上一步抛出去(或者被取消打断),这一项
        整体留在待办上,目录一个字都不动,补跑时账还在。
        """
        self._reap_book(str(work / "book"))
        shutil.rmtree(work, ignore_errors=True)

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
        started, rows = 读账(str(book))
        pids = [r for r in rows if "pid" in r]
        if not pids:
            if started:
                raise AssertionError(
                    f"{book} 立了开工标记、账上一条都没有 —— 那边可能已经起了东西,"
                    "判不出来不许当成没得收")
            return                       # 没开工:本来就没有什么要收的
        errs = []
        for row in pids:
            # **一项判不出来不许放弃其余项。** 上一版是直接循环,第一个号抛出去之后剩下
            # 三个连试都没试过 —— 实测 fd 紧张那一次漏的就是后面那三个加一个在服务的页面。
            try:
                self._reap_pid(row["pid"], row.get("id"))
            except Exception as exc:                        # noqa: BLE001
                errs.append(f'{row.get("name")}/{row["pid"]}: {exc}')
        if errs:
            raise AssertionError("这几个号判不出归属,一个都没敢动:" + "; ".join(errs))
        # **进程全收干净了才动它交出来的目录。** 顺序跟上面那条一样的理由:建目录的那个
        # 进程被强杀时自己删不掉它,而删早了抽掉的是一个可能还在服务的夹具的 cwd。
        for row in rows:
            if "dir" in row:
                shutil.rmtree(row["dir"], ignore_errors=True)

    def _group_left(self, pgid: int, skip: int) -> list[int]:
        """那一组里除了 `skip` 还剩哪些进程。量不出来一律当「还剩着」。

        用 `ps -eo pid=,pgid=` 自己过滤,不用 `ps -g`:后者在各家 BSD/Linux 上的语义
        不一样(有的按会话首解释)。`ps` 起不来或者输出读不成时返回一个占位的号 ——
        **判不出来不许当成「都走了」**,那正是「等了 0 毫秒就去删目录」的形态;等满上限
        顶多慢 5 秒。
        """
        try:
            out = subprocess.run(["ps", "-eo", "pid=,pgid="],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
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
                   stopped: bool = False, name: str = "kid") -> str:
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

        self._担上(_collect)
        self._记(name, pid)          # 跨进程那一份同样「拿到手就交接」
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
