# -*- coding: utf-8 -*-
"""夹具自己什么时候退 —— 判据一组,后果一组。

**后果是「那个进程还在不在」**,所以这里不满足于打 `should_exit` 的返回值:真起一个
`tests/serve_workspace.py`,等它自己退(或者钉住它不该退时确实还在)。判据那一组是
为了把边界铺开,后果那一组才是这件事本身。

**每一条该退的都配一条不该退的**:空闲到点要退 / 一直有请求不许退、在途没回完不许退;
丢了 owner 要退 / 没有 owner(手起、跨 shell 共用)不许退。后面那半边是这套设计的核心
—— 它一旦失守,会话就没法再手起夹具跑探针了。

**收尾只认有所有权证据的进程**:端口不是凭据(另一个本地服务可能正好占着这个号),
所以孙进程的 pid 由中间进程交回,按端口兜底那一道也要核验命令行连端口号一起对上。
"""

from __future__ import annotations

import contextlib
import http.server
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import fixture_life  # noqa: E402


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
    """造一份跟 `fixture_server()` 同形的凭据:(读端, 写端, 环境变量值)。"""
    r, w = os.pipe()
    os.write(w, token.encode())
    return r, w, f"{r}:{token}"


class 判据(unittest.TestCase):
    """`should_exit` 是唯一那份判据,边界都打在它身上。"""

    def setUp(self):
        fixture_life.touch()

    def test_空闲没到不退(self):
        self.assertEqual(fixture_life.should_exit(60, False, 0), "")

    def test_空闲到点要退(self):
        # 把时钟往回拨,别靠「门槛小到比两行代码还短」—— 那样用例对实现不敏感
        fixture_life._LAST = time.monotonic() - 10
        self.assertIn("没有请求", fixture_life.should_exit(1, False, 0))

    def test_空闲上限为零就永不因空闲退(self):
        """反过来钉一边:手动调试页面的人把它关掉之后,放着一天也不许自己退。"""
        fixture_life._LAST = time.monotonic() - 86400
        self.assertEqual(fixture_life.should_exit(0, False, 0), "")

    def test_有在途请求就不按空闲退(self):
        """只在进门时续一次命的话,一个慢请求跑到一半夹具就退了,客户端拿到 502。"""
        fixture_life._LAST = time.monotonic() - 86400
        self.assertEqual(fixture_life.should_exit(1, False, 1), "")

    def test_在途回完之后照旧会退(self):
        """反过来钉一边:在途归零就不该再拦着,否则这道防线等于没装。"""
        fixture_life._LAST = time.monotonic() - 86400
        self.assertIn("没有请求", fixture_life.should_exit(1, False, 0))

    def test_丢了owner立刻退且不等在途(self):
        """那一刻已经没人在等这个结果了,在途请求不该再拖着。"""
        self.assertIn("原调用方", fixture_life.should_exit(0, True, 7))

    def test_没有owner就不看它(self):
        """反过来钉一边:手起的夹具没有 owner,父 shell 退掉也不许自杀。"""
        self.assertEqual(fixture_life.should_exit(60, False, 0), "")


