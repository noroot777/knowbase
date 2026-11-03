# -*- coding: utf-8 -*-
"""勾了好几个来源时,**各来源同时跑**:每个来源一条线程,各自 抓取 → 导出。

一个来源的两步挨在一起跑完(「跳过这一类」「结束本类型」要这样才说得清),来源与来源
之间不再排队 —— 串着跑的时候一轮的来源段是各段耗时相加(公众号限速停顿、X 一页页加载、
YouTube 等模型回复,47–68 分钟),同时跑之后取最长那一路。只有一个来源时那条老路一行不动
(`pipeline._run_pipeline`,既有回归钉着它递给 `_run_crawl` / `_run_export` 的参数)。

**全库那几步前面有一道门**(`Lanes.wait_gate`):入库处理要等所有来源跑完 —— 新导出的
文件不进索引,日报就取不到它们。门开着等的时候面板上那一行写着在等谁,用户可以点
「不等了，继续」(`pipeline_flow.request(..., "go")`):没跑完的那几路照常跑完、照常记账,
只是这一轮的入库和日报不等它;它们的**导出挪到全库那几步之后**(`Lanes.hold`)—— 订阅
那几类的导出整段攥着维护锁,跟入库处理抢锁的话索引最坏要干等十几分钟。整轮收尾之前
等所有来源都跑完(`Lanes.join`):管道是单例,没跑完的那一路还在写库,不许宣布空闲。

**归属按线程认**(`pipeline_lanes`):每条来源线程进门时绑上自己的通道,阶段卡片的分段、
结果账、日志都据此知道一句话是谁说的。

**账本那几笔的先后照旧**(`fetch_rounds`):开轮在前、空回执其次、各来源的回执并进
来、**所有来源都走完之后**才记「交完了」。开轮那道闸拦住时照样抛,而且抛在空回执之前
—— 那一轮压根没起跑,不许先落一张「确定 0 条」的回执。开轮在主线程上做(各路起跑之前),
所以那道闸抛出去的异常照旧穿到编排层。

**没跑的那一段也要建账,但不交覆盖范围**(`_settle`):跳过的、被停掉没轮到的、
提前结束没导出的那几格记成 `skipped` / `stopped`,当日账本把它们按「没执行过」
读(`pipeline_retry.stage_ran`)—— 零覆盖,旧欠账原样留着。**一格都不建是错的**:
没有自己那一格时折账退回整步那张合并卡片,而只要别的来源都成了,那张卡片就是绿的,
没跑的这个来源的旧失败会被它结清。

硬约束在 `docs/decisions/sync-flow.md` 第一节。
"""
from __future__ import annotations

import threading
import time

from kb import pipeline_flow as flow
from kb import pipeline_lanes as lanes
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


def _one_source(scope: dict, collector, steps: list[str], filters: dict, run: "Lanes",
                node: str) -> None:
    """一个来源的 抓取 → 导出,跑在它自己那条线程上(`Lanes._lane`)。"""
    from kb import fetch_rounds

    owner = run.owner
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
    if not run.hold(node) or _state.get("stop_requested"):
        _settle("export", scope, collector, "stopped", STOPPED)
    elif flow.advance(owner, node, "export"):
        _guarded("export", scope, collector,
                 lambda c, s: fetch_rounds.note(run.key, c.materialize))
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


