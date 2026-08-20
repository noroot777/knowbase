#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wiki 层共享库:实体归一、页面渲染、正文双链化、index 渲染。

wiki 层长在 new-start 里(与 kb 索引同一个 vault):
  source/dr-gzh/...        原始文章(kb 管道产物;linkify 只加双链装饰,索引层
                           的 scanner.normalize_body 保证 chunk/向量不受影响)
  wiki/{tools,models,orgs,concepts}/  实体页(骨架可再生;「近期动态」marker 区半再生)
  wiki/topics/             主题页
  wiki/digest/             每日动态摘要(LLM,不可再生)
  wiki/notes/              问答回填区(纯手写资产,任何脚本不得覆盖)
  index.md / log.md        路由表 / 只追加日志
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data" / "kb.db"
VAULT = Path.home() / "obs" / "new-start"
EPOCH = "2026-06-30"          # digest/synthesis 等 LLM 环节只处理这之后的日期
SOURCE = "dr-gzh"

# 调 claude CLI 时钉死的模型代。别名("sonnet")永远指向当代最新版,新一代发布
# 当天所有批量任务就会静默换模型——贵一截,风格也变。要升代在这里改一个字面量。
SONNET = "claude-sonnet-4-5"

GRAPH_MARK = "<!-- kb-graph -->"
SYN_S, SYN_E = "<!-- synthesis:start -->", "<!-- synthesis:end -->"

KIND_DIR = {"product": "tools", "project": "tools", "model": "models", "org": "orgs"}
KIND_ZH = {"tools": "工具与产品", "models": "模型", "orgs": "公司与组织", "concepts": "概念"}

DISPLAY_OVERRIDE = {
    "gpt": "GPT", "mcp": "MCP", "chatgpt": "ChatGPT", "deepseek": "DeepSeek",
    "openai": "OpenAI", "glm": "GLM", "claude": "Claude", "gemini": "Gemini",
    "cursor": "Cursor", "codex": "Codex", "anthropic": "Anthropic",
    "google": "Google", "slack": "Slack", "github": "GitHub", "n8n": "n8n",
    "mythos": "Mythos", "fable 5": "Fable 5", "agent": "Agent",
    "obsidian": "Obsidian", "ollama": "Ollama", "nvidia": "NVIDIA",
    "qwen": "Qwen", "kimi": "Kimi", "minimax": "MiniMax", "openclaw": "OpenClaw",
}

_DESC_FILE = Path(__file__).parent / "wiki_entity_desc.json"
DESCS: dict = json.loads(_DESC_FILE.read_text()) if _DESC_FILE.exists() else {}


def norm_key(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).lower()
    s = re.sub(r"[-_\s]+", " ", s).strip()
    return re.sub(r"^fable5$", "fable 5", s)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", name).strip()


def connect_ro() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ── 实体宇宙(全库) ────────────────────────────────────────────────────────────

