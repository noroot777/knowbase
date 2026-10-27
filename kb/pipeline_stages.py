# -*- coding: utf-8 -*-
"""管道各阶段的实现:抓取 → 导出 → 索引 → 提取 → 条目化 → wiki。

- 抓取/导出: 委托给来源采集器(kb.collectors),本模块不知道来源是什么
- 索引: 进程内(复用已加载的 embedding 模型)
- 提取: 进程内并发调 LLM,tag 自动回写 obs frontmatter
- 条目化: 子进程跑 scripts/*_projects_export.py,产物回灌 pre_tagged 来源
- wiki: 子进程跑 scripts/wiki/wiki_daily.py

编排(谁先谁后、并发存量提取)在 kb.pipeline。
"""

from __future__ import annotations

from kb.collectors import FetchScope, active_collector, document_failure
from kb.pipeline_batch import round_key
from kb import pipeline_results as results
from kb.pipeline_segments import stage_segment
from kb.pipeline_state import _log, _now, _stage, _state


# ── 采集:抓取 + 导出,两阶段都只跟采集器契约打交道 ────────────────────────────

def _scope_entries(crawl_filter: dict | str | None) -> list[dict]:
    """一份范围里那几段来源,**原样**。两种写法在这里收口,形状的口径只有这一份。

    一次同步可以同时抓几个来源(面板上勾几个就是几个),所以范围是
    `channels: [{"channel": ..., "accounts": [...]}]`;只抓一个来源时仍写成
    老样子的 `channel` + `accounts`,存量的状态文件、定时配置和重试记录
    因此一律不用迁移。

    **`crawl_channels` 在它之上只留 `channel` / `accounts` 两个键,那是有意的**
    —— 那一份是**要递给执行器的真范围**,多一个键就会跟着跑进执行和付费边界
    (见 `kb/feeds/translate.py` 那段「`crawl_filter` 到不了这里」)。要读某一段上
    别的字段(`retry` 那个展示标记、`expect` 那份确认描述)的,从这里读,别去扩
    那一份。
    """
    if isinstance(crawl_filter, str) or crawl_filter is None:
        crawl_filter = {"channel": crawl_filter or ""}
    # 只有一个来源那种写法:**整份就是那一段,原样递出去**(这个函数的名字就是
    # 「原样」)。多出来的键(`force`、日期窗口)没人读:取用这一份的只认
    # channel / accounts / retry。
    #
    # **「原样」顺带保住了单来源那种写法上的 `retry`,而这一截是冗余带、眼下没有
    # 自己的红**(定点改回「重挑 channel/accounts 拼个新字典」,整组回归全绿)——
    # 后端这边拿得到的重试轮范围**一律**是 `channels` 那种写法
    # (`start_retry_failed` 里 `if scopes:` 无条件写上它),而那个标记的唯一产地
    # 是 `retry_crawl_scopes`,经 `scope_payload` 的一律被剥成两个键。前端那一份
    # 同口径的单来源分支**是走得到的**(存量调用方直接递裸 filter 进来,
    # `tests/test_retry_multi_source.py` 钉着)。别改回去重拼字典:两份实现的形状
    # 要对齐,而上游哪天让这种写法带上标记时,这里不会静默把它吞掉。
    return [s for s in (crawl_filter.get("channels") or [crawl_filter])
            if isinstance(s, dict)]


def crawl_channels(crawl_filter: dict | str | None = None) -> list[dict]:
    """把一次同步的来源摊平成 [{"channel", "accounts"}],一个或多个都是这个读法。

    **这一份是执行范围**:两个键,不多一个(理由在 `_scope_entries`)。
    """
    return [{"channel": s.get("channel") or "",
             "accounts": [n for n in (s.get("accounts") or [])]}
            for s in _scope_entries(crawl_filter)]


#: 按来源跑的那两步,各读**自己那一份**范围(口径同 `pipeline_daybook._owners`)。
#: 余下那几步(`_run_index` 扫整个 vault、`_run_extract` 扫全库待提取队列、
#: `_run_projects` / `_run_wiki` 同理)连通道参数都不收,所以它们一格都不加。
_STAGE_FILTERS = (("crawl", "crawl_filter"), ("export", "export_filter"))


