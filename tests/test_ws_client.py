# -*- coding: utf-8 -*-
"""`kb/feeds/ws_client.py` 的回归:帧编解码 + 升级握手。

**对端是真 socket**(`tests/ws_stub.py`),因为要验的就是这两层 —— 把
`WebSocket` 打桩掉正好绕开唯一要验的那件事。每条用例关心的是一种具体形状:
分片、控制帧、粘包、错位的长度字段。
"""

from __future__ import annotations

import time
import unittest

from kb.feeds import ws_client as W
from tests import ws_stub as S


def deadline(seconds: float = 5.0) -> float:
    return time.monotonic() + seconds


class HandshakeTests(unittest.TestCase):

    def test_request_carries_the_four_required_headers(self):
        """Host 必须是 IP:端口(Chrome 拿它防 DNS rebinding),且不发 Origin。"""
        def handler(conn, box):
            box['request'] = S.handshake(conn)
            conn.sendall(S.frame(W.OP_TEXT, b'hi'))
        with S.serving(handler) as (port, box):
            ws = W.WebSocket.connect('127.0.0.1', port, '/devtools/browser/abc')
            try:
                self.assertEqual(ws.recv(deadline()), 'hi')
            finally:
                ws.close()
        request = box['request']
        self.assertIn(b'GET /devtools/browser/abc HTTP/1.1', request)
        self.assertIn(f'Host: 127.0.0.1:{port}'.encode(), request)
        self.assertIn(b'Upgrade: websocket', request)
        self.assertIn(b'Sec-WebSocket-Version: 13', request)
        self.assertNotIn(b'Origin:', request)
        # 不协商扩展 —— 协商了才可能收到 RSV1 置位的压缩帧(见模块注释)。
        self.assertNotIn(b'Sec-WebSocket-Extensions', request)

    def test_a_frame_glued_to_the_response_head_is_not_lost(self):
        """响应头和第一帧在同一个包里时,第一条消息照样读得到。

        这是最阴的一种:丢掉它表现成一次莫名的超时,而 CDP 的第一条消息往往
        正是我们在等的那条回复。
        """
        def handler(conn, box):
            S.handshake(conn, trailer=S.frame(W.OP_TEXT, b'{"id":1}'))
        with S.serving(handler) as (port, _):
            ws = W.WebSocket.connect('127.0.0.1', port, '/x')
            try:
                self.assertEqual(ws.recv(deadline()), '{"id":1}')
            finally:
                ws.close()

    def test_non_101_status_is_refused(self):
        def handler(conn, box):
            S.handshake(conn, status=b'404 Not Found')
        with S.serving(handler) as (port, _):
            with self.assertRaises(W.WsError) as caught:
                W.WebSocket.connect('127.0.0.1', port, '/x')
        self.assertIn('404', str(caught.exception))

    def test_wrong_accept_token_is_refused(self):
        """accept 对不上 = 对端不是个 WebSocket(或者有东西在中间)。"""
        def handler(conn, box):
            S.handshake(conn, accept=b'definitely-not-it')
        with S.serving(handler) as (port, _):
            with self.assertRaises(W.WsError) as caught:
                W.WebSocket.connect('127.0.0.1', port, '/x')
        self.assertIn('Sec-WebSocket-Accept', str(caught.exception))

    def test_nothing_listening_says_so(self):
        import socket as _socket
        probe = _socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(W.WsError) as caught:
            W.WebSocket.connect('127.0.0.1', port, '/x', timeout=2)
        self.assertIn('连不上', str(caught.exception))


class FrameTests(unittest.TestCase):

    def _echo_once(self, handler, send: str = '') -> dict:
        """连上、(可选)发一条、收一条。返回 box。"""
        with S.serving(handler) as (port, box):
            ws = W.WebSocket.connect('127.0.0.1', port, '/x')
            try:
                if send:
                    ws.send(send)
                box['got'] = ws.recv(deadline())
            finally:
                ws.close()
            return box

    def test_client_frames_are_masked_and_round_trip(self):
        """客户端方向必须掩码(桩里断言了),内容逐字到达。"""
        def handler(conn, box):
            S.handshake(conn)
            _fin, _op, payload = S.read_frame(conn)
            box['sent'] = payload.decode('utf-8')
            conn.sendall(S.frame(W.OP_TEXT, b'ok'))
        box = self._echo_once(handler, send='{"method":"Runtime.evaluate","中文":1}')
        self.assertEqual(box['sent'], '{"method":"Runtime.evaluate","中文":1}')
        self.assertEqual(box['got'], 'ok')
        self.assertNotIn('error', box)

    def test_fragmented_message_is_joined(self):
        """三帧拼成一条消息。CDP 的大回包(截图)就是这个形状。"""
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(W.OP_TEXT, b'{"a":', fin=False))
            conn.sendall(S.frame(W.OP_CONT, b'1,"b":', fin=False))
            conn.sendall(S.frame(W.OP_CONT, b'2}'))
        self.assertEqual(self._echo_once(handler)['got'], '{"a":1,"b":2}')

    def test_ping_is_answered_with_pong_before_the_message(self):
        """不回 pong 对端有权直接断开 —— 那会表现成抓取中途一次莫名的中断。"""
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(W.OP_PING, b'ping-payload'))
            _fin, opcode, payload = S.read_frame(conn)
            box['answer'] = (opcode, payload)
            conn.sendall(S.frame(W.OP_TEXT, b'after-pong'))
        box = self._echo_once(handler)
        self.assertEqual(box['answer'], (W.OP_PONG, b'ping-payload'))
        self.assertEqual(box['got'], 'after-pong')

    def test_unsolicited_pong_is_ignored(self):
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(W.OP_PONG, b''))
            conn.sendall(S.frame(W.OP_TEXT, b'still-here'))
        self.assertEqual(self._echo_once(handler)['got'], 'still-here')

    def test_close_frame_becomes_eof(self):
        """EOFError 而不是 WsError:调用方(x_browser)把"浏览器走了"和协议错
        分开处理,前者是一次可重试的会话中断。"""
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(W.OP_CLOSE, (1001).to_bytes(2, 'big')))
            try:
                box['echo'] = S.read_frame(conn)[1]
            except (EOFError, OSError):
                box['echo'] = None
        with S.serving(handler) as (port, box):
            ws = W.WebSocket.connect('127.0.0.1', port, '/x')
            try:
                with self.assertRaises(EOFError):
                    ws.recv(deadline())
            finally:
                ws.close()
        self.assertEqual(box.get('echo'), W.OP_CLOSE)   # 关闭握手回了一帧

    def test_a_masked_server_frame_is_still_decoded(self):
        """服务端按 RFC 不该掩码,真掩了也得解得开 —— 容错比断言便宜。"""
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(W.OP_TEXT, b'masked-anyway', mask=b'\x01\x02\x03\x04'))
        self.assertEqual(self._echo_once(handler)['got'], 'masked-anyway')

    def test_large_message_crosses_the_64bit_length_branch(self):
        """70 KB 走 16 位分支、70 万走 64 位分支。截图回包就是这个量级。"""
        for size in (70_000, 700_000):
            with self.subTest(size=size):
                blob = ('x' * size).encode()

                def handler(conn, box, blob=blob):
                    S.handshake(conn)
                    conn.sendall(S.frame(W.OP_TEXT, blob))
                self.assertEqual(len(self._echo_once(handler)['got']), size)


