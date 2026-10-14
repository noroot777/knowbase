# -*- coding: utf-8 -*-
"""管道运行状态:阶段进度、日志、最近一次结果的持久化。

单进程单管道,状态就是一个模块级 dict(与 WeChatOA 的 _crawl_state 同套路),
前端轮询 /api/pipeline/status 展示。各阶段实现与编排都改这里的状态,
单独成模块是为了让 stages / retry / 编排三方都能依赖它而不成环。
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime

from kb.config import DATA_DIR

_STATE_FILE = DATA_DIR / "pipeline_state.json"

STAGES = ("crawl", "export", "index", "extract", "projects", "wiki")

_state: dict = {
    "running": False,
    "stop_requested": False,
    "stopped": False,
    # 这一轮运行的身份。管道是单例,但"正在跑"这个布尔认不出**跑的是哪一轮**:
    # 日报在两次两秒轮询之间,自己那一轮可能已经结束、另一项同步已经接替上来,
    # 它看到的仍是连续的 running=True(2026-09-30 第 2 轮审查实测)。有了 id,
    # "停我自己那一轮"和"等我自己那一轮"才表达得出来。永不复用(进程内自增)。
    "run_id": "",
    "current_stage": "",
    "started_at": "",
    "finished_at": "",
    "crawl_filter": {},
    # 导出阶段**自己**那一份来源范围。常规同步里它和 crawl_filter 一样,重试时
    # 不一样:crawl 收窄到失败的那几个目标,而 export 要照旧走上一轮原来那几个
    # 来源(一个来源完全可能 crawl 成功、export 失败)。**所以它必须单独存一份**
    # —— 只存收窄后的 crawl_filter 的话,下一次重试会从那份收窄范围再派生一次
    # 导出范围,仍然导出失败的那个来源就此被摘掉:面板报「导出完成·已保存 0 条」,
    # 而它那几篇文章一个都没落盘,再点重试也不会回来(2026-10-09 第 2 轮审查实测)。
    "export_filter": {},
    # 「重试失败项」要重试谁:{"run": 这一轮的身份, "channels": {来源: 失败名单}}。
    # 见 note_retry_targets —— 带着 run id,所以不需要在每条启动路径上各清一次。
    "crawl_retry": {},
    "run_kind": "",
    "planned_stages": None,
    # 每阶段: {status: pending/running/ok/skipped/error/stopped, detail: str}
    "stages": {s: {"status": "", "detail": ""} for s in STAGES},
    "log": [],          # 最近若干条进度行
}
_lock = threading.Lock()
_run_seq = 0

# 已经收尾的那几轮运行的终态,按 run id 存着。等一轮同步的人(日报)靠两秒一跳的
# 轮询取快照,而全局状态只有一份:我们那一轮收尾、另一轮马上接替时,轮询就再也
# 看不到自己那一轮的阶段结果了——只知道"不是我的了",不知道它是成功、失败还是
# 被停的。没有这份记录只能猜,而猜成"成功"就是拿一份没人证明过的结果去记账
# (2026-09-30 第 3 轮审查实测)。只留最近几轮:够覆盖"等的那一轮刚被接替"这个
# 窄窗口,又不让状态无限长。
_finished: dict[str, dict] = {}
_FINISHED_KEEP = 8


def new_run_id() -> str:
    """给新一轮运行发一个身份。只在 `_lock` 里调(`start_pipeline` 那一段)。"""
    global _run_seq
    _run_seq += 1
    return f'run-{_run_seq}'


def run_result(run_id: str) -> dict | None:
    """那一轮运行的终态(还在跑、或者已经被挤出记录时回 None)。

    形状和 `get_state()` 的相关字段一致(`stages` / `stopped` / `finished_at`),
    调用方拿它当"我那一轮的最后一份快照"用。
    """
    with _lock:
        got = _finished.get(run_id)
        return deepcopy(got) if got else None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    # 带日期:日志保留 80 行且跨天不清,只打时分秒会看不出是哪天跑的
    _state["log"].append(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}")
    del _state["log"][:-80]


def _stage(name: str, status: str, detail: str = "",
           failures: list[dict] | None = None) -> None:
    """failures: [{"title", "reason"}] 失败明细,面板卡片可展开查看。
    截断 50 条防状态体膨胀(历史积压场景可能一次几百篇)。"""
    entry: dict = {"status": status, "detail": detail}
    if failures:
        entry["failures"] = failures[:50]
    segments = _segments.get(name)
    if segments:
        # 这一阶段正被多个来源各跑一遍:只更新当前这一段,再把各段合成一条对外的结论
        segments[-1].update(entry)
        entry = _merge_segments(segments)
        detail = f"{segments[-1]['label']}：{detail}" if detail else detail
    _state["stages"][name] = entry
    if status == "running":
        _state["current_stage"] = name
    if detail:
        _log(f"{name}: {detail}")


# ── 多来源分段汇报 ──────────────────────────────────────────────────────────────
# 一次同步可以同时抓几个来源,而 crawl / export 两个阶段是每个来源各跑一遍的。
# 各自直接写 _state["stages"][name] 的话,后一个来源的结论会把前一个整个盖掉——
# 面板上只剩最后那个来源的成败,前面几个是成功、失败还是跳过都看不见了。
_segments: dict[str, list[dict]] = {}

# 合并后的状态取"最要紧"的那一档:有一个红就是红,还有没跑完的就是执行中,
# 剩下全跳过才算跳过。
_MERGE_ORDER = ("error", "running", "pending", "stopped", "skipped", "ok")


def _merge_segments(segments: list[dict]) -> dict:
    status = next((s for s in _MERGE_ORDER
                   if any(seg["status"] == s for seg in segments)), "ok")
    entry: dict = {
        "status": status,
        "detail": "；".join(f"{seg['label']}：{seg['detail']}"
                            for seg in segments if seg.get("detail")),
    }
    failures = [item for seg in segments for item in seg.get("failures", [])]
    if failures:
        entry["failures"] = failures[:50]
    return entry


@contextmanager
def stage_segment(name: str, label: str):
    """把这一段时间里对 name 阶段的汇报记成"label 这个来源的那一段"。"""
    segments = _segments.setdefault(name, [])
    segments.append({"label": label, "status": "running", "detail": ""})
    try:
        yield
    finally:
        current = segments[-1]
        if current["status"] == "running":
            # 采集器连一次最终状态都没报(异常穿出去了):不替它编一个成功
            current.update({"status": "error", "detail": current["detail"] or "没有返回结果"})
        _state["stages"][name] = _merge_segments(segments)


def clear_segments() -> None:
    _segments.clear()


# ── 「重试失败项」要重试谁 ─────────────────────────────────────────────────────
# 报得出"这一轮哪几个目标失败了"的采集器在这里留一份**稳定身份**的名单(订阅是
# 它的 id),「重试失败项」据此把下一轮的抓取范围收到失败项上。
#
# **名单必须在失败那一刻记下来,不能事后反推。** 事后能查的只有订阅行上的
# `last_error`,而用户修正地址时 `save_subscription` 的 reset 会把它连同退避一起
# 清空 —— 那是正当的改址行为、不是"抓成功了"的证据,照它反推就把「已改正、还没
# 验证过」的那条当成「已经恢复」筛掉:用户修完地址点重试,那个账号一条都不抓,
# 界面还显示「已跳过」(2026-10-09 第 1 轮审查实测,而那正是这颗按钮的来由)。
#
# **而且必须按来源各记一份。** 一轮可以同时抓几个来源,而 `_merge_segments` 把各
# 段的失败明细扁平合并、项里不带来源归属;拿"合并之后有没有失败项"当一个全局开关
# 的话,RSS 的一条逐项失败会让**所有**来源都按失败项收窄,而整批失败的那个来源
# (一条订阅都没碰到、一份名单都没留下)就被收窄成空集 —— 恢复之后点重试,RSS 补抓
# 了,大V一条也没抓,面板上还不再显示有失败要处理(同轮审查实测)。所以判据是
# 「**这个来源**留下名单了吗」:没留下 = 整批失败、或者它报不出名单(公众号的失败
# 是文章级的,`--targets` 是账号级的,压根对不上),一律按原范围重跑。
#
# 名单带着这一轮的 run id。下一轮一份名单都没留下时(来源级整轮故障),陈旧名单会
# 连同这一轮的状态一起落盘,拿它收窄就是按上一轮的失败去抓 —— 带上身份之后这件事
# 不必在每条启动路径上各清一次,少一处就是少一个会漏的地方。

def note_retry_targets(channel: str, targets, at: str) -> None:
    """记下「这个来源这一轮有哪几个目标失败了」。

    `targets` 是稳定身份(订阅 id 这类),不是显示名 —— 显示名会被改。

    `at` 是这份名单的时刻,用来回答"这个目标后来有没有成功同步过"。**必填,而且
    要用采集器自己那套时间格式**(订阅那边是 `reg.now()` 的 UTC ISO,不是这里
    `_now()` 的本地空格格式):名单只有写它的那个采集器解释得了,而两种格式直接
    比字符串是错的 —— ISO 的 `T`(0x54)排在空格(0x20)后面,同一天里 ISO 那个
    **恒大于**空格那个,于是"它后来成功过"这一问会一律答成"是",整份名单当场
    被摘空。所以这里不给默认值、也不替它回落到 `_now()`:格式由写名单的人负责,
    少传一个参数会当场报错,而回落只会把"填错了"变成"一个很久以后才表现成『重试
    什么都不抓』的坏时钟"。空串原样存着 —— 那等于不设时间判定,保守多抓一次。
    """
    run = _state.get("run_id") or ""
    note = _state.get("crawl_retry") or {}
    if note.get("run") != run:
        note = {"run": run, "channels": {}}
        _state["crawl_retry"] = note
    note["channels"][channel] = {"targets": [str(t) for t in targets], "at": at}


def retry_notes(run: dict) -> dict:
    """那一轮各来源留下的失败名单:{channel: {"targets": [...], "at": ts}}。

    `run` 是 `load_last_run()` 或 `get_state()` 给的那份快照。身份对不上一律
    当成"这一轮没有名单"(宁可按原范围重跑,也不拿上一轮的失败去收窄)。
    """
    note = run.get("crawl_retry") or {}
    if note.get("run") != (run.get("run_id") or ""):
        return {}
    return note.get("channels") or {}


def run_payload() -> dict:
    """一轮运行的终态记录。**落盘的那份和按 id 留的那份是同一份**。

    两处曾经各写各的:`_finished` 只留了身份、结束时刻、stopped 和 stages。于是
    "没亲眼看到自己那一轮收尾、照 id 去取"的调用方(`daily_run._sync_round` 被
    接替的那条路)拿到的是一份**缺计划、缺来源范围、缺失败名单**的记录 —— 它看着
    像一份完整结果,合并时却因为没有 `planned_stages` 被整个过滤掉,那一轮真实
    的抓取失败就此消失。契约只有一份,两边都用它。
    """
    return {
        "started_at": _state["started_at"],
        "finished_at": _state["finished_at"],
        "stopped": _state.get("stopped", False),
        "run_kind": _state.get("run_kind", ""),
        "planned_stages": _state.get("planned_stages"),
        "stages": deepcopy(_state["stages"]),
        "crawl_filter": deepcopy(_state["crawl_filter"]),
        # 导出范围单独落一份:下一次重试要从**它**恢复,不是从收窄后的抓取范围
        "export_filter": deepcopy(_state.get("export_filter") or {}),
        # run_id 跟名单一起落盘:retry_notes 要拿它认"这份名单是不是这一轮的"
        "run_id": _state.get("run_id", ""),
        "crawl_retry": deepcopy(_state.get("crawl_retry") or {}),
        "log": list(_state["log"]),
    }


# **「最近一次运行」的权威在内存里,磁盘只是重启之后的后路。**
# 落盘会失败(满盘、权限、目录没了),而那一下**是吞掉的** —— 吞掉本身没错,一轮同步
# 不该因为写不进状态文件就判失败。错的是拿磁盘当"有没有更新的一轮"的判据:那一轮
# 明明跑完了、结论还在内存里,磁盘上却还是上一份,于是一批更早的结果被判定"有资格
# 当最新",把它刚刚的失败改写成成功、「重试失败项」跟着消失。
# 进程内的每一次收尾都更新这一份,它不会失败,所以它才是判据。
_last_result: dict | None = None
# 内存那份是**某一个**状态文件的回写缓存。`_STATE_FILE` 被改指向别处(切数据目录、
# 测试打桩)时它说的不是同一个库,一律作废,改问新的那份磁盘。
_last_result_file = None


def _remember_result(payload: dict) -> None:
    """记下「本进程见过的最近一次终态」。落盘成不成功都要记。"""
    global _last_result, _last_result_file
    _last_result, _last_result_file = deepcopy(payload), _STATE_FILE


def _authoritative() -> dict | None:
    return _last_result if _last_result_file == _STATE_FILE else None


def latest_run_id() -> str:
    """本进程见过的最近一轮终态的身份。空串 = 这个进程还没见过任何一轮收尾。"""
    return (_authoritative() or {}).get("run_id") or ""


def _save_last_run() -> None:
    """持久化最近一次结果,重启 kb-web 后仍能看到。"""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(run_payload(), ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except OSError as e:
        # 不判这一轮失败,但**也不许一声不吭**:面板上那份结论从这一刻起只活在
        # 内存里,重启就没了。写进这一轮自己的日志,它跟着结论一起被人看到。
        _log(f"这一轮的结果没能写进磁盘({e});本进程内照常可查,重启后会丢")
    _remember_result(run_payload())


def load_last_run() -> dict | None:
    """最近一次运行的终态。**内存那份优先** —— 它是权威,磁盘是重启后的后路。

    面板(`/api/pipeline/status` 的 `last_run`)和「重试失败项」
    (`pipeline_retry.retry_failed_plan`)都走这里,所以两者看到的始终是同一份;
    分别去读磁盘的话,落盘失败的那一轮会让两边一起退回上一轮的结论。
    """
    mine = _authoritative()
    if mine is not None:
        return deepcopy(mine)
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_state() -> dict:
    return deepcopy(_state)


def request_stop(run_id: str = "") -> bool:
    """Finish active work before releasing the single-pipeline lock.

    `run_id` 非空时只停这一轮:调用方手里那个 id 跟当前运行不一致,说明自己
    那一轮已经结束、现在跑的是别人起的,停它就是越权(日报的"停止"原来就会
    误停紧接着启动的普通同步)。空串 = 停当前这一轮,不问是谁起的(面板上那个
    停止按钮就是这个语义)。
    """
    with _lock:
        if not _state["running"]:
            return False
        if run_id and _state.get("run_id") != run_id:
            return False
        if not _state.get("stop_requested"):
            _state["stop_requested"] = True
            _log("已请求停止：当前执行中的阶段结束后，不再启动后续阶段")
        return True


def _end_run() -> None:
    with _lock:
        # 分段是"这一轮某个阶段被几个来源各跑一遍"的临时账本,收尾时一定要清掉:
        # 留着的话下一轮的第一条汇报会被并进上一轮的段里。
        clear_segments()
        stopped = bool(_state.get("stop_requested"))
        _state["stopped"] = stopped
        for name, stage in _state["stages"].items():
            if stage["status"] in ("pending", "running"):
                _stage(name, "stopped" if stopped else "error",
                       ("已停止，未继续执行" if stopped else "流程中断，未完成")
                       + (" · " + stage["detail"] if stage.get("detail") else ""),
                       failures=stage.get("failures"))
        _state["current_stage"] = ""
        _state["finished_at"] = _now()
        _log("同步已停止，已完成的结果保留" if stopped else "管道结束")
        _save_last_run()
        # 收尾状态按 run id 留一份,再把 running 放下:等这一轮的人晚一步来取
        # 也还拿得到自己那一轮的结果(阶段修正已经在上面做完了,存的就是终态)。
        run_id = _state.get("run_id") or ""
        if run_id:
            # 跟落盘那份同一个契约(见 `run_payload`):少一个字段,照 id 来取的
            # 调用方就会拿到一份"看着完整、其实合并不进去"的结果。
            _finished[run_id] = run_payload()
            for old_id in list(_finished)[:-_FINISHED_KEEP]:
                del _finished[old_id]
        _state["running"] = False


# ── 一次业务轮里的几轮管道,合并成一份结果 ──────────────────────────────────────
# 日报那一轮不是一次管道运行,而是 N+1 次:每个来源各一轮 crawl→export,再一轮
# 全库的 index→extract。而「最近一次运行」只有一份,**后跑的会把先跑的盖掉** ——
# 于是 RSS 抓取失败之后,同步中心显示的是最后那一轮入库的「同步已完成」,失败明细
# 和「重试失败项」一起消失(`retry_failed_plan` 读的也是这一份)。两段式之前就已经
# 是这样了(只有最后一个来源的结果留得下来),改成两段式之后变成每次必然发生。
# 所以收尾时把这一批合并成一份发布,让查看和重试面对的是**整个业务轮**。

# 合并时谁盖谁。只在**各自计划里有这个阶段**的记录之间比:没计划过的那一档是
# `skipped/未选择`,让它参与比较会把另一轮真实的 ok 盖成「跳过」。
_MERGE_RANK = {"error": 4, "stopped": 3, "skipped": 2, "pending": 1, "running": 1, "ok": 0}


def _merge_stage(owned: list[tuple[str, dict]]) -> dict:
    """同一个阶段在几轮里的结果,合成**不丢东西**的一份。

    `owned` 是 (这一轮的来源名, 那一轮这个阶段的结果),按时间顺序。

    不能只挑最重的那一个对象:RSS 和网页专栏各挂一条订阅时,两轮的 crawl 都是
    error,挑一个等于把另一个来源的原因和失败名单整个扔掉 —— 面板上「来源:
    RSS + 网页专栏」却只列得出一条明细。status 仍取最重的那一档。

    **按来源挂名,不按文本去重。** 两个来源各失败一条时两边的 detail 一模一样
    (都是「失败 1 个」),照文本去重就把两条说成一条,而卡片上写着来源是两个。
    `failures` 同理,而且更严重:显示用的标题压根不是身份 —— 两条订阅可以同名
    (两个来源各有一个「每日速览」),同名同因就被并掉一条,面板上少一行、
    计数也少一个,看上去就是"只有一个来源出问题"。这里**一条都不并**,原样按
    轮次接起来;真有重复只能是同一轮被记了两遍,那是别处的 bug,并掉等于替它
    遮住。上限跟 `_stage` 一致(50 条),防状态体膨胀。
    """
    worst = max((stage for _, stage in owned), key=_merge_rank)
    merged = {k: v for k, v in deepcopy(worst).items() if k not in ("detail", "failures")}
    details, failures = [], []
    for scope, stage in owned:
        text = (stage.get("detail") or "").strip()
        if text:
            details.append(f"{scope}：{text}" if scope else text)
        failures.extend(deepcopy(item) for item in stage.get("failures") or [])
    if details:
        merged["detail"] = "；".join(details)[:400]
    if failures:
        merged["failures"] = failures[:50]
    return merged


def _merge_rank(stage: dict) -> int:
    from kb.pipeline_retry import _FAIL_DETAIL_RE
    # status=ok 但 detail 里写着「失败 3 个」也是失败(判据和面板按钮、
    # `retry_failed_plan` 共用那一条正则,三处不一致的话按钮会亮着而计划是空的)。
    if _FAIL_DETAIL_RE.search(stage.get("detail") or ""):
        return _MERGE_RANK["error"]
    return _MERGE_RANK.get(stage.get("status") or "", 0)


def _scope_name(run: dict) -> str:
    """这一轮作用在哪几个来源上,写成人话。全库那一轮回空串(它不属于谁)。"""
    from kb.collectors import channel_labels     # 延迟:它反过来 import 本模块
    from kb.pipeline_stages import run_scopes
    labels = channel_labels()
    return " + ".join(labels.get(s["channel"], s["channel"]) for s in run_scopes(run))


def _merge_scopes(records: list[dict], key: str) -> dict:
    """把几轮的来源范围并起来。同一个来源出现多次时按先到的那份算。"""
    from kb.pipeline_stages import crawl_channels   # 延迟:它反过来 import 本模块
    merged: list[dict] = []
    for run in records:
        planned = run.get("planned_stages") or []
        if not ({"crawl", "export"} & set(planned)):
            continue              # 全库那一轮没有来源范围可言,见 `run_scopes`
        for scope in crawl_channels(run.get(key) or run.get("crawl_filter")):
            if scope["channel"] and not any(s["channel"] == scope["channel"] for s in merged):
                merged.append(scope)
    if not merged:
        return {}
    # 一个来源时写回老样子的 channel + accounts:存量读取方(和 `crawl_channels`
    # 自己)两种都认,但旧的那种更窄,能被更多消费方直接读。
    if len(merged) == 1:
        return {"channel": merged[0]["channel"], "accounts": merged[0]["accounts"]}
    return {"channel": merged[0]["channel"], "channels": merged}


class PublishRefused(Exception):
    """这一批没能发布出去。`reason` 是人话原因,调用方要把它说出来。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def publish_batch(records: list[dict]) -> dict | None:
    """把一次业务轮里跑过的几轮管道合并成一份,发布成「最近一次运行」。

    `records` 是各轮的终态快照(`get_state()` 在它 running 放下之后的那一份,
    或者照 id 取回来的 `run_result`,两者同一个契约见 `run_payload`),按时间顺序。
    少于两轮就不发布 —— 那时管道自己落的那份已经代表整轮了。

    **逐阶段合成不丢东西的一份**(见 `_merge_stage`),而且只在计划过这个阶段的
    记录之间比。来源范围、失败名单(`crawl_retry`)按来源并起来,这样「重试失败项」
    拿得到那一轮的目标和原导出范围,不必从当前订阅状态反推。

    合并后的身份**沿用这一批最后那一轮的 run_id**,不另发一个:另发等于把一份
    早先的批次伪装成"刚刚产生的最新结果",而最新指针是有先后的。

    **发布权**:只有当盘上那份「最近一次运行」仍然是这一批里的某一轮时才替换。
    期间要是另一轮独立运行已经收尾(手动同步在日报收尾前跑完一轮),盘上那份是
    **比我们新**的结果,替换掉它等于把一次刚失败的手动同步改写成成功、顺带让它的
    「重试失败项」消失 —— 那一档拒绝发布并抛 `PublishRefused`,由调用方如实说出来。
    一把锁只防同时写,答不了"我有没有资格当最新"。

    写不进去(OSError)同样抛:静默返回会让日报带着一份"失败已经没人看得到"的
    状态照常出报。
    """
    records = [r for r in records if r]
    if len(records) < 2:
        return None
    with _lock:
        ours = {r.get("run_id") for r in records if r.get("run_id")}
        # **判据是内存里那份权威身份,不是磁盘。** 磁盘写会失败而且是吞掉的:
        # 那一轮手动同步明明已经收尾、结论也在内存里,磁盘上却还是我们这一批的
        # 某一轮 —— 照磁盘判,我们就"有资格"把它刚刚的失败改写成成功。
        latest = latest_run_id()
        if latest:
            if latest not in ours:
                raise PublishRefused(
                    f'期间已经有更新的一轮同步收尾({latest}),这一轮日报的'
                    '同步结果没有写进「最近一次运行」——同步中心显示的是那一轮')
        else:
            # 这个进程还没见过任何一轮收尾(刚重启、这一批是从别处恢复的)。
            # 只能问磁盘,而**读不出来时失败关闭**:一份读不出的状态既可能是
            # 「没有更新的运行」也可能是「有而且更重要」,拿不准就不许替换。
            try:
                current = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            except FileNotFoundError:
                current = {}
            except (OSError, json.JSONDecodeError) as e:
                raise PublishRefused(
                    f'读不出当前的「最近一次运行」({e}),不确定期间有没有更新的'
                    '一轮收尾,这一批没有替换它') from e
            if current.get("run_id") and current["run_id"] not in ours:
                raise PublishRefused(
                    f'期间已经有更新的一轮同步收尾({current["run_id"]}),这一轮日报的'
                    '同步结果没有写进「最近一次运行」——同步中心显示的是那一轮')
        run_id = records[-1].get("run_id") or new_run_id()
        scopes = [_scope_name(r) for r in records]
        stages: dict[str, dict] = {}
        for name in STAGES:
            owned = [(scopes[i], r["stages"][name]) for i, r in enumerate(records)
                     if name in (r.get("planned_stages") or []) and (r.get("stages") or {}).get(name)]
            stages[name] = (_merge_stage(owned) if owned
                            else {"status": "skipped", "detail": "未选择"})
        planned = [s for s in STAGES
                   if any(s in (r.get("planned_stages") or []) for r in records)]
        notes: dict = {}
        for run in records:
            note = run.get("crawl_retry") or {}
            if note.get("run") == (run.get("run_id") or ""):
                notes.update(note.get("channels") or {})
        started = [r.get("started_at") for r in records if r.get("started_at")]
        finished = [r.get("finished_at") for r in records if r.get("finished_at")]
        merged = {
            "started_at": min(started) if started else "",
            "finished_at": max(finished) if finished else _now(),
            "stopped": any(r.get("stopped") for r in records),
            "run_kind": "sync",
            "planned_stages": planned,
            "stages": stages,
            "crawl_filter": _merge_scopes(records, "crawl_filter"),
            "export_filter": _merge_scopes(records, "export_filter"),
            "run_id": run_id,
            "crawl_retry": {"run": run_id, "channels": notes} if notes else {},
            "log": [line for r in records for line in (r.get("log") or [])][-80:],
        }
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATE_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        except OSError as e:
            raise PublishRefused(f'同步结果没写进「最近一次运行」({e})') from e
        # 这一批的身份就是最后那一轮,所以这里是**把它那份记录补全成整轮的**,
        # 不是再塞一条新的。
        _finished[run_id] = deepcopy(merged)
        _remember_result(merged)
        for old_id in list(_finished)[:-_FINISHED_KEEP]:
            del _finished[old_id]
        return merged
