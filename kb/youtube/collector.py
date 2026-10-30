import contextlib
import json
import time
from datetime import datetime, timedelta, timezone

from kb.collectors import Coverage
from kb.collectors.feeds import FeedCollector
from kb.feeds import registry as reg
from kb.feeds.sync import FAILURE_CAP, _claim_write, _save_failure, _within_history
from kb.maintenance import maintenance_lock
from kb.pipeline_flow import halted
from kb.pipeline_segments import note_retry_targets
from kb.pipeline_state import _stage
from kb.youtube import (candidate_metadata, diagnosis, discovery, login, media, notes,
                        products, ranking, storage, throttle)
from kb.youtube.admission import TEMPORARY, UNDATED, calibrate, gate
from kb.youtube.channels import config
from kb.youtube.settings import of


def stopped():
    """整轮停止,或者正在跑的这一类被提前结束(口径只有一份:`pipeline_flow.halted`)。"""
    return halted()


DISCOVERY_PER_SOURCE = 30.0
DISCOVERY_SPACING = 1.0
DISCOVERY_CEILING = 900.0
RANGE_DISCOVERY_CEILING = 1800.0


class _Told(RuntimeError):
    """交给主干那个共用出口之前,**把展示文本按统一口径定下来**。

    `_save_failure` 自己 `str(error)[:FAILURE_CAP]`,那一刀**从头裁**;而
    `discovery.merge` 的消息是两份各约 800 字的工具摘要拼起来的、轻松超过那道上限 ——
    审查实测:详细路径报超时、后路 flat 末尾报 `challenge solver` 和
    `Requested format is not available`,分档和完整诊断都对(`js_challenge`),写进库的
    频道 `last_error` 恰好 1200 字、决定性那两行一个字都没留下,面板上展开「查看失败原因」
    看到的只有先前那次超时和后路开头的普通上下文。**上一轮说那一刀「只是冗余带」是错的**。

    **主干那一侧不动**:它服务所有来源,而 `diagnostic` 这个约定今天只有 YouTube 有
    (主干不认识任何具体来源)。所以转换放在这条适配边界上 —— 过了 `shown` 之后文本
    一定不长于那道上限,主干那一刀就真的只剩冗余带了。

    `_save_failure` 还会读 `unavailable` / `notice`(「这几条取不到」那条提示走它),
    **原样转过去**:换一个异常对象把它们丢掉,那条提示就从面板上消失了。
    """

    def __init__(self, error):
        super().__init__(diagnosis.shown(error, FAILURE_CAP))
        for name in ('unavailable', 'notice'):
            if hasattr(error, name):
                setattr(self, name, getattr(error, name))


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


def caution(summary, source_ids, ledger, conclusion=''):
    """这一段里工具报过的那些话:记在账上,由收尾归并成一句话。

    **只认 YouTube 那个上游说的**(`diagnosis.witnessed`):ffmpeg、whisper-cli 的输出
    和登录态一点关系都没有,它们那句 `permission denied` 进了这套判据就成了「读不出
    浏览器的 cookie」,面板让人去钥匙串放行而问题在磁盘上。

    **不是失败**:取到的条目照旧收下、照旧出成品,只是并列说一句这一轮可能没带上登录态
    (口径同 `login.check` 的 `CAUTIONS` —— 那边是「成了,但这份成功可能不算数」)。
    升级成失败的话,一条 cookie 警告就能让这个频道颗粒无收,而它明明带回了东西。
    **按分档归并、不逐条堆原文**:同一个出口一次全中,逐条堆等于同一句话印 N 遍
    (口径同 `wall`)。认不出的档一个字都不说(见 `login.cautioned`)。
    """
    # **和这一步结论冲突的那一档不说**,口径与「检查登录态」同一份(`login.beside`)。
    for noise in diagnosis.witnessed(ledger):
        code = login.beside(conclusion, noise)
        if code in login.CAUTIONS:
            known = summary.setdefault('cautions', {}).setdefault(code, [])
            known.extend(str(source_id) for source_id in source_ids)
    # **这一步实际用的是哪一份 cookie 库,也从同一本账上读**(`login.arguments` 记的那条道)。
    # 同步这条路原先一个字都不说:面板那颗「检查登录态」会提醒没钉住,而真正抓东西的这一轮
    # 悄悄用了可能挑错的那一份 —— 而它才是「抓回来的内容到底带没带登录态」的那一轮。
    # 不过 `classify`:那条道上记的是我们自己这一侧的准备结果,不是 YouTube 说的话。
    if diagnosis.witnessed(ledger, diagnosis.COOKIES):
        known = summary.setdefault('cautions', {}).setdefault(login.UNPINNED, [])
        known.extend(str(source_id) for source_id in source_ids)


