# -*- coding: utf-8 -*-
"""业务批次:一轮日报 = N+1 次管道运行,而「最近一次运行」只有一个位子。

从 `pipeline_state` 里分出来的:那边是**一轮运行自己的状态、收尾和两份落盘**,
这边是**把几轮归成一个业务结果**(内存权威、批次账本、发布资格)。分家的直接原因
是 `pipeline_state` 顶着单文件 700 行上限(CLAUDE.md 那条),但这条缝本来就在:
那边答"这一轮怎么样了",这边答"这一批算哪一份、有没有资格当最新"。

**依赖方向只有一个:本模块 → `pipeline_state` / `pipeline_merge`。** 那边只在
`_end_run` / `_latest_locked` / `_run_record` 这几处延迟 import 反过来的方向(同
`pipeline_merge` 那条),所以这边可以在模块级 import 它们。公开的名字
(`open_batch` / `close_batch` / `PublishRefused`)**直接从本模块取**,不在
`pipeline_state` 那边再导出一次 —— 再导出会把"模块级互相 import"重新引回来,而那种
环炸不炸取决于谁先被 import。`kb.pipeline` 照旧在它的 `__all__` 里转出去,所以日报
那边的 `pl.open_batch()` 一个字不用改。

**硬约束(含两份落盘必须是同一个业务结果的两个投影)、事故形态、回归清单都在
`docs/decisions/sync-schedule.md` 的第一节** —— 动这个文件之前先读那一份:这边写错
多半是静默把一整批的失败显示成成功,重试救不回来。

**锁还是那一把。** 收尾那一步(`pipeline_state._end_run`)进 `_lock` 之后才调
`_publish_in_batch`,"写哪一份、内存权威换成谁"仍然和这一轮的终态在同一个临界区里
—— 搬动职责不许换锁,也不许在这边另开一把(`open_batch` / `close_batch` 自己取的
是同一个 `ps._lock`)。
"""
from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy

from kb import pipeline_state as ps
from kb.pipeline_merge import _merge_runs


# **「最新那一轮」的权威在内存里,磁盘只是重启之后的后路。** 这件事的主口径在
# `last_result()` / `_latest_locked()`(认 `_state`),这里只补批次那一档:一轮日报的
# 几次管道运行合并出来的那份汇总**不是任何一个 `_state`**,而它才是这一刻的最新。
# 落盘会失败(满盘、权限、目录没了),而「写成不成功」答不了「本进程这一批有没有出过
# 那条失败」—— 所以汇总照旧当权威,与写成不成功无关。
_aggregate: dict | None = None
# 它是**某一个**状态文件的汇总。`_STATE_FILE` 被改指向别处(切数据目录、测试打桩)
# 时它说的不是同一个库,一律作废,改认 `_state` / 磁盘那一份。
_aggregate_file = None
# 这一份权威**实际合并了哪几轮**。**「权威位上有东西」证明不了「它覆盖了整批」** ——
# 单轮写失败时躺在这儿的是那一轮自己的快照(见 `_save_own`),它一轮都没合并。谁被它
# 合并进去了只有发布那一刻知道,所以在那一刻一并记下,不让下游去猜。
_covered: frozenset[str] = frozenset()


def _remember_aggregate(payload: dict | None, covers: Iterable[str] = ()) -> None:
    """记下(或清掉)这一批的汇总,连同**它实际合并了哪几轮**(`covers`)。

    **独立的一轮收尾时要清** —— 不清的话它比 `_state` 旧却还当着权威,面板和重试会
    一起退回到那一批的结论。

    `covers` 缺省是空集,而**空集那一边是安全方向**:历史那边据此顶掉行,少顶一行
    只是历史里多留一条旧记录,多顶一行是把别人唯一的副本删掉(事故形态写在
    `pipeline_state._save_history`)。所以新的赋值点忘了传它只会少删,不会丢数据。
    """
    global _aggregate, _aggregate_file, _covered
    _aggregate = deepcopy(payload) if payload else None
    _aggregate_file = ps._STATE_FILE
    _covered = frozenset(covers) if payload else frozenset()