class 所有权凭据(unittest.TestCase):
    """`claim_owner()` 只认「确实是起我们那一位给的那根 pipe」。"""

    def setUp(self):
        self._saved = os.environ.get(fixture_life.OWNER_ENV)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop(fixture_life.OWNER_ENV, None)
        else:
            os.environ[fixture_life.OWNER_ENV] = self._saved

    def test_没给就是没有(self):
        os.environ.pop(fixture_life.OWNER_ENV, None)
        self.assertEqual(fixture_life.claim_owner(), -1)

    def test_给了真凭据就认(self):
        r, w, spec = _owner_pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (r, w)
                                 if not _closed(fd)])
        os.environ[fixture_life.OWNER_ENV] = spec
        self.assertEqual(fixture_life.claim_owner(), r)

    def test_token对不上不认(self):
        """**这是防陈旧继承值那一半**:环境变量能被继承,那根 pipe 不能。

        **顺带钉住「认领这一下不许阻塞」**:这里环境里那个 token 比管道里的长,
        第一版拿阻塞读循环去凑字数,读完已有数据之后就永远等下去 —— 而这一段跑在
        模块加载里,夹具会卡死在 import,一个字的日志都没有。
        """
        r, w, _ = _owner_pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (r, w)
                                 if not _closed(fd)])
        os.environ[fixture_life.OWNER_ENV] = f"{r}:别的token0000000"
        self.assertEqual(fixture_life.claim_owner(), -1)

    def test_不是管道就不认(self):
        """陈旧的 fd 号在当前进程里可能指着一个普通文件。"""
        handle = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(os.unlink, handle.name)
        self.addCleanup(handle.close)
        os.environ[fixture_life.OWNER_ENV] = f"{handle.fileno()}:tok0123456789abcd"
        self.assertEqual(fixture_life.claim_owner(), -1)

    def test_stdio的号不认(self):
        os.environ[fixture_life.OWNER_ENV] = "2:tok0123456789abcd"
        self.assertEqual(fixture_life.claim_owner(), -1)

    def test_垃圾值不认(self):
        for bad in ("", "不是数字:tok", "7", ":tok", "7:"):
            os.environ[fixture_life.OWNER_ENV] = bad
            self.assertEqual(fixture_life.claim_owner(), -1, bad)


def _closed(fd: int) -> bool:
    try:
        os.fstat(fd)
        return False
    except OSError:
        return True