@contextlib.contextmanager
def heeded(summary, source_ids):
    """这一步开一本诊断账,**不管它最后成没成,账上有的都要交**(`finally`)。

    **「这条诊断有没有发生」不能由「后面那个产物成没成」推断。** 两头各错过一次:
    交账押在正常返回上时,解析不过、校验不过、转录失败、落库失败都能让它整段蒸发;
    而**记账**押在各个调用方自己的成功分支上时(列表 parse 之后、probe 写完缓存之后、
    字幕拿到产物之后),工具明明报了话、这一次却压根没记下来 —— 同一个漏法换个组合
    就又回来(审查三轮各实测到一种:面板上只剩「没有可用字幕」或者「一个没能归类的
    错误」,`cautions` 空、`last_notice` 也空,而根因是这一轮没带上登录态)。
    所以**记在 `media.run` 的完成边界上**(`diagnosis.record`),这儿只负责开账和交账。
    """
    with diagnosis.heard() as ledger:
        conclusion = ''
        try:
            yield ledger
        except Exception as error:
            conclusion = login.coded(error)
            raise
        finally:
            caution(summary, source_ids, ledger, conclusion)


def discovery_budget(count, per_source=None, ceiling=None):
    """这一轮发现的总额度。**歇的那几拍要算进去,不是只算频道自己的时间。**

    不算的话额度正好卡在 `count * DISCOVERY_PER_SOURCE` 上,而真实耗时还要多出
    `(count - 1)` 拍 —— 于是每一轮固定有最后几个频道被那道闸拦掉、静默进 `skipped`,
    而它们本该轮得到。至少留够一个频道的额度,
    `DISCOVERY_CEILING` 是上限(超出的下一轮接着查)。
    """
    per_source = DISCOVERY_PER_SOURCE if per_source is None else per_source
    ceiling = DISCOVERY_CEILING if ceiling is None else ceiling
    return min(ceiling, max(per_source, count * (per_source + DISCOVERY_SPACING)))


