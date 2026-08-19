#!/usr/bin/env python3
"""从 kb.db 生成 Karpathy LLM-wiki 形式的独立演示 vault。

只读来源:
  - /Users/fjh/code/knowledge-base/data/kb.db
  - /Users/fjh/obs/new-start/source/dr-gzh/  (文章正文与图片,只读拷贝)
输出(全新目录,不碰 new-start):
  - /Users/fjh/obs/llm-wiki-demo/
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

DB = "/Users/fjh/code/knowledge-base/data/kb.db"
SRC_VAULT = Path("/Users/fjh/obs/new-start")
OUT = Path("/Users/fjh/obs/llm-wiki-demo")
SINCE = "2026-06-30"
ENTITY_MIN_DOCS = 2
TOPIC_MIN_DOCS = 3
DESC_FILE = Path("/tmp/wiki_entity_desc.json")  # 实体简介,人工填写
LIST_ONLY = "--list" in sys.argv

KIND_DIR = {"product": "tools", "project": "tools", "model": "models", "org": "orgs"}

DISPLAY_OVERRIDE = {
    "gpt": "GPT", "mcp": "MCP", "chatgpt": "ChatGPT", "deepseek": "DeepSeek",
    "openai": "OpenAI", "glm": "GLM", "claude": "Claude", "gemini": "Gemini",
    "cursor": "Cursor", "codex": "Codex", "anthropic": "Anthropic",
    "google": "Google", "slack": "Slack", "github": "GitHub", "n8n": "n8n",
    "mythos": "Mythos", "fable 5": "Fable 5", "agent": "Agent",
    "obsidian": "Obsidian", "ollama": "Ollama", "nvidia": "NVIDIA",
    "qwen": "Qwen", "kimi": "Kimi", "minimax": "MiniMax",
}


def norm_key(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).lower()
    s = re.sub(r"[-_\s]+", " ", s).strip()
    s = re.sub(r"^fable5$", "fable 5", s)
    return s


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", name).strip()


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    docs = con.execute(
        "SELECT path, title, account, published, url, summary FROM documents "
        "WHERE source='dr-gzh' AND published>=? AND extracted_at IS NOT NULL "
        "ORDER BY published", (SINCE,),
    ).fetchall()
    doc_paths = [d["path"] for d in docs]
    ph = ",".join("?" * len(doc_paths))

    rows = con.execute(
        f"SELECT path, entity, kind FROM entities WHERE path IN ({ph})", doc_paths
    ).fetchall()
    tag_rows = con.execute(
        f"SELECT path, tag FROM doc_tags WHERE tag LIKE 'topic/%' AND path IN ({ph})",
        doc_paths,
    ).fetchall()

    # --- 实体归一 ---
    variants = defaultdict(set)          # key -> {原始写法}
    kinds = defaultdict(list)            # key -> [kind,...]
    ent_docs = defaultdict(set)          # key -> {doc path}
    for r in rows:
        k = norm_key(r["entity"])
        if not k or len(k) < 2:
            continue
        variants[k].add(r["entity"])
        if r["kind"] and r["kind"] != "other":
            kinds[k].append(r["kind"])
        ent_docs[k].add(r["path"])

    page_keys = sorted(
        (k for k, ds in ent_docs.items() if len(ds) >= ENTITY_MIN_DOCS),
        key=lambda k: -len(ent_docs[k]),
    )

    def display(k):
        if k in DISPLAY_OVERRIDE:
            return DISPLAY_OVERRIDE[k]
        cands = sorted(variants[k], key=lambda v: (v.islower(), -len(v)))
        return cands[0]

    def kind_of(k):
        return kinds[k][0] if kinds[k] else "other"

    if LIST_ONLY:
        for k in page_keys:
            print(f"{display(k)}\t{kind_of(k)}\t{len(ent_docs[k])}")
        return

    descs = json.loads(DESC_FILE.read_text()) if DESC_FILE.exists() else {}

    # --- 主题 ---
    topic_docs = defaultdict(set)
    for r in tag_rows:
        name = r["tag"].split("/", 1)[1]
        topic_docs[name].add(r["path"])
    topics = {t: ds for t, ds in topic_docs.items() if len(ds) >= TOPIC_MIN_DOCS and t != "其他"}

    # --- 反向映射 ---
    doc_ents = defaultdict(list)   # path -> [key](本文中、且有页面的实体)
    for k in page_keys:
        for p in ent_docs[k]:
            doc_ents[p].append(k)
    doc_topics = defaultdict(list)
    for t, ds in topics.items():
        for p in ds:
            doc_topics[p].append(t)

    # 共现
    cooc = defaultdict(lambda: defaultdict(int))
    for p, ks in doc_ents.items():
        for a in ks:
            for b in ks:
                if a != b:
                    cooc[a][b] += 1

    doc_by_path = {d["path"]: d for d in docs}

    if OUT.exists():
        precious = (
            list((OUT / "wiki" / "digest").glob("*.md"))
            + [f for f in (OUT / "wiki" / "notes").glob("*.md") if f.stem != "_说明"]
        )
        has_synthesis = any(
            "<!-- synthesis:start -->" in f.read_text(encoding="utf-8")
            for sub in ("tools", "models", "orgs", "concepts")
            for f in (OUT / "wiki" / sub).glob("*.md")
        ) if (OUT / "wiki").exists() else False
        if (precious or has_synthesis) and "--force-rebuild" not in sys.argv:
            sys.exit(
                "已退役:vault 含不可再生内容(digest/notes/synthesis),整库重建会毁掉它们。\n"
                "确要重建请自行备份后加 --force-rebuild。"
            )
        shutil.rmtree(OUT)
    (OUT / ".obsidian").mkdir(parents=True)
    (OUT / "wiki" / "topics").mkdir(parents=True)
    for sub in set(KIND_DIR.values()) | {"concepts"}:
        (OUT / "wiki" / sub).mkdir(parents=True, exist_ok=True)

    page_file = {}   # key -> (子目录, 文件名)
    used_names = set()
    for k in page_keys:
        name = sanitize_filename(display(k))
        base = name
        i = 2
        while name.lower() in used_names:
            name = f"{base} ({i})"
            i += 1
        used_names.add(name.lower())
        page_file[k] = (KIND_DIR.get(kind_of(k), "concepts"), name)

    def ent_link(k, label=None):
        _, name = page_file[k]
        if label and label != name:
            return f"[[{name}|{label}]]"
        return f"[[{name}]]"

    def doc_link(p):
        d = doc_by_path[p]
        rel = "sources/" + "/".join(p.split("/")[2:])  # source/dr-gzh/DATE/f.md -> sources/DATE/f.md
        return f"[[{rel[:-3]}|{d['title']}]]"

    # --- 文章拷贝 + 双链化 ---
    asset_count = 0
    link_count = 0
    for d in docs:
        p = d["path"]
        src = SRC_VAULT / p
        rel_parts = p.split("/")[2:]          # DATE/file.md
        dst = OUT / "sources" / Path(*rel_parts)
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")

        m = re.match(r"^---\n.*?\n---\n", text, re.S)
        fm, body = (m.group(0), text[m.end():]) if m else ("", text)

        # 复制引用图片
        for rel in set(re.findall(r"\]\((assets/[^)\s]+)\)", body)):
            a_src = src.parent / rel
            if a_src.exists():
                a_dst = dst.parent / rel
                a_dst.parent.mkdir(parents=True, exist_ok=True)
                if not a_dst.exists():
                    shutil.copy2(a_src, a_dst)
                    asset_count += 1

        # 正文首次出现的实体 -> 双链(跳过代码块/链接/URL 行内片段)
        lines = body.split("\n")
        in_code = False
        linked = set()
        names_by_len = sorted(
            ((k, v) for k in doc_ents[p] for v in variants[k] | {display(k)}),
            key=lambda kv: -len(kv[1]),
        )
        for i, line in enumerate(lines):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                continue
            if in_code or "](" in line or "http" in line or line.startswith("#"):
                continue
            for k, v in names_by_len:
                if k in linked or len(v) < 2:
                    continue
                if re.fullmatch(r"[\x00-\x7f]+", v):
                    pat = re.compile(
                        r"(?<![A-Za-z0-9_\-\[])" + re.escape(v) + r"(?![A-Za-z0-9_\-\]])",
                        re.I,
                    )
                else:
                    pat = re.compile(re.escape(v))
                mm = pat.search(line)
                if mm:
                    line = line[: mm.start()] + ent_link(k, mm.group(0)) + line[mm.end():]
                    linked.add(k)
                    link_count += 1
            lines[i] = line
        body = "\n".join(lines)

        # 底部图谱区:保证实体/主题连边齐全
        tail = []
        ents = [ent_link(k) for k in sorted(doc_ents[p], key=lambda k: -len(ent_docs[k]))]
        tops = [f"[[主题-{t}|{t}]]" for t in sorted(doc_topics[p])]
        if ents or tops:
            tail.append("\n---\n")
            if ents:
                tail.append("**本文实体**:" + " · ".join(ents) + "\n")
            if tops:
                tail.append("**主题**:" + " · ".join(tops) + "\n")
        dst.write_text(fm + body + "".join(tail), encoding="utf-8")

    # --- 实体 wiki 页 ---
    for k in page_keys:
        sub, name = page_file[k]
        kd = kind_of(k)
        kind_zh = {"product": "产品/工具", "project": "开源项目", "model": "模型", "org": "公司/组织"}.get(kd, "概念")
        alias = sorted({v for v in variants[k] if v != name})
        lines = ["---", f"kind: {kd}"]
        if alias:
            lines.append("aliases:")
            lines += [f"  - {a}" for a in alias]
        lines += ["---", "", f"# {name}", ""]
        desc = descs.get(name) or descs.get(k)
        if desc:
            lines += [desc, ""]
        lines += [f"> 类型:{kind_zh} · 近期 {len(ent_docs[k])} 篇文章提到", ""]

        rel = sorted(cooc[k].items(), key=lambda x: -x[1])[:8]
        if rel:
            lines += ["## 相关", ""]
            lines += [f"- {ent_link(r)}(共同出现 {c} 篇)" for r, c in rel]
            lines.append("")

        lines += ["## 出现在这些文章", ""]
        for p in sorted(ent_docs[k], key=lambda p: doc_by_path[p]["published"], reverse=True):
            d = doc_by_path[p]
            lines.append(f"- {doc_link(p)} — {d['account']} · {d['published'][:10]}")
            if d["summary"]:
                s = d["summary"].strip().replace("\n", " ")
                lines.append(f"  - {s[:80]}{'…' if len(s) > 80 else ''}")
        lines.append("")
        (OUT / "wiki" / sub / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")

    # --- 主题页 ---
    for t, ds in sorted(topics.items(), key=lambda x: -len(x[1])):
        lines = [f"# 主题:{t}", "", f"> 近期 {len(ds)} 篇文章。", "", "## 高频实体", ""]
        cnt = defaultdict(int)
        for p in ds:
            for k in doc_ents[p]:
                cnt[k] += 1
        for k, c in sorted(cnt.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"- {ent_link(k)}({c} 篇)")
        lines += ["", "## 文章", ""]
        for p in sorted(ds, key=lambda p: doc_by_path[p]["published"], reverse=True):
            d = doc_by_path[p]
            lines.append(f"- {doc_link(p)} — {d['account']} · {d['published'][:10]}")
        lines.append("")
        (OUT / "wiki" / "topics" / f"主题-{t}.md").write_text("\n".join(lines), encoding="utf-8")

    # --- index.md ---
    zh = {"tools": "工具与产品", "models": "模型", "orgs": "公司与组织", "concepts": "概念"}
    idx = ["# 索引(Index)", "",
           "这是整个 wiki 的路由表:先看这里,再顺着链接走。每个实体页底部有反向链接,文章页底部有图谱区。", ""]
    by_sub = defaultdict(list)
    for k in page_keys:
        sub, name = page_file[k]
        by_sub[sub].append((len(ent_docs[k]), name))
    for sub in ["tools", "models", "orgs", "concepts"]:
        items = sorted(by_sub[sub], reverse=True)
        if not items:
            continue
        idx += [f"## {zh[sub]}", "", " · ".join(f"[[{n}]]" for _, n in items), ""]
    idx += ["## 主题", "", " · ".join(
        f"[[主题-{t}|{t}]]" for t, _ in sorted(topics.items(), key=lambda x: -len(x[1]))), ""]
    idx += ["## 文章(按日期)", ""]
    by_date = defaultdict(list)
    for d in docs:
        by_date[d["published"][:10]].append(d)
    for date in sorted(by_date, reverse=True):
        idx.append(f"### {date}")
        idx.append("")
        for d in sorted(by_date[date], key=lambda d: d["published"], reverse=True):
            idx.append(f"- {doc_link(d['path'])} — {d['account']}")
        idx.append("")
    (OUT / "index.md").write_text("\n".join(idx), encoding="utf-8")

    # --- log.md ---
    n_pages = len(page_keys)
    (OUT / "log.md").write_text(
        "# 摄取日志(Log)\n\n"
        "## [2026-07-05] ingest | 首次批量摄取\n\n"
        f"从公众号知识库(kb.db)导入 {len(docs)} 篇 dr-gzh 文章"
        f"(发布于 {SINCE} ~ 2026-07-04),生成 {n_pages} 个实体页、{len(topics)} 个主题页,"
        f"正文内插入 {link_count} 处双链,复制图片 {asset_count} 张。"
        "来源:~/obs/new-start/source/dr-gzh + ~/code/knowledge-base(只读)。\n",
        encoding="utf-8",
    )

    # --- CLAUDE.md ---
    (OUT / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")
    (OUT / ".obsidian" / "app.json").write_text("{}\n", encoding="utf-8")
    (OUT / ".obsidian" / "graph.json").write_text(json.dumps({
        "colorGroups": [
            {"query": "path:wiki/tools", "color": {"a": 1, "rgb": 5431378}},
            {"query": "path:wiki/models", "color": {"a": 1, "rgb": 11621088}},
            {"query": "path:wiki/orgs", "color": {"a": 1, "rgb": 16744272}},
            {"query": "path:wiki/concepts", "color": {"a": 1, "rgb": 5419488}},
            {"query": "path:wiki/topics", "color": {"a": 1, "rgb": 15258703}},
            {"query": "path:sources", "color": {"a": 1, "rgb": 8421504}},
        ],
        "showTags": False, "showAttachments": False, "hideUnresolved": True,
        "showOrphans": False,
    }, ensure_ascii=False), encoding="utf-8")

    print(f"docs={len(docs)} entity_pages={n_pages} topic_pages={len(topics)} "
          f"inline_links={link_count} assets={asset_count}")
    print("OUT:", OUT)


CLAUDE_MD = """# LLM Wiki 规则(本 vault 的路由器)

