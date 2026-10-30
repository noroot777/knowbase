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
import os
import sys
from hashlib import blake2b
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
        "sub": "",
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
        "sub": "「本体是软件、只是带 skill 能力」的(Claude Code、豆包这类)不计入。",
        "accent": "#7c5cff",
        "unit": "个 skill",
        "repo_toggle": False,
        # X = 归类时判出来的「不是 skill」,不进图谱
        "drop": frozenset({"X"}),
    },
}


MIN_KEEP = 0.5     # 新图的节点数/已归类数不得低于旧图的这个比例(系统性误判的兜底)


def publish_sig(kind: str, out_dir: Path) -> str:
    """一次发布的基线:**发布序号 + 正式缓存内容 + 正式页面字节**。

    序号(`taxonomy.bump_gen`,动正式产物之前就推进)回答「发布序列有没有往前
    走」,内容签名回答「东西一不一样」——两个问题都要问:

    - 只比内容会被 **ABA** 骗过去:任务跑的这段时间里正式产物经历 A→B→A(回退
      再恢复,或者同样输入的一次重发),结束时缓存和页面字节与开工时逐字相同,
      旧任务就认定「没人发布过」,把自己那份陈旧快照发出去,期间明明有两次发布
      成功完成(2026-09-23 复审第 8 轮发现)。
    - 只比序号则挡不住**绕过我们发布流程的改动**(页面被手工删掉/覆盖、缓存被
      别的脚本写过):那时候序号没动,内容却已经不是我们读到的那一版了。

    一轮构建的产物有两个文件,内容基线也得是两个。只比 `norm → code` 的话页面
    这一层完全没保护,而页面还取决于锁外读到的那份候选快照(数量、名字、简介、
    链接、提及次数、共现边),这些变了缓存可以一个字节都不动:`--no-llm` 撞上
    新候选只把它显示成「待归类」,默认构建全部命中缓存时简介/热度变了也不写缓存,
    长期缓存里本来就躺着当前快照没有的 norm(2026-09-23 复审第 7 轮发现)。
    缓存那半按内容算(别人只更新了昵称之类的话判定没变,不该让整轮白跑),页面
    那半按字节算——同样的输入渲染出同样的字节,页面没有「只是重排、内容等价」
    这回事。

    **要在读候选之前取**(`main()` 里是 `load_groups()` 之前):候选快照一读完,
    我们手里的输入就定死了,这之后别人发布的任何一版都比我们新。

    **读不出来一律 fail-closed**,不许退化成「还没有」:序号读失败折叠成空串、
    页面读失败折叠成「没有页面」,都等于自己把基线的一半丢掉,ABA 就又能骗过
    CAS 了(2026-09-23 复审第 10 轮发现)。取基线是在动任何东西之前,这时候退出
    最干净。
    """
    v = VIEWS[kind]
    out = out_dir / v["out"]
    try:
        page = blake2b(out.read_bytes(), digest_size=12).hexdigest()
    except FileNotFoundError:
        page = "-"                                   # 还没有页面
    except OSError as e:
        raise SystemExit(f"[{kind}] 读不了正式页面 {out}({e}),这一轮不发布:"
                         f"发布基线少一半就认不出别人发布过,产物原样保留")
    try:
        gen = T.read_gen(v["taxo"])
    except T.GenUnreadable as e:
        raise SystemExit(f"[{kind}] 发布序号不可用({e}),这一轮不发布:序号是"
                         f"并发发布的唯一顺序凭据,读不到就没法确认别人有没有"
                         f"发布过。正式页面与分类缓存原样保留,修好盘上的权限/"
                         f"内容后重跑")
    return f"{gen}|{T.cache_fingerprint(v['taxo'])}:{page}"


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

    **「一个候选都没有」不是「没什么可验收的」**:上游 vault 没挂上、扫描出错、
    某个过滤器回归,都会让这一轮扫出 0 组,原来那句 `if not picked: return ""`
    直接跳过了下面所有比例检查,几千节点的好页面被一张 0 节点的新页原子替换,
    还以退出码 0 收场(2026-09-22 复审第 2 轮发现)。旧图是空的(或首次生成)
    才放行,确实要清空就加 --allow-shrink。
    """
    old = P.page_stats(out)
    if not picked:
        if old and old["nodes"] and not allow_shrink:
            return (f"这一轮一个候选都没扫到,而旧图还有 {old['nodes']} 个节点"
                    f"(多半是 vault 没挂上或上游扫描出了问题)")
        return ""                       # 本来就没候选,空图是如实反映
    if not meta["total"]:
        return f"新图 0 个节点({len(picked)} 个候选全被归类踢出去了)"
    if not meta["classified"]:
        return f"归类结果为空({meta['total']} 个条目一个都没归类)"
    if not old or allow_shrink:
        return ""
    for what, now, before in (("节点", meta["total"], old["nodes"]),
                              ("已归类的节点", meta["classified"], old["classified"])):
        if before and now < before * MIN_KEEP:
            return (f"新图只剩 {now} 个{what},不到旧图 {before} 个的 "
                    f"{MIN_KEEP:.0%}")
    return ""


def _atomic_write(out: Path, data: str | bytes) -> None:
    """页面落盘用「同目录唯一临时文件 + rename」,别直接往正式产物上写。

    直接 `write_text` 的话,磁盘满/进程被杀/写到一半抛异常都会先把正式页面截成
    半截,再报错出去——缓存那头的两阶段提交还完好,kb-web 却已经在拿一份坏 HTML
    对外服务了(2026-09-22 第 1 轮审查发现)。同目录才能保证 rename 是同一个
    文件系统内的原子替换;临时文件名带 pid,两个进程同时生成不会抢同一个 tmp。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        if isinstance(data, bytes):
            tmp.write_bytes(data)
        else:
            tmp.write_text(data, encoding="utf-8")
        tmp.replace(out)                      # 失败前旧页面逐字节不变
    finally:
        tmp.unlink(missing_ok=True)


