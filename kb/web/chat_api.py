# -*- coding: utf-8 -*-
"""问答 API:会话增删查、流式/非流式提问、把回答回填成 vault 笔记。

问答本身在 kb.askflow(内部优先走 kb.askagent 的 agentic 检索);
这里负责会话持久化(conversations/messages 表)与 NDJSON 流式外壳。
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from kb import llm, store
from kb.askflow import filter_refs_to_cited as askflow_filter_refs
from kb.askflow import run_ask, run_ask_stream
from kb.config import VAULT_ROOT
from kb.web.common import VAULT_NAME, now as _now

router = APIRouter()


class AskRequest(BaseModel):
    query: str
    model: str | None = None              # None = 用 llm.DEFAULT_MODEL
    conversation_id: int | None = None   # None = 新开一个会话



# ── 问答回填 notes ──────────────────────────────────────────────────────────────
# 把值得留的回答存进 vault 的 wiki/notes/(Karpathy 式 Query 回填,知识复利)。
# notes 区是手写资产:只新建文件,从不覆盖;引用转成指向 source 文章的双链。

class NoteRequest(BaseModel):
    question: str
    answer: str
    refs: list[dict] = []


_CITE_RE = re.compile(r"\[(\d+)\]")


def linkify_citations(answer: str, refs: list[dict]) -> str:
    """把正文里的 `[n]` 换成指向来源文章的 Obsidian 内部链接。

    网页端是在渲染时把 `[n]` 替换成 docA 链接的(chat.js),存盘的是模型原文,
    所以笔记里正文的 `[n]` 一直是纯文本——只有末尾「## 引用」列表能点。

    **必须用 markdown 链接形式,不能用双链**:wikilink 的 alias 不允许出现
    方括号(Obsidian 官方把 `[`/`]` 列为会让链接失效的字符),`[[路径|[1]]]`
    会被解析器吃掉;去掉方括号写成 `[[路径|1]]` 又会让连着的几个引用糊成
    「123」,认不出边界。`[\\[1\\]](路径.md)` 显示成 `[1]`、点得动,而且
    文本里没有连续的 `[[`,不会被 wikilink 扫描误吞。
    路径整体 URL 编码(空格→%20,括号→%28/%29),否则路径里的 `)` 会把
    markdown 链接提前截断——vault 里的文件名带括号很常见。
    """
    by_n: dict[str, dict] = {}
    for r in refs:
        if str(r.get("path") or ""):
            by_n[str(r.get("n"))] = r

    def one(m: re.Match) -> str:
        r = by_n.get(m.group(1))
        if r is None:
            return m.group(0)
        href = quote(str(r["path"]))
        title = str(r.get("title") or "").replace('"', "'").strip()
        return f"[\\[{m.group(1)}\\]]({href}" + (f' "{title}")' if title else ")")

    return _CITE_RE.sub(one, answer)


@router.post("/api/notes")
def api_save_note(body: NoteRequest):
    q, ans = body.question.strip(), body.answer.strip()
    if not ans:
        raise HTTPException(400, "答案不能为空")
    notes_dir = VAULT_ROOT / "wiki" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    title = re.sub(r'[\\/:*?"<>|#^\[\]\n]+', " ", q or ans)[:40].strip() or "未命名笔记"
    path, i = notes_dir / f"{title}.md", 2
    while path.exists():
        path = notes_dir / f"{title} ({i}).md"
        i += 1

    ref_lines = []
    for r in body.refs:
        p = str(r.get("path") or "")
        t = str(r.get("title") or p or "?")
        # alias 里出现 [ ] | # ^ 会让整条双链失效,标题里带这些字符的文章不少
        alias = re.sub(r"[\[\]|#^]+", " ", t).strip() or "来源"
        link = f"[[{p.removesuffix('.md')}|{alias}]]" if p else t
        extra = " · ".join(x for x in (str(r.get("account") or ""),) if x)
        url = str(r.get("url") or "")
        ref_lines.append(f"{r.get('n', '-')}. {link}"
                         + (f" — {extra}" if extra else "")
                         + (f" · [原文]({url})" if url else ""))

    md = (f"---\ncreated: \"{_now()}\"\nsource: kb-web-ask\n---\n\n"
          f"# {title}\n\n> 问题:{q}\n\n{linkify_citations(ans, body.refs)}\n")
    if ref_lines:
        md += "\n## 引用\n\n" + "\n".join(ref_lines) + "\n"
    path.write_text(md, encoding="utf-8")

    log = VAULT_ROOT / "log.md"
    if not log.exists():
        log.write_text("# 摄取日志(Log)\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n## [{_now()[:10]}] note | {path.stem}\n")

    rel = str(path.relative_to(VAULT_ROOT))
    obs = f"obsidian://open?vault={quote(VAULT_NAME)}&file={quote(rel.removesuffix('.md'))}"
    return {"ok": True, "path": rel, "obsidian": obs}


# ── 问答会话(chatbox) ─────────────────────────────────────────────────────────

@router.get("/api/conversations")
def list_conversations():
    """会话列表,按最近更新排序。"""
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT c.id, c.title, c.updated_at,"
            " (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS n_messages"
            " FROM conversations c ORDER BY c.updated_at DESC"
        ).fetchall()
        return {"conversations": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/api/conversations/{cid}")
def get_conversation(cid: int):
    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if c is None:
            raise HTTPException(404, "会话不存在")
        msgs = []
        for m in conn.execute(
            "SELECT id, role, content, refs_json, model, created_at"
            " FROM messages WHERE conversation_id=? ORDER BY id", (cid,)
        ):
            refs = json.loads(m["refs_json"] or "[]")
            for r in refs:
                r["vault"] = VAULT_NAME
            msgs.append({"id": m["id"], "role": m["role"], "content": m["content"],
                         "refs": refs, "model": m["model"], "created_at": m["created_at"]})
        return {"id": c["id"], "title": c["title"], "messages": msgs}
    finally:
        conn.close()


@router.delete("/api/conversations/{cid}", status_code=204)
def delete_conversation(cid: int):
    conn = store.connect()
    try:
        cur = conn.execute("DELETE FROM conversations WHERE id=?", (cid,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "会话不存在")
    finally:
        conn.close()


def _load_history(conn, cid: int | None) -> list[dict]:
    """校验会话存在并取其历史消息(供规划指代消解 + 综合上下文)。cid=None → []。"""
    if cid is None:
        return []
    if conn.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone() is None:
        raise HTTPException(404, "会话不存在")
    return [
        {"role": m["role"], "content": m["content"],
         "refs": json.loads(m["refs_json"] or "[]")}
        for m in conn.execute(
            "SELECT role, content, refs_json FROM messages"
            " WHERE conversation_id=? ORDER BY id", (cid,),
        )
    ]


def _save_qa(conn, cid: int | None, q: str, answer: str, refs: list, plan: dict,
             model: str) -> tuple[int, int, int]:
    """一轮问答落库:新会话则建会话,写 user + assistant 两行并提交。

    回 `(会话 id, 提问那条消息 id, 回答那条消息 id)`。**两个消息 id 是要往
    `done` 帧里带的**:页面上那两条气泡靠它们证明「我已经在库里了」,片段收藏
    的归属核验也认它(见 `kb/web/favorites_api.py` 的 `_chat_message_id`)。
    只回会话 id 的话,客户端只能拿「当前是哪个对话」当归属 —— 而那在流提前
    结束时会指向一个从来没有这句话的会话。
    """
    now = _now()
    if cid is None:
        title = q[:30] + ("…" if len(q) > 30 else "")
        cid = conn.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, now, now),
        ).lastrowid
    else:
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, cid))
    uid = conn.execute(
        "INSERT INTO messages (conversation_id, role, content, model, created_at)"
        " VALUES (?, 'user', ?, ?, ?)", (cid, q, model, now),
    ).lastrowid
    aid = conn.execute(
        "INSERT INTO messages (conversation_id, role, content, refs_json,"
        " plan_json, model, created_at) VALUES (?, 'assistant', ?, ?, ?, ?, ?)",
        (cid, answer, json.dumps(refs, ensure_ascii=False),
         json.dumps(plan, ensure_ascii=False), model, _now()),
    ).lastrowid
    conn.commit()
    return cid, uid, aid


@router.post("/api/ask")
def api_ask(body: AskRequest):
    """多轮问答:意图规划(枚举/检索/直接追问)→ 执行 → LLM 综合。

    同步阻塞 20-90s(非流式,保留作兼容/兜底;前端走 /api/ask/stream)。
    成功后才把 user+assistant 两条消息落库,失败时不留半截记录(前端保留输入,可重试)。
    """
    q = body.query.strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    model = body.model or llm.get_default_model()
    conn = store.connect()
    try:
        history = _load_history(conn, body.conversation_id)
        try:
            result = run_ask(conn, q, model=model, history=history)
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        result["refs"] = askflow_filter_refs(result["answer"], result["refs"])
        cid, _uid, _aid = _save_qa(conn, body.conversation_id, q, result["answer"],
                                   result["refs"], result["plan"], model)
    finally:
        conn.close()
    for r in result["refs"]:
        r["vault"] = VAULT_NAME
    return {"conversation_id": cid, "answer": result["answer"],
            "refs": result["refs"], "plan": result["plan"]}


@router.post("/api/ask/stream")
def api_ask_stream(body: AskRequest):
    """流式问答(NDJSON,每行一帧)。前端据此边收边显示、可随时停止。

    帧类型:progress*(agent 研究进度,可为 0 条)→ meta(plan+refs+model)→
    若干 delta(text)→ done(conversation_id+refs)。出错发 error 帧且不落库(同非流式失败行为)。客户端断开(用户点停止)→ finally 里
    stream.close() 把 GeneratorExit 穿透到 llm.chat_stream 关闭底层流,后端生成随之停止,
    半截回答一律不落库。会话不存在的 404 在开始流之前校验,能正常返回。
    """
    q = body.query.strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    model = body.model or llm.get_default_model()
    conn = store.connect()
    try:
        history = _load_history(conn, body.conversation_id)   # 404 在开流前抛,干净
    except BaseException:
        conn.close()
        raise

    def frame(**kw) -> str:
        return json.dumps(kw, ensure_ascii=False) + "\n"

    def gen():
        stream = run_ask_stream(conn, q, model=model, history=history)
        try:
            parts: list[str] = []
            refs: list = []
            plan = None
            try:
                for ev in stream:
                    if ev["type"] == "meta":
                        refs, plan = ev["refs"], ev["plan"]
                        yield frame(type="meta", plan=plan, refs=refs, model=model)
                    elif ev["type"] == "progress":
                        # agent 研究进度(检索/枚举步骤),前端显示在气泡阶段行
                        yield frame(type="progress", text=ev["text"])
                    else:
                        parts.append(ev["text"])
                        yield frame(type="delta", text=ev["text"])
            except RuntimeError as e:
                yield frame(type="error", message=str(e))
                return                       # 失败不落库
            # 正常收完 → 只留实际引用的来源 → 落库 + done
            answer = "".join(parts).strip()
            refs = askflow_filter_refs(answer, refs)
            cid, uid, aid = _save_qa(
                conn, body.conversation_id, q, answer, refs, plan, model)
            for r in refs:
                r["vault"] = VAULT_NAME
            # **`done` 帧是「这一轮已经在库里了」唯一的凭证**,所以把那两条消息
            # 的 id 一起带回去:页面拿它给气泡打身份,片段收藏把它发回来由
            # `favorites_api._chat_message_id` 再核一次。正常 EOF 替代不了这一帧
            # (传输提前结束时也是正常 EOF,而那一轮一个字都没落库)。
            yield frame(type="done", conversation_id=cid, refs=refs,
                        user_message_id=uid, message_id=aid)
        finally:
            stream.close()   # 断开时确保关闭底层 LLM 流(真停止);已耗尽则无操作
            conn.close()

    return StreamingResponse(gen(), media_type="application/x-ndjson")
