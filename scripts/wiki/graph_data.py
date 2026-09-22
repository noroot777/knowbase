#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开源项目 / skill 图谱的数据层:从条目化产物聚合出节点、共现边与文章表。

分组口径**直接复用 kb.projects**(图鉴那一套):同一个工具的写法变体按
entity_norm 合并、hard 软广条目整条剔除、soft 打标记。自己再扫一遍文件的话,
软广那套口径就有了第二份实现,改一处漏一处。

节点里的文章不内嵌标题/链接,只存**文章表下标**:一篇公众号文章常常拆出十几个
项目条目,内嵌等于把同一份标题+URL 复制十几遍——开源项目那张图的 10460 条记录
其实只出自 3190 篇文章。
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ARTS_PER_NODE = 10      # 面板里「出自这些文章」最多列几条
EDGES_PER_NODE = 10     # 每个节点最多留几条共现边(全留会让边表比节点表还大)


def _safe_url(u: str) -> str:
    """只放行 http(s)。project_url 是 LLM 从公众号正文里读出来写进 frontmatter
    的,`javascript:` 这种点一下就执行;顺带让「只看有仓库」和面板上的链接
    用同一个判断,不会出现"有小圆点却没链接"。"""
    u = (u or "").strip()
    return u if u[:7].lower() == "http://" or u[:8].lower() == "https://" else ""


def _kb():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from kb import projects

    return projects


def load_groups() -> list[dict]:
    return _kb().all_groups()


def select_projects(groups: list[dict]) -> list[dict]:
    """开源项目:kind 判成 project 的,外加挂着 GitHub 仓库的其它条目。

    只看 kind 会漏——同一个东西在有的文章里被当"产品"介绍(kind=product),
    可它的 project_url 明明指着 github 仓库。两条口径取并集。
    """
    return [g for g in groups
            if g["kind"] == "project" or "github.com" in (g["url"] or "")]


def select_skills(groups: list[dict]) -> list[dict]:
    """skill 候选:任一条目打过 topic/skill工具。

    这一层是**粗筛**,里面混着 Claude Code、豆包这种"本身是软件、只是带 skill
    能力"的(标签打在条目上,聚合时任一条目命中就算)。精筛交给归类那一步:
    分类表里有个 X 档专门收它们,判成 X 的不进图谱。
    """
    return [g for g in groups if "skill工具" in g["all_topics"]]


def rank(groups: list[dict]) -> list[dict]:
    """归类和展示共用的排序:提及多的在前,同样多的最近提及的在前。

    归类按这个顺序排队,--max-new 截断时先判的一定是最值得看的那批。
    """
    return sorted(groups, key=lambda g: (g["n"], g["latest"]), reverse=True)


def build(groups: list[dict], codes: dict[str, str], taxo,
          *, drop_codes: frozenset[str] = frozenset()) -> dict:
    """groups(已排序)+ {norm: code} → 前端要的整份数据。

    drop_codes 里的类目(skill 图谱的 X = 不是 skill)直接踢出去,不占节点。
    """
    groups = [g for g in groups
              if codes.get(g["norm"], taxo.fallback) not in drop_codes]

    d30 = (date.today() - timedelta(days=30)).isoformat()
    d90 = (date.today() - timedelta(days=90)).isoformat()

    arts: list[list] = []
    art_id: dict[str, int] = {}

    def art_index(e: dict) -> int | None:
        """文章表去重键用 url;没有 url 的退回标题,再没有就不收录。"""
        key = e["url"] or e["article_title"]
        if not key:
            return None
        if key not in art_id:
            art_id[key] = len(arts)
            arts.append([e["article_title"][:70], e["account"],
                         e["published"][:10], _safe_url(e["url"])])
        return art_id[key]

    nodes: list[dict] = []
    by_art: dict[int, list[int]] = defaultdict(list)

    for g in groups:
        idx = len(nodes)
        seen: list[int] = []
        for e in g["entries"]:
            ai = art_index(e)
            if ai is not None and ai not in seen:
                seen.append(ai)
                by_art[ai].append(idx)
        code = codes.get(g["norm"], taxo.fallback)
        nodes.append({
            "l": g["name"], "q": g["norm"], "c": code,
            "s": (g["summary"] or "").strip().replace("\n", " ")[:150],
            "u": _safe_url(g["url"]), "n": g["n"], "k": g["kind"],
            "a": g["accounts"], "f": g["first"], "t": g["latest"],
            "r30": sum(1 for e in g["entries"] if e["published"][:10] >= d30),
            "r90": sum(1 for e in g["entries"] if e["published"][:10] >= d90),
            "tp": g["topics"],
            "ar": seen[:ARTS_PER_NODE],
            **({"promo": 1} if g.get("promo_soft") else {}),
        })

    # 共现边:同一篇文章里一起被拆出来的两个项目。权重 = 一起出现过几篇。
    # 一篇文章拆出 n 个条目就有 n(n-1)/2 对,盘点类文章能拆出几十个,
    # 全留会炸——先按权重排,每个节点只留最强的 EDGES_PER_NODE 条。
    pair: Counter = Counter()
    for members in by_art.values():
        if len(members) < 2 or len(members) > 40:
            continue          # 超大合集文章(月度盘点)里的共现没有信息量
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair[(a, b) if a < b else (b, a)] += 1

    keep: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for (a, b), w in pair.most_common():
        if len(keep[a]) < EDGES_PER_NODE and len(keep[b]) < EDGES_PER_NODE:
            keep[a].append((b, w))
            keep[b].append((a, w))
    edges = sorted({(min(a, b), max(a, b), w)
                    for a, lst in keep.items() for b, w in lst})

    cats = [{"c": c.code, "g": c.group, "s": c.sub}
            for c in taxo.cats if c.code not in drop_codes]
    counts = Counter(n["c"] for n in nodes)
    return {
        "nodes": nodes, "edges": [list(e) for e in edges], "arts": arts,
        "cats": [{**c, "n": counts.get(c["c"], 0)} for c in cats],
        "meta": {
            "total": len(nodes),
            "classified": sum(1 for n in nodes if n["c"] != taxo.fallback),
            "arts": len(arts), "edges": len(edges),
            "hot30": sum(1 for n in nodes if n["r30"]),
            "built": date.today().isoformat(),
        },
    }
