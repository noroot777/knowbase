# -*- coding: utf-8 -*-
"""最小 WebSocket 客户端(RFC 6455 的客户端那一侧),只够 CDP 用。

**为什么手写而不添一条依赖**:CDP over WebSocket 用的是这个协议最窄的一条缝
—— 一条连接、只发文本帧、不协商任何扩展、对端是本机 loopback 上的 Chrome。
现成的那几个库(`websockets`、`aiohttp`)都是围着 asyncio 和服务端场景长起来
的,而这里的调用方(采集阶段、面板那颗「检查连接」)全是同步代码,套一层事件
循环只为发一条 `Runtime.evaluate` 不划算。口径同 `x_cdp` 那条「协议就是一个
recv 循环的事」。

**读永远带 deadline**(`recv(deadline)` 一路传到 `_fill`)。理由同
`x_cdp.CALL_TIMEOUT` 和 embed_pool 那次事故:攥着锁阻塞的 read 会把一整条链
冻住,所以把「挂住」变成「报错重试」。

**写也永远带 deadline,而且不许用 `sendall`**:对端(卡住的浏览器、或者一个
压根不读数据的端口)不收的时候,`sendall` 在内核缓冲满了之后会**无限**阻塞 ——
而这条链的上游是采集阶段,那一下把整轮冻住:采集预算和用户按的那颗「停止」谁
都轮不到执行(2026-10-09 审查实测:`call(timeout=0.05)` 0.61 秒后仍阻塞着,
只有测试主动把对端关掉才解开)。所以 socket 一律非阻塞(见 `__init__`:光
「select 等可写 + send」挡不住,BSD 的 `sosend` 对大载荷会在内核里循环等到写完)、
`_write` 按 select 等可写、一段段 `send`,
而且 **超时之后这条连接就废了**:帧只发出去一半,之后发什么都会接在那半截后面
被对端当成协议错 —— 比"再等一次"更坏的是把错位留着。握手、自动 pong、关闭帧
走的是同一条写路径,所以同样受限。

**不协商 permessage-deflate**(握手里压根不发 `Sec-WebSocket-Extensions`),
于是收到的帧 RSV 位一定是 0 —— 置位就是对端在用我们没同意过的扩展,当协议
错处理,不去猜它想压缩什么。

**握手时多读的字节要留着**:HTTP 响应头和第一个数据帧很可能在同一个 TCP 包里,
按 `\\r\\n\\r\\n` 切完把尾巴丢掉就是丢掉第一条消息 —— 而 CDP 的第一条消息往往
正是我们在等的那条回复,表现成一次莫名的超时。所以 `_read_until` 只从缓冲区
里取走它要的那一段。
"""

from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import time

_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
# 单帧上限。Chrome 侧最大的一条是截图(整屏 PNG 的 base64,实测几 MB)。上限
# 存在的理由同 `embed_pool._MAX_FRAME`:帧一旦错位,长度字段会读出一个天文
# 数字,没有上限的话这里会试着按它分配内存。
MAX_PAYLOAD = 64 << 20
# HTTP 响应头的上限。正常是几百字节;超这个数说明对端压根不是个 WebSocket。
_MAX_HEAD = 1 << 16
_CHUNK = 1 << 16
HANDSHAKE_TIMEOUT = 10.0
#: 单次写的上限,还会被这条命令自己的 deadline 压一道(`_write` 取两者的小者,
#: 所以 `call(timeout=0.05)` 就是 0.05 秒失败,不是十秒)。本机 loopback 上一条
#: CDP 命令只有几 KB、内核缓冲是几百 KB —— 健康的浏览器压根不会在写上停住,
#: 十秒收不下几 KB 就是它卡住了。
WRITE_TIMEOUT = 10.0
#: 收尾那一下的上限。关连接不许为了"干净"再无限阻塞一次。
CLOSE_TIMEOUT = 2.0

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA


class WsError(RuntimeError):
    """协议层或连接层的失败。message 是成品文案,直接给用户看。"""


class WsTimeoutError(WsError):
    """A response deadline elapsed without evidence that the transport closed."""


def _mask(payload: bytes, key: bytes) -> bytes:
    """按 4 字节 key 异或 —— 客户端发出去的帧必须掩码(RFC 6455 §5.3)。

    **用大整数异或而不是逐字节循环**:服务端按 RFC 不该掩码,可真掩了我们也得
    解得开(容错比断言便宜),而那种帧可能是几 MB 的截图 —— 逐字节 comprehension
    在 4 MB 上是几百毫秒,`int` 的异或是 C 实现的一次操作。
    """
    if not payload:
        return payload
    n = len(payload)
    pad = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, 'big') ^ int.from_bytes(pad, 'big')).to_bytes(n, 'big')


