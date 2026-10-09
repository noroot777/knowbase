"""Incremental fetch and atomic Markdown export for all feed-backed source types."""
from __future__ import annotations

import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from kb.config import VAULT_ROOT
from kb.feeds import registry as reg
from kb.maintenance import maintenance_lock
from kb.feeds import fulltext, translate, x_channels
from kb.feeds.fetching import CollectionBudgetExceeded, SOURCE_SECONDS, fetch_source
from kb.pipeline_state import _state, _stage, note_retry_targets
from kb.scanner import parse_frontmatter

FETCH_WORKERS = 4
COLLECT_SECONDS = 90
# X 的登录态通道(feedgrab / 浏览器)另算预算:它们是串行的 —— 同一个浏览器
# profile 只能被一个实例打开 —— 而单个账号要滚时间线、必要时还要等懒加载,
# 90 秒连五个号都跑不完。这条通道按天同步(见 `source_types` 里 social 的
# interval),所以给得宽;用完的账号记成"留待下次",靠 last_attempt 轮转补齐。
X_LOCAL_SECONDS = float(os.environ.get('KB_X_LOCAL_SECONDS', '1200'))
BRIDGE_FAILURE_LIMIT = 3
BUDGET_MESSAGE = '本轮采集时间预算不足，剩余订阅留待下次同步'


def _fetch_with_budget(source, settings, scope, collect_deadline):
    # Check again in the worker: submission/scheduling can use up the margin.
    # Each admitted source gets its own full allowance, never a clipped tail.
    deadline = time.monotonic() + SOURCE_SECONDS
    if deadline > collect_deadline:
        raise CollectionBudgetExceeded
    # 桥接通道的 X 长文补全挂在这儿(另三条通道在预取里就补过了)。判据和动作
    # 都在 `x_channels` 那一侧:它是「当前这套设置会怎么跑」的唯一口径,而
    # `targets()` 回答的正是同一个问题的另一面(哪些订阅**不**走 HTTP)。
    return x_channels.fill_bridge(source, settings, fetch_source(
        source, settings, unconditional=scope.ranged,
        deadline=deadline, collect_deadline=collect_deadline))


def _within_history(source, entries):
    """新订阅只收最近几天的内容,不把源站的整个存档一起拖进来。

    下界是订阅创建(或改链接重置)那一刻钉死的一个日期,**不是"距今 N 天"的
    滑动窗口**。两点理由,缺一条这道闸就从防线变成漏斗:

    ①滑动窗口会在源站停更几天后恢复时,把那几天的内容静默丢掉——而钉死的下界
      对一个已经建立起来的订阅永远是过去式,只在它自己最前面那几次抓取起作用。
    ②只拦"第一次抓取"同样不行:第一次之后 etag 命中 304、什么都不返回,等源站
      下次更新时整份 feed(连同那 500 条存档)又回来了,那时闸门已经撤了。
      按固定日期判则与抓第几次无关。

    没有发布时间的条目照收:它身上没有任何"很旧"的证据,丢掉是不可恢复的静默
    损失,而日报那边本来就按"补录"接住这一档(只会出现一次)。区间补抓
    (`scope.ranged`)不受这道闸限制——那是用户点名要的那段日期,拿它当逃生口。

    **下界现读,不信手里那份订阅快照**:快照是发请求**之前**读的,而这中间
    `feed_trim_history` 可能刚给这个订阅钉上下界、把存档清了出去——按旧快照
    (空下界)存结果就是把刚删掉的旧文原样再入库一遍,文件和归属一起回来
    (2026-10-01 审查实测 1 条)。订阅行已经没了的时候才退回快照那一份。
    """
    floor = _stored_floor(source)[:10]
    if not floor:
        return entries
    return [e for e in entries if not e['published'] or e['published'][:10] >= floor]


def _stored_floor(source) -> str:
    with reg.db() as c:
        row = c.execute('SELECT history_floor FROM subscriptions WHERE id=?',
                        (source['id'],)).fetchone()
    return (row['history_floor'] if row else source.get('history_floor')) or ''


def _repointed(c, source) -> bool:
    """手里这份结果还属于这条订阅吗? —— 存盘那个事务里现读一次身份再答。

    这一轮的订阅快照是**发请求之前**读的,而中间用户完全可能把这条订阅改指到
    另一个账号/另一个 feed(`registry.save_subscription` 的 `reset`:清 config、
    清条目归属、重算历史下界)。那之后按 `source['id']` 把手里这份写回去就是:
    旧账号的推文记成新账号的内容、旧账号那条 etag/resolved_url 盖在新 feed 上、
    **旧账号的水位写进新账号那个键** —— 最后一条最狠,它让新账号比这个时间更早
    的推文从此任何一轮增量都选不到(2026-10-08 审查实测:改成 NewExample 之后
    库里躺着 `OldExample/status/900`,`next_sync_selection=[]`,而面板报 ok)。

    **判据必须和这次写入同生共死,所以调用方得先拿到写锁(见 `_claim_write`),
    不能在 API 入口先 SELECT 一次。** 入口那道 `intake_api.guard`(忙就 409)
    是**早一道**:它只回答"这一刻在跑吗",答不了"我保存的这一瞬间还没在跑吗"
    —— 已经通过检查、还在途中的那个修改请求照样会落在采集中间(审查就是这么
    复现的:修改先过 guard,随后管道才起跑)。口径同 `collector` 那条"判据就在
    UPSERT 自己的 WHERE 里"和清理脚本那条"真正的互斥是那把锁,问管道忙不忙只是
    早一道":先查后写在两个进程之间压根不成立,而 SQLite 把写入串行化。

    订阅行已经被删掉时同样返回 True:那一轮的结果不该把条目归属重新插回去。
    """
    row = c.execute('SELECT url, feed_url FROM subscriptions WHERE id=?',
                    (source['id'],)).fetchone()
    if not row:
        return True
    return (row['url'], row['feed_url']) != (source.get('url'), source.get('feed_url'))


