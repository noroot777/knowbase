# -*- coding: utf-8 -*-
"""问答入口:优先 agentic(kb.askagent),失败退回本文件的旧规划管线。

旧管线(意图规划 → 枚举或检索 → 综合)保留作兜底:
- 检索类("哪篇讲了 X / X 怎么用"):答案集中在少数几篇 → top-K 检索够用
- 枚举类("都提过哪些 X / 这周有什么"):答案要求穷举 → SQL 全量聚合
规划用便宜快的模型把问题解析成结构化计划。已知局限(agentic 模式即为
解决它们而生):单主题词字面匹配措辞对不上会漏、单来源格子盖不住来源组。

模型调用统一走 kb.llm(Anthropic / OpenAI,均 API key),不再依赖本地 claude CLI。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import date

from kb import llm
from kb import askagent
from kb import store
from kb.aggregate import enumerate_documents, enumerate_entities
from kb.search import Filters, _filter_sql, search

_PLAN_PROMPT = """\
今天是 {today}。把用户的知识库问题解析成查询计划,只输出 JSON,不要其他文字:

{{
  "mode": "enumerate_entities | enumerate_docs | retrieve | direct",
  "since": "YYYY-MM-DD 或 null",
  "until": "YYYY-MM-DD 或 null",
  "source": "dr-gzh | dr-youtube-dig | null",
  "kind": "project | product | org | model | null",
  "topic": "枚举时的主题过滤词 或 null",
  "query": "用于语义检索的查询语句"
}}

判断规则:
- 问"都提过哪些/列出所有/有哪些 XX"(要求穷举实体) → enumerate_entities;
  若问的是开源项目/工具 kind=project,产品 kind=product,模型 kind=model,公司 kind=org
- 用户明确要求"全知识库扫描/全库/扫一遍/完整检查"等全量覆盖时,必须走 enumerate,
  禁止 retrieve(top-K 检索会漏)。问的是具体事物(项目/工具/skill/产品/模型/公司)
  → enumerate_entities;只有问"有哪些文章/更新"这种以文章为对象的才 enumerate_docs
- enumerate 时若问题限定了主题(如"有哪些 skill/MCP server/视频工具"),把主题提炼成
  单个最核心的过滤词放进 topic,去掉 server/工具/框架 这类后缀,用原文的语言
  (问 skill → "skill",问 MCP server → "MCP",问视频工具 → "视频");
  没有主题限定(问"所有内容/全部实体")或非 enumerate 模式 → topic=null;
  topic 非 null 时 kind 一律给 null(主题词已足够过滤,kind 分类不准会漏条目)
- 问"这段时间/今天/昨天/最近有什么文章/信息/更新/动态",或"总结/概述/汇总
  <某时间>的信息/内容/动态"(要穷举某个时间段发布的文章) → enumerate_docs
- 问"怎么做/哪篇讲了/介绍一下 XX"(找具体内容) → retrieve
- 仅当是基于【之前对话】已有内容重新组织(换个说法/翻译/做成表格/接着上一条继续),
  完全不查任何新资料时 → direct。注意:"总结今天/昨天/最近的信息"是要查知识库某
  时间段的文章,属 enumerate_docs,不是 direct;拿不准就 retrieve
- "公众号"→ source=dr-gzh,"YouTube/视频"→ source=dr-youtube-dig,
  "日报/每日新闻/AI 日报"→ source=dr,没提到来源 → null
- 时间词换算成具体日期:"今天"→ since=until=今天;"昨天"→ since=until=昨天;
  "前天"→ since=until=前天;"最近一周/这周"→ since=7天前;"最近/近期/这几天"
  (无具体数字)→ since=3天前;"最近一个月"→ since=30天前;没提时间 → null
