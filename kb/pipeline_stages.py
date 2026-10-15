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

import re
import subprocess
from datetime import datetime
from pathlib import Path

from kb.collectors import FetchScope, active_collector
from kb.config import VIDEOS_ROOT
from kb.pipeline_segments import stage_segment
from kb.pipeline_state import _log, _now, _stage, _state


# ── 采集:抓取 + 导出,两阶段都只跟采集器契约打交道 ────────────────────────────

def crawl_channels(crawl_filter: dict | str | None = None) -> list[dict]:
    """把一次同步的来源摊平成 [{"channel", "accounts"}],一个或多个都是这个读法。

    一次同步可以同时抓几个来源(面板上勾几个就是几个),所以范围是
    `channels: [{"channel": ..., "accounts": [...]}]`;只抓一个来源时仍写成
    老样子的 `channel` + `accounts`,存量的状态文件、定时配置和重试记录
    因此一律不用迁移。
    """
    if isinstance(crawl_filter, str) or crawl_filter is None:
        crawl_filter = {"channel": crawl_filter or ""}
    scopes = crawl_filter.get("channels") or [
        {"channel": crawl_filter.get("channel") or "",
         "accounts": crawl_filter.get("accounts") or []}]
    return [{"channel": s.get("channel") or "",
             "accounts": [n for n in (s.get("accounts") or [])]} for s in scopes]


def run_scopes(run: dict | None) -> list[dict]:
    """这一轮**实际作用在哪几个来源上**。既没抓取也没导出时是 `[]`。

    **只有 crawl / export 认 `crawl_filter`**:`_run_index` 全量扫 vault、
    `_run_extract` 扫全库待提取队列,`_run_projects` / `_run_wiki` 同理,这几个
    函数连通道参数都不收。而 `start_pipeline` **一律**会往 filter 里盖一个默认
    采集器名(实测 `wechat_local`)—— 只跑 index/extract 的那一轮(日报的「入库
    处理」就是)照着 filter 念,会把公众号判成"正在同步"、把订阅写保护错加在它
    头上,而真正参与这一轮的来源反倒被说成别人的任务。

    判据是这一轮**计划里有没有** crawl / export,不是 filter 里写着谁。前端那一
    份同口径在 `kb/static/js/pipe_channels.js` 的 `pipeRunScopes`。

    `planned_stages` 读不到时(存量落盘记录、旧快照)按老样子摊平,不许一律判成
    「不按来源」—— 那会让写保护对所有存量记录静默失效。
    """
    run = run or {}
    planned = run.get("planned_stages")
    if isinstance(planned, list) and not ({"crawl", "export"} & set(planned)):
        return []
    return crawl_channels(run.get("crawl_filter"))


def _channel_label(collector) -> str:
    return getattr(collector, "channel_label", collector.label)


