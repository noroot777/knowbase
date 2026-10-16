# -*- coding: utf-8 -*-
"""给人翻的那份历史**留多久、留多大**。

从 `pipeline_persist` 里分出来的:那边是落盘原语(序列化、原子换文件、谁更新),
这边只有一件事 —— 一轮记录什么时候可以不要了。分开为了行数,也因为这一块的消费方
不止那一处:失败账本(`pipeline_failures`)的回收窗口也得跟这里**同一个口径**,
它不该为了问一句「历史留几天」去 import 落盘原语那一整块。

**窗口是唯一会让一轮记录消失的时间判据。** 按条数封顶的旧口径在「一天里点了二十次
重试」那天会把前几天连同它们的红一起挤掉,而那正是最想回查的时候;更糟的是它和
用户要的「保留三天」**根本不是一个轴** —— 401 轮全落在 66 小时内也会掉一轮,而掉
的那一轮距离到期还有五个多小时(第 1 轮审查实测)。所以条数上限整个撤掉,体量那道
守卫改成按**字节**:它要防的本来就是「这份文件每轮重写一次」的成本,而成本按字节算。

硬约束见 `docs/decisions/pipeline-history.md` 的第一节 —— 这里写错多半**不可恢复**
(那一轮的红没有第二个地方查得到)。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from kb.pipeline_persist import _dump_bytes

#: 窗口:这么多天之内跑过的那几轮全留着。**唯一决定「一轮还在不在」的时间判据。**
_HISTORY_DAYS = 3
#: 下限要小。它存在是为了「永远还有几轮可翻」(一周只同步一次的人,窗口里只有当前
#: 这一轮,历史列表形同虚设),顺带限制时钟跳跃那一刻的损失。给大了会反过来把窗口
#: 架空 —— 三天里本来就常只跑五到二十轮,下限 10 的话实际生效的又成了「按条数留」。
_HISTORY_FLOOR = 3
#: 体量预算(字节)。实测一轮 1~28 KB(明细占大头,日志其次),正常三天几十轮也就
#: 几百 KB —— 这个数只在病态重试循环(一分钟一轮,每轮带满明细)那一档才咬得到。
#: **它只管削重物,不管删记录**,理由在 `shed_bulk` 的 docstring 里。
_HISTORY_BYTES = 8 * 1024 * 1024
#: 削明细时每个阶段留前几条。留几条是为了「翻开还有东西看」,总数照旧说实话。
_FAILURE_SHOW = 3


def history_keep_days() -> int:
    """历史按几天留。**面板上那句话从这里取,不在前端另写一个数字。**

    失败账本的回收窗口也从这里取(`pipeline_failures`)——「账本盖不盖得住还看得到
    的那些轮次」如果靠两个各自写死的条数去猜,一边一改另一边就静默失效。
    """
    return _HISTORY_DAYS


def parse_stamp(raw):
    """把落盘的那种时刻字符串解析出来,**读不出来一律 `None`**(不是「很久以前」)。

    格式是 `pipeline_state._now()` 那一份(`%Y-%m-%d %H:%M:%S`,不带时区)。这些字段
    来自落盘文件,**形状没有任何保证** —— 落成数字时 `strptime` 抛的是 `TypeError`
    不是 `ValueError`,所以这里按类型先筛一道再解析。

    **一个口径两处用**:历史记录的时刻走 `run_stamp`,账本里那一条的时刻直接走这里。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def run_stamp(run: dict):
    """一条历史记录自己说的收尾时刻,读不出来就是 `None`。

    没收尾时间就退到开工时间:一轮被硬杀在半路时只有后者,而它照样是「这条记录有多老」
    的一个诚实答案。
    """
    for key in ("finished_at", "started_at"):
        got = parse_stamp(run.get(key) if isinstance(run, dict) else None)
        if got is not None:
            return got
    return None


def prune_history(runs: list[dict], *, days: int | None = None, floor: int | None = None,
                  now: datetime | None = None) -> list[dict]:
    """按**时间窗口**裁历史(`runs` 是倒序的,最新在最前)。**只按时间,不按条数。**

    两道口径,方向不同,**缺哪一道都有自己的事故**:

    **窗口(`days`)** 是主判据:这么多天之内跑过的那几轮全留着,一条不少。体量那道
    守卫在 `shed_bulk`,它按字节算、而且只削重物不删记录 —— **不许在这里再塞一个条数
    上限**:那等于悄悄把「保留三天」降级成「保留最近 N 轮」,而两者在用户那边是两句
    完全不同的话。

    **判不出时间的一律留着。** 丢一条历史是**不可恢复**的(那一轮的红没有第二个地方
    查得到),多留一条只是多一行 —— 同元规则「漏报是静默丢数据,误报顶多重来一次」。
    所以 `run_stamp()` 给 `None` 的记录不算过期:升级前的旧记录、被手改坏那一行、
    被硬杀在半路连 `started_at` 都没来得及写的那一轮,都走这一档。

    **`floor` 是下限,不是上限。** 最近这几轮无论多老一律留着,管两档:一周只同步一次
    的人窗口里只有当前这一轮,历史列表形同虚设;墙上时钟往前跳(手动改、NTP 校正、换
    时区)的那一刻**整份历史看着全都过期了**,而那恰恰是最不该清空它的时刻。
    **它只限制损失,不等于兜住了时钟** —— 跳过去之后那一轮自己也带着跳过的时刻,拿
    「最新那条记录的时刻」当锚点照样救不回来。
    """
    days = _HISTORY_DAYS if days is None else days
    floor = _HISTORY_FLOOR if floor is None else floor
    # **和写时刻同一个钟。** 两边都出自 `pipeline_state._now()`(`datetime.now()`,不带
    # 时区),拿一个按项目时区算的此刻去比,UTC 宿主机上会凭空差出八小时 —— 那一档会把
    # 窗口边缘上的几轮误判成过期。把 `_now()` 对齐到项目时区是另一件事,它要动的是两份
    # 落盘文件里既有字段的含义(元规则「只增不减」)。
    at = now or datetime.now()
    oldest = at - timedelta(days=days)
    kept = []
    for i, run in enumerate(runs):
        stamp = run_stamp(run)
        if i < floor or stamp is None or stamp >= oldest:
            kept.append(run)
    return kept


