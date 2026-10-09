# -*- coding: utf-8 -*-
"""`kb/feeds/cdp_attach.py` 的回归:怎么发现那个浏览器、连上之后守什么规矩。

分两半,各自有各自不可替代的射程:

- **发现那半是纯单元的**(摆端口文件、起一个什么都不回的 TCP server)。它要钉
  的是"陈旧端口文件不算活"、"探活不许发 WebSocket"这类判定 —— 这些在真浏览器
  上恰好都是正常路径,看不出来。
- **连接那半起一个真的无头浏览器**(独立临时 profile + `--remote-debugging-port=0`,
  不碰任何现有浏览器)。`Attached` 的全部价值在于"在别人的浏览器里干活还不打扰
  他",而那件事只有真连一次才验得出:退出之后浏览器还得活着、用户原来那个
  标签页还得在。

**契约和后果各写一条**:「绝不发 `Browser.close`」是契约(用一个只记命令的子类
验),「退出之后浏览器还活着」是后果(真浏览器)。只验后果的话,一个发了
`Browser.close` 但恰好失败了的实现也能过;只验契约的话,换个别的方式关掉浏览器
(比如关掉所有标签页)照样过。
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from shutil import rmtree
from unittest import mock

from kb.feeds import cdp_attach as A
from kb.feeds import ws_client, x_browser, x_cdp
from kb.feeds.x_cdp import CdpError
from tests import ws_stub
from tests.headless import INSTALL_HINT, SHELL

_NEEDS_SHELL = unittest.skipUnless(
    SHELL is not None, '缺 chrome-headless-shell —— ' + INSTALL_HINT)


@contextmanager
def _dead_port():
    """一个确定没人听的端口号。"""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    yield port


@contextmanager
def _silent_server():
    """一个只 accept、什么都不回的 TCP server,并记下收到过多少字节。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(2)
    box = {'bytes': 0, 'connections': 0}

    def run() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            box['connections'] += 1
            conn.settimeout(0.6)
            try:
                data = conn.recv(4096)
                box['bytes'] += len(data)
            except OSError:
                pass
            finally:
                conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield srv.getsockname()[1], box
    finally:
        srv.close()
        thread.join(timeout=3)


def _port_file(tmp: Path, text: str) -> A.Browser:
    path = tmp / 'DevToolsActivePort'
    path.write_text(text, 'utf-8')
    return A.Browser('probe', '探针浏览器', path)


class PortFileTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='kb-portfile-'))
        self.addCleanup(rmtree, self.tmp, True)

    def test_two_lines_give_port_and_ws_path(self):
        browser = _port_file(self.tmp, '55286\n/devtools/browser/abc-123\n')
        self.assertEqual(A.read_port_file(browser.port_file),
                         (55286, '/devtools/browser/abc-123'))

    def test_one_line_leaves_the_path_empty(self):
        browser = _port_file(self.tmp, '9222\n')
        self.assertEqual(A.read_port_file(browser.port_file), (9222, ''))

    def test_unusable_contents_read_as_nothing(self):
        """「没开开关」和「文件里是垃圾」对调用方是同一件事:现在用不上。"""
        for text in ('', '\n', 'not-a-port\n/x', '0\n/x', '70000\n/x', '  \n'):
            with self.subTest(text=text):
                browser = _port_file(self.tmp, text)
                self.assertEqual(A.read_port_file(browser.port_file), (0, ''))

    def test_missing_file_reads_as_nothing(self):
        self.assertEqual(A.read_port_file(self.tmp / 'nope'), (0, ''))


class ProbeTests(unittest.TestCase):

    def test_alive_probe_sends_zero_bytes(self):
        """探活必须是 TCP connect。发 WebSocket 会让浏览器弹授权弹窗,而这条
        判断是面板每次刷新都要做的 —— 那等于每刷一次弹用户一次。"""
        with _silent_server() as (port, box):
            self.assertTrue(A.port_alive(port))
            time.sleep(0.3)
        self.assertEqual(box['connections'], 1)
        self.assertEqual(box['bytes'], 0, '探活不该往连接里写任何东西')

    def test_dead_port_is_not_alive(self):
        with _dead_port() as port:
            self.assertFalse(A.port_alive(port, timeout=1.0))


class DiscoverTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='kb-discover-'))
        self.addCleanup(rmtree, self.tmp, True)

    def test_a_stale_port_file_is_skipped(self):
        """浏览器退出不保证删掉端口文件(崩溃、强杀都留着)。只读文件不探活的话,
        面板会说「已就绪」,而真抓的时候才在 connect 上失败。"""
        with _dead_port() as port:
            browser = _port_file(self.tmp, f'{port}\n/devtools/browser/stale')
            self.assertIsNone(A.discover(browsers=[browser], env={}))

    def test_the_first_live_candidate_wins(self):
        with _dead_port() as dead, _silent_server() as (live, _):
            first = _port_file(self.tmp, f'{dead}\n/devtools/browser/stale')
            second_dir = self.tmp / 'second'
            second_dir.mkdir()
            second = _port_file(second_dir, f'{live}\n/devtools/browser/live')
            found = A.discover(browsers=[first, second], env={})
        self.assertIsNotNone(found)
        self.assertEqual((found.port, found.path), (live, '/devtools/browser/live'))

    def test_no_candidates_means_nothing_found(self):
        self.assertIsNone(A.discover(browsers=[], env={}))

    def test_explicit_endpoint_env_wins(self):
        with _silent_server() as (port, _):
            env = {A.ENDPOINT_ENV: f'ws://127.0.0.1:{port}/devtools/browser/forced'}
            # 候选里摆一个也活着的,断言显式那一档排在它前面。
            with _silent_server() as (other, _):
                decoy = _port_file(self.tmp, f'{other}\n/devtools/browser/decoy')
                found = A.discover(browsers=[decoy], env=env)
        self.assertEqual((found.port, found.path), (port, '/devtools/browser/forced'))
        self.assertIn(A.ENDPOINT_ENV, found.label)

    def test_explicit_endpoint_that_is_dead_finds_nothing(self):
        """显式指定了就不再去猜别的:指错了要当场看出来,而不是悄悄连上另一个
        浏览器(那会让「我明明指到了小号那个 profile」变成一句空话)。"""
        with _dead_port() as port, _silent_server() as (live, _):
            decoy = _port_file(self.tmp, f'{live}\n/devtools/browser/decoy')
            env = {A.ENDPOINT_ENV: f'ws://127.0.0.1:{port}/devtools/browser/x'}
            self.assertIsNone(A.discover(browsers=[decoy], env=env))


