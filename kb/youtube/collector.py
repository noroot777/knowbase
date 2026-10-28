import json
import time
from datetime import datetime, timedelta, timezone

from kb.collectors import Coverage
from kb.collectors.feeds import FeedCollector
from kb.feeds import registry as reg
from kb.feeds.sync import _claim_write, _save_failure, _within_history
from kb.maintenance import maintenance_lock
from kb.pipeline_flow import halted
from kb.pipeline_segments import note_retry_targets
from kb.pipeline_state import _stage
from kb.youtube import discovery, login, media, notes, products, ranking, storage
from kb.youtube.admission import TEMPORARY, UNDATED, calibrate, gate
from kb.youtube.channels import config
from kb.youtube.settings import of


def stopped():
    """整轮停止,或者正在跑的这一类被提前结束(口径只有一份:`pipeline_flow.halted`)。"""
    return halted()


#: 单个频道的发现预算。两条路按 `discovery.DETAIL_SHARE` 分这一份:带登录态逐条解析
#: 拿大头(单频道半分钟一档),估算列举拿剩下那点。
DISCOVERY_PER_SOURCE = 90.0
#: **两个频道之间歇这么久。借的是 daily-report 的参数**
#: (`fetch_yt_channels_v2.py` 的 `time.sleep(1)  # Rate limit`):那份每天跑 48 个频道、
#: 每个频道逐条解析 20 条,从没撞过 429。
DISCOVERY_SPACING = 1.0
#: 整轮发现的上限。**串行之后同样的 900 秒只够 ~20 个频道**(实测单频道 10 条约 43 秒),
#: 一轮覆盖不到一半 —— 翻到 1800 秒按实测能覆盖 ~40 个。剩下的照旧记进 `skipped`,
#: 下一轮接着查(`pending` 按 `last_attempt` 排序,轮着来;按 URL 去重,不会重复入库)。
DISCOVERY_CEILING = 1800.0


def wall(summary, source_ids, error):
    """把这次失败的分档记在账上。**分类只在拿着异常对象的这一刻做一次。**

    原先是事后拿 `summary['errors']` 里的文本重新 `classify` 一遍 —— 而那份文本一路被裁过
    三道(`process` 的 500、`_save_failure` 的 1200、异常消息自己的摘要),决定性那一行很可能
    已经不在里面了(审查实测:长诊断里的 429 这样丢掉,整批失败被归并成「机器人验证」,
    照它的提示去重新登录没有用)。`login.coded` 优先用 `ToolFailure` 在完整诊断上算好的那份。
    """
    code = login.coded(error)
    if code in login.AUTH_CODES:
        known = summary.setdefault('walls', {}).setdefault(code, [])
        known.extend(str(source_id) for source_id in source_ids)


def discovery_budget(count):
    """这一轮发现的总额度。**歇的那几拍要算进去,不是只算频道自己的时间。**

    不算的话额度正好卡在 `count * DISCOVERY_PER_SOURCE` 上,而真实耗时还要多出
    `(count - 1)` 拍 —— 于是每一轮固定有最后几个频道被那道闸拦掉、静默进 `skipped`,
    而它们本该轮得到。`max(90.0, ...)` 是只有一两个频道那一轮的地板,
    `DISCOVERY_CEILING` 是上限(超出的下一轮接着查)。
    """
    return min(DISCOVERY_CEILING, max(90.0, count * (DISCOVERY_PER_SOURCE + DISCOVERY_SPACING)))


