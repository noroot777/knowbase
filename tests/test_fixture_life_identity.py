# -*- coding: utf-8 -*-
"""**按身份收掉一个进程**这件事的后果:对上就得收掉,对不上一个字都不许碰。

从 `tests/test_fixture_life_outcome.py` 拆出来的,原因只有一个:那份过了 700 行的上限。
起/收那些机械活在 `tests/fixture_life_cases.py`,纯函数在 `tests/fixture_life_probe.py`。

这一组盯的是同一个不变量:**身份只能由 pid 加启动时刻构成** —— 那是进程**不可变**的那
部分。掺进会变的东西(命令行会被 `execv` 换掉)就会把自己的进程判成别人的、一个字都不敢
碰;丢掉 pid 又会把同一秒里起来的另一个进程判成自己的、一刀砍下去。两头都钉着。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from unittest import mock

from tests import fixture_life
from tests import fixture_life_probe
from tests.fixture_life_cases import 起与收
from tests.fixture_life_probe import (
    _free_port, _identity, _stat_of, _wait_listening, ROOT,
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
        born = _identity(child)      # 记在它 exec 之前
        self.addCleanup(self._reap_pid, child, born)
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
            a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            b = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            for proc in (a, b):
                self.addCleanup(self._reap, proc)
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