class ParseEndpointTests(unittest.TestCase):

    def test_three_accepted_spellings(self):
        cases = {
            '9222': ('127.0.0.1', 9222, ''),
            '127.0.0.1:9222': ('127.0.0.1', 9222, ''),
            'localhost:9333': ('localhost', 9333, ''),
            'ws://127.0.0.1:9222/devtools/browser/abc': ('127.0.0.1', 9222,
                                                         '/devtools/browser/abc'),
            'http://127.0.0.1:9222/devtools/browser/abc': ('127.0.0.1', 9222,
                                                           '/devtools/browser/abc'),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                got = A.parse_endpoint(text)
                self.assertIsNotNone(got, text)
                self.assertEqual((got.host, got.port, got.path), want)

    def test_unparsable_values_are_none(self):
        """**认不出一律返回 None,绝不抛。** 它的调用方是面板每次刷新都要走的
        那条判断,抛出去就是一个 500:`urlsplit(...).port` 对 `:not-a-port` 和
        `:65536` 都抛 `ValueError`(2026-10-09 审查实测两个接口双双 500)。
        """
        for text in ('', '   ', 'not-a-port', 'ws://nohost', 'ftp://127.0.0.1:9222',
                     'ws://127.0.0.1/devtools',
                     'ws://127.0.0.1:not-a-port',       # 端口不是数字
                     'ws://127.0.0.1:65536',            # 越界
                     'ws://127.0.0.1:0', '127.0.0.1:0', '127.0.0.1:65536',
                     '127.0.0.1:-1', '0'):
            with self.subTest(text=text):
                self.assertIsNone(A.parse_endpoint(text))

    def test_the_edges_of_the_port_range_are_in(self):
        for text in ('1', '65535', 'ws://h:65535/p'):
            with self.subTest(text=text):
                self.assertIsNotNone(A.parse_endpoint(text))

    def test_a_malformed_explicit_endpoint_never_falls_back(self):
        """**显式指定了就不再去猜别的。** 指错了要当场看出来,而不是悄悄连上
        另一个浏览器 —— 所以这一档既不读端口文件、也不扫回落端口。"""
        probed: list[int] = []
        with mock.patch.object(A, 'port_alive',
                               side_effect=lambda p, *a, **kw: probed.append(p) or True):
            found = A.discover(env={A.ENDPOINT_ENV: 'ws://127.0.0.1:65536'})
        self.assertIsNone(found)
        self.assertEqual([], probed, '写错了还去探别的端口')

    def test_a_malformed_explicit_endpoint_says_what_is_wrong(self):
        """原因必须有人说出来:只说「没有发现开着远程调试的浏览器」的话,用户
        照提示去勾开关,勾完还是连不上(真实原因是这个环境变量写错了)。"""
        problem = A.explicit_problem({A.ENDPOINT_ENV: 'ws://127.0.0.1:65536'})
        self.assertIn(A.ENDPOINT_ENV, problem)
        self.assertIn('ws://127.0.0.1:65536', problem)        # 把填错的那串还给他
        self.assertIn('1-65535', A.ENDPOINT_HINT)             # 下一步
        for env in ({}, {A.ENDPOINT_ENV: ''}, {A.ENDPOINT_ENV: '9222'}):
            with self.subTest(env=env):
                self.assertEqual('', A.explicit_problem(env))


class _StuckAttached(A.Attached):
    """`__enter__` 不再去连 —— 传输已经由摆盘塞好了。

    **不这么做的话这组用例压根走不到写路径**:真的 `__enter__` 会按那个假端点
    重新握一次手、当场连不上,于是整条用例验的变成了"连不上"(定点注入时实测
    各版本全绿,而写路径一行都没执行)。跳过它之后,第一条真命令就是采集自己
    发的那一条,正是审查复现的那个形状。
    """

    def __enter__(self) -> '_StuckAttached':
        return self


@contextmanager
def _stuck_attached():
    """一个**真** `Attached`,它的传输是一条灌满了的真 socket。

    对端扮演那个"卡住的浏览器":连上了、但永远不读数据。灌满内核缓冲之后任何
    一次写都只能等它来读 —— 原来那条裸 `sendall` 在这里**无限**阻塞,而这条链
    的上游是采集阶段(2026-10-09 审查实测:`call(timeout=0.05)` 0.61 秒后仍
    阻塞着,只有强行关掉对端才解开)。摆盘和「怎么才算真的灌满」在
    `tests/ws_stub.stuck_pair`(`test_ws_client` 共用同一份)。

    `Session.call`、`ws_client._write` 一行不打桩 —— 打桩正好绕开唯一要验的
    那件事。
    """
    with ws_stub.stuck_pair() as (mine, _peer):
        session = _StuckAttached(A.Endpoint('127.0.0.1', 1, '/stub', '卡住的浏览器'))
        session._ws = ws_client.WebSocket(mine)     # 构造时就设成非阻塞
        yield session


class _DeadlineProbe(x_cdp.Session):
    """只记下 `_post` 收到的 deadline。两条传输共用 `Session.call`,所以这条
    契约对管道那边同样成立。"""

    def __init__(self):
        super().__init__()
        self.deadlines: list[float] = []

    def _post(self, msg: dict, deadline: float) -> None:
        self.deadlines.append(deadline)
        raise CdpError('到这儿就够了')

    def _take(self, deadline: float) -> dict:         # pragma: no cover
        raise AssertionError('发都没发出去,不该来收')


class CallDeadlineTests(unittest.TestCase):
    """**一条命令的截止时刻要在发出去之前就算出来,而且发送也受它约束。**

    原来是先 `_send` 再算 deadline,于是 `timeout` 只管收、不管发:对端卡住
    不读数据时,一条命令停在写上无限等 —— 采集预算和用户按的那颗「停止」谁都
    轮不到执行。
    """

    def test_the_deadline_is_computed_before_the_command_goes_out(self):
        probe = _DeadlineProbe()
        before = time.monotonic()
        with self.assertRaises(CdpError):
            probe.call('Browser.getVersion', timeout=7.0)
        self.assertEqual(1, len(probe.deadlines))
        # 发送拿到的就是「现在 + timeout」那一个,不是一个空值或者无限。
        self.assertLessEqual(probe.deadlines[0], time.monotonic() + 7.0)
        self.assertGreaterEqual(probe.deadlines[0], before)

    def test_a_stuck_browser_does_not_block_a_call(self):
        """后果那一半:卡住的对端只能让这条命令**按时失败**。

        **载荷必须大、判据必须认写那一侧的文案**:灌满的 socket 过一会儿还会腾出
        两 KB,一条几十字节的命令照样挤得进去 —— 那时失败来自随后的**读**超时,
        这条用例对写路径压根不敏感(定点注入时实测全绿)。两条错误文案也不一样:
        读是「没有在预期时间内响应」,写是「没有在预期时间内收下命令」。
        """
        big = {'expression': 'x' * (4 << 20)}
        with _stuck_attached() as session:
            started = time.monotonic()
            with self.assertRaises(CdpError) as caught:
                session.call('Runtime.evaluate', big, timeout=0.3)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, f'写了 {elapsed:.2f} 秒才回来')
        self.assertIn('收下命令', str(caught.exception))

    def test_a_stuck_browser_does_not_freeze_the_whole_round(self):
        """后果那一半:`harvest` 要回来,而不是把整轮冻在第一条命令上。

        **只替掉「浏览器从哪儿来」这一下**(口径同 `test_x_collectors._FakeChrome`)
        —— 给它一个传输真的卡住的 `Attached`,`harvest` 自己那段编排、
        `Session.call` 和写路径全是真的。
        """
        with _stuck_attached() as session:
            jobs = [('alice', ''), ('bob', '')]
            started = time.monotonic()
            with mock.patch.object(x_browser, 'browser_session', return_value=session), \
                 mock.patch.object(ws_client, 'WRITE_TIMEOUT', 0.3):
                # `open_page` 用的是默认 45 秒的命令超时,真正救场的是写上限那一道
                # —— 把它压到 0.3 秒只是别让这条用例真等十秒。
                got = x_browser.harvest(jobs, profile='/tmp/nope', attach=True,
                                        budget=30.0)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0, f'整轮卡了 {elapsed:.1f} 秒')
        self.assertEqual({'alice', 'bob'}, set(got))
        for handle, row in got.items():
            with self.subTest(handle=handle):
                self.assertTrue(row['error'], '卡住了却没报错')
                self.assertEqual([], row['entries'])


