# -*- coding: utf-8 -*-
"""agentic 问答:研究循环(模型自选工具多轮查库)→ 综合回答。

取代旧的"小模型填一张表(mode/source/topic)→ 按表查一次"的规划管线:
表格式规划把问题压进单主题词+单来源的格子,措辞对不上或来源映射漏了
条目树就整体漏查(2026-07 两次踩坑:视频来源全军覆没/文生图只回 1 条)。

两阶段设计:
- 研究阶段:Anthropic 模型 + 工具(检索/枚举/读全文)多轮循环,只查资料
  不写答案;结果太少会换近义词、换来源组重查。
- 综合阶段:把研究阶段的资料拼成上下文,用户所选模型(任意 provider)
  流式生成最终回答。研究/综合分离让 GPT 系模型也能享受 agentic 检索,
  且最终回答保持流式体验。

失败兜底由 askflow 负责:这里抛 RuntimeError,askflow 退回旧规划管线。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date

from kb import llm
from kb import store
from kb.aggregate import enumerate_documents, enumerate_entities
from kb.search import Filters, search

MAX_ROUNDS = 5          # 研究阶段最多几轮工具调用(每轮可含多个并列调用)
MAX_CALLS = 12          # 总调用数上限
EVIDENCE_CAP = 50_000   # 综合阶段资料总字符上限(超出丢弃后来的低价值结果)

_SYSTEM = """\
你是个人知识库的检索研究员。今天是 {today}。你的任务:针对用户问题,用工具把知识库里的\
相关资料查全,然后停下。**你不写最终回答**——资料查够了就停止调用工具,只回复 OK。

知识库来源分组(sources 参数接受组名/具体来源名/列表,不传=全库):
- gzh = 公众号系(dr-gzh 文章原文 + dr-gzh-projects 工具条目)
- video = 视频系(dr-youtube-dig 视频概览摘要 + dr-video-projects 按原字幕转写的工具条目)
- dr = AI 日报
条目树(*-projects)是"一个工具/项目一篇"的介绍页,查工具、项目、模型靠它们最全;
概览、新闻、访谈、观点只在原始来源里。用户点名"公众号/视频"时才限定组,否则不限来源。

工具选择规则:
- 穷举类问题("都有哪些/列出所有/有哪些 XX")必须用 kb_enumerate_entities,\
top-K 检索天然会漏。topics 给 2-4 个近义变体、中英文都给(如 ["文生图","图像生成",\
"text-to-image"]),库里的措辞经常和问题措辞不一样。
- "某时间段有什么文章/更新/动态/总结一下" → kb_enumerate_docs。
- 找具体内容或用法("哪篇讲了 X / X 怎么用 / 介绍一下 X") → kb_search。
- 需要某篇的完整细节再 kb_read,不要整库乱读。
- 结果为 0 或明显太少:换近义词、放宽来源限制再查一轮,不要直接放弃。
- 结果够回答了就停,不要为"再确认一下"多花轮次。
- 纯基于对话历史的追问(换说法/翻译/接着上一条)不需要工具,直接停。
- 时间词换算成 since/until(YYYY-MM-DD):"最近一周"= 7 天前起,"最近"= 3 天前起。

