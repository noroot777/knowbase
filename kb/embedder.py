# -*- coding: utf-8 -*-
"""Embedding 抽象:检索层只面对 embed_texts / embed_query 两个函数。

**硬约束在 `docs/decisions/embedding.md` 第一节 —— 改这块之前先读那一份。**

当前实现是本地 bge-m3(sentence-transformers),**跑在子进程里**——模型一旦碰过
MPS,那笔显存在本进程内就再也收不回来了,原委见 kb/embed_worker.py。子进程的
起停由 kb/embed_pool.py 管,这里只负责把结果翻译成调用方要的形状。

批量写向量统一走 embed_pending_chunks():索引的三处调用点原本各抄了一份同样的
「取无向量 chunk → 128 条一批编码 → 写回 → commit」循环。
"""

from __future__ import annotations

import struct
import sys

from kb.config import EMBED_IN_PROCESS

# 批量写库时一次取多少 chunk。这是 DB 侧的分批(每批一次 commit),
# 模型侧还会按文本长度再切一次(见 embed_worker._BATCH_BUDGET)。
_DB_BATCH = 128


# ── 进程内兜底 ────────────────────────────────────────────────────────────────
# 只在 KB_EMBED_IN_PROCESS=1 或子进程实在起不来时走。能用,就是内存降不下来。

_model = None


def _get_model():
    global _model
    if _model is None:
        try:
            # 注意 import 要连着调用一起包在 try 里:embed_worker 模块本身只依赖
            # 标准库,sentence_transformers 是在 _load_model() 内部才 import 的。
            # 只包 import 的话缺依赖时漏出来的是 ImportError,而 search.py 只
            # 认 RuntimeError——检索会直接 500,而不是降级成纯 FTS。
            from kb.embed_worker import _load_model
            _model = _load_model()
        except ImportError as e:
            raise RuntimeError("未安装向量依赖。运行: uv sync") from e
    return _model


def _encode_in_process(texts: list[str], batch_size: int) -> tuple[int, bytes]:
    from kb.embed_worker import _encode as _worker_encode
    _, dim, blob = _worker_encode(_get_model(), texts, batch_size)
    return dim, blob


# ── 对外 ──────────────────────────────────────────────────────────────────────

def _encode(texts: list[str], batch_size: int) -> tuple[int, bytes]:
    """返回 (dim, n×dim 个 float32 的裸字节)。"""
    if EMBED_IN_PROCESS:
        return _encode_in_process(texts, batch_size)
    from kb import embed_pool
    try:
        return embed_pool.encode(texts, batch_size)
    except embed_pool.EmbedWorkerError as e:
        # 子进程路子断了。检索可用比省内存重要,退回进程内加载——但要吵一声,
        # 否则内存悄悄涨回 8 GB 而没人知道为什么。
        print(f"[embed] 子进程不可用,退回进程内加载(此进程内存将不再回落): {e}",
              file=sys.stderr, flush=True)
        return _encode_in_process(texts, batch_size)


def _to_lists(dim: int, blob: bytes) -> list[list[float]]:
    n = len(blob) // (dim * 4)
    unpack = struct.Struct(f"<{dim}f").unpack_from
    return [list(unpack(blob, i * dim * 4)) for i in range(n)]


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """文档 chunk 批量编码。"""
    if not texts:
        return []
    dim, blob = _encode(texts, batch_size)
    return _to_lists(dim, blob)


def embed_query(query: str) -> list[float]:
    """查询编码(搜索时用)。"""
    return embed_texts([query])[0]


def shutdown() -> None:
    """收掉编码子进程,立刻把显存还给系统。批量任务结束时调,幂等。"""
    if EMBED_IN_PROCESS:
        return
    from kb import embed_pool
    embed_pool.shutdown()


def embed_pending_chunks(conn, progress=None) -> int:
    """把库里所有还没有向量的 chunk 编码入库,返回处理条数。

    编码完成后收掉子进程:一轮索引会让它的显存池涨到几 GB,而索引通常是「跑完
    就没事了」的活,没理由让那几 GB 再干等 EMBED_IDLE_SECONDS 秒。

    progress: 可选回调 progress(done, total)。开工前先以 (0, total) 调一次,
    这样调用方不必自己再查一遍 chunks_without_vectors——那个查询会把每条 chunk
    的正文都捞出来,查两遍不便宜。
    """
    from kb import store

    rows = store.chunks_without_vectors(conn)
    if not rows:
        return 0
    if progress:
        progress(0, len(rows))
    try:
        for i in range(0, len(rows), _DB_BATCH):
            batch = rows[i:i + _DB_BATCH]
            dim, blob = _encode([r["text"] for r in batch], 32)
            stride = dim * 4
            store.insert_vectors(
                conn, [r["id"] for r in batch],
                [blob[j * stride:(j + 1) * stride] for j in range(len(batch))],
            )
            conn.commit()
            if progress:
                progress(min(i + _DB_BATCH, len(rows)), len(rows))
    finally:
        shutdown()
    return len(rows)