def _claim_write(c, source) -> bool:
    """先拿写锁、再认身份,返回「这一轮的结果还写不写得进去」。

    **两句的顺序就是这个函数存在的理由。** `reg.db()` 交出来的连接**不在事务
    里** —— 它自己那个 `BEGIN IMMEDIATE` 在初始化/迁移之后就提交了(见
    `registry.db`),而 python 的 `sqlite3` 在 legacy `isolation_level` 下
    **SELECT 压根不开事务、不持任何锁**。所以光把 `_repointed` 搬进
    `with reg.db()` 块里只是把窗口从「整轮采集」缩成「检查到第一笔写入之间」,
    另一个连接的修改照样插得进来,后果和这道判据本来要挡的事故一模一样:
    2026-10-08 第 3 轮审查实测检查那一刻 `in_transaction=false`,随后那次真
    PATCH 以 200 提交,旧账号的 `/status/900` 照样记成了新账号的内容、水位也
    跟着过去(`next_sync_selection=[]`),失败那一侧是旧账号的错误连同 1800 秒
    退避记在了一条从没被访问过的新订阅头上。

    `BEGIN IMMEDIATE` 当场拿到写锁,于是这期间的订阅修改只有两种结局,两种的
    末态都是对的:①赶在取锁之前提交 —— 那就会被这次 `_repointed` 看见,整轮
    作废;②等我们提交之后才落下 —— `save_subscription` 的 `reset` 会把刚写进去
    的条目归属、config(两条通道的水位)、成功时间戳一并清掉,等于用户在一轮
    采集**结束之后**改指,本来就是这个结果。不再有「检查通过了又被串号」这一档。

    **不能反过来写成「先 SELECT 再升级成写事务」**:WAL 下拿不到快照报的是
    `SQLITE_BUSY_SNAPSHOT`,`busy_timeout` 管不着它(口径同清理脚本那条)。
    """
    c.execute('BEGIN IMMEDIATE')
    return not _repointed(c, source)


def _save_result(source, result, scope):
    entries, validators = result
    if scope.ranged:
        entries = [e for e in entries if e['published'] and scope.since <= e['published'][:10] <= scope.until]
        # A ranged import must not cache away entries outside the selected dates.
        # 重建这一份也顺手丢掉 `config_patch`(登录态通道的水位):区间补抓读的
        # 是用户点名的那段日期,拿它当增量进度就是宣布中间那段已经收过。
        validators = {k: source[k] for k in ('etag', 'modified', 'resolved_url')}
    # 存盘整段在维护锁里:存量清理和索引持同一把锁,两边交错的话它删掉的旧文
    # 会被这一轮按旧快照原样写回去(见 `kb.maintenance`)。锁内再现读一次下界,
    # 所以对方不持锁的旁路同样挡得住 —— 两道各管一段,缺一不可。
    with maintenance_lock():
        if not scope.ranged:
            entries = _within_history(source, entries)
        # **条目、水位、成功时间戳一个事务**。分成两个的话,`put_items` 失败那
        # 一轮水位已经推进了,而这批推文一条都没入库 —— 它们比旧水位新、又比
        # 新水位老,从此任何一轮增量都选不到(见 `x_channels.watermark_patch`)。
        with reg.db() as c:
            if not _claim_write(c, source):
                # 这条订阅在本轮采集期间被改指到别的源了 —— 手里这份属于旧的那个。
                # 一个字都不写(条目、水位、成功时间戳一样都不写):这一轮作废,
                # 下一轮按新身份重新抓。
                return None
            added = reg.put_items(source, entries, conn=c)
            reg.merge_config(source['id'], validators.get('config_patch') or {}, conn=c)
            c.execute('''UPDATE subscriptions SET last_attempt=?,last_success=?,last_error='',
                failures=0,retry_at=0,etag=?,modified=?,resolved_url=?,last_note=? WHERE id=?''',
                (reg.now(),reg.now(),validators['etag'],validators['modified'],
                 validators['resolved_url'],
                 # 区间补抓不改日常增量的任何状态,这条提示也算 —— 清掉它等于替
                 # 日常同步宣布"读完了",而那一边的进度一点没动。
                 source.get('last_note','') if scope.ranged else validators.get('note',''),
                 source['id']))
    return added


