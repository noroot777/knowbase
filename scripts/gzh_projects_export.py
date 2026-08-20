#!/usr/bin/env python3
"""公众号文章 → 知识库工具/项目条目导出。

dr-gzh 里大量「N 个开源项目推荐」「工具介绍/评测」文章一篇混着多个工具:
LLM extract 的实体反链有上限截断,也没有每个工具的独立介绍页。这条管道
把它们按工具拆成独立条目(与 scripts/video_projects_export.py 同构):

  1. gate:       haiku 判断文章是否「工具/项目介绍型」(single/multi)
  2. transcribe: sonnet 按工具切分,每个工具一篇独立条目 md

产物落 ~/obs/new-start/source/dr-gzh-projects/YYYY-MM-DD/<uid>-<slug>.md,
frontmatter 自带 entity/topic 标签(pre_tagged 路线,kb index 后无需 extract)。

候选取自 kb.db 里 source in (dr-gzh, gzh) 的文档;克隆号互相转发的重复文
按正文哈希去重(同 hash 只处理发布最早的一篇,其余记为 dup)。
状态在 data/gzh_projects_state.json,按文档 path 增量,可随时中断重跑;
gate/transcribe 不要与自身并发跑(state 文件互相覆盖)。

用法:
  uv run python scripts/gzh_projects_export.py scan
  uv run python scripts/gzh_projects_export.py gate [--limit N]
  uv run python scripts/gzh_projects_export.py report
  uv run python scripts/gzh_projects_export.py transcribe [--limit N] [--doc PATH] [--out DIR] [--force] [--model NAME]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.config import DB_PATH, VAULT_ROOT  # noqa: E402
from kb.scanner import normalize_body, parse_frontmatter  # noqa: E402

GZH_SOURCES = ("dr-gzh", "gzh")
OUT_ROOT = VAULT_ROOT / "source" / "dr-gzh-projects"
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "gzh_projects_state.json"
TZ = timezone(timedelta(hours=8))

GATE_MODEL = "haiku"
# 钉死模型代:别名("sonnet")永远指向当代最新版,新一代发布当天这条批量管道
# 就会静默换模型(贵一截,风格也变)。要升代在这里改字面量,或临时传 --model。
TRANSCRIBE_MODEL = "claude-sonnet-4-5"
GATE_WORKERS = 8
TRANSCRIBE_WORKERS = 6
MIN_BODY = 200          # 正文太短(空壳/纯图文)不进管道
GATE_HEAD = 2400        # gate 只看正文开头
TRANSCRIBE_BODY_CAP = 45000

TOPIC_CANDIDATES = "AI编程, AI Agent, skill工具, 开源项目, 大模型, 提示词工程, 知识管理, AI资讯, 效率工具, 机器人, AI视频图像, 其他"

GATE_PROMPT = """\
你是文章内容分类员。判断下面这篇公众号文章是否是「工具/项目介绍型」,只输出一个 JSON 对象。

算「工具/项目介绍型」的:文章的主要内容是介绍具体的软件项目/工具/产品/模型——\
它是什么、能干什么、怎么用、表现如何。
  - 集中介绍/评测/上手一个工具、项目或模型 → style=single
  - 「N 个开源项目/工具」推荐合集、逐段介绍多个具体产品的资讯速览 → style=multi
不算的:纯方法论教程(教你怎么用已熟知的工具组合完成某件事)、行业评论/观点分析、
公司融资八卦/人事新闻、个人成长经验分享、技术知识科普/面试题讲解、卖课卖会员的营销文。
边界:判断标准是「有没有逐个介绍具体项目/产品的能力与用法」——新模型/新产品发布报道
只要讲了能力/价格/用法就算;只拿工具名当由头谈观点趋势的不算。

输出格式(单个 JSON,无其他文字):
{{"project_article": true|false, "style": "single"|"multi"|null, "confidence": "high"|"medium"|"low", "reason": "一句话理由,用中文"}}

文章信息:
标题: {title}
公众号: {account}
发布: {published}
已提取实体(可能不全): {entities}
一句话摘要: {summary}

