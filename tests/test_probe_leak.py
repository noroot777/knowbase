# -*- coding: utf-8 -*-
"""浏览器探针起的无头壳,必须在任何一条退出路径上都被收掉。

2026-10-09 在本机实测到 16 个泄漏的 chrome-headless-shell:存活 6 小时到 1 天
11 小时,连同渲染子进程共 66 个进程、3.8 GB,全部 ppid=1、0 条活跃 CDP 连接、
而且当时一个测试都没在跑。来源是 12 个探针共有的那个结构 —— `spawn` 在开头、
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

③ 的**取消时机有两档,各有自己的用例**:浏览器已经起好之后取消,靠那个轮询;
而**起浏览器之前**取消(探针本来就要先等沙箱后端监听、先摆局)——那时现读的 ppid
已经是 1,把它当成「没有调用方、不用盯」就等于永久跳过检查,随后照旧起一个浏览器
留在后台。所以期望的调用方由 `run_probe` 用 `KB_PROBE_PARENT` 明确传下去,而且起
任何长寿东西之前先问一次。

判据一律是**后果**:那个 pid 还在不在。只验「注册了钩子没有」的话,一个注册完
却杀不掉它的实现照样全绿。

同一个所有权问题的另一半是**目录**(浏览器 profile、沙箱那份临时库):那几条靠源码
结构钉,在 `tests/test_probe_contract.py`。
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

from tests.headless import (  # noqa: E402
    NODE, PARENT_ENV, probe_env, requires_headless, run_probe)

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


def _wait_file(path: Path, timeout: float = 90.0) -> dict[str, str]:
    """等探针把一份 `k=v k=v` 的标记写到盘上;等不到就是它压根没走到那一步。

    走盘而不走 stdout:取消那两条用例的调用方是另一个 Python 进程,探针的 stdout
    被它的 `run_probe` 收进管道、外面读不到 —— 而那几条恰恰要在调用方**死掉之后**
    去查这几个 pid 还在不在。
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            got = dict(kv.split("=") for kv in path.read_text().split() if "=" in kv)
            if got:
                return got
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"没在 {timeout} 秒内等到 {path}")


# 「真的挂住」只能靠一个 **ref 的** handle。`await new Promise(() => {})` 挂不住:
# 它不是 handle,event loop 一空 node 就以 exit 13 正常退出("unsettled top-level
# await")—— 那时第一层的 'exit' 钩子把浏览器收掉了,于是**本该验第二层的用例测的
# 是第一层**,去掉 run_probe 里两处 killpg 照样全绿(实测注入 0 红就是这么来的)。
HANG = "await new Promise(r => setTimeout(r, 600000));"


# 起浏览器**之前**那一段异步:真探针在这儿等沙箱后端监听、摆局、读 fixture
# (`x_channel_probe.mjs` 就是先等本地后端、再 launchShell)。这个 interval 是
# **ref 的**,所以 node 会真的在这儿等着 —— 调用方死在这一段里正是那个启动窗口。
# 同一个屏障,但等的方式是**同步**的:node 是单线程的,这期间那个 400ms 的轮询一次
# 都跑不起来 —— 起浏览器之前那段工作只要是同步的(`execSync` 起个辅助进程、读一份大
# fixture),唯一救得了它的就是「起之前先问一次」。`execSync('sleep')` 而不是忙等:
# 忙等同样阻塞事件循环,但会把一个核吃满。
SYNC_BARRIER = '''\
writeFileSync(process.env.PROBE_READY_FILE, "node=" + process.pid + "\\n");
while (!existsSync(process.env.PROBE_GO_FILE)) { execSync("sleep 0.05"); }
'''


BARRIER = '''\
writeFileSync(process.env.PROBE_READY_FILE, "node=" + process.pid + "\\n");
await new Promise(res => {
  const t = setInterval(() => {
    if (existsSync(process.env.PROBE_GO_FILE)) { clearInterval(t); res(); }
  }, 50);
});
'''