def _publish(out: Path, html: str, res, kind: str) -> None:
    """**先推进发布序号,再动正式产物**;页面/缓存失败就把页面回滚成原样。

    顺序不能反过来。序号、页面、缓存是三次独立的 rename,凑不成一个事务,所以
    只能选「先推进」还是「后推进」——两种失败方向的代价差着一个量级:

    - **后推进(错的)**:推进那一步失败、或者进程刚好死在页面已经换好之后,就
      留下「正式产物已经变了、序号没变」的状态。A→B→A 走完一圈,签名逐字回到
      开工时那一版,旧任务认不出期间发生过两次发布,静默覆盖最终页面——本来加
      序号就是为了挡这个(2026-09-23 复审第 9 轮发现)。这是**危险的假阴性**,
      而且普通的 kill、宿主机重启都能触发,不依赖任何 I/O 故障。
    - **先推进(对的)**:页面/缓存后面失败时,序号多走了一步。别的任务看到基线
      变了会**多拒一轮**(打一行说明、退出码 0,下一轮自己就好了),正式产物一个
      字节都没动。这是**安全的假阳性**。

    所以序号写失败一律 fail-closed:这一轮连页面都不换。它代表的是「有人在这段
    时间里动过发布流程」,不是「发布一定成功了」——CAS 要的正是前者。

    页面换好了、正式缓存却因为磁盘满/权限/IO 错误提交不上时,任务是报错退出了,
    可 Vault 里的正式页面已经变成新的 120 节点、缓存还停在旧的 100 条——「失败的
    发布不改变正式产物」这条当场不成立,而且没人重试的话这个不一致会一直挂着
    (2026-09-22 复审第 4 轮发现)。所以先把旧页面收在手里,提交失败就原子换回去。
    """
    try:
        T.bump_gen(VIEWS[kind]["taxo"])
    except OSError as e:
        raise SystemExit(
            f"[{kind}] 发布序号没能推进({e}),这一轮不动正式产物:序号是并发发布"
            f"的唯一顺序凭据,缺了它旧任务会认不出期间发生过发布。"
            f"{out.name} 与分类缓存原样保留,修好盘上的权限/空间后重跑")
    backup = out.read_bytes() if out.exists() else None
    _atomic_write(out, html)
    try:
        res.commit()
    except BaseException:
        try:
            if backup is None:
                out.unlink(missing_ok=True)   # 本来就没有页面:回到没有
            else:
                _atomic_write(out, backup)
        except OSError as e:                  # 回滚都失败了,至少说清楚
            print(f"[{kind}] 缓存提交失败后回滚页面也失败({e}),"
                  f"{out.name} 现在是新的一版、缓存还是旧的,请手工重跑",
                  file=sys.stderr)
        raise


