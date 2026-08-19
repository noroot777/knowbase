# -*- coding: utf-8 -*-
"""Embedding 抽象:检索层只面对 embed_texts / embed_query 两个函数。

当前实现是本地 bge-m3(sentence-transformers)。以后换云 API 或换模型,
只改这个文件,库里向量重建一遍即可(kb index --reembed)。
"""

from __future__ import annotations

from kb.config import EMBEDDING_MODEL

_model = None


def _get_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "未安装向量依赖。运行: uv sync"
            ) from e
        # 模型已缓存时离线加载:跳过 HuggingFace 的一堆 HEAD 校验请求,
        # 启动快好几秒且断网可用;没缓存(首次)再走在线下载
        try:
            _model = SentenceTransformer(EMBEDDING_MODEL, local_files_only=True)
        except Exception:
            _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


# 一批的注意力张量约 B × heads × L² × 4 字节,随文本长度平方涨。Apple 的 MPS
# 后端对单个 buffer 有上限(约 2/3 统一内存),一条 3566 token 的 chunk 混进
# 32 条的批就要 24.25 GiB,直接 RuntimeError: Invalid buffer size。
# 所以按 B × L² 控预算动态缩批:短文本仍然满批,长文本自动降到一两条。
# 预算取「32 条 768 长度」的规模,与原先常见批次的开销持平。
# 长度用字符数估:中文约 1 字 1 token,英文/代码的 token 数远少于字符数,
# 即这个估计只会偏保守(批切得比必要的更小),不会漏判。
_BATCH_BUDGET = 32 * 768 * 768


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """文档 chunk 批量编码(索引时用)。"""
    if not texts:
        return []
    model = _get_model()
    # 按长度降序分批,批内长度接近 → padding 浪费小,且批首即批内最长,
    # 可以直接用它估这一批的显存规模
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]), reverse=True)
    out: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
    i = 0
    while i < len(order):
        longest = max(len(texts[order[i]]), 1)
        n = max(1, min(batch_size, _BATCH_BUDGET // (longest * longest)))
        idx = order[i:i + n]
        vecs = model.encode([texts[j] for j in idx], batch_size=len(idx),
                            normalize_embeddings=True)
        for j, v in zip(idx, vecs):
            out[j] = v.tolist()
        i += n
    return out


def embed_query(query: str) -> list[float]:
    """查询编码(搜索时用)。"""
    model = _get_model()
    return model.encode([query], normalize_embeddings=True)[0].tolist()