class PipeWriteTests(unittest.TestCase):
    """管道那条传输同样会在写上阻塞(缓冲 64 KB 一满 `os.write` 就停住),
    而它攥着的也是整轮采集。这里不起浏览器,只把那条写路径摆进死局。"""

    def test_our_write_end_is_non_blocking(self):
        """契约那一半:`__enter__` 要把我们这个写端设成非阻塞 —— 阻塞的 `write`
        对一条大于剩余空间的载荷会等到全部写完,下面那个循环就白设上限了。"""
        chrome = x_cdp.Chrome('/tmp/nope')
        r, w = os.pipe()                                 # 和生产一样:默认阻塞
        self.addCleanup(lambda: [os.close(fd) for fd in (r, w)])
        chrome._w = w
        chrome._nonblocking_write()                      # `__enter__` 里就是这一句
        self.assertFalse(os.get_blocking(w))

    def test_a_full_pipe_times_out_instead_of_blocking(self):
        chrome = x_cdp.Chrome('/tmp/nope')
        r, w = os.pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (r, w)])
        os.set_blocking(w, False)                        # 灌满这一步由用例自己来
        try:
            while True:
                os.write(w, b'x' * 4096)             # 灌满,没人读
        except BlockingIOError:
            pass
        chrome._w = w
        started = time.monotonic()
        with self.assertRaises(CdpError) as caught:
            chrome._post({'id': 1, 'method': 'X', 'params': {}}, time.monotonic() + 0.3)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn('预期时间', str(caught.exception))


