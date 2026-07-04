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


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """文档 chunk 批量编码(索引时用)。"""
    model = _get_model()
    vecs = model.encode(
        texts, batch_size=batch_size, normalize_embeddings=True,
        show_progress_bar=len(texts) > 64,
    )
    return [v.tolist() for v in vecs]


def embed_query(query: str) -> list[float]:
    """查询编码(搜索时用)。"""
    model = _get_model()
    return model.encode([query], normalize_embeddings=True)[0].tolist()