def build_one(kind: str, groups: list[dict], *, use_llm: bool, max_new: int | None,
              model, workers: int, out_dir: Path, allow_shrink: bool = False,
              base_sig: str | None = None) -> Path:
    """`base_sig`:这一轮是基于哪一版正式产物跑出来的(见 `publish_sig`)。
    调用方在**读候选之前**取一份传进来;不传就现取一份(直接调这个函数的路径,
    比如测试)。"""
    v = VIEWS[kind]
    taxo = v["taxo"]
    if base_sig is None:
        base_sig = publish_sig(kind, out_dir)
    picked = G.rank(v["select"](groups))
    print(f"[{kind}] 候选 {len(picked)} 组")

    items = [(g["norm"], g["name"], g["summary"]) for g in picked]
    if use_llm:
        res = T.classify(taxo, items, model=model, workers=workers,
                         max_new=max_new, void_codes=v["drop"],
                         allow_degenerate=allow_shrink)
        codes = res.codes
    else:
        codes = T.load_cache(taxo)
        # --no-llm 这条路一样要比基线:它的 commit 是空操作,更容易被忽略,可它
        # 照样会把整张页面重发一遍——把别人刚发的新候选/新简介刷回旧快照。
        res = T.Verdicts(codes)
        print(f"  [{kind}] --no-llm:只用缓存里的 {len(codes)} 条归类")

    data = G.build(picked, codes, taxo, drop_codes=v["drop"])
    meta = data["meta"]
    out = out_dir / v["out"]
    # 判定本身就不能用(重判判了一半、或判出来的东西不作数):原因由 classify
    # 给,该留的半成品它留着、该扔的它扔了,这里只负责不拿它发出去。**跟有没有
    # 旧页面无关**——首次生成、页面被人删了或产物丢了的时候,把一份已经判定
    # 「不作数」的结果写成 0 节点的新页面、还以退出码 0 收场,管道面板照样绿
    # (2026-09-22 第 1 轮审查发现)。没有旧页面可保不等于坏结果可以发。
    if not res.complete:
        raise SystemExit(f"[{kind}] {res.reason};"
                         f"{'拒绝覆盖已有的 ' + out.name if out.exists() else '拒绝发布 ' + out.name}")
    html = P.render(
        data, title=v["title"], h1=v["h1"], sub=v["sub"], accent=v["accent"],
        foot=P.foot_html(meta, v["unit"]), repo_toggle=v["repo_toggle"],
        other_group=taxo.get(taxo.fallback).group,
    )
    # **验收 → 落页 → 提交缓存这一段要独占**(跨进程锁,见 taxonomy.publish_lock)。
    # 页面和缓存各自 rename 只保证单个文件不是半截的,管不住两个 writer 交错:
    # A 写完页面、B 写页面又提交了 B 的缓存、A 再提交 A 的缓存——两个进程都以 0
    # 退出,最终页面来自 B、缓存来自 A,下一轮又拿 A 的缓存把页面刷回去,一路
    # 无告警(2026-09-22 复审第 3 轮发现)。验收读的基线也得在锁里,不然刚比过的
    # 旧图转头就被另一个 writer 换掉了。
    with T.publish_lock(taxo):
        now = publish_sig(kind, out_dir)
        if now != base_sig:
            # **读候选 → 判定 → 建图 → 渲染那一大段跑在锁外**(判定要问模型,几分钟
            # 起,攥着锁不放会把另一条路堵死),所以锁只保证两次发布动作不交错,认不
            # 出后拿到锁的这个是基于陈旧输入跑出来的:旧任务能以退出码 0 把刚发布的
            # 40 节点页面刷回 30 节点、连新增的 10 条缓存一起抹掉(2026-09-23 复审
            # 第 6 轮发现);缓存一个字节没动、只有页面更新了的那几条路同样能被刷回
            # 去(第 7 轮发现),内容一样但发布序列已经走过两步的 ABA 也一样(第 8
            # 轮发现)——所以基线是「发布序号 + 缓存内容 + 页面字节」,见 publish_sig。
            # 别人在我们跑的这段时间里发布过 = 那一版比我们这份新,这一轮就不发了。
            # 重试再发没有意义——我们的候选扫描也是那时候做的,一样旧。
            res.keep()          # 自己判出来、还没进正式缓存的那些并回暂存
            print(f"[{kind}] 正式产物在这一轮生成期间被别的 writer 换过了"
                  f"(基线 {base_sig[:8]}… → {now[:8]}…),那一版更新,"
                  f"这一轮不发布 {out.name};判出来的判定已并回暂存,下一轮接着提交")
            return out
        # 判定是完整的,但出来的图塌了。--no-llm 首次生成不拦(那时满屏待归类是
        # 如实反映);问过模型却塌了,即使没有旧页也不许发。
        bad = quality_problem(meta, picked, out, allow_shrink=allow_shrink)
        if bad and (out.exists() or (use_llm and not allow_shrink)):
            # 这份判定不作数,半成品一起扔掉,免得下一轮全命中缓存、不再问模型
            res.discard()
            raise SystemExit(
                f"[{kind}] {bad},拒绝{'覆盖已有的' if out.exists() else '发布'} "
                f"{out.name};先确认模型配置/缓存文件是否正常,确认这就是预期"
                f"结果就加 --allow-shrink 重跑")
        # 新图验收通过:落页面、换正式缓存。**顺序不能反**:先换缓存再校验的话,
        # 上面任何一道拦截都只保住页面保不住缓存——那份还能用的旧缓存已经没了,
        # 之后每轮都命中新缓存、不再问模型,页面就再也更新不了(2026-09-22 第 4
        # 轮审查发现)。写页面失败同理:没提交,下次重来。缓存提交失败则把页面
        # 回滚回去,别留下「页面新、缓存旧」的半提交状态(见 _publish)。
        _publish(out, html, res, kind)
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
                    help="新图的节点数或已归类数比旧图少一半以上、或这一轮判定"
                         "压倒性地落在「待归类/不是 skill」上时照样发;确认这轮"
                         "缩水是预期的(比如故意收紧了分类规则)再用")
    a = ap.parse_args()

    kinds = list(VIEWS) if a.kind == "all" else [a.kind]
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 基线要在**读候选之前**取:load_groups 一跑完,我们手里的输入就定死了,
    # 这之后别人发布的任何一版都比我们新(--kind all 时 skills 是在 projects
    # 判完几分钟后才开始建的,拿那时候的产物当基线就等于漏掉这段窗口)。
    base = {k: publish_sig(k, out_dir) for k in kinds}
    groups = G.load_groups()
    print(f"条目分组 {len(groups)} 组(口径同图鉴:已合并写法变体、已剔除 hard 软广)")

    model = None
    if not a.no_llm:
        from kb.llm import parse_model
        model = parse_model(a.model) if a.model else T.default_model()
        if model is None:
            raise SystemExit(f"认不出模型 {a.model} 属于哪家,请写成 provider:model")

    for k in kinds:
        build_one(k, groups, use_llm=not a.no_llm,
                  max_new=a.max_new or None, model=model,
                  workers=a.workers, out_dir=out_dir,
                  allow_shrink=a.allow_shrink, base_sig=base[k])


if __name__ == "__main__":
    main()