def process(rows, settings, scope, summary, deadline):
    policy = of(settings)
    now = datetime.now(timezone.utc)
    floor = now - timedelta(days=policy['window_days'])
    ready, why = notes.available()
    chosen = ranking.select(rows, policy, scope.force)
    failures = {}
    spent = 0
    attempts = 0
    probes = 0
    for position, row in enumerate(chosen):
        if storage.permanently_excluded(row):
            continue
        if stopped() or time.monotonic() >= deadline:
            summary['stopped'] = stopped()
            summary['message'] = '视频处理已停止或时间预算用尽，未完成的内容留待下轮'
            for pending in chosen[position:]:
                storage.update(pending['video_id'], state='deferred', reason=summary['message'])
            break
        if not ready or attempts >= policy['max_videos'] or probes >= policy['max_videos'] * 3:
            storage.update(row['video_id'], state='deferred', reason=why or '本轮数量预算已用完，留待下轮')
            continue
        try:
            probes += 1
            metadata = media.probe(row, settings, deadline, stopped,
                                   scope.force or row['gate_code'] in TEMPORARY)
            if decision := gate(metadata, policy, row):
                storage.update(row['video_id'], state=decision.state, reason=decision.reason,
                               gate_code=decision.code, retry_at=time.time() + decision.retry_seconds)
                continue
            # ── 事实日期一到手就闭环:换掉估算值,再用**同一个** `rejection` 重判资格 ──
            # 排名那一轮是拿估出来的日子排的(后路只有频道页的列表顺序),所以指定日期
            # 范围、订阅历史下界、未来时间这几道当时判的都是估算值。**这一轮不许只判
            # 窗口**:审查实测,估 10-27 而实际 10-25 的那条,在 `since=until=10-27` 的
            # 补抓里一路走到成品,日期还是 10-27。
            # **`fallback=False` 挡不住这一类** —— 它只拦住这一轮新增估算候选,库里
            # 上一轮留下的那些照旧会被这一轮挑中。
            if row['entry'].get('approximate'):
                calibrated = calibrate(row, metadata)
                if calibrated is None:
                    storage.update(row['video_id'], state=UNDATED.state, reason=UNDATED.reason,
                                   gate_code=UNDATED.code, retry_at=time.time() + UNDATED.retry_seconds)
                    continue
                storage.recalibrate(row['video_id'], calibrated)
                # **重判要对每个归属都来一次**,而且走 `ranking.qualify` 那一份口径 ——
                # 只重判代表那一个归属的话,新订阅的历史下界会把这条视频对老订阅也一起
                # 判死(审查实测:老订阅单独判是合格的,结果全局 excluded、下一轮入选 0)。
                calibrated_row = {**row, 'entry': calibrated}
                representative, reasons, unknown = ranking.qualify(calibrated_row, policy, scope, now, floor)
                if representative is None:
                    # **日期这类判据是「这一轮不要」,不是「永久不要」。** 写成 `excluded`
                    # 的话,下一轮 `rejection` 那道 `state=='excluded' and not force` 会在
                    # 看日期之前就拒掉它 —— 用户把回看天数放宽、或换一天补抓,这条视频
                    # 再也回不来。`filtered` 才是「这一轮没进」:下一轮 `score()` 拿事实
                    # 日期重新判,该回来就回来。永久排除只由用户的移除动作产生。
                    # 校准之后 `approximate` 已经摘了,所以这里的拒绝一律判得准、
                    # `unknown` 必然是空的 —— 两个都取只是不留 IndexError 的口子。
                    storage.update(row['video_id'], state='filtered', gate_code='window',
                                   reason=(reasons or unknown)[0], score_rank=0)
                    continue
                row = representative
            duration = float(metadata['duration'])
            storage.update(row['video_id'], duration=duration)
            if storage.permanently_excluded(row):
                continue
            if spent + duration > policy['max_minutes'] * 60:
                storage.update(row['video_id'], state='deferred', reason='超过本轮累计视频时长预算')
                continue
            spent += duration
            attempts += 1
            storage.update(row['video_id'], state='processing', gate_code='', reason='正在读取字幕或音频，随后翻译为中文')
            _stage('crawl', 'running', f"YouTube · 处理 {attempts}/{policy['max_videos']} · {row['entry']['title']}")
            transcript = media.obtain(row, metadata, settings, deadline, stopped)
            if storage.permanently_excluded(row):
                continue
            entry = notes.build(row, transcript, deadline, stopped)
            storage.update(row['video_id'], state='ready', note=json.dumps(entry, ensure_ascii=False),
                           validation=products.certify(row, metadata, transcript, entry), repair_pending=0,
                           reason='中文内容笔记已完成', retry_at=0, last_error='')
            summary['processed'] += 1
        except media.Interrupted as error:
            for pending in chosen[position:]:
                storage.update(pending['video_id'], state='deferred', reason=str(error))
            summary['message'] = str(error)
            summary['stopped'] = stopped()
            break
        except Exception as error:
            storage.failed(row, error)
            affected = row.get('eligible_sources', [row['source_id']])
            wall(summary, affected, error)
            for source_id in affected:
                # 展示用的那一份**两头都留**(`login.evidence`),别从一头裁。
                failures.setdefault(source_id, []).append(login.evidence(error, 500))
    if not ready and chosen:
        summary['message'] = '已完成轻量排名；入选视频待配置翻译和内容笔记模型：' + why
        summary['unconfigured'] = len({row['source_id'] for row in chosen})
    summary['admitted'] = attempts
    summary['video_minutes'] = round(spent / 60, 1)
    return failures