- query 必须是独立完整的检索语句:若问题里有"它/这个/那第二个"这类指代,
  结合对话历史把指代补全
{history_block}
用户问题: {question}
"""


def _fmt_history(history: list[dict], max_msgs: int = 6, clip: int = 800) -> str:
    """最近几条对话拼成文本。assistant 回答可能很长,截断到 clip 字。"""
    lines = []
    for m in history[-max_msgs:]:
        who = "用户" if m["role"] == "user" else "助手"
        text = m["content"].strip()
        if m["role"] == "assistant" and len(text) > clip:
            text = text[:clip] + "…(已截断)"
        lines.append(f"{who}: {text}")
    return "\n\n".join(lines)


def _run_claude(prompt: str, model: str, timeout: int = 180) -> str:
    # 兼容旧函数名的薄封装:实际路由到 kb.llm(Anthropic / OpenAI 均 API key)
    return llm.chat(prompt, model=model, timeout=timeout)


def plan_query(question: str, history: list[dict] | None = None,
               plan_model: str = None) -> dict:
    """意图规划。解析失败时退回 retrieve 模式(保底可用)。"""
    fallback = {"mode": "retrieve", "since": None, "until": None,
                "source": None, "kind": None, "topic": None, "query": question}
    hist = _fmt_history(history or [], clip=300)
    history_block = f"\n对话历史(供理解指代):\n{hist}\n" if hist else ""
    try:
        out = _run_claude(
            _PLAN_PROMPT.format(today=date.today().isoformat(), question=question,
                                history_block=history_block),
            model=plan_model or llm.get_default_model(), timeout=60,
        )
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return fallback
        plan = json.loads(m.group(0))
    except Exception:
        return fallback
    plan = {**fallback, **{k: v for k, v in plan.items() if v not in ("null", "")}}
    if plan["mode"] not in ("enumerate_entities", "enumerate_docs", "retrieve", "direct"):
        plan["mode"] = "retrieve"
    return plan


def _hard_filter_paths(conn, f: Filters) -> set[str]:
    """把调用方显式给的硬过滤编译成"允许出现的文档 path"集合。

    枚举那两条路(aggregate)只认 since/until/source/kind/topic,表达不了
    account/tag/entity;拿这个集合做后置交集,硬过滤才在所有模式下都真生效。
    """
    where, params = _filter_sql(f)
    rows = conn.execute(f"SELECT d.path FROM documents d WHERE 1=1{where}", params)
    return {r["path"] for r in rows}


def _prepare_ask(conn, question: str, model: str, history: list[dict],
                 filters: Filters | None = None) -> dict:
    """规划意图 + 取资料 + 拼提示词。返回 {plan, refs, prompt, answer}:

    - answer 非 None:空结果等短路情形,已有现成回答,prompt 为 None,无需调 LLM。
    - answer 为 None:prompt 是拼好的完整提示词,交给上层(非流式 _run_claude 或
      流式 llm.chat_stream)综合。

    抽出这层是为了让非流式 run_ask 与流式 run_ask_stream 共用同一套规划/取数逻辑。

    filters: 调用方(如 kb ask --source/--tag/--entity/--account)给的硬过滤,
    与规划结果合并,显式项覆盖规划猜的;枚举路径用 path 集合做交集。
    """
    plan = plan_query(question, history, plan_model=llm.get_plan_model(model))
    refs: list[dict] = []

    allowed: set[str] | None = None
    if filters is not None:
        # 显式过滤优先于规划猜的,免得两边打架把结果掐成空
        if filters.source:
            plan["source"] = filters.source
        if filters.since:
            plan["since"] = filters.since
        if filters.until:
            plan["until"] = filters.until
        # path 集合只有枚举那两条路要用(retrieve 走 search 直接编进 SQL),按需算
        if plan["mode"] in ("enumerate_entities", "enumerate_docs"):
            allowed = _hard_filter_paths(conn, filters)

    hist_text = _fmt_history(history)
    hist_block = f"之前的对话(供衔接上下文):\n{hist_text}\n\n" if hist_text else ""

    if plan["mode"] == "direct" and not history:
        plan["mode"] = "retrieve"   # 没有上文可依据,退回检索

    if plan["mode"] == "direct":
        prompt = (
            "下面是你(知识库助手)与用户之前的对话。请基于对话里已有的内容回答"
            "用户的新问题,不要编造对话里没有的信息;保留原有的 [编号] 引用标记。\n\n"
            f"{hist_text}\n\n用户新问题: {question}"
        )
        # 引用沿用上一条 assistant 消息的来源(编号体系一致)
        for m in reversed(history):
            if m["role"] == "assistant" and m.get("refs"):
                refs = list(m["refs"])
                break
        return {"plan": plan, "refs": refs, "prompt": prompt, "answer": None}

    if plan["mode"] == "enumerate_entities":
        items = enumerate_entities(
            conn, since=plan["since"], until=plan["until"],
            source=plan["source"], kind=plan["kind"], topic=plan["topic"],
        )
        if allowed is not None:   # 硬过滤:只留还剩出处的实体
            items = [it for it in
                     ({**it, "docs": [d for d in it["docs"] if d["path"] in allowed]}
                      for it in items) if it["docs"]]
        if not items:
            return {"plan": plan, "refs": [], "prompt": None,
                    "answer": "该条件下知识库里没有找到任何实体。"}
        lines = []
        seen_paths: dict[str, int] = {}
        for it in items:
            doc_strs = []
            for d in it["docs"]:
                if d["path"] not in seen_paths:
                    seen_paths[d["path"]] = len(seen_paths) + 1
                    refs.append({"n": seen_paths[d["path"]], "title": d["title"],
                                 "path": d["path"], "account": d["account"],
                                 "url": d["url"]})
                doc_strs.append(f"[{seen_paths[d['path']]}]{d['title']}")
            snip = f"\n  原文片段: {it['snippet']}" if it.get("snippet") else ""
            lines.append(f"- {it['entity']} ({it['kind']}) ← {'; '.join(doc_strs)}{snip}")
        ctx = "\n".join(lines)
        prompt = (
            f"下面是知识库按宽泛关键词全量枚举出的候选实体清单(共 {len(items)} 个,"
            f"大部分附有原文片段),其中可能混有与用户问题无关的条目。"
            f"请回答用户的问题:只使用与问题相关的条目,无关的不要罗列;"
            f"若问题本身就要求穷举全部,则完整覆盖、同类合并。"
            f"提到某个实体时必须用一句话说清楚它是做什么的——优先依据原文片段,"
            f"其次才从标题推断;只有片段和标题都看不出用途时,才写\"原文未详述\","
            f"不要用\"XX 推荐的工具\"\"趣味项目\"这类只描述出处不描述用途的话;"
            f"保留 [编号] 引用。\n\n"
            f"{hist_block}用户问题: {question}\n\n{ctx}"
        )
    elif plan["mode"] == "enumerate_docs":
        docs = enumerate_documents(
            conn, since=plan["since"], until=plan["until"], source=plan["source"],
            topic=plan["topic"],
        )
        if allowed is not None:
            docs = [d for d in docs if d["path"] in allowed]
        if not docs:
            return {"plan": plan, "refs": [], "prompt": None,
                    "answer": "该条件下没有文章。"}
        lines = []
        for i, d in enumerate(docs, 1):
            refs.append({"n": i, "title": d["title"], "path": d["path"],
                         "account": d["account"], "url": d["url"]})
            lines.append(f"[{i}] {d['title']} ({d['account']}, {d['published'][:10]})"
                         f"{' — ' + d['summary'] if d['summary'] else ''}")
        prompt = (
            f"下面是知识库按条件全量枚举出的文章清单(共 {len(docs)} 篇),"
            f"其中可能混有与用户问题无关的文章。请回答用户的问题:"
            f"只使用与问题相关的文章,无关的不要罗列;"
            f"若问题本身就要求全量概述,则按主题归类完整覆盖。"
            f"保留 [编号] 引用。\n\n"
            f"{hist_block}用户问题: {question}\n\n" + "\n".join(lines)
        )
    else:  # retrieve
        hits = search(
            conn, plan["query"] or question,
            Filters(source=plan["source"], since=plan["since"], until=plan["until"],
                    account=filters.account if filters else None,
                    tags=list(filters.tags) if filters else [],
                    entities=list(filters.entities) if filters else []),
            top_k=8,
        )
        if not hits:
            return {"plan": plan, "refs": [], "prompt": None,
                    "answer": "知识库里没有找到相关内容。"}
        parts = []
        for i, h in enumerate(hits, 1):
            full = store.doc_body(conn, h.path)
            parts.append(f"[{i}] {h.title} ({h.account}, {h.published[:10]})\n"
                         f"{full[:2500]}")
            refs.append({"n": i, "title": h.title, "path": h.path,
                         "account": h.account, "url": h.url})
        prompt = (
            "根据下面知识库文章回答用户问题。只基于给出的文章,不要编造;"
            "文章里没有就明说没有;引用时标注 [编号]。\n\n"
            f"{hist_block}用户问题: {question}\n\n" + "\n\n---\n\n".join(parts)
        )

    return {"plan": plan, "refs": refs, "prompt": prompt, "answer": None}


def filter_refs_to_cited(answer: str, refs: list[dict],
                         keep_all_below: int = 40) -> list[dict]:
    """agent 模式的引用注册表可能有几百条(枚举工具的出处全在里面),
    落库/展示前只留回答里实际引用的 [n];小体量(≤keep_all_below)或
    回答里一个编号都没有时原样保留(别把 direct 追问的沿用编号清空)。"""
    if len(refs) <= keep_all_below:
        return refs
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    kept = [r for r in refs if r["n"] in cited]
    return kept or refs


def run_ask(conn, question: str, model: str = None,
            history: list[dict] | None = None,
            filters: Filters | None = None) -> dict:
    """完整问答(非流式)。返回 {answer, refs, plan}。refs 的 path 相对 vault 根。

    优先走 agentic 模式(kb.askagent:模型自选工具多轮查库);
    agent 不可用(没配 anthropic key)或研究阶段失败时退回旧规划管线。

    history: 同一会话里之前的消息 [{"role": "user"|"assistant", "content": str}],
    按时间序。用于意图规划的指代消解和综合回答的上下文衔接。

    filters: 调用方给的硬过滤(kb ask --source/--account/--tag/--entity)。
    给了就不走 agent —— agent 自己规划检索,约束只能靠提示词"求"它遵守;
    走规划管线才能把过滤真正编进 SQL,任何兜底分支都不会漏掉。
    """
    history = history or []
    model = model or llm.get_default_model()
    if filters is None and askagent.agent_available(model):
        try:
            return askagent.run_agent(conn, question, model, history)
        except RuntimeError:
            pass   # 研究/综合失败 → 退回旧管线重来(非流式无半截输出问题)
    prep = _prepare_ask(conn, question, model, history, filters)
    if prep["answer"] is not None:
        return {"answer": prep["answer"], "refs": prep["refs"], "plan": prep["plan"]}
    answer = _run_claude(prep["prompt"], model=model)
    return {"answer": answer, "refs": prep["refs"], "plan": prep["plan"]}


def run_ask_stream(conn, question: str, model: str = None,
                   history: list[dict] | None = None) -> Iterator[dict]:
    """流式问答。事件序:progress*(agent 研究进度,可为 0 条)→ meta(plan+refs)
    → delta*(text)。上层据此推 NDJSON 给前端;调用方 close() 时 GeneratorExit
    穿透到底层 LLM 流关闭,后端生成随之停止(真停止)。

    agentic 优先:研究阶段(meta 之前)失败静默退回旧规划管线;
    meta 已发出后(综合阶段)的失败按原样上抛,由上层转 error 帧。
    """
    history = history or []
    model = model or llm.get_default_model()
    if askagent.agent_available(model):
        agen = askagent.run_agent_stream(conn, question, model, history)
        emitted_meta = False
        try:
            for ev in agen:
                if ev["type"] == "meta":
                    emitted_meta = True
                yield ev
            return
        except RuntimeError:
            if emitted_meta:
                raise
            # 研究阶段失败(网关抖动/模型不支持工具等)→ 退回旧管线
        finally:
            agen.close()
    yield from _legacy_stream(conn, question, model, history)


def _legacy_stream(conn, question: str, model: str,
                   history: list[dict]) -> Iterator[dict]:
    """旧规划管线的流式路径(agent 不可用/失败时的兜底)。"""
    prep = _prepare_ask(conn, question, model, history)
    yield {"type": "meta", "plan": prep["plan"], "refs": prep["refs"]}
    if prep["answer"] is not None:
        yield {"type": "delta", "text": prep["answer"]}
        return
    for chunk in llm.chat_stream(prep["prompt"], model=model):
        yield {"type": "delta", "text": chunk}