def _stage_scopes(run: dict, key: str) -> list[dict]:
    """这一步作用在哪几个来源上,**带上那一段的 `retry`**(面板那行文案要念它)。

    回落到 `crawl_filter` 是给升级前那些没有导出范围的旧记录用的,口径同
    `pipeline_retry.start_retry_failed` / `pipeline_daybook._owners`。
    """
    raw = run.get(key) or run.get("crawl_filter")
    marks = {s.get("channel") or "": s.get("retry") or "" for s in _scope_entries(raw)}
    return [{**scope, "retry": marks.get(scope["channel"], "")}
            for scope in crawl_channels(raw)]


def run_scopes(run: dict | None) -> list[dict]:
    """这一轮**实际作用在哪几个来源上**。既没抓取也没导出时是 `[]`。

    **这是「按来源执行的那几步各自范围的并集」,不是某一步范围的别名。** 抓取和
    导出各有自己的范围(`crawl_filter` / `export_filter`:抓取收窄到失败的那几条、
    导出照旧走整条来源),所以拿抓取那一份当整轮事实会**整个漏掉只导出的那一轮**
    —— 实测「只补导出」那一轮的抓取范围是 `{"force": true}`(一个来源名都没有),
    于是:开着的订阅页在重试成功之后**一直显示翻译失败**(它靠「这一轮带不带本
    来源」决定重读条目,而仅翻译/导出不改订阅行也不改收录总数,下一次轮询照旧
    什么都不重读,只刷新整页才看得到),同步中心写「选择来源后开始一次同步」,
    历史那行写「未记录来源」,订阅的增/改/删写保护整条失效(第 1 轮自由审查 R1
    实页实测)。**也不许写成 `crawl_filter || export_filter`**:只导出那一轮
    `{"force": true}` 已经为真,而抓取 A / 导出 A+B 那一轮又会漏掉 B。

    **判据是这一轮计划里有没有那一步**,不是 filter 里写着谁:`start_pipeline`
    **一律**会往 filter 里盖一个默认采集器名(实测 `wechat_local`)—— 只跑
    index/extract 的那一轮(日报的「入库处理」就是)照着 filter 念,会把公众号判成
    「正在同步」、把订阅写保护错加在它头上,而真正参与这一轮的来源反倒被说成别人
    的任务。**纯全库那几步一个来源都不凭空附加。**

    同一个来源在两步里都出现时,那一格带的 `accounts` / `retry` 取**抓取**那一份
    (它是收窄过的那一份,而面板那行「本次范围」念的正是它);导出只补上抓取没碰到
    的来源。**要问「某一步在这个来源上的范围」的一律自己读那一步的 filter**
    (`_stage_scopes` / `_owners`),别拿这一份当它用。

    `scopes` 这一格是**落盘记录自己写下的那个答案**(`pipeline_persist.run_record`):
    给人翻的那份历史记录**故意不存导出范围**(那一份不许喂给重试派生,见
    `pipeline_state` 的 `scope` 欠账),所以它改为直接存结论。有就念它。

    `planned_stages` 读不到时(存量落盘记录、旧快照)按老样子只摊平抓取范围,
    不许一律判成「不按来源」—— 那会让写保护对所有存量记录静默失效。前端那一份
    同口径在 `kb/static/js/pipe_channels.js` 的 `pipeRunScopes`,**两份的输出形状
    也一样**(channel / accounts / retry);那一份还多认一种「直接递一份裸
    `crawl_filter` 进来」的老调用方,后端这边从来没有那种调用。

    硬约束在 `docs/decisions/retry-scope.md` 第一节(落盘那一侧在
    `pipeline-history.md` 第一节)。
    """
    run = run or {}
    told = run.get("scopes")
    if isinstance(told, list):
        return [{"channel": s.get("channel") or "",
                 "accounts": [n for n in (s.get("accounts") or [])],
                 "retry": s.get("retry") or ""}
                for s in told if isinstance(s, dict)]
    planned = run.get("planned_stages")
    if not isinstance(planned, list):
        return _stage_scopes(run, "crawl_filter")
    merged: list[dict] = []
    for stage, key in _STAGE_FILTERS:
        if stage not in planned:
            continue
        for scope in _stage_scopes(run, key):
            if not any(s["channel"] == scope["channel"] for s in merged):
                merged.append(scope)
    return merged


def _channel_label(collector) -> str:
    return getattr(collector, "channel_label", collector.label)


