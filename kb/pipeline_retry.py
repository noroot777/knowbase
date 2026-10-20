# -*- coding: utf-8 -*-
"""重试失败项:按上次运行的失败状态编排 回填空正文 → 重建索引 → 重跑提取。

**硬约束在 `docs/decisions/retry-scope.md` 第一节 —— 改这块之前先读那一份。**

面板上那个全局「重试失败项」按钮的后端。计划由 retry_failed_plan 推导,
执行复用 kb.pipeline_stages 里的阶段函数。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from copy import deepcopy
from pathlib import Path

from kb import pipeline_daybook as daybook
from kb.collectors import active_collector, channel_labels, retry_narrower
from kb.pipeline_state import (
    _SEQ_GEN,
    STAGES,
    _end_run,
    _latest_locked,
    _lock,
    _log,
    _now,
    _stage,
    _state,
    last_result,
    mark_open,
    new_run_id,
    restore_gap,
    same_round,
    retry_notes,
)
from kb.pipeline_stages import (
    crawl_channels,
    _run_crawl,
    _run_export,
    _run_extract,
    _run_index,
    _run_projects,
    _run_wiki,
)

# 正文回填脚本是**可选外挂**:路径由 KB_REFILL_SCRIPT 指定,仓库里不带它。
# 它要干的事(找 dr-gzh 树里正文为空的 md、重抓原文回填,转载 stub 还要走平台列表
# API 定位原文短链)取决于各人存量是怎么坏的,没法内置成一个通用实现——所以做成
# "配了就用、没配就整段不做",而不是在代码里写死某台机器上的一个路径:后者让每个
# 外部用户每次点「重试失败项」都看到一行指向别人家目录的提示,而这个功能对他们
# 永远不可用。
_REFILL_ENV = "KB_REFILL_SCRIPT"


def refill_script() -> Path | None:
    """配置好的回填脚本路径(绝对路径);没配返回 None。**这个函数永远不抛异常。**

    现读环境变量而不是在导入时定下来:测试要能逐个用例换路径。

    **必须在这里就变成绝对路径**:跑它的时候 `cwd` 是脚本自己所在的目录(脚本按相对
    路径找 vault 和存量 md),相对路径配置下这两件事会打架 ——
    `KB_REFILL_SCRIPT=tools/refill.py` 的存在性检查在当前目录下通过,子进程却带着
    `cwd=tools/` 再去找 `tools/refill.py`,于是打开的是 `tools/tools/refill.py`,
    报一句「回填脚本异常: ... No such file」,而检查明明说文件在
    (2026-10-02 审查实测)。检查和执行用同一个绝对路径,这条缝就没有了。

    **绝对化只做字符串层面的 `abspath`,不做 `resolve()`。** `Path.resolve()` 要顺着
    符号链接走,**链接成环时 Python 3.11 抛 `RuntimeError: Symlink loop`** —— 而这个
    函数在 `retry_failed_plan()` 里被调用,异常从计划生成阶段一路冒到 HTTP 层:面板上
    点「重试失败项」只看到一句「重试未能启动 / Internal Server Error」,**连同一轮里
    本该重试的 index / extract 一起没了**,而这只是一个可选外挂配错了
    (2026-10-02 第 2 轮审查实测,真走 `POST /api/pipeline/retry-failed` 复现 500)。
    路径对不对本来就有 `_run_refill` 里那道 `is_file()` 负责报(坏链接上它返回 False,
    不抛),这里只负责交出一个「检查和执行共用」的绝对路径。`abspath` 是纯字符串运算、
    碰不到文件系统,唯一的例外是相对路径要问一次 cwd。

    **`~` 展开也必须在同一条异常边界里面。** `Path.expanduser()` 对 `~不存在的用户名/…`
    抛 `RuntimeError: Could not determine home directory.`(Python 3.11)—— 写错一个
    用户名、或者从别人的配置里抄一条 `~someone/...` 过来,后果和上面那个坏链接**一模
    一样**:`POST /api/pipeline/retry-failed` 返回 500,面板上只有一句「重试未能启动」,
    index / extract 一起没了(2026-10-02 第 3 轮审查实测)。

    所以这个函数的契约是一句话:**不管配进来的是什么字符串,它只返回路径、永不抛。**
    任何一种解析失败都把原字符串原样交回去(这也是最好的诊断文案 —— 提示里出现的就是
    用户自己配的那一行),照样让 `is_file()` 那道检查去报「指不到脚本文件」。
    """
    raw = os.environ.get(_REFILL_ENV, "").strip()
    if not raw:
        return None
    try:
        return Path(os.path.abspath(Path(raw).expanduser()))
    except (OSError, RuntimeError, ValueError):
        return Path(raw)

# 阶段 detail 里写了失败条数就算失败(状态本身是 ok 也算),与面板按钮同一套判定
_FAIL_DETAIL_RE = re.compile(r"失败 [1-9]|\d+ 个账号失败")


def stage_failed(stage: dict | None) -> bool:
    """这一格阶段结果**算不算失败**。

    **「算失败」的判据只有这一份。** 三处消费:重试的计划(`retry_failed_plan`)、
    几轮合并时谁盖谁(`pipeline_merge._merge_rank`)、当日账本里哪一格还欠着
    (`pipeline_daybook`)。面板按钮那边的同口径在 `kb/static/js/pipe_view.js` 的
    `pipeHasFailure`,改这里要跟着改它 —— 两边不一致的话按钮亮着而计划是空的,
    用户点下去只收到 409。

    `status=ok` 但 detail 里写着「失败 3 个」也算:逐项失败的那一档整个阶段是绿的。
    """
    stage = stage or {}
    return (stage.get("status") == "error"
            or bool(_FAIL_DETAIL_RE.search(stage.get("detail") or "")))


def _run_refill() -> None:
    """子进程跑配置好的回填脚本:找 dr-gzh 树里正文为空的 md,重抓原文回填
    (转载 stub 会走平台列表 API 定位原文短链)。占用 export 卡片展示进度。

    脚本按约定逐行输出 `OK <相对路径>` / `FAIL <说明>`,没有空正文时打印
    "没有找到空正文文件"。回填成功的文档重置 extracted_at:防御历史上空正文却
    仅凭标题"提取成功"的假成功,让它们跟着这次的 extract 重提。"""
    script = refill_script()
    if script is None:   # 正常形态:没配这个外挂,这一步不存在
        _stage("export", "skipped", f"未配置正文回填脚本({_REFILL_ENV}),跳过回填")
        return
    # 配了却指不到一个文件——这是配置错了,得说出来。`is_file()` 只吞「不存在 / 不是
    # 目录 / 链接成环」那几个 errno,**名字太长(ENAMETOOLONG)它是照抛的** —— 而这里
    # 抛出去就把 `_run_retry_failed` 的循环打断,后面的 index / extract 一个都不跑
    # (POST 已经返回 200 了,面板上只看到回填这张卡片停在 running)。坏路径的处置
    # 只有一种:说清楚、跳过。
    try:
        usable = script.is_file()
    except (OSError, ValueError):
        usable = False
    if not usable:
        _stage("export", "skipped", f"{_REFILL_ENV} 指不到脚本文件:{script}")
        return
    _stage("export", "running", "回填空正文(重抓原文)")
    try:
        proc = subprocess.run(
            ["python3", str(script)],
            cwd=script.parent, capture_output=True, text=True,
            timeout=30 * 60,
        )
    except subprocess.TimeoutExpired:
        _stage("export", "error", "回填超时")
        return
    out = proc.stdout or ""
    fixed_rel, fail_items = [], []
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("OK"):
            fixed_rel.append(s.split(None, 1)[1])
        elif s.startswith("FAIL"):
            fail_items.append({"title": s[4:].strip()[:80], "reason": "回填失败"})
            _log(f"export: 回填 {s}")
    failed = len(fail_items)
    if not fixed_rel and failed == 0:
        if "没有找到空正文文件" in out:
            _stage("export", "ok", "没有空正文文件,直接重跑提取")
        else:
            _stage("export", "error",
                   f"回填脚本异常: {(proc.stderr or out)[-200:]}")
        return
    if fixed_rel:
        from kb import store
        conn = store.connect()
        try:
            for rel in fixed_rel:
                conn.execute(
                    "UPDATE documents SET extracted_at=NULL, summary='' "
                    "WHERE path=?", (f"source/dr-gzh/{rel}",))
            conn.commit()
        finally:
            conn.close()
    _stage("export", "ok" if failed == 0 else "error",
           f"回填 {len(fixed_rel)} 篇"
           + (f",{failed} 篇回填失败" if failed else ""),
           failures=fail_items)


def unfinished_today() -> dict:
    """今天还欠的那些事,摆成一轮运行的样子。**只在这里取,降级说明丢掉。**

    单独问一句「有没有失败项」的调用方不需要那句降级说明(它没有消息可以带出去);
    真要起跑的那条路走 `daybook.unfinished` 自己拿,它会把那句话跟着返回。
    """
    return daybook.unfinished(last_result())[0]


def owed_now(pending: dict) -> tuple[list[str], list[dict]]:
    """今天还欠、而且**现在真补得到**的那几步,外加抓取这一次的范围。

    **状态端和起跑端读同一份。** 两处各判一遍的后果是页面上一句「今天还有 1 个步骤
    没补上」配一句「这颗按钮没有要补的」:账本记着抓取欠着,而执行端按当日继承、
    停用、删除把目标过滤空了(`sync.still_failing`),刷新也不会变 —— 一个点不动、
    也消不掉的死局(审查 R2 实测三种形态:当天成功过之后又失败、随后停用、随后删除)。

    **只摘得掉抓取那一步。** 别的几步各有自己的可补性,不能因为抓取过滤空了就把整张
    问题卡抹掉(index / extract 真失败时照旧要补)。

    **不改账本,只改这一次派生出来的那一份。** 账本记的是「今天真失败过」,那是事实;
    改写它等于把历史改了,而下一次就再也说不清当时发生过什么。

    **问范围要读那个来源自己的订阅库,所以只在抓取真欠着的时候问**(顺序同
    `start_retry_failed`:只有 index 失败、而订阅库恰好打不开的那一轮,不该被一个
    跟它毫无关系的故障拖住)。读不出来一律**照抛**,由调用方决定降级 —— 状态端兜住
    它、宁可多显示一次(点下去照旧按真实范围跑),起跑端照旧让它说出原因。
    """
    stages = pending.get("stages") or {}
    owed = [name for name in STAGES if stage_failed(stages.get(name))]
    if "crawl" not in owed:
        return owed, []
    scopes = retry_crawl_scopes(pending)
    if scopes:
        return owed, scopes
    # 抓取那几个目标现在一个都不用重抓了(后来成功过、停用、或者删掉了):**它不再
    # 是「今天还欠的一步」** —— 留着它,页面就会说还欠一步而点下去没有可补的。
    return [name for name in owed if name != "crawl"], []


def today_failed() -> list[str]:
    """**今天**还欠着、而且**现在真补得到**的那几步(面板拿它决定按钮露不露脸)。

    **不许只看最近一次运行。** 那颗按钮原来是「最近一轮有没有红」才显示,而这颗
    按钮要补的是今天还欠的那些:09:12 抓 RSS 失败、10:20 一轮公众号全绿收尾之后,
    最近那一轮一个红都没有 —— 按钮当场藏掉,于是 09:12 那条**连点的机会都没有**,
    后端这一侧补得再对也到不了(审查 R1 的同一条路)。

    账本空着时它退回最近一次运行,所以这一份只会**多**显示、不会少显示:多显示的
    那一档点下去照旧按真实计划跑,少显示那一档是用户再也碰不到的红。

    **「还欠着」不等于「补得到」。** 当天成功过之后又失败的那条订阅、随后被停用或
    删掉的那几条,执行端是跳过它们的(`sync.still_failing`),所以这里要和起跑端读
    **同一份**派生(`owed_now`)—— 两处各判一遍就是「说没补完 + 点了又没有可补」那
    个死局(审查 R2)。

    **合成那一步炸了也只退回存量行为,不许把整条状态接口带下去。** 它只是面板上一
    颗按钮的显示判据,而 `/api/pipeline/status` 是面板的命脉 —— 连「管道在不在跑」
    都从它读(`./kb.sh` 杀进程前的那道保护就是),一个只读字段不该让那一问变成 500。
    """
    latest = last_result()
    try:
        pending, _ = daybook.unfinished(latest)
    except Exception:                              # noqa: BLE001 - 见 docstring
        pending = latest
    try:
        return owed_now(pending)[0]
    except Exception:                              # noqa: BLE001 - 见 docstring
        # 范围问不出来(那个来源的订阅库打不开)就退回只按账本答。**宁可多显示
        # 一次** —— 点下去照旧按真实范围跑,而把今天欠的东西从面板上抹掉,用户
        # 再也碰不到它。
        stages = pending.get('stages') or {}
        return [name for name in STAGES if stage_failed(stages.get(name))]


def retry_failed_plan(nothing_to_crawl: bool = False, run: dict | None = None) -> list[str]:
    """按**今天还欠的那些**编排重试链(面板全局"重试失败项"按钮的后端)。

    `nothing_to_crawl`:上一轮失败的那几个目标现在一个都不用重抓了(它们后来都已
    成功同步,见 `retry_crawl_scopes`)。这一档不能把 crawl 排进计划 —— 范围是空的,
    真让它跑,`crawl_channels` 会按"没指定来源"回落到默认采集器,抓的是另一个来源。

    - crawl 有账号失败/出错 → 重抓(增量爬取,便宜)
    - export 有失败/出错、或链里重抓过 → 重导出
    - extract 有"正文空"跳过、且配了回填脚本 → 先回填(refill)
    - export/refill 可能补了数据、或 index 自己出错 → 重建索引
    - extract 出过错、或前面补了新文章 → 重提取
    - projects/wiki 出过错 → 重跑(它们在链尾,不牵连别人)

    「算失败」的判定必须和面板按钮(kb/static/js/pipe.js)保持同一套规则:
    status=error,或 detail 里写了失败条数。两边不一致的话按钮会亮着但这里
    返回空计划,用户点下去只收到 409。
    """
    # **`run` 由调用方传进来,不在这里另读一次。** `start_retry_failed` 的范围和
    # 计划原来各读一次「最近一次」,两次之间别的窗口或调度器恰好跑完一轮的话,计划
    # 来自新那一轮的失败、而导出范围还是旧那一轮的来源 —— 实测里重试去抓了订阅 A,
    # 而导致这个计划的失败发生在订阅 B。缺省自己取是给单独问「有没有失败项」的调
    # 用方用的。
    # **缺省读的是「今天还欠什么」,不是「最近一次运行」**(`pipeline_daybook`):
    # 这颗按钮会跑哪几步只许有一个答案 —— 两处各判一次的话,单独问一句的人看到
    # 「要补 export」,真点下去却按最近那一轮重跑整条链(2026-10-19 10:34 那一次)。
    prev = ((unfinished_today() if run is None else run).get("stages") or {})

    def _detail(name: str) -> str:
        return (prev.get(name) or {}).get("detail") or ""

    def _failed(name: str) -> bool:
        return stage_failed(prev.get(name))

    # projects 的"入库失败"卡在最后的 chunk 入库/编码那步 —— gate/transcribe 的
    # 大模型调用其实已经成功了。而 index 扫的是整个 vault,dr-*-projects 只是它的
    # 子集,交给 index 重建就够,不必再烧一遍额度重跑条目化。
    projects_ingest_only = (_failed("projects")
                            and _detail("projects").startswith("入库失败"))

    plan: list[str] = []
    if _failed("crawl") and not nothing_to_crawl:
        plan.append("crawl")
    if _failed("export") or "crawl" in plan:
        plan.append("export")
    # 没配回填脚本时这一步压根不进计划:让外部用户看一张"本次跳过"的卡片,
    # 比不显示更糟——那个功能对他们不存在。
    # 「正文空」这三个字是和 pipeline_stages._finish_extract_stage 约好的暗号,
    # **只有回填救得了的那一档才写它**。整篇是一张长海报图那种(detail 里写的是
    # 「无正文 N」)绝不能进来:重抓回来还是同一张图,每一轮都在白跑一遍脚本。
    if "正文空" in _detail("extract") and refill_script() is not None:
        plan.append("refill")
    if plan or _failed("index") or projects_ingest_only:
        plan.append("index")
    if _failed("extract") or "index" in plan:
        plan.append("extract")
    if _failed("projects") and not projects_ingest_only:
        plan.append("projects")
    if _failed("wiki"):
        plan.append("wiki")
    if not plan:
        # 上次没记录失败,但可能还有提取积压(如手动往 vault 放过文档)
        from kb import store
        from kb.extract import pending_docs
        conn = store.connect()
        try:
            if pending_docs(conn, limit=1):
                plan = ["extract"]
        except Exception:
            # 积压要跨库问一次订阅的提取开关(见 `kb.extract.pending_docs`),
            # 问不到时**照样把 extract 排进计划**:这条路只是在决定「要不要
            # 跑这一步」,真正的诊断归 `_extract_pass` —— 它会把同一个异常落成
            # 一句红色的阶段文案。在这里吞掉的话用户收到的是 409「上次运行
            # 没有失败项」,而真实情况是库读不到,两者指向完全相反的下一步。
            plan = ["extract"]
        finally:
            conn.close()
    return plan


def _run_retry_failed(plan: list[str], crawl_filter: dict | None = None,
                      export_filter: dict | None = None) -> None:
    """crawl 走收窄过的范围,export 走上一轮**原来**那几个来源。

    两份不是同一个:收窄会把"这一轮没有要重抓的"那个来源整个摘掉(摘不掉的话
    这颗按钮又变成"把全部订阅再抓一遍"),而 export 的失败是它自己那一档 ——
    一个 crawl 成功、export 失败的来源会跟着被摘掉,它那一步就再也没人重试了。
    """
    try:
        runners = {"crawl": lambda: _run_crawl(crawl_filter),
                   "export": lambda: _run_export(export_filter
                                                 if export_filter is not None
                                                 else crawl_filter),
                   "refill": _run_refill, "index": _run_index,
                   "extract": _run_extract, "projects": _run_projects, "wiki": _run_wiki}
        for stage in plan:
            if _state.get("stop_requested"):
                break
            runners[stage]()
    finally:
        _end_run()


# 上一轮失败的那几条后来都已经不用重抓了(后来单独同步过、被删被停,或者**今天
# 前面某轮已经抓成过** —— 当日继承,见 `sync.still_failing`)。两件事都要说:为什么
# 是空的、以及真想再抓一次该走哪个入口 —— 否则用户看到的是一句"上次运行没有失败
# 项",而面板上明明还显示着上一轮的失败。
RETRY_NOTHING = ("上一轮失败的那几个来源，今天都已经成功抓过了（或者后来单独同步过、"
                 "已删除、已停用），这颗按钮没有要补的；想再取一次新内容，请在"
                 "「我的订阅」里单独同步它，或者跑一次完整同步")


def retry_crawl_scopes(prev: dict) -> list[dict]:
    """这一轮重试要抓哪几个来源、各抓哪几个目标([] = 一个都不用抓)。

    继承的是上一轮的**来源**(哪几个 channel、上次点名了哪几个目标),范围则按
    **各来源自己留下的失败名单**逐个收窄(见 `pipeline_segments.note_retry_targets`)。
    每项多一个 `retry` 说明这一项是怎么来的:`targets` = 收窄到了失败的那几个,
    `all` = 这个来源没法收窄,按原范围重跑。面板那行「本次范围」照它说话。

    **不能用一个全局布尔值管所有来源**:大V整批失败 + RSS 一条逐项失败那一轮,
    全局开关会让两个来源都按失败项收窄,而整批失败的大V一份名单都没留下 ——
    收窄成空集,恢复之后点重试它一条也不抓(2026-10-09 第 1 轮审查实测)。
    """
    notes = retry_notes(prev)
    scopes: list[dict] = []
    for scope in crawl_channels(prev.get("crawl_filter")):
        targets = None
        if (note := notes.get(scope["channel"])) is not None:
            try:
                narrower = retry_narrower(active_collector(scope["channel"]))
                targets = narrower.retry_targets(note) if narrower else None
            except Exception:
                # **认名字和问名单两件事一起护住,而且故障只圈在这一个来源里。**
                # 认不出名字(改名/退役)是一种;问名单那一下也会炸 —— 它要读这个
                # 来源自己的订阅库(见 `sync.still_failing`),库打不开就抛。
                # 两种都只能按原范围重跑,**不许抛到 HTTP 层**:那会把同一轮里
                # 本该重试的 index/extract 一起带走(同 `refill_script` 那条
                # "永不抛"的理由),而那两步跟这个来源的订阅库毫无关系。
                targets = None
        if targets is None:
            # 这个来源**没留下名单**(整批失败,那时一条订阅都还没被碰到),或者它
            # 压根报不出名单(公众号的失败是文章级的,`--targets` 是账号级的)——
            # 两种都只能按原范围重跑:收窄成空集就是"一条也不抓",而它恰恰是最该
            # 重抓一遍的那个。
            scopes.append({**scope, "retry": "all"})
        elif targets:
            scopes.append({"channel": scope["channel"], "accounts": list(targets),
                           "retry": "targets"})
        # 名单是空的:这个来源这一轮没有要重抓的,整个来源都不进这一轮 —— 留着它
        # 等于"把它的全部订阅再抓一遍",而那正是这颗按钮的老毛病。
    return scopes


# 一个来源都抓不动时那句话。**拒绝起跑而不是让它原地红一遍**:拒绝不改任何状态,
# 上一轮的结论和失败名单一字不动,下一次重试照旧从它派生;真让它跑完,这一份一模
# 一样的失败就成了「最近一次运行」,而计划和范围都从最近那一轮派生 —— 等于拿一轮
# 白跑换掉了手上那份诊断(2026-10-19 那次 10:34 的重试就是这么把 09:12 那轮的结论
# 顶掉的)。
_BLOCKED_PREFIX = '现在一个来源都抓不动，而这一次只有抓取要补，先把这件事处理掉再重试：'
# 抓不动、但这一轮还有别的正事要做时,抓取那张卡片上写这句话。**不写成失败**
# (`status` 是 skipped):它这一次压根没跑过,而「失败」会被面板和下一次的计划判据
# 当成这一轮新产生的红。该补的那几个来源留在当日账本里,下一次重试照旧派生得出来。
_BLOCKED_SKIPPED = '现在抓不动，这一步留着下次补：'


def crawl_blocked(scopes: list[dict]) -> str:
    """这一轮要重抓的来源**是不是一个都抓不动**;有一个抓得动就返回空串。

    **「抓得动」的判据是采集器自己那份 `status()`**(`online and ready`),这里不
    另写一份 —— 新增一条通道时这个函数一个字都不用改,口径跟面板上那张来源卡片
    也就不会岔开(同 `registry.configuration_error` 那条)。

    **问的是这一次要抓的那几个,不是整条通道。** 范围收窄到几条订阅时就把名单交给
    `status(targets=...)`(契约在 `kb.collectors.SourceCollector.status`)—— 不交的
    后果是**用 B 能抓证明 A 能抓**:上一轮只有 A 失败(它要用的浏览器被占着),而
    同一条来源里 B 填了专用订阅链接、好得很,整条通道于是"就绪",这道闸当场放行,
    A 又失败一次。那一轮白跑还会把手上那份诊断顶掉(见 `_BLOCKED_PREFIX`)。
    `retry: "all"` 那一档没有名单(整批失败,或者来源报不出账号级名单),照旧问整条
    通道 —— 那时要抓的确实是它的全部订阅。

    **问不出来一律放行,范围对不上也一样。** `status()` 要起子进程、读订阅库、碰
    文件系统,它自己炸掉跟「能不能抓」是两件事;而范围里一条启用着的订阅都没匹配上
    (名单里那条刚被删掉或停用)时,它答的是"这一轮没有来源",不是"这几个抓不动"。
    两档都拦住整轮的话,会把同一轮里本该重试的 index / extract 一起带走(同
    `refill_script` 那条「永不抛」的理由)。**漏拦一次的代价只是白抓一轮,错拦一次
    的代价是该补的一步也没补。**

    **有一个抓得动就不拦,而且当场返回** —— 那一轮有正事可做,拦住它等于拿一个
    来源的故障扣下别人的重试;顺带也省掉剩下那几次探测(公众号那一下要起采集器
    CLI,冷缓存时几十秒)。
    """
    reasons: list[str] = []
    labels = channel_labels()
    for scope in scopes:
        channel = scope.get('channel') or ''
        wanted = tuple(scope.get('accounts') or ())
        try:
            st = active_collector(channel).status(targets=wanted)
            if st.online and st.ready:
                return ''
            # 范围给了、却一条启用着的订阅都没对上:它答的是"这一轮没有来源",
            # 答不了"这几个抓不动"。**这一档放行**(理由同上:错拦更贵)。
            if wanted and not st.targets:
                return ''
            why = (st.message or '').strip()
            hint = (st.hint or '').strip()
        except Exception:
            return ''
        reasons.append(f"{labels.get(channel) or channel or '来源'}：{why or '现在抓不动'}"
                       + (f"（{hint}）" if hint else ''))
    return '；'.join(reasons)


# 三档各自说清**为什么这会儿不能重试**,而不是笼统一句「恢复得不完整」——
# 用户要据此决定下一步(三档都是「跑一次完整同步就好了」,但原因不同)。
_GAP_REFUSED = {
    'scope': ('重启时只从历史记录恢复到最新那一轮，这一轮的重试范围没能留档。'
              '跑一次完整同步之后再重试'),
    'unknown': ('重启时有一份留档读不出来，核对不了哪一轮是最新的。'
                '跑一次完整同步之后再重试'),
    'order': ('磁盘上两份留档各记着不同的一轮，先后分不出来。'
              '跑一次完整同步之后再重试'),
    'broken': ('最新那一轮的留档损坏，读不出它的结果和范围，页面显示的是更早一轮。'
               '跑一次完整同步之后再重试'),
    'lost': ('上一轮同步的结果一个字都没能留档，页面显示的是更早一轮、已经过期。'
             '跑一次完整同步之后再重试'),
}


def start_retry_failed() -> tuple[bool, str]:
    """一键重试所有失败项。返回 (started, msg);无失败项或已在跑时 False。"""
    # **整次重试只取一份快照**,下面的计划、范围、导出范围全从它派生(理由在
    # `retry_failed_plan` 的 `run` 那段)。
    prev = last_result()
    # **欠账要在「有没有失败项」之前问。** 恢复得不完整时手上这份快照本来就不该拿来
    # 派生计划:最新那一轮的留档坏到渲染不了时 `last_result()` 给的是空字典,计划于是
    # 也是空的 —— 拒绝理由就成了「上次运行没有失败项」,而真相是「那一轮的留档坏了,
    # 屏幕上这一轮不是最新的」。两句话会让人做出完全不同的下一步。锁内那一道照旧
    # 保留(它读的是拿到锁那一刻的欠账,这一道只是**早一道、把话说准**)。
    gap = restore_gap()
    if gap:
        return False, _GAP_REFUSED.get(gap, _GAP_REFUSED['order'])
    # **计划和范围从「今天还欠什么」派生,不是从「最近一次运行」。** 一天里会跑好
    # 几轮,而那一格只有一个位子:09:12 抓 RSS 失败、10:20 一轮公众号成功收尾,后者
    # 当场把前者顶掉 —— 于是这颗按钮要么说「上次运行没有失败项」,要么按最近那一轮
    # 的范围从头再跑一遍(2026-10-19 10:34 那一次),而 09:12 该补的那条一直没补上。
    # 账本按 (来源, 步骤) 记、只认今天,所以「同一天继承之前的状态」和「不跨天」
    # 是同一份判据的两面(见 `pipeline_daybook`)。
    # **身份核对照旧认 `prev`**:那一问是「这期间又跑完了一轮吗」,答它的只能是
    # 真实的那一轮,合成快照没有可信身份。
    pending, degraded = daybook.unfinished(prev)
    # **「今天还欠什么」和「现在真补得到什么」只派生一次**(`owed_now`),面板上那颗
    # 按钮读的是同一份 —— 两处各判一遍就是「页面说还欠一步、点下去又说没有可补」那个
    # 死局(审查 R2)。
    owed, scopes = owed_now(pending)
    # 上一轮 crawl 确实失败过,可那几个目标现在一个都不用重抓了(后来成功过、停用、
    # 或者删掉了):把 crawl 摘出计划 —— 范围是空的,真让它跑会按「没指定来源」回落
    # 到默认采集器,抓的是另一个来源。
    crawl_resolved = ("crawl" not in owed
                      and stage_failed((pending.get("stages") or {}).get("crawl")))
    plan = retry_failed_plan(nothing_to_crawl=crawl_resolved, run=pending)
    # **只有真要重抓才去问各来源的失败名单**(那一道在 `owed_now` 里,按「抓取欠不
    # 欠着」判)。问名单要读那个来源自己的订阅库,而这颗按钮的射程远不止抓取:上一轮
    # 只有 index 失败、这会儿订阅库又恰好打不开(锁着、权限、路径没挂上)时,无条件
    # 先问一遍等于让一个跟 index 毫无关系的故障把整次重试顶成 HTTP 500 —— 索引和后面
    # 的提取一步都没起跑(2026-10-09 第 2 轮审查实测)。
    # **拦路的东西还拦着就别起跑。** 这道闸要在一切状态改动之前(理由在
    # `_BLOCKED_PREFIX` 那段):拒绝 = 上一轮的结论、失败名单、导出范围一字未动。
    # **它只是早一道,不是互斥**(元规则那条):探测完到真起跑之间那个窗口里拦路
    # 的东西照样能回来,那一档由抓取阶段自己红掉 —— 这道闸换的是「别拿一轮白跑
    # 顶掉手上那份诊断」。
    #
    # **一个来源都抓不动,不等于整条计划没有正事可做。** 同一轮里红掉的 index /
    # extract / Wiki 跟那个浏览器毫无关系,把它们一起扣下来的后果是:用户点一次
    # 收到一句「先把这件事处理掉」,而本来能补的几步一步都没补(审查 R2)。所以
    # 这里只摘掉抓取那一段,剩下的照跑 —— 真的只剩抓取可做时才拒绝(那才是白跑)。
    # **摘掉不等于标成功**:抓取那一步是 skipped、卡片上写着为什么,而该补的那几个
    # 来源留在当日账本里(这一轮没把它们列进计划,`daybook.fold` 就不碰那几格),
    # 下一次重试照旧派生得出来 —— 标成功才会把那几条红就此丢掉(审查 R1 那一档)。
    blocked_note = ''
    if scopes and (blocked := crawl_blocked(scopes)):
        rest = retry_failed_plan(nothing_to_crawl=True, run=pending)
        if not rest:
            return False, _BLOCKED_PREFIX + blocked
        plan, scopes, blocked_note = rest, [], _BLOCKED_SKIPPED + blocked
    # **导出范围从上一轮的导出范围恢复,不是从它的抓取范围派生。** 两者在重试轮
    # 里本来就不一样(crawl 收窄、export 照旧走全部来源),而落盘只存一份收窄后
    # 的抓取范围的话,第二次重试就从那份收窄范围再派生一次导出范围 —— 第一次
    # 重试里仍然导出失败的那个来源就此被摘掉,面板报「导出完成·已保存 0 条」,
    # 它那几篇文章一个都没落盘,再点也不回来(2026-10-09 第 2 轮审查实测)。
    # 回落到 `crawl_filter` 是给升级前那些没有这个字段的旧记录用的。
    export_filter = deepcopy(pending.get("export_filter")
                             or pending.get("crawl_filter") or {})
    crawl_filter = deepcopy(pending.get("crawl_filter") or {})
    # 退避是失败自己留下的(最短 1800 秒,见 `sync._save_failure`),手动重试必须
    # 跳过它 —— 不跳的话失败后半小时内点这颗按钮,唯一该抓的那条被记成
    # `deferred` 跳过:该抓的不抓,正好和上面那个"不该抓的全抓"凑成一对。
    crawl_filter["force"] = True
    if scopes:
        crawl_filter["channels"] = scopes
        crawl_filter["channel"] = scopes[0]["channel"]
    if not plan:
        return False, RETRY_NOTHING if crawl_resolved else "上次运行没有失败项,也没有提取积压"
    with _lock:
        if _state["running"]:
            return False, "管道已在运行中"
        # **取快照到现在这段时间里,别的窗口或调度器可能又跑完了一轮。** 那样上面
        # 派生出来的计划、抓取范围和导出范围全是**上一轮**的,拿它起跑等于按一份跟
        # 当前失败毫无关系的范围去抓。身份变了一律拒,让用户再点一次(那一下读到的
        # 就是新快照)。
        # **手上这一份不是「可信的最新且完整」时,一律拒绝起跑。** 三档各自的
        # 后果都是**静默做错事**:范围不全会让导出把失败那个来源摘掉(面板报
        # 「导出完成 · 已保存 0 条」,而它那几篇一个都没落盘,见 `retry-scope.md`);
        # 核对不了或先后分不出时,拿到的可能是**上一轮**那份范围 —— 实测里导出
        # 阶段真去抓了上一轮那个来源,而失败发生在另一个来源上。
        # **「证明不了最新 / 恢复不了范围」要说出来并拦住,不许默认旧那一份可信。**
        gap = restore_gap()
        if gap:
            return False, _GAP_REFUSED.get(gap, _GAP_REFUSED['order'])
        # **核对无条件做,而且判据是 `same_round`**(「这两份是不是同一轮」)——
        # 不是比展示版本。那个版本保证非空、**不保证一轮唯一**:两条都没有身份而
        # 收尾时间又相同的旧记录版本就会撞,核对于是放行,而手上这份其实已经不是
        # 最新那一轮了。**这一处要的是 `same_round` 而不是 `same_event`** ——
        # 它问的是「这期间状态变没变」,而两边同源时(没跑新的一轮)必须判成真,
        # 那只能靠内容那一级;真又跑完一轮的话,那一轮带着当前世代的身份,身份那一级
        # 当场判出不等。**「谁是最新」那个问题不在这里问**(那边过 `same_event`)。
        # 原来是「两边都得有 run_id 才
        # 比、拿不准一律放行」—— 为的是别把重启后那一档和升级前的老记录误拒,可那
        # 两档恰好也是**并发窗口**:实测里拿一份没有身份的旧快照、期间另一轮刚成功
        # 结束,这道核对整条跳过,上一轮失败的那几个订阅又被抓了一遍,而失败其实
        # 发生在另一个来源上。兼容旧记录是降级比对的事,不是放掉不变量的理由。
        # **另一边走 `_latest_locked()` 而不是直读 `_state`** —— 跟 `prev` 同一份
        # 口径(`last_result`)。直读内存的话「恢复还没成功」那一档上两边会岔开:
        # `prev` 是磁盘上真实那一轮,内存却还是空的,于是每一次重试都被误拒。
        if not same_round(prev, _latest_locked()):
            return False, "这期间又跑完了一轮，请重新点一次「重试失败项」"
        # card 标记这一轮会碰哪些阶段:按 STAGES 生成,STAGES 加新阶段也不会漏 key。
        # refill 不是独立卡片,占用 export 那张展示进度。
        card = {s: s in plan for s in STAGES}
        card["export"] = card["export"] or "refill" in plan
        detail = {s: "" if card[s] else "本次无需重试" for s in STAGES}
        if crawl_resolved:
            detail["crawl"] = RETRY_NOTHING
        elif blocked_note:
            detail["crawl"] = blocked_note
        # 这条也是一次"新的运行":发个新身份,别让它继承上一轮的 id
        # (日报只停自己那一轮,靠的就是这个 id 不复用)。
        # **先占住持久化边界,占不住就什么都不改**(口径和理由在
        # `pipeline_state.mark_open`)—— 重试轮的终态同样只在收尾时写,它一样会整轮
        # 失证,而失证之后这颗按钮下一次按的就是**上一轮**的范围。
        begun = {"run_id": new_run_id(), "seq_gen": _SEQ_GEN,
                 "started_at": _now(), "run_kind": "retry"}
        planned = [s for s in STAGES if card[s]]
        # **标记那一笔只带这一轮运行自己的身份**(和普通起跑同一处对称:第 4 轮往里
        # 塞过抓取身份,第 5 轮审查证伪了那条,理由在 `pipeline_open.mark`)。
        err = mark_open(begun)
        if err:
            return False, err
        _state.update({
            **begun,
            "running": True, "current_stage": "", "log": [],
            "stop_requested": False, "stopped": False,
            "finished_at": "",
            "crawl_filter": crawl_filter,
            # 导出范围单独存一份,下一次重试从它恢复(见上面那段)
            "export_filter": export_filter,
            # 重试是独立的一轮,不属于任何业务批次(见 `_state` 里那一格的说明)。
            # `run_kind` 不在这里写:它已经在上面那个 `begun` 里(`mark_open` 要它)。
            "batch": "",
            "planned_stages": planned,
            "stages": {s: {"status": "pending" if card[s] else "skipped",
                           "detail": detail[s]}
                       for s in STAGES},
        })
    # 账本降级、抓取被摘掉这两档**都要跟着起跑消息一起说出去**:一句
    # 「index → extract」看不出今天还有没补上的东西(见 `daybook.unfinished`)。
    # **而且要进运行日志**:起跑那句话是一次性的(面板上那条 toast 一会儿就没了),
    # 而「今天早些时候那几笔这次补不到」是事后回头查才想得起来的事 —— 日志跟着
    # 这一轮落盘、也进历史记录,那才是它该待的地方(审查 N1)。
    notes = [n for n in (blocked_note, degraded) if n]
    line = " → ".join(plan) + (f"（{'；'.join(notes)}）" if notes else "")
    _log("重试失败项: " + line)
    threading.Thread(target=_run_retry_failed,
                     args=(plan, crawl_filter, export_filter), daemon=True).start()
    return True, line
