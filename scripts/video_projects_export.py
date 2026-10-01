#!/usr/bin/env python3
"""daily-report 视频 → 知识库项目条目导出。

从 ~/code/daily-report/videos 的视频目录读原字幕(subtitles/source.srt),
两阶段处理:
  1. gate:      判断视频是否「项目/产品介绍型」(单项目深度介绍或多项目速览)
  2. transcribe: 按项目切分,把字幕文章化转写成中文条目,一个项目一个 md
  两步各用哪个模型看 kb-web 设置面板的「模型分配」(kb.llm_stages 的 gate / transcribe)

产物落 ~/obs/new-start/source/dr-video-projects/YYYY-MM-DD/<videoid>-<slug>.md,
frontmatter 自带 entity/topic 标签(pre_tagged 路线,kb index 后无需再 extract)。

状态存 data/video_projects_state.json,按 video id 增量,可随时中断重跑。
用法:
  uv run python scripts/video_projects_export.py scan                 # 看候选统计
  uv run python scripts/video_projects_export.py gate [--limit N]     # 跑分类
  uv run python scripts/video_projects_export.py report               # 分类清单
  uv run python scripts/video_projects_export.py transcribe [--limit N] [--video ID] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.config import DATA_DIR, VAULT_ROOT, VIDEOS_ROOT

OUT_ROOT = VAULT_ROOT / "source" / "dr-video-projects"
STATE_PATH = DATA_DIR / "video_projects_state.json"
TZ = timezone(timedelta(hours=8))

# 两步都走 kb.llm 的 API 通道;用哪家的哪个模型由设置面板的
# 「管道 · 条目化判定」/「管道 · 条目化拆条目」两个阶段决定,理由同 gzh_projects_export.py
GATE_WORKERS = 6
TRANSCRIBE_WORKERS = 4

TOPIC_CANDIDATES = "AI编程, AI Agent, skill工具, 开源项目, 大模型, 提示词工程, 知识管理, AI资讯, 效率工具, 机器人, AI视频图像, 其他"

GATE_PROMPT = """\
你是视频内容分类员。判断下面这个 YouTube 视频是否是「项目/产品介绍型」视频,只输出一个 JSON 对象。

算「项目介绍型」的:视频的主要内容是介绍具体的软件项目/工具/模型/研究成果——演示它是什么、能干什么、怎么用、表现如何。
  - 一个视频集中介绍/评测一个项目或模型 → style=single
  - 新闻速览型,一个视频逐段介绍多个项目/产品/模型(每个几分钟也算) → style=multi
  - 「N 个开源项目/工具推荐」合集 → style=multi
不算的:纯教程或工作流方法论(教你怎么组合使用已熟知的工具)、访谈播客、会议演讲、
公司八卦/人事新闻、个人成长经验分享、没有具体项目介绍的行业评论/观点分析。
边界:判断标准是「有没有逐个介绍具体项目/产品的能力与用法」,不是「是不是新闻」——
AI 新闻速览只要逐段介绍了具体项目/模型就算 multi;新模型发布速递(讲能力/价格/用法)算
项目介绍;只拿工具当例子讲方法论/趋势的不算。

输出格式(单个 JSON,无其他文字):
{{"project_video": true|false, "style": "single"|"multi"|null, "confidence": "high"|"medium"|"low", "reason": "一句话理由,用中文"}}

视频信息:
标题: {title}
频道: {channel}
时长: {duration} 秒
描述(截断):
{description}

章节列表:
{chapters}

已有内容摘要(截断,可能为空):
{summary}
"""

TRANSCRIBE_PROMPT = """\
你是技术知识库的编辑。下面是一个 YouTube 视频的完整英文字幕(自动转录,专有名词可能拼错,\
可参考链接表和章节表纠正),视频里介绍了一个或多个 AI 项目/产品/模型。\
你的任务:把其中每一个被实质介绍的项目,转写成一篇独立的中文知识库文章。

对每个项目输出一个条目,格式严格如下(每个条目以 ===PROJECT=== 单独一行开始):

===PROJECT===
name: 项目官方名称(保留英文原名)
url: 项目主页/仓库链接(从链接表里挑最匹配的一条;没有就留空)
kind: open-source|product|model|research|other
entities: 逗号分隔,该条目实际着墨的实体(项目名、公司名、模型名,官方拼写,最多 6 个)
topics: 逗号分隔,从固定候选选最多 3 个:{topic_candidates}。\
项目本体是 Agent Skill/SKILL.md 指令集/技能包的必须含 skill工具;\
平台或产品只是内置技能功能、普通工具只是也能当 skill 安装的,不算 skill工具
summary: 一句话(<=60 字)说清「是什么+能干什么」,不要英文双引号
---BODY---
(文章化中文正文,markdown)

