# -*- coding: utf-8 -*-
"""枚举类查询:全量 SQL 聚合,不走 top-K,保证不漏。

适用于"最近一周都提过哪些开源项目"这类问题——答案要求穷举,
top-K 检索天然会漏,必须按结构化条件扫全表。

排序与截断的三条防偏差规则(2026-07 修,此前视频来源被公众号整体挤出):
- 计数按去重 URL:同一内容的原文+条目化衍生树(dr-gzh 与 dr-gzh-projects、
  dr-youtube-dig 与 dr-video-projects 的 url 相同)只算一篇,消双份权重
- 并列时按最新提及时间排,不再退化成实体名字母序
- 截断前按来源组保底(GROUP_FLOOR),小体量来源(视频系)不会被大体量来源
  (公众号系,文档数 ~17 倍)整体挤出候选清单
"""

from __future__ import annotations

import sqlite3

from kb.config import PRIMARY_SOURCE_NAMES, SOURCE_GROUPS, expand_sources
from kb.norm import kind_norm

# 截断时每个来源组在结果里的保底名额(不足这个数的组全保留)
GROUP_FLOOR = 25
# 单个实体在输出里最多附带的出处文档数(CogVideo 这类被反复转发的不用全列)
DOCS_PER_ENTITY = 6

_SOURCE_TO_GROUP = {name: g for g, names in SOURCE_GROUPS.items() for name in names}


def _norm_topics(topic, topics) -> list[str]:
    """topic(单词,兼容旧签名)与 topics(多词 OR)归一成词列表。"""
    out = list(topics) if topics else []
    if topic:
        out.append(topic)
    return [t.strip() for t in out if t and str(t).strip()]


def _dedup_key(url: str, path: str) -> str:
    """同源去重键:url 相同的原文与衍生条目算同一篇;无 url 的退回 path。"""
    return url or path


def enumerate_entities(
    conn: sqlite3.Connection,
    since: str | None = None,
    until: str | None = None,
    source: str | list[str] | None = None,
    kind: str | None = None,
    topic: str | None = None,
    limit: int = 200,
    topics: list[str] | None = None,
) -> list[dict]:
    """按条件枚举实体及其出处文章。返回按去重后提及篇数降序。

    kind: project/product/org/model/other;None = 全部。
    topic/topics: 主题过滤词(可多个,OR 关系,如 ["文生图", "图像生成"])。
      匹配实体名,或原文里实体与任一词同段出现。没有主题词时排序截断实际是
      "全库热度榜",长尾实体(72% 只被 1 篇提及)会被挤掉——主题类枚举必须传。
    source: 来源名 / 组名("视频"→dig+video-projects) / 列表;None = 全部。
    """
    terms = _norm_topics(topic, topics)
    srcs = expand_sources(source)

    clauses, params = ["1=1"], []
    if since:
        clauses.append("d.published >= ?")
        params.append(since)
    if until:
        clauses.append("d.published <= ?")
        params.append(until + " 23:59")
    if srcs:
        clauses.append(f"d.source IN ({','.join('?' * len(srcs))})")
        params.extend(srcs)
    if kind:
        clauses.append("e.kind = ?")
        params.append(kind_norm(kind))
    if terms:
        ors = []
        for t in terms:
            kw = f"%{t}%"
            ors.append(
                "e.entity LIKE ? OR EXISTS ("
                "SELECT 1 FROM chunks c WHERE c.path = e.path"
                " AND c.text LIKE '%' || e.entity || '%' AND c.text LIKE ?)"
            )
            params.extend([kw, kw])
        clauses.append("(" + " OR ".join(ors) + ")")
    where = " AND ".join(clauses)

    rows = conn.execute(
        f"""
        SELECT e.entity, e.kind, e.norm, d.title, d.path, d.published, d.account,
               d.url, d.summary, d.source
        FROM entities e JOIN documents d ON d.path = e.path
        WHERE {where}
        ORDER BY e.norm, d.published DESC
        """,
        params,
    ).fetchall()

    # 按 norm 聚合:同一实体的写法变体(Skill Creator/skill-creator)归为一项,
    # 展示名取出现最多的写法,kind 取非 other 的多数票
    by_entity: dict[str, dict] = {}
    for r in rows:
        item = by_entity.setdefault(r["norm"], {
            "norm": r["norm"], "docs": [],
            "_paths": set(), "_urls": set(), "_groups": set(),
            "_names": {}, "_kinds": {}, "_latest": "",
        })
        item["_names"][r["entity"]] = item["_names"].get(r["entity"], 0) + 1
        if r["kind"] and r["kind"] != "other":
            item["_kinds"][r["kind"]] = item["_kinds"].get(r["kind"], 0) + 1
        if r["path"] in item["_paths"]:
            continue
        item["_paths"].add(r["path"])
        item["_urls"].add(_dedup_key(r["url"], r["path"]))
        item["_groups"].add(_SOURCE_TO_GROUP.get(r["source"], r["source"]))
        item["_latest"] = max(item["_latest"], (r["published"] or "")[:10])
        item["docs"].append({
            "title": r["title"], "path": r["path"],
            "published": r["published"], "account": r["account"], "url": r["url"],
            "summary": r["summary"],
        })
    for item in by_entity.values():
        item["entity"] = max(item["_names"], key=item["_names"].get)
        item["kind"] = (max(item["_kinds"], key=item["_kinds"].get)
                        if item["_kinds"] else "other")
        item["mentions"] = len(item["_urls"])   # 去重后的提及篇数(排序权重)
        del item["_names"], item["_kinds"], item["_paths"]

    if terms:
        # 纯热度排序会让 Claude Code 这类与任何主题都共现的泛称霸榜:
        # 名字命中的排最前,其余按"共现篇数占该实体总篇数的比例"(专属度)降序;
        # 并列再按最新提及时间(此前退化成字母序,截断落点变成随机彩票)
        kws = [t.lower() for t in terms]
        totals = {r[0]: r[1] for r in conn.execute(
            "SELECT e.norm,"
            "       COUNT(DISTINCT COALESCE(NULLIF(d.url,''), d.path))"
            " FROM entities e JOIN documents d ON d.path = e.path GROUP BY e.norm")}
        ranked = sorted(by_entity.values(), key=lambda x: (
            any(k in x["entity"].lower() for k in kws),
            x["mentions"] / max(totals.get(x["norm"], 1), 1),
            x["mentions"],
            x["_latest"],
        ), reverse=True)
    else:
        ranked = sorted(by_entity.values(),
                        key=lambda x: (x["mentions"], x["_latest"]), reverse=True)

    ranked = _truncate_with_group_floor(ranked, limit)
    for it in ranked:
        it["docs"] = it["docs"][:DOCS_PER_ENTITY]
        it["snippet"] = _entity_snippet(conn, it["entity"], it["docs"])
        del it["_urls"], it["_groups"], it["_latest"]
    return ranked