def _frame(opcode: int, payload: bytes = b'') -> bytes:
    """一个完整的客户端帧:FIN=1 + 掩码。我们从不主动分片(命令都是小的)。"""
    n = len(payload)
    if n < 126:
        head = bytes([0x80 | opcode, 0x80 | n])
    elif n <= 0xFFFF:
        head = bytes([0x80 | opcode, 0x80 | 126]) + n.to_bytes(2, 'big')
    else:
        head = bytes([0x80 | opcode, 0x80 | 127]) + n.to_bytes(8, 'big')
    key = os.urandom(4)
    return head + key + _mask(payload, key)


class WebSocket:
    """一条客户端连接。用完要 `close()`(调用方多半是 `try/finally`)。"""

    def __init__(self, sock: socket.socket):
        # **socket 必须是非阻塞的,超时一律由 select 按 deadline 管。**
        # 光靠「select 等可写 + send」挡不住阻塞:BSD/macOS 的 `sosend` 对一条
        # 大载荷会在内核里**循环等到全部写完**,于是一次 `send(4 MB)` 在没人读
        # 的连接上照样无限挂住(2026-10-09 实测:测试进程卡死,要 kill)。
        # 非阻塞之下 `send` 只写得进多少写多少、立刻返回,deadline 才真的管事。
        sock.setblocking(False)
        self._sock = sock
        self._buf = bytearray()
        self._closed = False
        self._broken = False
        self._parts = []
        self._kind = OP_TEXT

    # ── 建立 ────────────────────────────────────────────────────────────────

    @classmethod
    def connect(cls, host: str, port: int, path: str, *,
                timeout: float = HANDSHAKE_TIMEOUT) -> 'WebSocket':
        """连上并完成升级握手。

        **Host 头必须是 `IP:端口`**:Chrome 的调试端口会校验它(防 DNS
        rebinding —— 一个网页把某个域名解析到 127.0.0.1 就能来敲这个端口),
        所以这里原样用调用方给的 host,不做任何反解。**不发 `Origin`** 同理:
        带着一个浏览器不认的 Origin 过去会被直接拒。
        """
        try:
            sock = socket.create_connection((host, port), timeout)
        except OSError as e:
            raise WsError(f'连不上 {host}:{port} —— {e}') from None
        deadline = time.monotonic() + timeout
        key = base64.b64encode(os.urandom(16))
        request = (f'GET {path or "/"} HTTP/1.1\r\n'
                   f'Host: {host}:{port}\r\n'
                   'Upgrade: websocket\r\n'
                   'Connection: Upgrade\r\n'
                   f'Sec-WebSocket-Key: {key.decode("ascii")}\r\n'
                   'Sec-WebSocket-Version: 13\r\n\r\n')
        ws = cls(sock)
        try:
            ws._write(request.encode('ascii'), deadline)
            ws._handshake(key, deadline)
        except BaseException:
            ws._shutdown()
            raise
        return ws

    def _handshake(self, key: bytes, deadline: float) -> None:
        head = self._read_until(b'\r\n\r\n', deadline)
        status, _, rest = head.partition(b'\r\n')
        parts = status.split(b' ')
        if len(parts) < 2 or parts[1] != b'101':
            raise WsError('浏览器拒绝了 WebSocket 升级:'
                          + status.decode('latin-1', 'replace'))
        want = base64.b64encode(hashlib.sha1(key + _GUID).digest()).decode('ascii')
        got = ''
        for line in rest.split(b'\r\n'):
            name, _, value = line.partition(b':')
            if name.strip().lower() == b'sec-websocket-accept':
                got = value.strip().decode('latin-1', 'replace')
        if got != want:
            raise WsError('WebSocket 握手校验没过(Sec-WebSocket-Accept 对不上)')

    # ── 收发 ────────────────────────────────────────────────────────────────

    def send(self, text: str, deadline: float) -> None:
        """发一条文本消息。`deadline` 是这条命令**整体**的截止时刻(含发送)。"""
        self._write(_frame(OP_TEXT, text.encode('utf-8')), deadline)

    def recv(self, deadline: float) -> str:
        """收下一条**完整**消息。分片拼起来,控制帧就地处理掉。

        ping 自动回 pong:Chrome 平时不 ping,但它要是 ping 了而我们不回,
        对端有权直接断开 —— 那会表现成抓取中途一次莫名的连接中断。
        """
        while True:
            fin, opcode, payload = self._read_frame(deadline)
            if opcode == OP_PING:
                self._write(_frame(OP_PONG, payload), deadline)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._closed = True
                try:
                    self._write(_frame(OP_CLOSE, payload[:2]),
                                min(deadline, time.monotonic() + CLOSE_TIMEOUT))
                except WsError:
                    pass                  # 对端已经走了,回不回都一样
                raise EOFError('浏览器关闭了 WebSocket 连接')
            if opcode in (OP_TEXT, OP_BINARY):
                if self._parts:
                    raise WsError('上一条 WebSocket 消息的分片还没结束')
                self._kind, self._parts = opcode, [payload]
            elif opcode == OP_CONT:
                if not self._parts:
                    raise WsError('收到一个没有开头的 WebSocket 分片')
                self._parts.append(payload)
            else:
                raise WsError(f'不认识的 WebSocket 帧类型(opcode={opcode})')
            if fin:
                if self._kind == OP_BINARY:
                    raise WsError('CDP 不该发二进制帧,连接可能已经错位')
                message = b''.join(self._parts).decode('utf-8')
                self._parts = []
                return message

    def _read_frame(self, deadline: float) -> tuple[bool, int, bytes]:
        def require(size):
            while len(self._buf) < size:
                self._fill(deadline)

        require(2)
        head = self._buf
        if head[0] & 0x70:
            raise WsError('浏览器用了没协商过的 WebSocket 扩展(RSV 位非零)')
        fin, opcode = bool(head[0] & 0x80), head[0] & 0x0F
        masked, n = bool(head[1] & 0x80), head[1] & 0x7F
        cursor = 2
        if n == 126:
            require(4)
            n = int.from_bytes(head[2:4], 'big')
            cursor = 4
        elif n == 127:
            require(10)
            n = int.from_bytes(head[2:10], 'big')
            cursor = 10
        if n > MAX_PAYLOAD:
            raise WsError(f'WebSocket 帧超长({n} 字节),连接可能已经错位')
        if opcode >= 0x8 and (not fin or n > 125):
            raise WsError('WebSocket 控制帧不许分片、也不许超过 125 字节')
        require(cursor + (4 if masked else 0) + n)
        key = bytes(head[cursor:cursor + 4]) if masked else b''
        cursor += len(key)
        payload = bytes(head[cursor:cursor + n])
        del self._buf[:cursor + n]
        return fin, opcode, _mask(payload, key) if key else payload

    # ── 字节层 ──────────────────────────────────────────────────────────────

    def _write(self, data: bytes, deadline: float) -> None:
        """按 deadline 写完,写不动就废掉这条连接(见模块注释)。

        上限取「这条命令的 deadline」和 `WRITE_TIMEOUT` 的小者:前者让
        `call(timeout=0.05)` 真的在 0.05 秒失败,后者兜住压根没给 deadline 的
        那些路(收尾、pong)。
        """
        if self._broken:
            raise WsError('这条浏览器连接已经废了(上一次没写完,帧错位了)')
        view = memoryview(data)
        limit = min(deadline, time.monotonic() + WRITE_TIMEOUT)
        while view:
            left = limit - time.monotonic()
            if left <= 0 or not select.select([], [self._sock], [], left)[1]:
                # 半截帧已经出去了 —— 这条连接从此不可用,当场断掉比留着错位好。
                self._broken = self._closed = True
                self._shutdown()
                raise WsError('浏览器没有在预期时间内收下命令(连接已断开)')
            try:
                sent = self._sock.send(view)
            except BlockingIOError:
                continue                  # select 说可写、内核又改主意了,重算 deadline
            except OSError as e:
                self._broken = True
                raise WsError(f'写浏览器连接出错:{e}') from None
            view = view[sent:]

    def _fill(self, deadline: float) -> None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise WsTimeoutError('浏览器没有在预期时间内响应')
        if not select.select([self._sock], [], [], left)[0]:
            return                         # 超时由上面那句判,这里只管有没有数据
        try:
            chunk = self._sock.recv(_CHUNK)
        except BlockingIOError:
            return                        # 非阻塞下的空读:下一轮重新 select
        except OSError as e:
            raise WsError(f'读浏览器连接出错:{e}') from None
        if not chunk:
            raise EOFError('浏览器已断开 WebSocket 连接')
        self._buf += chunk

    def _read_exact(self, n: int, deadline: float) -> bytes:
        while len(self._buf) < n:
            self._fill(deadline)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _read_until(self, sep: bytes, deadline: float) -> bytes:
        while sep not in self._buf:
            if len(self._buf) > _MAX_HEAD:
                raise WsError('对端返回的 HTTP 响应头超长,这个端口可能不是调试端口')
            self._fill(deadline)
        end = self._buf.find(sep) + len(sep)
        out = bytes(self._buf[:end])
        del self._buf[:end]
        return out

    # ── 收尾 ────────────────────────────────────────────────────────────────

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._write(_frame(OP_CLOSE, (1000).to_bytes(2, 'big')),
                            time.monotonic() + CLOSE_TIMEOUT)
            except WsError:
                pass                      # 对端已经走了或者卡住了,收尾不等它
        self._shutdown()

    def _shutdown(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
