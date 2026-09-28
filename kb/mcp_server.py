# -*- coding: utf-8 -*-
"""MCP server:把知识库检索暴露给 Claude Code / Claude Desktop。

注册(在任意目录):
    claude mcp add kb --scope user -- uv run --directory ~/code/knowbase kb-mcp

之后在对话里可直接用 kb_search / kb_read 工具。
不提供 ask 工具 —— 调用方本身就是 LLM,拿到检索结果自己综合即可。
"""

from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

from kb import store
from kb.config import VAULT_ROOT
from kb.search import Filters, search

mcp = FastMCP("knowbase")


@mcp.tool()
def kb_search(
    query: str,
    source: str | None = None,
    tag: str | None = None,
    entity: str | None = None,
    since: str | None = None,
    until: str | None = None,
    top: int = 10,
) -> list[dict]:
    """搜索个人知识库(微信公众号文章 + YouTube 视频摘要 + 工具条目树)。

    混合检索:关键词(FTS) + 语义(向量),适合精确名词和意图描述两类查询。
    注意:这是 top-K 相关性检索;"都提过哪些 XX"类穷举问题请用 kb_enumerate。

    Args:
        query: 关键词或一句话意图,如 "book-to-skill" 或 "把书变成技能的工具"
        source: 限定来源。推荐用来源组:gzh/公众号(dr-gzh+dr-gzh-projects)、
            video/视频(dr-youtube-dig+dr-video-projects)、dr/日报;
            也可给具体来源名(dr-gzh、dr-youtube-dig、dr-video-projects 等)
        tag: 限定主题标签,如 topic/skill工具、topic/ai-coding
        entity: 限定实体(项目/产品/公司名),如 "Claude Code"
        since: 只看这天之后发布的,格式 YYYY-MM-DD
        until: 只看这天之前发布的(含当天),格式 YYYY-MM-DD
        top: 返回条数,默认 10

    Returns:
        文档列表,含 title/account/published/url/path/summary/best_chunk。
        用 kb_read(path) 读某篇全文。
        向用户展示结果时:标题写成 markdown 链接 [标题](url),
        并附公众号名与发布日期,引用原文内容时标注来源编号。
    """
    conn = store.connect()
    try:
        hits = search(
            conn, query,
            Filters(source=source, tags=[tag] if tag else [],
                    entities=[entity] if entity else [],
                    since=since, until=until),
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
def kb_enumerate(
    what: str = "entities",
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    kind: str | None = None,
    topic: str | None = None,
) -> list[dict]:
    """全量枚举知识库内容(不是 top-K,保证不漏)。

    适合"最近一周公众号都提过哪些开源项目"这类穷举问题:
    kb_enumerate(what="entities", since="2026-06-27", source="gzh", kind="project")

    Args:
        what: "entities"(枚举实体,带出处文章) 或 "docs"(枚举文章)
        since: 只看这天之后发布的,格式 YYYY-MM-DD
        until: 只看这天之前发布的(含当天)
        source: 来源组或来源名,None=全部。推荐用组:gzh/公众号、video/视频、dr/日报
            (组包含对应的条目化衍生树,查工具/项目类信息最全)。
            what=docs 且 None 时只扫原始来源(不含条目化衍生树,避免内容重复)
        kind: 实体类型 project(开源项目)/product/org(公司)/model(模型),仅 what=entities 有效
        topic: 主题过滤词,可给多个近义变体用逗号分隔(如 "文生图,图像生成,text-to-image",
            OR 关系)。库里措辞和问题措辞常不一致,多给变体防漏
    """
    from kb.aggregate import enumerate_documents, enumerate_entities
    topics = [t for t in re.split(r"[,，、/]", topic or "") if t.strip()] or None
    conn = store.connect()
    try:
        if what == "docs":
            return enumerate_documents(conn, since=since, until=until, source=source,
                                       topics=topics)
        return enumerate_entities(conn, since=since, until=until,
                                  source=source, kind=kind, topics=topics)
    finally:
        conn.close()


@mcp.tool()
def kb_read(path: str) -> str:
    """读取知识库中一篇文档的 Markdown 全文。

    Args:
        path: kb_search 返回的 path 字段(相对 vault 根的路径)
    """
    root = VAULT_ROOT.resolve()
    full = (root / path).resolve()
    # 防目录穿越:必须落在 vault 内。按路径层级比,不能比字符串前缀 ——
    # 前缀判定会把同前缀的兄弟目录(new-start-private)当成 vault 内部。
    if full != root and root not in full.parents:
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
        # 按 norm 聚合写法变体,展示名取出现最多的写法
        totals: dict[str, int] = {}
        best_name: dict[str, tuple[int, str]] = {}
        for r in conn.execute(
            "SELECT norm, entity, COUNT(DISTINCT path) AS n FROM entities "
            "GROUP BY norm, entity"
        ):
            totals[r["norm"]] = totals.get(r["norm"], 0) + r["n"]
            if r["norm"] not in best_name or r["n"] > best_name[r["norm"]][0]:
                best_name[r["norm"]] = (r["n"], r["entity"])
        items = [{"entity": best_name[n][1], "docs": c} for n, c in totals.items()]
        if prefix:
            p = prefix.lower()
            items = [it for it in items if it["entity"].lower().startswith(p)]
        items.sort(key=lambda x: x["docs"], reverse=True)
        return items[:top]
    finally:
        conn.close()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