def _scope_label(scope: dict) -> str:
    """连采集器都没取到那一档,这个来源那一段挂什么名。

    **不许在这里再问一次 `active_collector`** —— 它正是可能炸的那一步;而且打桩
    是按调用次数发实例的(既有回归就这么摆),多问一次会让后面那个来源拿到串位的
    采集器(实测 `StopIteration` + 另一个来源的结论写到了这一段上)。
    """
    return scope.get("channel") or "未知来源"


def _one_channel(stage: str, scope: dict, run, collector) -> None:
    """一个来源跑一遍这个阶段:给它建自己那一格账,然后跑。

    每个来源各记一条**自己的**结论(结果账,见 `kb/pipeline_results.py` 模块头:
    分段那一份只够展示,拿它回答「谁还欠着」会把成功的来源也记成失败)。

    **执行器交回来的那份覆盖范围要原样递进账里**(`results.cover`):请求的范围不是
    执行的范围 —— 退避期内那一轮一次取文都没发,却以绿色收尾,拿请求范围当覆盖范围
    就会把今天早些时候那笔真失败结清(审查 R1,口径在 `kb.collectors.Coverage`)。
    `run` 报不出来(返回 `None`、或者返回的不是 `Coverage`)照旧按计划范围记。

    **采集器由调用方取好传进来,这里不自己取** —— 取它是隔离要包住的那几步之一,
    而且**整条路上只许取一次**:打桩是按调用次数发实例的(既有回归就这么摆),
    多取一次会让后面那个来源拿到串位的采集器(实测 `StopIteration`,而且一个来源
    的结论写到了另一个来源那一段上)。
    """
    with results.channel_run(stage, scope["channel"], collector, scope["accounts"]):
        results.cover(stage, run(collector, scope))


def _per_channel(stage: str, crawl_filter: dict | str | None, run) -> None:
    """让每个选中的来源各跑一遍这个阶段。

    多来源时每个来源的汇报各记一段(stage_segment),否则后一个来源的结论会
    把前一个整个盖掉,面板上只剩最后那个来源的成败。

    **隔离要包住这个来源的准备、执行和汇报,一步都不许露在外面。** 只包执行那
    一句的话,建账时算计划范围要读一次订阅库(`pipeline_results.planned_targets`
    → `FeedCollector.stable_targets`),这次读失败时异常是从 `with` 那一行抛的
    —— 连这个来源自己的 try 都还没进去,整个来源循环当场跳出:勾了订阅源和公众号
    两个来源、订阅库临时读不出来,**公众号一次都不跑**(第 4 轮审查 R1 实测
    `wechat_calls=[]`)。
    """
    scopes = crawl_channels(crawl_filter)
    for scope in scopes:
        # **停止这一道在建账之前**:建了账等于说「这个来源这一步有结论」,而它压根没
        # 起跑(理由在 `pipeline_results.channel_run`)。
        if len(scopes) > 1 and _state.get("stop_requested"):
            break            # 停止是对整次同步说的,别再起下一个来源
        if len(scopes) == 1:
            # 只有一个来源:异常照旧**穿出去**,由 `_run_pipeline` 记成这一步失败
            # (存量行为)。在这里吞掉的话这一步看着像跑完了,后面几步照旧往下跑。
            _one_channel(stage, scope, run, active_collector(scope["channel"]))
            continue
        try:
            collector = active_collector(scope["channel"])
            with stage_segment(stage, _channel_label(collector)):
                try:
                    _one_channel(stage, scope, run, collector)
                except Exception as e:
                    # 勾了好几个来源:这一个炸了不该把剩下的一起带走。记成它自己那一段
                    # 的失败,面板上照样看得见是谁挂的,后面的来源继续跑。
                    _stage(stage, "error", f"{type(e).__name__}: {e}")
        except Exception as e:
            # 准备或汇报自己炸了(取采集器、算覆盖范围要读一次订阅库、分段收尾)。
            # 它还没来得及记下自己那一段,**这里补一段** —— 不补的话这一笔会落成
            # 「整步 error」,把这一轮里别的来源也一起染红(那正是结果账要避免的事)。
            with stage_segment(stage, _scope_label(scope)):
                _stage(stage, "error", f"{type(e).__name__}: {e}")


def source_status(channel: str | None = None) -> dict:
    """当前来源的健康状况,给状态面板用。"""
    collector = active_collector(channel)
    payload = collector.status().as_dict()
    payload["label"] = collector.label
    payload["range_hint"] = getattr(collector, "range_hint", "")
    payload["channel"] = collector.name
    payload["channel_label"] = getattr(collector, "channel_label", collector.label)
    return payload


