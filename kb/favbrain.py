# -*- coding: utf-8 -*-
"""收藏智能层:行动卡提取 + 待试清单分诊。

设计动机:收藏的语义是"待试备忘",压力来自库存要自己记、自己筛。这里把两件事
交给 LLM:
- extract_action:收藏瞬间(后台线程)把"这条收藏要试的东西"提炼成行动卡
  {name, what, cmd, eta, kind},写回 favorites.action_json。
- run_triage:把所有 inbox 条目整体分诊成 hero/quick/deep/dup/cold/quotes,
  结果缓存在 data/fav_triage.json,带条目指纹——inbox 一变缓存即视为过时
  (仍可展示,前端提示重新分诊)。

模型用 llm.get_fav_model()(设置面板可选,默认回落问答默认模型)。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading

from kb import llm, store
from kb.config import DATA_DIR, VAULT_ROOT
from kb.favorite_metadata import FAVORITE_DOCUMENT_COLUMNS, FAVORITE_DOCUMENT_JOIN

_TRIAGE_PATH = DATA_DIR / "fav_triage.json"

# 片段来源的中文名。给人读的只有收藏面板那一处(`kb/static/js/fav.js` 自己有
# 一份同样的表),这里这一份是喂给模型的 —— 没有文档的收藏拿它当"出处"。
ORIGIN_LABEL = {"doc": "知识库文章", "daily": "每日日报",
                "chat": "知识问答", "feed": "订阅内容"}

# 分诊运行状态(进程内;kb-web 常驻,与管道同模式)
_state_lock = threading.Lock()
_state = {"running": False, "error": ""}


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _read_body(path: str | None, limit: int) -> str:
    """读文章正文(去 frontmatter),截断到 limit 字。

    `path` 可以为空:日报正文、问答回答、还没导出的订阅条目收下来的片段没有
    vault 文档(见 `kb.store_migrate.FAVORITES_TABLE`)。那时行动卡只能围着引文本身
    做 —— 拿 `VAULT_ROOT / None` 去读是 TypeError,会把整条行动卡打掉。
    """
    if not path:
        return ""
    try:
        text = (VAULT_ROOT / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    body = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)
    return body[:limit]


def _parse_json(text: str) -> dict | None:
    """从 LLM 输出里挖 JSON:优先 ```json 块,其次首个 { 到末个 }。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = [m.group(1)] if m else []
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        candidates.append(text[a:b + 1])
    for c in candidates:
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    return None


# ── 行动卡提取 ────────────────────────────────────────────────────────────────

_ACTION_PROMPT = """你在帮用户整理知识库收藏。用户收藏的语义是"看到好东西没时间试,先记下"。
请把这条收藏提炼成一张可直接行动的"行动卡"。

文章标题:{title}
出处:{account} · {published}
摘要:{summary}
{quote_block}正文(截断):
{body}

只输出一个 JSON 对象,不要其它文字:
{{
  "name": "要试的工具/项目名;没有明确工具就给这条收藏起个≤12字的短名",
  "what": "一句话说明是什么、对用户的价值,≤40字",
  "cmd": "最小上手命令(npx xxx / git clone url / pip install xxx 等);文中找不到就留空",
  "eta": "预估上手耗时:~10 分钟 / ~1 小时 / 半天+;纯阅读类写 阅读约X分钟",
  "kind": "try=可动手试的工具 / read=值得读的文章教程 / reference=摘抄引用无需行动"
}}"""


def extract_action(fav_id: int) -> dict | None:
    """给一条收藏生成行动卡并写库。同步执行(调用方决定放不放后台线程)。
    失败静默返回 None——行动卡是增强,不能生成也不影响收藏本身。"""
    conn = store.connect()
    try:
        # LEFT JOIN:没有 path 的收藏(日报/问答/未导出的订阅条目)在内连接下
        # 一行都查不到 —— 行动卡静默不生成,面板上那一条永远只是一段引文。
        # 元数据回落到收藏那一刻的快照。
        row = conn.execute(
            "SELECT f.id, f.path, f.quote, f.note, f.origin, f.created_at,"
            + FAVORITE_DOCUMENT_COLUMNS + FAVORITE_DOCUMENT_JOIN
            + " WHERE f.id=?",
            (fav_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    quote_block = (
        f"用户只收藏了文中这一段(行动卡应该围绕这段话):\n{row['quote']}\n\n"
        if row["quote"] else ""
    )
    # 出处那两格在没有文档的来源上是空的(日报正文、问答回答都不是某个公众号
    # 在某天发的)。留空会在 prompt 里渲染成「出处: · 」,模型只能照着一行标点
    # 猜;填 origin 的中文名 + 收藏日期,它至少知道这段话是自己库里的产物。
    account = row["account"] or ORIGIN_LABEL.get(row["origin"], "知识库")
    published = (row["published"] or "")[:10]
    published = f"发布于 {published}" if published else f"收藏于 {(row['created_at'] or '')[:10]}"
    prompt = _ACTION_PROMPT.format(
        title=row["title"], account=account, published=published,
        summary=row["summary"], quote_block=quote_block,
        body=_read_body(row["path"], 3000),
    )
    try:
        raw = llm.chat(prompt, llm.get_fav_model(), max_tokens=1000, timeout=120)
    except RuntimeError:
        return None
    action = _parse_json(raw)
    if not action or "name" not in action:
        return None
    action = {
        "name": str(action.get("name", ""))[:40],
        "what": str(action.get("what", ""))[:80],
        "cmd": str(action.get("cmd", ""))[:200],
        "eta": str(action.get("eta", ""))[:20],
        "kind": action.get("kind") if action.get("kind") in ("try", "read", "reference") else "try",
    }
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE favorites SET action_json=? WHERE id=?",
            (json.dumps(action, ensure_ascii=False), fav_id),
        )
        conn.commit()
    finally:
        conn.close()
    return action


def extract_action_async(fav_id: int) -> None:
    threading.Thread(target=extract_action, args=(fav_id,), daemon=True).start()


# ── 分诊 ────────────────────────────────────────────────────────────────────

_TRIAGE_PROMPT = """你在帮用户分诊他的"待试收藏清单"。他的痛点:收藏越堆越多、压力越大、
想不起哪个值得先试。请替他把清单分好类,砍掉该砍的。今天是 {today}。

待试条目(JSON 数组;saved_at=收藏时间,action=已提取的行动卡,quote=只收藏了这段话):
{items}

只输出一个 JSON 对象,不要其它文字:
{{
  "hero": {{"id": 0, "why": "为什么现在最值得先试它,≤50字,口语化"}},
  "quick": [{{"id": 0}}],
  "deep": [{{"id": 0}}],
  "dup": [{{"ids": [1, 2], "verdict": "方向如何重合+建议,≤60字", "keep": 1}}],
  "cold": [{{"id": 0, "why": "为什么大概凉了,≤30字"}}],
  "quotes": [0]
}}
规则:
- 每个条目 id 恰好出现在 quick/deep/dup/cold/quotes 五组之一,不重不漏
- quick=15分钟内能上手;deep=要整块时间;dup=方向明显重合的一组(keep 为建议保留者,确实重合才给);
  cold=发布久远/时效已过/明显不会再碰;quotes=摘抄引用性质(多为片段收藏),无需行动
- hero 从 quick 里挑,没有 quick 再从 deep 挑;id 必须真实存在
- why/verdict 是给用户看的:用工具名/文章名指代,绝不出现 id 数字"""


def _fingerprint(rows) -> str:
    sig = json.dumps([[r["id"], r["updated_at"]] for r in rows], sort_keys=True)
    return hashlib.sha1(sig.encode()).hexdigest()[:16]


def _inbox_rows():
    conn = store.connect()
    try:
        # LEFT JOIN,理由同 extract_action:内连接会把没有 path 的那批条目从
        # 分诊里整个漏掉 —— 它们照旧堆在待试清单上,却永远分不到任何一组
        # (前端兜底把未覆盖的塞进"新收藏,还没分诊",于是分诊跑完它们还在那儿)。
        # **指纹也是按这批行算的**(`_fingerprint`),漏掉等于收藏新增一条日报
        # 片段时缓存仍判"新鲜"。
        return conn.execute(
            "SELECT f.id, f.quote, f.note, f.action_json, f.created_at, f.updated_at,"
            + FAVORITE_DOCUMENT_COLUMNS + FAVORITE_DOCUMENT_JOIN
            + " WHERE f.status='inbox' ORDER BY f.id",
        ).fetchall()
    finally:
        conn.close()


def _read_cache() -> dict:
    try:
        return json.loads(_TRIAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def triage_status() -> dict:
    """给 GET /api/favorites/triage:运行状态 + 缓存结果 + 是否仍新鲜。"""
    cache = _read_cache()
    fresh = bool(cache) and cache.get("fingerprint") == _fingerprint(_inbox_rows())
    with _state_lock:
        return {
            "running": _state["running"], "error": _state["error"],
            "fresh": fresh, "data": cache.get("data"),
            "generated_at": cache.get("generated_at", ""),
            "model": cache.get("model", ""),
        }


def run_triage() -> None:
    """全量分诊并写缓存。由 API 层放后台线程跑,状态经 triage_status 轮询。"""
    with _state_lock:
        if _state["running"]:
            return
        _state.update(running=True, error="")
    try:
        rows = _inbox_rows()
        if not rows:
            _TRIAGE_PATH.write_text(json.dumps({
                "generated_at": _now(), "model": "", "fingerprint": _fingerprint(rows),
                "data": {"hero": None, "quick": [], "deep": [], "dup": [],
                         "cold": [], "quotes": []},
            }, ensure_ascii=False), encoding="utf-8")
            return
        items = []
        for r in rows:
            it = {"id": r["id"], "title": r["title"], "account": r["account"],
                  "published": (r["published"] or "")[:10],
                  "saved_at": (r["created_at"] or "")[:10],
                  "summary": r["summary"]}
            if r["quote"]:
                it["quote"] = r["quote"][:300]
            if r["note"]:
                it["note"] = r["note"]
            if r["action_json"]:
                try:
                    it["action"] = json.loads(r["action_json"])
                except ValueError:
                    pass
            items.append(it)
        model = llm.get_fav_model()
        raw = llm.chat(
            _TRIAGE_PROMPT.format(today=_now()[:10],
                                  items=json.dumps(items, ensure_ascii=False)),
            model, max_tokens=4000, timeout=180,
        )
        data = _parse_json(raw)
        if not data:
            raise RuntimeError("分诊输出不是有效 JSON")
        # 兜底清洗:只留真实 id,防 LLM 幻觉
        valid = {r["id"] for r in rows}
        def ids_of(lst, key="id"):
            return [x for x in (lst or []) if isinstance(x, dict) and x.get(key) in valid]
        clean = {
            "hero": data.get("hero") if isinstance(data.get("hero"), dict)
                    and data["hero"].get("id") in valid else None,
            "quick": ids_of(data.get("quick")),
            "deep": ids_of(data.get("deep")),
            "dup": [g for g in (data.get("dup") or []) if isinstance(g, dict)
                    and isinstance(g.get("ids"), list)
                    and all(i in valid for i in g["ids"]) and len(g["ids"]) >= 2],
            "cold": ids_of(data.get("cold")),
            "quotes": [i for i in (data.get("quotes") or []) if i in valid],
        }
        # 不漏:没被分到组的条目兜底进 quick(有行动卡)/quotes(纯片段)
        seen = ({clean["hero"]["id"]} if clean["hero"] else set())
        seen |= {x["id"] for x in clean["quick"] + clean["deep"] + clean["cold"]}
        seen |= {i for g in clean["dup"] for i in g["ids"]}
        seen |= set(clean["quotes"])
        for r in rows:
            if r["id"] not in seen:
                (clean["quotes"] if r["quote"] else clean["quick"]).append(
                    r["id"] if r["quote"] else {"id": r["id"]})
        _TRIAGE_PATH.write_text(json.dumps({
            "generated_at": _now(), "model": model,
            "fingerprint": _fingerprint(rows), "data": clean,
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        with _state_lock:
            _state["error"] = str(e)
    finally:
        with _state_lock:
            _state["running"] = False


def run_triage_async() -> bool:
    """启动后台分诊;已在跑返回 False。"""
    with _state_lock:
        if _state["running"]:
            return False
    threading.Thread(target=run_triage, daemon=True).start()
    return True