def process(rows, settings, scope, summary, deadline):
    """排名之后那一段:逐条探元数据、取字幕、出中文成品。

    **要在一轮里调它**(`throttle.opened`,`collect` 已经开好):「这一轮撞没撞上限流」
    是本轮共享的账,轮外调用拿不到那本账 —— 那时它只是没有熔断,不是熔断失效。
    """
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
        if notice := throttle.notice():
            # **这一轮已经有上游说了「请求过多」,就不要再探下一条。** 判据是本轮共享的
            # 熔断账(`throttle`),不是「刚才那个 except 看到了什么」—— 发现阶段撞上的那一次
            # 压根不经过这里:审查实测,频道 2 的 429 之后照旧去 probe 频道 1 的视频,
            # 把一条本来正常的视频也带成 `failed` 和退避。
            # **剩下的一律 `deferred`**:它们没毛病,只是没轮到,下一轮照旧回得来。
            summary['message'] = notice
            for pending in chosen[position:]:
                storage.update(pending['video_id'], state='deferred', reason=notice)
            break
        if not ready or attempts >= policy['max_videos'] or probes >= policy['max_videos'] * 3:
            storage.update(row['video_id'], state='deferred', reason=why or '本轮数量预算已用完，留待下轮')
            continue
        try:
            probes += 1
            # 取成功时工具报的那一句也要收上来(账本,见 `caution`)——「元数据取到了」和
            # 「这一轮可能没带上登录态」可以同时成立。**一步一本账、出了这一步就交账**
            # (`heeded` 的 `finally`):一本账跨两步的话同一条警告会被后面那一步再记一遍
            # (归并按频道去重,但档数不去重)。
            with heeded(summary, row.get('eligible_sources', [row['source_id']])):
                metadata = row.get('_metadata') or media.probe(
                    row, settings, deadline, stopped, scope.force or row['gate_code'] in TEMPORARY)
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
            # 字幕和音频**才真正用得上登录态**,所以这两步报的警告也要收上来(见 `caution`)。
            with heeded(summary, row.get('eligible_sources', [row['source_id']])):
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
        except throttle.NotAttempted as error:
            # **「这一次没发出去」不是这条视频的失败。** 不记 attempts、不推退避、不进
            # 「重试失败项」:元数据多半已经取到了,只是后面那个请求被本轮熔断挡住
            # (审查实测:页面上那条候选显示「处理失败,待重试」、频道显示「连接失败」,
            # 而 `last_error` 自己写着「没有再发起新的请求」,两边都各推了半小时退避)。
            # 当前这条连同剩下的一律 `deferred` —— 它们没毛病,下一轮照旧回得来。
            notice = throttle.notice(getattr(error, 'origin', None)) or str(error)
            summary['message'] = notice
            for deferred in chosen[position:]:
                storage.update(deferred['video_id'], state='deferred', reason=notice)
            break
        except Exception as error:
            storage.failed(row, error)
            affected = row.get('eligible_sources', [row['source_id']])
            wall(summary, affected, error)
            # 刚接住的这一条自己说是限流的话,先记进本轮再往下走(`throttle.caught` 是
            # 冗余带,正路在请求入口那两道闸上)。剩下的候选由循环头那一道收尾 ——
            # **收手这件事只有一个判据**,不在两处各写一遍。
            throttle.caught(error)
            for source_id in affected:
                # 展示用的那一份**两头都留**(`login.evidence`),别从一头裁。
                failures.setdefault(source_id, []).append(login.evidence(error, 500))
    # 撞在**最后一条**候选上时循环头没有下一次,那句话就得在这儿补上(没有别的东西可延后了)。
    if not summary['message']:
        summary['message'] = throttle.notice()
    if not ready and chosen:
        # **这句话不许盖掉已经写下的那条**:两件事可以同时成立,而「这一轮为什么停的」是
        # 人要先知道的那一件(审查实测:撞上 429 的那一轮 message 只剩「待配置模型」,面板上
        # 既看不出限流、`walls` 也是空的,看着像只差个模型配置)。并列着说,按因果排序。
        summary['message'] += ('；' if summary['message'] else '') + \
            '已完成轻量排名；入选视频待配置翻译和内容笔记模型：' + why
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
    """一轮 YouTube 同步。**熔断账的生命周期就是这一轮**(见 `throttle` 的模块说明)。"""
    with throttle.opened():
        return _round(scope)