class ProtocolErrorTests(unittest.TestCase):

    def _expect_error(self, handler, needle: str) -> None:
        with S.serving(handler) as (port, _):
            ws = W.WebSocket.connect('127.0.0.1', port, '/x')
            try:
                with self.assertRaises(W.WsError) as caught:
                    ws.recv(deadline())
            finally:
                ws.close()
        self.assertIn(needle, str(caught.exception))

    def test_rsv_bit_set_is_a_protocol_error(self):
        """我们没协商压缩,所以 RSV 置位只能是对端在用我们不懂的扩展。"""
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(W.OP_TEXT, b'compressed?', rsv=1))
        self._expect_error(handler, 'RSV')

    def test_fragmented_control_frame_is_refused(self):
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(W.OP_PING, b'nope', fin=False))
        self._expect_error(handler, '控制帧')

    def test_oversized_length_is_refused_instead_of_allocated(self):
        """错位之后长度字段会读出一个天文数字 —— 不设上限就按它分配内存了。"""
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.long_frame(W.OP_TEXT, W.MAX_PAYLOAD + 1))
        self._expect_error(handler, '超长')

    def test_continuation_without_a_start_is_refused(self):
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(W.OP_CONT, b'orphan'))
        self._expect_error(handler, '没有开头')

    def test_binary_frame_is_refused(self):
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(W.OP_BINARY, b'\x00\x01'))
        self._expect_error(handler, '二进制')

    def test_unknown_opcode_is_refused(self):
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(0x5, b'what'))
        self._expect_error(handler, 'opcode')

    def test_silence_times_out_rather_than_hanging(self):
        """读永远带 deadline(理由同 embed_pool 那次事故:攥着锁阻塞的 read 会
        把一整条链冻住)。"""
        def handler(conn, box):
            S.handshake(conn)
            time.sleep(3)
        with S.serving(handler) as (port, _):
            ws = W.WebSocket.connect('127.0.0.1', port, '/x')
            started = time.monotonic()
            try:
                with self.assertRaises(W.WsError) as caught:
                    ws.recv(time.monotonic() + 0.4)
                # **在块里量**:退出 `serving` 要等那个 sleep(3) 的线程 join,
                # 算进去的话这条断言量的是桩的收尾、不是 recv 的返回。
                elapsed = time.monotonic() - started
            finally:
                ws.close()
        self.assertIn('预期时间内', str(caught.exception))
        self.assertLess(elapsed, 2.0)

    def test_peer_vanishing_mid_stream_is_eof(self):
        def handler(conn, box):
            S.handshake(conn)
            conn.sendall(S.frame(W.OP_TEXT, b'{', fin=False))
            conn.close()
        with S.serving(handler) as (port, _):
            ws = W.WebSocket.connect('127.0.0.1', port, '/x')
            try:
                with self.assertRaises(EOFError):
                    ws.recv(deadline())
            finally:
                ws.close()


class MaskTests(unittest.TestCase):
    """`_mask` 走的是大整数异或(大 payload 上逐字节 comprehension 太慢),
    所以它的正确性要单独钉:自反、空输入、长度不是 4 的倍数。"""

    def test_mask_is_its_own_inverse(self):
        key = b'\xde\xad\xbe\xef'
        for payload in (b'', b'a', b'abc', b'abcd', b'abcde', bytes(range(256)) * 7):
            with self.subTest(n=len(payload)):
                once = W._mask(payload, key)
                self.assertEqual(len(once), len(payload))
                self.assertEqual(W._mask(once, key), payload)

    def test_mask_matches_the_byte_by_byte_definition(self):
        key = b'\x01\x02\x03\x04'
        payload = bytes(range(37))
        want = bytes(b ^ key[i & 3] for i, b in enumerate(payload))
        self.assertEqual(W._mask(payload, key), want)


if __name__ == '__main__':
    unittest.main()