def _batch_round_ids() -> set[str]:
    """这一批里**已经各自落过盘**的那几轮的身份(含正在收尾这一轮)。

    两处用:判这一批有没有资格发布(`_publish_right`),以及汇总发布出去时交代
    「这份记录包含了谁」(`_remember_aggregate` 的 `covers`)。**口径只有这一份。**
    """
    return {r.get("run_id") for r in _batch_runs if r.get("run_id")}


def _batch_aggregate() -> dict | None:
    return _aggregate if _aggregate_file == ps._STATE_FILE else None


def _aggregate_covers() -> frozenset[str]:
    """内存里那份权威**实际合并了哪几轮**。空集 = 它只代表自己那一轮。

    历史那边据此顶掉那几轮各自留下的行(`pipeline_state._save_history`)。**只有
    真的合并过的那几轮算**:独立的一轮、以及发布被拒之后写下的那份局部结果都没合并
    过任何人,拿「权威位非空」当「覆盖了整批」的证明,前几轮那几条历史就被删掉了,
    而那是它们唯一的副本。`_STATE_FILE` 指向别处时同 `_batch_aggregate`,一律作废。
    """
    return _covered if _aggregate_file == ps._STATE_FILE else frozenset()


# 本进程**上一轮**收尾的身份。在 `_end_run` 的收尾处记,所以 `_publish_in_batch`
# 读到的是我们这一轮之前那一轮 —— 批次资格判据要的正是它(见 `_publish_right`)。
_last_settled = ""
_last_settled_file = None


def _remember_settled(run_id: str) -> None:
    global _last_settled, _last_settled_file
    _last_settled, _last_settled_file = run_id, ps._STATE_FILE


def latest_run_id() -> str:
    """本进程上一轮收尾的身份。空串 = 这个进程还没见过任何一轮收尾(或者换库了)。"""
    return _last_settled if _last_settled_file == ps._STATE_FILE else ""


def _with_persist_issue(payload: dict, issue: str) -> dict:
    """把"这份结果没写进磁盘"记进它自己:一格结构化字段 + 一行日志。

    只写日志的话前端只能让人展开日志去找,面板上那句收尾文案仍是一句干净的
    「同步完成」—— 看不出这一轮是降级完成的(重启就退回旧那份)。`persist_issue`
    这一格供面板直接显示(`pipe_view.js`),日志那行留着时序。
    """
    payload = {**payload, "persist_issue": issue,
               "log": [*(payload.get("log") or []), ps._stamp(issue)][-80:]}
    ps._log(issue)
    return payload


# ── 业务批次:一轮日报 = N+1 次管道运行,而「最近一次运行」只有一个位子 ────────
# **子轮一收尾就把整批的汇总写进去**,不是先写它自己、再等日报那边稍后补回。
# 先写自己的后果:这一刻盘上那份只有刚收尾的这一轮(入库段那一轮 crawl 是
# 「本次不执行」),前几轮的抓取失败只活在进程内 —— 进程在这中间退出(重启、
# OOM、被杀),重启后同步中心只看到「同步已完成」,既没有那条 RSS 抓取失败,
# 也没有「重试失败项」。那个窗口有多窄都不行:它正是"写了一份会删掉前序失败的
# 局部结果,然后指望还活着的调用方稍后修好"。
# 账本待在**结果存储这一层**,由 `_end_run` 喂:日报那边看不到自己那一轮收尾
# (被下一轮挤出记录)的那一档也照样进账。
_batch_id = ""              # 开着的批次身份,空串 = 没有批次
_batch_runs: list[dict] = []        # 这一批里已经收尾的子轮终态,按时间顺序
_batch_issue = ""           # 这一批**当前**没能发布出去的原因,空串 = 一切正常
# 这一批自己的事件(落盘失败/恢复),跟子轮的日志分开存。**必须单独留一份**:
# 下一轮的合并是从子轮账本重新拼日志的,而子轮快照是在落盘之前拍的 —— 只把那行
# 字追加到当轮合并结果里的话,后面一轮发布成功之后这条历史就没了,而面板上写着
# "原因留在运行日志里"(第 6 轮实测:最终日志里找不到那次失败)。
_batch_notes: list[str] = []
# 这一批是谁起的(`open_batch(kind=...)`),合并时写进 `run_kind`。空串 = 一次普通
# 同步。**只在 `open_batch` 里设,`close_batch` 里清** —— 留着的话下一批会继承上
# 一批的身份,而那正是「分不出是谁起的」换一个方向犯一遍。
_batch_kind = ""