def source_targets(channel: str | None = None) -> list[str]:
    """来源里可筛选的子集(公众号名等),给面板的筛选下拉用。"""
    from kb.collectors import target_manager

    collector = active_collector(channel)
    manager = target_manager(collector)
    if manager:
        return [t.name for t in manager.list_targets() if t.enabled]
    return list(collector.status().targets)


def source_pending(channel: str | None = None) -> int | None:
    """采集器那边已有、但还没落进 vault 的条数。算不出返回 None。"""
    return active_collector(channel).pending()


def _run_crawl(crawl_filter: dict | None = None) -> None:
    """让采集器把远端内容拉到它本地。

    crawl_filter 为 None 走增量;否则形如
    {"accounts": [...], "since_date": "YYYY-MM-DD", "until_date": "YYYY-MM-DD"}
    的区间补抓,accounts 为空 = 该来源的全部订阅目标。多来源同时抓时
    accounts 记在各自的 channels 项里(见 crawl_channels)。

    **进来就在当日账本上开一轮**(`fetch_rounds.open_round`):「今天第几次抓取」
    由抓取自己登记,不由后面哪个阶段的产出反推 —— 一次什么都没抓到的抓取也是
    一次抓取,而只勾了索引的那种运行不是。开轮还要交两件事:**这次运行后面跑不跑
    物化**(两种运行的账本行长得一样,而一个的 0 条是确定的、另一个是
    「不可确认」),以及**这是整批里的哪个子轮**(`seg`,凭证按子轮集合算,见
    `pipeline_batch.round_key`)。
    """
    from kb import fetch_rounds

    filters = crawl_filter or {}
    planned = _state.get("planned_stages")
    key, seg = round_key()
    # 闸在这里抛(参与证据一处都记不下,见 `fetch_rounds.open_round`)。**抛要照抛**:
    # 后面的导出阶段才是真会往 vault 落文章的那一步,吞掉就等于没拦。拦住的原因由
    # 编排那一层写进阶段卡片(`pipeline._run_pipeline` 的 crawl 分支)。
    fetch_rounds.open_round(
        key, seg=seg,
        materializes="export" in planned if isinstance(planned, list) else True)

    # **人确认那一刻那几个目标各是什么样**,按来源分开拿(`_scope_entries` 那一份
    # 才有这一键:执行范围 `crawl_channels` 照旧只有两个键,理由在它的 docstring)。
    # 这一份要交到执行器手里 —— 只在入口核一次关不掉「确认完、真读到之前被改指」
    # 那个窗口,而执行器手上只有编号时会按编号重读一份新配置当成旧授权来跑
    # (第 1 轮自由审查 R2 实测)。
    expects = {s.get("channel") or "": dict(s.get("expect") or {})
               for s in _scope_entries(crawl_filter)}

    def _fetch(collector, scope: dict):
        """**采集器报的覆盖范围要原样交上去**(见 `_one_channel`)。"""
        _log(f"采集通道: {_channel_label(collector)}")
        return collector.fetch(FetchScope(
            targets=tuple(scope["accounts"]),
            since=filters.get("since_date", ""),
            until=filters.get("until_date", ""),
            force=bool(filters.get("force")),
            expect=tuple(sorted(
                (str(k), str(v)) for k, v in expects.get(scope["channel"], {}).items())),
        ))

    _per_channel("crawl", crawl_filter, _fetch)


def _run_export(crawl_filter: dict | str | None = None) -> None:
    """让采集器把它本地的内容落成 obs md。

    **这一步的产出就是「这一轮抓取抓到了多少」,而且只认采集器自己交回来的那份
    清单**(`materialize()` 的返回值,契约在 `kb.collectors`)。拿旁观者的视角
    冒充生产者的成果错过两版(索引的"头一回进库" / 全 vault 路径差集),实测
    形态在 `docs/decisions/daily.md`。**每个来源各记一次、记在自己的 `finally`
    里**:后一个炸了不该抹掉前面那几个真落下来的文章,而炸掉的那个要留一笔
    「不可确认」—— 它可能已经写了一半。

    **「交完了」是循环走完之后单独记的一行**(`note_end`),不是某个来源的回执:
    空回执只是起点,把收到回执当成交完的话,进程在中途没了就留下一个**确定的
    0 条**(理由和实测形态在 `fetch_rounds.note_end`)。那一行**记在自己这个子轮
    名下** —— 一轮日报里每个来源各跑一次这个阶段,共用一个业务 key。
    """
    from kb import fetch_rounds

    key, seg = round_key()
    # 先落一张空回执:成果从「确定的空集」起步,各来源的回执并进来。一个来源都
    # 没选中时它就是最终结论(确定 0 条,不是不可确认)。
    fetch_rounds.note_new(key, ())
    _per_channel("export", crawl_filter,
                 lambda collector, scope: fetch_rounds.note(key, collector.materialize))
    fetch_rounds.note_end(key, seg)


