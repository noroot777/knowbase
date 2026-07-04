# -*- coding: utf-8 -*-
"""MCP server:把知识库检索暴露给 Claude Code / Claude Desktop。

注册(在任意目录):
    claude mcp add kb --scope user -- uv run --directory ~/code/knowledge-base kb-mcp

之后在对话里可直接用 kb_search / kb_read 工具。
不提供 ask 工具 —— 调用方本身就是 LLM,拿到检索结果自己综合即可。
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from kb import store
from kb.config import VAULT_ROOT
from kb.search import Filters, search

mcp = FastMCP("knowledge-base")


@mcp.tool()
def kb_search(
    query: str,
    source: str | None = None,
    tag: str | None = None,
    entity: str | None = None,
    top: int = 10,
) -> list[dict]:
    """搜索个人知识库(微信公众号文章 + YouTube 视频摘要)。

    混合检索:关键词(FTS) + 语义(向量),适合精确名词和意图描述两类查询。

    Args:
        query: 关键词或一句话意图,如 "book-to-skill" 或 "把书变成技能的工具"
        source: 限定来源,可选 dr-gzh(公众号) / dr-youtube-dig(YouTube)
        tag: 限定主题标签,如 topic/skill工具、topic/ai-coding
        entity: 限定实体(项目/产品/公司名),如 "Claude Code"
        top: 返回条数,默认 10

    Returns:
        文档列表,含 title/account/published/url/path/summary/best_chunk。
        用 kb_read(path) 读某篇全文。
    """
    conn = store.connect()
    try:
        hits = search(
            conn, query,
            Filters(source=source, tags=[tag] if tag else [],
                    entities=[entity] if entity else []),
            top_k=min(top, 30),
        )
        out = []
        for h in hits:
            d = conn.execute(
                "SELECT summary FROM documents WHERE path=?", (h.path,)
            ).fetchone()
            out.append({
                "title": h.title, "account": h.account,
                "published": h.published, "source": h.source,
                "url": h.url, "path": h.path,
                "summary": (d["summary"] if d else ""),
                "best_chunk": h.best_chunk,
                "matched_by": h.matched_by,
            })
        return out
    finally:
        conn.close()


@mcp.tool()
def kb_read(path: str) -> str:
    """读取知识库中一篇文档的 Markdown 全文。

    Args:
        path: kb_search 返回的 path 字段(相对 vault 根的路径)
    """
    full = (VAULT_ROOT / path).resolve()
    # 防目录穿越:必须落在 vault 内
    if not str(full).startswith(str(VAULT_ROOT.resolve())):
        raise ValueError("path 越界")
    try:
        return full.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"读取失败: {e}")


@mcp.tool()
def kb_entities(prefix: str = "", top: int = 30) -> list[dict]:
    """列出知识库里的实体(项目/产品/公司名)及文档数,用于发现可过滤的实体名。

    Args:
        prefix: 可选前缀过滤(不区分大小写)
        top: 返回条数
    """
    conn = store.connect()
    try:
        if prefix:
            rows = conn.execute(
                "SELECT entity, COUNT(*) n FROM entities WHERE entity LIKE ? COLLATE NOCASE "
                "GROUP BY entity ORDER BY n DESC LIMIT ?",
                (f"{prefix}%", top),
            )
        else:
            rows = conn.execute(
                "SELECT entity, COUNT(*) n FROM entities GROUP BY entity ORDER BY n DESC LIMIT ?",
                (top,),
            )
        return [{"entity": r["entity"], "docs": r["n"]} for r in rows]
    finally:
        conn.close()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
