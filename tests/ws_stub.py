# -*- coding: utf-8 -*-
"""一个按脚本动作的 WebSocket 服务端,给 `test_ws_client` 当对端。

**是真 socket,不是打桩的 `WebSocket` 类**:要验的正是帧编解码和握手这两层,
把它们桩掉等于什么都没验。每条用例自己写 handler,决定这一次回什么 ——
"回一个坏的 accept"、"把响应头和第一帧粘在一个包里"这类形状只有在字节层才
摆得出来。
"""

from __future__ import annotations

import base64
import hashlib
import re
import select
import socket
import threading
import time
from contextlib import contextmanager

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def accept_key(request: bytes) -> bytes:
    key = re.search(rb'Sec-WebSocket-Key:\s*(\S+)', request).group(1)
    return base64.b64encode(hashlib.sha1(key + GUID).digest())


def handshake(conn: socket.socket, *, accept: bytes | None = None,
              status: bytes = b'101 Switching Protocols', trailer: bytes = b'') -> bytes:
    """读客户端的升级请求,回一个响应。返回那份请求以便断言。

    `trailer` 跟在响应头后面**同一次 sendall** 里发出去 —— 那是"响应头和第一个
    数据帧粘在一个 TCP 包里"这个真实形状,也是 `_read_until` 必须把多读的字节
    留在缓冲里的理由。
    """
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return buf
        buf += chunk
    token = accept_key(buf) if accept is None else accept
    conn.sendall(b'HTTP/1.1 ' + status + b'\r\nUpgrade: websocket\r\n'
                 b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + token
                 + b'\r\n\r\n' + trailer)
    return buf


def frame(opcode: int, payload: bytes = b'', *, fin: bool = True, rsv: int = 0,
          mask: bytes | None = None) -> bytes:
    """服务端方向的一帧。按 RFC 服务端不掩码,但 `mask=` 能摆出掩码的那种。"""
    first = (0x80 if fin else 0) | (rsv << 4) | opcode
    n = len(payload)
    flag = 0x80 if mask else 0
    if n < 126:
        head = bytes([first, flag | n])
    elif n <= 0xFFFF:
        head = bytes([first, flag | 126]) + n.to_bytes(2, 'big')
    else:
        head = bytes([first, flag | 127]) + n.to_bytes(8, 'big')
    if mask is None:
        return head + payload
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return head + mask + masked


def long_frame(opcode: int, declared: int) -> bytes:
    """只有帧头、声明一个超大长度 —— 用来摆"连接错位"那一档。"""
    return bytes([0x80 | opcode, 127]) + declared.to_bytes(8, 'big')


def read_frame(conn: socket.socket) -> tuple[bool, int, bytes]:
    """读客户端发来的一帧并解掩码。客户端**必须**掩码,这里顺带断言它掩了。"""
    def exact(n: int) -> bytes:
        out = b''
        while len(out) < n:
            chunk = conn.recv(n - len(out))
            if not chunk:
                raise EOFError('客户端断开了')
            out += chunk
        return out
    head = exact(2)
    fin, opcode = bool(head[0] & 0x80), head[0] & 0x0F
    masked, n = bool(head[1] & 0x80), head[1] & 0x7F
    if n == 126:
        n = int.from_bytes(exact(2), 'big')
    elif n == 127:
        n = int.from_bytes(exact(8), 'big')
    assert masked, '客户端发出去的帧没有掩码(RFC 6455 §5.3 要求)'
    key = exact(4)
    payload = exact(n)
    return fin, opcode, bytes(b ^ key[i & 3] for i, b in enumerate(payload))


@contextmanager
def serving(handler):
    """起一个只接一条连接的 server。yield (port, box),box 给 handler 回传东西。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    box: dict = {}

    def run() -> None:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            handler(conn, box)
        except Exception as e:                        # noqa: BLE001 - 留给断言看
            box['error'] = repr(e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield port, box
    finally:
        srv.close()
        thread.join(timeout=5)


@contextmanager
def stuck_pair():
    """一对真 socket,其中**我们那一头写不进去**:对端连上了但永远不读。

    扮演的是"卡住的浏览器"。灌满内核缓冲之后任何一次写都只能等对端来读 ——
    原来那条裸 `sendall` 在这里**无限**阻塞,而这条链的上游是采集阶段。

    **「灌满」要连着几轮都不可写才算数,不能一轮就收手。** 实测:刚报过
    `EAGAIN` 的 socket 过 0.3 秒又可写了(TCP 还在把数据搬去对端的接收缓冲),
    那时一条几十字节的 CDP 命令照样挤得进去 —— 用这种半满的局摆出来的用例,
    失败来自随后的读超时,对写路径压根不敏感。连着 `_QUIET` 轮不可写之后它才
    真的停住(实测灌进去 ~514 KB,之后连续三秒都写不动)。

    返回 `(我们这头, 对端)`;两头都在退出时关掉。
    """
    quiet_rounds, slice_seconds, cap_seconds = 3, 0.1, 5.0
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    mine = socket.create_connection(srv.getsockname())
    peer, _ = srv.accept()
    srv.close()
    try:
        mine.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        mine.setblocking(False)
        quiet, started = 0, time.monotonic()
        while time.monotonic() - started < cap_seconds and quiet < quiet_rounds:
            if select.select([], [mine], [], slice_seconds)[1]:
                quiet = 0
                try:
                    mine.send(b'x' * 4096)
                except BlockingIOError:
                    pass
            else:
                quiet += 1
        yield mine, peer
    finally:
        for sock in (peer, mine):
            try:
                sock.close()
            except OSError:
                pass