class _StopWhileWaiting(Exception):
    """等维护锁的时候用户按了停止。"""


def _lock_waiter(stage: str):
    """给一个阶段配一套等维护锁的回调,返回 `(等过没有, on_poll, on_wait)`。

    两个回调分工不同,这是分开的全部理由:出声是低频的(面板日志是个 80 行的
    环,每条提示占一行),而「用户按了停止」得跟轮询同频 —— 两件事挂在同一个
    回调上的话,等锁期间按下的那一下最坏要 `NOTIFY_EVERY` 秒后才被看见
    (2026-10-01 审查实测:停止没有中止等待,最后落的是 error 而不是 stopped)。

    「等过没有」给调用方用:`flock` 一成功就立刻返回,正好落在两次轮询之间按下
    的那一下轮询不到,所以**等过锁**的话拿到手之后还要再认一次。没等过就不认
    —— 按钮承诺的是「当前阶段结束后停止」,不是取消刚开跑的这个阶段。
    """
    waited = {"on": False}

    def on_poll(_sec: float) -> None:
        waited["on"] = True
        if _state.get("stop_requested"):
            raise _StopWhileWaiting()

    def on_wait(sec: float) -> None:
        _stage(stage, "running",
               f"等维护锁:另一个 kb.db 维护任务正在跑(已等 {int(sec)} 秒)")

    return waited, on_poll, on_wait


def _run_index() -> None:
    """扫 vault → 增量 upsert → 收掉已删的 → 补向量。**整趟在维护锁里。**

    锁的原委和取舍都在 `kb.maintenance`:scan 读到的是那一刻磁盘上的内容,而
    删文件和 documents 行是另一个进程的事(存量清理
    `scripts/feed_trim_history.py`)。不互斥的话,一轮在清理之前开扫的索引会拿
    删除**之前**读到的那个 Doc 完成 upsert —— 库里留下一行指向不存在文件的
    documents:搜得到、点开没有原文,而清理那一侧已经收尾、重跑也找不到它
    (2026-10-01 审查实测 `documents=1`、全文检索命中 1)。

    等不到锁就**不做**这一轮:索引漏一轮下一轮自己就补上了(扫描是全量比对),
    而按旧快照写回去的那行假结果要等到下一次索引才会被收掉。
    """
    from kb.maintenance import LockBusy, maintenance_lock

    _stage("index", "running", "扫描 vault")
    waited, on_poll, on_wait = _lock_waiter("index")
    try:
        with maintenance_lock(on_wait=on_wait, on_poll=on_poll):
            if waited["on"] and _state.get("stop_requested"):
                raise _StopWhileWaiting()
            _index_pass()
    except _StopWhileWaiting:
        _stage("index", "stopped", "已请求停止:等维护锁时放弃了这一轮索引")
    except LockBusy as e:
        _stage("index", "error", f"索引没做: {e}")
    except Exception as e:
        _stage("index", "error", f"索引失败: {e}")


def _index_pass() -> None:
    from kb import store
    from kb.chunker import split_chunks
    from kb.extract import parse_pretagged, save_extraction
    from kb.scanner import ScanReport, scan_all

    conn = store.connect()
    try:
        now = _now()
        report = ScanReport()
        new = 0
        for doc in scan_all(report=report):
            if store.observed_document_is_current(conn, doc):
                continue
            store.upsert_document(conn, doc, split_chunks(doc.body), now)
            pre = parse_pretagged(doc)
            if pre is not None:
                save_extraction(conn, doc.path, pre)
            new += 1
            if new % 25 == 0:
                conn.commit()
                _stage("index", "running", f"已索引 {new} 篇")
        removed = store.remove_missing_documents(conn, report)
        conn.commit()
        # **这里不替抓取记账。**「今天抓了多少」是物化那一步的产出,记在
        # `_run_export`;索引看到的"头一回进库"混着上一轮抓回来还没索引的、手工
        # 导入的和重建索引的,而且要等分批提交全过去才知道,中途断电就永久漏记。

        def _report(done: int, total: int) -> None:
            _stage("index", "running",
                   f"编码 {total} 个新 chunk" if done == 0 else f"编码 {done}/{total}")

        # 向量编码也在锁里:`chunk_vec` 是没有外键级联的虚表,清理在编码中途
        # 删掉那些 chunk 的话,后面几批会按已经不存在的 chunk id 插回孤儿向量
        # 行,谁也不会再来收(见 `kb.maintenance`)。
        from kb.embedder import embed_pending_chunks
        n = embed_pending_chunks(conn, _report)
        _stage("index", "error" if report.errors else "ok",
               f"新增/更新 {new},移除 {removed},新向量 {n}{report.warning}")
    finally:
        conn.close()