def _save_failure(source, error, summary) -> bool:
    """失败也要先拿写锁再认身份(见 `_claim_write`),返回这一笔记没记上。

    被改指之后把旧账号那次失败记在新账号头上,是把一条好订阅按别人的毛病推进
    退避(最长 24 小时),面板上还显示"连接失败" —— 而新账号一次都没被访问过。
    """
    reason = str(error)[:1200] or type(error).__name__
    with reg.db() as c:
        if not _claim_write(c, source):
            return False
        summary['errors'].append({'title': source['name'], 'reason': reason})
        # 「重试失败项」靠这份身份收窄下一轮的范围(见 `note_retry_targets`)。
        # 记 id 而不是名字:名字会被改,而改完之后这条订阅还是同一条。
        summary['failed_ids'].append(str(source['id']))
        c.execute('''UPDATE subscriptions SET last_attempt=?,last_error=?,failures=failures+1,
            retry_at=? WHERE id=?''', (reg.now(),reason,
            time.time() + min(86400, 1800 * 2 ** min(source['failures'], 6)),source['id']))
    return True


def _prefetch_x(kind, sources, settings, scope):
    """X 登录态通道的整批预取。跑在起线程池之前,所以读侧不必加锁。

    一个账号都抓不到也不抛:每个账号的失败已经落在它自己那一项上,随后
    `fetch_source` 会把它翻成这条订阅的失败 —— 一个改了名的号不该把整轮带掉。
    通道是 bridge(或这一类来源没有通道概念)时 `targets()` 返回空,直接空转。
    """
    x_channels.reset()
    if not x_channels.supports_channels(kind):
        return
    picked = x_channels.targets(sources, settings)
    if not picked:
        return
    name = x_channels.label(x_channels.channel_of(settings))
    _stage('crawl', 'running', f'{reg.KINDS[kind]} · {name}:准备读取 {len(picked)} 个账号')

    def progress(done, count, handle):
        _stage('crawl', 'running', f'{reg.KINDS[kind]} · {name} {done}/{count} · @{handle}')

    # 区间补抓把它那段日期的起点交给采集器当下界:日常水位可能已经停在那段
    # 日期之后,按水位读的话用户点名的那几天一条都翻不到(见 `prefetch`)。
    x_channels.prefetch(sources, settings, budget=X_LOCAL_SECONDS,
                        since=scope.since if scope.ranged else '',
                        on_progress=progress,
                        should_stop=lambda: bool(_state.get('stop_requested')))


def still_failing(kind, note):
    """上一轮失败的那几条订阅里,现在仍然需要重抓的那些(返回订阅 id)。

    判据**不是** `last_error` 还在不在:改链接时 `save_subscription` 的 reset 会把
    它连同退避一起清空,照它筛就把「已改正、还没验证过」的那条当成「已经恢复」漏掉
    —— 用户修完地址点「重试失败项」,那个账号一条都不抓(见 `note_retry_targets`)。

    判据是**那一轮失败之后有没有成功同步过**:`last_success` 比名单的时刻新,说明它
    后来已经成功了(比如用户在「我的订阅」里单独同步过一次),不必再抓一遍 —— 而改过
    链接的那条 `last_success` 被 reset 清成了空串,所以它留在名单里。订阅被删掉、
    停用、或者名单里那个 id 压根不存在时一并摘掉:没有可抓的东西。
    """
    at = note.get('at') or ''
    ids = {str(t) for t in (note.get('targets') or [])}
    # 名单是空的(这个来源那一轮扫完了、没有要重抓的)就别去开订阅库:答案只能是
    # 空集,而读库会在库打不开时抛 —— 那个故障跟同一轮里的 index/extract 毫无
    # 关系,不该由它们承担(调用方另有一道按来源的隔离,见 `retry_crawl_scopes`)。
    if not ids:
        return []
    live = []
    for source in reg.subscriptions(kind):
        if str(source['id']) not in ids or not source['enabled']:
            continue
        if at and source['last_success'] and source['last_success'] > at:
            continue
        live.append(str(source['id']))
    return live