def _remember_batch_note(note: str) -> None:
    """把一条保存事件记进**批次账本**(当前有批次才记)。

    **两个写出口都要记,不按"走的是普通保存还是汇总保存"分裂。** 这一批的头一轮
    走的是 `_save_last_run`:它那行字只进了当轮的 payload,而后面几轮的合并是从
    子轮账本重新拼日志的(子轮快照在落盘**之前**拍),于是首轮那次保存失败在后
    一轮写成功之后就没了 —— 第 1 轮实测注入首轮写失败,最终日志里找不到它,而
    决策文档和回归都写着"原因留在运行日志里"。
    """
    if _batch_id and ps._state.get("batch") == _batch_id:
        _batch_notes.append(ps._stamp(note))


_batch_seq = 0


def batch_note(note: str, batch: str) -> bool:
    """往**这一批**的日志里记一行人话,**并且立刻发布一次**。

    给的是「这一批里发生了、但不属于任何一个阶段」的事:日报没出来、出报用的模型
    挂了、库整理那一段被跳过。**它必须进批次账本而不是某一轮的日志** —— 后面几轮
    的合并是从子轮账本重新拼日志的,只追加到当轮合并结果里的话,下一轮发布成功之后
    这行字就没了(理由和 `_remember_batch_note` 那条一样)。

    **身份是调用方自己手里那个 `batch`,不是全局管道当前那一批。** 两者答的是两个
    不同的问题,所以是两份判据而不是「口径分裂」:`_remember_batch_note` 答「**正在
    收尾的这一轮**属不属于这一批」(它在 `_end_run` 里跑,问的只能是那一轮),这里
    答「**这条事件**属不属于这一批」—— 而记事件的人手里就有批次身份。拿前者来判的
    后果:出报期间用户自己点了一轮同步,`ps._state['batch']` 换成了别人的,于是日报
    「生成失败: Claude 503」那行字被静默丢掉(审查 R6)。

    **记完就发布,不等下一个子轮捎带,而且两份持久化副本一起收。** 出报是这一批的
    **最后一件事**:库整理被关掉、设置读不出、用户按了停止、管道被占住起不来 ——
    这几档后面压根没有子轮会收尾,而 `close_batch` 紧接着就把 `_batch_notes` 清了,
    于是内存里那份汇总和磁盘上那份都还是出报**之前**的样子,同步中心看不到这次失败
    (审查 R6)。只发布「最近一次运行」那一份也不够:历史副本是另一个子轮在
    `_end_run` 里顺手写的,**这条路后面没有子轮**,于是翻历史那张清单只看得到
    「管道结束」,日报到底出没出来一个字不说;下一次普通同步接替「最近一次运行」
    之后,这条结论就两份副本都没有了(审查 R3)。所以这里传 `archive=True`。
    发布成没成不改返回值:记上了就是记上了,没发布出去的原因进 `_batch_issue`,
    由 `close_batch` 的调用方说出来。

    返回值说的是「记上了没有」,调用方据此决定要不要另找地方说(日报自己那份
    `daily_last_run.json` 照旧写,这条只是让同步中心也看得见)。**永不抛**:它在
    `_run` 的 `finally` 里跑,抛出去会挡住收尾(见 `daily_library.library_tail`)。
    """
    with ps._lock:
        if not batch or batch != _batch_id:
            return False
        _batch_notes.append(ps._stamp(note))
        # 一轮子轮都还没收尾(全被占住挡掉了)时没有可发布的汇总 —— 合并要的就是
        # 那几轮的终态。这行字留在账本里,真有子轮收尾时跟着一起出去。
        if _batch_runs:
            _publish_merged(archive=True)
        return True