def _run_extract() -> None:
    """单独跑提取阶段(管道编排里 crawl+extract 同选时走 _extract_pass 的并行路径)。"""
    stats = _new_extract_stats()
    _extract_pass("", stats)
    _finish_extract_stage(stats)


def _new_extract_stats() -> dict:
    # failed/empty 按路径记 set:存量+增量两轮会重试同一篇,计数去重,
    # 面板上的"失败 N"才等于真正有问题的文档数
    # failures: path -> {"title", "reason"},重试成功会移除,只留最终仍失败的
    # stale_paths: 正文在模型调用期间被换掉、这次结果作废的那些(不算失败)
    # no_text_paths: 有内容但一个字都没有(整篇一张图之类)。和 empty_paths 分开,
    # 因为它不进「需处理」明细、不判红、也不该触发回填(见 extract.body_gap)
    # blocked: 队列**压根没读出来**(跨库问订阅的提取开关失败,见
    # `extract.pending_docs`)。它和上面那几项不是一回事:那些数的是「处理过的
    # 文档」,而这一档下一篇都没看到 —— 不单记一档的话,收尾那句
    # `ok+failed+empty+no_text+stale == 0` 会把它当成「没有待提取的文档」,
    # 把阶段刷成绿色(2026-10-11 审查实测:真页面上同时显示红色错误日志和绿色
    # 「同步已完成」「提取知识:已完成」)。
    return {"ok": 0, "promo": 0, "failed_paths": set(), "empty_paths": set(),
            "no_text_paths": set(), "stale_paths": set(), "failures": {},
            "blocked": ""}


def _finish_extract_stage(stats: dict) -> None:
    # **排在所有计数之前**:队列没读出来时那几个计数全是 0,而 0 在下面那句里
    # 的含义是「没有待提取的文档」—— 两种完全相反的情况长得一模一样。
    # 单独一条 failures 明细,面板上展开就能看见原因。
    if stats.get("blocked"):
        _stage("extract", "error", stats["blocked"],
               failures=[{"title": "提取队列", "reason": stats["blocked"]}])
        return
    ok = stats["ok"]
    failed = len(stats["failed_paths"])
    empty = len(stats["empty_paths"])
    no_text = len(stats["no_text_paths"])
    stale = len(stats["stale_paths"])
    if ok + failed + empty + no_text + stale == 0:
        _stage("extract", "ok", "没有待提取的文档")
        return
    detail = f"成功 {ok},失败 {failed}"
    if stats.get("promo"):
        detail += f",软广 {stats['promo']}(已排除出条目化)"
    if empty:
        detail += f",跳过 {empty}(正文空,需回填)"
    if no_text:
        # 措辞里**不许出现「正文空」** —— pipeline_retry 就是按这三个字决定要不要
        # 跑回填脚本的(见 retry_failed_plan),而这一档回填救不了,每次都是白跑。
        detail += f",无正文 {no_text}(仅图片等,已记录不再提示)"
    if stale:
        detail += f",跳过 {stale}(正文已更新,下轮重提)"
    _stage("extract", "ok" if failed == 0 and empty == 0 else "error", detail,
           failures=list(stats["failures"].values()))


# 提取并发数。瓶颈是逐篇几十秒的 LLM 调用,与绿泡泡反爬无关,可以放心并发。
# 2026-08-26 起提取走 kb.llm 的 API 通道(模型见设置面板的「模型分配」),每个 worker 只是一个
# HTTP 请求,不再是 claude CLI(Node)子进程,本机 RAM/CPU 不再是上限;真正的
# 约束变成中转站的并发/速率限制与按 token 计费,故沿用实测稳定的 6 不上调。
EXTRACT_WORKERS = 6


