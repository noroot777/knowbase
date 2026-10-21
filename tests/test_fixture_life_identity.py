# -*- coding: utf-8 -*-
"""**按身份收掉一个进程**这件事的后果:对上就得收掉,对不上一个字都不许碰。

从 `tests/test_fixture_life_outcome.py` 拆出来的,原因只有一个:那份过了 700 行的上限。
起/收那些机械活在 `tests/fixture_life_cases.py`,纯函数在 `tests/fixture_life_probe.py`。

这一组盯的是同一个不变量:**身份只能由 pid 加启动时刻构成** —— 那是进程**不可变**的那
部分。掺进会变的东西(命令行会被 `execv` 换掉)就会把自己的进程判成别人的、一个字都不敢
碰;丢掉 pid 又会把同一秒里起来的另一个进程判成自己的、一刀砍下去。两头都钉着。

后半组盯的是紧挨着它的另一件事:**取样是会失败的,而让用例报红不等于把它创建的进程收回
了**。所以从「进程建出来」到「有人认领它」之间不许留失败窗口,登记前失败、回收时失败各
钉一条;两条证明都用不上的时候只许抛,那一边也钉着。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests import fixture_life
from tests import fixture_life_cases
from tests import fixture_life_probe
from tests.fixture_life_cases import 起与收
from tests.fixture_life_probe import (
    IdentityUnknown, _free_port, _identity, _stat_of, _wait_listening, ROOT,
)


class 身份与回收(起与收):
    """真起进程,断言「该收的收掉了、不该碰的一个字都没碰」。"""

    def test_身份对不上的同型夹具一个字都不碰(self):
        """**第 1 条的后果。** 「是这一种服务、正在用这个端口」在另一轮的同型夹具接手
        之后一样成立 —— 实测照着这两个条件清理,把另一轮活着的那个杀掉了。所以回收只
        认「那个 pid + 它的启动时刻」:同一个 pid 上的两个进程,启动时刻必然不同。
        """
        _, first = self._spawn(idle="0")
        stale = _identity(first.pid)
        self.assertTrue(stale, "先确认拿得到身份")
        first.kill()
        first.wait(timeout=5)
        _, second = self._spawn(idle="0")          # 另一轮的同型夹具
        self._reap_pid(second.pid, stale)          # 拿上一个的身份去收
        time.sleep(0.5)
        self.assertIsNone(second.poll(), "身份对不上,一个字都不许碰")

    def test_身份对上就真收掉(self):
        """反过来钉一边:核验不是为了谁都不收 —— 对上就得收得掉。

        这里断的是**退出码**而不是「`ps` 里还在不在」:它是这个测试进程自己的子进程,
        收掉之后在被 `wait()` 之前是僵尸,照样列在 `ps` 里。
        """
        _, proc = self._spawn(idle="0")
        self._reap_pid(proc.pid, _identity(proc.pid))
        self.assertEqual(proc.wait(timeout=5), -signal.SIGKILL, "身份对上就该收掉")

    def test_exec换掉命令行之后照旧收得掉(self):
        """**身份只能记不可变的那部分。**

        `Popen` 一返回就能拿到 pid,但那一刻子进程可能还是父进程的一份副本,`ps` 给出的
        命令行还没换。上一版把命令行也算进身份里,于是记在 exec 之前的身份跟它后来的样子
        对不上,`_reap_pid` 的保守分支一个字都不碰 —— 而「手起的夹具父进程退了也不退」那
        条用例的立意正是没有 owner、空闲防线也关了,**没有任何别的东西会来收它**。实测:
        整套一起跑稳定留下一个手起的夹具,单条跑机器闲、exec 抢在 pid 那一行前面,一次都
        看不见。再加大等待预算也不是不变量 —— 等超了照样记下一个会变的样子。

        这里用 fork 之后先睡一会儿再 `execv` 把那个窗口**撑开**(不靠机器忙),断言的是
        后果:**在它换命令行之前记下的身份,换完之后照旧收得掉**。反过来那一边由
        `test_身份对不上的同型夹具一个字都不碰` 钉着:启动时刻不同就一个字都不许碰。
        """
        port = _free_port()
        needle = f"serve_workspace.py --port {port}"
        # 脚本名和端口走 argv,不写进 `-c` 的正文 —— 否则连父进程的命令行都「像」夹具了
        code = "\n".join([
            "import os, sys, time",
            "script, port = sys.argv[1], sys.argv[2]",
            "pid = os.fork()",
            "if pid == 0:",
            "    time.sleep(2.0)",     # exec 推后,把那个窗口撑开
            "    os.execv(sys.executable, [sys.executable, script, '--port', port])",
            "print(pid, flush=True)",
            "time.sleep(600)",
        ])
        env = {**os.environ, fixture_life.IDLE_ENV: "0"}   # 它不该自己退,才看得出收没收
        env.pop(fixture_life.OWNER_ENV, None)
        middle = subprocess.Popen(
            [sys.executable, "-c", code, "tests/serve_workspace.py", str(port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self._reap, middle)
        self.addCleanup(middle.stdout.close)
        child = int(middle.stdout.readline().strip())
        born = self._claim_pid(child, middle)    # 记在它 exec 之前
        self.assertTrue(born, "先确认这一刻就拿得到身份")
        before = subprocess.run(["ps", "-o", "command=", "-p", str(child)],
                                capture_output=True, text=True).stdout
        self.assertNotIn(needle, before,
                         "这条用例要的是 exec 之前那一刻,没撑开就什么也没验到")
        _wait_listening(port, middle)
        after = subprocess.run(["ps", "-o", "command=", "-p", str(child)],
                               capture_output=True, text=True).stdout
        self.assertIn(needle, after, "它确实换过一副命令行")
        self.assertEqual(_identity(child), born, "换命令行不该换掉身份")
        # **先收掉中间进程再验「收得掉」。** 它是孙进程的父亲、又永远不会 `wait()`,
        # 孙进程被收掉之后会以僵尸的身份一直列在 `ps` 里(口径同 `test_身份对上就真收掉`)。
        middle.kill()
        middle.wait(timeout=5)
        self._reap_pid(child, born)
        self.assertTrue(self._wait_dead(child, 5), "换过命令行也该收得掉")
        self.assertFalse(self._port_open(port), "收掉了就不该再有人占着这个端口")

    def test_量不出身份时不许当成它已经没了(self):
        """**空身份是静默漏进程的形态。** `ps` 起不来、被权限或资源挡住的时候回空串,
        等于告诉回收方「没什么要收的」,于是一个活着的夹具留在后台,而用例全绿。

        所以 `_identity` 只在 `ps` 明确说「没有这个进程」时回空串,量不出来一律抛 ——
        测试里抛出去是**响的**。反过来钉一边:真的没有这个进程,照旧安静地回空串。
        """
        _, proc = self._spawn(idle="0")
        with mock.patch.object(fixture_life_probe.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 1, "", "ps: 起不来")):
            with self.assertRaises(AssertionError):
                _identity(proc.pid)
        # 反过来钉一边:用一个**真的已经退了并且被 `wait` 收过**的号(不用随手编一个大
        # 数字 —— `ps` 对超出 pid 范围的输入是报错,那落在上面那一边)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=10)
        self.assertEqual(_identity(dead.pid), "", "真没有这个进程就该安静地回空串")

    def test_判不出身份的那几种形状都是同一种(self):
        """**失败契约只有一种形状,下游才认得出它。**

        回收那边那条「量不出来就换另一条证明」只认 `IdentityUnknown`。所以凡是「判不出
        来」都得在 `_identity` 这一层收成它:
        - **起 `ps` 这个子进程本身失败**(fd 不够时建管道/fork 就失败)—— 内核给的是
          `OSError`,照原样漏出去的话下游认不出,那条还能用的证明压根没机会用;
        - **`ps` 自己被信号打死、而且什么都没说** —— 上一版的 absent 判据是「非零退出码 +
          stderr 是空的」,这一种正好满足它,于是一个活着的进程被判成「没什么要收的」。

        反过来钉一边:平台确实说「没有这个进程」的那一种(`rc == 1`、两个流都空)照旧安静
        地回空串,上一条用真退掉的进程钉着。
        """
        _, proc = self._spawn(idle="0")
        with mock.patch.object(fixture_life_probe.subprocess, "run",
                               side_effect=OSError(24, "Too many open files")):
            with self.assertRaises(IdentityUnknown):
                _identity(proc.pid)
        with mock.patch.object(fixture_life_probe.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], -9, "", "")):
            with self.assertRaises(IdentityUnknown):
                _identity(proc.pid)

    def test_取样抛的是资源错也照样收得掉(self):
        """**上一版这条判据写的是 `except AssertionError`。**

        而真实的取样失败来自起 `ps` 那个子进程本身,内核给的是 `OSError` —— 它从判据里漏
        出去,`holder` 那条还能用的证明压根没机会用,清理当场中断。实测把软上限压到 64 占
        满 fd 再跑整轮清理(那一条在隔壁,后果落在「夹具还在不在」上):清理全部跑完、中
        间进程也收掉了,而那个夹具还在、页面照样 200。

        这里只钉判据这一层:取样抛的是**资源错**时,照旧按 holder 那条证明收得掉。
        """
        port, middle, grandchild = self._spawn_via_middle(idle="0", owned=False)
        born = _identity(grandchild)
        with mock.patch.object(fixture_life_probe.subprocess, "run",
                               side_effect=OSError(24, "Too many open files")):
            self._reap_pid(grandchild, born, holder=middle)
        middle.kill()                     # 先收掉它的父亲,否则孙进程是僵尸(见上面那条)
        middle.wait(timeout=5)
        self.assertTrue(self._wait_dead(grandchild, 10),
                        "取样抛的是资源错也不许放过它")
        self.assertFalse(self._port_open(port), "页面不该还开着")

    def test_被按住的进程身份对不上连唤醒都不许(self):
        """**SIGCONT 也是信号。** 上一版为了「先放它走才收得动」把它发在核验之前,
        那等于对一个还没认过的 pid 动手 —— 号被回收之后唤醒的是别人的进程。
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import os,signal,time;"
             "os.kill(os.getpid(), signal.SIGSTOP); time.sleep(600)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap, proc)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "T" not in _stat_of(proc.pid):
            time.sleep(0.1)
        self.assertIn("T", _stat_of(proc.pid), "先确认它真的被按住了")
        self._reap_stopped(proc.pid, "这不是它的身份")
        time.sleep(0.5)
        self.assertIn("T", _stat_of(proc.pid),
                      "身份对不上,连 SIGCONT 都不许发")


    def test_同一秒里起来的两个进程身份必须不同(self):
        """**启动时刻只精确到秒。** 丢掉 pid 的话,同一秒里起来的两个进程读出来一模一样
        —— 而回收那两个出口只比这个串,pid 是另一个参数。实测:杀掉本轮的 A、紧接着起另
        一轮的 B,拿 A 的身份去收 B,串对上了,**活着的 B 被一刀砍掉**。

        上面那条「同型夹具一个字都不碰」要撞上这个才会红,而两次起进程落不落在同一秒要看
        机器 —— 所以这里**把同秒那一对构造出来**再断言,不靠运气。
        """
        pairs = []
        for _ in range(20):
            # **一起一个登记一个。** 两个都起完才登记的话,第二个 `Popen` 抛出来时第一
            # 个就没人收了(口径同 `_claim_pid`:拿到手就立责任,不等后面那步成不成)。
            a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            self.addCleanup(self._reap, a)
            b = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            self.addCleanup(self._reap, b)
            raw = [subprocess.run(["ps", "-o", "ls" "tart=", "-p", str(p.pid)],
                                  capture_output=True, text=True).stdout.strip()
                   for p in (a, b)]
            if raw[0] and raw[0] == raw[1]:
                pairs.append((a, b))
                break
        self.assertTrue(pairs, "没构造出同一秒里起来的一对,这条用例没验到东西")
        a, b = pairs[0]
        self.assertNotEqual(_identity(a.pid), _identity(b.pid),
                            "同一秒里起来的两个进程,身份必须认得出不是一个")

    def test_取样失败也不许把起好的夹具丢在后台(self):
        """**让用例报红不等于把它创建的进程收回了。**

        `addCleanup(self._reap_pid, pid, _identity(pid))` 是先求实参、后登记:`_identity`
        量不出来时一律抛(它接口里就这么定的),抛在那一行登记就压根没发生。实测在取样处
        定点注入它自己声明的那个 `AssertionError`:已有清理(收中间进程、关 stdout)全部
        执行成功,留下的还是那个真夹具 —— ppid 已经是 1、页面照样 200。而这一条起的正是
        「没有 owner、空闲也关掉」那一档,**没有任何别的东西会来收它**。

        所以这里不断言「抛没抛」,断言的是后果:取样失败之后把那个壳的清理照常跑完,
        **那个进程没了、端口也不响应了**。
        """
        caught: dict = {}
        real_identity = fixture_life_cases._identity
        real_port = fixture_life_cases._free_port

        def _boom(pid: int) -> str:
            caught.setdefault("pid", pid)
            caught.setdefault("born", real_identity(pid))
            raise AssertionError("injected:量不出身份")

        def _grab_port() -> int:
            caught["port"] = real_port()
            return caught["port"]

        shell = fixture_life_cases.起与收()   # 一个独立的壳,好让我们自己跑它的清理
        # **清理栈本身也是「一拿到手就登记」的那种东西。** 它自己的 `_claim_pid` 登记在它
        # 自己那本账上,而外层 unittest 不会去跑另一个 TestCase 的账 —— 下面任何一步红了
        # (抛的不是预期那个、断言不过、等不到就绪),那本账就没人翻,中间进程和孙进程
        # 一起留在后台。实测在取样处制造真实 fd 紧张:外层红了、清理也跑完了,内层还欠
        # 着三条,中间进程活着、孙进程的端口照旧 200。`doCleanups` 是幂等的(弹空就空
        # 转),所以这里登记一次、下面照旧显式跑一次。
        self.addCleanup(shell.doCleanups)
        with mock.patch.object(fixture_life_cases, "_identity", _boom), \
                mock.patch.object(fixture_life_cases, "_free_port", _grab_port):
            with self.assertRaises(AssertionError):
                shell._spawn_via_middle(idle="0", owned=False)
        self.assertIn("pid", caught, "没走到取样那一步,这条用例没验到东西")
        # 兜底:下面的断言红了也不许把它留在后台 —— 这条用例的立意就是这个
        self.addCleanup(self._reap_pid, caught["pid"], caught["born"])
        _wait_listening(caught["port"], timeout=30)   # 先确认它真的在服务
        shell.doCleanups()
        self.assertTrue(self._wait_dead(caught["pid"], 10),
                        "取样失败也不许把起好的那个夹具丢在后台")
        self.assertFalse(self._port_open(caught["port"]), "页面不该还开着")

    def test_回收时量不出身份也不许放过它(self):
        """**另一个窗口:登记好了,真要收的时候那一次取样抛了。**

        两个回收出口原来都是「再取样一次,对不上就返回」—— 取样本身抛出来的时候
        `addCleanup` 记下一条错误,而那个进程照旧留在后台。创建者手里本来还有一条**不靠
        取样**的证明:`holder` 是它的父进程、又永远不 `wait()`,活着的时候这个号即使已经
        退了也还只是僵尸,不可能落到别人手上。

        断言的是后果:取样抛了,**它照样收得掉**。
        """
        port, middle, grandchild = self._spawn_via_middle(idle="0", owned=False)
        born = _identity(grandchild)
        # 注入的是它**接口里真会抛的那个类型**(`IdentityUnknown`),不是更宽的
        # `AssertionError` —— 判据只认那一种形状,注入得比契约宽等于没按契约验
        with mock.patch.object(fixture_life_cases, "_identity",
                               mock.Mock(side_effect=IdentityUnknown("injected"))):
            self._reap_pid(grandchild, born, holder=middle)
        # 先收掉中间进程再看「收没收掉」:它是孙进程的父亲又永远不 `wait()`,孙进程被收
        # 掉之后会以僵尸的身份一直列在 `ps` 里(口径同 `test_身份对上就真收掉`)。
        middle.kill()
        middle.wait(timeout=5)
        self.assertTrue(self._wait_dead(grandchild, 10),
                        "回收时量不出身份也不许放过它")
        self.assertFalse(self._port_open(port), "页面不该还开着")

    def test_两条证明都用不上时宁可红也不凭号动手(self):
        """**反过来钉一边:认不出来的时候只有抛是诚实的。**

        凭一个号就动手会杀掉别人的进程(同一秒里起来的那一对就是这么验出来的);安静地
        放过它则是静默漏一个还在服务的夹具。所以两条证明都用不上时 `_mine` 抛 —— 测试里
        抛出去是响的,`addCleanup` 当场记一条错误,而那个无关的进程一个字都没被碰。
        """
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self._reap, proc)
        with self.assertRaises(AssertionError):
            self._reap_pid(proc.pid, None)     # 身份还没量出来,又没有 holder
        time.sleep(0.3)
        self.assertIsNone(proc.poll(), "认不出是不是我们的,一个字都不碰")

    def test_不是量不出身份的那种错不许被吞掉(self):
        """**判据只认那一种形状。**

        `IdentityUnknown` 是 `AssertionError` 的子类(为了「测试里抛出去是响的」),所以
        把捕获写宽成 `except AssertionError` 时,**别处的** `AssertionError` —— 另一个 bug、
        或者用例自己注入的断言 —— 会被一起当成「量不出身份」,静默换去走另一条证明。上一
        轮的反向验证就是这么变成假绿的:注入 `AssertionError` 的那一版照旧被判据吞掉。

        所以这里钉的是:不是那一种形状的错**必须抛出去**,而那个夹具由它自己的清理去收。
        """
        _, middle, kid = self._spawn_via_middle(idle="0", owned=False)
        born = _identity(kid)
        with mock.patch.object(fixture_life_cases, "_identity",
                               mock.Mock(side_effect=AssertionError("别处的 bug"))):
            with self.assertRaises(AssertionError):
                self._reap_pid(kid, born, holder=middle)

    def test_嵌套的清理栈要立刻交给外层(self):
        """**清理栈本身也是「一拿到手就登记」的那种东西。**

        内层那个壳自己的 `_claim_pid` 记在**它自己**那本账上,而外层 unittest 不会去跑另一
        个 `TestCase` 的账 —— 创建它之后任何一步红了(抛的不是预期那个、断言不过、等不到
        就绪),那本账就没人翻,它起的中间进程和夹具一起留在后台。实测在就绪等待那一处注
        入失败:外层红了、外层的清理也跑完了,内层还欠着三条,中间进程活着、孙进程的端口
        照旧 200。

        这里把那个形态原样搭出来(外层壳 → 内层壳 → 起一个真夹具 → 中途抛),断言后果:
        **外层 `run()` 回来之后,内层起的那个夹具没了、端口也不响应**。全仓有没有漏掉的同
        类位置,由 `tests/test_probe_contract.py` 那条按结构判的守卫盯着。
        """
        box: dict = {}
        outer = self

        class 外层(fixture_life_cases.起与收):
            def runTest(self):                       # noqa: D102 - 见外面那条 docstring
                shell = fixture_life_cases.起与收()
                self.addCleanup(shell.doCleanups)    # ← 被钉的就是这一行
                port, middle, kid = shell._spawn_via_middle(idle="0", owned=False)
                box.update(port=port, kid=kid, born=_identity(kid))
                # 兜底:下面抛出去之后万一没人收,也别留在后台
                outer.addCleanup(outer._reap_pid, kid, box["born"])
                raise AssertionError("injected:壳建出来之后任何一步红了")

        外层().run(unittest.TestResult())
        self.assertIn("kid", box, "没走到起夹具那一步,这条用例没验到东西")
        self.assertTrue(self._wait_dead(box["kid"], 10),
                        "内层那本账没人翻 —— 它起的夹具留在后台了")
        self.assertFalse(self._port_open(box["port"]), "页面不该还开着")