class _Recording(A.Attached):
    """不连真浏览器,只记下发出去的命令 —— 用来验「发了什么/没发什么」。"""

    def __init__(self):
        super().__init__(A.Endpoint('127.0.0.1', 1, '/stub', '桩'))
        self.sent: list[dict] = []

    def __enter__(self) -> '_Recording':
        return self                        # 跳过握手,这条用例不关心传输

    def _post(self, msg: dict, deadline: float) -> None:
        self.sent.append(msg)

    def _take(self, deadline: float) -> dict:
        msg = self.sent[-1]
        result: dict = {}
        if msg['method'] == 'Target.createTarget':
            result = {'targetId': 'T-1'}
        elif msg['method'] == 'Target.attachToTarget':
            result = {'sessionId': 'S-1'}
        return {'id': msg['id'], 'result': result}

    def methods(self) -> list[str]:
        return [m['method'] for m in self.sent]

    def closed(self) -> list[str]:
        return [m['params']['targetId'] for m in self.sent
                if m['method'] == 'Target.closeTarget']


class _FailingInit(_Recording):
    """`createTarget` 成功、下一条 `attachToTarget` 失败 —— 「标签页已经开出来
    了,初始化半途炸了」那个形状。"""

    def _take(self, deadline: float) -> dict:
        msg = self.sent[-1]
        if msg['method'] == 'Target.attachToTarget':
            return {'id': msg['id'], 'error': {'message': '注入:初始化失败'}}
        return super()._take(deadline)