def _extract_one(path: str) -> tuple[dict | None, str, str, str]:
    """工作线程:读模型输入(独立连接,WAL 下并发读安全)→ 调 LLM。不碰共享连接。

    返回 (判定结果, 空档位, 读到的那一版 content_hash, 那组输入的指纹)。正文为空
    时不调 LLM —— 那必然失败,或者产出一份只凭标题编出来的劣质判定。空档位由
    extract.body_gap 给:`empty` 留给「重试失败项」回填,`no_text` 是回填也救不了
    的那一种(内容在图里),交给主线程落成终态。**写库留在主线程**,这里只回答判定。

    **标题、账号、正文、版本号四样都取自 `extract.model_input` 那一个快照** ——
    原来标题和账号是调用方从待提取名单里带进来的、正文在这儿另读一次,两次读
    之间索引可以把这一篇换掉,于是模型收到旧标题配新正文(后果见
    `extract.input_fingerprint`)。
    后两个返回值出自同一个快照,但回答的不是一件事,缺哪个就漏掉哪一种交错:
    - **版本号**("索引里这一篇是哪一版文件")一路带到落终态那一步:判定和写入
      之间另一条连接的索引可能把这一篇换成一版有正文的内容,那时终态必须落空而
      不是盖到新版上(见 extract.mark_no_body)。
    - **指纹**("我读到的那组输入长什么样")一路带到落库那一步:模型调用要几十秒,
      这期间正文可能被整篇换掉(补抓把跳转壳换成全文),那次结果就得作废
      (见 extract.commit_extraction)。
    """
    from kb import store
    from kb.extract import body_gap, extract_with_llm, model_input

    conn = store.connect()
    try:
        snap = model_input(conn, path)
    finally:
        conn.close()
    gap = body_gap(path, snap["body"])
    if gap:
        return None, gap, snap["version"], ""
    return (extract_with_llm(snap["title"], snap["body"],
                             account=snap["account"]), "",
            snap["version"], snap["seen"])


