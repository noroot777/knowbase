# -*- coding: utf-8 -*-
"""问答流程:意图规划 → 枚举或检索 → 综合回答。

两类问题的根本差异:
- 检索类("哪篇讲了 X / X 怎么用"):答案集中在少数几篇 → top-K 检索够用
- 枚举类("都提过哪些 X / 这周有什么"):答案要求穷举 → 必须 SQL 全量聚合,top-K 会漏

规划步骤用 haiku(约 2-4s,一分钱级),把自然语言问题解析成结构化查询计划;
枚举类走 aggregate 模块全量扫,检索类走混合检索,最后都交给 Claude 综合。
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import date

from kb.aggregate import enumerate_documents, enumerate_entities
from kb.search import Filters, search

_PLAN_PROMPT = """\
今天是 {today}。把用户的知识库问题解析成查询计划,只输出 JSON,不要其他文字:

{{
  "mode": "enumerate_entities | enumerate_docs | retrieve",
  "since": "YYYY-MM-DD 或 null",
  "until": "YYYY-MM-DD 或 null",
  "source": "dr-gzh | dr-youtube-dig | null",
  "kind": "project | product | org | model | null",
  "query": "用于语义检索的查询语句"
}}

判断规则:
- 问"都提过哪些/列出所有/有哪些 XX"(要求穷举实体) → enumerate_entities;
  若问的是开源项目/工具 kind=project,产品 kind=product,模型 kind=model,公司 kind=org
- 问"这段时间有什么文章/更新"(穷举文章) → enumerate_docs
- 问"怎么做/哪篇讲了/介绍一下 XX"(找具体内容) → retrieve
- "公众号"→ source=dr-gzh,"YouTube/视频"→ source=dr-youtube-dig,没提到来源 → null
- "最近一周/这周"→ since=7天前,"最近一个月"→ since=30天前,没提时间 → null

用户问题: {question}
"""


def _run_claude(prompt: str, model: str, timeout: int = 180) -> str:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", model],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude 调用失败: {proc.stderr[:300]}")
    return proc.stdout.strip()


def plan_query(question: str) -> dict:
    """意图规划。解析失败时退回 retrieve 模式(保底可用)。"""
    fallback = {"mode": "retrieve", "since": None, "until": None,
                "source": None, "kind": None, "query": question}
    try:
        out = _run_claude(
            _PLAN_PROMPT.format(today=date.today().isoformat(), question=question),
            model="haiku", timeout=60,
        )
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return fallback
        plan = json.loads(m.group(0))
    except Exception:
        return fallback
    plan = {**fallback, **{k: v for k, v in plan.items() if v not in ("null", "")}}
    if plan["mode"] not in ("enumerate_entities", "enumerate_docs", "retrieve"):
        plan["mode"] = "retrieve"
    return plan


def run_ask(conn, question: str, model: str = "sonnet") -> dict:
    """完整问答。返回 {answer, refs, plan}。refs 的 path 相对 vault 根。"""
    plan = plan_query(question)
    refs: list[dict] = []

    if plan["mode"] == "enumerate_entities":
        items = enumerate_entities(
            conn, since=plan["since"], until=plan["until"],
            source=plan["source"], kind=plan["kind"],
        )
        if not items:
            return {"answer": "该条件下知识库里没有找到任何实体。", "refs": [], "plan": plan}
        lines = []
        seen_paths: dict[str, int] = {}
        for it in items:
            doc_strs = []
            for d in it["docs"]:
                if d["path"] not in seen_paths:
                    seen_paths[d["path"]] = len(seen_paths) + 1
                    refs.append({"n": seen_paths[d["path"]], "title": d["title"],
                                 "path": d["path"], "account": d["account"], "url": ""})
                doc_strs.append(f"[{seen_paths[d['path']]}]{d['title']}")
            lines.append(f"- {it['entity']} ({it['kind']}) ← {'; '.join(doc_strs)}")
        ctx = "\n".join(lines)
        prompt = (
            f"下面是知识库按条件全量枚举出的实体清单(完整,无遗漏),"
            f"共 {len(items)} 个。请整理成分类清晰的回答:同类合并、"
            f"每项一句话说明是什么(根据文章标题推断),保留 [编号] 引用。\n\n"
            f"用户问题: {question}\n\n{ctx}"
        )
    elif plan["mode"] == "enumerate_docs":
        docs = enumerate_documents(
            conn, since=plan["since"], until=plan["until"], source=plan["source"],
        )
        if not docs:
            return {"answer": "该条件下没有文章。", "refs": [], "plan": plan}
        lines = []
        for i, d in enumerate(docs, 1):
            refs.append({"n": i, "title": d["title"], "path": d["path"],
                         "account": d["account"], "url": ""})
            lines.append(f"[{i}] {d['title']} ({d['account']}, {d['published'][:10]})"
                         f"{' — ' + d['summary'] if d['summary'] else ''}")
        prompt = (
            f"下面是知识库按条件全量枚举出的文章清单(完整,共 {len(docs)} 篇)。"
            f"请按主题归类概述,保留 [编号] 引用。\n\n"
            f"用户问题: {question}\n\n" + "\n".join(lines)
        )
    else:  # retrieve
        hits = search(
            conn, plan["query"] or question,
            Filters(source=plan["source"], since=plan["since"], until=plan["until"]),
            top_k=8,
        )
        if not hits:
            return {"answer": "知识库里没有找到相关内容。", "refs": [], "plan": plan}
        parts = []
        for i, h in enumerate(hits, 1):
            full = conn.execute(
                "SELECT group_concat(text, char(10)) AS body FROM chunks WHERE path=?",
                (h.path,),
            ).fetchone()
            parts.append(f"[{i}] {h.title} ({h.account}, {h.published[:10]})\n"
                         f"{(full['body'] or '')[:2500]}")
            refs.append({"n": i, "title": h.title, "path": h.path,
                         "account": h.account, "url": h.url})
        prompt = (
            "根据下面知识库文章回答用户问题。只基于给出的文章,不要编造;"
            "文章里没有就明说没有;引用时标注 [编号]。\n\n"
            f"用户问题: {question}\n\n" + "\n\n---\n\n".join(parts)
        )

    answer = _run_claude(prompt, model=model)
    return {"answer": answer, "refs": refs, "plan": plan}
