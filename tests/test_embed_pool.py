# -*- coding: utf-8 -*-
"""编码子进程协议层的回归测试。

用 stub worker(见 tests/_stub_worker.py)跑,不加载真模型——协议归协议,
模型归模型,拿 3 GB 的 bge-m3 来测帧格式没道理,也没人愿意跑。

钉住的核心不变量:**向量帧的 4 字节长度前缀必须被吃掉**。第一版把向量当裸
字节按 n*dim*4 读,前缀被当成第一个 float,整批向量平移 4 字节(表现为余弦
相似度从 1.0 掉到 0.0),而且多出来的 4 字节留在管道里,把下一个请求的帧同步
也带歪。单条请求的形状检查发现不了它——值是错的,形状是对的。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import struct
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import embed_pool  # noqa: E402

STUB = str(Path(__file__).resolve().parent / "_stub_worker.py")
DIM = 8


def expected(text: str) -> list[float]:
    """stub 的产向量规则,与 _stub_worker.py 里保持一致。"""
    return [float(len(text) + i) for i in range(DIM)]


def decode(dim: int, blob: bytes) -> list[list[float]]:
    n = len(blob) // (dim * 4)
    unpack = struct.Struct(f"<{dim}f").unpack_from
    return [list(unpack(blob, i * dim * 4)) for i in range(n)]


class EmbedPoolProtocolTests(unittest.TestCase):

    def setUp(self) -> None:
        self._orig_cmd = embed_pool._WORKER_CMD
        embed_pool._WORKER_CMD = [sys.executable, STUB]
        self.pool = embed_pool._Pool()

    def tearDown(self) -> None:
        self.pool.shutdown()
        embed_pool._WORKER_CMD = self._orig_cmd

    def test_single_request_not_shifted(self) -> None:
        """最直接的回归:第一个 float 必须是真数据,不是长度前缀。"""
        dim, blob = self.pool.encode(["abc", "de"], 32)
        self.assertEqual(DIM, dim)
        self.assertEqual([expected("abc"), expected("de")], decode(dim, blob))

    def test_blob_length_is_exact(self) -> None:
        """字节数必须正好 n×dim×4,多一个都说明帧读歪了。"""
        dim, blob = self.pool.encode(["x"] * 5, 32)
        self.assertEqual(5 * DIM * 4, len(blob))

    def test_frame_sync_survives_many_requests(self) -> None:
        """残留字节会在下一个请求上暴露,所以连着打多次、每次条数还不同。"""
        for n in (1, 7, 3, 12, 2, 9):
            with self.subTest(n=n):
                dim, blob = self.pool.encode([f"t{i}" for i in range(n)], 32)
                self.assertEqual([expected(f"t{i}") for i in range(n)],
                                 decode(dim, blob))

    def test_worker_error_is_reported_not_swallowed(self) -> None:
        """worker 侧编码失败要抛出来,而且不能把管道搞乱。"""
        with self.assertRaisesRegex(embed_pool.EmbedError, "编码失败"):
            self.pool.encode(["__BOOM__"], 32)
        dim, blob = self.pool.encode(["ok"], 32)   # 还活着,后续请求照常
        self.assertEqual([expected("ok")], decode(dim, blob))

    def test_encode_error_is_a_runtime_error(self) -> None:
        """search.py 的向量腿靠 `except RuntimeError` 降级成纯 FTS。

        EmbedError 一旦不再是 RuntimeError,一次编码失败就会把整个搜索请求
        打成 500,而不是安静地退回关键词检索。
        """
        self.assertTrue(issubclass(embed_pool.EmbedError, RuntimeError))
        self.assertTrue(issubclass(embed_pool.EmbedWorkerError, RuntimeError))
        # 且编码失败不该被当成管道故障去重起 worker
        self.assertFalse(issubclass(embed_pool.EmbedError,
                                    embed_pool.EmbedWorkerError))

    def test_respawns_after_worker_dies(self) -> None:
        self.pool.encode(["warmup"], 32)
        proc = self.pool._proc
        proc.kill()
        proc.wait(timeout=5)
        dim, blob = self.pool.encode(["after"], 32)
        self.assertEqual([expected("after")], decode(dim, blob))

    def test_shutdown_reaps_process(self) -> None:
        self.pool.encode(["warmup"], 32)
        proc = self.pool._proc
        self.pool.shutdown()
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(self.pool._proc)

    def test_shutdown_is_idempotent(self) -> None:
        self.pool.shutdown()
        self.pool.shutdown()

    def test_concurrent_callers_do_not_interleave(self) -> None:
        """kb-web 是多线程的:管道是字节流,两个请求交错写进去就全乱了。"""
        import threading
        errs: list[Exception] = []

        def hit() -> None:
            try:
                for _ in range(3):
                    dim, blob = self.pool.encode(["锚点", "第二条"], 32)
                    assert decode(dim, blob) == [expected("锚点"),
                                                 expected("第二条")]
            except Exception as e:      # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=hit) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual([], errs)


class EmbedPoolHangGuardTests(unittest.TestCase):
    """错位/卡死必须变成可恢复的错误,而不是永久阻塞。

    父进程是攥着 _lock 阻塞的,一旦卡住,kb-web 的搜索和索引一起冻住,除了重启
    没救。这两条是把「会 hang」钉成「会报错」。
    """

    def setUp(self) -> None:
        self._orig_cmd = embed_pool._WORKER_CMD
        embed_pool._WORKER_CMD = [sys.executable, STUB]
        self.pool = embed_pool._Pool()

    def tearDown(self) -> None:
        self.pool.shutdown()
        embed_pool._WORKER_CMD = self._orig_cmd

    def test_absurd_frame_length_is_rejected_not_awaited(self) -> None:
        """stub 回一个声称几 GB 的帧:必须当场判错位,不能傻等。"""
        with self.assertRaisesRegex(embed_pool.EmbedWorkerError, "超过上限"):
            self.pool.encode(["__HUGEFRAME__"], 32)

    def test_silent_worker_times_out(self) -> None:
        """worker 收了请求就装死:到点必须超时抛错,不能无限阻塞。"""
        orig = embed_pool.EMBED_REQUEST_TIMEOUT
        embed_pool.EMBED_REQUEST_TIMEOUT = 1.0
        try:
            t0 = time.monotonic()
            with self.assertRaisesRegex(embed_pool.EmbedWorkerError, "超过"):
                self.pool.encode(["__SILENT__"], 32)
            self.assertLess(time.monotonic() - t0, 30, "超时没生效,卡住了")
        finally:
            embed_pool.EMBED_REQUEST_TIMEOUT = orig


class EmbedPoolLifecycleTests(unittest.TestCase):
    def test_idle_reaper_kills_worker(self) -> None:
        orig_cmd = embed_pool._WORKER_CMD
        orig_idle = embed_pool.EMBED_IDLE_SECONDS
        orig_interval = embed_pool._REAP_INTERVAL
        embed_pool._WORKER_CMD = [sys.executable, STUB]
        embed_pool.EMBED_IDLE_SECONDS = 0.2
        embed_pool._REAP_INTERVAL = 0.1
        pool = embed_pool._Pool()
        try:
            pool.encode(["warmup"], 32)
            proc = pool._proc
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and pool._proc is not None:
                time.sleep(0.1)
            self.assertIsNone(pool._proc, "空闲超时后 worker 没被收掉")
            self.assertIsNotNone(proc.poll())
            self.assertEqual(DIM, pool.encode(["again"], 32)[0])  # 还能重起
        finally:
            pool.shutdown()
            embed_pool._WORKER_CMD = orig_cmd
            embed_pool.EMBED_IDLE_SECONDS = orig_idle
            embed_pool._REAP_INTERVAL = orig_interval

    def test_worker_exits_when_stdin_closes(self) -> None:
        """父进程一死 worker 必须跟着走,否则会攒出一堆几 GB 的孤儿。"""
        proc = subprocess.Popen([sys.executable, STUB],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        proc.stdin.close()
        self.assertEqual(0, proc.wait(timeout=10))
        proc.stdout.close()


class InsertVectorsTests(unittest.TestCase):
    """store.insert_vectors 要能直接吃子进程回来的裸字节。"""

    def test_bytes_and_floats_produce_same_row(self) -> None:
        from kb import store

        vec = [0.5, -0.25, 1.0, 0.0]
        blob = struct.pack("<4f", *vec)
        conn = __import__("sqlite3").connect(":memory:")
        conn.execute("CREATE TABLE chunk_vec (chunk_id INTEGER PRIMARY KEY, "
                     "embedding BLOB)")
        store.insert_vectors(conn, [1], [vec])
        store.insert_vectors(conn, [2], [blob])
        rows = dict(conn.execute("SELECT chunk_id, embedding FROM chunk_vec"))
        self.assertEqual(rows[1], rows[2])
        conn.close()


if __name__ == "__main__":
    unittest.main()