def _extract_pass(label: str, stats: dict) -> None:
    """跑一轮提取,结果累加进 stats。label 用于进度展示("存量"/"增量")。

    LLM 调用在线程池里并发,DB 写全部留在本线程串行,避免多写者竞争。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from kb import store
    from kb.extract import (NO_BODY_NO_TEXT, StaleBody, commit_extraction,
                            mark_no_body, pending_docs)

    conn = store.connect()
    try:
        try:
            todo = pending_docs(conn)
        except Exception as e:
            # 「哪些文档该提取」要跨库问一次 feeds.db 里的订阅开关
            # (见 `kb.extract.pending_docs`)。问不到时既不能当成「没有待提取
            # 的」放过去(那会静默停掉所有 feeds 来源的提取),也不该让异常冒出
            # 这个阶段把状态机留在 running。
            # **记进 stats、让收尾去落阶段**,不在这里自己 `_stage` 一次就
            # return:这个函数的两个调用方(`_run_extract` 单轮、管道的
            # 存量+增量双轮)随后都无条件走 `_finish_extract_stage`,而那边
            # 按「计数全 0」把阶段刷成绿色的「没有待提取的文档」,红的那一句
            # 当场被盖掉。
            stats["blocked"] = f"读不到订阅的提取开关:{e}"
            _stage("extract", "error", stats["blocked"])
            return
        # 读到了 = 上一轮那次失败**已经恢复**,把它清掉。两轮共用同一份 stats,
        # 只在异常时赋值、不在成功时清除的话,存量那轮失败、增量那轮读到并且
        # 真的提取成功之后,收尾仍然优先报那条过时的错误:面板上「1 个步骤需
        # 处理」、要求重试失败项,而日志里增量明明是 1/1
        # (2026-10-11 第 2 轮审查实测,真管道 + 真模型桩复现)。
        # **必须排在下面那句空队列 return 之前** —— 后一轮合法地读到空队列
        # (比如订阅那会儿正好被关掉)同样是恢复,在 return 之后清等于没清。
        stats["blocked"] = ""
        if not todo:
            if label:
                _log(f"extract: {label}无待提取文档")
            return
        _stage("extract", "running", f"{label}提取 0/{len(todo)}")
        done = 0
        with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as pool:
            # 只传 path:标题和账号由工作线程从**同一个快照**里取,不从这份
            # 待提取名单带过去(名单是上面那次查询的快照,会比正文旧)。
            futs = {pool.submit(_extract_one, row["path"]): row
                    for row in todo}
            for fut in as_completed(futs):
                row = futs[fut]
                done += 1
                try:
                    data, gap, version, seen = fut.result()
                    if gap == NO_BODY_NO_TEXT:
                        # 终态:这一版内容里没有字可提。不进 failures、不判红 ——
                        # 它不是「还没做完的事」,是「做不了也不用再问的事」。
                        # 标记跟着 content_hash 走,正文哪天补回来会自己回队列。
                        marked = mark_no_body(conn, row["path"], version)
                        conn.commit()
                        if marked:
                            stats["no_text_paths"].add(row["path"])
                            stats["empty_paths"].discard(row["path"])
                            # 存量轮失败、增量轮落终态:旧失败计数要一起清掉,
                            # 否则收尾按它判红,而「需处理」明细里一条都没有
                            stats["failed_paths"].discard(row["path"])
                            stats["failures"].pop(row["path"], None)
                            _log("extract: 无正文可提取(仅图片等) "
                                 f"{row['title'][:24]}")
                        else:
                            # 判定期间内容换版了:这一轮什么都不记,留在队列里
                            # 等下一轮按新版重新判(见 extract.mark_no_body)
                            _log("extract: 正文在判定期间更新,留待下一轮 "
                                 f"{row['title'][:24]}")
                    elif data is None:
                        stats["empty_paths"].add(row["path"])
                        stats["failures"][row["path"]] = document_failure(row, "正文为空,需回填")
                        _log(f"extract: SKIP 正文为空 {row['title'][:24]}")
                    else:
                        # 校验 → 落库 → 提交 → tag 回写 frontmatter(决策:自动
                        # 回写)整段在「维护锁 → kb.db 写锁」里,这中间没有别人
                        # 能把正文换掉;回写改了文件会顺手刷新 hash,免得下次
                        # index 白白重建这批文档。见 extract.commit_extraction。
                        commit_extraction(conn, row["path"], data, seen)
                        promo = data.get("promo") or {}
                        if promo.get("level") == "hard":
                            stats["promo"] += 1
                            _log(f"extract: 软广 {row['title'][:24]} — "
                                 f"{promo.get('reason', '')[:40]}")
                        stats["ok"] += 1
                        # 存量轮失败、增量轮成功:清掉旧失败记录,计数才是最终状态
                        stats["failed_paths"].discard(row["path"])
                        stats["empty_paths"].discard(row["path"])
                        stats["no_text_paths"].discard(row["path"])
                        stats["stale_paths"].discard(row["path"])
                        stats["failures"].pop(row["path"], None)
                except StaleBody:
                    # 不是故障:正文在模型调用期间被整篇换过(补抓补上了全文),
                    # 这一篇还在待提取集合里,下一轮按新正文重提。记成失败会让
                    # 面板标红,而这里什么都没坏。
                    stats["stale_paths"].add(row["path"])
                    stats["failed_paths"].discard(row["path"])
                    stats["failures"].pop(row["path"], None)
                    _log(f"extract: SKIP 正文已更新 {row['title'][:24]}")
                except Exception as e:
                    stats["failed_paths"].add(row["path"])
                    stats["failures"][row["path"]] = document_failure(row, str(e)[:160])
                    _log(f"extract: FAIL {row['title'][:24]}: {e}")
                _stage("extract", "running",
                       f"{label}提取 {done}/{len(todo)} — {row['title'][:24]}")
    finally:
        conn.close()


# ── wiki 层与条目化两段搬去了 `kb/pipeline_wiki_stages.py`(行数,见那边模块头)──
# **按旧位置再导出一次**:`pipeline.py` / `pipeline_retry.py` 的 import 块、以及
# `tests/test_index_lock.py` 都按这里取。**打桩要打在那边**(那几个函数查的是它自己
# 的全局名字,在这里 patch 拦不住 —— 名字带的是绑定不是引用)。
from kb.pipeline_wiki_stages import (        # noqa: E402 - 再导出,见上
    VIDEOS_DIR as VIDEOS_DIR,
    WIKI_DAILY as WIKI_DAILY,
    _fail_wiki as _fail_wiki,
    _ingest_pass as _ingest_pass,
    _ingest_pretagged_sources as _ingest_pretagged_sources,
    _run_export_script as _run_export_script,
    _run_projects as _run_projects,
    _run_wiki as _run_wiki,
)
