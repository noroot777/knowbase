import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from kb.collectors import Coverage
from kb.collectors.feeds import FeedCollector
from kb.feeds import registry as reg
from kb.feeds.sync import _claim_write, _save_failure, _within_history
from kb.maintenance import maintenance_lock
from kb.pipeline_segments import note_retry_targets
from kb.pipeline_state import _stage, _state
from kb.youtube import discovery, media, notes, ranking, storage
from kb.youtube.channels import config
from kb.youtube.settings import of


def stopped():
    return bool(_state.get('stop_requested'))


def gate(metadata, settings):
    duration = metadata.get('duration')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        return '没有可靠时长，不开始下载或转录'
    if metadata.get('live_status') in ('is_live', 'is_upcoming', 'was_live', 'post_live') or metadata.get('is_live') or metadata.get('was_live'):
        return '直播、预告或直播回放不进入自动处理'
    if '/shorts/' in str(metadata.get('webpage_url', '')) or metadata.get('media_type') == 'shorts':
        return 'Shorts 不进入自动处理'
    if duration > settings['max_video_minutes'] * 60:
        return '超过单条视频时长预算'
    return ''


def process(rows, settings, scope, summary, deadline):
    policy = of(settings)
    ready, why = notes.available()
    chosen = ranking.select(rows, policy, scope.force)
    failures = {}
    spent = 0
    attempts = 0
    probes = 0
    for row in chosen:
        if stopped() or time.monotonic() >= deadline:
            summary['stopped'] = stopped()
            summary['message'] = '视频处理已停止或时间预算用尽，未完成的内容留待下轮'
            break
        if not ready or attempts >= policy['max_videos'] or probes >= policy['max_videos'] * 3:
            storage.update(row['video_id'], state='deferred', reason=why or '本轮数量预算已用完，留待下轮')
            continue
        try:
            probes += 1
            metadata = media.probe(row, deadline, stopped)
            if reason := gate(metadata, policy):
                storage.update(row['video_id'], state='deferred' if '预算' in reason else 'excluded', reason=reason)
                continue
            duration = float(metadata['duration'])
            storage.update(row['video_id'], duration=duration)
            if spent + duration > policy['max_minutes'] * 60:
                storage.update(row['video_id'], state='deferred', reason='超过本轮累计视频时长预算')
                continue
            spent += duration
            attempts += 1
            storage.update(row['video_id'], state='processing', reason='正在读取字幕或音频，随后翻译为中文')
            _stage('crawl', 'running', f"YouTube · 处理 {attempts}/{policy['max_videos']} · {row['entry']['title']}")
            transcript = media.obtain(row, metadata, settings, deadline, stopped)
            entry = notes.build(row, transcript, deadline, stopped)
            storage.update(row['video_id'], state='ready', note=json.dumps(entry, ensure_ascii=False),
                           reason='中文内容笔记已完成', retry_at=0)
            summary['processed'] += 1
        except media.Interrupted as error:
            storage.update(row['video_id'], state='deferred', reason=str(error))
            summary['message'] = str(error)
            summary['stopped'] = stopped()
            break
        except Exception as error:
            storage.failed(row, error)
            failures.setdefault(row['source_id'], []).append(str(error)[:500])
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
            summary['added'] += reg.put_items(source, entries, conn=connection)
            urls = {entry['url'] for entry in entries}
            for row in completed:
                if row['entry']['url'] in urls:
                    connection.execute('UPDATE youtube_candidates SET committed=1 WHERE video_id=?', (row['video_id'],))
            if pending_failure:
                connection.execute('UPDATE subscriptions SET last_attempt=?,last_notice=? WHERE id=?',
                                   (reg.now(), summary['message'], source['id']))
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
               'discovered': 0, 'ranked': 0, 'processed': 0}
    pending = []
    for source in sources:
        if source['retry_at'] > time.time() and not scope.force:
            summary['deferred'] += 1
        elif not config(source).get('ranking_enabled', True):
            summary['skipped'] += 1
        else:
            pending.append(source)
    pending.sort(key=lambda source: (source['last_attempt'], source['id']))
    succeeded = []
    discovery_deadline = time.monotonic() + 90
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='youtube-feed') as pool:
        while pending and not stopped() and time.monotonic() + 20 <= discovery_deadline:
            batch, pending = pending[:4], pending[4:]
            futures = {pool.submit(discovery.fetch, source): source for source in batch}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    entries = future.result()
                    storage.discover(source, entries)
                    summary['discovered'] += len(entries)
                    succeeded.append(source)
                except Exception as error:
                    _save_failure(source, error, summary)
                summary['checked'] += 1
                _stage('crawl', 'running', f"YouTube · 轻量检查 {summary['checked']}/{len(sources)} 个频道")
    summary['skipped'] += len(pending)
    rows = storage.rows([source['id'] for source in succeeded])
    ranked = ranking.score(rows, of(settings), scope)
    summary['ranked'] = len(ranked)
    failures = process(ranked, settings, scope, summary, time.monotonic() + of(settings)['run_seconds'])
    unresolved = {row['source_id'] for row in storage.rows([source['id'] for source in succeeded])
                  if row['state'] == 'failed'}
    for source in succeeded:
        completed = storage.ready(source)
        if scope.ranged:
            completed = [row for row in completed if scope.since <= row['entry']['published'][:10] <= scope.until]
        try:
            if not commit_notes(source, completed, summary, source['id'] in unresolved, scope.ranged):
                summary['repointed'] += 1
                continue
            if source['id'] in failures:
                _save_failure(source, RuntimeError('；'.join(failures[source['id']])), summary)
            elif source['id'] in unresolved:
                summary['deferred'] += 1
            else:
                summary['succeeded_ids'].append(str(source['id']))
        except Exception as error:
            _save_failure(source, error, summary)
    summary['stopped'] |= stopped()
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
