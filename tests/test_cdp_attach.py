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
from kb.feeds.x_cdp import CdpError
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
        for text in ('', '   ', 'not-a-port', 'ws://nohost', 'ftp://127.0.0.1:9222',
                     'ws://127.0.0.1/devtools'):
            with self.subTest(text=text):
                self.assertIsNone(A.parse_endpoint(text))


class _Recording(A.Attached):
    """不连真浏览器,只记下发出去的命令 —— 用来验「发了什么/没发什么」。"""

    def __init__(self):
        super().__init__(A.Endpoint('127.0.0.1', 1, '/stub', '桩'))
        self.sent: list[dict] = []

    def __enter__(self) -> '_Recording':
        return self                        # 跳过握手,这条用例不关心传输

    def _post(self, msg: dict) -> None:
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


class ContractTests(unittest.TestCase):

    def test_pages_open_in_the_background(self):
        """不加 `background` 的新标签页会抢到前台,把用户正在看的东西顶掉。"""
        with _Recording() as page:
            page.open_page('https://example.org/')
        created = [m for m in page.sent if m['method'] == 'Target.createTarget']
        self.assertEqual(len(created), 1)
        self.assertIs(created[0]['params'].get('background'), True)

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

    def test_probe_goes_through_the_same_path(self):
        """`x_browser.probe(attach=True)` 和抓取走同一条路。这里连的是一个
        没有 X 登录态的空浏览器,所以结论必然是"不就绪" —— 要验的是它**真连上
        了**并给出成品文案,而不是在前置检查上就返回。
        """
        from kb.feeds import x_browser
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
            session._post({'id': 1, 'method': 'X', 'params': {}})
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