def _per_channel(stage: str, crawl_filter: dict | str | None, run) -> None:
    """让每个选中的来源各跑一遍这个阶段。

    多来源时每个来源的汇报各记一段(stage_segment),否则后一个来源的结论会
    把前一个整个盖掉,面板上只剩最后那个来源的成败。
    """
    scopes = crawl_channels(crawl_filter)
    for scope in scopes:
        collector = active_collector(scope["channel"])
        if len(scopes) == 1:
            run(collector, scope)
            continue
        if _state.get("stop_requested"):
            break            # 停止是对整次同步说的,别再起下一个来源
        with stage_segment(stage, _channel_label(collector)):
            try:
                run(collector, scope)
            except Exception as e:
                # 勾了好几个来源:这一个炸了不该把剩下的一起带走。记成它自己那一段的
                # 失败,面板上照样看得见是谁挂的,后面的来源继续跑。
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
    """
    filters = crawl_filter or {}

    def _fetch(collector, scope: dict) -> None:
        _log(f"采集通道: {_channel_label(collector)}")
        collector.fetch(FetchScope(
            targets=tuple(scope["accounts"]),
            since=filters.get("since_date", ""),
            until=filters.get("until_date", ""),
            force=bool(filters.get("force")),
        ))

    _per_channel("crawl", crawl_filter, _fetch)


def _run_export(crawl_filter: dict | str | None = None) -> None:
    """让采集器把它本地的内容落成 obs md。"""
    _per_channel("export", crawl_filter, lambda collector, scope: collector.materialize())


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
    from kb.scanner import scan_all

    conn = store.connect()
    try:
        now = _now()
        seen: set[str] = set()
        new = 0
        for doc in scan_all():
            seen.add(doc.path)
            if not store.doc_needs_reindex(conn, doc.path, doc.content_hash):
                continue
            store.upsert_document(conn, doc, split_chunks(doc.body), now)
            pre = parse_pretagged(doc)
            if pre is not None:
                save_extraction(conn, doc.path, pre)
            new += 1
            if new % 25 == 0:
                conn.commit()
                _stage("index", "running", f"已索引 {new} 篇")
        removed = store.remove_missing_documents(conn, seen)
        conn.commit()

        def _report(done: int, total: int) -> None:
            _stage("index", "running",
                   f"编码 {total} 个新 chunk" if done == 0 else f"编码 {done}/{total}")

        # 向量编码也在锁里:`chunk_vec` 是没有外键级联的虚表,清理在编码中途
        # 删掉那些 chunk 的话,后面几批会按已经不存在的 chunk id 插回孤儿向量
        # 行,谁也不会再来收(见 `kb.maintenance`)。
        from kb.embedder import embed_pending_chunks
        n = embed_pending_chunks(conn, _report)
        _stage("index", "ok", f"新增/更新 {new},移除 {removed},新向量 {n}")
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
                        stats["failures"][row["path"]] = {
                            "title": row["title"], "reason": "正文为空,需回填"}
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
                    stats["failures"][row["path"]] = {
                        "title": row["title"], "reason": str(e)[:160]}
                    _log(f"extract: FAIL {row['title'][:24]}: {e}")
                _stage("extract", "running",
                       f"{label}提取 {done}/{len(todo)} — {row['title'][:24]}")
    finally:
        conn.close()


# ── wiki 层同步 ─────────────────────────────────────────────────────────────────

WIKI_DAILY = Path(__file__).resolve().parent.parent / "scripts" / "wiki" / "wiki_daily.py"
_ERR_KEEP = 1500          # 失败时保留的 stderr 尾部长度(日志只留 80 条,别让一条撑爆)


def _run_wiki() -> None:
    """extract 之后跑 wiki 层:双链化 → 实体/主题页 → index → digest/synthesis → graph。

    子进程用当前 venv 的 python(wiki_daily 里 linkify 要 import kb.store)。
    digest/synthesis 有日期门(只看最近 2 天且 >= EPOCH),历史批量导入只会触发
    机械的双链化和页面重渲染,不烧 LLM。"""
    import sys as _sys

    _stage("wiki", "running", "同步 wiki 层(双链/页面/摘要)")
    try:
        proc = subprocess.run(
            [_sys.executable, str(WIKI_DAILY), "--recent-days", "2"],
            cwd=WIKI_DAILY.parent, capture_output=True, text=True, timeout=45 * 60,
        )
    except subprocess.TimeoutExpired:
        _stage("wiki", "error", "wiki 同步超时(45 分钟)")
        return
    tail = [l for l in proc.stdout.splitlines()
            if l.startswith(("linkify:", "digest:", "synthesis:",
                             "graph:", "catgraph:"))]
    detail = ";".join(t.strip() for t in tail) or proc.stdout.strip()[-160:]
    # 面板卡片空间有限,压成一行短语;完整输出看日志
    m1 = re.search(r"新双链化 (\d+)", proc.stdout)
    m2 = re.search(r"digest: 生成 (\d+) 天", proc.stdout)
    m3 = re.search(r"synthesis: 更新 (\d+) 个实体", proc.stdout)
    if m1 or m2 or m3:
        detail = (f"双链化 {m1.group(1) if m1 else 0} 篇 · "
                  f"日摘要 {m2.group(1) if m2 else 0} 天 · "
                  f"综合 {m3.group(1) if m3 else 0} 实体")
    # 图谱子步骤自己会让 wiki_daily 非零退出,但那三项摘要一重建就把它盖没了,
    # 面板上只剩「失败: 」一句空话。图谱那几行单拎出来,附在详情后面。
    graph = [l.strip() for l in proc.stdout.splitlines()
             if l.startswith(("graph:", "catgraph:"))]
    bad = [l for l in graph if ": FAIL" in l]
    if m1 or m2 or m3:
        note = ";".join(bad) if bad else ("图谱 ok" if graph else "")
        if note:
            detail = f"{detail} · {note}"
    if proc.returncode != 0:
        _fail_wiki(proc, bad, detail)
    else:
        _stage("wiki", "ok", detail[:200] or "完成")


def _fail_wiki(proc, bad: list[str], detail: str) -> None:
    """wiki 阶段标红:标签(哪张图)、根因(一句话)、完整 stderr(可展开+进日志)。

    三样都得留住,少一样面板上就不够诊断(2026-09-22 第 9 轮审查发现):

    - **标签单独留出来,只截错误正文**。整句一起 `[-200:]` 的话,稍长一点的
      报错就把开头的 `graph:`/`catgraph:` 截没了,红态里连是哪张图挂的都看不出来。
    - **摘要优先取 stderr 里最后一条真正的错误行**。多行 traceback 在 stdout
      那边只留一句「Traceback (most recent call last):」,根因在 stderr 末行;
      `wiki_daily:` 开头那句是它自己的汇总(只说哪张图挂了),跳过去继续往上找。
    - **完整 stderr 进日志、也进失败明细**。子进程是 `capture_output=True` 抓的,
      不主动写出来的话它哪儿都不会出现,用户只能手工重跑一次才能看到根因。
    """
    err = (proc.stderr or "").strip()
    lines = [x.strip() for x in err.splitlines() if x.strip()]
    root = next((x for x in reversed(lines) if not x.startswith("wiki_daily:")), "")
    tags = "/".join(dict.fromkeys(l.split(":", 1)[0] for l in bad))
    body = root or ";".join(l.partition(": FAIL")[2].strip() or l
                            for l in bad) or detail.strip()
    fails = None
    if err:
        _log(f"wiki 子进程 stderr:\n{err[-_ERR_KEEP:]}")
        fails = [{"title": f"{tags or 'wiki_daily'} 子进程报错",
                  "reason": err[-_ERR_KEEP:]}]
    _stage("wiki", "error", f"{tags or 'wiki'} 失败: {body[-160:]}", failures=fails)


# ── 条目化阶段:工具/项目拆条目(dr-gzh-projects / dr-video-projects)─────────────

KB_DIR = Path(__file__).resolve().parent.parent
VIDEOS_DIR = VIDEOS_ROOT


def _run_export_script(script: str, *argv: str, timeout_min: int):
    import sys
    return subprocess.run(
        [sys.executable, f"scripts/{script}", *argv],
        cwd=KB_DIR, capture_output=True, text=True, timeout=timeout_min * 60,
    )


def _ingest_pretagged_sources() -> tuple[int, int]:
    """把条目化脚本新产出的 md 就地入库(只增不删;删除留给下轮 index 阶段)。

    **整趟在维护锁里**,理由和 index 阶段逐字相同(见 `kb.maintenance`):这条
    路也是「scan 读一份磁盘快照 → upsert → 补向量」,而 `embed_pending_chunks`
    补的是**全库**待编码的 chunk、不限于条目来源。不取锁的话,一轮并发的存量
    清理删掉的那些 chunk 会被这里按旧快照插回 `chunk_vec` —— 那张虚表没有外键
    级联,孤儿行谁也不会再来收,还照样占着语义检索的候选名额:清理之后仍然留着
    的文章从此召不回来,重跑索引也不恢复(2026-10-01 审查实测 151 行孤儿向量、
    语义检索返回空)。「只增不删」防不住这件事 —— 要删的是另一个进程。
    """
    from kb.maintenance import maintenance_lock

    waited, on_poll, on_wait = _lock_waiter("projects")
    with maintenance_lock(on_wait=on_wait, on_poll=on_poll):
        if waited["on"] and _state.get("stop_requested"):
            raise _StopWhileWaiting()
        return _ingest_pass()


def _ingest_pass() -> tuple[int, int]:
    from kb import store
    from kb.chunker import split_chunks
    from kb.extract import parse_pretagged, save_extraction
    from kb.scanner import scan_all

    targets = {"dr-video-projects", "dr-gzh-projects"}
    conn = store.connect()
    try:
        now = _now()
        new = 0
        for doc in scan_all():
            if doc.source not in targets:
                continue
            if not store.doc_needs_reindex(conn, doc.path, doc.content_hash):
                continue
            store.upsert_document(conn, doc, split_chunks(doc.body), now)
            pre = parse_pretagged(doc)
            if pre is not None:
                save_extraction(conn, doc.path, pre)
            new += 1
        conn.commit()
        from kb.embedder import embed_pending_chunks
        return new, embed_pending_chunks(conn)
    finally:
        conn.close()


def _run_projects() -> None:
    """条目化:gate 判「工具/项目介绍型」→ transcribe 按工具拆条目。
    两步各用哪个模型看设置面板的「模型分配」(kb.llm_stages 的 gate / transcribe)。

    公众号侧 transcribe 只处理最近 3 天发布的文章——存量积压(数千篇)由用户
    手动分批消化(scripts/gzh_projects_export.py transcribe --limit N),
    避免定时同步悄悄烧大额度。脚本自带文件锁:手动实例在跑时本阶段跳过。"""
    from datetime import timedelta

    from kb.maintenance import LockBusy
    since = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    steps = [
        ("gzh gate", "gzh_projects_export.py", ["gate"], 30),
        ("gzh 条目化", "gzh_projects_export.py",
         ["transcribe", "--since", since], 60),
    ]
    if VIDEOS_DIR.is_dir():
        steps += [
            ("视频 gate", "video_projects_export.py", ["gate"], 20),
            ("视频转写", "video_projects_export.py", ["transcribe"], 90),
        ]
    summaries = []
    for label, script, argv, tmo in steps:
        _stage("projects", "running", f"{label}…")
        try:
            proc = _run_export_script(script, *argv, timeout_min=tmo)
        except subprocess.TimeoutExpired:
            _stage("projects", "error", f"{label} 超时({tmo}分钟)")
            return
        if proc.returncode == 3:
            _stage("projects", "skipped", f"{label}: 有手动实例在跑,本轮跳过")
            return
        if proc.returncode != 0:
            _stage("projects", "error",
                   f"{label} 失败: {(proc.stderr or proc.stdout)[-200:]}")
            return
        tail = [l for l in proc.stdout.splitlines() if "完成:" in l]
        if tail:
            summaries.append(f"{label} {tail[-1].split('完成:')[-1].strip()}")
    _stage("projects", "running", "新条目入库")
    try:
        n_new, n_vec = _ingest_pretagged_sources()
    except _StopWhileWaiting:
        _stage("projects", "stopped", "已请求停止:等维护锁时放弃了新条目入库")
        return
    except LockBusy as e:
        _stage("projects", "error", f"新条目没入库: {e}")
        return
    except Exception as e:
        _stage("projects", "error", f"入库失败: {e}")
        return
    summaries.append(f"入库 {n_new} 条目")
    _stage("projects", "ok", " · ".join(summaries)[:200] or "无新增")