def open_batch(kind: str = "") -> str:
    """开一个业务批次,返回它的身份 —— 交给 `start_pipeline(batch=...)`。

    `kind` 是这一批的身份,原样写进合并结果的 `run_kind`(见 `_merge_runs`):
    日报那一轮传 `"daily"`,面板的历史清单据此写「日报」而不是「同步」。

    **和 `close_batch()` 成对**,调用方放在 try/finally 里:不关的话下一轮管道
    运行会被算进一个早就结束的批次。

    **身份跨进程唯一**(带上 `ps._RUN_EPOCH`,口径同 `new_run_id`):它现在还是
    当日抓取账本里「一次业务抓取」的键(`kb.fetch_rounds`),而那份账本在盘上跨
    重启留着 —— 只用进程内自增的话,重启后发出的 `batch-1` 会撞上重启前那一轮,
    于是今天第二次抓取被并进第一次,面板上永远写着「第 1 次」。
    """
    global _batch_id, _batch_runs, _batch_issue, _batch_notes, _batch_seq
    global _batch_kind
    with ps._lock:
        _batch_seq += 1
        _batch_id, _batch_runs = f"batch-{ps._RUN_EPOCH}-{_batch_seq}", []
        _batch_issue, _batch_notes, _batch_kind = "", [], kind
        return _batch_id


def close_batch() -> str:
    """关掉批次,返回它没能发布出去的原因(空串 = 发布正常)。

    盘上那份已经在最后一个子轮收尾时写全了,这里不再写 —— 所以**提早返回、
    甚至中途崩掉都不会少东西**。
    """
    global _batch_id, _batch_runs, _batch_issue, _batch_notes, _batch_kind
    with ps._lock:
        issue, _batch_id, _batch_runs = _batch_issue, "", []
        _batch_issue, _batch_notes, _batch_kind = "", [], ""
        return issue


def round_key() -> tuple[str, str]:
    """这一刻属于哪一次**业务抓取**(当日抓取账本的键),外加**它的哪个子轮**。

    有批次身份就用它 —— 一轮日报、一次勾了三个来源的同步都是 N 次管道运行、
    **一次**抓取。没有批次的独立运行(单来源同步、重试失败项)用自己的 run id。
    两种身份都跨进程唯一,理由分别在 `open_batch` 和 `pipeline_state.new_run_id`。

    子轮身份一律是**这次运行自己的** `run_id`:一次业务抓取的物化由 N 个子轮各跑
    一段,「交完了」要按**参与的子轮集合**算 —— 第一个子轮交完证明不了后面那几个
    也交完(事故形态在 `fetch_rounds.note_end`)。**两样身份在同一处算出来**:
    分开两处取的话迟早有一处把"批次有没有开"判反,于是整批的凭证记到某个子轮
    头上,或者反过来每个来源各算一次抓取。

    **「在跑」要由 `running` 证明,不能只看 `batch`/`run_id` 还在不在。** 那两个字段是
    收尾时**故意留下**的(晚一步来取结果的人还得按 id 认领自己那一轮,见
    `pipeline_state._end_run`),所以空闲时它们躺着的是上一轮的身份 —— 管道外直接入库
    的那一笔会被盖上一个跟它毫无关系的批次号,而 `put_items` 的 `DO NOTHING` 和
    `article_fetch_rounds` 的 `INSERT OR IGNORE` 都是**首次说了算**:盖错了救不回来,
    tooltip 从此把它算成「这一次新抓到的」。拿不准就留「说不清」(空串),它落进回执的
    `unsure` 那一档,不冒充任何一次抓取。置位是安全的那一边:`running` 和 `batch` 在
    同一把锁里一起写下、工作线程才启动(`pipeline.start_pipeline`、`pipeline_retry`),
    收尾只在 `_end_run` 里放下它,所以它严格包着全部阶段。

    (第 4 轮一度把这条算法单独抽成 `round_key_of`,供读开工标记的那一侧还原身份;
    第 5 轮审查证伪了「标记能当参与证据」那条,那个消费方跟着没了,算法折回这一处。)
    """
    if not ps._state.get("running"):
        return ("", "")
    batch, run_id = ps._state.get("batch"), ps._state.get("run_id")
    return (str(batch or run_id or ""), str(run_id or ""))


