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

from tests import fixture_life, fixture_voice
from tests.fixture_life_cases import 起与收
from tests.fixture_life_probe import (   # noqa: F401 - 见那边的说明
    _shut_fds, _stat_of, _open_fds, _free_port, _alive, _wait_gone, _ping, _wait_listening, _owner_pipe, _reset_state, _age, _closed,
    ROOT,
)


class 后果(起与收):
    """真起一个夹具,断言的是「那个进程还在不在」。"""

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
        self._claim_pid(child, middle, stopped=True)
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

    def test_高位凭据下调用方取消照样带走夹具(self):
        """**第 1 条的后果。** 调用方先开着一千来个文件时,凭据落在 fd 1024 以上。

        上一版用 `select.select()` 等它可读,高位号抛 `ValueError` 被当成「从来没有
        owner」—— 实测:调用方被 SIGKILL 之后页面照旧返回 200、进程还是同一个,而默认
        900 秒的空闲那一道救不了它(页面还在轮询就一直续命)。
        """
        port, middle, grandchild = self._spawn_via_middle(idle="0", owned=True,
                                                          highfd=True)
        self.assertGreater(self._owner_fd, 1023,
                           f"没把凭据挤到高位去(fd={self._owner_fd}),没验到东西")
        self.assertTrue(self._port_open(port), "先确认它真的起来了")
        middle.kill()
        middle.wait(timeout=5)
        self.assertTrue(self._wait_dead(grandchild, 15),
                        "高位的凭据一样得认出调用方没了")
        self.assertFalse(self._port_open(port), "页面不该还开着")

    def test_启动提示堵住也装得上防线(self):
        """**第 2 条的后果。** 预先把夹具的 stdout 填满,它那行启动提示就写不出去。

        上一版六个入口都先 `print(..., flush=True)` 再 `guard(app)`,而 `install()` 在
        `guard` 里面 —— 那次 print 阻塞在满管道上,`install()` 永远执行不到。手起这条
        路本来就没有 owner,于是一道防线都没装:实测过了空闲上限进程还在、端口根本不
        响应,而 print 阻塞不抛异常,外面什么都看不到。
        """
        _, w = self._plug()
        port, proc = self._spawn(idle="1", stdout=w)
        self.assertTrue(self._port_open(port),
                        "提示写不出去也必须照旧起来服务 —— 提示丢得起")
        self.assertTrue(_wait_gone(proc, 25),
                        "空闲那道防线必须已经装上了")

    def test_摆盘阶段别人替我们说话也堵不住防线(self):
        """**`announce` 只管入口自己那行提示,摆盘途中还有别人替我们说话。**

        阅读页和片段那两个夹具要先调 `kb.cli._index_pass()` 建索引,它完成时那行 `print`
        走的是普通 stdout。默认缓冲下那一小行留在 Python 的缓冲区里,所以满 stdout 的烟
        测照旧能开页面 —— 这一条**偶然**;`-u` / `PYTHONUNBUFFERED=1` 下它立刻落到底层
        fd,手起又没人消费那根管道时,进程就堵在建库里,`install()` 根本没跑到,唯一那道
        空闲防线一行都没装上。

        所以边界不在入口里、在 `tests/fixture_voice.py`:模块一加载就把摆盘那段的 stdout
        换成一根谁都堵不住的管子。这里用 `-u` 关掉缓冲、把 stdout 预先灌满,断言后果:
        **照旧起得来服务,空闲到点照旧自己退**。
        """
        r, w = self._plug()
        port = _free_port()
        env = {**os.environ, fixture_life.IDLE_ENV: "1", "PYTHONUNBUFFERED": "1"}
        env.pop(fixture_life.OWNER_ENV, None)
        proc = subprocess.Popen(
            [sys.executable, "-u", "tests/serve_reading.py", "--port", str(port)],
            cwd=ROOT, env=env, stdout=w, stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap, proc)
        _wait_listening(port, proc, timeout=120)
        self.assertTrue(self._port_open(port),
                        "摆盘阶段的输出写不出去也必须照旧起来服务")
        self.assertTrue(_wait_gone(proc, 30), "空闲那道防线必须已经装上了")
        self.assertFalse(self._port_open(port), "退了就不该再占着这个端口")

    def test_摆盘阶段写stderr堵住也堵不住防线(self):
        """**上面那条只圈住了 stdout,摆盘期的另一条通道一样堵得死防线。**

        七个 ASGI 入口导入 `kb.server` 那一路会发弃用警告,`kb/store.py` 导入的 jieba 还
        会按 `logging.StreamHandler(sys.stderr)` 把初始化日志**同步**写 stderr —— 两者都
        不经过 `_say`,也就都不在那个有界队列里面。实测给真阅读入口一根填满的 stderr、
        `-u` 关掉缓冲:过了三倍空闲上限进程还在、HTTP 不响应、沙箱根还在;一开始读那根
        管子,它八秒内就起来服务、随后按空闲正常退掉 rc=0。手起这条路没有 owner,空闲那
        道又必须等摆盘结束才装,所以堵在那儿的时候**两个自退条件都没在跑**,
        `KB_FIXTURE_IDLE` 设成多小都没用。

        断言的是后果:**照旧起得来服务,空闲到点照旧自己退**。
        """
        _, w = self._plug()
        port = _free_port()
        env = {**os.environ, fixture_life.IDLE_ENV: "1", "PYTHONUNBUFFERED": "1"}
        env.pop(fixture_life.OWNER_ENV, None)
        proc = subprocess.Popen(
            [sys.executable, "-u", "tests/serve_reading.py", "--port", str(port)],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=w)
        self.addCleanup(self._reap, proc)
        _wait_listening(port, proc, timeout=120)
        self.assertTrue(self._port_open(port),
                        "摆盘阶段写 stderr 写不出去也必须照旧起来服务")
        self.assertTrue(_wait_gone(proc, 30), "空闲那道防线必须已经装上了")
        self.assertFalse(self._port_open(port), "退了就不该再占着这个端口")

    def test_摆盘边界立不起来就不许接着开服务(self):
        """**立不起那道边界的时候不许「就这样算了」继续跑。**

        `dup` / `pipe` / 搬运线程任意一步失败,上一版都是直接 `return`:接下来**可能**一
        切正常,也可能在摆盘途中某一次别人的同步输出上永久阻塞 —— 而那正是「一道防线都
        没装、进程永久留着」那个形态(手起没有 owner,空闲那道要等摆盘结束才装)。

        这里在真入口上定点注入 `os.pipe` 的 `EMFILE`(它自己声明会抛的那个),断言后果:
        **它当场退、退出码是认得出来的那一个,一个端口都没开出来**,而不是安静地跑进一
        个没有防线的运行态。
        """
        port = _free_port()
        code = "\n".join([
            "import os, runpy, sys",
            "def boom():",
            "    raise OSError(24, 'injected EMFILE')",
            "os.pipe = boom",
            "sys.argv = sys.argv[1:]",            # argv[0] 得是那个入口自己
            "runpy.run_path(sys.argv[0], run_name='__main__')",
        ])
        env = {**os.environ, fixture_life.IDLE_ENV: "1"}
        env.pop(fixture_life.OWNER_ENV, None)
        done = subprocess.run(
            [sys.executable, "-c", code, "tests/serve_workspace.py",
             "--port", str(port)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, fixture_voice.NO_BOUNDARY_RC,
                         f"立不起边界就该当场退:{done.stdout[-400:]!r} "
                         f"{done.stderr[-400:]!r}")
        self.assertFalse(self._port_open(port), "一个端口都不该开出来")

    def test_低上限下高位用例不许污染整轮(self):
        """**准备阶段失败只该让这一条跳过,不该把整个测试进程推到 `EMFILE`。**

        高位凭据那条用例要先占满低号。上一版用一句列表推导一次申请 1100 个,全成功了才
        `addCleanup` —— 中途某一次 `os.open` 抛出来时那个列表压根还没赋给变量,前面已经
        开成功的几百个号就没人收了(整数被丢掉不会关掉内核里的 fd)。实测把软上限压到
        128 跑它,清理全跑完之后进程里还剩 125 个开着的号,紧接着一次普通
        `open(os.devnull)` 就 `EMFILE`,**后面每一条用例都跟着坏**。

        这里把**硬**上限也压到 128(抬不动),在一个独立子进程里跑那条真用例,断言后果:
        它**跳过**而不是红在半路、没留下开着的号、随后还能正常开文件。
        """
        code = "\n".join([
            "import os, resource, sys, unittest",
            "resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))",
            f"sys.path.insert(0, {str(ROOT)!r})",
            "res = unittest.main(module=None, exit=False, argv=['x',",
            "    'tests.test_fixture_life.所有权凭据.test_高位的凭据照样认得出来'],",
            "    verbosity=0).result",
            "def is_open(fd):",
            "    try:",
            "        os.fstat(fd)",
            "        return True",
            "    except OSError:",
            "        return False",
            "left = sum(1 for fd in range(3, 128) if is_open(fd))",
            "try:",
            "    probe = os.open(os.devnull, os.O_RDONLY)",
            "    os.close(probe)",
            "    ok = True",
            "except OSError:",
            "    ok = False",
            "print('left=%d skipped=%d bad=%d followup=%s' % (",
            "    left, len(res.skipped), len(res.errors) + len(res.failures), ok),",
            "    flush=True)",
        ])
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                              capture_output=True, text=True, timeout=180)
        line = next((l for l in done.stdout.splitlines() if l.startswith("left=")), "")
        self.assertTrue(line, f"子进程没报出结果:{done.stdout!r} {done.stderr[-400:]!r}")
        got = dict(part.split("=", 1) for part in line.split())
        # **后果排在前面**:漏没漏号、后面还能不能开文件,才是「污染整轮」这件事本身
        self.assertEqual(got["left"], "0", f"准备阶段失败不许漏下开着的号:{line}")
        self.assertEqual(got["followup"], "True", f"后面还得能正常开文件:{line}")
        self.assertEqual(got["bad"], "0", f"上限不够该跳过,不该红在半路:{line}")
        self.assertEqual(got["skipped"], "1", f"该明说跳过:{line}")

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
            "    print(base, base.pid, base.root, flush=True)",
            "    time.sleep(600)",
        ])
        env = {**os.environ, fixture_life.IDLE_ENV: "0"}   # 单验 owner 这一道
        middle = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self._reap, middle)
        self.addCleanup(middle.stdout.close)
        line = middle.stdout.readline().strip()
        base, raw_pid, raw_root = (line.split(" ") + ["", ""])[:3]
        self.assertTrue(base.startswith("http://"), f"没拿到 base URL:{line!r}")
        port = int(base.rsplit(":", 1)[1])
        # **中间进程是被 SIGKILL 收掉的**,它 `fixture_server` 的 `finally` 一行都不跑,
        # 那份临时根就留在盘上(实测这一条每跑一次留一个空的 `kb-fixture-*`)。所以这一
        # 轮的根由我们收,而且只收**前缀对得上、又正好在临时目录底下**的那一个 —— 路径
        # 是从子进程的输出里读来的,不核一遍不许往 `rmtree` 上送。
        self.assertEqual(tempfile.gettempdir(), os.path.dirname(raw_root), line)
        self.assertTrue(os.path.basename(raw_root).startswith("kb-fixture-"), line)
        self.addCleanup(shutil.rmtree, raw_root, ignore_errors=True)
        # **按它交回来的 pid 收,不按端口找。** 端口不是所有权凭据(见 `_identity`)。
        grandchild = int(raw_pid)
        self._claim_pid(grandchild, middle)
        middle.kill()
        middle.wait(timeout=5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self._port_open(port):
            time.sleep(0.2)
        self.assertFalse(self._port_open(port),
                         "真 fixture_server 起的夹具也该认出调用方没了")



if __name__ == "__main__":
    unittest.main()