资料行首的 [n] 是引用编号,综合回答阶段要用,你不用管。"""

TOOLS = [
    {
        "name": "kb_search",
        "description": "混合检索(关键词+语义)知识库,返回最相关的若干篇。适合找具体内容;穷举类问题不要只靠它。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索语句,关键词或一句话意图"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "来源限定:组名 gzh/video/dr 或具体来源名,省略=全库"},
                "since": {"type": "string", "description": "YYYY-MM-DD,只看这天起发布的"},
                "until": {"type": "string", "description": "YYYY-MM-DD,只看这天前发布的(含当天)"},
                "top": {"type": "integer", "description": "返回条数,默认 8,最大 20"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_enumerate_entities",
        "description": "全量枚举实体(项目/产品/模型/公司)及出处,不是 top-K,穷举类问题必用。按主题词过滤时给多个近义变体。",
        "input_schema": {
            "type": "object",
            "properties": {
                "topics": {"type": "array", "items": {"type": "string"},
                           "description": "主题过滤词,OR 关系,给 2-4 个近义变体(中英)。省略=全部(热度榜)"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "来源限定:组名 gzh/video/dr 或具体来源名,省略=全库"},
                "kind": {"type": "string",
                         "description": "实体类型:project(开源项目)/product/model/org,省略=全部"},
                "since": {"type": "string"}, "until": {"type": "string"},
                "limit": {"type": "integer", "description": "默认 200"},
            },
        },
    },
    {
        "name": "kb_enumerate_docs",
        "description": "全量枚举某条件下的文章清单(标题+摘要),给'这段时间有什么文章/动态'类问题。",
        "input_schema": {
            "type": "object",
            "properties": {
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "来源限定,省略=全部原始来源(不含条目树,避免重复)"},
                "topics": {"type": "array", "items": {"type": "string"},
                           "description": "主题过滤词,OR 关系"},
                "since": {"type": "string"}, "until": {"type": "string"},
                "limit": {"type": "integer", "description": "默认 300"},
            },
        },
    },
    {
        "name": "kb_read",
        "description": "读一篇文档的全文(按 path,来自其他工具的结果)。",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]


class _Tools:
    """工具执行器:持有 conn 与引用注册表,把结果格式化成带 [n] 编号的紧凑文本。"""

    def __init__(self, conn):
        self.conn = conn
        self.refs: list[dict] = []
        self._n_by_path: dict[str, int] = {}

    def _ref(self, path: str, title: str, account: str, url: str) -> int:
        if path not in self._n_by_path:
            self._n_by_path[path] = len(self.refs) + 1
            self.refs.append({"n": self._n_by_path[path], "title": title,
                              "path": path, "account": account, "url": url})
        return self._n_by_path[path]

    def run(self, name: str, args: dict) -> tuple[str, str]:
        """执行一个工具调用,返回 (给模型的结果文本, 给前端的进度描述)。"""
        try:
            if name == "kb_search":
                return self._search(args)
            if name == "kb_enumerate_entities":
                return self._enum_entities(args)
            if name == "kb_enumerate_docs":
                return self._enum_docs(args)
            if name == "kb_read":
                return self._read(args)
            return f"未知工具 {name}", f"未知工具 {name}"
        except Exception as e:  # 单次调用失败告诉模型,让它调整,不炸整个循环
            return f"工具执行出错: {e}", f"{name} 出错"

    @staticmethod
    def _src_label(args: dict) -> str:
        s = args.get("sources")
        return f" · {','.join(s)}" if s else ""

    def _search(self, args: dict) -> tuple[str, str]:
        hits = search(
            self.conn, args["query"],
            Filters(source=args.get("sources"), since=args.get("since"),
                    until=args.get("until")),
            top_k=min(int(args.get("top") or 8), 20),
        )
        lines = []
        for h in hits:
            n = self._ref(h.path, h.title, h.account, h.url)
            d = self.conn.execute("SELECT summary FROM documents WHERE path=?",
                                  (h.path,)).fetchone()
            gist = (d["summary"] if d and d["summary"] else h.best_chunk)[:220]
            lines.append(f"[{n}] {h.title}｜{h.source}｜{h.account}｜"
                         f"{h.published[:10]}\n    {gist}")
        text = f"共 {len(hits)} 条:\n" + "\n".join(lines) if hits else "没有命中。"
        return text, f"检索「{args['query'][:30]}」{self._src_label(args)} → {len(hits)} 条"

    def _enum_entities(self, args: dict) -> tuple[str, str]:
        items = enumerate_entities(
            self.conn, since=args.get("since"), until=args.get("until"),
            source=args.get("sources"), kind=args.get("kind"),
            topics=args.get("topics"),
            limit=min(int(args.get("limit") or 200), 300),
        )
        lines = []
        for it in items[:120]:
            marks = "".join(
                f"[{self._ref(d['path'], d['title'], d['account'], d['url'])}]"
                for d in it["docs"][:3])
            snip = (it.get("snippet") or "")[:150]
            lines.append(f"- {it['entity']}({it['kind']},{it['mentions']} 篇){marks}"
                         f"{': ' + snip if snip else ''}")
        more = f"\n…还有 {len(items) - 120} 个(已按相关度排序,尾部相关性低)" \
            if len(items) > 120 else ""
        topic_s = "/".join(args.get("topics") or []) or "全部"
        text = (f"匹配 {len(items)} 个实体:\n" + "\n".join(lines) + more
                ) if items else "该条件下没有实体。"
        return text, f"枚举实体[{topic_s}]{self._src_label(args)} → {len(items)} 个"

    def _enum_docs(self, args: dict) -> tuple[str, str]:
        docs = enumerate_documents(
            self.conn, since=args.get("since"), until=args.get("until"),
            source=args.get("sources"), topics=args.get("topics"),
            limit=min(int(args.get("limit") or 300), 400),
        )
        lines = []
        for d in docs[:150]:
            n = self._ref(d["path"], d["title"], d["account"], d["url"])
            summ = f" — {d['summary'][:100]}" if d["summary"] else ""
            lines.append(f"[{n}] {d['title']}({d['account']},{d['published'][:10]}){summ}")
        more = f"\n…还有 {len(docs) - 150} 篇" if len(docs) > 150 else ""
        text = (f"共 {len(docs)} 篇:\n" + "\n".join(lines) + more
                ) if docs else "该条件下没有文章。"
        return text, f"枚举文章{self._src_label(args)} → {len(docs)} 篇"

    def _read(self, args: dict) -> tuple[str, str]:
        path = args.get("path") or ""
        d = self.conn.execute("SELECT * FROM documents WHERE path=?",
                              (path,)).fetchone()
        if d is None:
            return "没有这篇文档(path 要来自其他工具的结果)。", "读原文:未找到"
        full = store.doc_body(self.conn, path)
        n = self._ref(path, d["title"], d["account"], d["url"])
        return (f"[{n}] {d['title']}\n{full[:5000]}",
                f"读原文《{d['title'][:24]}》")


def _fmt_history(history: list[dict], max_msgs: int = 6, clip: int = 800) -> str:
    lines = []
    for m in history[-max_msgs:]:
        who = "用户" if m["role"] == "user" else "助手"
        text = m["content"].strip()
        if len(text) > clip:
            text = text[:clip] + "…(已截断)"
        lines.append(f"{who}: {text}")
    return "\n\n".join(lines)


def _research(conn, question: str, research_model: str,
              history: list[dict]) -> Iterator[dict]:
    """研究阶段生成器:yield {"type":"progress"} 事件,return 最终状态。

    return 值(通过 StopIteration.value / `yield from` 拿到):
    {"tools": _Tools, "evidence": [str], "plan": dict}
    """
    tools = _Tools(conn)
    evidence: list[str] = []
    calls_log: list[dict] = []

    messages: list[dict] = []
    for m in history[-6:]:
        text = m["content"][:500]
        messages.append({"role": m["role"], "content": text or " "})
    messages.append({"role": "user", "content": question})

    system = _SYSTEM.format(today=date.today().isoformat())
    n_calls = 0
    for _ in range(MAX_ROUNDS):
        msg = llm.anthropic_tool_turn(research_model, system, messages, TOOLS)
        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        if not tool_uses:
            break
        messages.append({"role": "assistant", "content": [
            b.model_dump(exclude_none=True) for b in msg.content]})
        results = []
        for tu in tool_uses:
            if n_calls >= MAX_CALLS:
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": "已达调用上限,停止研究,用现有资料回答。"})
                continue
            n_calls += 1
            out, progress = tools.run(tu.name, tu.input or {})
            calls_log.append({"tool": tu.name, "args": tu.input})
            if sum(len(e) for e in evidence) < EVIDENCE_CAP:
                evidence.append(f"### {progress}\n{out}")
            yield {"type": "progress", "text": progress}
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": out})
        messages.append({"role": "user", "content": results})
        if n_calls >= MAX_CALLS:
            break

    # 模型一次工具都没调且没有上文可依(不是追问)→ 兜底自动检索一次,
    # 保证综合阶段起码有资料可用,而不是空手编造
    if not evidence and not history:
        out, progress = tools.run("kb_search", {"query": question})
        evidence.append(f"### {progress}\n{out}")
        calls_log.append({"tool": "kb_search", "args": {"query": question}})
        yield {"type": "progress", "text": progress}

    plan = {"mode": "agent", "research_model": research_model,
            "rounds": min(MAX_ROUNDS, len(calls_log)), "calls": calls_log[:20]}
    return {"tools": tools, "evidence": evidence, "plan": plan}


def _synthesis_prompt(question: str, evidence: list[str],
                      history: list[dict]) -> str:
    hist = _fmt_history(history)
    hist_block = f"之前的对话(供衔接上下文):\n{hist}\n\n" if hist else ""
    if evidence:
        ctx = "\n\n".join(evidence)
        return (
            "根据下面知识库检索到的资料回答用户问题。只基于资料,不要编造;"
            "资料里没有就明说没有;引用时标注资料行首的 [编号]。"
            "提到工具/项目/模型时用一句话说清它是做什么的——优先依据资料原文;"
            "若问题要求穷举,把资料里所有相关条目都覆盖、同类合并,无关的不要罗列。\n\n"
            f"{hist_block}用户问题: {question}\n\n【检索资料】\n{ctx}"
        )
    return (
        "下面是你(知识库助手)与用户之前的对话。请基于对话里已有的内容回答"
        "用户的新问题,不要编造对话里没有的信息;保留原有的 [编号] 引用标记。\n\n"
        f"{hist_block}用户新问题: {question}"
    )


def agent_available(model: str) -> bool:
    """研究阶段能不能跑:有 anthropic key 就行(综合阶段用什么模型都可以)。"""
    return llm.anthropic_model_for_tools(prefer=model) is not None


def run_agent_stream(conn, question: str, model: str,
                     history: list[dict] | None = None) -> Iterator[dict]:
    """agentic 问答(流式)。事件序:
    progress*(研究进度)→ meta(plan+refs)→ delta*(最终回答)。

    研究阶段(meta 之前)抛的异常由 askflow 捕获退回旧管线;
    综合阶段的异常同旧流程,由上层转 error 帧。
    """
    history = history or []
    research_model = llm.anthropic_model_for_tools(prefer=model)
    if research_model is None:
        raise RuntimeError("anthropic 未配置 key,agent 模式不可用")
    state = yield from _research(conn, question, research_model, history)
    tools: _Tools = state["tools"]
    # direct 型(没调工具但有上文):refs 沿用上一条 assistant 的编号体系
    refs = tools.refs
    if not state["evidence"]:
        for m in reversed(history):
            if m["role"] == "assistant" and m.get("refs"):
                refs = list(m["refs"])
                break
    yield {"type": "meta", "plan": state["plan"], "refs": refs}
    prompt = _synthesis_prompt(question, state["evidence"], history)
    for chunk in llm.chat_stream(prompt, model=model):
        yield {"type": "delta", "text": chunk}


def run_agent(conn, question: str, model: str,
              history: list[dict] | None = None) -> dict:
    """agentic 问答(非流式,给 /api/ask 与 CLI)。返回 {answer, refs, plan}。"""
    history = history or []
    research_model = llm.anthropic_model_for_tools(prefer=model)
    if research_model is None:
        raise RuntimeError("anthropic 未配置 key,agent 模式不可用")
    gen = _research(conn, question, research_model, history)
    state = None
    while state is None:
        try:
            next(gen)
        except StopIteration as si:
            state = si.value
    tools: _Tools = state["tools"]
    refs = tools.refs
    if not state["evidence"]:
        for m in reversed(history):
            if m["role"] == "assistant" and m.get("refs"):
                refs = list(m["refs"])
                break
    prompt = _synthesis_prompt(question, state["evidence"], history)
    answer = llm.chat(prompt, model=model)
    return {"answer": answer, "refs": refs, "plan": state["plan"]}