正文开头(截断):
{body_head}
"""

TRANSCRIBE_PROMPT = """\
你是技术知识库的编辑。下面是一篇公众号文章,里面介绍了一个或多个软件项目/工具/产品/模型。\
你的任务:把其中每一个被实质介绍的对象,整理成一篇独立的中文知识库条目。

对每个对象输出一个条目,格式严格如下(每个条目以 ===PROJECT=== 单独一行开始):

===PROJECT===
name: 项目官方名称(保留英文原名)
url: 项目主页/仓库链接(从正文里挑该项目自己的链接,如「开源地址」;没有就留空)
kind: open-source|product|model|research|other
entities: 逗号分隔,该条目实际着墨的实体(项目名、公司名、模型名,官方拼写,最多 6 个)
topics: 逗号分隔,从固定候选选最多 3 个:{topic_candidates}。\
项目本体是 Agent Skill/SKILL.md 指令集/技能包的必须含 skill工具;\
平台或产品只是内置技能功能、普通工具只是也能当 skill 安装的,不算 skill工具
summary: 一句话(<=60 字)说清「是什么+能干什么」,不要英文双引号
---BODY---
(条目正文,markdown)

正文整理要求:
- 这是「整理」不是「摘要」:原文关于这个对象的实质信息全部保留——功能、用法、安装命令、\
参数、star 数、价格、benchmark、硬件要求、作者的评价与吐槽,一个都不丢;原文表述好的可直接复用
- 结构:开头 1-2 句导语;然后用 ### 小节或要点列表组织;关键名词和数字用 **加粗**;\
短段落短句,便于扫读
- 图片引用、二维码、「点击关注」「往期推荐」、广告与推广段落全部去掉
- 作者的主观判断保留,但标明是文章作者的观点(如「作者认为…」)
- 严禁添加原文没有的信息,严禁脑补细节
- 只被点名、没有实质介绍的东西不单独成条(可在相关条目里带一句)

如果文章其实不是工具/项目介绍(没有一个可成条的对象),只输出一行:===NONE===

文章信息:
标题: {title}
公众号: {account}
发布日期: {published}
原文链接: {url}