def _save_own() -> None:
    """只写**这一轮自己**那份:独立的一轮,以及一批的头一轮(那时它自己就是整批)。

    **每条收尾分支都要重设内存权威,一条都不许跳过。** 上一批那份汇总还挂在权威位
    上的话,新一批的头一轮收尾之后面板显示的仍是上一批的结论、「重试失败项」也跟着
    去抓上一批的来源 —— 而这一刻的最新明明就是 `_state` 自己(见
    `_remember_aggregate` / `_latest_locked`)。

    写失败**原样抛给 `_end_run`**(它分别诊断两份留档、据此决定要不要清开工标记,
    所以这里不许吞)。抛之前把「这一轮没写进磁盘」记进内存那份权威:面板上那句收尾
    文案要看得出这一轮是降级完成的(`persist_issue` 一格,见 `_with_persist_issue`),
    不然只剩日志里一行字、而标题仍是一句干净的「同步完成」。**记下的是这一轮自己那份
    快照,它一轮都没合并,所以 `covers` 留空** —— 这一档正是「权威位非空」证明不了
    「覆盖了整批」的那一档(见 `_aggregate_covers`)。
    **要不要记进批次账本不在这里判**:那是「这一轮属不属于当前这一批」,判据只有
    `_remember_batch_note` 那一份(头一轮要记 —— 后面几轮的合并是从子轮账本重新拼
    日志的,而子轮快照在落盘**之前**拍;批次外那一轮不记 —— 它的落盘事件不该被念
    进那一批的日志)。在调用处再判一遍的话,两处迟早在某一档上岔开。
    """
    _remember_aggregate(None)
    try:
        ps._save_last_run()
    except Exception as e:                         # noqa: BLE001 - 原样抛给 _end_run
        note = (f'这一轮的结果没能写进磁盘({e});本进程内照常可查,'
                '重启后会退回上一次写成功的那一份')
        _remember_batch_note(note)
        _remember_aggregate(_with_persist_issue(ps.run_payload(), note))
        raise


def _publish_in_batch() -> None:
    """子轮收尾时写**状态文件**那一份。只在 `_end_run` 里调(已持 `_lock`)。

    不属于任何批次、或者这一批的头一轮:写自己那份就是全的(走 `_save_own`)。往后
    每一轮都把**整批**合并着写,盘上那份于是始终等于"已经收尾的这几轮合起来"。

    **写失败一律抛出去**,由 `_end_run` 那段统一诊断(它分别尝试两份留档、据此决定
    要不要清开工标记)。抛之前要做的那两件事在 `_publish_merged` 里(把原因记进批次
    账本、**整批那份照旧当权威**),**不许退回去写当前子轮那一份局部结果**。
    """
    if not _batch_id or ps._state.get("batch") != _batch_id:
        _save_own()
        return
    _batch_runs.append(ps.run_payload())
    if len(_batch_runs) < 2:
        _save_own()
        return
    # **资格冲突和 I/O 故障是两件完全不同的事,分开判。** 混成一种异常的后果是
    # I/O 那一档被按"别人更新了"处理:退回去写当前子轮那一份局部成功,把整批的
    # 失败连同重试名单一起抹掉 —— 入库那一轮的 crawl 是「未选择」,前面那条抓取
    # 失败于是内存里和磁盘上一起没了(第 5 轮实测:一次临时写失败就够)。
    # `archive=False`:历史那一份紧接着由 `_end_run` 自己那一笔写(它两份分别尝试、
    # 分别诊断,见那边的注释)。这里再写一次只是多一次全量重写历史文件。
    err = _publish_merged(archive=False)
    if isinstance(err, PublishRefused):
        # 没资格:期间有更新的一轮独立运行收尾(用户自己点了同步)。退回只写自己
        # 那一份 —— **我们这一轮确实是此刻最新的**,而把前几轮一起合进去等于拿一批
        # 比那一轮更早的结果去盖掉它。原因留给 `close_batch` 的调用方说出来。
        _save_own()
        return
    if err is not None:
        raise err                                  # 写盘炸了:原样抛给 `_end_run`