class Universe:
    """dr-gzh 全部已提取文档 + 实体归一 + 页面命名。"""

    def __init__(self, con: sqlite3.Connection, min_docs: int = 2):
        self.docs = {r["path"]: dict(r) for r in con.execute(
            "SELECT path, title, account, published, url, summary FROM documents "
            "WHERE source=? AND extracted_at IS NOT NULL", (SOURCE,))}
        self.variants: dict[str, set] = defaultdict(set)
        self.kinds: dict[str, list] = defaultdict(list)
        self.ent_docs: dict[str, set] = defaultdict(set)
        ph_rows = con.execute(
            "SELECT e.path, e.entity, e.kind FROM entities e "
            "JOIN documents d ON d.path=e.path "
            "WHERE d.source=? AND d.extracted_at IS NOT NULL", (SOURCE,))
        for r in ph_rows:
            k = norm_key(r["entity"])
            if not k or len(k) < 2:
                continue
            self.variants[k].add(r["entity"])
            if r["kind"] and r["kind"] != "other":
                self.kinds[k].append(r["kind"])
            self.ent_docs[k].add(r["path"])

        self.topic_docs: dict[str, set] = defaultdict(set)
        for r in con.execute(
                "SELECT t.path, t.tag FROM doc_tags t JOIN documents d ON d.path=t.path "
                "WHERE d.source=? AND t.tag LIKE 'topic/%'", (SOURCE,)):
            name = r["tag"].split("/", 1)[1]
            if name != "其他":
                self.topic_docs[name].add(r["path"])

        self.page_keys = sorted(
            (k for k, ds in self.ent_docs.items() if len(ds) >= min_docs),
            key=lambda k: -len(self.ent_docs[k]))

        # 页面命名(跨类目去重)
        self.page_file: dict[str, tuple[str, str]] = {}
        used = set()
        for k in self.page_keys:
            name = sanitize_filename(self.display(k))
            base, i = name, 2
            while name.lower() in used:
                name = f"{base} ({i})"
                i += 1
            used.add(name.lower())
            self.page_file[k] = (KIND_DIR.get(self.kind_of(k), "concepts"), name)
        self.name_to_key = {v[1]: k for k, v in self.page_file.items()}

        # 每篇文档 → 有页面的实体
        self.doc_ents: dict[str, list] = defaultdict(list)
        for k in self.page_keys:
            for p in self.ent_docs[k]:
                self.doc_ents[p].append(k)

    def display(self, k: str) -> str:
        if k in DISPLAY_OVERRIDE:
            return DISPLAY_OVERRIDE[k]
        # 末位字典序决胜:变体排序必须跨进程稳定,否则页面名会漂移出重复页
        return sorted(self.variants[k], key=lambda v: (v.islower(), -len(v), v))[0]

    def kind_of(self, k: str) -> str:
        return self.kinds[k][0] if self.kinds[k] else "other"

    def ent_link(self, k: str, label: str | None = None) -> str:
        name = self.page_file[k][1]
        return f"[[{name}|{label}]]" if label and label != name else f"[[{name}]]"

    def doc_link(self, p: str) -> str:
        d = self.docs[p]
        return f"[[{p[:-3]}|{d['title']}]]"

    def cooc(self, k: str, top: int = 10) -> list[tuple[str, int]]:
        cnt: dict[str, int] = defaultdict(int)
        for p in self.ent_docs[k]:
            for o in self.doc_ents[p]:
                if o != k:
                    cnt[o] += 1
        return sorted(cnt.items(), key=lambda x: -x[1])[:top]


# ── 页面渲染 ────────────────────────────────────────────────────────────────────

def _keep_synthesis(existing: str | None) -> str:
    """从旧页面里摘出「## 近期动态」marker 区(含标题),没有返回空串。"""
    if not existing or SYN_S not in existing:
        return ""
    m = re.search(r"## 近期动态\n\n" + re.escape(SYN_S) + r".*?" + re.escape(SYN_E) + r"\n",
                  existing, re.S)
    return (m.group(0) + "\n") if m else ""


def render_entity_page(u: Universe, k: str, existing: str | None = None,
                       list_cap: int = 30) -> str:
    sub, name = u.page_file[k]
    kd = u.kind_of(k)
    kind_zh = {"product": "产品/工具", "project": "开源项目",
               "model": "模型", "org": "公司/组织"}.get(kd, "概念")
    ds = sorted(u.ent_docs[k], key=lambda p: u.docs[p]["published"], reverse=True)
    alias = sorted({v for v in u.variants[k] if v != name})

    L = ["---", f"kind: {kd}"]
    if alias:
        L.append("aliases:")
        L += [f"  - {a}" for a in alias]
    L += ["---", "", f"# {name}", ""]
    desc = DESCS.get(name) or DESCS.get(k)
    if desc:
        L += [desc, ""]
    L += [f"> 类型:{kind_zh} · 共 {len(ds)} 篇文章提到,最近 {u.docs[ds[0]]['published'][:10]}", ""]

    syn = _keep_synthesis(existing)
    if syn:
        L.append(syn.rstrip("\n"))
        L.append("")

    rel = u.cooc(k)
    if rel:
        L += ["## 相关", ""]
        L += [f"- {u.ent_link(r)}(共同出现 {c} 篇)" for r, c in rel]
        L.append("")

    L += ["## 出现在这些文章", ""]
    for p in ds[:list_cap]:
        d = u.docs[p]
        L.append(f"- {u.doc_link(p)} — {d['account']} · {d['published'][:10]}")
        if d["summary"]:
            s = d["summary"].strip().replace("\n", " ")
            L.append(f"  - {s[:80]}{'…' if len(s) > 80 else ''}")
    if len(ds) > list_cap:
        L.append(f"- *…另有 {len(ds) - list_cap} 篇更早的文章(可用 kb 搜索查看)*")
    L.append("")
    return "\n".join(L)


