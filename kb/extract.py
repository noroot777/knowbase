# -*- coding: utf-8 -*-
"""entity/topic/summary 提取。

两条路:
- pre_tagged 来源(dr-youtube-dig):frontmatter 里已有 entity/xxx、topic/xxx
  标签,直接解析进 entities 表,零成本。
- 其他来源(dr-gzh、gzh):调 LLM API 读正文提取,结果进 DB;用哪个模型看设置
  面板的「管道 · 提取打标」阶段;--writeback 时同步回写 md frontmatter 的
  tags(obs 里可见可点)。

回写只动 frontmatter 的 tags 列表,正文一个字节不碰。
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from kb.config import SOURCES, VAULT_ROOT
from kb.norm import entity_norm, kind_norm

# 提取走 kb.llm 的 API 通道,用哪家的哪个模型由设置面板的「管道 · 提取打标」
# 阶段决定(kb.llm_stages 的 "extract");没配就用那里登记的内置默认 gpt-5.5。
# 别在这里写死字面量:改模型要能在面板上看见、也能按供应商分开选。

_PROMPT = """\
你是知识库索引员。读下面这篇文章,提取检索用元数据,只输出一个 JSON 对象,不要任何其他文字:

{{
  "entities": [{{"name": "实体规范名", "kind": "project|product|org|model|other"}}],
  "topics": ["主题词"],
  "summary": "一句话说明文章讲了什么(<=60字)"
}}

规则:
- entities 是文章实际介绍/讨论的具体对象:开源项目名、产品名、公司名、模型名。
  用官方拼写(如 book-to-skill、Claude Code),最多 8 个,只提文章真正着墨的,顺带一提的不要。
- topics 从这个固定候选里选,最多 4 个:AI编程, AI Agent, skill工具, 开源项目, 大模型,
  提示词工程, 知识管理, AI资讯, 效率工具, 机器人, AI视频图像, 其他
- summary 说清楚"是什么+能干什么",让人不点开也知道要不要看。
- JSON 字符串值里不要出现英文双引号,引用词一律用中文引号「」。

标题: {title}