def collect(kind, scope):
    started = reg.now()
    sources = [s for s in reg.subscriptions(kind) if s['enabled'] and
               (not scope.targets or str(s['id']) in scope.targets or s['name'] in scope.targets)]
    summary = {'sources': len(sources), 'checked': 0, 'added': 0, 'deferred': 0,
               'unconfigured': 0, 'skipped': 0, 'message': '', 'errors': [],
               'partial': [], 'repointed': 0, 'stopped': False,
               # 这一轮失败的那几条订阅的稳定身份,留给「重试失败项」收窄范围
               'failed_ids': []}
    settings = reg.get_settings(kind)
    pending, unconfigured_why = [], ''
    for source in sources:
        if problem := reg.configuration_error(source, settings):
            summary['unconfigured'] += 1
            # 理由按当前通道走(桥接要模板、feedgrab 要登录态、浏览器要登录过),
            # 所以收尾那行提示也从这里取,不写死"请填写 X 桥接"。
            unconfigured_why = unconfigured_why or problem.removeprefix('待配置：')
        elif source['retry_at'] > time.time() and not scope.force:
            summary['deferred'] += 1
        else:
            pending.append(source)
    # Budget-limited runs continue with never/least recently attempted sources.
    pending.sort(key=lambda s: (s['last_attempt'], s['id']))
    _prefetch_x(kind, pending, settings, scope)
    # 预取可能跑十几分钟(见 X_LOCAL_SECONDS),所以 HTTP 那一侧的预算要在它
    # **之后**才开始算。算在前面的话,预取一跑完每条订阅都已经超了预算,整轮
    # 一条也取不走、全部记成"留待下次"—— 而东西明明已经抓在手里了。
    deadline = time.monotonic() + COLLECT_SECONDS
    # 桥接熔断只对桥接通道成立:它防的是"公共实例挂了,别再拿 50 个账号去撞"。
    # 登录态通道的内容已经在预取里抓完,这里只是逐条取走,断开只会白扔掉已经
    # 付过代价的结果;而那两条通道真出问题(登录失效)时每个账号拿到的是同一
    # 句话,熔断省不下任何东西。
    shared_upstream = (x_channels.supports_channels(kind)
                       and x_channels.channel_of(settings) == x_channels.BRIDGE)
    bridge_failures = 0
    # Fetches alone run concurrently; persistence and pipeline state have one writer.
    # Submit only one bounded batch so failures/stop never leave a 51-job backlog.
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix='feed') as pool:
        while pending:
            if _state.get('stop_requested'):
                summary['stopped'] = True
                break
            if deadline - time.monotonic() < SOURCE_SECONDS:
                summary['skipped'] += len(pending)
                summary['message'] = BUDGET_MESSAGE
                break
            if bridge_failures >= BRIDGE_FAILURE_LIMIT:
                independent = [s for s in pending if not reg.uses_bridge(s)]
                summary['skipped'] += len(pending) - len(independent)
                summary['message'] = 'X 桥接连续失败，已暂停本轮其余桥接请求；请检查桥接设置后重试'
                pending = independent
            batch, pending = pending[:FETCH_WORKERS], pending[FETCH_WORKERS:]
            if not batch:
                break
            _stage('crawl', 'running', f"{reg.KINDS[kind]} · 正在读取 {len(batch)} 个来源（已检查 {summary['checked']}/{len(sources)}）")
            futures = {pool.submit(_fetch_with_budget, s, settings, scope, deadline): s for s in batch}
            shared_ok, shared_errors = False, 0
            for future in as_completed(futures):
                source = futures[future]
                try:
                    result = future.result()
                    shared_ok |= shared_upstream and reg.uses_bridge(source)
                    saved = _save_result(source, result, scope)
                    if saved is None:
                        # 这条订阅在采集期间被改指到别的源(见 `_repointed`):
                        # 这一份结果作废,既不算新增也不算检查过 —— 说"检查过"
                        # 等于替新账号宣布它同步过了。
                        summary['repointed'] += 1
                        continue
                    summary['added'] += saved
                    # **「没读完」不是失败,但也不许静默**:抓到的照旧入库、水位
                    # 留在原处(下一轮接着读),可"停在时间线半中间"和"全读完了"
                    # 在面板上不该长得一模一样。记成 error 更不对 —— 那会显示
                    # "连接失败"并让这条订阅进退避重试,而连接好得很。
                    if note := result[1].get('note'):
                        summary['partial'].append({'title': source['name'], 'reason': note})
                except CollectionBudgetExceeded:
                    summary['skipped'] += 1
                    summary['message'] = BUDGET_MESSAGE
                    continue
                except Exception as e:
                    if not _save_failure(source, e, summary):
                        summary['repointed'] += 1
                        continue
                    shared_errors += int(shared_upstream and reg.uses_bridge(source))
                summary['checked'] += 1
                _stage('crawl', 'running', f"{reg.KINDS[kind]} {summary['checked']}/{len(sources)} · {source['name']}")
            bridge_failures = 0 if shared_ok else bridge_failures + shared_errors
    summary['stopped'] |= bool(_state.get('stop_requested'))
    with reg.db() as c:
        c.execute('INSERT INTO feed_runs(kind,started_at,finished_at,summary) VALUES(?,?,?,?)',
                  (kind,started,reg.now(),json.dumps(summary,ensure_ascii=False)))
        c.execute('DELETE FROM feed_runs WHERE kind=? AND id NOT IN '
                  '(SELECT id FROM feed_runs WHERE kind=? ORDER BY id DESC LIMIT 30)', (kind,kind))
    # 把这一轮失败的那几条留给「重试失败项」。**一条都没失败也要记**:那份空名单
    # 本身就是"这个来源这一轮扫完了、没有要重抓的"这句话,而「压根没留下名单」另有
    # 含义 —— 整批失败,按原范围重跑(见 `note_retry_targets`)。
    #
    # 被停掉、或者有订阅因为预算/退避没轮到的那一轮,名单里只有**真失败过**的那几
    # 条:没轮到的不是失败项,它们照旧按 `last_attempt` 轮转,下一轮常规同步自己会
    # 补上(水位一个字没动),不该借这颗按钮把整个来源再抓一遍。
    # 时刻用 `reg.now()`:它要和 `subscriptions.last_success` 比(见 `still_failing`),
    # 两边必须是同一套格式。
    note_retry_targets(kind, summary['failed_ids'], reg.now())
    label = f"检查 {summary['checked']}/{len(sources)} 个来源，新增 {summary['added']} 条"
    if summary['deferred']:
        label += f"，{summary['deferred']} 个等待重试"
    if summary['errors']:
        label += f"，失败 {len(summary['errors'])} 个"
    if summary['partial']:
        label += f"，{len(summary['partial'])} 个没读完（{summary['partial'][0]['reason']}）"
    if summary['repointed']:
        label += f"，{summary['repointed']} 个采集期间改了订阅链接（这一轮的结果已丢弃）"
    if summary['unconfigured']:
        label += f"，待配置 {summary['unconfigured']} 个（{unconfigured_why}）"
    if summary['message']:
        label += f"，跳过 {summary['skipped']} 个；{summary['message']}"
    status = 'error' if summary['errors'] else 'skipped' if summary['unconfigured'] else 'ok'
    _stage('crawl', 'stopped' if summary['stopped'] else status,
           label, failures=summary['errors'])
    return summary


