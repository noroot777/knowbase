# -*- coding: utf-8 -*-
"""存量条目 skill工具 标签补齐/复核(一次性数据修补)。

背景:转写打标每条最多 3 个 topic,skill工具 常被更宽泛的 AI编程/开源项目 挤掉,
存量里该打未打;早期也有误打(如 MCP server 被标 skill工具)。

做法(规则圈候选 + LLM 只判候选),判定单位是「实体」不是「条目」——
同一项目不同文章的 summary 措辞不一,逐条判会自相矛盾,而图鉴聚合按 entity_norm:
  补标池 = 无任何条目带标签、但任一条目 name/summary 含 skill/技能 的实体 → 判"是"则全部条目补标
  复核池 = 任一条目已带 topic/skill工具 的实体                            → 判"否"则全部条目去标
判定只喂 name+若干条 summary。严格标准按「分发形态」:项目本体就是 Agent Skill/
SKILL.md 指令集/技能包才算;平台内置技能功能、普通软件"也能当 skill 安装"、
帮你生成 skill 的工具、泛指人类技能的都不算。

用法:
  uv run python scripts/skill_tag_backfill.py            # dry-run:判定并打印计划,不写文件
  uv run python scripts/skill_tag_backfill.py --apply    # 写入 frontmatter,变更日志落 data/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kb.config import VAULT_ROOT  # noqa: E402
from kb.norm import entity_norm  # noqa: E402

SOURCES = ("dr-gzh-projects", "dr-video-projects")
TAG = "topic/skill工具"
TAG_LINE = f"  - {TAG}"
KEYWORD = re.compile(r"skill|技能", re.I)
JUDGE_MODEL = "claude-sonnet-4-5"   # 钉死模型代,别名会随新一代发布静默漂移
BATCH = 40
SUMMARIES_PER_ENTITY = 3

JUDGE_PROMPT = """\
你是技术知识库的标签审核员。下面每行是一个项目:id. 名称 :: 简介(同一项目在多篇文章\
里的一句话简介,用 ⁄ 分隔,措辞可能不一致,综合判断)。
逐条判断该项目是否该打「skill工具」标签。

标准按「分发形态」判,不按功能类别:
- true:项目本体就是给 AI Agent/AI 编程工具用的 Skill——用户拿到手的就是 SKILL.md \
指令集、技能包/技能合集、CLAUDE.md 规范文件。简介明说"XX 是一个 Skill/技能包"的算。
- false:它是独立软件/平台/服务,skill 只是沾边——平台内置技能功能或带 Skill 市场;\
Agent 框架带技能沉淀机制;普通工具只是"也能作为 skill 安装";帮你「生成/管理/审计 \
skill」的软件(它产出 skill,但本体是软件);"技能"泛指人的技能。

例子:
- taste-skill(SKILL.md 设计品味指令集)→ true
- superpowers(编码代理的可组合技能包)→ true
- andrej-karpathy-skills(单文件 CLAUDE.md 规范)→ true
- 腾讯云 ADP(平台,带 Skill 市场)→ false
- book-to-skill(把书转换成 skill 文件的工具,本体是转换软件)→ false
- react-doctor(代码扫描工具,只是可作为技能安装)→ false
- ai-engineering-from-scratch(帮人掌握工程化技能的教程)→ false

只输出 JSON 数组,不要解释、不要代码围栏,每个元素 {{"id": <数字>, "skill": true|false}},\
必须覆盖全部 id。