def render_topic_page(u: Universe, t: str, list_cap: int = 30) -> str:
    ds = sorted(u.topic_docs[t], key=lambda p: u.docs[p]["published"], reverse=True)
    ds = [p for p in ds if p in u.docs]
    cnt: dict[str, int] = defaultdict(int)
    for p in ds:
        for k in u.doc_ents[p]:
            cnt[k] += 1
    L = [f"# 主题:{t}", "", f"> 共 {len(ds)} 篇文章。", "", "## 高频实体", ""]
    for k, c in sorted(cnt.items(), key=lambda x: -x[1])[:12]:
        L.append(f"- {u.ent_link(k)}({c} 篇)")
    L += ["", f"## 文章(最近 {min(list_cap, len(ds))} 篇)", ""]
    for p in ds[:list_cap]:
        d = u.docs[p]
        L.append(f"- {u.doc_link(p)} — {d['account']} · {d['published'][:10]}")
    L.append("")
    return "\n".join(L)


def render_index(u: Universe, vault: Path, recent_days: int = 14) -> str:
    from datetime import date, timedelta
    L = ["# 索引(Index)", "",
         "wiki 层的路由表:先看这里,再顺着链接走。原始文章在 `source/dr-gzh/`,",
         "实体/主题/摘要页在 `wiki/`,问答沉淀在 `wiki/notes/`。", ""]

    digests = sorted((vault / "wiki" / "digest").glob("*.md"), reverse=True)
    if digests:
        L += ["## 动态摘要(Digest)", "",
              "每日要闻与趋势信号;回答\"最近有哪些大事\"先看这里。", "",
              " · ".join(f"[[wiki/digest/{p.stem}|{p.stem}]]" for p in digests[:14]), ""]

    notes = [p for p in sorted((vault / "wiki" / "notes").glob("*.md"))
             if not p.stem.startswith("_")]
    if notes:
        L += ["## 笔记(Query 回填)", "",
              " · ".join(f"[[wiki/notes/{p.stem}|{p.stem}]]" for p in notes), ""]

    by_sub: dict[str, list] = defaultdict(list)
    for k in u.page_keys:
        sub, name = u.page_file[k]
        by_sub[sub].append((len(u.ent_docs[k]), name))
    for sub in ("tools", "models", "orgs", "concepts"):
        items = sorted(by_sub[sub], reverse=True)
        if items:
            L += [f"## {KIND_ZH[sub]}({len(items)})", "",
                  " · ".join(f"[[{n}]]" for _, n in items), ""]

    tops = sorted(u.topic_docs.items(), key=lambda x: -len(x[1]))
    L += ["## 主题", "",
          " · ".join(f"[[主题-{t}|{t}({len(ds)})]]" for t, ds in tops), ""]

    cutoff = (date.today() - timedelta(days=recent_days)).isoformat()
    recent = [d for d in u.docs.values() if d["published"][:10] >= cutoff]
    by_date: dict[str, list] = defaultdict(list)
    for d in recent:
        by_date[d["published"][:10]].append(d)
    L += [f"## 文章(最近 {recent_days} 天,共 {len(recent)} 篇)", ""]
    for dt in sorted(by_date, reverse=True):
        L.append(f"### {dt}")
        L.append("")
        for d in sorted(by_date[dt], key=lambda x: x["published"], reverse=True):
            L.append(f"- {u.doc_link(d['path'])} — {d['account']}")
        L.append("")
    return "\n".join(L)