def shed_bulk(runs: list[dict], *, budget: int | None = None, floor: int | None = None,
              items: int | None = None) -> list[dict]:
    """体量那道守卫:超出预算时**从最老那头削重物,一条记录都不删**(就地改)。

    这份文件每收尾一轮就整份重写一次,所以它的体量得有个上限。**但那个上限不许靠删
    记录来守** —— 删掉的那一轮,它的红没有第二个地方查得到,而「保留三天」是用户说的
    话;而且条数和体量根本不是一个轴:一千轮瘦记录(各 1 KB)比四十轮带满明细的
    (各 28 KB)还小,按条数封顶会在前者上白削、在后者上又不够。实测一轮 1~28 KB,
    日志占五成、明细占大头。

    所以削的是**重物**,两级,都从最老那头开始:

    1. **日志正文**(`log`)。削掉之后阅读页那一栏照旧有诚实的话:面板本来就为
       「那一轮没留日志」备了一句「上次结束于 X,没有保留详细日志」。
    2. **明细清单削到前几条**(`items`),`failures_total` 留着不动 —— 于是面板念的是
       既有那句「明细 · 50 项(仅列前 3 条)」,和后端 `_cap_failures` 截断过的那一档
       **走同一条显示路径**,一个字都不用新写。

    两级都削完还超,就**留着超**:再往下只剩删记录,而那是这道守卫存在的前提里被排除
    掉的那件事。真正的最后一道是窗口本身 —— 过了三天它们一起消失。

    `floor` 那几轮(最近的)一律不动:下限的意思是「永远还有几轮可翻」,翻开却是空的
    不算可翻。

    **成本必须是线性的:每条记录只序列化它自己,不许为了对一次总数把整份再编码一遍。**
    这一步跑在收尾那把锁里(`_end_run`),而状态端点要同一把锁 —— 慢在这儿就是页面
    读不到状态。原来每削一条都重新编码整份:420 轮带满明细(约 10 MB)实测累计编码
    5.2 GB、收尾 29.6 秒,而面板那条请求 15 秒就超时报断线。所以总量按「各条自己的
    字节之和」增量维护,改一条只重编那一条。**和最终那份文件差一点缩进和逗号** ——
    预算本来就是数量级口径(防的是「每轮重写一次」的成本),不是精确到字节的不变量。

    **坏行只许坏自己。** 落盘那一层只保证根是字典、每条记录是字典(`_history_runs`),
    **没保证 `stages` 也是字典** —— 历史里一条 `stages` 是非空数组/字符串的记录,
    `.values()` 当场抛 `AttributeError`,由收尾那层接住报「留档失败」,于是**整份历史
    一个字节都没写进去**:那条坏行还在,下一轮超预算时又抛一次,新跑的几轮全都查不到。
    面板和解析层对这种行的既有契约是「标成 `invalid`、只跳过这一行」(同
    `pipeline_failures.strip` 和 `_valid_round` 的显式类型检查),这里跟着那个契约。
    """
    budget = _HISTORY_BYTES if budget is None else budget
    floor = _HISTORY_FLOOR if floor is None else floor
    items = _FAILURE_SHOW if items is None else items
    sizes = [len(_dump_bytes(run)) for run in runs]
    used = sum(sizes)
    if used <= budget:
        return runs
    # 从最老那头削:倒着走,`floor` 那几轮不碰
    for i in reversed(range(floor, len(runs))):
        if not runs[i].get("log"):
            continue
        runs[i]["log"] = []
        after = len(_dump_bytes(runs[i]))
        used += after - sizes[i]
        sizes[i] = after
        if used <= budget:
            return runs
    for i in reversed(range(floor, len(runs))):
        stages = runs[i].get("stages")
        if not isinstance(stages, dict):
            continue                       # 坏行只许坏自己,见上面那段
        shed = False
        for stage in stages.values():
            got = stage.get("failures") if isinstance(stage, dict) else None
            if isinstance(got, list) and len(got) > items:
                # **总数先补上再削。** `_cap_failures` 只在它自己截断过的那一档写
                # `failures_total`,没截断过的记录没有这个字段 —— 不补的话面板按数组
                # 长度念,50 条明细被削成 3 条之后它写的是「明细 · 3 项」,而那一轮
                # 真有 50 条:削过和没削过长得一模一样。
                stage.setdefault("failures_total", len(got))
                stage["failures"] = got[:items]
                shed = True
        if not shed:
            continue
        after = len(_dump_bytes(runs[i]))
        used += after - sizes[i]
        sizes[i] = after
        if used <= budget:
            return runs
    return runs