项目:
{items}
"""


@dataclass
class Entry:
    path: Path
    name: str
    summary: str
    tagged: bool


@dataclass
class Entity:
    norm: str
    name: str
    entries: list[Entry]
    tagged: bool        # 任一条目带标签
    keyword: bool       # 任一条目 name/summary 命中关键词
    summaries: list[str]


def _clean_env() -> dict:
    """claude CLI 子进程要干净环境:宿主可能带 ANTHROPIC_BASE_URL 等托管变量。"""
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    for k in ("HOME", "USER", "LOGNAME", "TZ", "LANG", "LC_ALL"):
        v = os.environ.get(k)
        if v:
            env[k] = v
    return env


def _claude(prompt: str, timeout: int = 300) -> str:
    proc = subprocess.run(
        ["claude", "-p", "--model", JUDGE_MODEL, "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=timeout,
        env=_clean_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI 退出码 {proc.returncode}: {proc.stderr[:200]}")
    return proc.stdout.strip()


def _fm_field(fm: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*(.*)$", fm, re.M)
    return (m.group(1).strip().strip('"') if m else "")


def scan() -> list[Entity]:
    entries = []
    for src in SOURCES:
        root = VAULT_ROOT / "source" / src
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            text = p.read_text(encoding="utf-8", errors="ignore")
            m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
            if not m:
                continue
            fm = m.group(1)
            name = _fm_field(fm, "project")
            if not name:
                continue
            entries.append(Entry(
                path=p, name=name,
                summary=_fm_field(fm, "summary"),
                tagged=TAG in fm,
            ))

    groups: dict[str, list[Entry]] = {}
    for e in entries:
        groups.setdefault(entity_norm(e.name), []).append(e)

    entities = []
    for norm, es in groups.items():
        names = Counter(e.name for e in es)
        # 去重 summary,取最长的几条给 LLM(信息量大)
        uniq = sorted({e.summary for e in es if e.summary}, key=len, reverse=True)
        entities.append(Entity(
            norm=norm,
            name=names.most_common(1)[0][0],
            entries=es,
            tagged=any(e.tagged for e in es),
            keyword=any(KEYWORD.search(f"{e.name} {e.summary}") for e in es),
            summaries=uniq[:SUMMARIES_PER_ENTITY],
        ))
    return entities


def judge(ents: list[Entity]) -> dict[int, bool]:
    """返回 {ents 下标: 是否 skill工具}。按批调 LLM,漏判的重试一轮。"""
    verdicts: dict[int, bool] = {}
    pending = list(range(len(ents)))
    for attempt in (1, 2):
        if not pending:
            break
        missing: list[int] = []
        for b in range(0, len(pending), BATCH):
            batch = pending[b:b + BATCH]
            lines = "\n".join(
                f"{i}. {ents[i].name} :: {' ⁄ '.join(ents[i].summaries) or '(无简介)'}"
                for i in batch)
            out = _claude(JUDGE_PROMPT.format(items=lines))
            out = re.sub(r"^```(json)?|```$", "", out.strip(), flags=re.M).strip()
            try:
                arr = json.loads(out)
            except json.JSONDecodeError:
                print(f"  [judge] 批次解析失败(第 {attempt} 轮),稍后重试: {out[:80]}")
                missing.extend(batch)
                continue
            got = {int(x["id"]): bool(x["skill"]) for x in arr
                   if isinstance(x, dict) and "id" in x and "skill" in x}
            for i in batch:
                if i in got:
                    verdicts[i] = got[i]
                else:
                    missing.append(i)
            print(f"  [judge] 已判 {len(verdicts)}/{len(ents)}")
        pending = missing
    if pending:
        names = ", ".join(ents[i].name for i in pending[:5])
        print(f"  [judge] 两轮后仍有 {len(pending)} 个实体未判(跳过不动): {names}")
    return verdicts


def edit_tags(path: Path, add: bool) -> bool:
    """在 frontmatter 的 tags 块加/删 skill工具 行。返回是否实际修改。"""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^(---\n)(.*?)(\n---\n)", text, re.S)
    if not m:
        return False
    fm = m.group(2)
    lines = fm.split("\n")
    if add:
        if TAG_LINE in lines:
            return False
        # 追加在 tags: 块最后一个条目之后
        try:
            ti = lines.index("tags:")
        except ValueError:
            return False
        end = ti + 1
        while end < len(lines) and lines[end].startswith("  - "):
            end += 1
        lines.insert(end, TAG_LINE)
    else:
        if TAG_LINE not in lines:
            return False
        lines.remove(TAG_LINE)
    new = m.group(1) + "\n".join(lines) + m.group(3) + text[m.end():]
    path.write_text(new, encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="skill工具 标签补齐/复核")
    ap.add_argument("--apply", action="store_true", help="实际写入(默认 dry-run)")
    ap.add_argument("--verdicts", help="{norm: bool} 判定文件,加载后跳过 LLM 判定")
    args = ap.parse_args()

    entities = scan()
    review = [e for e in entities if e.tagged]
    backfill = [e for e in entities if not e.tagged and e.keyword]
    pool = review + backfill
    n_entries = sum(len(e.entries) for e in entities)
    print(f"[scan] 条目 {n_entries},实体 {len(entities)},"
          f"复核池 {len(review)} 实体,补标池 {len(backfill)} 实体")

    if args.verdicts:
        vmap = json.loads(Path(args.verdicts).read_text(encoding="utf-8"))
        verdicts = {j: vmap[e.norm] for j, e in enumerate(pool) if e.norm in vmap}
        print(f"[judge] 从 {args.verdicts} 加载 {len(verdicts)}/{len(pool)} 个判定")
    else:
        verdicts = judge(pool)
        vpath = (Path(__file__).resolve().parent.parent / "data"
                 / "skill_tag_verdicts-latest.json")
        vpath.write_text(json.dumps(
            {pool[j].norm: v for j, v in verdicts.items()},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[judge] 判定已存档: {vpath}")

    adds = [e for j, e in enumerate(pool) if verdicts.get(j) and not e.tagged]
    removes = [e for j, e in enumerate(pool)
               if j in verdicts and not verdicts[j] and e.tagged]
    # 已带标签但判 true 的实体:可能只有部分条目带标签,补齐其余条目
    fills = [e for j, e in enumerate(pool)
             if verdicts.get(j) and e.tagged
             and any(not x.tagged for x in e.entries)]

    print(f"\n[plan] 补标 {len(adds)} 个实体(全部条目打标):")
    for e in adds:
        print(f"  + {e.name} ({len(e.entries)} 条) | {(e.summaries or [''])[0][:50]}")
    print(f"\n[plan] 补齐 {len(fills)} 个实体(已判是,部分条目缺标):")
    for e in fills:
        miss = sum(1 for x in e.entries if not x.tagged)
        print(f"  ± {e.name} (缺 {miss}/{len(e.entries)} 条)")
    print(f"\n[plan] 去标 {len(removes)} 个实体(全部条目去标):")
    for e in removes:
        print(f"  - {e.name} ({len(e.entries)} 条) | {(e.summaries or [''])[0][:50]}")

    if not args.apply:
        print("\n(dry-run,未写入。--apply 生效)")
        return 0

    log = []
    for e in adds + fills:
        for x in e.entries:
            if edit_tags(x.path, add=True):
                log.append({"action": "add", "name": e.name,
                            "path": str(x.path.relative_to(VAULT_ROOT))})
    for e in removes:
        for x in e.entries:
            if edit_tags(x.path, add=False):
                log.append({"action": "remove", "name": e.name,
                            "path": str(x.path.relative_to(VAULT_ROOT))})
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = Path(__file__).resolve().parent.parent / "data" / f"skill_tag_backfill-{ts}.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[apply] 修改 {len(log)} 个文件,日志: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
