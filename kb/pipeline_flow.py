# -*- coding: utf-8 -*-
"""一次同步按「类型」排成的那条线:这一轮有哪几类、走到哪一类、谁被跳过、谁被提前结束。

**类型是给人看的那一层,阶段是给机器跑的那一层。** 同步中心那张卡片上排着的是
「订阅源 / 公众号 / 入库处理 / 生成日报 / 整理工具与 Wiki」这样的类型节点,每个
节点下面才是它的那几步(`crawl` / `export` / `index` …)。用户在节点上点「本次跳过」
「结束本类型」,本模块就是那几个请求落下来的地方,以及编排那一层回头问「轮到它了,
还跑不跑」的那个答案。

**本模块只记「要求了什么、走到了哪」,不记成败。** 成败照旧在阶段卡片和结果账里
(`pipeline_state` / `pipeline_results`),口径只有那一份;这里多记一份就是两份口径。
唯一的例外是「生成日报」那一个节点:它不是管道里的阶段,没有卡片可挂,所以它的
结论由日报那一轮在离开时交进来(`leave(..., result)`)。

**谁的计划,由谁收尾。** 计划有一个 owner:手动同步是那一轮的 run id,日报是那一批
的 batch 身份(一批里有好几轮管道,共用一份计划)。所有入口都带 owner,对不上的一律
当成「这一份计划管不到你」—— 日报两轮之间短暂空闲,挤进来的一次手动同步不能顺手
把日报那份计划改掉,也不能被它的跳过名单拦住。

**计划按 owner 各存一份(`_plans`),谁都不许替换别人那一份。** 原来全局只有一格:
手动同步正在抓、用户刚点了「结束本类型」,定时日报这时开张(它在等管道空出来之前就建
计划),那一格被换成日报的,手动那一轮回头再问「还跑不跑」时已经找不到自己的计划,
于是被当成「本来就不受控」放行 —— 导出照跑,已经受理的那一下就此作废。一份计划只在
它自己的 owner 收尾时(`end`)才消失。

**锁序:`pipeline_state._lock` → 本模块的 `_lock`**(`pipeline_batch` 合并发布时在
那把锁里读 `record`)。所以持着本模块的锁时**不许**再碰会取那把锁的函数;写日志
一律放到锁外。

硬约束在 `docs/decisions/sync-flow.md` 第一节。
"""
from __future__ import annotations

import threading
from copy import deepcopy

from kb import pipeline_state as ps

#: 每个来源类型节点下面的那两步(按来源各跑一遍的就是这两步)。
SOURCE_STEPS = ("crawl", "export")

#: 全库那几步按类型归组。`generate` 不是管道阶段,只出现在日报那一份计划里。
GROUPS = (("ingest", "入库处理", ("index", "extract")),
          ("daily", "生成日报", ("generate",)),
          ("tidy", "整理工具与 Wiki", ("projects", "wiki")))

STALE = "这一轮已经结束或换了一轮，刷新一下再看"

_lock = threading.Lock()
# owner → 那一份计划。改它一律持 `_lock`;只读一格的地方(`cutting`)不持锁,理由在那边。
_plans: dict[str, dict] = {}


def source_key(channel: str) -> str:
    return f"src:{channel}"


def _labels() -> dict:
    """通道 id → 人话名字。**取不到就退回 id**:名字只是展示,不能为它拦住一次同步。"""
    try:
        from kb.collectors import channel_labels
        return channel_labels()
    except Exception:                                  # noqa: BLE001 - 见上
        return {}


def source_nodes(channels, steps) -> list[dict]:
    """每个来源一个节点,只挂这一轮真要跑的那几步(`steps` 为空就一个都不建)。"""
    steps = [s for s in SOURCE_STEPS if s in steps]
    if not steps:
        return []
    labels, out = _labels(), []
    for channel in channels:
        key = source_key(channel)
        if channel and all(n["key"] != key for n in out):
            out.append({"key": key, "kind": "src", "channel": channel,
                        "label": labels.get(channel) or channel, "steps": list(steps)})
    return out