# export 自己产出的 frontmatter 字段:每一轮都按 registry 里的当前事实重写。
# 这张表之外的键都是**别人**写进这篇 md 的(提取回写的 tags 是唯一的现役例子),
# 重导出时必须原样留下。
_OWNED_META = ('source', 'title', 'account', 'published', 'url', 'fetched_at',
               'content_kind', 'lang', 'translated_from', 'original_title')


def _kept_meta(path: Path) -> dict:
    """旧文件里那些不属于 export 的 frontmatter 字段。

    重导出是整份重写这篇 md。原来只写 export 自己那几个键,于是提取阶段回写的
    `tags`(topic/entity/flag)被一起抹掉 —— 而索引按 frontmatter 重建 doc_tags、
    `documents.extracted_at` 又还在,提取不会再做一遍:这篇文章从此按话题再也
    搜不到,以后每次同步都是同一个结果(2026-09-30 审查实测:一篇已提取的英文
    旧文补译后,按原话题检索当场从 1 条归零)。

    重导出原本少见(只有源内容变了才发生),翻译把它变成了常规路径:没配 key
    那阵子按英文入库的条目补上 key 之后要重写一遍,老解析器留下的半成品补译
    标题也要重写一遍。保留而不是重做提取,是因为标签口径没变 —— 内容还是同一
    篇,只是换了语言,再买一次提取只是重复付钱。
    """
    try:
        meta, _ = parse_frontmatter(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError):
        return {}                     # 还没导出过(或者读不了),没什么可留的
    return {k: v for k, v in meta.items() if k not in _OWNED_META}


def _new_failures(conn, queue: str, cond: str, args: list, extra: list,
                  keys: list[str], column: str = 'translate_error') -> tuple[int, int]:
    """这一轮刚失败、而且**还排在队列里**的那些,按"入没入过库"分两档。

    返回 `(未入库的, 已入库的)`。`keys` 是这一轮真失败的那几个条目;空列表
    (比如 `export` 被单独调用、这一轮压根没跑那一步)时两档都是 0 —— 没跑就没有
    "本轮新失败"可言,队列里那些错误全是此前留下的。

    **翻译和正文补抓共用这一份**(`column` 选哪一列的错误,`cond`/`extra` 给各自
    那个队列判据)。两边要回答的是同一个问题:"这一轮真失败的那几条,现在还被
    扣在队列里吗" —— 各写一遍的话,一边判红一边不判,而两处的出口规则
    (试满 MAX_TRIES 就离开队列、不该判红)一模一样。`column` 只在这个模块里被
    两个字面量调用,没有外部输入拼进 SQL。
    """
    if not keys:
        return 0, 0
    fresh = shipped = 0
    ph = ','.join('?' * len(keys))
    for t in conn.execute(
            f"SELECT exported_hash='' AS fresh, count(*) AS n {queue} AND ({cond})"
            f" AND {column}<>'' AND key IN ({ph}) GROUP BY fresh",
            (*args, *extra, *keys)):
        if t['fresh']:
            fresh = t['n']
        else:
            shipped = t['n']
    return fresh, shipped


def export(kind: str, hold_untranslated: bool = False,
           translated: dict | None = None, fetched: dict | None = None):
    # 整段在维护锁里:导出是「选出待导出的行 → 逐篇写 vault」,存量清理是
    # 「删 feeds.db 行 → 删 vault 文件」。不互斥的话清理删掉行、导出随后按
    # 自己那份快照把文件写回去,而 feed_items 里已经没有这一条——vault 里从此
    # 躺着一篇谁也管不着的旧文,索引照样收它,下一次清理也找不到它
    # (见 `kb.maintenance`)。
    with maintenance_lock():
        return _export(kind, hold_untranslated, translated, fetched)