def _publish_merged(*, archive: bool) -> Exception | None:
    """把**整批**(已收尾的子轮 + 批次事件)合并着发布一次。已持 `ps._lock`。

    返回 None = 发布成功;返回异常 = 这一次没发布出去(`PublishRefused` = 没资格,
    其它 = 写盘炸了)。原因已经记进 `_batch_issue`,**调用方只决定自己那一步还要不要
    做**:子轮收尾要在没资格时退回去写自己那一份、要把写盘异常抛给 `_end_run`;
    `batch_note` 两样都不做(它没有"自己那一轮"可写)。

    `archive` 回答的是「**历史那一份副本谁来写**」,**故意没有缺省值** —— 新的调用方
    必须自己答一次。子轮收尾传 False(紧接着 `_end_run` 自己那一笔会写);批次事件传
    True,它后面没有子轮了,不自己补一次就只有「最近一次运行」收到这一版(审查 R3)。

    **口径只有这一份**:哪一批有资格发布、合成什么、谁当权威、覆盖了哪几轮。两个
    调用方(子轮收尾、批次事件)在这几件事上分裂一次就等于一边把整批的失败写丢了。
    """
    global _batch_issue
    try:
        _publish_right(_batch_round_ids())
    except PublishRefused as e:
        _batch_issue = e.reason
        return e
    merged = _merge_runs(_batch_runs, _batch_notes, _batch_kind)
    try:
        ps._write_state(merged)
    except Exception as e:                         # noqa: BLE001 - 见 docstring
        # **不退回去写局部结果。** 盘上那份是这一批上一轮的汇总(同样带着前面
        # 那些失败),原样留着就是最好的后路。
        _batch_issue = f'这一批的同步结果没能写进磁盘({e})'
        note = (f'{_batch_issue};本进程内照常可查,'
                '重启后会退回上一次写成功的那一份')
        # **直接追加,不走 `_remember_batch_note`**:这一刻我们正在发布的就是这一批,
        # 它属于谁没有可判的 —— 走那一份判据的话,全局管道已经被别人接替时这行字
        # 被静默丢掉(审查 R6 的同一个形状)。
        _batch_notes.append(ps._stamp(note))
        _remember_aggregate(_with_persist_issue(merged, note),
                            covers=_batch_round_ids())
        # **两份分别尝试、分别诊断**(口径同 `ps._end_run` 的收尾):状态那一份写
        # 不进去,不代表历史那一份也写不进去,而历史是这条结论唯一的另一处落脚点。
        if archive:
            _archive_merged()
        return e
    # 发布成功就解除之前那句警告:原因留在运行日志里,但"当前仍未保存"这句话不能
    # 挂着不放 —— 后来真写进去了,日报却还在说没写进。
    _batch_issue = ""
    _remember_aggregate(merged, covers=_batch_round_ids())
    if archive:
        _archive_merged()
    return None


