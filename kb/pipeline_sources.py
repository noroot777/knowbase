# -*- coding: utf-8 -*-
"""勾了好几个来源时,**按来源串着跑**:A 抓取 → A 导出 → B 抓取 → B 导出。

原来是按阶段串的(所有来源抓完,再所有来源导出)。同步中心按「类型」排节点之后,
「跳过这一类」「提前结束这一类、直接下一类」要求一个来源的两步挨在一起跑完 ——
按阶段串的话,「订阅源」这一类要等公众号抓完才轮到它的导出,提前结束它也就无从
说起。只有一个来源时那条老路一行不动(`pipeline._run_pipeline`,既有回归钉着它
递给 `_run_crawl` / `_run_export` 的参数)。

**账本那几笔的先后照旧**(`fetch_rounds`):开轮在前、空回执其次、各来源的回执并进
来、整轮走完之后才记「交完了」。开轮那道闸拦住时照样抛,而且抛在空回执之前 ——
那一轮压根没起跑,不许先落一张「确定 0 条」的回执。

**没跑的那一段也要建账,但不交覆盖范围**(`_settle`):跳过的、被停掉没轮到的、
提前结束没导出的那几格记成 `skipped` / `stopped`,当日账本把它们按「没执行过」
读(`pipeline_retry.stage_ran`)—— 零覆盖,旧欠账原样留着。**一格都不建是错的**:
没有自己那一格时折账退回整步那张合并卡片,而只要别的来源都成了,那张卡片就是绿的,
没跑的这个来源的旧失败会被它结清。

硬约束在 `docs/decisions/sync-flow.md` 第一节。
"""
from __future__ import annotations

from kb import pipeline_flow as flow
from kb import pipeline_results as results
from kb.collectors import FetchScope, active_collector
from kb.pipeline_batch import round_key
from kb.pipeline_segments import stage_segment
from kb.pipeline_stages import _channel_label, _one_channel, _scope_label, crawl_channels
from kb.pipeline_state import _log, _stage, _state

SKIPPED = "本次跳过"
STOPPED = "已停止，未继续执行"
CUT = "本类型提前结束，这次没导出"
CUT_REST = "这一类提前结束，这一步没跑"


def _settle(stage: str, scope: dict, collector, status: str, detail: str) -> None:
    """这个来源这一步**没跑**:记它自己那一段,建一格账,不交覆盖范围。

    建账要算一次计划范围(可能读订阅库,见 `pipeline_results.planned_targets`),读不
    出来就只记那一段 —— 折账退回整步卡片,而这一段本身就是 skipped/stopped,不会
    让那张卡片变绿。
    """
    label = _channel_label(collector) if collector is not None else _scope_label(scope)
    with stage_segment(stage, label):
        if collector is not None:
            try:
                with results.channel_run(stage, scope["channel"], collector,
                                         scope["accounts"]):
                    _stage(stage, status, detail)
                return
            except Exception:                          # noqa: BLE001 - 见上
                pass
        _stage(stage, status, detail)


def _collector(scope: dict):
    """取这个来源的采集器,**整条路上每个来源只取一次**(理由在 `pipeline_stages._one_channel`)。"""
    try:
        return active_collector(scope["channel"]), None
    except Exception as e:                             # noqa: BLE001 - 记成它自己那一段
        return None, f"{type(e).__name__}: {e}"


def _broken(stage: str, scope: dict, label: str, detail: str) -> None:
    """这个来源这一步没起得来:合并卡片上记它那一段,**结果账上也记它自己那一格**。

    只记前一样的话,同步中心按类型点开它时找不到它自己的结论 —— 那张合并卡片是几个
    来源合起来的,不许拿来念某一个 —— 失败的来源就这么没了下文(`results.failed`)。
    """
    with stage_segment(stage, label):
        _stage(stage, "error", detail)
    results.failed(stage, scope["channel"], scope.get("accounts"), detail)


def _guarded(stage: str, scope: dict, collector, run) -> None:
    """跑这个来源的这一步;炸了记成它自己那一段的失败,别把后面的来源一起带走。

    隔离的范围同 `pipeline_stages._per_channel`:建账、执行、汇报一步都不露在外面。
    建账那一下(`results.channel_run` 进门)就炸的,自己那一格还没建 —— 由 `_broken` 补上。
    """
    try:
        with stage_segment(stage, _channel_label(collector)):
            try:
                _one_channel(stage, scope, run, collector)
            except Exception as e:                     # noqa: BLE001 - 见上
                _stage(stage, "error", f"{type(e).__name__}: {e}")
                results.failed(stage, scope["channel"], scope.get("accounts"),
                               f"{type(e).__name__}: {e}")
    except Exception as e:                             # noqa: BLE001 - 分段收尾自己炸了
        _broken(stage, scope, _scope_label(scope), f"{type(e).__name__}: {e}")