这是一个 Karpathy LLM-wiki 形式的知识库演示,数据来自公众号文章知识库。

## 结构

- `sources/YYYY-MM-DD/*.md` — 原始文章(相当于 raw,但已是 Markdown)。frontmatter 带 title/account/published/url/tags;正文中实体首次出现处是双链;底部有"本文实体/主题"图谱区。
- `wiki/tools|models|orgs|concepts/*.md` — 实体页:一个实体一页,含简介、相关实体(共现)、出现过的文章列表。
- `wiki/topics/主题-*.md` — 主题页:聚合同一主题下的文章与高频实体。
- `index.md` — 路由表:所有实体页、主题页、文章按日期的入口。先读这里。
- `log.md` — 摄取日志:每次批量导入记一条。

## 摄取新内容时(增量,不要重建)

1. 新文章放入 `sources/YYYY-MM-DD/`,保留 frontmatter。
2. 提取文中实体:已有实体页则在其"出现在这些文章"里追加;新实体出现≥2 篇才建页,按 kind 放入对应子目录。
3. 正文中每个实体的首次出现替换为 `[[实体页名|原文写法]]`;文末补"本文实体/主题"图谱区。
4. 更新 `index.md` 对应分区与日期区;在 `log.md` 追加一条记录。
5. 判断依据优先看 `index.md`、`log.md` 和反向链接,不要全库扫描。

## 命名

- 实体页文件名 = 规范显示名(如 `Claude Code.md`),变体写进 frontmatter `aliases`。
- kind 分桶:product/project → tools,model → models,org → orgs,其余 → concepts。

## 三大操作(照 Karpathy gist)

- **Ingest**:见上。`sources/` 等同 gist 的 raw 层,**不可变**——只读不改,是事实来源;wiki 层归 LLM 所有。
- **Query**:回答问题时先读 `index.md` 再钻入具体页;有价值的答案要**回填成新的 wiki 页**(放 `wiki/notes/`),不要消失在对话里。
- **Lint**:定期检查——页面间矛盾、被新文章取代的旧结论、无入链的孤儿页、被多次提到却没有页面的实体、缺失的交叉链接。

## 日志格式

`log.md` 只追加,条目统一前缀 `## [YYYY-MM-DD] <操作> | <说明>`,可用 `grep "^## \[" log.md | tail -5` 快速取最近记录。
"""

if __name__ == "__main__":
    main()
