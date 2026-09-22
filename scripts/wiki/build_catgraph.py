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


MIN_KEEP = 0.5     # 新图的节点数/已归类数不得低于旧图的这个比例(系统性误判的兜底)


def quality_problem(meta: dict, picked: list, out: Path, *,
                    allow_shrink: bool) -> str:
    """新图能不能盖掉旧图?能就返回空串,不能就返回一句人话的原因。

    验收看的是**这张图实际长什么样**,不是某个 code 的名字。归类塌掉的样子有
    好几种:全落「待归类」、skill 全判成「不是 skill」(判完就被 drop 掉,节点
    数直接归零)、或者只剩零星几个活下来。第 5 轮审查那次就是 drop 把 101 个
    候选全踢光,`meta.total == 0` 从「有条目但没归类」这道检查底下溜过去,空图
    连同坏缓存一起提交、之后再不重试(2026-09-22 第 5 轮审查发现)。所以按
    **可见输出**验收,顺带用旧图兜一道比例下限——只挡塌方,正常增减(以及确实
    该缩的那次)放行,后者加 --allow-shrink。

    比例要**两个维度都比**:`total` 管「节点被 drop 掉了多少」,`classified`
    管「多少节点退化成了待归类」。fallback 节点不会被 drop,所以 100 条判定
    退化成「待归类」时 `total` 和旧图一模一样、节点数那道阈值压根不动——好页面
    照样被换成满屏待归类(2026-09-22 第 7 轮审查发现)。
    """
    if not picked:
        return ""                       # 本来就没候选,空图是如实反映
    if not meta["total"]:
        return f"新图 0 个节点({len(picked)} 个候选全被归类踢出去了)"
    if not meta["classified"]:
        return f"归类结果为空({meta['total']} 个条目一个都没归类)"
    old = P.page_stats(out)
    if not old or allow_shrink:
        return ""
    for what, now, before in (("节点", meta["total"], old["nodes"]),
                              ("已归类的节点", meta["classified"], old["classified"])):
        if before and now < before * MIN_KEEP:
            return (f"新图只剩 {now} 个{what},不到旧图 {before} 个的 "
                    f"{MIN_KEEP:.0%}")
    return ""


def build_one(kind: str, groups: list[dict], *, use_llm: bool, max_new: int | None,
              model, workers: int, out_dir: Path, allow_shrink: bool = False) -> Path:
    v = VIEWS[kind]
    taxo = v["taxo"]
    picked = G.rank(v["select"](groups))
    print(f"[{kind}] 候选 {len(picked)} 组")

    items = [(g["norm"], g["name"], g["summary"]) for g in picked]
    if use_llm:
        res = T.classify(taxo, items, model=model, workers=workers,
                         max_new=max_new, void_codes=v["drop"])
        codes = res.codes
    else:
        codes = T.load_cache(taxo)
        res = T.Verdicts(codes)
        print(f"  [{kind}] --no-llm:只用缓存里的 {len(codes)} 条归类")

    data = G.build(picked, codes, taxo, drop_codes=v["drop"])
    meta = data["meta"]
    out = out_dir / v["out"]
    # 判定本身就不能用(重判判了一半、或判出来的东西不作数):原因由 classify
    # 给,该留的半成品它留着、该扔的它扔了,这里只负责不拿它盖掉好页面。
    if not res.complete and out.exists():
        raise SystemExit(f"[{kind}] {res.reason};拒绝覆盖已有的 {out.name}")
    # 判定是完整的,但出来的图塌了。首次生成(还没有旧图)不拦,那时如实反映。
    bad = quality_problem(meta, picked, out, allow_shrink=allow_shrink)
    if bad and out.exists():
        # 这份判定不作数,半成品一起扔掉,免得下一轮全命中缓存、不再问模型
        res.discard()
        raise SystemExit(
            f"[{kind}] {bad},拒绝覆盖已有的 {out.name};"
            f"先确认模型配置/缓存文件是否正常,确认这就是预期结果就加 "
            f"--allow-shrink 重跑")
    out.write_text(P.render(
        data, title=v["title"], h1=v["h1"], sub=v["sub"], accent=v["accent"],
        foot=P.foot_html(meta, v["unit"]), repo_toggle=v["repo_toggle"],
        other_group=taxo.get(taxo.fallback).group,
    ), encoding="utf-8")
    # 新图验收通过、也落盘了,这才把重判的新结论换上正式缓存。**顺序不能反**:
    # 先换缓存再校验的话,上面任何一道拦截都只保住页面保不住缓存——那份还能用的
    # 旧缓存已经没了,之后每轮都命中新缓存、不再问模型,页面就再也更新不了
    # (2026-09-22 第 4 轮审查发现)。写页面失败同理:没提交,下次重来。
    res.commit()
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
    ap.add_argument("--allow-shrink", action="store_true",
                    help="新图的节点数或已归类数比旧图少一半以上时照样写;确认"
                         "这轮缩水是预期的(比如故意收紧了分类规则)再用")
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
                  workers=a.workers, out_dir=out_dir,
                  allow_shrink=a.allow_shrink)


if __name__ == "__main__":
    main()