正文:
{body}
"""


def _tag_slug(name: str) -> str:
    s = re.sub(r"\s+", "-", name.strip().lower())
    return re.sub(r"[#,\[\]{}|^]", "", s)


def parse_pretagged(doc) -> dict | None:
    """从已有 tags 解析 entity/topic(pre_tagged 来源的管线产物)。

    条目化来源(dr-video-projects / dr-gzh-projects)的 frontmatter 还带
    project/kind/summary:主实体的 kind、文档 summary 一并取用。"""
    entities, topics = [], []
    meta = getattr(doc, "meta", None) or {}
    proj = str(meta.get("project") or "").strip()
    proj_norm = entity_norm(proj) if proj else None
    proj_kind = kind_norm(str(meta.get("kind") or "")) if meta.get("kind") else None
    for t in doc.tags:
        if t.startswith("entity/"):
            name = t.split("/", 1)[1]
            kind = "other"
            if proj_norm and proj_kind and entity_norm(name) == proj_norm:
                kind = proj_kind
            entities.append({"name": name, "kind": kind})
        elif t.startswith("topic/"):
            topics.append(t.split("/", 1)[1])
    if not entities and not topics:
        return None
    return {"entities": entities, "topics": topics,
            "summary": str(meta.get("summary") or "")}


def extract_with_llm(title: str, body: str, timeout: int = 120) -> dict:
    """调 LLM API 提取。异常往上抛,由调用方决定跳过还是中止。

    模型偶发在字符串值里输出未转义双引号产生坏 JSON(概率性,同文重跑
    通常就好),解析失败原地重试一次;API 报错/超时不重试,留给下轮管道。"""
    last_err: Exception | None = None
    for _ in range(2):
        try:
            return _extract_once(title, body, timeout)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
    raise last_err


def _extract_once(title: str, body: str, timeout: int) -> dict:
    from kb.llm import chat
    from kb.llm_stages import stage_model

    prompt = _PROMPT.format(title=title, body=body[:6000])
    # 产物是一个小 JSON 对象,2048 足够;OpenAI 侧推理 token 另有内部下限兜底
    out = chat(prompt, stage_model("extract"), max_tokens=2048, timeout=timeout)
    # 容错:模型可能包 ```json 围栏
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError(f"输出里没有 JSON: {out[:200]}")
    data = json.loads(m.group(0))
    data.setdefault("entities", [])
    data.setdefault("topics", [])
    data.setdefault("summary", "")
    return data


def save_extraction(conn, path: str, data: dict) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM entities WHERE path=?", (path,))
    for e in data["entities"]:
        name = str(e.get("name") or "").strip()
        if name:
            conn.execute(
                "INSERT OR IGNORE INTO entities (path, entity, kind, norm)"
                " VALUES (?, ?, ?, ?)",
                (path, name, kind_norm(str(e.get("kind") or "")), entity_norm(name)),
            )
    # topic 以 tag 形式统一存(和 pre_tagged 来源对齐)
    for t in data["topics"]:
        t = str(t).strip()
        if t:
            conn.execute(
                "INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
                (path, f"topic/{_tag_slug(t)}"),
            )
    for e in data["entities"]:
        name = str(e.get("name") or "").strip()
        if name:
            conn.execute(
                "INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
                (path, f"entity/{_tag_slug(name)}"),
            )
    conn.execute(
        "UPDATE documents SET extracted_at=?, summary=? WHERE path=?",
        (now, str(data.get("summary") or ""), path),
    )


def writeback_tags(doc_path: str, new_tags: list[str]) -> bool:
    """把新 tag 合并进 md 的 frontmatter tags 列表。只改 frontmatter,不碰正文。"""
    full = VAULT_ROOT / doc_path
    text = full.read_text(encoding="utf-8")
    m = re.match(r"\A(---\n.*?\n---\n)", text, re.S)
    if not m:
        return False
    fm_block, body = m.group(1), text[m.end():]

    existing = set(re.findall(r"^\s*-\s+(\S+)\s*$", fm_block, re.M))
    to_add = [t for t in new_tags if t not in existing]
    if not to_add:
        return False

    add_lines = "".join(f"  - {t}\n" for t in to_add)
    if re.search(r"^tags:\s*$", fm_block, re.M):
        # 已有 tags: 列表 → 追加到列表末尾(即 frontmatter 结束线前)
        new_fm = re.sub(r"\n---\n\Z", f"\n{add_lines}---\n", fm_block)
    else:
        new_fm = re.sub(r"\n---\n\Z", f"\ntags:\n{add_lines}---\n", fm_block)
    full.write_text(new_fm + body, encoding="utf-8")
    return True


def refresh_content_hash(conn, doc_path: str) -> None:
    """writeback 改了 frontmatter 后刷新库里的 hash,避免下次 index 无谓重建。
    正文没变(chunker 只看 body),chunks/FTS/向量都无需动。"""
    import hashlib
    full = VAULT_ROOT / doc_path
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        return
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    conn.execute("UPDATE documents SET content_hash=? WHERE path=?", (h, doc_path))


def pending_docs(conn, limit: int = 0) -> list:
    """还没提取过的、需要走 LLM 的文档(pre_tagged 来源除外)。"""
    pre_tagged_sources = {s.name for s in SOURCES if s.pre_tagged}
    ph = ",".join("?" * len(pre_tagged_sources))
    sql = f"""
        SELECT path, title FROM documents
        WHERE extracted_at IS NULL AND source NOT IN ({ph})
        ORDER BY published DESC
    """
    rows = conn.execute(sql, list(pre_tagged_sources)).fetchall()
    return rows[:limit] if limit > 0 else rows