def _round(scope):
    started = reg.now()
    sources = [source for source in reg.subscriptions('youtube')
               if source['enabled'] and reg.in_scope(source, scope.targets)]
    settings = reg.get_settings('youtube')
    summary = {'sources': len(sources), 'checked': 0, 'added': 0, 'deferred': 0,
               'unconfigured': 0, 'skipped': 0, 'message': '', 'skip_reasons': [],
               'errors': [], 'partial': [], 'notices': [], 'repointed': 0, 'cautions': {},
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
    per_source = 90.0 if scope.ranged else DISCOVERY_PER_SOURCE
    discovery_started = time.monotonic()
    ceiling = RANGE_DISCOVERY_CEILING if scope.ranged else DISCOVERY_CEILING
    discovery_deadline = discovery_started + discovery_budget(len(pending), per_source, ceiling)
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
            with heeded(summary, [source['id']]):
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
        except throttle.NotAttempted:
            # 口径同处理阶段那一道:这个频道**一次请求都没发出去**,不记失败、不推退避。
            # 它算「没轮到」—— 退回 `pending`,由下面那句「还有 N 个频道未轮到」收口,
            # 下一轮照旧回得来。记成失败的那一版把一条好订阅推进了半小时退避(审查实测)。
            pending = [source, *pending]
            break
        except Exception as error:
            wall(summary, [source['id']], error)
            _save_failure(source, _Told(error), summary)
            throttle.caught(error)
        summary['checked'] += 1
        _stage('crawl', 'running', f"YouTube · 频道列表 {summary['checked']}/{len(sources)} 个频道")
        # **撞上 429 就当场收手这一轮。** 限的是**出口**不是这个频道:接着查下去的话,
        # 剩下的频道每一个都再去撞一次同样的墙 —— 惩罚被这一轮自己加深、拖长,面板上
        # 多出几十条不是它们自己毛病的失败,它们还一起进了退避(下一轮想试都试不了)。
        # **判据是本轮的熔断账,不是这个 `except` 看到了什么**:审查实测两种漏法都走的是
        # 成功分支 —— 后路 flat 补上来的那一轮、以及先印出一条正常视频再 429 的那一轮,
        # 限流都只剩一句被丢弃的说明文字,而 `walls` 是空的。
        # **没轮到的那些一个字都不写**(口径同登录态不就绪那一道):它们没失败,只是
        # 没轮到,照旧进 `skipped`、下一轮接着查。撞上的那一个照常记失败和退避 ——
        # 它这一次确实没拿到东西,原文也得留得住。
        if throttle.tripped(throttle.YOUTUBE):
            break
    summary['skipped'] += len(pending)
    discovery_seconds = time.monotonic() - discovery_started
    rows = storage.rows([source['id'] for source in succeeded])
    metadata_started = time.monotonic()
    _stage('crawl', 'running', f"YouTube · 频道列表完成 {summary['checked']}/{len(sources)}，增量更新元数据")
    prepared = candidate_metadata.refresh(
        rows, settings, scope, metadata_started + candidate_metadata.RUN_SECONDS, stopped,
        observe=lambda row: heeded(summary, [owner['source_id'] for owner in row['owners']]),
        progress=lambda done, total: _stage('crawl', 'running', f'YouTube · 元数据更新 {done}/{total}'))
    summary['metadata'] = prepared['stats']
    failures = {}
    for source_id, errors in prepared['source_errors'].items():
        for error in errors:
            wall(summary, [source_id], error)
            failures.setdefault(source_id, []).append(login.evidence(error, 500))
    ranked = ranking.score(prepared['rows'], of(settings), scope)
    summary['ranked'] = len(ranked)
    processing_started = time.monotonic()
    summary['timings'] = {'discovery_seconds': round(discovery_seconds, 2),
                          'metadata_seconds': round(processing_started - metadata_started, 2)}
    selected_ids = {row['video_id'] for row in ranking.select(ranked, of(settings), scope.force)}
    waiting_metadata = prepared['pending_ids'] - selected_ids
    for source_id, reasons in process(ranked, settings, scope, summary,
                                      processing_started + of(settings)['run_seconds']).items():
        failures.setdefault(source_id, []).extend(reasons)
    summary['timings']['processing_seconds'] = round(time.monotonic() - processing_started, 2)
    # **「取到了,但…」和「卡住了」分开说**:前者这一轮确实带回了东西,后者没有。
    # 混成一句会把一条 cookie 警告说得像整个频道失败了(口径见 `caution`)。
    # **要在落库之前归并**:订阅那条提醒(`last_notice`)取的就是这一刻的 `message`,
    # 放到后面去拼等于面板上那行字永远少了这一句(而它正是唯一的展示落点)。
    # **这句话不许自称「取到了内容」** —— 同一条警告在失败路上照样要说(见 `heeded`),
    # 而那时内容恰恰没拿到。只说得住的那半截:请求确实发出去了。
    # **同一个频道的同一档不说两遍**:那条警告已经成了这个频道的失败结论(`walls`)时,
    # 再并列说一句「但工具同时报了…」等于同一件事印两遍(口径同 `wall` 的归并)。
    # 账上那一份照旧留着(`summary['cautions']` 是事实,不是文案)。
    for code, ids in summary.get('cautions', {}).items():
        # 变量名别叫 `pending` —— 外面那个「还没轮到的频道」正叫这个名字,一赋值就把
        # 收尾那句「撞上 429 后提前收手，还有 N 个频道未轮到」整段吃掉(回归当场红)。
        unsaid = {str(source_id) for source_id in ids} - set(summary.get('walls', {}).get(code, []))
        if not unsaid:
            continue
        summary['message'] += ('；' if summary['message'] else '') + \
            f"{len(unsaid)} 个频道这一轮发出过请求，" + login.cautioned(code, gateway['browser'])
    for source in succeeded:
        outstanding = (source['id'] in prepared['pending_sources'] or
                       any(not row['excluded'] and row['video_id'] not in waiting_metadata and
                           (row['last_error'] or row['state'] in ('deferred', 'processing'))
                           for row in storage.rows([source['id']])))
        completed = storage.ready(source)
        if scope.ranged:
            completed = [row for row in completed if scope.since <= row['entry']['published'][:10] <= scope.until]
        try:
            if not commit_notes(source, completed, summary, outstanding, scope.ranged):
                summary['repointed'] += 1
                continue
            if source['id'] in failures:
                _save_failure(source, _Told(RuntimeError('；'.join(failures[source['id']]))), summary)
            elif outstanding:
                summary['deferred'] += 1
            else:
                summary['succeeded_ids'].append(str(source['id']))
        except Exception as error:
            wall(summary, [source['id']], error)
            _save_failure(source, _Told(error), summary)
            throttle.caught(error)
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
        # 按熔断账判,不按 `walls` 判:后路补上来的那一轮和部分成功那一轮都没有异常落进
        # `walls`,而这一轮确实是撞上 429 之后提前收的手 —— 照 `walls` 判会说成「还有 N 个
        # 频道未轮到」,听起来像预算用完了。
        summary['message'] += ('；撞上 429 后这一轮提前收手，' if throttle.tripped(throttle.YOUTUBE)
                               else '；') + \
            f'还有 {len(pending)} 个频道未轮到，留待下轮轻量检查'
    with reg.db() as connection:
        connection.execute('INSERT INTO feed_runs(kind,started_at,finished_at,summary) VALUES(?,?,?,?)',
                           ('youtube', started, reg.now(), json.dumps(summary, ensure_ascii=False)))
        connection.execute("DELETE FROM feed_runs WHERE kind='youtube' AND id NOT IN "
                           "(SELECT id FROM feed_runs WHERE kind='youtube' ORDER BY id DESC LIMIT 30)")
    note_retry_targets('youtube', summary['failed_ids'], reg.now())
    message = (f"YouTube · 检查 {summary['checked']}/{len(sources)} 个频道，"
               f"排名 {summary['ranked']} 条，处理 {summary['processed']} 条，入库 {summary['added']} 条")
    timing, metadata = summary['timings'], summary['metadata']
    message += (f"；列表发现 {timing['discovery_seconds']:g} 秒；元数据更新 {timing['metadata_seconds']:g} 秒"
                f"（复用 {metadata['reused']} 条缓存，新查 {metadata['refreshed']} 条，"
                f"待更新 {metadata['pending']} 条，失败 {metadata['failed']} 条）；"
                f"视频深加工 {timing['processing_seconds']:g} 秒")
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