def group_nodes(stages) -> list[dict]:
    """全库那几组:组里有任何一步在计划里,这一组才算这一轮的一类。"""
    return [{"key": key, "kind": key, "label": label,
             "steps": [s for s in steps if s in stages]}
            for key, label, steps in GROUPS if any(s in stages for s in steps)]


def _new(owner: str, kind: str, nodes: list[dict]) -> dict:
    return {"owner": owner, "kind": kind, "nodes": nodes,
            "skips": [], "cuts": {}, "cut": "",
            "running": "", "step": "", "done": [],
            "times": {}, "results": {}}


def begin(run_id: str, batch: str, planned, crawl_filter) -> str:
    """一轮管道起跑时调一次,返回这一轮该带着的 owner(空串 = 这一轮没有计划)。

    - 属于某一批、而那一批有计划(日报)⇒ 沿用那份,不新建;
    - 其余一律按这一轮自己的计划新建,owner 是它的 run id。日报那份计划还在也一样:
      两份各存各的,新建这一份碰不到它(原来全局一格时只能不给计划)。
    """
    with _lock:
        if batch and batch in _plans:
            return batch
    from kb.pipeline_stages import crawl_channels      # 延迟:那边 import 链会绕回来
    channels = [s["channel"] for s in crawl_channels(crawl_filter)]
    nodes = source_nodes(channels, planned) + group_nodes(planned)
    with _lock:
        # 锁外建节点的那段间隙里别人开张了也无所谓:落笔只写自己这一格
        _plans[run_id] = _new(run_id, "sync", nodes)
    return run_id


def open_daily(batch: str, channels, library: bool) -> None:
    """日报那一批开张时建它的整份计划:各来源 → 入库处理 → 生成日报 →(整理)。"""
    stages = ["index", "extract", "generate"] + (["projects", "wiki"] if library else [])
    nodes = source_nodes(channels, SOURCE_STEPS) + group_nodes(stages)
    with _lock:
        # **只写自己这一格**:此刻正在跑的那轮手动同步的计划(连同它已经受理的跳过 /
        # 提前结束)原样留着,直到它自己收尾
        _plans[batch] = _new(batch, "daily", nodes)


def _node(owner: str, key: str) -> tuple[dict | None, dict | None]:
    """持锁调用。返回 (那份计划, 那一类);owner 没有计划、或者计划里没有这一类,都是 None。"""
    plan = _plans.get(owner) if owner else None
    if plan is None:
        return None, None
    return plan, next((n for n in plan["nodes"] if n["key"] == key), None)


def enter(owner: str, key: str) -> bool:
    """轮到 `key` 这一类了:返回 True 就照常跑,False 就是用户要求这次跳过它。

    管不到的(owner 对不上、计划里没有它)一律 True —— 这份计划不认识的东西,不该由
    它来拦。可重入:日报那一轮先进一次,它起的那轮管道再进一次,时刻按第一次记。
    """
    with _lock:
        plan, node = _node(owner, key)
        if node is None:
            return True
        if key in plan["skips"]:
            if key not in plan["done"]:
                plan["done"].append(key)
            plan["results"].setdefault(key, {"status": "skipped", "detail": "本次跳过"})
            return False
        if plan["running"] != key:
            plan["running"], plan["step"] = key, ""
        plan["times"].setdefault(key, {"started_at": ps._now(), "finished_at": "",
                                       "steps": {}})
        return True


