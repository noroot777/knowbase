# -*- coding: utf-8 -*-
"""编码子进程的父进程侧:起它、锁它、喂它、到点收它。

为什么模型不留在本进程里,见 kb/embed_worker.py 的模块注释(一句话:MPS 的显存
池只涨不退,进程内怎么都收不回,只有进程边界能真正还给系统)。

## 生命周期

懒起:第一次编码请求才 spawn,冷启动实测 3.2 秒(import 1.8 + 加载 1.3 + 首次
编码 0.1)。空闲 `EMBED_IDLE_SECONDS` 秒由守护线程收掉;批量任务结束时调用方
可以 `shutdown()` 立刻收,不用等那几分钟。收掉之后下次再用会重新 spawn。

子进程 stdin 读到 EOF 就退出,所以父进程无论怎么死(包括 SIGKILL、包括 kb.sh
的 kill -INT)都不会留下孤儿。反过来子进程用 `start_new_session=True` 独立成会话,
不会被打给父进程组的信号在编码半途打断。

## 协议

父 → 子:4 字节小端长度 + JSON,`{"op": "encode", "texts": [...], "batch_size": N}`
子 → 父:4 字节长度 + JSON 头;头里 ok=true 时紧跟第二帧,是 n×dim 个 float32
的裸字节。走裸字节而不是 JSON 数字数组,是因为批量索引一次要回 128×1024 个 float,
JSON 编解码比编码本身还贵。
"""

from __future__ import annotations

import atexit
import json
import os
import select
import struct
import subprocess
import sys
import threading
import time

from kb.config import EMBED_IDLE_SECONDS, EMBED_REQUEST_TIMEOUT

_LEN = struct.Struct("<I")
_REAP_INTERVAL = 30.0
# 起 worker 的命令。单独拎成常量是为了测试能换成 stub worker——协议层的回归
# 测试没必要真去加载 3 GB 的模型。
_WORKER_CMD = [sys.executable, "-m", "kb.embed_worker"]
# 帧长上限。正常最大的一帧是一批向量:128 条 × 1024 维 × 4 字节 = 512 KB。
# 定这个上限不是为了省内存,是为了**把管道错位变成可恢复的错误**:一旦帧对不齐,
# 读到的"长度"其实是别的什么字节,往往是个天文数字,然后 read 就永远阻塞在那儿——
# 而且是攥着 _lock 阻塞的,搜索和索引一起冻住,只能重启 kb-web。超限直接判错位,
# 收掉 worker 重来一次。
_MAX_FRAME = 64 * 1024 * 1024


class EmbedWorkerError(RuntimeError):
    """子进程起不来或答不上话(管道层面的故障)。会触发收掉重起 + 重试一次。"""


class EmbedError(RuntimeError):
    """worker 活着,但这次编码本身失败了(通常是 torch 抛的)。不重试。

    刻意继承 RuntimeError:`search.py` 的向量腿是 `except RuntimeError` 降级成
    纯 FTS 的,换成别的基类就会让一次编码失败把整个搜索请求打成 500。
    """