def commit_notes(source, completed, summary, pending_failure=False, ranged=False):
    with maintenance_lock():
        entries = [json.loads(row['note']) for row in completed]
        if not ranged:
            entries = _within_history(source, entries)
        with reg.db() as connection:
            if not _claim_write(connection, source):
                return False
            current = connection.execute('SELECT enabled FROM subscriptions WHERE id=?', (source['id'],)).fetchone()
            if not current or not current['enabled']:
                return False
            urls = {entry['url'] for entry in entries}
            for row in completed:
                linked = connection.execute('SELECT 1 FROM subscription_items WHERE subscription_id=? AND item_key=?',
                    (source['id'], reg.item_key('youtube', row['entry']['url']))).fetchone()
                if row['entry']['url'] in urls or linked:
                    summary['added'] += products.publish(connection, source, row)
                    storage.mark_committed(connection, source, row['video_id'])
            if pending_failure:
                connection.execute('UPDATE subscriptions SET last_attempt=?,last_notice=? WHERE id=?',
                                   (reg.now(), summary['message'] or '视频仍有未完成处理或待重试错误', source['id']))
            else:
                connection.execute('''UPDATE subscriptions SET last_attempt=?,last_success=?,last_error='',
                    failures=0,retry_at=0,last_notice=? WHERE id=?''',
                                   (reg.now(), reg.now(), summary['message'], source['id']))
    return True


