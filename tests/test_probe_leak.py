# -*- coding: utf-8 -*-
"""浏览器探针起的无头壳,必须在任何一条退出路径上都被收掉。

2026-10-09 在本机实测到 16 个泄漏的 chrome-headless-shell:存活 6 小时到 1 天
11 小时,连同渲染子进程共 66 个进程、3.8 GB,全部 ppid=1、0 条活跃 CDP 连接、
而且当时一个测试都没在跑。来源是 13 个探针共有的那个结构 —— `spawn` 在开头、
`proc.kill()` 在**正常路径的末尾**,而探针整个是 ESM 顶层 await:中间任何一步
抛异常都让 node 以 unhandled rejection 退出,那一句永远到不了,chrome 被 init
收养之后再没人管。

三条泄漏路径、三层防线,**各有自己的用例**,因为它们谁也兜不住另外两档:
① 探针自己抛异常 → `tests/probe_shell.mjs` 的 `process.on('exit')`;
② Python 侧 timeout(发的是 **SIGKILL**,node 的钩子一个都不执行)
   → `tests/headless.py` 的 `run_probe()` 按进程组收;
③ **调用方被取消**(CI 取消作业、`kill` 那个 pytest、`kill -- -<pgid>`)——
   ② 的独立会话把 node 挪出了调用方的进程组,于是这个信号再也波及不到它,而
   Python 对没有 handler 的 SIGTERM 是直接结束、`finally` 一行都不跑
   → `probe_shell.mjs` 的 `watchParent()` 由**子进程自己**盯 `process.ppid`。

判据一律是**后果**:那个 pid 还在不在。只验「注册了钩子没有」的话,一个注册完
却杀不掉它的实现照样全绿。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.headless import NODE, probe_env, requires_headless, run_probe  # noqa: E402

HERE = Path(__file__).resolve().parent


def _alive(pid: int) -> bool:
    """这个 pid 还在不在(只问存活,不问是谁)。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:          # 在,只是不归我们管
        return True
    return True


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    """等它消失;等到了返回 True。SIGKILL 之后内核要一小会儿才收尸。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _wait_pidfile(path: Path, timeout: float = 90.0) -> tuple[int, int]:
    """等探针把 (node, chrome) 的 pid 写到盘上;等不到就是它压根没起来。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            fields = dict(kv.split("=") for kv in path.read_text().split() if "=" in kv)
            return int(fields["node"]), int(fields["chrome"])
        except (OSError, ValueError, KeyError):
            time.sleep(0.05)
    raise AssertionError(f"探针没在 {timeout} 秒内写出 pid 文件 {path}")


# 「真的挂住」只能靠一个 **ref 的** handle。`await new Promise(() => {})` 挂不住:
# 它不是 handle,event loop 一空 node 就以 exit 13 正常退出("unsettled top-level
# await")—— 那时第一层的 'exit' 钩子把浏览器收掉了,于是**本该验第二层的用例测的
# 是第一层**,去掉 run_probe 里两处 killpg 照样全绿(实测注入 0 红就是这么来的)。
HANG = "await new Promise(r => setTimeout(r, 600000));"


def _write_probe(tmp: Path, name: str, body: str) -> Path:
    """写一个用公共模块起浏览器的最小探针,把 chrome 的 pid 打在 stdout 上。

    顺带也写进 `PROBE_PID_FILE`(如果设了):第三层那条用例的调用方是另一个 Python
    进程,探针的 stdout 被它的 `run_probe` 收进管道,外面读不到 —— 而那条用例恰恰
    要在调用方**死掉之后**去查这几个 pid 还在不在,只能走盘上这一份。
    """
    p = tmp / name
    p.write_text(textwrap.dedent(f'''\
        import {{ writeFileSync }} from "node:fs";
        import {{ launchShell }} from "{HERE / "probe_shell.mjs"}";
        const {{ proc }} = await launchShell(["--window-size=800,600"]);
        process.stdout.write("CHROME_PID=" + proc.pid + "\\n");
        if (process.env.PROBE_PID_FILE) {{
          writeFileSync(process.env.PROBE_PID_FILE,
                        "node=" + process.pid + " chrome=" + proc.pid + "\\n");
        }}
        {body}
    '''), encoding="utf-8")
    return p