正文:
{body}
"""


# ---------------------------------------------------------------- 基础设施

def _clean_env() -> dict:
    """给 claude CLI 子进程一个干净环境。

    宿主(会话/kb-web)可能带 ANTHROPIC_BASE_URL 等托管变量,claude 子进程
    继承后会走错凭据全部失败,必须从零构造。"""
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    for k in ("HOME", "USER", "LOGNAME", "TZ", "LANG", "LC_ALL"):
        v = os.environ.get(k)
        if v:
            env[k] = v
    return env


def _claude(prompt: str, model: str, timeout: int) -> str:
    proc = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=timeout,
        env=_clean_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI 退出码 {proc.returncode}: {proc.stderr[:200]}")
    return proc.stdout.strip()


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"docs": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _slug(name: str) -> str:
    s = re.sub(r"\s+", "-", name.strip().lower())
    s = re.sub(r"[^\w\-.一-鿿]", "", s)
    return s.strip("-.") or "unnamed"


_LOCK_FILE = None  # 持引用防 GC 释放锁


def _acquire_lock() -> None:
    """gate/transcribe 对 state 是全文件覆盖写,绝不能并发。
    拿不到锁退出码 3(管道据此把条目化阶段标为跳过)。"""
    global _LOCK_FILE
    import fcntl
    lock_path = STATE_PATH.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_FILE = open(lock_path, "w")
    try:
        fcntl.flock(_LOCK_FILE, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[lock] 已有 gate/transcribe 实例在跑,退出(state 不能并发写)", flush=True)
        sys.exit(3)


# ---------------------------------------------------------------- 扫描

def scan_candidates() -> dict[str, dict]:
    """从 kb.db 取 gzh 文档,读正文并按正文哈希去重。

    返回 {canonical_path: {title, account, published, url, summary,
                           entities, body, hash, dups}},
    canonical = 同正文里发布最早的一篇。"""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ph = ",".join("?" * len(GZH_SOURCES))
    docs = conn.execute(
        f"SELECT path, title, account, published, url, summary FROM documents"
        f" WHERE source IN ({ph}) ORDER BY published, path",
        list(GZH_SOURCES),
    ).fetchall()
    ents: dict[str, list[str]] = {}
    for r in conn.execute(
        f"SELECT e.path, e.entity FROM entities e JOIN documents d ON d.path=e.path"
        f" WHERE d.source IN ({ph})", list(GZH_SOURCES)
    ):
        ents.setdefault(r["path"], []).append(r["entity"])
    conn.close()

    by_hash: dict[str, dict] = {}
    short = 0
    for r in docs:
        try:
            text = (VAULT_ROOT / r["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        _, body = parse_frontmatter(text)
        body = normalize_body(body).strip()
        if len(body) < MIN_BODY:
            short += 1
            continue
        h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        if h in by_hash:
            by_hash[h]["dups"].append(r["path"])
            continue
        by_hash[h] = {
            "path": r["path"], "title": r["title"], "account": r["account"],
            "published": r["published"], "url": r["url"],
            "summary": r["summary"] or "", "entities": ents.get(r["path"], []),
            "body": body, "hash": h, "dups": [],
        }
    out = {v["path"]: v for v in by_hash.values()}
    out["_stats"] = {"total": len(docs), "unique": len(by_hash), "short": short}
    return out


def _pop_stats(cands: dict) -> dict:
    return cands.pop("_stats", {"total": 0, "unique": 0, "short": 0})


# ---------------------------------------------------------------- gate

def gate_one(info: dict) -> dict:
    prompt = GATE_PROMPT.format(
        title=info["title"], account=info["account"],
        published=(info["published"] or "")[:10],
        entities=", ".join(info["entities"][:12]) or "(无)",
        summary=info["summary"] or "(无)",
        body_head=info["body"][:GATE_HEAD],
    )
    out = _claude(prompt, GATE_MODEL, timeout=120)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError(f"gate 输出无 JSON: {out[:200]}")
    data = json.loads(m.group(0))
    return {
        "project_article": bool(data.get("project_article")),
        "style": data.get("style"),
        "confidence": data.get("confidence") or "low",
        "reason": str(data.get("reason") or "")[:200],
        "checked_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }


def cmd_gate(args) -> int:
    state = load_state()
    cands = scan_candidates()
    _pop_stats(cands)
    todo = [(p, info) for p, info in cands.items()
            if "gate" not in state["docs"].get(p, {})]
    if args.limit:
        todo = todo[: args.limit]
    print(f"[gate] 去重后候选 {len(cands)},待分类 {len(todo)},{GATE_WORKERS} 并发", flush=True)
    ok = failed = 0
    with ThreadPoolExecutor(max_workers=GATE_WORKERS) as pool:
        futs = {pool.submit(gate_one, info): (p, info) for p, info in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            p, info = futs[fut]
            try:
                gate = fut.result()
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(todo)}] FAIL {info['title'][:40]}: {str(e)[:100]}", flush=True)
                continue
            try:
                rec = state["docs"].setdefault(p, {})
                rec.update({"title": info["title"], "account": info["account"],
                            "published": info["published"], "url": info["url"],
                            "hash": info["hash"], "dups": info["dups"], "gate": gate})
                save_state(state)
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(todo)}] STATE-FAIL {info['title'][:40]}: {str(e)[:100]}", flush=True)
                continue
            ok += 1
            if i % 25 == 0 or i == len(todo):
                print(f"[{i}/{len(todo)}] ok={ok} fail={failed}", flush=True)
    print(f"[gate] 完成: 成功 {ok},失败 {failed}(失败的下次重跑)", flush=True)
    return 0


def cmd_report(args) -> int:
    state = load_state()
    docs = [(p, r) for p, r in state["docs"].items() if "gate" in r]
    passed = [x for x in docs if x[1]["gate"]["project_article"]]
    rejected = [x for x in docs if not x[1]["gate"]["project_article"]]

    def _line(p, r):
        g = r["gate"]
        style = g.get("style") or "-"
        done = " [已条目化]" if r.get("transcribed_at") else ""
        ndup = f" (+{len(r['dups'])} 转发)" if r.get("dups") else ""
        return (f"- `{style}/{g['confidence']}` [{r['account']}] {r['title']}{ndup}"
                f"{done}\n  {g['reason']} · {p}")

    print(f"# 公众号工具条目分类清单({datetime.now(TZ).date()})\n")
    print(f"已分类 {len(docs)}:通过 {len(passed)},拒绝 {len(rejected)}\n")
    print(f"## 通过({len(passed)})\n")
    for p, r in sorted(passed, key=lambda x: x[1].get("published") or "", reverse=True):
        print(_line(p, r))
    print(f"\n## 拒绝({len(rejected)})\n")
    for p, r in sorted(rejected, key=lambda x: x[1].get("published") or "", reverse=True):
        print(_line(p, r))
    return 0


# ---------------------------------------------------------------- transcribe

def _parse_entries(out: str) -> list[dict]:
    if "===NONE===" in out and "===PROJECT===" not in out:
        return []
    entries = []
    for block in out.split("===PROJECT===")[1:]:
        head, _, body = block.partition("---BODY---")
        fields = {}
        for ln in head.splitlines():
            m = re.match(r"^(name|url|kind|entities|topics|summary):\s*(.*)$", ln.strip())
            if m:
                fields[m.group(1)] = m.group(2).strip()
        name = fields.get("name")
        body = body.strip()
        # 防御:围栏/分隔线残留
        body = re.sub(r"\n```\s*$", "", body).strip()
        body = re.sub(r"\n-{3,}\s*$", "", body).strip()
        if not name or len(body) < 80:
            continue
        fields["body"] = body
        fields["entities"] = [e.strip() for e in (fields.get("entities") or "").split(",") if e.strip()]
        fields["topics"] = [t.strip() for t in (fields.get("topics") or "").split(",") if t.strip()]
        entries.append(fields)
    return entries


def _yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', "'") + '"'


def write_entry(entry: dict, uid: str, info: dict, out_root: Path) -> Path:
    published = info.get("published") or ""
    day = published[:10] if published else "undated"
    day_dir = out_root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{uid}-{_slug(entry['name'])}.md"

    tags = ["source/dr-gzh-projects"]
    if info.get("account"):
        tags.append(f"account/{_slug(info['account'])}")
    seen = set(tags)
    ents = entry["entities"] or [entry["name"]]
    if entry["name"] not in ents:
        ents.insert(0, entry["name"])
    for e in ents:
        t = f"entity/{_slug(e)}"
        if t not in seen:
            tags.append(t); seen.add(t)
    for t in entry["topics"]:
        tt = f"topic/{_slug(t)}"
        if tt not in seen:
            tags.append(tt); seen.add(tt)

    title = entry["name"]
    if entry.get("summary"):
        title = f"{entry['name']}:{entry['summary']}"[:80]
    fm = ["---",
          f"title: {_yaml_str(title)}",
          "source: dr-gzh-projects",
          f"account: {_yaml_str(info.get('account') or '')}",
          f"published: {_yaml_str(published)}",
          f"url: {_yaml_str(info.get('url') or '')}",
          f"article_title: {_yaml_str(info.get('title') or '')}",
          f"project: {_yaml_str(entry['name'])}",
          f"project_url: {_yaml_str(entry.get('url') or '')}",
          f"kind: {entry.get('kind') or 'other'}",
          f"summary: {_yaml_str(entry.get('summary') or '')}",
          "tags:"] + [f"  - {t}" for t in tags] + ["---", ""]
    link = info.get("url") or ""
    src_line = (f"> 整理自公众号文章 [{info.get('title', '')}]({link})"
                f" · {info.get('account', '')} · {published[:10]}\n")
    path.write_text("\n".join(fm) + src_line + "\n" + entry["body"] + "\n", encoding="utf-8")
    return path


def transcribe_one(info: dict, model: str = TRANSCRIBE_MODEL) -> list[dict]:
    prompt = TRANSCRIBE_PROMPT.format(
        topic_candidates=TOPIC_CANDIDATES,
        title=info["title"], account=info["account"],
        published=(info.get("published") or "")[:10],
        url=info.get("url") or "(无)",
        body=info["body"][:TRANSCRIBE_BODY_CAP],
    )
    out = _claude(prompt, model, timeout=900)
    return _parse_entries(out)


def cmd_transcribe(args) -> int:
    state = load_state()
    cands = scan_candidates()
    _pop_stats(cands)
    out_root = Path(args.out).expanduser() if args.out else OUT_ROOT
    todo = []
    for p, rec in state["docs"].items():
        if args.doc and p != args.doc:
            continue
        if args.since and (rec.get("published") or "") < args.since:
            continue
        gate = rec.get("gate")
        if not (gate and gate["project_article"]):
            continue
        if rec.get("transcribed_at") and not args.force:
            continue
        if p not in cands:
            continue
        todo.append((p, rec, cands[p]))
    todo.sort(key=lambda x: x[2].get("published") or "", reverse=True)
    if args.limit:
        todo = todo[: args.limit]
    model = getattr(args, "model", None) or TRANSCRIBE_MODEL
    print(f"[transcribe] 待条目化 {len(todo)},{TRANSCRIBE_WORKERS} 并发,模型 {model} → {out_root}", flush=True)
    ok = failed = empty = 0
    with ThreadPoolExecutor(max_workers=TRANSCRIBE_WORKERS) as pool:
        futs = {pool.submit(transcribe_one, info, model): (p, rec, info)
                for p, rec, info in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            p, rec, info = futs[fut]
            try:
                entries = fut.result()
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(todo)}] FAIL {info['title'][:40]}: {str(e)[:120]}", flush=True)
                continue
            try:
                if not entries:
                    empty += 1
                    rec["transcribed_at"] = datetime.now(TZ).isoformat(timespec="seconds")
                    rec["n_projects"] = 0
                    rec["transcribe_note"] = "模型判定无可成条对象"
                    save_state(state)
                    print(f"[{i}/{len(todo)}] EMPTY {info['title'][:40]}", flush=True)
                    continue
                files = [str(write_entry(e, info["hash"][:10], info, out_root))
                         for e in entries]
                rec["transcribed_at"] = datetime.now(TZ).isoformat(timespec="seconds")
                rec["n_projects"] = len(entries)
                rec["files"] = files
                rec["model"] = model
                save_state(state)
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(todo)}] WRITE-FAIL {info['title'][:40]}: {str(e)[:120]}", flush=True)
                continue
            ok += 1
            print(f"[{i}/{len(todo)}] ok {info['title'][:36]} → {len(entries)} 条", flush=True)
    print(f"[transcribe] 完成: 成功 {ok},空 {empty},失败 {failed}", flush=True)
    return 0


def cmd_scan(args) -> int:
    state = load_state()
    cands = scan_candidates()
    stats = _pop_stats(cands)
    gated = sum(1 for p in cands if "gate" in state["docs"].get(p, {}))
    passed = sum(1 for r in state["docs"].values()
                 if "gate" in r and r["gate"]["project_article"])
    done = sum(1 for r in state["docs"].values() if r.get("transcribed_at"))
    n_entries = sum(r.get("n_projects", 0) for r in state["docs"].values())
    print(f"gzh 文档: {stats['total']}(正文过短跳过 {stats['short']})")
    print(f"正文去重后候选: {stats['unique']}")
    print(f"已分类: {gated},其中通过: {passed}")
    print(f"已条目化: {done},产出条目: {n_entries}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    g = sub.add_parser("gate")
    g.add_argument("--limit", type=int, default=0)
    sub.add_parser("report")
    t = sub.add_parser("transcribe")
    t.add_argument("--limit", type=int, default=0)
    t.add_argument("--doc", help="只处理这个文档 path(documents.path)")
    t.add_argument("--since", help="只处理 published >= 此日期(YYYY-MM-DD)的文章(管道增量用,避开存量积压)")
    t.add_argument("--out", help="输出根目录(默认正式树,试跑可指到 staging)")
    t.add_argument("--force", action="store_true", help="已条目化的也重跑")
    t.add_argument("--model", default=TRANSCRIBE_MODEL,
                   help=f"转写模型,传给 claude CLI 的 --model(默认 {TRANSCRIBE_MODEL})。"
                        "别名(sonnet)会解析到当代最新版,想钉死某一代就写全名")
    args = ap.parse_args()
    if args.cmd in ("gate", "transcribe"):
        _acquire_lock()
    return {"scan": cmd_scan, "gate": cmd_gate,
            "report": cmd_report, "transcribe": cmd_transcribe}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