def advance(owner: str, key: str, stage: str) -> bool:
    """`key` 这一类要开始跑 `stage` 这一步了(上一步顺手收尾)。

    返回 False = 它已经被要求提前结束,**这一步不跑**(编排层把余下的步骤记成没跑)。
    管不到的一律 True。

    **判据和换步在同一把锁里**(同 `request`):提前结束只在手上那一步还不是最后
    一步时受理,而这里一换步,请求就按新的那一步重新判 —— 不会出现「请求受理了、
    下一步照样开跑」(那一步要是导出,翻译 / 补抓读不到这个请求,会整步跑完)。
    """
    with _lock:
        plan, node = _node(owner, key)
        if node is None or plan["running"] != key:
            return True
        if key in plan["cuts"]:
            return False
        now, steps = ps._now(), plan["times"][key]["steps"]
        prev = plan["step"]
        if prev and prev != stage and prev in steps and not steps[prev][1]:
            steps[prev][1] = now
        steps.setdefault(stage, [now, ""])
        plan["step"] = stage
        return True


def _close_step(plan: dict, now: str) -> None:
    prev = plan["step"]
    steps = (plan["times"].get(plan["running"]) or {}).get("steps") or {}
    if prev and prev in steps and not steps[prev][1]:
        steps[prev][1] = now


def leave(owner: str, key: str, result: dict | None = None) -> None:
    """`key` 这一类走完了(跑完、被提前结束、出错穿出去都算)。没进过门的不记。"""
    with _lock:
        plan, node = _node(owner, key)
        if node is None:
            return
        times = plan["times"].get(key)
        if times is None and key not in plan["skips"]:
            return
        now = ps._now()
        if plan["running"] == key:
            _close_step(plan, now)
            plan["running"], plan["step"] = "", ""
        if key not in plan["done"]:
            plan["done"].append(key)
        if times is not None and not times["finished_at"]:
            times["finished_at"] = now
        if plan["cut"] == key:
            plan["cut"] = ""
        if result:
            plan["results"][key] = dict(result)


def leave_running(owner: str) -> None:
    """收尾兜底:还挂在「正在跑」上的那一类,一律按走完记。"""
    with _lock:
        plan = _plans.get(owner) if owner else None
        running = plan["running"] if plan else ""
    if running:
        leave(owner, running)


def cutting() -> bool:
    """**此刻正在跑的那一类**被要求提前结束了。采集器的「该停了吗」读这一份。

    **还要核一遍这份计划是不是此刻这轮管道的**(owner 等于它的 run id 或它所在那一批)。
    日报那一批进了某一类、却还排在别人起的那轮手动同步后面等的时候,那一类上的提前
    结束不许落到别人那一轮的采集器头上 —— 只看「计划里正在跑谁」的话,别人抓得正欢
    的那一步会被它停掉。

    **不持锁**:采集器在热循环里问它,而这里只读两格(字典取值、字段比较),写那一侧
    都在锁里整格落笔;读到的最坏情况是晚一次轮询才看到刚受理的那一下。
    """
    state = ps._state
    if not state.get("running"):
        return False
    for owner in (state.get("run_id"), state.get("batch")):
        plan = _plans.get(owner) if owner else None
        if plan and plan["cut"] and plan["cut"] == plan["running"]:
            return True
    return False


def halted() -> bool:
    """采集器里那几处「该停了吗」的唯一口径:整轮停止,或者这一类被提前结束。"""
    return bool(ps._state.get("stop_requested")) or cutting()


def stopping(kind: str) -> bool:
    """这一份计划所在的那一轮已经被要求停止了(日报那一批另认日报自己的停止)。"""
    if ps._state.get("stop_requested"):
        return True
    if kind != "daily":
        return False
    from kb.daily_state import _state as daily      # 延迟:日报那边整条链很重
    return bool(daily.get("stop_requested"))


def _cut_step(plan: dict, node: dict) -> tuple[str, str]:
    """能不能提前结束 `node`(它正在跑);能就返回 (正在跑的那一步, ''),不能返回 ('', 原因)。"""
    if node["kind"] == "daily":
        return "", "日报已经开始生成，没法中途结束，整轮也停不下来（模型已经在出稿）；做完会接着往下走"
    steps = node["steps"]
    current = plan["step"] or (steps[0] if steps else "")
    if node["kind"] == "src" and current != "crawl":
        return "", "这已经是这一类的最后一步，做完就接着下一类"
    if current not in steps or steps.index(current) == len(steps) - 1:
        return "", "这已经是这一类的最后一步，做完就接着下一类"
    return current, ""


