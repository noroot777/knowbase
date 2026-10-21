# -*- coding: utf-8 -*-
"""夹具自退的**后果**:真起一个进程,断言「它还在不在」「那份根还在不在」。

判据、凭据、两条适配路那几层在 `tests/test_fixture_life.py`。放在两份文件里是因为
加起来过了 700 行的上限;共用的小工具在 `tests/fixture_life_probe.py`。

`tests/test_probe_contract.py` 的「谁可以自己起沙箱服务」对**这一份**开了例外,而且
必须开:这里有一条验的正是**夹具自己**退出时删不删得掉那份根,所以不能给
`KB_FIXTURE_ROOT` —— 走 `fixture_server` 的话根归父进程,它的 `finally` 会把结论盖掉,
收场信号改错了也照样全绿。
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
import urllib.error
import urllib.request

from tests import fixture_life
from tests.fixture_life_probe import (   # noqa: F401 - 见那边的说明
    _shut_fds, _stat_of, _open_fds, _free_port, _alive, _wait_gone, _ping, _wait_listening, _owner_pipe, _identity, _reset_state, _age, _closed,
    ROOT,
)


class 后果(unittest.TestCase):
    """真起一个夹具,断言的是「那个进程还在不在」。"""

    def _spawn(self, *, idle: str, upstream: str = "", stderr=subprocess.DEVNULL):
        """直接起一个 `serve_workspace.py`(手起这一档:没有 owner 凭据)。"""
        port = _free_port()
        env = {**os.environ, fixture_life.IDLE_ENV: idle}
        env.pop(fixture_life.OWNER_ENV, None)
        argv = [sys.executable, "tests/serve_workspace.py", "--port", str(port)]
        if upstream:
            argv += ["--upstream", upstream]
        proc = subprocess.Popen(argv, cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=stderr)
        self.addCleanup(self._reap, proc)
        _wait_listening(port, proc)
        return port, proc

    def _spawn_via_middle(self, *, idle: str, owned: bool,
                          nonblocking: bool = False):
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
            "pass_fds = ()",
            "if owned:",
            "    r, w = os.pipe()",
            "    os.write(w, b'tok0123456789abcd')",
            "    if nonblocking:",
            "        os.set_blocking(r, False)",
            "    env[key] = '%d:tok0123456789abcd' % r",
            "    pass_fds = (r,)",
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
        grandchild = int(middle.stdout.readline().strip())
        self.addCleanup(self._reap_pid, grandchild, _identity(grandchild))
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

    def _reap_pid(self, pid: int, started: str) -> None:
        """按**这一轮那个进程的身份**收 —— `started` 是起它的时候记下的 `_identity`。

        身份对不上就一个字都不碰:要么它已经退了,要么这个号上现在是别人。只比脚本名
        不够(另一轮的同型夹具也叫这个名字),只比 pid 不够(号会被回收)。
        """
        if not started or _identity(pid) != started:
            return
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

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

    def test_空闲到点真的会退(self):
        port, proc = self._spawn(idle="2")
        self.assertTrue(_wait_gone(proc, 25), "空闲够久了,夹具应该自己退掉")
        self.assertFalse(self._port_open(port), "退了就不该还占着端口")

    def test_一直有请求就不退(self):
        """反过来钉一边 —— 顺带钉住在途计数真的接在请求上了。"""
        port, proc = self._spawn(idle="2")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _ping(port)
            time.sleep(0.4)
        self.assertTrue(_alive(proc), "一直有人用,不该被空闲超时收掉")

    def test_关掉之后放着也不退(self):
        """`KB_FIXTURE_IDLE=0` 是手动调试页面的人的出口,必须真的关得掉。"""
        _, proc = self._spawn(idle="0")
        self.assertFalse(_wait_gone(proc, 4), "关掉了空闲超时就不该自己退")

    def test_在途的慢请求不会被砍断(self):
        """**这条钉的是「空闲」的定义**:只在进门时续一次命的话,读上游那几秒全被
        算成空闲,一个合法的慢请求会被拦腰砍断、客户端拿到 502。
        """
        import http.server
        import threading

        class Slow(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(4)
                self.send_response(200)
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):
                    self.wfile.write(b"finished")

            def log_message(self, *a):
                pass

        up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        self.addCleanup(up.server_close)
        self.addCleanup(up.shutdown)
        port, proc = self._spawn(
            idle="1", upstream=f"http://127.0.0.1:{up.server_port}")
        began = time.monotonic()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/slow",
                                    timeout=15) as r:
            body = r.read()
        self.assertEqual(body, b"finished", "在途的慢请求不该被砍断")
        self.assertGreater(time.monotonic() - began, 3, "这一条要真的等满上游那几秒")
        self.assertTrue(_alive(proc), "请求刚回完,不该已经退了")

    def test_日志堵住也照退(self):
        """**日志不许挡住防线。** 没人消费 stderr 时那根管道会满,而满管道上的 write
        是阻塞不是异常:实测它能让退出路径卡在发信号之前(连硬退都到不了),也能让
        请求线程永远停在「在途」上,把空闲超时一并堵死。
        """
        port, proc = self._spawn(idle="1", stderr=subprocess.PIPE)
        clients = []
        try:
            for n in range(6):           # 灌满 stderr:长地址 → 长访问日志
                c = socket.create_connection(("127.0.0.1", port), timeout=2)
                c.sendall((f"GET /missing-{n}-" + "x" * 60000
                           + " HTTP/1.0\r\nHost: l\r\n\r\n").encode())
                clients.append(c)
            self.assertTrue(_wait_gone(proc, 20),
                            "stderr 堵着也必须退得掉 —— 日志是可丢弃的,防线不是")
        finally:
            for c in clients:
                with contextlib.suppress(OSError):
                    c.close()
            with contextlib.suppress(Exception):
                proc.stderr.close()

    def test_自己退的时候把临时根删掉(self):
        """**这条钉的是收场信号选得对不对。**

        发 SIGTERM 的话 Python 没有 handler 就直接结束进程,`main()` 里那个
        `with contextlib.ExitStack()` 一行不跑,盘上留下一份完整的沙箱库 —— 那正是
        `tests/fixture_root.py` 记载的事故形态。手动跑这一档根归夹具自己删。

        **用自己独占的 TMPDIR**:拿全局前缀做差集的话,并行跑的别的测试新建同前缀
        目录就会让这条误报。
        """
        sandbox = tempfile.mkdtemp(prefix="kb-life-tmp-")
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        port = _free_port()
        env = {**os.environ, fixture_life.IDLE_ENV: "3", "TMPDIR": sandbox}
        env.pop(fixture_life.OWNER_ENV, None)
        env.pop("KB_FIXTURE_ROOT", None)          # 手动跑:根归它自己
        proc = subprocess.Popen(
            [sys.executable, "tests/serve_sources.py", "--port", str(port)],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap, proc)
        _wait_listening(port, proc, timeout=120)
        self.assertTrue(os.listdir(sandbox), "这一轮的根本来就该建在我们给的 TMPDIR 里")
        self.assertTrue(_wait_gone(proc, 40), "空闲够久了,夹具应该自己退掉")
        time.sleep(1)
        self.assertEqual(os.listdir(sandbox), [],
                         "自己退的时候得把临时根删掉,不能留一份沙箱库在盘上")

    # ---- 所有权那一道 ----

    def test_调用方被强杀夹具自己退(self):
        """`finally` 里的 reap 一行不跑那一档 —— 全靠夹具自己读到那根 pipe 的 EOF。"""
        _, middle, grandchild = self._spawn_via_middle(idle="0", owned=True)
        middle.kill()
        middle.wait(timeout=5)
        self.assertTrue(self._wait_dead(grandchild, 15),
                        "调用方没了,夹具应该认出 EOF 自己退")

    def test_调用方死在摆盘途中夹具也不开服务(self):
        """**这条是凭据从 pid 换成 pipe 的理由。**

        夹具要先建库、导出、建索引才开服务,调用方死在那几十秒里时它读到的 ppid 已经
        是 1 —— 拿「现在的 ppid 还等于环境里那个 pid 吗」判凭据可不可信,会把**正确
        的**凭据当成继承来的陈旧值丢掉,副防线整个不装。实测过:真调用方被 SIGKILL,
        夹具照样从暂停点继续开服务、还能访问。

        这里用 SIGSTOP 把夹具按在模块加载**之前**(那是最早的时刻),杀掉中间进程,再
        放它走 —— 它应该一加载就发现 EOF,一个端口都不开。
        """
        port = _free_port()
        wrapper = ("import os,runpy,signal,sys;os.kill(os.getpid(),signal.SIGSTOP);"
                   "sys.argv=sys.argv[1:];"
                   "runpy.run_path(sys.argv[0],run_name='__main__')")
        code = "\n".join([
            "import os, subprocess, sys, time",
            "env = dict(os.environ)",
            f"key = {fixture_life.OWNER_ENV!r}",
            "r, w = os.pipe()",
            "os.write(w, b'tok0123456789abcd')",
            "env[key] = '%d:tok0123456789abcd' % r",
            f"p = subprocess.Popen([sys.executable, '-c', {wrapper!r},"
            f" 'tests/serve_workspace.py', '--port', '{port}'],"
            " env=env, pass_fds=(r,),"
            " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
            "print(p.pid, flush=True)",
            "time.sleep(600)",
        ])
        env = {**os.environ, fixture_life.IDLE_ENV: "0"}   # 单验 owner 这一道
        env.pop(fixture_life.OWNER_ENV, None)
        middle = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self._reap, middle)
        self.addCleanup(middle.stdout.close)
        child = int(middle.stdout.readline().strip())
        self.addCleanup(self._reap_stopped, child, _identity(child))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(child)],
                                   capture_output=True, text=True).stdout
            if "T" in state:
                break
            time.sleep(0.1)
        else:
            self.fail("夹具没有在加载之前被按住,这条用例没验到那个窗口")
        middle.kill()
        middle.wait(timeout=5)
        os.kill(child, signal.SIGCONT)
        self.assertTrue(self._wait_dead(child, 15),
                        "调用方已经没了,放它走之后不该再开服务")
        self.assertFalse(self._port_open(port), "一个端口都不该开出来")

    def _reap_stopped(self, pid: int, started: str) -> None:
        """被 SIGSTOP 按住的那个:**先核验身份,再 SIGCONT**。

        SIGCONT 也是信号。上一版为了「先放它走才收得动」把它发在核验之前,那等于对一个
        还没认过的 pid 动手 —— 号被回收之后唤醒的是别人的进程。发信号的每一条分支都只
        许作用在核验过的那个身份上,一条都不例外。
        """
        if not started or _identity(pid) != started:
            return
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGCONT)
            os.kill(pid, signal.SIGKILL)

    def test_日志堵住时正常请求照旧拿得到响应(self):
        """**上面那条只验了「退得掉」,这条验「别的请求没被它带走」。**

        把访问日志交还给 `http.server` 原来的实现(写 `sys.stderr`)之后,fd 2 已经被设成
        非阻塞,于是那次 write 不再是卡住而是抛 `BlockingIOError` —— 异常把请求线程打断,
        实测一个毫无关系的正常请求**连响应行都收不到,对端直接把连接关了**。
        退得掉和回得出是两件事,少钉一边就看不出这个差别。
        """
        port, proc = self._spawn(idle="0", stderr=subprocess.PIPE)
        clients = []
        try:
            for n in range(6):
                c = socket.create_connection(("127.0.0.1", port), timeout=2)
                c.sendall((f"GET /missing-{n}-" + "x" * 60000
                           + " HTTP/1.0\r\nHost: l\r\n\r\n").encode())
                clients.append(c)
            time.sleep(2)                 # 等那几条把管道灌满
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            clients.append(c)
            c.sendall(b"GET / HTTP/1.0\r\nHost: l\r\n\r\n")
            self.assertIn(b"200", c.recv(200).split(b"\r\n")[0],
                          "日志丢得起,正在服务的请求丢不起")
        finally:
            for c in clients:
                with contextlib.suppress(OSError):
                    c.close()
            with contextlib.suppress(Exception):
                proc.stderr.close()

    def test_不改调用方共享的stderr状态(self):
        """**第 2 条的后果。** 手起夹具继承的 stderr 跟调用方是**同一个**
        open-file-description,`O_NONBLOCK` 挂在那上面 —— 夹具把 fd 2 切成非阻塞,
        调用方和一切共享者一起看得见。实测调用方自己什么都没做,它的 `write` 开始抛
        `BlockingIOError`、输出缺行。`dup` 也隔不开:复制出来的 fd 指的还是那一份。
        """
        r, w = os.pipe()
        self.addCleanup(_shut_fds, r, w)
        self.assertTrue(os.get_blocking(w), "先确认起点是阻塞的")
        port, proc = self._spawn(idle="1", stderr=w)   # 让夹具把它当 stderr 继承
        for n in range(6):                             # 灌满,逼它走丢日志那条路
            with contextlib.suppress(OSError):
                c = socket.create_connection(("127.0.0.1", port), timeout=2)
                c.sendall((f"GET /missing-{n}-" + "x" * 60000
                           + " HTTP/1.0\r\nHost: l\r\n\r\n").encode())
                self.addCleanup(c.close)
        self.assertTrue(_wait_gone(proc, 20), "日志堵住也必须退得掉")
        self.assertTrue(os.get_blocking(w),
                        "调用方那一端的模式一个比特都不许动")

    def test_凭据是非阻塞的也不许当成没有调用方(self):
        """既然不碰它的模式,那就得受得住一个非阻塞的凭据。

        阻塞读在这种 fd 上当场拿到 `BlockingIOError` —— 它是 `OSError` 的子类,会被
        看门狗那个 `except` 吞成「写端关了」,夹具刚起来就自己退掉。所以等可读走
        `select`。
        """
        port, middle, grandchild = self._spawn_via_middle(idle="0", owned=True,
                                                          nonblocking=True)
        time.sleep(3)                     # 远超 POLL_SECONDS
        self.assertFalse(self._wait_dead(grandchild, 0.1),
                         "调用方还活着,不该把非阻塞的凭据当成 EOF")
        self.assertTrue(self._port_open(port), "它还该好好地服务着")
        middle.kill()
        middle.wait(timeout=5)
        self.assertTrue(self._wait_dead(grandchild, 15),
                        "反过来钉一边:调用方真没了还得认得出来")

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

    def test_起夹具失败不漏文件描述符(self):
        """**第 4 条的后果。** 凭据那根管道是裸 fd:写 token、拼 env、`Popen` 任何一步
        抛都到不了 `finally`,没有 Python 对象替它收。最难受的是「快到打开文件上限了」
        这种失败 —— 失败本身还在继续吃掉本该释放的资源,重试越多越快撞墙。
        """
        from tests import headless

        def boom(*a, **k):
            raise OSError(24, "Too many open files")

        before = _open_fds()
        real = headless.subprocess.Popen
        headless.subprocess.Popen = boom
        try:
            for _ in range(8):
                with self.assertRaises(OSError):
                    with headless.fixture_server(script="serve_workspace.py"):
                        pass
        finally:
            headless.subprocess.Popen = real
        leaked = _open_fds() - before
        self.assertLessEqual(leaked, 1,
                             f"起了 8 次都失败,漏了 {leaked} 个 fd(每次两个就是 16)")

    def test_手起的夹具父进程退了也不退(self):
        """**这条是整套设计的核心**:会话手起夹具、起它那个 shell 当场就退,随后另
        一个 shell 才跑探针。拿「父进程没了」当判据的话,夹具一起跑就自杀。
        """
        port, middle, grandchild = self._spawn_via_middle(idle="0", owned=False)
        middle.kill()
        middle.wait(timeout=5)
        time.sleep(3)                     # 远超 POLL_SECONDS,真要自杀早该退了
        self.assertFalse(self._wait_dead(grandchild, 0.1),
                         "没有 owner 时不该凭父进程的死活自杀")
        self.assertTrue(self._port_open(port), "它还该好好地服务着")

    def test_真的fixture_server给得出可用的凭据(self):
        """**两侧各有自己的红**:上面那条验夹具认不认 EOF,这条验起它的那一侧确实
        把凭据发了出来 —— 中间那个进程用的是真的 `fixture_server()`。
        """
        code = "\n".join([
            "import time",
            "from tests import headless",
            "with headless.fixture_server(script='serve_workspace.py',"
            " timeout=60) as base:",
            "    print(base, base.pid, flush=True)",
            "    time.sleep(600)",
        ])
        env = {**os.environ, fixture_life.IDLE_ENV: "0"}   # 单验 owner 这一道
        middle = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self._reap, middle)
        self.addCleanup(middle.stdout.close)
        line = middle.stdout.readline().strip()
        base, _, raw_pid = line.partition(" ")
        self.assertTrue(base.startswith("http://"), f"没拿到 base URL:{line!r}")
        port = int(base.rsplit(":", 1)[1])
        # **按它交回来的 pid 收,不按端口找。** 端口不是所有权凭据(见 `_identity`)。
        grandchild = int(raw_pid)
        self.addCleanup(self._reap_pid, grandchild, _identity(grandchild))
        middle.kill()
        middle.wait(timeout=5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self._port_open(port):
            time.sleep(0.2)
        self.assertFalse(self._port_open(port),
                         "真 fixture_server 起的夹具也该认出调用方没了")



if __name__ == "__main__":
    unittest.main()