def collect(scope):
    started = reg.now()
    sources = [source for source in reg.subscriptions('youtube')
               if source['enabled'] and reg.in_scope(source, scope.targets)]
    settings = reg.get_settings('youtube')
    summary = {'sources': len(sources), 'checked': 0, 'added': 0, 'deferred': 0,
               'unconfigured': 0, 'skipped': 0, 'message': '', 'skip_reasons': [],
               'errors': [], 'partial': [], 'notices': [], 'repointed': 0,
               'stopped': False, 'failed_ids': [], 'succeeded_ids': [],
               'discovered': 0, 'ranked': 0, 'processed': 0, 'walls': {}}
    pending = []
    for source in sources:
        if source['retry_at'] > time.time() and not scope.force:
            summary['deferred'] += 1
        elif not config(source).get('ranking_enabled', True):
            summary['skipped'] += 1
        else:
            pending.append(source)
    pending.sort(key=lambda source: (source['last_attempt'], source['id']))
    # **登录态不就绪就不开跑。** 这一轮本来会让每个频道各撞一次同样的墙:面板上 48 条
    # 一模一样的失败、48 条退避,而原因只有一个、还在别处。**不写 last_error、不推退避** ——
    # 缺的是本机配置,不是这些订阅的毛病,配好之后该立刻能跑,不该先等退避过去。
    gateway = login.readiness(settings)
    if not gateway['ready'] and pending:
        summary['message'] = gateway['message']
        summary['unconfigured'] = len(pending)
        _stage('crawl', 'error', 'YouTube · ' + gateway['message'])
        with reg.db() as connection:
            connection.execute('INSERT INTO feed_runs(kind,started_at,finished_at,summary) VALUES(?,?,?,?)',
                               ('youtube', started, reg.now(), json.dumps(summary, ensure_ascii=False)))
        return summary
    succeeded = []
    # **一个频道一个频道地查,两个之间歇一拍 —— 不许并发。** 这条是撞过 429 之后按
    # daily-report 改的:它那份 `fetch_yt_channels.py` 起先也是 8 路并发(拿 RSS 地址),
    # 后来整个换成 `fetch_yt_channels_v2.py` 的串行 + `time.sleep(1)  # Rate limit`,
    # 自此每天 48 个频道、每个逐条解析 20 条都跑得通。我们这边原先 4 路并发、频道之间
    # 不歇,量比它小却撞上了 429 —— YouTube 限的是**出口的瞬时请求速率**,不是一天的总量,
    # 所以快不起来的地方只能用等来换。**不要为了快改回并发**:429 的报错落在登录态检查上
    # (见 `login.classify` 的 `rate_limited`),从面板上看像是「没登录」,追起来要绕一大圈。
    #
    # **发现的预算按主路真实的速度定。** 全程登录态之后每一轮都是逐条解析:单频道半分钟
    # 一档(实测每条约 4.3 秒,`discovery.DETAIL_LIMIT` 封在 10 条)。预算卡在 90 秒就只有
    # 头几个频道轮得到,其余静默进 `skipped`(面板上看不出区别)—— 所以按待查频道数算一个
    # 够用的额度(连歇的那几拍一起算进去),再用 `DISCOVERY_CEILING` 封顶。
    per_source = DISCOVERY_PER_SOURCE
    discovery_deadline = time.monotonic() + discovery_budget(len(pending))
    while pending and not stopped():
        # **第一个频道无条件放行。** 「剩下的时间够不够再查一个」这道闸只能拦后续的:
        # 额度正好是 `len(pending) * (per_source + spacing)` 时,这个判断在第一个上就差
        # 那几微秒 —— 结果是一轮一个频道都没查、全进 `skipped`,而面板上只说「部分频道
        # 未轮到」(只有一个待查频道的那一轮必然撞上,实测 checked=0)。
        if summary['checked'] and time.monotonic() + per_source > discovery_deadline:
            break
        if summary['checked']:
            # **歇在下一个频道之前,不是查完就歇** —— 否则最后一个频道后面那一拍纯属白等,
            # 而它正好压在「还来不来得及处理」那条预算上。歇完再看一眼停止请求:人点了
            # 停止还接着查下一个频道,那一下就等于没响应。
            time.sleep(DISCOVERY_SPACING)
            if stopped():
                break
        source, pending = pending[0], pending[1:]
        # 显式日期范围那一轮只禁**估算**那条路(第 2 条);带登录态逐条解析那条拿的是
        # 真实日期,照旧可以走(见 `discovery.fetch`)。
        # **每个频道一份自己的 `notes`**:那是「列了几条之后中断」的说明,共写一份的话
        # 就说不出是哪个频道没列完。
        told = []
        try:
            entries = discovery.fetch(source, settings, per_source, not scope.ranged, told)
            storage.discover(source, entries)
            summary['discovered'] += len(entries)
            succeeded.append(source)
            # **「没列完」不是失败,但也不许静默**(口径同 `feeds.sync` 的 `partial`):
            # 列到的照旧入库、下一轮按 URL 去重地接着列,可「停在第 7 条」和「整个
            # 频道都列完了」在面板上不该长得一模一样。记成 error 更不对 —— 那会显示
            # 「连接失败」并让这条订阅进退避重试,而它这一轮明明带回了东西。
            for note in told:
                summary['partial'].append({'title': source['name'], 'reason': note})
        except Exception as error:
            wall(summary, [source['id']], error)
            _save_failure(source, error, summary)
        summary['checked'] += 1
        _stage('crawl', 'running', f"YouTube · 轻量检查 {summary['checked']}/{len(sources)} 个频道")
    summary['skipped'] += len(pending)
    rows = storage.rows([source['id'] for source in succeeded])
    ranked = ranking.score(rows, of(settings), scope)
    summary['ranked'] = len(ranked)
    failures = process(ranked, settings, scope, summary, time.monotonic() + of(settings)['run_seconds'])
    for source in succeeded:
        outstanding = any(not row['excluded'] and (row['last_error'] or row['state'] in ('deferred', 'processing'))
                          for row in storage.rows([source['id']]))
        completed = storage.ready(source)
        if scope.ranged:
            completed = [row for row in completed if scope.since <= row['entry']['published'][:10] <= scope.until]
        try:
            if not commit_notes(source, completed, summary, outstanding, scope.ranged):
                summary['repointed'] += 1
                continue
            if source['id'] in failures:
                _save_failure(source, RuntimeError('；'.join(failures[source['id']])), summary)
            elif outstanding:
                summary['deferred'] += 1
            else:
                summary['succeeded_ids'].append(str(source['id']))
        except Exception as error:
            wall(summary, [source['id']], error)
            _save_failure(source, error, summary)
    summary['stopped'] |= stopped()
    # **同一个原因不要在面板上重复一遍又一遍。** 登录态失效时每个频道都会报同样那句话,
    # 逐条读完才明白是一个问题 —— 这里归并成一句带频道数的话,逐条明细照旧留在 errors 里。
    # 按 `AUTH_CODES` 整个集合判,并且**按真正出现的那一档说话** —— 机器人验证、
    # cookie 读不出来、登录失效这三件事的下一步完全不同,归并不该把它们说成同一句。
    for code, ids in summary.get('walls', {}).items():
        summary['message'] += ('；' if summary['message'] else '') + \
            f"{len(set(ids))} 个频道卡在同一处：" + login.describe(code, gateway['browser'])
    if summary['partial']:
        summary['message'] += ('；' if summary['message'] else '') + \
            f"{len(summary['partial'])} 个频道没列完（{summary['partial'][0]['reason']}）"
    if pending:
        summary['message'] += '；部分频道未轮到，留待下轮轻量检查'
    with reg.db() as connection:
        connection.execute('INSERT INTO feed_runs(kind,started_at,finished_at,summary) VALUES(?,?,?,?)',
                           ('youtube', started, reg.now(), json.dumps(summary, ensure_ascii=False)))
        connection.execute("DELETE FROM feed_runs WHERE kind='youtube' AND id NOT IN "
                           "(SELECT id FROM feed_runs WHERE kind='youtube' ORDER BY id DESC LIMIT 30)")
    note_retry_targets('youtube', summary['failed_ids'], reg.now())
    message = (f"YouTube · 检查 {summary['checked']}/{len(sources)} 个频道，"
               f"排名 {summary['ranked']} 条，处理 {summary['processed']} 条，入库 {summary['added']} 条")
    if summary['errors']:
        message += f"；{len(summary['errors'])} 个频道处理失败，详情可重试"
    if summary['message']:
        message += '；' + summary['message']
    _stage('crawl', 'stopped' if summary['stopped'] else 'error' if summary['errors'] else 'ok',
           message, failures=summary['errors'])
    return summary


class YouTubeCollector(FeedCollector):
    def __init__(self, name):
        super().__init__(name)
        self.range_hint = '按视频发布时间筛选，仅检查频道当前 feed 提供的视频；不保证补齐完整历史。'

    def fetch(self, scope):
        summary = collect(scope)
        whole = not scope.targets and not any(summary[key] for key in
                    ('failed_ids', 'deferred', 'unconfigured', 'skipped', 'repointed', 'stopped'))
        return Coverage(targets=tuple(summary['succeeded_ids']), whole=whole)