正文写作要求:
- 这是「转写」不是「摘要」:字幕里关于这个项目的实质信息全部保留——功能、参数、数字、\
benchmark、价格、硬件要求、演示中看到的效果、作者的评价和吐槽,一个都不丢
- 但表达要文章化:去掉口播腔和过场,不是逐句翻译,是重新组织成适合阅读的文章
- 结构:开头 1-2 句导语;然后用 ### 小节或要点列表组织;关键名词和数字用 **加粗**;\
短段落短句,便于扫读
- 作者的主观判断保留,但标明是视频作者的观点(如「作者认为…」)
- 严禁添加字幕里没有的信息,严禁脑补细节
- 赞助广告段(sponsor)、频道自推、订阅呼吁直接跳过,不算项目
- 只被点名、没有实质介绍的东西不单独成条(可在相关条目里带一句)

如果字幕内容其实不是项目介绍(整个视频没有一个可成条的项目),只输出一行:===NONE===

视频信息:
标题: {title}
频道: {channel}
发布日期: {published}

链接表:
{links}

章节表:
{chapters}

完整字幕:
{transcript}
"""


# ---------------------------------------------------------------- 基础设施

def _stage_model(stage: str):
    """该阶段当前该用哪家的哪个模型(设置面板里配的,kb.llm_stages)。

    每次调用现取:面板改完不用重启管道,下一个视频就换过来了。"""
    from kb.llm_stages import stage_model

    return stage_model(stage)


def _llm(prompt: str, model, timeout: int, max_tokens: int = 8192) -> str:
    """走 kb.llm 的 API 通道调模型。

    key/base_url 由 kb.llm 统一解析(环境变量优先,其次 data/secrets.json);
    调用失败统一抛 RuntimeError,由各 worker 计入失败、留给下轮重跑。"""
    from kb.llm import chat

    return chat(prompt, model, max_tokens=max_tokens, timeout=timeout)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"videos": {}}


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

def srt_to_text(path: Path) -> str:
    """srt → 纯文本:去序号/时间戳,连续行合并。"""
    lines = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8", errors="replace")):
        for ln in block.splitlines():
            ln = ln.strip()
            if not ln or ln.isdigit() or "-->" in ln:
                continue
            lines.append(ln)
    return "\n".join(lines)


def scan_candidates() -> dict[str, dict]:
    """扫 videos 目录,按 video id 去重(取 source.srt 最大的目录)。

    返回 {video_id: {dir, title, channel, duration, published, url, ...}}"""
    cands: dict[str, dict] = {}
    for d in sorted(VIDEOS_ROOT.iterdir()):
        if not d.is_dir():
            continue
        meta_p = d / "metadata" / "youtube_metadata.json"
        srt_p = d / "subtitles" / "source.srt"
        if not (meta_p.is_file() and srt_p.is_file()):
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        vid = meta.get("id")
        if not vid:
            continue
        size = srt_p.stat().st_size
        if vid in cands and cands[vid]["srt_size"] >= size:
            continue
        ts = meta.get("timestamp")
        if ts:
            published = datetime.fromtimestamp(ts, TZ).isoformat()
        else:
            ud = meta.get("upload_date") or ""
            published = f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}" if len(ud) == 8 else ""
        cands[vid] = {
            "dir": d.name,
            "title": meta.get("title") or "",
            "channel": meta.get("channel") or meta.get("uploader") or "",
            "duration": meta.get("duration") or 0,
            "published": published,
            "url": meta.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
            "description": meta.get("description") or "",
            "srt_size": size,
        }
    return cands


def _read_chapters(vdir: Path) -> list[dict]:
    p = vdir / "metadata" / "chapters.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("chapters") or []
    except (json.JSONDecodeError, OSError):
        return []


def _read_links(vdir: Path, description: str) -> list[dict]:
    p = vdir / "metadata" / "source_links.json"
    links = []
    if p.is_file():
        try:
            links = json.loads(p.read_text(encoding="utf-8")).get("links") or []
        except (json.JSONDecodeError, OSError):
            pass
    # source_links 可能不全,描述里的「文字 链接」行一并合入(按 url 去重)
    seen = {l.get("url") for l in links}
    for ln in description.splitlines():
        m = re.match(r"^(.{2,60}?)\s+(https?://\S+)\s*$", ln.strip())
        if m and m.group(2) not in seen:
            links.append({"label": m.group(1).strip(" :："), "url": m.group(2)})
            seen.add(m.group(2))
    return links


def _read_summary(vdir: Path) -> str:
    p = vdir / "metadata" / "video_content_summary.md"
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


# ---------------------------------------------------------------- gate

def gate_one(info: dict) -> dict:
    vdir = VIDEOS_ROOT / info["dir"]
    chapters = _read_chapters(vdir)
    ch_lines = "\n".join(f"- {c.get('title', '')}: {c.get('summary', '')}" for c in chapters) or "(无)"
    prompt = GATE_PROMPT.format(
        title=info["title"], channel=info["channel"], duration=info["duration"],
        description=info["description"][:1800] or "(无)",
        chapters=ch_lines[:1500],
        summary=_read_summary(vdir)[:1200] or "(无)",
    )
    out = _llm(prompt, _stage_model("gate"), timeout=120, max_tokens=2048)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError(f"gate 输出无 JSON: {out[:200]}")
    data = json.loads(m.group(0))
    return {
        "project_video": bool(data.get("project_video")),
        "style": data.get("style"),
        "confidence": data.get("confidence") or "low",
        "reason": str(data.get("reason") or "")[:200],
        "checked_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }


def cmd_gate(args) -> int:
    state = load_state()
    cands = scan_candidates()
    todo = [(vid, info) for vid, info in cands.items()
            if "gate" not in state["videos"].get(vid, {})]
    if args.limit:
        todo = todo[: args.limit]
    print(f"[gate] 候选 {len(cands)},待分类 {len(todo)},{GATE_WORKERS} 并发", flush=True)
    ok = failed = 0
    with ThreadPoolExecutor(max_workers=GATE_WORKERS) as pool:
        futs = {pool.submit(gate_one, info): (vid, info) for vid, info in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            vid, info = futs[fut]
            try:
                gate = fut.result()
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(todo)}] FAIL {info['title'][:40]}: {str(e)[:100]}", flush=True)
                continue
            rec = state["videos"].setdefault(vid, {})
            rec.update({"dir": info["dir"], "title": info["title"],
                        "channel": info["channel"], "published": info["published"],
                        "url": info["url"], "gate": gate})
            save_state(state)
            ok += 1
            if i % 10 == 0 or i == len(todo):
                print(f"[{i}/{len(todo)}] ok={ok} fail={failed}", flush=True)
    print(f"[gate] 完成: 成功 {ok},失败 {failed}(失败的下次重跑)", flush=True)
    return 0


def cmd_report(args) -> int:
    state = load_state()
    vids = [(v, r) for v, r in state["videos"].items() if "gate" in r]
    passed = [x for x in vids if x[1]["gate"]["project_video"]]
    rejected = [x for x in vids if not x[1]["gate"]["project_video"]]

    def _line(vid, r):
        g = r["gate"]
        style = g.get("style") or "-"
        done = " [已转写]" if r.get("transcribed_at") else ""
        return (f"- `{style}/{g['confidence']}` [{r['channel']}] {r['title']}"
                f"{done}\n  {g['reason']} · {r['url']}")

    print(f"# 视频项目分类清单({datetime.now(TZ).date()})\n")
    print(f"已分类 {len(vids)}:通过 {len(passed)},拒绝 {len(rejected)}\n")
    print(f"## 通过({len(passed)})\n")
    for vid, r in sorted(passed, key=lambda x: x[1].get("published") or "", reverse=True):
        print(_line(vid, r))
    print(f"\n## 拒绝({len(rejected)})\n")
    for vid, r in sorted(rejected, key=lambda x: x[1].get("published") or "", reverse=True):
        print(_line(vid, r))
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


def write_entry(entry: dict, vid: str, info: dict, out_root: Path) -> Path:
    published = info.get("published") or ""
    day = published[:10] if published else "undated"
    day_dir = out_root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{_slug(vid)}-{_slug(entry['name'])}.md"

    tags = ["source/dr-video-projects", "daily-report/video-projects"]
    if info.get("channel"):
        tags.append(f"channel/{_slug(info['channel'])}")
    seen = set()
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
          "source: dr-video-projects",
          f"channel: {_yaml_str(info.get('channel') or '')}",
          f"published: {_yaml_str(published)}",
          f"youtube: {_yaml_str(info['url'])}",
          f"video_title: {_yaml_str(info.get('title') or '')}",
          f"project: {_yaml_str(entry['name'])}",
          f"project_url: {_yaml_str(entry.get('url') or '')}",
          f"kind: {entry.get('kind') or 'other'}",
          f"summary: {_yaml_str(entry.get('summary') or '')}",
          "tags:"] + [f"  - {t}" for t in tags] + ["---", ""]
    src_line = (f"> 转写自视频 [{info.get('title', '')}]({info['url']})"
                f" · {info.get('channel', '')} · {published[:10]}\n")
    path.write_text("\n".join(fm) + src_line + "\n" + entry["body"] + "\n", encoding="utf-8")
    return path


def transcribe_one(vid: str, rec: dict, info: dict, model=None) -> tuple[list[dict], str]:
    model = model or _stage_model("transcribe")
    vdir = VIDEOS_ROOT / info["dir"]
    transcript = srt_to_text(vdir / "subtitles" / "source.srt")
    chapters = _read_chapters(vdir)
    ch_lines = "\n".join(f"- [{int(c.get('start', 0)) // 60}min] {c.get('title', '')}: {c.get('summary', '')}"
                         for c in chapters) or "(无)"
    links = _read_links(vdir, info["description"])
    link_lines = "\n".join(f"- {l.get('label', '')}: {l.get('url', '')}" for l in links) or "(无)"
    prompt = TRANSCRIBE_PROMPT.format(
        topic_candidates=TOPIC_CANDIDATES,
        title=info["title"], channel=info["channel"],
        published=(info.get("published") or "")[:10],
        links=link_lines[:4000], chapters=ch_lines[:3000],
        transcript=transcript[:180000],
    )
    out = _llm(prompt, model, timeout=900, max_tokens=32000)
    return _parse_entries(out), out


def cmd_transcribe(args) -> int:
    state = load_state()
    cands = scan_candidates()
    out_root = Path(args.out).expanduser() if args.out else OUT_ROOT
    todo = []
    for vid, rec in state["videos"].items():
        if args.video and vid != args.video:
            continue
        gate = rec.get("gate")
        if not (gate and gate["project_video"]):
            continue
        if rec.get("transcribed_at") and not args.force:
            continue
        if vid not in cands:
            continue
        todo.append((vid, rec, cands[vid]))
    if args.limit:
        todo = todo[: args.limit]
    # 模型在这里定一次,整批用同一个:面板中途改了也不会出现半批新半批旧
    model = _stage_model("transcribe")
    print(f"[transcribe] 待转写 {len(todo)},{TRANSCRIBE_WORKERS} 并发,模型 {model} → {out_root}", flush=True)
    ok = failed = empty = 0
    with ThreadPoolExecutor(max_workers=TRANSCRIBE_WORKERS) as pool:
        futs = {pool.submit(transcribe_one, vid, rec, info, model): (vid, rec, info)
                for vid, rec, info in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            vid, rec, info = futs[fut]
            try:
                entries, raw = fut.result()
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(todo)}] FAIL {info['title'][:40]}: {str(e)[:120]}", flush=True)
                continue
            if not entries:
                empty += 1
                rec["transcribed_at"] = datetime.now(TZ).isoformat(timespec="seconds")
                rec["n_projects"] = 0
                rec["transcribe_note"] = "模型判定无可成条项目"
                save_state(state)
                print(f"[{i}/{len(todo)}] EMPTY {info['title'][:40]}", flush=True)
                continue
            try:
                files = [str(write_entry(e, vid, info, out_root)) for e in entries]
                rec["transcribed_at"] = datetime.now(TZ).isoformat(timespec="seconds")
                rec["n_projects"] = len(entries)
                rec["files"] = files
                rec["model"] = str(model)
                save_state(state)
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(todo)}] WRITE-FAIL {info['title'][:40]}: {str(e)[:120]}", flush=True)
                continue
            ok += 1
            print(f"[{i}/{len(todo)}] ok {info['title'][:40]} → {len(entries)} 项目", flush=True)
    print(f"[transcribe] 完成: 成功 {ok},空 {empty},失败 {failed}", flush=True)
    return 0


def cmd_scan(args) -> int:
    state = load_state()
    cands = scan_candidates()
    gated = sum(1 for v in cands if "gate" in state["videos"].get(v, {}))
    passed = sum(1 for v, r in state["videos"].items()
                 if "gate" in r and r["gate"]["project_video"])
    done = sum(1 for r in state["videos"].values() if r.get("transcribed_at"))
    print(f"候选视频(去重,有字幕+元数据): {len(cands)}")
    print(f"已分类: {gated},其中通过: {passed}")
    print(f"已转写: {done}")
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
    t.add_argument("--video", help="只转写这个 video id")
    t.add_argument("--out", help="输出根目录(默认正式树,试跑可指到 staging)")
    t.add_argument("--force", action="store_true", help="已转写的也重跑")
    args = ap.parse_args()
    if args.cmd in ("gate", "transcribe"):
        _acquire_lock()
    return {"scan": cmd_scan, "gate": cmd_gate,
            "report": cmd_report, "transcribe": cmd_transcribe}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