class ContractTests(unittest.TestCase):

    def test_pages_open_in_the_background(self):
        """不加 `background` 的新标签页会抢到前台,把用户正在看的东西顶掉。"""
        with _Recording() as page:
            page.open_page('https://example.org/')
        created = [m for m in page.sent if m['method'] == 'Target.createTarget']
        self.assertEqual(len(created), 1)
        self.assertIs(created[0]['params'].get('background'), True)

    def test_a_tab_created_before_a_failed_init_is_still_ours_to_close(self):
        """**所有权在 `createTarget` 一回来就登记,不等后面的初始化。**

        原来是等整个 `open_page` 返回才登记,于是 `attachToTarget` / `Page.enable`
        任何一步失败时,退出清理压根不知道有这张页 —— 一张空白页就永久留在
        **用户自己的**浏览器里,「检查连接」反复失败会一直堆(2026-10-09 审查
        拿真无头浏览器实测:`leftover_tabs: ['data:...review-leftover']`)。
        """
        page = _FailingInit()
        with self.assertRaises(CdpError):
            with page:                     # 退出清理照旧跑
                page.open_page('https://example.org/')
        self.assertEqual(['T-1'], page.closed(), '创建出来的那张标签页没被收掉')

    def test_exit_closes_only_our_own_tabs_and_never_the_browser(self):
        """`Browser.close` 会关掉用户整个浏览器 —— 连同他所有标签页和没提交的
        表单。独立 profile 那条能关是因为那个浏览器是我们自己起的。"""
        with _Recording() as page:
            page.open_page('https://example.org/')
        self.assertNotIn('Browser.close', page.methods())
        closed = [m['params']['targetId'] for m in page.sent
                  if m['method'] == 'Target.closeTarget']
        self.assertEqual(closed, ['T-1'])

    def test_user_tabs_are_never_enumerated_on_the_happy_path(self):
        """要的是 profile 里那份登录态,标签页是用户的东西 —— 不列、不导航、不读。"""
        with _Recording() as page:
            page.open_page('https://example.org/')
            page.eval('1 + 1')
        self.assertNotIn('Target.getTargets', page.methods())

    def test_missing_browser_says_how_to_turn_it_on(self):
        """**这里必须打桩 `discover`,不能让它走真发现。** 跑测试的机器上很可能
        正开着一个勾过远程调试的日常浏览器(本机就是,9222 上是用户自己那个
        Chrome)—— 不打桩的话这条用例会真连上它,既不可复现,也正是这条通道
        要求"只有用户自己说了算"的那个浏览器。
        """
        with mock.patch.object(A, 'discover', return_value=None):
            with self.assertRaises(CdpError) as caught:
                with A.Attached(None):
                    pass                                         # pragma: no cover
        message = str(caught.exception)
        self.assertIn('没有发现开着远程调试的浏览器', message)
        self.assertIn('chrome://inspect/#remote-debugging', message)