@requires_headless
class ProbeShellReapTests(unittest.TestCase):
    """第一层:探针自己退出时(正常或异常)把浏览器带走。"""

    def _run_and_get_pid(self, body: str, name: str) -> int:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            probe = _write_probe(Path(td), name, body)
            # 这里**必须**是裸 subprocess.run,不许改成 run_probe:这一层验的正是
            # 探针自己退出时收不收得干净,而 run_probe 那一下 killpg 会把浏览器一起
            # 收掉 —— 被测的第一层坏掉了也照样全绿。契约那条用例因此把本文件排除在外。
            r = subprocess.run([NODE, str(probe)], capture_output=True, text=True,
                               timeout=90, env=probe_env())
        for line in r.stdout.splitlines():
            if line.startswith("CHROME_PID="):
                return int(line.split("=", 1)[1])
        self.fail(f"探针没报出 chrome 的 pid\nstdout={r.stdout}\nstderr={r.stderr}")

    def test_throwing_probe_takes_the_browser_with_it(self):
        """抛异常退出 —— 这是生产里最常见的那一条,也是 16 个孤儿的来源。"""
        pid = self._run_and_get_pid('throw new Error("模拟一条检查失败");', "boom.mjs")
        self.assertTrue(_wait_gone(pid),
                        f"探针抛异常退出之后 chrome {pid} 还活着(就是那 16 个孤儿的形态)")

    def test_process_exit_takes_the_browser_with_it(self):
        """`process.exit(1)` —— 探针报完失败就是这么收场的。"""
        pid = self._run_and_get_pid("process.exit(1);", "bail.mjs")
        self.assertTrue(_wait_gone(pid), f"探针 process.exit 之后 chrome {pid} 还活着")

    def test_normal_exit_takes_the_browser_with_it(self):
        """反过来钉一边:一切顺利的那一轮同样不许留下东西。"""
        pid = self._run_and_get_pid("", "fine.mjs")
        self.assertTrue(_wait_gone(pid), f"探针正常跑完之后 chrome {pid} 还活着")

    def test_sigterm_takes_the_browser_with_it(self):
        """被 SIGTERM 掐掉(CI 取消、人按 Ctrl-C 之外的那种)也得收干净。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            probe = _write_probe(Path(td), "hang.mjs", HANG)
            # `with` 不能省:退出时它会把这两根管道关掉。裸 Popen 留下的
            # `ResourceWarning: unclosed file` 会跟着这个用例一路报出来。
            with subprocess.Popen([NODE, str(probe)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True,
                                  env=probe_env()) as proc:
                pid = None
                end = time.monotonic() + 60
                while time.monotonic() < end:
                    line = proc.stdout.readline()
                    if line.startswith("CHROME_PID="):
                        pid = int(line.split("=", 1)[1])
                        break
                self.assertIsNotNone(pid, "探针没来得及报出 chrome 的 pid")
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=30)
        self.assertTrue(_wait_gone(pid), f"探针收到 SIGTERM 之后 chrome {pid} 还活着")


@requires_headless
class RunProbeGroupReapTests(unittest.TestCase):
    """第二层:Python 侧 timeout 发的是 SIGKILL,node 的钩子一个都不执行。"""

    def test_timeout_kills_the_whole_group(self):
        """超时那一档 —— 第一层在这里完全是瞎的,所以它必须自己有一条。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            probe = _write_probe(Path(td), "stuck.mjs", HANG)
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                run_probe([NODE, str(probe)], timeout=20, env=probe_env())
            out = caught.exception.output or ""
        pid = None
        for line in out.splitlines():
            if line.startswith("CHROME_PID="):
                pid = int(line.split("=", 1)[1])
        self.assertIsNotNone(
            pid, f"超时之后没收到探针已经打印的输出,排查超时就没线索了\n{out!r}")
        self.assertTrue(_wait_gone(pid),
                        f"run_probe 超时之后 chrome {pid} 还活着 —— 进程组没收干净")

    def test_output_and_returncode_match_subprocess_run(self):
        """形状得和 `subprocess.run` 一样,调用点才能原样读 .returncode/.stdout。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            probe = _write_probe(Path(td), "ok.mjs",
                                 'process.stdout.write("DONE\\n"); process.exit(3);')
            r = run_probe([NODE, str(probe)], timeout=90, env=probe_env())
        self.assertEqual(3, r.returncode)
        self.assertIn("DONE", r.stdout)
        self.assertIsInstance(r.stderr, str)

    def test_child_runs_in_its_own_process_group(self):
        """探针要在**自己的**进程组里 —— 不然那一下 killpg 会把调用方一起带走。

        判据是子进程**真实的 pgid**(让它自己问 `ps`),不是它的 pid:拿 pid 跟调用方
        的 pgid 比是一句恒真的废话,「不开新会话」注入进去照样全绿 —— 第一版就是
        这么写的,注入实测 0 红。
        """
        r = run_probe(
            [NODE, "-e",
             "const {execSync} = require('node:child_process');"
             "const pgid = execSync('ps -o pgid= -p ' + process.pid).toString().trim();"
             "process.stdout.write('PID=' + process.pid + ' PGID=' + pgid + '\\n');"],
            timeout=60, env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        fields = dict(kv.split("=") for kv in r.stdout.split() if "=" in kv)
        pid, pgid = int(fields["PID"]), int(fields["PGID"])
        self.assertEqual(pid, pgid,
                         "探针不是自己那个进程组的 leader —— start_new_session 没生效,"
                         "killpg(proc.pid) 要么找不到组、要么打到别人身上")
        self.assertNotEqual(os.getpgid(os.getpid()), pgid,
                            "探针跟调用方在同一个进程组里,killpg 会误伤跑测试的进程自己")


@requires_headless
class CallerCancelledReapTests(unittest.TestCase):
    """第三层:**调用方**被取消 —— ② 的独立会话正好把信号挡在了 node 外面。

    ①② 对这一档都是瞎的:node 没有抛异常也没收到任何信号(① 的钩子一个都不触发),
    而 Python 侧那个 timeout 随调用方一起消失(② 的 `finally` 一行都不跑)。
    """

    def test_canceling_the_caller_group_takes_the_browser_with_it(self):
        """取消调用方的**整个进程组**,浏览器和 node 都得跟着走。

        判据是**后果**(那两个 pid 还在不在),不是「装了 watchdog 没有」。
        实测修之前:调用方以 -15 退出,node 被过继给 init 之后照旧活着、chrome 挂在
        它下面、DevTools 端口继续监听,而 `run_probe` 那个超时保护也随调用方一起没了
        —— 于是那个浏览器**永久**留着,正是用户在端口面板上看到的那一批。

        **必须取消整个组、而且调用方必须是另一个进程**:只对 node 发信号走的是 ①
        那条路(它的 handler 接得住),验不出这一档;而调用方要是跑测试的自己,
        这一下 killpg 会把测试进程一起带走。
        """
        import tempfile
        root = str(Path(__file__).resolve().parent.parent)
        node_pid = chrome_pid = None
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                probe = _write_probe(tmp, "hang.mjs", HANG)
                pidfile = tmp / "pids.txt"
                caller = tmp / "caller.py"
                # 一个最小的调用方:只做一件事 —— 用 run_probe 起探针,然后等着。
                caller.write_text(
                    "import sys\n"
                    f"sys.path.insert(0, {root!r})\n"
                    "from tests.headless import NODE, probe_env, run_probe\n"
                    "try:\n"
                    "    run_probe([NODE, sys.argv[1]], timeout=600, env=probe_env())\n"
                    "except BaseException:\n"
                    "    pass\n", encoding="utf-8")
                env = {**probe_env(), "PROBE_PID_FILE": str(pidfile)}
                # 调用方自己也开一个新会话:它的 pgid 于是等于它的 pid,下面那一下
                # killpg 只打到它,不会波及跑测试的这个进程。
                with subprocess.Popen(
                        [sys.executable, str(caller), str(probe)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=env, start_new_session=True) as caller_proc:
                    node_pid, chrome_pid = _wait_pidfile(pidfile)
                    self.assertTrue(_alive(chrome_pid), "摆局没成:浏览器压根没起来")
                    os.killpg(caller_proc.pid, signal.SIGTERM)
                    caller_proc.wait(timeout=30)
                self.assertEqual(
                    -signal.SIGTERM, caller_proc.returncode,
                    "摆局没成:调用方不是被这一下组取消收场的,后面那两条就不作数了")
                self.assertTrue(
                    _wait_gone(chrome_pid),
                    f"调用方被取消之后 chrome {chrome_pid} 还活着 —— 它已经没有任何"
                    "调用方在等结果了,只会一直占着内存和那个 DevTools 端口")
                self.assertTrue(
                    _wait_gone(node_pid),
                    f"调用方被取消之后 node {node_pid} 还活着(ppid 已经是 1)")
        finally:
            # 这条用例自己不许留下孤儿:断言红了也要把这一轮起的东西收掉。
            for pid in (chrome_pid, node_pid):
                if pid and _alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass


class ProbeSourceContractTests(unittest.TestCase):
    """口径只有一份:探针不许再自己 spawn 浏览器,测试不许再裸 subprocess.run 探针。

    这两条不需要浏览器,所以不挂 @requires_headless —— 没装无头壳的机器上它们
    照样是有效的回归(上面那几条 skip 掉之后,靠它们守住结构)。
    """

    def test_no_probe_spawns_the_shell_itself(self):
        offenders = []
        for f in sorted(HERE.glob("*.mjs")):
            if f.name == "probe_shell.mjs":
                continue
            s = f.read_text(encoding="utf-8")
            if "HEADLESS_SHELL" in s and "spawn(" in s and "launchShell" not in s:
                offenders.append(f.name)
        self.assertEqual([], offenders,
                         "这些探针又自己 spawn 浏览器了,收尾就不归 probe_shell 管了")

    def test_no_test_runs_a_browser_probe_bare(self):
        offenders = []
        for f in sorted(HERE.glob("test_*.py")):
            # 本文件是唯一的例外,而且必须是:上面第一层那几条验的正是**探针自己**
            # 退出时收不收得干净,走 run_probe 的话它那道 killpg 会把结论盖掉 ——
            # 被测的实现坏掉了也照样全绿。
            if f.name == Path(__file__).name:
                continue
            s = f.read_text(encoding="utf-8")
            if "probe_env()" not in s:
                continue
            for i, line in enumerate(s.splitlines(), 1):
                if "subprocess.run(" in line and "run_probe" not in line:
                    tail = "\n".join(s.splitlines()[i - 1:i + 3])
                    if "probe_env()" in tail:
                        offenders.append(f"{f.name}:{i}")
        self.assertEqual(
            [], offenders,
            "这些地方又裸 subprocess.run 跑探针了 —— 它的 timeout 是 SIGKILL,"
            "探针起的浏览器会被漏在外面,要走 tests.headless.run_probe")


if __name__ == "__main__":
    unittest.main()
