# -*- coding: utf-8 -*-
"""编码子进程的本体:加载 bge-m3,按帧收请求、回向量,stdin 读到 EOF 就退出。

**把模型放进子进程的唯一理由是内存**:bge-m3 在 MPS 上光权重就 ~3.0 GB,而批量
编码时每出现一种新的 batch 形状,MPS 的显存池就再涨一截、且永不回落。kb-web 跑
几轮 index 之后常驻 8.8 GB(实测峰值 11 GB),`ps` 的 RSS 还看不出来——绝大部分
被 macOS 压缩换出了,只表现为 swap 打满。

而这笔内存在进程内根本收不回来(2026-09-08 实测):
  - `torch.mps.empty_cache()`  → driver 4.82→4.20 GB,phys_footprint 只掉 8 MB
  - `del model` + gc + 上面那句 → phys_footprint 5901→4157 MB,还剩 4 GB 要不回
  - 改 fp16 只把权重从 2.96 压到 1.07 GB,编码峰值一模一样(5.9 GB),不解决问题

**进程边界是唯一可靠的回收手段**,所以模型活在这里,活完就死。协议与生命周期
见 kb/embed_pool.py。
"""

from __future__ import annotations

import json
import os
import struct
import sys

from kb.config import EMBED_BATCH_BUDGET, EMBEDDING_MODEL

_LEN = struct.Struct("<I")

# 一批的注意力张量约 B × heads × L² × 4 字节,随文本长度平方涨。Apple 的 MPS
# 后端对单个 buffer 有上限(约 2/3 统一内存),一条 3566 token 的 chunk 混进
# 32 条的批就要 24.25 GiB,直接 RuntimeError: Invalid buffer size。
# 所以按 B × L² 控预算动态缩批:短文本仍然满批,长文本自动降到一两条。
# 长度用字符数估:中文约 1 字 1 token,英文/代码的 token 数远少于字符数,
# 即这个估计只会偏保守(批切得比必要的更小),不会漏判。
#
# 预算大小直接决定峰值内存:它定的是「一批最多多大」,而 MPS 的显存池会涨到
# 见过的最大批次那个规模。取值见 kb/config.py 的 EMBED_BATCH_BUDGET。
_BATCH_BUDGET = EMBED_BATCH_BUDGET


def _read_exactly(f, n: int) -> bytes | None:
    """读满 n 字节;中途 EOF 返回 None(父进程没了,该收摊了)。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _read_frame(f) -> bytes | None:
    head = _read_exactly(f, _LEN.size)
    if head is None:
        return None
    return _read_exactly(f, _LEN.unpack(head)[0])


def _write_frame(f, payload: bytes) -> None:
    f.write(_LEN.pack(len(payload)))
    f.write(payload)
    f.flush()


def _load_model():
    from sentence_transformers import SentenceTransformer
    # 模型已缓存时离线加载:跳过 HuggingFace 的一堆 HEAD 校验请求,
    # 启动快好几秒且断网可用;没缓存(首次)再走在线下载
    try:
        return SentenceTransformer(EMBEDDING_MODEL, local_files_only=True)
    except Exception:
        return SentenceTransformer(EMBEDDING_MODEL)


def _encode(model, texts: list[str], batch_size: int):
    """返回 (n, dim, float32 裸字节)。"""
    import numpy as np
    # 按长度降序分批,批内长度接近 → padding 浪费小,且批首即批内最长,
    # 可以直接用它估这一批的显存规模
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]), reverse=True)
    out: list = [None] * len(texts)
    i = 0
    while i < len(order):
        longest = max(len(texts[order[i]]), 1)
        n = max(1, min(batch_size, _BATCH_BUDGET // (longest * longest)))
        idx = order[i:i + n]
        vecs = model.encode([texts[j] for j in idx], batch_size=len(idx),
                            normalize_embeddings=True)
        for j, v in zip(idx, vecs):
            out[j] = v
        i += n
    arr = np.asarray(out, dtype=np.float32)
    return arr.shape[0], arr.shape[1], arr.tobytes()


def main() -> int:
    # stdout 是协议通道,不能被任何库的 print / 进度条污染。先把真 stdout 抢到
    # 一个独立 fd 上,再把 1 号 fd 指向 stderr:之后谁往 stdout 写都只是写日志,
    # 冲不散帧边界。(sentence-transformers 的下载进度条就往那儿写。)
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    inp = sys.stdin.buffer

    model = None
    while True:
        raw = _read_frame(inp)
        if raw is None:
            return 0            # 父进程退了或主动关了 stdin
        try:
            req = json.loads(raw)
            op = req.get("op")
            if op == "shutdown":
                return 0
            if op == "ping":
                _write_frame(out, b'{"ok": true}')
                continue
            if op != "encode":
                raise ValueError(f"未知请求: {op!r}")
            if model is None:
                model = _load_model()
            n, dim, blob = _encode(model, req["texts"],
                                   int(req.get("batch_size", 32)))
        except Exception as e:
            # 编码失败不该拖垮 worker:回一个错误头,继续等下一个请求。
            # 真正致命的(模型加载不了)父进程那边会连着失败,自然会放弃。
            err = json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})
            _write_frame(out, err.encode())
            continue
        _write_frame(out, json.dumps({"ok": True, "n": n, "dim": dim}).encode())
        _write_frame(out, blob)


if __name__ == "__main__":
    sys.exit(main())
