# -*- coding: utf-8 -*-
"""工具图鉴数据层:dr-gzh-projects / dr-video-projects 条目按 entity_norm 聚合。

条目 md 的 frontmatter 自带 project/kind/summary/project_url(转写时产出),
这里直接扫文件聚合,不走 kb.db —— 展示要的"每工具多来源记录"粒度只有文件层有。
缓存按 (文件数, 最大 mtime) 失效:新条目入库或重转写后自动重建。
"""

from __future__ import annotations

from collections import Counter
from hashlib import blake2b
from pathlib import Path
from threading import Lock

from kb.config import VAULT_ROOT
from kb.norm import entity_norm, kind_norm
from kb.scanner import parse_frontmatter

PROJECT_SOURCES = ("dr-gzh-projects", "dr-video-projects")

_lock = Lock()
_cache: dict = {"sig": None, "entries": None, "groups": None}


def _roots() -> list[Path]:
    return [VAULT_ROOT / "source" / s for s in PROJECT_SOURCES]


def _scan_sig() -> tuple[int, float]:
    n, mt = 0, 0.0
    for root in _roots():
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            n += 1
            m = p.stat().st_mtime
            if m > mt:
                mt = m
    return (n, mt)


def _load_entry(path: Path) -> dict | None:
    try:
        fm, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    name = str(fm.get("project") or "").strip()
    if not name:
        return None
    tags = fm.get("tags") or []
    return {
        "path": str(path.relative_to(VAULT_ROOT)),
        "norm": entity_norm(name),
        "name": name,
        "kind": kind_norm(str(fm.get("kind") or "")),
        "summary": str(fm.get("summary") or ""),
        "project_url": str(fm.get("project_url") or ""),
        "title": str(fm.get("title") or path.stem),
        # 公众号条目用 account/url/article_title,视频条目用 channel/youtube/video_title
        "account": str(fm.get("account") or fm.get("channel") or ""),
        "url": str(fm.get("url") or fm.get("youtube") or ""),
        "article_title": str(fm.get("article_title") or fm.get("video_title") or ""),
        "published": str(fm.get("published") or ""),
        "source": str(fm.get("source") or ""),
        "topics": [t.split("/", 1)[1] for t in tags
                   if isinstance(t, str) and t.startswith("topic/")],
    }


def _pick_url(urls: list[str]) -> str:
    """多条记录的 project_url 可能不一致:优先 github,其次第一个非空。"""
    for u in urls:
        if "github.com" in u:
            return u
    return urls[0] if urls else ""


def _promo_conn():
    """给这一轮 _ensure 用的库连接;拿不到就返回 None(调用方一律降级)。

    图鉴本来是「纯扫文件」的,加了软广过滤之后才需要库。库不在/schema 旧不该
    让图鉴整个打不开,所以这里把异常吞掉,由 _promo_sig / _apply_promo 各自
    按 conn is None 退回加这层之前的行为。
    """
    try:
        from kb import store
        return store.connect()
    except Exception:
        return None


def _promo_sig(conn) -> tuple:
    """影响 _apply_promo 的库状态签名,和文件签名一起决定缓存要不要重建。

    条目文件的 mtime 看不见软广判定的变化:补判改的是**原文**那一行
    documents.promo_level,衍生条目文件一个字节都没动;hard 原文写回的
    `flag/软广` 也写在原文 md 上。只按文件签名缓存的话,跑完
    promo_backfill 刷新图鉴,刚判成 hard 的工具条目还在原地,除非重启服务
    (第 1 轮审查发现)。

    签名取的是**逐篇状态的摘要**,不是聚合量。聚合量会碰撞:按档位分组的
    「计数 + MAX(promo_at)」在「两篇同一秒对调 hard/soft」时完全不变,可图鉴
    该显示的卡片已经换了一张(第 2 轮审查发现)。promo_at 只到秒,一轮
    backfill 六个并发同秒改判很常见,这不是理论情形。

    摘要里是 (path, promo_level, url) 三元组:前两个决定「这篇是不是广告」,
    url 决定「哪些衍生条目跟着它走」(promo_level_sql 按 url 回溯继承)。
    promo_at 不进摘要——它变而结论没变时本来就不需要重建。
    没判过的文档不进摘要,它们对图鉴没有影响;拿不到库时返回固定值,行为
    退回加这层之前的样子。

    每个图鉴请求都要算一遍,所以量过:1888 篇已判文档 8.0 ms,而同一次请求里
    `_scan_sig` 扫 10213 个条目文件要 70.8 ms —— 摘要只占既有开销的一成,
    不值得为省这几毫秒换成「逐行异或免排序」那种要额外论证的写法。
    """
    if conn is None:
        return ()
    try:
        h = blake2b(digest_size=16)
        n = 0
        for r in conn.execute(
            "SELECT path, promo_level, url FROM documents"
            " WHERE promo_level <> '' ORDER BY path"
        ):
            h.update(f"{r['path']}\0{r['promo_level']}\0{r['url']}\0".encode())
            n += 1
        return (n, h.hexdigest())
    except Exception:
        return ()


