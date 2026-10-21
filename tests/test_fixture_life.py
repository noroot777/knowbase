# -*- coding: utf-8 -*-
"""夹具自己什么时候退 —— 判据一组,后果一组。

**后果是「那个进程还在不在」**,所以这里不满足于打 `should_exit` 的返回值:真起一个
`tests/serve_workspace.py`,等它自己退(或者钉住它不该退时确实还在)。判据那一组是
为了把边界铺开,后果那一组才是这件事本身。

**每一条该退的都配一条不该退的**:空闲到点要退 / 持续有请求不许退;原调用方没了要退
/ 没有原调用方(手起、跨 shell 共用)不许退。后面那半边是这套设计的核心 —— 它一旦失
守,会话就没法再手起夹具跑探针了,而这条路本身是合法的。
"""

from __future__ import annotations

import glob
import os
import signal
import socket
import subprocess
import sys
import tempfile
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
    """它在 `timeout` 内退掉了吗。"""
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _wait_listening(port: int, proc: subprocess.Popen, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"夹具还没监听就退了(rc={proc.returncode})")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).read()
            return
        except urllib.error.HTTPError:
            return                       # 有回应就算起来了,状态码不重要
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise AssertionError("夹具没有在限时内监听")


class 判据(unittest.TestCase):
    """`should_exit` 是唯一那份判据,边界都打在它身上。"""

    def test_空闲没到不退(self):
        fixture_life.touch()
        self.assertEqual(fixture_life.should_exit(limit=60, origin=0), "")

    def test_空闲到点要退(self):
        # 把时钟往回拨,别靠「门槛小到比两行代码还短」—— 那样用例对实现不敏感
        fixture_life._LAST = time.monotonic() - 10
        self.assertIn("没有请求", fixture_life.should_exit(limit=1, origin=0))
        fixture_life.touch()

    def test_空闲上限为零就永不因空闲退(self):
        """反过来钉一边:手动调试页面的人把它关掉之后,放着一天也不许自己退。"""
        fixture_life._LAST = time.monotonic() - 86400
        self.assertEqual(fixture_life.should_exit(limit=0, origin=0), "")
        fixture_life.touch()

    def test_原调用方还在不退(self):
        fixture_life.touch()
        self.assertEqual(
            fixture_life.should_exit(limit=60, origin=os.getppid()), "")

    def test_原调用方没了要退(self):
        fixture_life.touch()
        # 1 永远不会是我们的父进程(我们自己就不是 init 的直接子进程)
        self.assertIn("原调用方", fixture_life.should_exit(limit=60, origin=1))

    def test_没有原调用方就不看父进程(self):
        """反过来钉一边:手起的夹具 origin=0,父 shell 退掉也不许自杀。"""
        fixture_life.touch()
        self.assertEqual(fixture_life.should_exit(limit=60, origin=0), "")


class ASGI那条路(unittest.TestCase):
    """7 个 uvicorn 夹具的续命走 `guard`,`serve_workspace` 走 Mixin —— 两份实现
    各有自己的红。后果(进程真的退)由 `后果` 那一组在 Mixin 那条路上钉;这一组只
    钉这一层的职责:把请求接到 `touch` 上,并且照旧把请求传下去。
    """

    def test_每个请求续一次命并照旧传下去(self):
        import asyncio
        seen = []

        async def app(scope, receive, send):
            seen.append(scope)

        wrapped = fixture_life.guard(app, limit=0, origin=0)   # 不起看门狗线程
        fixture_life._LAST = time.monotonic() - 100
        asyncio.run(wrapped({"type": "http"}, None, None))
        self.assertLess(fixture_life.idle_seconds(), 1, "每个请求都该续一次命")
        self.assertEqual(len(seen), 1, "续命之后还得把请求原样传下去")
        fixture_life.touch()


class 原调用方的认定(unittest.TestCase):
    """`caller()` 只认「启动这一刻确实是我们父进程」的那个 pid。"""

    def setUp(self):
        self._saved = os.environ.get(fixture_life.PARENT_ENV)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop(fixture_life.PARENT_ENV, None)
        else:
            os.environ[fixture_life.PARENT_ENV] = self._saved

    def test_没给就是没有(self):
        os.environ.pop(fixture_life.PARENT_ENV, None)
        self.assertEqual(fixture_life.caller(), 0)

    def test_给了真父进程就认(self):
        os.environ[fixture_life.PARENT_ENV] = str(os.getppid())
        self.assertEqual(fixture_life.caller(), os.getppid())

    def test_继承来的陈旧值不认(self):
        """那个 pid 说的是别人的调用方 —— 认了会让夹具凭一个无关进程的死活自杀。"""
        stale = os.getppid() + 100000      # 几乎不可能正好是我们的父进程
        os.environ[fixture_life.PARENT_ENV] = str(stale)
        self.assertEqual(fixture_life.caller(), 0)

    def test_垃圾值不认(self):
        os.environ[fixture_life.PARENT_ENV] = "不是数字"
        self.assertEqual(fixture_life.caller(), 0)

    def test_一号进程不认(self):
        os.environ[fixture_life.PARENT_ENV] = "1"
        self.assertEqual(fixture_life.caller(), 0)


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


