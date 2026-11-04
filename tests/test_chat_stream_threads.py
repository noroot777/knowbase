# -*- coding: utf-8 -*-
"""流式问答的那条 kb.db 连接要扛得住「开在一个线程、收尾在另一个线程」。

`api_ask_stream` 在线程池线程里开连接,StreamingResponse 再用 `iterate_in_threadpool`
推进同步生成器 —— 每一次 next() 各借一个线程。agent 研究期间别的请求把原来那个
线程占走,收尾 `_save_qa` 就落在另一个线程上;连接默认的同线程检查当场抛
ProgrammingError,流被砍断(浏览器只报 network error),这一轮也没落库。

这里把「开连接」和「推进生成器」**确定地**放在两个不同线程上,断言后果:收到
`done` 帧、库里真有这一轮。反过来钉一边:`store.connect()` 默认仍然带同线程
检查,放开的只是流式问答这一条。

跑:  python -m unittest tests.test_chat_stream_threads -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import store                     # noqa: E402
from kb.web import chat_api              # noqa: E402

ANSWER = '跨线程收尾的这一轮也要落库'


def _stub_stream(conn, q, model='', history=None):
    # 生成过程中也碰一次连接,跟真的检索一样
    conn.execute('SELECT 1').fetchone()
    yield {'type': 'meta', 'refs': [], 'plan': None}
    yield {'type': 'delta', 'text': ANSWER}


class StreamConnAcrossThreadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'kb.db'
        real_connect = store.connect
        self.saved = (chat_api.store.connect, chat_api.run_ask_stream,
                      chat_api.StreamingResponse)
        chat_api.store.connect = lambda **kw: real_connect(self.db, **kw)
        chat_api.run_ask_stream = _stub_stream
        chat_api.StreamingResponse = lambda content, media_type=None: content
        self.real_connect = real_connect

    def tearDown(self):
        (chat_api.store.connect, chat_api.run_ask_stream,
         chat_api.StreamingResponse) = self.saved
        self.tmp.cleanup()

    def test_stream_finishes_on_another_thread(self):
        body = chat_api.AskRequest(query='问一句', model='stub')
        with ThreadPoolExecutor(1) as opener, ThreadPoolExecutor(1) as runner:
            gen = opener.submit(chat_api.api_ask_stream, body).result()
            frames = runner.submit(lambda: [json.loads(x) for x in gen]).result()
        self.assertEqual([f['type'] for f in frames], ['meta', 'delta', 'done'])
        done = frames[-1]
        conn = self.real_connect(self.db)
        try:
            row = conn.execute('SELECT content FROM messages WHERE id=?',
                               (done['message_id'],)).fetchone()
            convs = conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(row['content'], ANSWER)
        self.assertEqual(convs, 1)

    def test_default_connect_still_checks_thread(self):
        conn = self.real_connect(self.db)
        try:
            with ThreadPoolExecutor(1) as other:
                fut = other.submit(lambda: conn.execute('SELECT 1').fetchone())
                with self.assertRaises(sqlite3.ProgrammingError):
                    fut.result()
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