def _apply_promo(entries: list[dict], conn) -> list[dict]:
    """按软广判定过滤/标注条目:hard 的整条丢掉,soft 的打上标记。

    图鉴是唯一不走 kb.search / kb.aggregate 的展示面(它按文件聚合,那个
    「每工具多来源记录」的粒度只有文件层有),所以 promo_exclude_sql 那条
    统一口径覆盖不到这里——一篇 hard 投放稿拆出来的工具条目原来照样在图鉴
    里占一行。判定按条目自己的 path 查库,档位由 store.promo_level_sql 从
    同 url 的原文继承(条目自己永远没判过)。

    conn 为 None(库不在/查不动)时降级成「全部保留、不打标记」,和加这层
    之前的行为一致。
    """
    if not entries or conn is None:
        return entries
    try:
        from kb import store
        levels = store.effective_promo_levels(conn, [e["path"] for e in entries])
    except Exception:
        return entries
    kept = []
    for e in entries:
        lvl = levels.get(e["path"], "")
        if lvl == "hard":
            continue
        kept.append({**e, "promo_level": lvl})
    return kept


def _build(conn) -> tuple[list[dict], dict[str, dict]]:
    entries: list[dict] = []
    for root in _roots():
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            e = _load_entry(p)
            if e:
                entries.append(e)
    # 过滤放在分组之前:提及次数/号数/首末时间都是按条目算的,先分组再丢
    # 条目的话这些计数还带着广告那一份
    entries = _apply_promo(entries, conn)

    groups: dict[str, dict] = {}
    for e in entries:
        g = groups.setdefault(e["norm"], {"entries": []})
        g["entries"].append(e)

    for norm, g in groups.items():
        es = sorted(g["entries"], key=lambda x: x["published"], reverse=True)
        g["entries"] = es
        names = Counter(x["name"] for x in es)
        kinds = Counter(x["kind"] for x in es if x["kind"] != "other")
        topics = Counter(t for x in es for t in x["topics"])
        kind = kinds.most_common(1)[0][0] if kinds else "other"
        # 筛选要全量(top-N 截断会让"排第 5 的主题"筛不出该工具);
        # 展示仍取前 4,并去掉与 kind 徽章重复的"开源项目",位置留给更具体的标签
        ranked = [t for t, _ in topics.most_common()]
        display = [t for t in ranked
                   if not (kind == "project" and t == "开源项目")][:4]
        g["meta"] = {
            "norm": norm,
            "name": names.most_common(1)[0][0],
            "kind": kind,
            "summary": next((x["summary"] for x in es if x["summary"]), ""),
            "url": _pick_url([x["project_url"] for x in es if x["project_url"]]),
            "n": len(es),
            "sources": sorted({x["source"] for x in es}),
            "accounts": len({x["account"] for x in es if x["account"]}),
            "latest": es[0]["published"][:10],
            "first": es[-1]["published"][:10],
            "topics": display,
            "all_topics": ranked,
            # 卡片角标:这个工具**每一条**记录都出自 soft 软广时才标。有一条
            # 中立报道就不标——那说明它是被正经讨论过的,只是也有人恰饭。
            "promo_soft": all(x.get("promo_level") == "soft" for x in es),
        }
    return entries, groups


def _ensure() -> None:
    """缓存签名 = 条目文件签名 + 软广判定签名,少一半都会漏更新。

    一条连接同时喂给取签名和重建:重建本来就要查库,分开开两次等于每个图鉴
    请求多付一次 connect(那里面还有 sqlite_vec 扩展加载和几条建表/建索引)。
    """
    file_sig = _scan_sig()
    conn = _promo_conn()
    try:
        sig = (file_sig, _promo_sig(conn))
        with _lock:
            if _cache["sig"] != sig:
                _cache["entries"], _cache["groups"] = _build(conn)
                _cache["sig"] = sig
    finally:
        if conn is not None:
            conn.close()


def list_tools() -> dict:
    """图鉴列表:全部工具的聚合卡片数据 + 类别/主题计数。"""
    _ensure()
    groups = _cache["groups"]
    tools = sorted((g["meta"] for g in groups.values()),
                   key=lambda m: (m["latest"], m["n"]), reverse=True)
    kind_counts = Counter(m["kind"] for m in tools)
    topic_counts: Counter = Counter()
    for m in tools:
        topic_counts.update(m["all_topics"])  # 与筛选同口径,chip 计数 = 点击后的结果数
    return {
        "tools": tools,
        "kinds": dict(kind_counts),
        "topics": [{"value": t, "n": n} for t, n in topic_counts.most_common(24)],
        "total_entries": len(_cache["entries"]),
    }


def all_groups() -> list[dict]:
    """全部工具分组的 {**meta, "entries": [...]}(条目不含正文)。

    给图谱这类「要一次拿到所有分组 + 每组出自哪些文章」的只读消费方用:
    list_tools 只给 meta(没有条目明细),get_tool 会把每条的正文 md 读进来
    (5000 多组逐个调等于把整棵条目树读一遍)。走同一份 _ensure 缓存,
    软广过滤、分组口径都和图鉴完全一致。
    """
    _ensure()
    return [{**g["meta"], "entries": g["entries"]} for g in _cache["groups"].values()]


def get_tool(norm: str) -> dict | None:
    """单个工具的全部记录(含正文 md,给详情抽屉渲染)。"""
    _ensure()
    g = _cache["groups"].get(norm)
    if g is None:
        return None
    entries = []
    for e in g["entries"]:
        body = ""
        try:
            _fm, body = parse_frontmatter(
                (VAULT_ROOT / e["path"]).read_text(encoding="utf-8"))
        except OSError:
            pass
        # 首行的出处引用(> 整理自… / > 转写自…)详情页自己展示 meta,去掉防重复
        lines = body.lstrip().split("\n")
        if lines and lines[0].startswith("> ") and ("整理自" in lines[0] or "转写自" in lines[0]):
            lines = lines[1:]
        entries.append({**e, "body": "\n".join(lines).strip()})
    return {**g["meta"], "urls": sorted({x["project_url"] for x in g["entries"]
                                         if x["project_url"]}),
            "entries": entries}