@contextmanager
def _live_browser():
    """一个独立临时 profile 的无头浏览器,开着调试端口。不碰任何现有浏览器。

    启动页做成一个认得出的「用户标签页」:收尾那条断言要证明它一个字没动。
    """
    tmp = Path(tempfile.mkdtemp(prefix='kb-live-cdp-'))
    user_tab = 'data:text/html,<title>user-tab</title>'
    proc = subprocess.Popen(
        [str(SHELL), '--remote-debugging-port=0', f'--user-data-dir={tmp}',
         '--no-first-run', '--no-default-browser-check', user_tab],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = tmp / 'DevToolsActivePort'
        for _ in range(100):
            if port_file.exists() and A.read_port_file(port_file)[0]:
                break
            time.sleep(0.1)
        yield proc, A.Browser('live', '探针浏览器', port_file), user_tab
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:       # pragma: no cover
                proc.kill()
        rmtree(tmp, ignore_errors=True)


@_NEEDS_SHELL
class LiveBrowserTests(unittest.TestCase):
    """真连一次。这一组验的是「在别人的浏览器里干活」那几条规矩的后果。"""

    def test_discover_then_work_then_leave_no_trace(self):
        with _live_browser() as (proc, browser, user_tab):
            found = A.discover(browsers=[browser], env={})
            self.assertIsNotNone(found, '没发现那个开着调试端口的浏览器')
            self.assertTrue(found.path.startswith('/devtools/browser/'))

            with A.Attached(found) as page:
                before = _pages(page)
                page.open_page('data:text/html,<title>kb-probe</title>'
                               '<p id="x">hello</p>')
                time.sleep(0.6)
                self.assertEqual(page.eval('document.title'), 'kb-probe')
                self.assertEqual(page.eval("document.getElementById('x').textContent"),
                                 'hello')
                during = _pages(page)
            self.assertEqual(len(during), len(before) + 1, '后台标签页没开出来')

            # 后果一:浏览器还活着。`Browser.close` 会让这条当场红。
            self.assertIsNone(proc.poll(), '退出之后用户的浏览器被关掉了')
            # 后果二:自己开的那个标签页收掉了,用户原来那个一个字没动。
            with A.Attached(found) as again:
                left = _pages(again)
            self.assertEqual(left, before)
            self.assertTrue(any('user-tab' in url for url in left),
                            f'用户原来那个标签页不见了:{left}')

    def test_a_tab_is_not_left_behind_when_init_fails(self):
        """后果那一半,真浏览器:`createTarget` 成功、下一条命令失败,退出之后
        那张页不许留在用户的浏览器里(而浏览器本身照旧活着)。"""
        class _FailAttach(A.Attached):
            def call(self, method, params=None, sid='', timeout=x_cdp.CALL_TIMEOUT):
                if method == 'Target.attachToTarget':
                    raise CdpError('注入:初始化失败')
                return super().call(method, params, sid, timeout)

        leftover = 'data:text/html,<title>kb-leftover</title>'
        with _live_browser() as (proc, browser, _user_tab):
            found = A.discover(browsers=[browser], env={})
            self.assertIsNotNone(found)
            with A.Attached(found) as page:
                before = _pages(page)
            session = _FailAttach(found)
            with self.assertRaises(CdpError):
                with session:
                    session.open_page(leftover)
            time.sleep(0.3)
            with A.Attached(found) as again:
                left = _pages(again)
            self.assertEqual(before, left, f'初始化失败留下了标签页:{left}')
            self.assertIsNone(proc.poll(), '退出之后用户的浏览器被关掉了')

    def test_probe_goes_through_the_same_path(self):
        """`x_browser.probe(attach=True)` 和抓取走同一条路。这里连的是一个
        没有 X 登录态的空浏览器,所以结论必然是"不就绪" —— 要验的是它**真连上
        了**并给出成品文案,而不是在前置检查上就返回。
        """
        with _live_browser() as (_proc, browser, _tab):
            found = A.discover(browsers=[browser], env={})
            self.assertIsNotNone(found)
            with mock.patch.object(A, 'discover', return_value=found):
                ready, detail = x_browser.probe('', '', attach=True)
        self.assertFalse(ready)
        self.assertTrue(detail)
        # 没连上的话报的是「没有发现开着远程调试的浏览器」那句前置检查。
        self.assertNotIn('没有发现开着远程调试的浏览器', detail)


def _pages(page) -> list[str]:
    """当前的 page 型 target 列表(按 url)。只在测试里用 —— 产品代码不列用户的标签页。"""
    infos = page.call('Target.getTargets')['targetInfos']
    return sorted(t['url'] for t in infos if t['type'] == 'page')


class AttachedTransportTests(unittest.TestCase):

    def test_posting_after_close_is_a_clean_error(self):
        session = A.Attached(A.Endpoint('127.0.0.1', 1, '/x', '桩'))
        with self.assertRaises(CdpError):
            session._post({'id': 1, 'method': 'X', 'params': {}}, time.monotonic() + 1)
        with self.assertRaises(CdpError):
            session._take(time.monotonic() + 1)

    def test_endpoint_url_is_assembled_from_its_parts(self):
        endpoint = A.Endpoint('127.0.0.1', 9222, '/devtools/browser/abc', 'Chrome')
        self.assertEqual(endpoint.ws_url(), 'ws://127.0.0.1:9222/devtools/browser/abc')

    def test_known_browsers_are_distinct_and_named(self):
        browsers = A.known_browsers()
        self.assertTrue(browsers)
        self.assertEqual(len({b.id for b in browsers}), len(browsers))
        self.assertTrue(all(b.label and b.port_file.name == 'DevToolsActivePort'
                            for b in browsers))

    def test_json_version_fallback_tolerates_garbage(self):
        """只在端口文件没给第二行时才走 HTTP。对端不是调试端口时要静静返回空,
        而不是把异常冒给面板那条每次刷新都跑的判断。"""
        with _silent_server() as (port, _):
            self.assertEqual(A._browser_ws_path('127.0.0.1', port), '')
        with _dead_port() as port:
            self.assertEqual(A._browser_ws_path('127.0.0.1', port), '')


if __name__ == '__main__':
    unittest.main()