def _export(kind: str, hold_untranslated: bool = False,
            translated: dict | None = None, fetched: dict | None = None):
    # hold_untranslated:还排在翻译队列里的条目这一轮不写 vault。一轮只译
    # MAX_ITEMS 条,不拦的话第 41 条会先以英文落盘,索引/提取/检索/日报都按
    # 英文处理过一遍。翻译整个跳过时(没配 key)不能拦,否则这些条目永远进
    # 不了知识库 —— 那时英文总比没有强。
    #
    # translated:这一轮翻译的成品摘要(`translate.translate_pending` 的返回值)。
    # 导出是这条链上**最后**一个报阶段状态的人,翻译失败必须在这里汇总:只写
    # 进日志的话,这一行 _stage 随后把阶段刷成绿色,而实测那一轮确实有新文章
    # 因为翻译 503 一篇都没进知识库,日报那头照样记「同步完成」
    # (2026-09-30 审查实测)。
    #
    # fetched:这一轮正文补抓的成品摘要(`fulltext.fetch_pending` 的返回值)。
    # 理由和 translated 逐字相同 —— 它原来只写进日志,而 `_stage` 随后把这个
    # 阶段刷成绿色:实测一条新条目的原文回 403,面板报「已保存 0 条到知识库,
    # 1 条待补抓原文正文」、阶段 ok、failures 空,而那篇文章一个字都没进知识库
    # (2026-10-09 审查实测)。补抓是这条链上最前面那一步,它失败和翻译失败一样
    # 要在收尾处汇总。
    queue = 'FROM feed_items WHERE kind=?'
    base = queue + ' AND exported_hash<>content_hash'
    args: list = [kind]
    held = stuck = shipped = shipped_stuck = 0
    fresh_new = shipped_new = 0
    unfetched = unfetched_bad = refetch = refetch_bad = 0
    fetch_fresh_new = fetch_shipped_new = 0
    # **还排在正文补抓队列里的条目这一轮不写 vault**(见 `kb.feeds.fulltext`)。
    # 它们此刻的 body 是 feed 给的跳转壳,正文马上要被整条换掉 —— 先按壳落盘
    # 的话,索引、提取打标、检索、日报全都按那几十个字处理过一遍,而一轮之后
    # 同一篇又要重做。队列有出口(试满 `fulltext.MAX_TRIES` 就按壳导出),所以
    # 一个付费墙后面的链接不会让这条内容永远进不了知识库。
    #
    # 这里不像翻译那样留一个开关:翻译"整个被跳过"是有的(没配 key),那时扣着
    # 就等于永久扣着;而补抓不需要任何配置,抓不动会自己计次出队。
    #
    # **收窄 base 的那一句排在所有统计之后**(和下面翻译那道同一个位置):
    # `queue`/`cond` 那几条统计语句各自只带 `kind` 一个占位符,拼参数时用的是
    # `(*args, *extra)` —— 在它们之前往 `args` 里追加,绑定数当场对不上,
    # 整个导出阶段抛 `ProgrammingError`(写这段时就是这么先红的一次)。
    fetch_cond, fetch_args = fulltext.queued_sql()
    with reg.db() as c:
        # 四档,和翻译那边同一个形状:**积压、这一轮的故障、此前的故障、已经
        # 降级出队**是四件事,合成一个数就既说不清这一轮出没出故障,也说不清
        # 那个数字为什么天天不变。
        #   fresh(exported_hash='') 还没入库,导出闸门确实扣着它
        #   bad(fulltext_error<>'') 抓过、报了错、还可以再试
        # 已入库那一档(旧链接后来才打开补抓开关)照旧读得到,可库里躺的是那个
        # 几十字的跳转壳 —— 说成「同步完成」和翻译那一档一样是谎报。
        ftally = c.execute(
            f"SELECT exported_hash='' AS fresh, fulltext_error<>'' AS bad,"
            f' count(*) AS n {queue} AND ({fetch_cond}) GROUP BY fresh, bad',
            (*args, *fetch_args)).fetchall()
        # 口径取 `fetch_pending` 这一轮真失败的那几个 key 再回库查一次,不直接用
        # 摘要里的 `failed`/`empty` 计数:本轮失败之后试满 `fulltext.MAX_TRIES`
        # 的条目已经离开队列、按壳入库了,那是设计好的降级出口,不该判红
        # (见 `fulltext.queued_sql`)。回查天然把它们排除在外。
        fetch_fresh_new, fetch_shipped_new = _new_failures(
            c, queue, fetch_cond, args, fetch_args,
            list((fetched or {}).get('failed_keys') or []), 'fulltext_error')
    for t in ftally:
        if t['fresh']:
            unfetched += t['n']
            unfetched_bad += t['n'] if t['bad'] else 0
        else:
            refetch += t['n']
            refetch_bad += t['n'] if t['bad'] else 0
    if hold_untranslated:
        cond, extra = translate.queued_sql()
        with reg.db() as c:
            # 数的是**整个翻译队列**,不是下面那个"待导出"集合。按英文导出过、
            # 内容又没变的条目 exported_hash==content_hash,压根不在待导出集合
            # 里,可它们照样排在队列里等补译 —— 拿待导出集合去数,这一档的补译
            # 失败一条也数不出来,导出阶段绿、日报记「同步完成」,而素材清单里
            # 还躺着英文标题(2026-09-30 第 2 轮审查实测)。
            #
            # 两个维度各回答一件事,四档都得分开说:
            #   fresh(exported_hash='') 还没入库,导出闸门确实扣着它
            #   bad(translate_error<>'') 译过、报了错、还可以再试
            # 合成一个数就等于把故障说成积压,或者把"库里还是外文"说成"读不到"。
            # 判据和来源面板那三档是同一个(见 `registry.items`)。
            tally = c.execute(
                f"SELECT exported_hash='' AS fresh, translate_error<>'' AS bad,"
                f' count(*) AS n {queue} AND ({cond}) GROUP BY fresh, bad',
                (*args, *extra)).fetchall()
            # **本轮新失败**要单独数出来。上面那份 tally 数的是队列里所有
            # `translate_error` 非空的条目,而那是**累计**的:一条失败过一次
            # 的内容会一直带着这行错误待在队列里,直到轮到它重试。两者合成
            # 一个数的后果实测过 —— 63 条陈年失败让导出阶段天天判红、天天报
            # 同一个数字,而那一轮一次模型调用都没失败(2026-10-08)。
            #
            # 口径取 `translate_pending` 这一轮真失败的那几个 key,再**回库
            # 里查一次**,不直接用摘要里的 `failed` 计数:本轮失败之后试满
            # MAX_TRIES 的条目已经离开队列、按原文入库了,那是设计好的出口,
            # 不该判红(见 `translate.queued_sql`)。回查天然把它们排除在外。
            fresh_new, shipped_new = _new_failures(
                c, queue, cond, args, extra,
                list((translated or {}).get('failed_keys') or []))
        for t in tally:
            if t['fresh']:
                held += t['n']
                stuck += t['n'] if t['bad'] else 0
            else:
                shipped += t['n']
                shipped_stuck += t['n'] if t['bad'] else 0
        base += f' AND NOT ({cond})'
        args += extra
    base += f' AND NOT ({fetch_cond})'
    args += fetch_args
    with reg.db() as c:
        rows = c.execute(f'SELECT * {base}', args).fetchall()
    errors, done = [], 0
    root = VAULT_ROOT / 'source' / kind
    root.mkdir(parents=True, exist_ok=True)
    for item in rows:
        if _state.get('stop_requested'):
            break
        path = root / (item['key'] + '.md')
        rel = str(path.relative_to(VAULT_ROOT))
        title, body = translate.display(item)
        # 标题被译了也算"这一篇是译文":只有一行标题的短动态(正文为空)整条
        # 可读内容就是标题,不标的话 vault 里这一篇看不出是译文、也丢掉了
        # 回查原标题的入口。
        # 名字不能叫 translated:那是本函数的形参(这一轮的翻译摘要)。同名的话
        # 循环跑完之后摘要已经变成一个布尔值,收尾处 `.get('errors')` 当场
        # AttributeError —— 而那时成功的文章已经落盘、exported_hash 也更新过,
        # 异常却把 materialize 连同后面的 index/extract 一起带崩:同一轮里译成
        # 的新文章一篇都进不了检索和当天日报(2026-09-30 第 2 轮审查实测)。
        is_zh = body != item['body'] or title != item['title']
        meta = dict(source=kind, title=title, account=item['author'], published=item['published'],
                    url=item['url'], fetched_at=item['fetched_at'], content_kind=item['content_kind'])
        if is_zh:
            meta.update(lang='zh', translated_from='en')
            # 标题没跟着译时不写这一行:原标题和 title 一模一样,写上去只是噪音。
            # 真译了才留 —— 正文已经整篇换成中文,想按英文名找回这条只剩这个入口。
            if title != item['title']:
                meta['original_title'] = item['title']
        # 留在最后:writeback_tags 是往 frontmatter 末尾追加 tag 的,tags 不在
        # 末尾的话新 tag 会落进别的键下面。
        kept = _kept_meta(path)
        if item['extract_stale']:
            # 这一条的正文刚被整篇换过(几十字的跳转壳 → 整篇文章),于是旧文件
            # 里那些 `topic/`、`entity/` 是按**那个壳**算出来的。不摘的话重新
            # 提取时 `writeback_tags` 往后追加,`topic/link-only` 这种会永久留
            # 在这篇文章上。人工 tag 和 `flag/软广` 照留(口径在 `keep_tags`)。
            if tags := fulltext.keep_tags(kept.get('tags')):
                kept['tags'] = tags
            else:
                kept.pop('tags', None)
        meta.update(kept)
        notice = '\n> 以下为订阅源提供的摘要；完整内容请打开原文。\n' if item['content_kind'] == 'summary' else ''
        if is_zh:
            notice += '\n' + translate.NOTE + '\n'
        text = '---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n\n'
        # Keep the parsed text intact for the vault, indexing and extraction.
        # HTML safety belongs to the reader (textContent / DOMPurify).
        text += '# ' + title + '\n' + notice + '\n' + body + '\n'
        temp = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=root, suffix='.tmp', encoding='utf-8', delete=False) as f:
                temp = Path(f.name)
                f.write(text)
            os.replace(temp, path)
            with reg.db() as c:
                c.execute('UPDATE feed_items SET exported_hash=content_hash,path=? WHERE key=?', (rel,item['key']))
            done += 1
        except Exception as e:
            errors.append({'title': item['title'], 'reason': str(e)[:300]})
        finally:
            if temp:
                temp.unlink(missing_ok=True)
    # 正文被整篇换过的那几篇:新 md 已经落盘,现在让知识库里那份按旧壳算出来的
    # 摘要、主题和实体作废,下一轮提取重做一遍(见 `fulltext.retire_stale`)。
    # **接住异常**:这一步失败只是"摘要还是旧的",不该把整个导出阶段带崩 ——
    # 成功的文章已经落盘、`exported_hash` 也更新过了,异常冒上去会把
    # materialize 连同后面的 index/extract 一起带掉(同 `is_zh` 那次事故)。
    # 待办留在 `extract_stale` 上,下一轮导出自己会再来一次。
    try:
        fulltext.retire_stale(kind)
    except Exception as e:               # noqa: BLE001
        errors.append({'title': '让补抓条目的旧摘要作废', 'reason': str(e)[:300]})
    note = f'已保存 {done} 条到知识库'
    # 补抓那几档,拆法和下面翻译那几档逐字对应:本轮故障 / 此前故障（排队重试）
    # / 还没轮到。混成一个「N 条待补抓」的话,一条原文 403 的新文章和一条排在
    # 第 21 位还没轮到的新文章在面板上长得一模一样,而前者这一轮确实出了故障。
    if fetch_fresh_new:
        note += f'，本轮 {fetch_fresh_new} 条原文抓取失败（暂未入库）'
    if unfetched_bad - fetch_fresh_new > 0:
        note += f'，{unfetched_bad - fetch_fresh_new} 条此前原文抓取失败（排队重试，暂未入库）'
    if unfetched - unfetched_bad > 0:
        note += f'，{unfetched - unfetched_bad} 条待补抓原文正文（抓到正文再入库）'
    # 已经按壳入库的那一档:抓取失败不影响"读得到",可库里躺的是那个几十字的
    # 跳转壳 —— 检索搜不到文章里的任何一句话,提取打标判不出实体。所以本轮
    # 新失败的照样判红,只是文案说清库里此刻是什么。
    if fetch_shipped_new:
        note += f'，本轮 {fetch_shipped_new} 条原文抓取失败（库里仍是链接内容）'
    if refetch_bad - fetch_shipped_new > 0:
        note += (f'，{refetch_bad - fetch_shipped_new} 条此前原文抓取失败'
                 f'（排队重试，库里仍是链接内容）')
    if refetch - refetch_bad > 0:
        note += f'，{refetch - refetch_bad} 条等待原文正文覆盖'
    # 失败那两档各拆成「本轮」和「此前」:它们是两件事,混成一个数就既说不清
    # 这一轮到底出没出故障,也说不清那个数字为什么天天不变(2026-10-08 实测:
    # 63 条陈年失败连着让每一轮导出判红,而那几轮一次模型调用都没失败)。
    if fresh_new:
        note += f'，本轮 {fresh_new} 条翻译失败（暂未入库）'
    if stuck - fresh_new > 0:
        note += f'，{stuck - fresh_new} 条此前翻译失败（排队重试，暂未入库）'
    if held - stuck > 0:
        note += f'，{held - stuck} 条待翻译（译完再入库）'
    # 已经按原文入库的那一档:补译失败不影响"读得到",可库里躺的还是外文,
    # 说成「同步完成」照样是谎报 —— 实测那一轮日报的素材清单里就是英文标题。
    # 所以本轮新失败的照样判红,只是文案说清原文还在。
    if shipped_new:
        note += f'，本轮 {shipped_new} 条补译失败（库里仍是原文）'
    if shipped_stuck - shipped_new > 0:
        note += (f'，{shipped_stuck - shipped_new} 条此前补译失败'
                 f'（排队重试，库里仍是原文）')
    if shipped - shipped_stuck > 0:
        note += f'，{shipped - shipped_stuck} 条等待中文译文覆盖'
    # **只有本轮新失败才判红。** 这一轮确实有内容没能按中文进知识库,那不是
    # "稍后再说"。两类不算:①已经试满 MAX_TRIES、按原文入库的(不在扣留集合
    # 里,留着原文是设计好的退让);②此前失败、还排在队列里等重试的 —— 它们
    # 下一轮就会被优先取走(见 `translate.translate_pending` 的取数顺序),
    # 让它们把每一轮都染红,红点就不再指向任何一次真实故障。文案里照样如实
    # 列着,只是不判红。
    #
    # 补抓那两档同理只认本轮:口径、理由和出口规则(试满 `fulltext.MAX_TRIES`
    # 按壳入库)跟翻译一模一样,所以两边共用 `_new_failures`。
    bad = fresh_new + shipped_new
    fetch_bad = fetch_fresh_new + fetch_shipped_new
    fails = errors + ([] if not bad else (translated or {}).get('errors') or [])
    # 补抓的失败明细也要进 failures:它是面板卡片里可展开的那一份,只写进运行
    # 日志的话用户看到的是一句没有信息的「1 条原文抓取失败」,点进去什么都没有。
    fails += [] if not fetch_bad else (fetched or {}).get('errors') or []
    _stage('export', 'error' if errors or bad or fetch_bad else 'ok', note, failures=fails)