class 后果(unittest.TestCase):
    """真起一个夹具,断言的是「那个进程还在不在」。"""

    def _spawn(self, *, idle: str, told: bool = False, via_parent: bool = False):
        """起一个 `serve_workspace.py`,返回 (port, 直接起的句柄或 None, 中间进程或 None)。

        `via_parent=True` 时夹具由一个中间 python 进程起,好让我们杀掉那个中间进程
        来模拟「原调用方被 SIGKILL」—— 那正是 `fixture_server` 的 `finally` 里
        `reap()` 一行都不跑的那一档。`told` 决定中间进程告不告诉夹具「我是你的调用方」。
        """
        port = _free_port()
        env = {**os.environ, fixture_life.IDLE_ENV: idle}
        env.pop(fixture_life.PARENT_ENV, None)
        argv = [sys.executable, "tests/serve_workspace.py", "--port", str(port)]
        if not via_parent:
            proc = subprocess.Popen(argv, cwd=ROOT, env=env,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            self.addCleanup(self._reap, proc)
            _wait_listening(port, proc)
            return port, proc, None

        code = "\n".join([
            "import os, subprocess, sys, time",
            "env = dict(os.environ)",
            f"key = {fixture_life.PARENT_ENV!r}",
            f"told = {told!r}",
            "if told: env[key] = str(os.getpid())",
            "else: env.pop(key, None)",
            f"subprocess.Popen([sys.executable, 'tests/serve_workspace.py',"
            f" '--port', '{port}'], env=env,"
            " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
            "time.sleep(600)",
        ])
        middle = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap, middle)
        self.addCleanup(self._reap_port, port)
        _wait_listening(port, middle)
        return port, None, middle

    def _port_open(self, port: int) -> bool:
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _reap(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    def _reap_port(self, port: int) -> None:
        """中间进程那条路上夹具是孙子进程,收不到句柄 —— 按端口兜一道。"""
        if not self._port_open(port):
            return
        out = subprocess.run(["lsof", "-nP", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
                             capture_output=True, text=True)
        for pid in out.stdout.split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ValueError, ProcessLookupError, PermissionError):
                pass

    # ---- 空闲那一道 ----

    def test_空闲到点真的会退(self):
        port, proc, _ = self._spawn(idle="2")
        self.assertTrue(_wait_gone(proc, 25), "空闲够久了,夹具应该自己退掉")
        self.assertFalse(self._port_open(port), "退了就不该还占着端口")

    def test_一直有请求就不退(self):
        """反过来钉一边 —— 顺带钉住 touch 真的接在请求上了。

        这条要是失守,探针跑到一半夹具就没了,而症状是「页面打不开」,看不出真因。
        """
        port, proc, _ = self._spawn(idle="2")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read()
            except urllib.error.HTTPError:
                pass
            time.sleep(0.4)
        self.assertTrue(_alive(proc), "一直有人用,不该被空闲超时收掉")

    def test_关掉之后放着也不退(self):
        """`KB_FIXTURE_IDLE=0` 是手动调试页面的人的出口,必须真的关得掉。"""
        port, proc, _ = self._spawn(idle="0")
        self.assertFalse(_wait_gone(proc, 4), "关掉了空闲超时就不该自己退")

    def test_自己退的时候把临时根删掉(self):
        """**这条钉的是收场信号选得对不对。**

        发 SIGTERM 的话 Python 没有 handler 就直接结束进程,`main()` 里那个
        `with contextlib.ExitStack()` 一行不跑,盘上留下一份完整的沙箱库 —— 那正是
        `tests/fixture_root.py` 记载的事故形态,第一版实测复刻了一遍(退出码 -15、
        留下 `kb-source-qa-*`)。手动跑这一档根归夹具自己删,所以拿它来验。
        """
        pat = os.path.join(tempfile.gettempdir(), "kb-source-qa-*")
        before = set(glob.glob(pat))
        port = _free_port()
        env = {**os.environ, fixture_life.IDLE_ENV: "3"}
        env.pop(fixture_life.PARENT_ENV, None)
        env.pop("KB_FIXTURE_ROOT", None)          # 手动跑:根归它自己
        proc = subprocess.Popen(
            [sys.executable, "tests/serve_sources.py", "--port", str(port)],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap, proc)
        _wait_listening(port, proc, timeout=120)
        self.assertTrue(_wait_gone(proc, 40), "空闲够久了,夹具应该自己退掉")
        time.sleep(1)
        self.assertEqual(set(glob.glob(pat)) - before, set(),
                         "自己退的时候得把临时根删掉,不能留一份沙箱库在盘上")

    # ---- 原调用方那一道 ----

    def test_原调用方被杀夹具自己退(self):
        port, _, middle = self._spawn(idle="0", told=True, via_parent=True)
        middle.kill()
        middle.wait(timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self._port_open(port):
            time.sleep(0.2)
        self.assertFalse(self._port_open(port),
                         "原调用方没了,夹具应该自己退掉")

    def test_手起的夹具父进程退了也不退(self):
        """**这条是整套设计的核心**:会话手起夹具、起它那个 shell 当场就退,随后另
        一个 shell 才跑探针。照搬探针那边「现读 ppid」的话,夹具一起跑就自杀。
        """
        port, _, middle = self._spawn(idle="0", told=False, via_parent=True)
        middle.kill()
        middle.wait(timeout=5)
        time.sleep(3)                     # 远超 POLL_SECONDS,真要自杀早该退了
        self.assertTrue(self._port_open(port),
                        "没有原调用方时不该凭父进程的死活自杀")


if __name__ == "__main__":
    unittest.main()