class Lanes:
    """一轮里同时在跑的那几路来源。编排层的用法只有三句:`start` → `wait_gate` → `join`。

    - `start`:进门、开轮、每个进了门的来源起一条线程。异常只在开轮那道闸上穿出去
      (同单来源那条路)。
    - `wait_gate`:全库那几步开跑之前调。等所有来源跑完;用户点了「不等了」、或者整轮
      被停,就不等了 —— 那一刻还没跑完的几路记成「这一轮不等」(`late`)。
    - `join`:收尾之前**一定要调**(放在 `finally` 里)。先宣布全库那几步走完了(没等的
      那几路这才开始导出),再等所有来源跑完,最后才记账本那一笔「交完了」。
    """

    def __init__(self, stages: list[str], crawl_filter: dict | None, owner: str):
        self.steps = [s for s in flow.SOURCE_STEPS if s in stages]
        self.crawl_filter = crawl_filter
        self.filters = crawl_filter or {}
        self.owner = owner
        self.key = self.seg = ""
        self.opened = False
        # 有一路的异常穿出了它自己的隔离(`_guarded` 之外):这一轮不记「交完了」。
        self.broken = False
        self.threads: list[tuple[str, threading.Thread]] = []
        self.late: set[str] = set()
        self.exporting: set[str] = set()
        self.tail = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        """**账本上的这一轮等真有来源进门时才开**:一个来源都没跑(全被跳过、或者起跑时
        已经被停)的那一轮不是一次抓取,开了就是凭空多出一次「确定 0 条」的抓取。

        采集器**整条路上每个来源只取一次,按来源顺序在主线程上取**(理由在
        `pipeline_stages._one_channel`;放进各路线程里取的话谁先谁后就说不准了)。
        """
        if not self.steps:
            return
        scopes = crawl_channels(self.crawl_filter)
        self.key, self.seg = round_key()
        if _state.get("stop_requested"):
            # 停止是对整次同步说的:一个来源都不再起,但各自留一格「被停了」。
            for scope in scopes:
                collector, _ = _collector(scope)
                for stage in self.steps:
                    _settle(stage, scope, collector, "stopped", STOPPED)
            return
        entered = []
        for scope in scopes:
            node = flow.source_key(scope["channel"])
            if flow.enter(self.owner, node):
                entered.append((scope, node))
                continue
            collector, _ = _collector(scope)
            for stage in self.steps:
                _settle(stage, scope, collector, "skipped", SKIPPED)
        if not entered:
            return
        try:
            _open(self.steps, self.key, self.seg)
        except Exception as exc:
            # 这一轮没起跑(卡片上的原因 `_open` 已经写了);进了门的几个来源也要在卡住的
            # 那一步留下自己那一格,不然点开它是一片空白。后面那一步压根没轮到,不替它
            # 编结论(前端念成「结果未知」)
            for scope, node in entered:
                results.failed(self.steps[0], scope["channel"], scope.get("accounts"),
                               f"这一轮没有起跑:{exc}")
                flow.leave(self.owner, node)
            raise
        self.opened = True
        for scope, node in entered:
            collector, broken = _collector(scope)
            thread = threading.Thread(target=self._lane, args=(scope, node, collector, broken),
                                      name=f"src-{scope['channel']}", daemon=True)
            self.threads.append((node, thread))
            thread.start()

    def _lane(self, scope: dict, node: str, collector, broken: str | None) -> None:
        with lanes.bind(scope["channel"]):
            try:
                if collector is None:
                    for stage in self.steps:
                        _broken(stage, scope, _scope_label(scope), broken)
                else:
                    _one_source(scope, collector, self.steps, self.filters, self, node)
            except Exception as e:                     # noqa: BLE001 - 一路炸了不带走别的
                self.broken = True
                _log(f"这一路意外中断:{type(e).__name__}: {e}")
            finally:
                flow.leave(self.owner, node)

    def running(self) -> list[str]:
        """还没跑完的那几类(节点 key,按来源顺序)。"""
        return [node for node, thread in self.threads if thread.is_alive()]

    def wait_gate(self) -> None:
        """全库那几步开跑之前:等所有来源跑完。用户点了「不等了」或者整轮被停,就不等。"""
        if not self.running():
            flow.gate(self.owner, "open")
            return
        flow.gate(self.owner, "wait")
        while self.running():
            if _state.get("stop_requested") or flow.released(self.owner):
                break
            time.sleep(0.5)
        with self._lock:
            late = [] if _state.get("stop_requested") else \
                [n for n in self.running() if n not in self.exporting]
            self.late.update(late)
        if late:
            # 门打开那一刻还在跑的几路:照常跑完、照常记账,导出挪到全库那几步之后
            names = "、".join(flow.label_of(self.owner, n) for n in late)
            _log(f"这一轮不等 {names}：它照常跑完、照常记账，新内容进下一轮入库")
        flow.gate(self.owner, "go" if late else "open", late)

    def hold(self, node: str) -> bool:
        """这一路要开始导出了。没等它的那一路要等全库那几步走完;返回 False = 等的期间整轮被停了。

        「这一路算不算被不等」和「开始导出」在同一把锁里定:门正好在这一刻打开时,要么它
        已经在导出(那就照常导完,索引会排队等它那把维护锁),要么它被记成不等、导出挪后。
        """
        with self._lock:
            if node not in self.late:
                self.exporting.add(node)
                return True
        if not self.tail.is_set():
            flow.park(self.owner, node)
            _log("这一轮没等它，导出排在全库那几步之后")
        while not self.tail.wait(0.5):
            if _state.get("stop_requested"):
                return False
        return True

    def join(self) -> None:
        """收尾:宣布全库那几步走完了,等所有来源跑完,再记「交完了」。"""
        from kb import fetch_rounds

        self.tail.set()
        for _, thread in self.threads:
            thread.join()
        if self.opened and "export" in self.steps and not self.broken:
            # 「交完了」只在每一路都走完(含被停掉的那一档)之后记,理由在
            # `fetch_rounds.note_end`;有一路的异常穿出了隔离的那一轮不记,留成「不可确认」。
            fetch_rounds.note_end(self.key, self.seg)


def run_sources(stages: list[str], crawl_filter: dict | None, owner: str) -> Lanes:
    """多来源那一档:各来源同时起跑,返回那一组(编排层接着 `wait_gate` / `join`)。"""
    run = Lanes(stages, crawl_filter, owner)
    run.start()
    return run