def _archive_merged() -> None:
    """把刚记下的那一版也写进**历史副本**。已持 `ps._lock`。**永不抛。**

    走的是历史那一份唯一的出口 `ps._save_history()` —— 它读的就是内存里刚记下的那份
    汇总(`_batch_aggregate()`),所以必须排在 `_remember_aggregate` **之后**:排前面
    存进历史的是上一版,这行字照旧不在。按 run id 幂等,重复写只是替换同一条。

    **没资格发布那一档不走这里。** 那时期间已经有更新的一轮收尾了,而历史那张清单是
    倒序的 —— 把这一批补写进去就是插到最前面,于是一批更早的运行看着像是最后跑的
    那一轮。两份副本一起停在上一版、原因进 `_batch_issue`,这才是一致的。

    写不上只少一条历史记录(那份是只读展示),但**要说出来**:原因并进 `_batch_issue`,
    由 `close_batch` 的调用方报给用户。
    """
    global _batch_issue
    try:
        ps._save_history()
    except Exception as e:                         # noqa: BLE001 - 见 docstring
        why = (f'这一批的同步结果没能写进历史({e}),'
               '同步中心那张历史清单上这一轮还是上一次写成功的样子')
        _batch_issue = f'{_batch_issue};{why}' if _batch_issue else why


class PublishRefused(Exception):
    """这一批没能发布出去。`reason` 是人话原因,调用方要把它说出来。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _publish_right(ours: set[str]) -> None:
    """这一批有没有资格替换「最近一次运行」。没资格就抛 `PublishRefused`。

    **只管资格:不合并、不写磁盘;必要时读磁盘判断资格**(手里没有权威身份那
    一档,见函数末尾)。只有当那份「最近一次运行」仍然是这一批里的某一轮时才
    有资格。期间要是另一轮独立运行已经收尾(用户自己点了同步),
    它是**比我们新**的结果,替换掉它等于把一次刚失败的手动同步改写成成功、顺带
    让它的「重试失败项」消失。一把锁只防同时写,答不了"我有没有资格当最新"。
    """
    # **判据是内存里那份权威身份,不是磁盘。** 磁盘写会失败而且是吞掉的:
    # 那一轮手动同步明明已经收尾、结论也在内存里,磁盘上却还是我们这一批的
    # 某一轮 —— 照磁盘判,我们就"有资格"把它刚刚的失败改写成成功。
    latest = latest_run_id()
    if latest:
        if latest not in ours:
            raise PublishRefused(_taken_over(latest))
        return
    # 这个进程还没见过任何一轮收尾(刚重启、这一批是从别处恢复的)。只能问磁盘,
    # 而**读不出来时失败关闭**:一份读不出的状态既可能是「没有更新的运行」也可能
    # 是「有而且更重要」,拿不准就不许替换。
    # 口径跟恢复那边同一份(`_saved_round_strict`):**读不出来和形状不对同一档**。
    # 「可解析但形状不是预期的」当成「没有更新的运行」的话,正好把那一轮放过去了。
    try:
        current = ps._saved_round_strict() or {}
    except Exception as e:                         # noqa: BLE001 - 失败关闭,见上
        raise PublishRefused(
            f'读不出当前的「最近一次运行」({e}),不确定期间有没有更新的'
            '一轮收尾,这一批没有替换它') from e
    if current.get("run_id") and current["run_id"] not in ours:
        raise PublishRefused(_taken_over(current["run_id"]))


def _taken_over(other: str) -> str:
    """"被更新的一轮接替了"那句话。**只说已经发生的事。**

    原来这句写的是"同步中心显示的是那一轮" —— 而紧接着的退路会把当前子轮写
    进去,于是同步中心显示的其实是刚结束的这一轮,提示指着另一轮(第 5 轮实测
    文案不实)。所以只说"没有合并发布",显示的是谁由"最后收尾的那一轮"决定。
    """
    return (f'期间已经有更新的一轮同步收尾({other}),这一轮日报的几段同步结果'
            '没有合并成一份发布;「最近一次运行」显示的是最后收尾的那一轮')