# ── 正文双链化 ──────────────────────────────────────────────────────────────────

def linkify_doc(u: Universe, p: str) -> str | None:
    """给一篇文章加双链装饰。已处理过(带 GRAPH_MARK)或文件缺失返回 None。

    正文中每个有页面的实体首次出现 → [[页名|原文写法]];
    文末加 GRAPH_MARK 图谱尾区(实体+主题连边)。
    """
    full = VAULT / p
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        return None
    if GRAPH_MARK in text:
        return None
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    fm, body = (m.group(0), text[m.end():]) if m else ("", text)

    keys = u.doc_ents.get(p, [])
    lines = body.split("\n")
    in_code, linked = False, set()
    names = sorted(((k, v) for k in keys
                    for v in u.variants[k] | {u.display(k)}),
                   key=lambda kv: -len(kv[1]))
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or "](" in line or "http" in line or line.startswith("#") or "[[" in line:
            continue
        for k, v in names:
            if k in linked or len(v) < 2:
                continue
            if re.fullmatch(r"[\x00-\x7f]+", v):
                pat = re.compile(r"(?<![A-Za-z0-9_\-\[])" + re.escape(v)
                                 + r"(?![A-Za-z0-9_\-\]])", re.I)
            else:
                pat = re.compile(re.escape(v))
            mm = pat.search(line)
            if mm:
                line = line[:mm.start()] + u.ent_link(k, mm.group(0)) + line[mm.end():]
                linked.add(k)
        lines[i] = line
    body = "\n".join(lines)

    topics = sorted(t for t, ds in u.topic_docs.items() if p in ds)
    ents = [u.ent_link(k) for k in sorted(keys, key=lambda k: -len(u.ent_docs[k]))]
    tail = [f"\n{GRAPH_MARK}\n\n---\n"]
    if ents:
        tail.append("**本文实体**:" + " · ".join(ents) + "\n")
    if topics:
        tail.append("**主题**:" + " · ".join(f"[[主题-{t}|{t}]]" for t in topics) + "\n")
    return fm + body.rstrip("\n") + "\n" + "".join(tail)


# ── 杂项 ────────────────────────────────────────────────────────────────────────

def upsert_marker_section(text: str, block_body: str, n_docs: int, today: str | None = None) -> str:
    """把 synthesis 块写进实体页(marker 内替换,无则插在「> 类型」行后)。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    inner = (f"{SYN_S}\n{block_body}\n\n"
             f"*(AI 综合于 {today},基于近期 {n_docs} 篇文章)*\n{SYN_E}")
    if SYN_S in text:
        return re.sub(re.escape(SYN_S) + r".*?" + re.escape(SYN_E), inner, text, flags=re.S)
    block = f"## 近期动态\n\n{inner}\n"
    m = re.search(r"^> 类型:.*$\n", text, re.M)
    pos = m.end() if m else max(text.find("\n## "), 0)
    return text[:pos] + "\n" + block + text[pos:]


def synthesis_doc_count(text: str) -> int | None:
    """页面里上次综合时的篇数(没有 synthesis 返回 None)。"""
    m = re.search(r"基于近期 (\d+) 篇文章", text)
    return int(m.group(1)) if m else None


def append_log(vault: Path, op: str, detail: str) -> None:
    log = vault / "log.md"
    if not log.exists():
        log.write_text("# 摄取日志(Log)\n", encoding="utf-8")
    today = datetime.now().strftime("%Y-%m-%d")
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n## [{today}] {op} | {detail}\n")