class 空闲上限的读法(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(fixture_life.IDLE_ENV)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop(fixture_life.IDLE_ENV, None)
        else:
            os.environ[fixture_life.IDLE_ENV] = self._saved

    def test_没设用默认(self):
        os.environ.pop(fixture_life.IDLE_ENV, None)
        self.assertEqual(fixture_life.idle_limit(), fixture_life.IDLE_DEFAULT)

    def test_设零就是关掉(self):
        os.environ[fixture_life.IDLE_ENV] = "0"
        self.assertEqual(fixture_life.idle_limit(), 0)

    def test_负数按关掉办(self):
        os.environ[fixture_life.IDLE_ENV] = "-5"
        self.assertEqual(fixture_life.idle_limit(), 0)

    def test_垃圾值回默认(self):
        """判不准就回那个保守的默认值,不许变成「永不退出」。"""
        os.environ[fixture_life.IDLE_ENV] = "一刻钟"
        self.assertEqual(fixture_life.idle_limit(), fixture_life.IDLE_DEFAULT)

    def test_非有限值回默认(self):
        """`nan` 任何比较都是假、`inf` 永远不到点 —— 照单全收就是悄悄丢掉主防线。"""
        for bad in ("nan", "inf", "-inf", "Infinity"):
            os.environ[fixture_life.IDLE_ENV] = bad
            self.assertEqual(fixture_life.idle_limit(),
                             fixture_life.IDLE_DEFAULT, bad)


class ASGI那条路(unittest.TestCase):
    """7 个 uvicorn 夹具的在途走 `guard`,`serve_workspace` 走 Mixin —— 两份实现
    各有自己的红。后果(进程真的退)由 `后果` 那一组在 Mixin 那条路上钉;这一组只
    钉这一层的职责:把请求记进在途、照旧传下去、而且不许把 lifespan 当成请求。
    """

    def setUp(self):
        fixture_life.touch()

    def test_请求期间算在途回完才续命(self):
        import asyncio
        seen, busy = [], []

        async def app(scope, receive, send):
            seen.append(scope)
            busy.append(fixture_life.inflight())

        wrapped = fixture_life.guard(app, limit=0)
        fixture_life._LAST = time.monotonic() - 100
        asyncio.run(wrapped({"type": "http"}, None, None))
        self.assertEqual(busy, [1], "请求还在跑的时候就该算在途")
        self.assertEqual(fixture_life.inflight(), 0, "回完了要还回去")
        self.assertLess(fixture_life.idle_seconds(), 1, "回完那一刻才开始计空闲")
        self.assertEqual(len(seen), 1, "记完在途还得把请求原样传下去")

    def test_lifespan不算在途(self):
        """它从起到停贯穿整个进程 —— 算进在途的话空闲超时永远不会到点。"""
        import asyncio
        busy = []

        async def app(scope, receive, send):
            busy.append(fixture_life.inflight())

        wrapped = fixture_life.guard(app, limit=0)
        asyncio.run(wrapped({"type": "lifespan"}, None, None))
        self.assertEqual(busy, [0], "lifespan 不是一次请求")
        fixture_life.touch()


class http那条路(unittest.TestCase):
    """`TouchingHandlerMixin` 直接打单元 —— 只有同进程才读得到 `inflight()`。

    **夹具自己是 `HTTP/1.0`,一个请求就关连接**,所以 keep-alive 空闲读那个场景在它身上
    构造不出来:曾经有一版用真夹具发 keep-alive 请求来钉这道防线,它永远是绿的 —— 绿在
    服务端把连接关了,跟在途怎么算一点关系都没有。要钉就得自己搭一个 `HTTP/1.1` 的。
    """

    def _serve(self):
        class Handler(fixture_life.TouchingHandlerMixin,
                      http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def test_空闲的keepalive连接不算在途(self):
        """**反过来钉在途那一边**:keep-alive 连接等下一个请求时阻塞在读请求行上,把那
        一段算成在途的话,浏览器挂着一条连接就能让夹具永不退出 —— 空闲超时还是等于没装,
        只是换了个失守的方向。所以在途从**请求行已经读到**开始算。
        """
        srv = self._serve()
        before = fixture_life.inflight()
        conn = socket.create_connection(srv.server_address, timeout=5)
        self.addCleanup(conn.close)
        conn.sendall(b"GET / HTTP/1.1\r\nHost: l\r\n\r\n")
        self.assertIn(b"200", conn.recv(4096), "先确认这条连接真的还活着")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and fixture_life.inflight() > before:
            time.sleep(0.05)
        self.assertEqual(fixture_life.inflight(), before,
                         "连接挂着等下一个请求,那一段不算在途")


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

    def _spawn_via_middle(self, *, idle: str, owned: bool):
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
            "pass_fds = ()",
            "if owned:",
            "    r, w = os.pipe()",
            "    os.write(w, b'tok0123456789abcd')",
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
        self.addCleanup(self._reap_pid, grandchild)
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

    def _reap_pid(self, pid: int) -> None:
        """按**交回来的真实 pid** 收,动手前还要核验它确实是那个夹具。

        端口不是所有权凭据:孙进程早退之后那个号可能被另一个本地服务复用,照着端口
        发 SIGKILL 就杀错了人。pid 也可能被回收,所以再比一次命令行。
        """
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
        if "serve_workspace.py" not in cmd:
            return                        # 已经退了,或者号被别人复用 —— 都不该动
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
        self.addCleanup(self._reap_stopped, child)
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

    def _reap_stopped(self, pid: int) -> None:
        """被 SIGSTOP 按住的进程收不动,先放它走再收(核验同 `_reap_pid`)。"""
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGCONT)
        self._reap_pid(pid)

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
            "    print(base, flush=True)",
            "    time.sleep(600)",
        ])
        env = {**os.environ, fixture_life.IDLE_ENV: "0"}   # 单验 owner 这一道
        middle = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self._reap, middle)
        self.addCleanup(middle.stdout.close)
        base = middle.stdout.readline().strip()
        self.assertTrue(base.startswith("http://"), f"没拿到 base URL:{base!r}")
        port = int(base.rsplit(":", 1)[1])
        self.addCleanup(self._reap_listener, port)
        middle.kill()
        middle.wait(timeout=5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self._port_open(port):
            time.sleep(0.2)
        self.assertFalse(self._port_open(port),
                         "真 fixture_server 起的夹具也该认出调用方没了")

    def _reap_listener(self, port: int) -> None:
        """兜底:这条用例拿不到孙进程句柄,只能按端口找 —— **但必须核验命令行连端口
        号一起对上**,否则另一个本地服务正好占着这个号就被误杀了。
        """
        out = subprocess.run(["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout
        for raw in out.split():
            try:
                pid = int(raw)
            except ValueError:
                continue
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True).stdout
            if "serve_workspace.py" not in cmd or f"--port {port}" not in cmd:
                continue                  # 不是我们起的那一个,一个字都不碰
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
