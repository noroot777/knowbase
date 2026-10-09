# -*- coding: utf-8 -*-
"""连上用户**已经开着**的那个浏览器,在它里面开后台标签页干活。

这是拿到一个浏览器的第二条路,和 `x_cdp.Chrome`(自己起一个独立 profile 的无头
Chrome)并列。两条的边界完全不同,所以是一个设置而不是一个实现细节 ——
对照表在 `x_cdp` 的模块注释里。

**它的卖点只有一个:登录态。** 用户日常浏览器里装着他所有已经登录好的站点,
直连它就不必在一个独立 profile 里把 X 再登录一次(而那一次登录要过两步验证
和人机校验,是这条采集链上唯一必须人在场的步骤)。

**代价要说在前面:远程调试开着的时候,本机任何进程都能连上这个浏览器** ——
列标签页、读 cookie、以用户的身份发请求,全都不需要再过任何认证。所以这一档
不是默认,而且那个开关**只能由用户自己去开**:在浏览器地址栏打开
`chrome://inspect/#remote-debugging` 勾一次「Allow remote debugging for this
browser instance」。它只对这个浏览器实例有效(浏览器一关就没了),首次连接时
浏览器自己还会再弹一次授权。这两道都在用户手里,我们不代劳、也不去用别的
办法绕开它们(不改用户的启动参数、不写他的 Local State、不去唤起那个页面)。

**端口是读出来的,不是我们指定的。** 那个勾选开的是一个**随机**端口,Chrome
把它写进 user-data-dir 下的 `DevToolsActivePort`:第一行端口,第二行 browser
那条 WebSocket 的路径。所以发现 = 读这个文件 + TCP 探活;固定去敲 9222 只能
撞上「用户自己带 `--remote-debugging-port` 启动」那一种,那条留作回落。

**探活必须 TCP connect,不能拿 WebSocket 去试。** 首次 WebSocket 连接会让浏览
器弹一次授权弹窗,而这条判断是来源面板每次刷新都要做的
(`x_channels.readiness`)—— 拿 WebSocket 去探等于每刷一次面板弹用户一次。
TCP 握手不触发它。

**端口文件可能是陈旧的**:浏览器退出时不保证删掉它(崩溃、强杀都留着)。所以
读到端口不等于连得上,必须探活 —— 不探的话面板会说「已就绪」,而真抓的时候
才在 connect 上失败。

**不抄 web-access 那道「拦截页面对调试端口的探测」**(它用 `Fetch.enable` +
`Fetch.failRequest` 把页面发往 `127.0.0.1:<调试端口>` 的请求拒掉,防站点用这个
识别自动化)。那需要在协议层订阅事件并回一条命令,而 `Session.call` 现在把
事件全丢掉 —— 加一条事件回路是另一件事。不拦的后果是「和用户自己浏览时一样」:
这条通道的卖点本来就是用他自己的浏览器,比独立 profile 的无头壳更不像自动化。
真要拦,得先给 `Session` 加事件分发,记在这儿备查。
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from kb.feeds.x_cdp import CdpError, Session
from kb.feeds.ws_client import WebSocket, WsError

#: 用户自己去开那个开关的办法。三处文案(面板就绪说明、检查连接、登录端点的
#: 拒绝)共用这一份 —— 各写一遍必然漂。
ENABLE_HINT = ('在浏览器地址栏打开 chrome://inspect/#remote-debugging,勾上 '
               '"Allow remote debugging for this browser instance"'
               '(Edge 是 edge://inspect/#remote-debugging)。'
               '这个开关只对当前这个浏览器实例有效,浏览器重开要再勾一次。')
#: 手动 `--remote-debugging-port=N` 启动时的常见端口。只在读不到端口文件时才扫。
FALLBACK_PORTS = (9222, 9229, 9333)
#: 直接指定端点的逃生口:`9222` / `127.0.0.1:9222` / `ws://127.0.0.1:9222/devtools/browser/<id>`。
#: 给「Chrome 用了非默认 user-data-dir」这类发现不到的情况用。
ENDPOINT_ENV = 'KB_CDP_ENDPOINT'
#: `KB_CDP_ENDPOINT` 填错时的下一步。它和 `ENABLE_HINT` 是两回事:那句说的是
#: 「去把开关勾上」,而这一档的开关可能早就勾好了,坏的只是这个环境变量。
ENDPOINT_HINT = (f'{ENDPOINT_ENV} 的写法是 `9222` / `127.0.0.1:9222` / '
                 '`ws://127.0.0.1:9222/devtools/browser/<id>`,端口要在 1-65535。'
                 '清掉它就回到自动发现那条路。')
#: 首次连接的上限。比 `ws_client.HANDSHAKE_TIMEOUT` 宽得多:浏览器会弹一次授权
#: 弹窗,而用户要看见它、读一遍、再点「允许」。
ATTACH_TIMEOUT = 90.0
_PROBE_TIMEOUT = 1.0


@dataclass(frozen=True)
class Browser:
    """一个候选浏览器:它的 id、人话名字、以及它会把端口写到哪个文件。"""
    id: str
    label: str
    port_file: Path


@dataclass(frozen=True)
class Endpoint:
    """一个连得上的调试端点。"""
    host: str
    port: int
    path: str
    label: str

    def ws_url(self) -> str:
        return f'ws://{self.host}:{self.port}{self.path}'


def known_browsers() -> list[Browser]:
    """按平台列出候选。加一个新浏览器只改这里。"""
    home = Path.home()
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local'))
        rel = [('chrome', 'Chrome', 'Google/Chrome/User Data'),
               ('chromium', 'Chromium', 'Chromium/User Data'),
               ('edge', 'Microsoft Edge', 'Microsoft/Edge/User Data')]
        return [Browser(i, lb, base / p / 'DevToolsActivePort') for i, lb, p in rel]
    if (home / 'Library/Application Support').is_dir():
        base = home / 'Library/Application Support'
        rel = [('chrome', 'Chrome', 'Google/Chrome'),
               ('chrome-canary', 'Chrome Canary', 'Google/Chrome Canary'),
               ('chromium', 'Chromium', 'Chromium'),
               ('edge', 'Microsoft Edge', 'Microsoft Edge')]
        return [Browser(i, lb, base / p / 'DevToolsActivePort') for i, lb, p in rel]
    rel = [('chrome', 'Chrome', '.config/google-chrome'),
           ('chromium', 'Chromium', '.config/chromium'),
           ('edge', 'Microsoft Edge', '.config/microsoft-edge')]
    return [Browser(i, lb, home / p / 'DevToolsActivePort') for i, lb, p in rel]


def read_port_file(path: Path) -> tuple[int, str]:
    """`DevToolsActivePort` 的两行:端口 + browser 的 WebSocket 路径。

    读不出来返回 `(0, '')` —— 文件不存在(没开开关)和内容是垃圾(写到一半、
    被别的东西占了名字)对调用方是同一件事:这个浏览器现在用不上。
    """
    try:
        lines = path.read_text('utf-8').strip().splitlines()
    except (OSError, UnicodeDecodeError):
        return 0, ''
    if not lines:
        return 0, ''
    try:
        port = int(lines[0].strip())
    except ValueError:
        return 0, ''
    if not 0 < port < 65536:
        return 0, ''
    return port, (lines[1].strip() if len(lines) > 1 else '')


def port_alive(port: int, host: str = '127.0.0.1',
               timeout: float = _PROBE_TIMEOUT) -> bool:
    """TCP 握一下就断。**不发 WebSocket**(见模块注释:那会弹授权弹窗)。"""
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def parse_endpoint(text: str) -> Endpoint | None:
    """把 `KB_CDP_ENDPOINT` 的三种写法翻成一个 Endpoint。认不出返回 None。

    **绝不抛异常。** 它的调用方是来源面板每次刷新都要走的那条判断
    (`x_channels.readiness` → `registry.configuration_error`),抛出去就是一个
    500:`urlsplit(...).port` 对 `:not-a-port` 和 `:65536` 都抛 `ValueError`,
    于是「环境变量填错了」这件小事会让整个大V观点的订阅接口打不开(2026-10-09
    审查实测 `/api/intake/social` 与 `/api/intake/social/x-channel` 双双 500)。
    """
    value = (text or '').strip()
    if not value:
        return None
    if '://' in value:
        parts = urlsplit(value)
        try:
            port = parts.port or (80 if parts.scheme == 'http' else 0)
        except ValueError:                 # 端口不是数字,或者越界
            return None
        if parts.scheme not in ('ws', 'http') or not parts.hostname:
            return None
        if not _ok_port(port):
            return None
        return Endpoint(parts.hostname, port, parts.path or '', f'{ENDPOINT_ENV} 指定')
    host, _, tail = value.rpartition(':')
    try:
        port = int(tail)
    except ValueError:
        return None
    if not _ok_port(port):
        return None
    return Endpoint(host or '127.0.0.1', port, '', f'{ENDPOINT_ENV} 指定')


def _ok_port(port: int) -> bool:
    return 0 < port < 65536


def explicit_problem(env: dict | None = None) -> str:
    """`KB_CDP_ENDPOINT` 填了、却认不出来时的人话原因;正常时返回空串。

    **显式指定了就不再去猜别的**(见 `discover`),所以这个原因必须有人显示出来
    —— 不说的话面板上只剩一句「没有发现开着远程调试的浏览器」,而真实原因是这个
    环境变量写错了:用户照那句提示去勾开关,勾完还是连不上。
    """
    environ = os.environ if env is None else env
    raw = (environ.get(ENDPOINT_ENV, '') or '').strip()
    if not raw or parse_endpoint(raw) is not None:
        return ''
    return f'{ENDPOINT_ENV} 填的地址认不出来:{raw}'


def _browser_ws_path(host: str, port: int) -> str:
    """问 HTTP 那一侧要 browser 的 WebSocket 路径。

    只在端口文件没给第二行时才走这条 —— 也就是「用户手动带
    `--remote-debugging-port` 启动」那种情况,那种模式没有授权弹窗,所以这个
    HTTP 请求不会骚扰到谁。新版 Chrome 的 `/devtools/browser` 不带 id 是连不上
    的,所以不能拿它当默认值。
    """
    try:
        with urlopen(f'http://{host}:{port}/json/version', timeout=5) as resp:
            info = json.loads(resp.read().decode('utf-8'))
    except (URLError, OSError, ValueError, json.JSONDecodeError):
        return ''
    url = str(info.get('webSocketDebuggerUrl') or '')
    return urlsplit(url).path if url else ''


def discover(*, browsers: list[Browser] | None = None,
             env: dict | None = None) -> Endpoint | None:
    """找一个连得上的调试端点。找不到返回 None。

    顺序:显式指定(`KB_CDP_ENDPOINT`)→ 各浏览器的端口文件 → 回落扫常见端口。
    显式的那一档排在最前,因为它是「发现逻辑对我这台机器不成立」时唯一的出路。
    """
    environ = os.environ if env is None else env
    if raw := (environ.get(ENDPOINT_ENV, '') or '').strip():
        explicit = parse_endpoint(raw)
        # 写错了也不回落去猜别的:指错了要当场看出来(原因由 `explicit_problem`
        # 说清),而不是悄悄连上另一个浏览器。
        if explicit is None:
            return None
        if not port_alive(explicit.port, explicit.host):
            return None
        if explicit.path:
            return explicit
        path = _browser_ws_path(explicit.host, explicit.port)
        return Endpoint(explicit.host, explicit.port, path, explicit.label) if path else None
    for browser in (known_browsers() if browsers is None else browsers):
        port, path = read_port_file(browser.port_file)
        # 端口文件可能是上次运行留下的,所以读到也要探活(见模块注释)。
        if not port or not port_alive(port):
            continue
        if not path:
            path = _browser_ws_path('127.0.0.1', port)
        if path:
            return Endpoint('127.0.0.1', port, path, browser.label)
    for port in FALLBACK_PORTS:
        if not port_alive(port):
            continue
        if path := _browser_ws_path('127.0.0.1', port):
            return Endpoint('127.0.0.1', port, path, f'调试端口 {port}')
    return None


def found_label(endpoint: Endpoint | None) -> str:
    return endpoint.label if endpoint else ''


class Attached(Session):
    """用户那个浏览器里的一个后台标签页。退出时只关自己开的那几个。

    **绝不 `Browser.close`**:那是用户正在用的浏览器,关掉它等于把他所有标签页
    连同未提交的表单一起扔了。独立 profile 那条(`x_cdp.Chrome`)之所以能关,
    是因为那个浏览器是我们自己起的、除了我们没人在用。

    **也不去碰用户已有的标签页**:不枚举、不导航、不读。这条通道要的是那份
    登录态(它在 profile 里,不在某个标签页里),标签页是用户的东西。
    """

    _BACKGROUND = True

    def __init__(self, endpoint: Endpoint | None = None, *,
                 timeout: float = ATTACH_TIMEOUT):
        super().__init__()
        self._ep = endpoint
        self._timeout = timeout
        self._ws: WebSocket | None = None
        self._opened: list[str] = []

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def __enter__(self) -> 'Attached':
        endpoint = self._ep or discover()
        if endpoint is None:
            problem = explicit_problem()
            raise CdpError(f'{problem}。{ENDPOINT_HINT}' if problem else
                           '没有发现开着远程调试的浏览器。' + ENABLE_HINT)
        self._ep = endpoint
        try:
            self._ws = WebSocket.connect(endpoint.host, endpoint.port, endpoint.path,
                                         timeout=self._timeout)
        except WsError as e:
            raise CdpError(f'连不上{endpoint.label}:{e}。'
                           '如果浏览器弹了授权弹窗,点一下「允许」再试') from None
        try:
            self.call('Browser.getVersion', timeout=30)
        except (CdpError, EOFError, OSError) as e:
            self.__exit__(None, None, None)
            raise CdpError(f'连上了{endpoint.label},但它没有应答 CDP:{e}') from None
        return self

    def __exit__(self, *_exc) -> None:
        for target in self._opened:
            try:
                self.call('Target.closeTarget', {'targetId': target}, timeout=10)
            except (CdpError, EOFError, OSError, WsError):
                pass                       # 标签页可能已经被用户自己关了
        self._opened.clear()
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    # ── 页面 ────────────────────────────────────────────────────────────────

    def _adopt(self, target: str) -> None:
        """认下这张刚创建出来的标签页。**创建一回来就登记**,不等后面的初始化
        —— 理由和实测记在 `Session._adopt` 那条注释里。"""
        self._opened.append(target)

    # ── 传输 ────────────────────────────────────────────────────────────────

    def _post(self, msg: dict, deadline: float) -> None:
        if self._ws is None:
            raise CdpError('浏览器连接已经关掉了')
        try:
            self._ws.send(json.dumps(msg), deadline)
        except WsError as e:
            raise CdpError(str(e)) from None

    def _take(self, deadline: float) -> dict:
        if self._ws is None:
            raise CdpError('浏览器连接已经关掉了')
        try:
            return json.loads(self._ws.recv(deadline))
        except WsError as e:
            raise CdpError(str(e)) from None