def _truncate_with_group_floor(ranked: list[dict], limit: int) -> list[dict]:
    """截断到 limit,但保证每个来源组至少留 GROUP_FLOOR 个条目(不足全留)。

    公众号系体量 ~17 倍于视频系,不保底时截断线以下全是视频实体。
    先把欠额组的条目从截断线下补进来,再从排序尾部删超出的——
    只删"删掉后所属各组仍不低于保底"的条目。
    """
    if len(ranked) <= limit:
        return ranked
    order = {id(it): i for i, it in enumerate(ranked)}
    final = list(ranked[:limit])
    in_final = {id(it) for it in final}
    groups = {g for it in ranked for g in it["_groups"]}
    floor = {g: min(GROUP_FLOOR, sum(1 for it in ranked if g in it["_groups"]))
             for g in groups}
    for g in sorted(groups):
        need = floor[g] - sum(1 for it in final if g in it["_groups"])
        for it in ranked[limit:]:
            if need <= 0:
                break
            if g in it["_groups"] and id(it) not in in_final:
                final.append(it)
                in_final.add(id(it))
                need -= 1
    count = {g: sum(1 for it in final if g in it["_groups"]) for g in groups}
    for it in sorted(final, key=lambda x: order[id(x)], reverse=True):
        if len(final) <= limit:
            break
        if all(count[g] - 1 >= floor[g] for g in it["_groups"]):
            final.remove(it)
            for g in it["_groups"]:
                count[g] -= 1
    return sorted(final, key=lambda x: order[id(x)])[:limit]


def _entity_snippet(conn: sqlite3.Connection, entity: str, docs: list[dict]) -> str:
    """从提到该实体的 chunk 里截一段原文,供 LLM 说明"这是做什么的"。

    找不到原文提及时退回文章 summary(总比只有标题强)。
    """
    paths = [d["path"] for d in docs[:5]]
    ph = ",".join("?" * len(paths))
    row = conn.execute(
        f"SELECT text FROM chunks WHERE path IN ({ph}) AND text LIKE ? LIMIT 1",
        [*paths, f"%{entity}%"],
    ).fetchone()
    if row:
        text = row["text"].replace("\n", " ")
        i = text.lower().find(entity.lower())
        start = max(0, (i if i >= 0 else 0) - 30)
        return text[start:start + 180].strip()
    return next((d["summary"] for d in docs if d.get("summary")), "")


def enumerate_documents(
    conn: sqlite3.Connection,
    since: str | None = None,
    until: str | None = None,
    source: str | list[str] | None = None,
    tag: str | None = None,
    topic: str | None = None,
    limit: int = 300,
    topics: list[str] | None = None,
) -> list[dict]:
    """按条件枚举文档(标题+摘要),给"这段时间都有什么文章"类问题。

    topic/topics: 主题过滤词(可多个,OR),匹配标题/摘要/正文任一即算提及。
    source 为 None 时只扫原始来源(排除 derived 的条目化衍生树,
    否则同一内容会以原文+工具条目两种形态重复出现);
    要枚举某个衍生来源就显式传它的名字或来源组。
    """
    terms = _norm_topics(topic, topics)
    srcs = expand_sources(source)
    clauses, params = ["1=1"], []
    if since:
        clauses.append("d.published >= ?")
        params.append(since)
    if until:
        clauses.append("d.published <= ?")
        params.append(until + " 23:59")
    if srcs:
        clauses.append(f"d.source IN ({','.join('?' * len(srcs))})")
        params.extend(srcs)
    else:
        clauses.append(
            f"d.source IN ({','.join('?' * len(PRIMARY_SOURCE_NAMES))})")
        params.extend(PRIMARY_SOURCE_NAMES)
    if tag:
        clauses.append("d.path IN (SELECT path FROM doc_tags WHERE tag = ? OR tag LIKE ?)")
        params.extend([tag, f"{tag}/%"])
    if terms:
        ors = []
        for t in terms:
            kw = f"%{t}%"
            ors.append(
                "d.title LIKE ? OR d.summary LIKE ? OR EXISTS ("
                "SELECT 1 FROM chunks c WHERE c.path = d.path AND c.text LIKE ?)"
            )
            params.extend([kw, kw, kw])
        clauses.append("(" + " OR ".join(ors) + ")")
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT d.path, d.title, d.account, d.published, d.summary, d.url
        FROM documents d WHERE {where}
        ORDER BY d.published DESC LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [dict(r) for r in rows]
