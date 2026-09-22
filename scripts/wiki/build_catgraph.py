#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成分类图谱:开源项目图谱 + skill 图谱(各一个单文件 HTML,数据内嵌)。

和 build_graph_html.py 的分工:那张图看的是「最近两周公众号在聊什么」(实体
共现,滚动窗口);这两张看的是「我长期关注的两类东西现在有哪些、都归在什么
用途下」——全量、按固定分类表分区、不设时间窗(时间做成筛选器)。

    uv run python scripts/wiki/build_catgraph.py                 # 两张都生成
    uv run python scripts/wiki/build_catgraph.py --kind skills   # 只生成 skill 图谱
    uv run python scripts/wiki/build_catgraph.py --no-llm        # 不判新增,只用已有归类

归类结果长期缓存在 data/taxonomy_*.json,日常重生成是零 LLM 的;新条目进来才
判新增的那几个(--max-new 兜底,防止某天一次性涌入几百条时把一轮管道拖长)。
改了分类表/判定规则则整份作废、全量重判:那一趟不受 --max-new 限制,新结论先
攒在 *.migrating.json 里,判完才换上正式缓存——半截结果既不落正式缓存也不
覆盖旧页面。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))          # repo 根,import kb 用

import catgraph_page as P  # noqa: E402
import graph_data as G  # noqa: E402
import taxonomy as T  # noqa: E402

from kb.config import VAULT_ROOT  # noqa: E402

VIEWS = {
    "projects": {
        "taxo": T.PROJECTS,
        "select": G.select_projects,
        "out": "graph-projects.html",
        "title": "开源项目图谱 · 按用途归类",
        "h1": "开源项目图谱<em> · 按用途归类</em>",
        "sub": "库里攒下的每个开源项目,按它到底拿来干什么归类。"
               "点任意一个:看它是什么、常和谁一起被提到、出自哪些文章。",
        "accent": "#0e7edb",
        "unit": "个项目",
        "repo_toggle": True,
        "drop": frozenset(),
    },
    "skills": {
        "taxo": T.SKILLS,
        "select": G.select_skills,
        "out": "graph-skills.html",
        "title": "Skill 图谱 · 按用途归类",
        "h1": "Skill 图谱<em> · 按用途归类</em>",
        "sub": "给 AI Agent 装的技能包,按它帮你干什么归类。"
               "「本体是软件、只是带 skill 能力」的(Claude Code、豆包这类)已被剔除。",
        "accent": "#7c5cff",
        "unit": "个 skill",
        "repo_toggle": False,
        # X = 归类时判出来的「不是 skill」,不进图谱
        "drop": frozenset({"X"}),
    },
}


def build_one(kind: str, groups: list[dict], *, use_llm: bool, max_new: int | None,
              model, workers: int, out_dir: Path) -> Path:
    v = VIEWS[kind]
    taxo = v["taxo"]
    picked = G.rank(v["select"](groups))
    print(f"[{kind}] 候选 {len(picked)} 组")

    items = [(g["norm"], g["name"], g["summary"]) for g in picked]
    if use_llm:
        res = T.classify(taxo, items, model=model, workers=workers, max_new=max_new)
        codes = res.codes
    else:
        codes = T.load_cache(taxo)
        res = T.Verdicts(codes)
        print(f"  [{kind}] --no-llm:只用缓存里的 {len(codes)} 条归类")

    data = G.build(picked, codes, taxo, drop_codes=v["drop"])
    meta = data["meta"]
    out = out_dir / v["out"]
    # 一条都没归类却已经有一张旧图在那儿,几乎只可能是归类这一步整体失败了
    # (没配 key、中转站挂了、缓存文件坏了)。这时候写下去等于拿一张满屏
    # 「待归类」覆盖掉好页面,而且从日志里看不出来——宁可报错让这步红掉。
    # 首次生成(还没有旧图)不受影响,那时候满屏待归类是如实反映。
    if meta["total"] and not meta["classified"] and out.exists():
        raise SystemExit(
            f"[{kind}] 归类结果为空({meta['total']} 个条目一个都没归类),"
            f"拒绝覆盖已有的 {out.name};先确认模型配置/缓存文件是否正常")
    # 分类表改过、全量重判没判完:手上这份是半成品,大半节点会落「待归类」。
    # 拿它盖掉好页面和「一个都没判出来」是同一种事故,只是没那么显眼。
    if not res.complete and out.exists():
        raise SystemExit(
            f"[{kind}] 分类规则改过,全量重判还没做完(已判 {len(codes)} 条,"
            f"还差 {res.pending} 条),拒绝用半份结果覆盖已有的 {out.name};"
            f"进度已存盘,下次跑接着判")
    out.write_text(P.render(
        data, title=v["title"], h1=v["h1"], sub=v["sub"], accent=v["accent"],
        foot=P.foot_html(meta, v["unit"]), repo_toggle=v["repo_toggle"],
        other_group=taxo.get(taxo.fallback).group,
    ), encoding="utf-8")
    size = out.stat().st_size / 1024
    print(f"[{kind}] 节点 {meta['total']}(已归类 {meta['classified']}) "
          f"边 {meta['edges']} 文章 {meta['arts']} → {out} ({size:.0f} KB)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["projects", "skills", "all"], default="all")
    ap.add_argument("--out-dir", default=str(VAULT_ROOT))
    ap.add_argument("--no-llm", action="store_true", help="不判新增条目,只用已有归类缓存")
    ap.add_argument("--max-new", type=int, default=400,
                    help="本轮最多新判多少条(按热度排队);0 = 不限。"
                         "只管增量;分类表改过时的全量重判不受它限制")
    ap.add_argument("--model", default=None,
                    help="临时覆盖模型;默认用设置面板的「管道 · 图谱归类」阶段")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    kinds = list(VIEWS) if a.kind == "all" else [a.kind]
    groups = G.load_groups()
    print(f"条目分组 {len(groups)} 组(口径同图鉴:已合并写法变体、已剔除 hard 软广)")

    model = None
    if not a.no_llm:
        from kb.llm import parse_model
        model = parse_model(a.model) if a.model else T.default_model()
        if model is None:
            raise SystemExit(f"认不出模型 {a.model} 属于哪家,请写成 provider:model")

    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for k in kinds:
        build_one(k, groups, use_llm=not a.no_llm,
                  max_new=a.max_new or None, model=model,
                  workers=a.workers, out_dir=out_dir)


if __name__ == "__main__":
    main()