def _one_source(scope: dict, steps: list[str], filters: dict, key: str,
                owner: str, node: str) -> None:
    from kb import fetch_rounds

    collector, broken = _collector(scope)
    if collector is None:
        for stage in steps:
            _broken(stage, scope, _scope_label(scope), broken)
        return
    if "crawl" in steps and not flow.advance(owner, node, "crawl"):
        _settle("crawl", scope, collector, "skipped", CUT_REST)
    elif "crawl" in steps:
        def _fetch(c, s: dict):
            """**采集器报的覆盖范围要原样交上去**(见 `pipeline_stages._one_channel`)。"""
            _log(f"采集通道: {_channel_label(c)}")
            return c.fetch(FetchScope(
                targets=tuple(s["accounts"]),
                since=filters.get("since_date", ""),
                until=filters.get("until_date", ""),
                force=bool(filters.get("force")),
            ))
        _guarded("crawl", scope, collector, _fetch)
    if "export" not in steps:
        return
    if _state.get("stop_requested"):
        _settle("export", scope, collector, "stopped", STOPPED)
    elif flow.advance(owner, node, "export"):
        _guarded("export", scope, collector,
                 lambda c, s: fetch_rounds.note(key, c.materialize))
    else:
        _settle("export", scope, collector, "skipped", CUT)


def _open(steps: list[str], key: str, seg: str) -> None:
    """账本上开这一轮:开轮(要抓取时)在前,空回执(要导出时)其次。"""
    from kb import fetch_rounds

    if "crawl" in steps:
        planned = _state.get("planned_stages")
        try:
            fetch_rounds.open_round(
                key, seg=seg,
                materializes="export" in planned if isinstance(planned, list) else True)
        except fetch_rounds.NoEvidence as exc:
            # 原因先写进卡片再抛(理由同 `pipeline._run_pipeline` 单来源那一支)。
            _stage("crawl", "error", str(exc))
            _log(f"抓取没有起跑:{exc}")
            raise
    if "export" in steps:
        fetch_rounds.note_new(key, ())


def run_sources(stages: list[str], crawl_filter: dict | None, owner: str) -> None:
    """多来源那一档的抓取 + 导出。异常只在开轮那道闸上穿出去(同单来源那条路)。

    **账本上的这一轮等第一个真要跑的来源进门时才开**:一个来源都没跑(全被跳过、
    或者头一个就碰上停止)的那一轮不是一次抓取,口径同单来源那条路上跳过的那一档
    —— 开了就是凭空多出一次「确定 0 条」的抓取。
    """
    from kb import fetch_rounds

    steps = [s for s in flow.SOURCE_STEPS if s in stages]
    if not steps:
        return
    scopes = crawl_channels(crawl_filter)
    filters = crawl_filter or {}
    key, seg = round_key()
    opened = False
    for i, scope in enumerate(scopes):
        if _state.get("stop_requested"):
            # 停止是对整次同步说的:没轮到的来源一个都不再起,但各自留一格「被停了」。
            for rest in scopes[i:]:
                collector, _ = _collector(rest)
                for stage in steps:
                    _settle(stage, rest, collector, "stopped", STOPPED)
            break
        node = flow.source_key(scope["channel"])
        if not flow.enter(owner, node):
            collector, _ = _collector(scope)
            for stage in steps:
                _settle(stage, scope, collector, "skipped", SKIPPED)
            continue
        try:
            if not opened:
                opened = True
                try:
                    _open(steps, key, seg)
                except Exception as exc:
                    # 这一轮没起跑(卡片上的原因 `_open` 已经写了);进了门的这个来源
                    # 也要在卡住的那一步留下自己那一格,不然点开它是一片空白。后面那
                    # 一步压根没轮到,不替它编结论(前端念成「结果未知」)
                    results.failed(steps[0], scope["channel"], scope.get("accounts"),
                                   f"这一轮没有起跑:{exc}")
                    raise
            _one_source(scope, steps, filters, key, owner, node)
        finally:
            flow.leave(owner, node)
    if opened and "export" in steps:
        # 「交完了」只在循环正常走完(含被停掉的那一档)之后记,理由在
        # `fetch_rounds.note_end`;异常穿出去的那一轮不记,留成「不可确认」。
        fetch_rounds.note_end(key, seg)