def _write_probe(tmp: Path, name: str, body: str, *, before: str = "",
                 args: tuple[str, ...] = ("--window-size=800,600",)) -> Path:
    """写一个用公共模块起浏览器的最小探针,把 chrome 的 pid 打在 stdout 上。

    设了 `PROBE_PID_FILE` 就顺带写进盘里(理由见 `_wait_file`)。
    `before` 是「起浏览器之前先做点别的」那一段(见 BARRIER);`args` 是交给
    `launchShell` 的参数 —— 用 `--user-data-dir` 可以反过来验「有没有起过浏览器」:
    chrome 一启动就把那个目录建出来,它不存在就是**一个都没起过**。
    """
    p = tmp / name
    arglist = ", ".join(f'"{a}"' for a in args)
    p.write_text(textwrap.dedent(f'''\
        import {{ writeFileSync, existsSync }} from "node:fs";
        import {{ execSync }} from "node:child_process";
        import {{ launchShell }} from "{HERE / "probe_shell.mjs"}";
    ''') + before + textwrap.dedent(f'''\
        const {{ proc }} = await launchShell([{arglist}]);
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

    def test_a_temporary_profile_is_removed_on_the_throwing_path_too(self):
        """**profile 目录跟浏览器同一条口径:谁都不许留在盘上。**

        原来是各探针自己 `mkdtempSync` + `finally` 里 `rmSync`:建目录、起浏览器、
        连 CDP 这几步**在 try 之前**,那一段抛异常时 finally 一行都不跑,目录就留着
        (第 5 轮按代码指出;另一处实测的沙箱库残留是同一个所有权问题)。所以改成
        `launchShell({profile: 前缀})` 自己建、挂在同一个 'exit' 钩子上删。

        判据是后果:**那个目录还在不在**。
        """
        import tempfile
        lines = [
            'import { launchShell } from "%s";' % (HERE / "probe_shell.mjs"),
            'const { proc, profile } = await launchShell(["--window-size=800,600"],',
            '                                           { profile: "kb-leak-probe-" });',
            'process.stdout.write("CHROME_PID=" + proc.pid + "\\n");',
            'process.stdout.write("PROFILE=" + profile + "\\n");',
            'throw new Error("模拟一条检查失败");',
        ]
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "profile.mjs"
            probe.write_text("\n".join(lines) + "\n", encoding="utf-8")
            r = subprocess.run([NODE, str(probe)], capture_output=True, text=True,
                               timeout=90, env=probe_env())
        found = [l.split("=", 1)[1] for l in r.stdout.splitlines()
                 if l.startswith("PROFILE=")]
        self.assertTrue(found, f"探针没报出 profile 路径\nstdout={r.stdout}\nstderr={r.stderr}")
        self.assertNotIn(found[0], ("", "null"), found)
        for _ in range(50):          # 钩子是同步的,但进程退出本身要一点时间
            if not Path(found[0]).exists():
                break
            time.sleep(0.1)
        self.assertFalse(Path(found[0]).exists(),
                         f"探针抛异常退出之后 profile 目录 {found[0]} 还留着")
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


def _write_caller(tmp: Path) -> Path:
    """一个最小的调用方:只做一件事 —— 用 `run_probe` 起探针,然后等着。

    **必须是另一个进程**:这几条用例要取消它的**整个进程组**,而调用方要是跑测试的
    自己,那一下 killpg 会把测试进程一起带走;只对 node 发信号又走的是 ① 那条路
    (它的 handler 接得住),验不出这一档。
    """
    root = str(Path(__file__).resolve().parent.parent)
    caller = tmp / "caller.py"
    caller.write_text(
        "import sys\n"
        f"sys.path.insert(0, {root!r})\n"
        "from tests.headless import NODE, probe_env, run_probe\n"
        "try:\n"
        "    run_probe([NODE, sys.argv[1]], timeout=600, env=probe_env())\n"
        "except BaseException:\n"
        "    pass\n", encoding="utf-8")
    return caller


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
        node_pid = chrome_pid = None
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                probe = _write_probe(tmp, "hang.mjs", HANG)
                pidfile = tmp / "pids.txt"
                caller = _write_caller(tmp)
                env = {**probe_env(), "PROBE_PID_FILE": str(pidfile)}
                # 调用方自己也开一个新会话:它的 pgid 于是等于它的 pid,下面那一下
                # killpg 只打到它,不会波及跑测试的这个进程。
                with subprocess.Popen(
                        [sys.executable, str(caller), str(probe)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=env, start_new_session=True) as caller_proc:
                    pids = _wait_file(pidfile)
                    node_pid, chrome_pid = int(pids["node"]), int(pids["chrome"])
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

    def test_canceling_the_caller_before_launch_starts_no_browser(self):
        """起浏览器**之前**取消调用方 —— 这一档不许再起浏览器,也不许留下 node。

        真探针在 `launchShell` 之前本来就有一段异步:`x_channel_probe.mjs` 先等本地
        后端监听,再起浏览器。调用方死在那一段里时,现读的 `process.ppid` 已经是 1,
        所以**不能**把它当成「本来就没有调用方、不用盯」—— 那等于永久跳过检查。
        实测修之前:调用方以 -15 退出,node(ppid=1)照旧走完那段异步、起出 chrome、
        DevTools 端口持续监听,3 秒后(远超 400ms 轮询间隔)三样都还在 —— 不是检测
        慢,是检查压根没装。

        **判据是后果,而且「一个都没起过」是确定性的**:屏障放行排在取消之后,
        所以坏实现一定会在那一刻真起一个浏览器 —— 而 `--user-data-dir` 那个目录
        chrome 一启动就建出来,它不存在就是**压根没起过**。光靠 400ms 那个轮询
        去追这条路会是一条看运气的用例,这正是「起之前先问一次」存在的理由。
        """
        import tempfile
        node_pid = None
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                profile = tmp / "profile"
                probe = _write_probe(tmp, "wait.mjs", HANG, before=BARRIER,
                                     args=(f"--user-data-dir={profile}",))
                pidfile, ready, go = tmp / "pids.txt", tmp / "ready.txt", tmp / "go.txt"
                caller = _write_caller(tmp)
                env = {**probe_env(), "PROBE_PID_FILE": str(pidfile),
                       "PROBE_READY_FILE": str(ready), "PROBE_GO_FILE": str(go)}
                with subprocess.Popen(
                        [sys.executable, str(caller), str(probe)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=env, start_new_session=True) as caller_proc:
                    node_pid = int(_wait_file(ready)["node"])
                    self.assertFalse(
                        profile.exists(),
                        "摆局没成:浏览器在取消之前就起来了,这就不是启动窗口那一档了")
                    os.killpg(caller_proc.pid, signal.SIGTERM)
                    caller_proc.wait(timeout=30)
                self.assertEqual(
                    -signal.SIGTERM, caller_proc.returncode,
                    "摆局没成:调用方不是被这一下组取消收场的,后面那几条就不作数了")
                go.write_text("go", encoding="utf-8")   # 放行:坏实现在这一刻起浏览器
                self.assertTrue(
                    _wait_gone(node_pid),
                    f"调用方被取消之后 node {node_pid} 还活着(ppid 已经是 1),"
                    "它已经没有任何调用方在等结果了")
                self.assertFalse(
                    profile.exists(),
                    "调用方已经没了,探针还起了一个浏览器 —— 那就是端口面板上多出来"
                    "的那一条,而且再没人来收它")
                self.assertFalse(
                    pidfile.exists(),
                    f"launchShell 在没有调用方的情况下返回了(pidfile={pidfile})")
        finally:
            # 这条用例自己不许留下孤儿:断言红了也要把这一轮起的东西收掉。
            if node_pid and _alive(node_pid):
                try:
                    os.killpg(node_pid, signal.SIGKILL)   # node 是它那个组的 leader
                except OSError:
                    pass

    def test_canceling_the_caller_ends_the_probe_mid_startup(self):
        """同一个局,但**不放行**那段异步:探针自己就得结束,不许一直等下去。

        **这一条和上一条各有自己的射程,少哪一道红哪一条**:
        上一条放行了屏障,所以靠的是「起浏览器之前先问一次」那道闸;这一条压根不放行
        —— 真探针等的是沙箱后端起来,调用方被取消之后那件事可能永远不会发生,于是只有
        **模块一加载就装上的**那个轮询救得了它。不装的话 node 一直挂在那段异步里:
        没有浏览器可言,但那个进程连同它那几百 MB 的驻留集谁也不收。
        """
        import tempfile
        node_pid = None
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                profile = tmp / "profile"
                probe = _write_probe(tmp, "wait2.mjs", HANG, before=BARRIER,
                                     args=(f"--user-data-dir={profile}",))
                ready, go = tmp / "ready.txt", tmp / "go.txt"
                caller = _write_caller(tmp)
                env = {**probe_env(), "PROBE_READY_FILE": str(ready),
                       "PROBE_GO_FILE": str(go)}       # 这个 go 永远不会被写出来
                with subprocess.Popen(
                        [sys.executable, str(caller), str(probe)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=env, start_new_session=True) as caller_proc:
                    node_pid = int(_wait_file(ready)["node"])
                    os.killpg(caller_proc.pid, signal.SIGTERM)
                    caller_proc.wait(timeout=30)
                self.assertEqual(
                    -signal.SIGTERM, caller_proc.returncode,
                    "摆局没成:调用方不是被这一下组取消收场的,后面那条就不作数了")
                self.assertTrue(
                    _wait_gone(node_pid),
                    f"调用方被取消之后 node {node_pid} 还挂在起浏览器之前那段异步里 ——"
                    "它等的那件事(沙箱后端起来)可能永远不会发生,而没有谁在等结果了")
                self.assertFalse(profile.exists(), "居然还起了一个浏览器")
        finally:
            if node_pid and _alive(node_pid):
                try:
                    os.killpg(node_pid, signal.SIGKILL)
                except OSError:
                    pass

    def test_sync_startup_still_starts_no_browser_after_cancel(self):
        """同一个局,但那段异步换成**同步**的 —— 轮询跑不起来,只剩起之前那一问。

        **这是「起浏览器之前先问一次」那道闸独占的射程**:node 是单线程的,探针在起
        浏览器之前做一件同步的事(`execSync` 起个辅助进程、读一份大 fixture)时,那个
        400ms 的轮询**一次都跑不起来**;同步段一结束就直接进 `launchShell`,中间没有
        任何让定时器插进来的机会。去掉那一问就是:取消之后照样起出一个浏览器(随后
        才被轮询收掉),而「起过」这件事本身就是端口面板上多出来的那一条。
        今天 12 个探针那段都是异步的,所以这一条钉的是**合约**,不是某个探针的复现。
        """
        import tempfile
        node_pid = None
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                profile = tmp / "profile"
                probe = _write_probe(tmp, "sync.mjs", HANG, before=SYNC_BARRIER,
                                     args=(f"--user-data-dir={profile}",))
                ready, go = tmp / "ready.txt", tmp / "go.txt"
                caller = _write_caller(tmp)
                env = {**probe_env(), "PROBE_READY_FILE": str(ready),
                       "PROBE_GO_FILE": str(go)}
                with subprocess.Popen(
                        [sys.executable, str(caller), str(probe)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=env, start_new_session=True) as caller_proc:
                    node_pid = int(_wait_file(ready)["node"])
                    os.killpg(caller_proc.pid, signal.SIGTERM)
                    caller_proc.wait(timeout=30)
                self.assertEqual(-signal.SIGTERM, caller_proc.returncode, "摆局没成")
                go.write_text("go", encoding="utf-8")   # 放行:同步段结束,直奔 spawn
                # 顺序和上一条一样:**先**等 node 收场,再问「起过浏览器没有」。
                # 反过来写(放行完立刻查那个目录)是一条看运气的用例:坏实现也要先
                # spawn、chrome 再建那个目录,查得比它做得快就**任何**实现都答
                # 「还没起」—— 自查时就是这么写的,注入回去红 0 条。
                self.assertTrue(
                    _wait_gone(node_pid),
                    f"调用方被取消之后 node {node_pid} 还活着(ppid 已经是 1)")
                self.assertFalse(
                    profile.exists(),
                    "调用方已经没了,探针还是起了一个浏览器 —— 那就是端口面板上多出"
                    "来的那一条")
        finally:
            if node_pid and _alive(node_pid):
                try:
                    os.killpg(node_pid, signal.SIGKILL)
                except OSError:
                    pass

    def test_launch_shell_refuses_when_the_caller_is_already_gone(self):
        """合约:原调用方已经不在了,`launchShell` 一个浏览器都不许起。

        上一条是真取消那条路;这一条把同一道闸单独钉住,而且**不靠任何时序** ——
        拿一个**已经退出**的进程的 pid 当「原调用方」,于是在 node 眼里
        「我已经不是它的子进程了」和真被取消逐字相同。
        """
        import tempfile
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait(timeout=30)
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            profile = tmp / "profile"
            probe = _write_probe(tmp, "orphan.mjs", "", args=(f"--user-data-dir={profile}",))
            env = {**probe_env(), PARENT_ENV: str(dead.pid)}
            # 这里也必须是裸 subprocess.run:run_probe 会把 PARENT_ENV 覆盖成自己的 pid
            # (它是唯一的写入方),那就把这条用例要摆的局顶掉了。
            r = subprocess.run([NODE, str(probe)], capture_output=True, text=True,
                               timeout=90, env=env)
        self.assertFalse(
            profile.exists(),
            "没有调用方在等结果,探针照样起了一个浏览器\n"
            f"stdout={r.stdout}\nstderr={r.stderr}")
        self.assertNotIn("CHROME_PID=", r.stdout, "launchShell 居然返回了")
        self.assertEqual(143, r.returncode,
                         f"该以 143 收场(被取消的惯例)\nstderr={r.stderr}")


if __name__ == "__main__":
    unittest.main()