def request(owner: str, key: str, action: str) -> tuple[bool, str]:
    """用户在某一类上点的那颗按钮。返回 (受理了没有, 不受理的原因)。

    - `skip`:还没轮到的那一类,这次不跑;`unskip` 撤回(轮到之前随时可以)。
    - `cut`:正在跑的那一类,手上这一步停在下一个能停的地方,余下的步骤这次不跑,
      接着跑下一类。最后一步不受理(提前结束它和等它做完是一回事,承诺了也兑现不了)。

    **判据和落笔在同一把锁里**:「还没轮到」和「轮到它了」之间没有缝,用户点跳过的
    那一刻编排层正好进门,两边只会有一个赢。
    """
    with _lock:
        plan, node = _node(owner, key)
        if plan is None:
            return False, STALE
        if node is None:
            return False, "这一轮没有这一类"
        kind, label = plan["kind"], node["label"]
        if action not in ("skip", "unskip", "cut"):
            return False, f"不认识的操作:{action}"
    # 停止那一道读的是别处的状态,放到锁外问(问完再进锁核一遍其余条件)。
    if stopping(kind):
        return False, "已经请求停止这一轮了"
    with _lock:
        # 锁外那一问的间隙里这份计划可能已经收尾了:按 owner 重新取,不沿用上面那份
        plan = _plans.get(owner)
        if plan is None:
            return False, STALE
        started = key in plan["times"] or key in plan["done"]
        running = plan["running"] == key
        if action == "skip":
            if key in plan["skips"]:
                return True, ""
            if running:
                return False, "这一类已经开始了；要提前结束它，用「结束本类型」"
            if started:
                return False, "这一类已经做完了"
            if key not in plan["skips"]:
                plan["skips"].append(key)
            note = f"已请求本次跳过「{label}」"
        elif action == "unskip":
            if running or started:
                return False, "已经轮到它了"
            if key in plan["skips"]:
                plan["skips"].remove(key)
            note = f"已撤回跳过「{label}」"
        else:
            if not running:
                return False, "只有正在跑的那一类能提前结束"
            if key in plan["cuts"]:
                return True, ""
            current, why = _cut_step(plan, node)
            if why:
                return False, why
            plan["cut"] = key
            plan["cuts"][key] = {"step": current, "at": ps._now()}
            note = f"已请求提前结束「{label}」，手上这一步停下后接着下一类"
    ps._log(note)
    return True, ""


def live(state: dict) -> dict | None:
    """面板此刻该画的那一份计划(副本)。没有就是 None。

    管道在跑 ⇒ **这一轮自己的**那份(owner 是它的 run id 或它所在那一批),没有就是
    None —— 不拿别人那份来顶。管道空着 ⇒ 日报那一批的(两轮管道之间、正在生成日报)。
    前端那道身份核对(`pipe_flow.js` 的 `pipeFlowLive`)照旧再核一遍。
    """
    with _lock:
        if state.get("running"):
            owners = [o for o in (state.get("run_id"), state.get("batch")) if o]
            plan = next((_plans[o] for o in owners if o in _plans), None)
        else:
            plan = next((p for p in _plans.values() if p["kind"] == "daily"), None)
        return deepcopy(plan)


def record(owner: str) -> dict:
    """`owner` 那份计划的副本,落盘用。不是它的(或者已经没了)返回 `{}`。"""
    with _lock:
        plan = _plans.get(owner) if owner else None
        return deepcopy(plan) if plan else {}


def end(owner: str) -> None:
    """`owner` 那一轮整个收尾了,计划作废。别人那几份不动。"""
    with _lock:
        if owner:
            _plans.pop(owner, None)