class _Pool:
    """单个 worker 的持有者。全模块一个实例,内部用锁串行化管道读写。

    kb-web 是多线程的(uvicorn 的请求线程 + 管道自己那条线程),同一时刻可能
    既在跑 index 又有人在搜索。管道是有状态的字节流,两个请求交错写进去就全乱了,
    所以所有收发都在 `_lock` 里做——编码本身是 GPU 串行的,排队并不损失吞吐。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._last_use = 0.0
        self._reaper: threading.Thread | None = None

    # ── 进程生命周期 ──────────────────────────────────────────────────────

    def _spawn(self) -> subprocess.Popen:
        try:
            proc = subprocess.Popen(
                list(_WORKER_CMD),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                # stderr 不接管:worker 的日志和 traceback 直接落到 kb-web 的
                # 日志里,出问题时看得见。接管了又不读,反而会在管道写满时卡死。
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                start_new_session=True,
            )
        except Exception as e:
            raise EmbedWorkerError(f"编码子进程起不来: {e}") from e
        self._proc = proc
        self._ensure_reaper()
        return proc

    def _kill(self) -> None:
        """就地收掉当前 worker。调用方必须已持有 _lock。"""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()      # EOF:worker 自己会干净退出
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()             # 卡在一次超长编码里,不等了
                proc.wait(timeout=5)
        except Exception:
            pass
        finally:
            for f in (proc.stdout, proc.stdin):
                try:
                    if f and not f.closed:
                        f.close()
                except Exception:
                    pass

    def _ensure_reaper(self) -> None:
        if EMBED_IDLE_SECONDS <= 0 or (self._reaper and self._reaper.is_alive()):
            return
        t = threading.Thread(target=self._reap_loop, name="embed-reaper",
                             daemon=True)
        self._reaper = t
        t.start()

    def _reap_loop(self) -> None:
        while True:
            time.sleep(_REAP_INTERVAL)
            with self._lock:
                if self._proc is None:
                    return          # 已经没了,线程随之结束;下次 spawn 再起一条
                if time.monotonic() - self._last_use >= EMBED_IDLE_SECONDS:
                    self._kill()
                    return

    def shutdown(self) -> None:
        """立刻收掉 worker,把显存还给系统。空闲时调用是幂等的。"""
        with self._lock:
            self._kill()

    # ── 请求 ──────────────────────────────────────────────────────────────

    def encode(self, texts: list[str], batch_size: int) -> tuple[int, bytes]:
        """返回 (dim, n×dim 个 float32 的裸字节)。

        管道坏掉(worker 被 OOM killer 收了、上一次请求打断了帧同步)时重起一次
        再试:这类故障重试就好了,没必要把一整轮索引赔进去。第二次仍失败才抛。
        """
        payload = json.dumps({"op": "encode", "texts": texts,
                              "batch_size": batch_size}).encode()
        with self._lock:
            for attempt in (1, 2):
                proc = self._proc
                if proc is None or proc.poll() is not None:
                    self._kill()
                    proc = self._spawn()
                try:
                    return self._roundtrip(proc, payload)
                except EmbedWorkerError:
                    self._kill()
                    if attempt == 2:
                        raise
                finally:
                    self._last_use = time.monotonic()
            raise EmbedWorkerError("unreachable")

    def _roundtrip(self, proc: subprocess.Popen, payload: bytes) -> tuple[int, bytes]:
        deadline = time.monotonic() + EMBED_REQUEST_TIMEOUT
        try:
            proc.stdin.write(_LEN.pack(len(payload)))
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as e:
            raise EmbedWorkerError(f"写请求失败: {e}") from e

        head = _read_frame(proc.stdout, deadline)
        if head is None:
            raise EmbedWorkerError("编码子进程没回话就退出了(看 kb-web 日志里它的 stderr)")
        try:
            meta = json.loads(head)
        except ValueError as e:
            raise EmbedWorkerError(f"响应头不是 JSON: {head[:200]!r}") from e
        if not meta.get("ok"):
            # worker 侧的编码异常:它还活着,不用重起,直接把原因抛给调用方
            raise EmbedError(f"编码失败: {meta.get('error')}")

        n, dim = int(meta["n"]), int(meta["dim"])
        # 向量也是一个完整的帧,必须走 _read_frame 把 4 字节长度前缀吃掉。
        # 直接按 n*dim*4 裸读会把前缀当成第一个 float(整批向量平移 4 字节),
        # 而且剩 4 字节留在管道里,把下一个请求的帧同步也带歪。
        blob = _read_frame(proc.stdout, deadline)
        if blob is None:
            raise EmbedWorkerError("向量数据没收全,子进程中途死了")
        want = n * dim * 4
        if len(blob) != want:
            raise EmbedWorkerError(f"向量字节数不对: 收到 {len(blob)},应为 {want}")
        return dim, blob


def _read_exactly(f, n: int, deadline: float) -> bytes | None:
    """读满 n 字节;EOF 返回 None,超时抛 EmbedWorkerError。

    走裸 fd + select 而不是 BufferedReader.read():后者会一直阻塞到读满为止,
    没有任何办法给它设上限。**本模块不能对 proc.stdout 调 .read()**——两种读法
    混用的话,缓冲区里攒下的字节会凭空消失。
    """
    fd = f.fileno()
    buf = bytearray()
    while len(buf) < n:
        left = deadline - time.monotonic()
        if left <= 0:
            raise EmbedWorkerError(
                f"等编码结果超过 {EMBED_REQUEST_TIMEOUT} 秒(已收 {len(buf)}/{n} 字节)")
        if not select.select([fd], [], [], left)[0]:
            continue
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _read_frame(f, deadline: float) -> bytes | None:
    head = _read_exactly(f, _LEN.size, deadline)
    if head is None:
        return None
    (n,) = _LEN.unpack(head)
    if n > _MAX_FRAME:
        raise EmbedWorkerError(f"帧长 {n} 字节超过上限,管道多半已经错位")
    return _read_exactly(f, n, deadline)


_pool = _Pool()

encode = _pool.encode
shutdown = _pool.shutdown

atexit.register(shutdown)
