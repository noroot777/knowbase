"""频道列表完成后，按预算补齐新增/过期元数据；估算数据不进入正式排名。"""

import contextlib
import math
import time
from datetime import datetime, timedelta, timezone

from kb.youtube import admission, media, ranking, storage, throttle
from kb.youtube.settings import of


TTL_SECONDS = 6 * 3600
RUN_SECONDS = 120.0
VIDEO_SECONDS = 30.0
RETRY_SECONDS = 300
PENDING = '等待补齐真实发布时间与播放量，暂不参与排名'
STALE = '元数据超过 6 小时，等待刷新后重新排名'


def fresh(row, now=None):
    checked = row['entry'].get('metadata_checked_at')
    return (not row['entry'].get('approximate') and isinstance(checked, (int, float))
            and not isinstance(checked, bool) and math.isfinite(checked)
            and 0 <= (time.time() if now is None else now) - checked <= TTL_SECONDS)


def defer(row, reason, retry_at=None):
    storage.defer_metadata([(row, reason, retry_at)])


def calibrated(row, metadata):
    count = metadata.get('view_count')
    if (isinstance(count, bool) or not isinstance(count, (int, float))
            or not math.isfinite(count) or not 0 <= count <= 2 ** 63 - 1):
        return None
    original = row['entry']
    entry = admission.calibrate({**row, 'entry': {**original, 'views': 0, 'description': ''}}, metadata)
    if entry is None:
        return None
    entry.update(views=int(count), metadata_checked_at=time.time())
    if isinstance(metadata.get('title'), str) and metadata['title'].strip():
        entry['title'] = metadata['title'].strip()
    return entry


def refresh(rows, settings, scope, deadline, stopped, observe=None, progress=None):
    policy = of(settings)
    now = datetime.now(timezone.utc)
    floor = now - timedelta(days=policy['window_days'])
    stats = {'reused': 0, 'refreshed': 0, 'pending': 0, 'failed': 0, 'ttl_seconds': TTL_SECONDS}
    accepted, queue, errors, metadata_by_id = set(), [], [], {}
    positions = {}
    source_ids = {owner['source_id'] for row in rows for owner in row['owners']}
    for row in rows:
        if row.get('excluded'):
            continue
        representative, _, _ = ranking.qualify(row, policy, scope, now, floor)
        if representative is None:
            accepted.add(row['video_id'])
        elif fresh(row):
            accepted.add(row['video_id'])
            stats['reused'] += 1
        else:
            source_id = representative['source_id']
            position = row['entry'].get('listing_position', positions.get(source_id, 0))
            positions[source_id] = positions.get(source_id, 0) + 1
            queue.append((not row['entry'].get('approximate', False), position, source_id, row))
    pending = {item[3]['video_id']: item[3] for item in queue}
    queue.sort(key=lambda item: (*item[:3], item[3]['video_id']))
    for position, (_, _, _, row) in enumerate(queue, 1):
        if stopped() or time.monotonic() >= deadline or throttle.notice():
            break
        if row['retry_at'] > time.time() and not scope.force:
            continue
        if storage.permanently_excluded(row):
            pending.pop(row['video_id'], None)
            continue
        if progress:
            progress(position, len(queue))
        try:
            with observe(row) if observe else contextlib.nullcontext():
                metadata = media.probe(row, settings, min(deadline, time.monotonic() + VIDEO_SECONDS),
                                       stopped, force=True)
            if stopped() or storage.permanently_excluded(row):
                if storage.permanently_excluded(row):
                    pending.pop(row['video_id'], None)
                continue
            entry = calibrated(row, metadata)
            if entry is None:
                defer(row, PENDING, time.time() + RETRY_SECONDS)
                continue
            if storage.refresh_metadata(row, entry):
                accepted.add(row['video_id'])
                metadata_by_id[row['video_id']] = metadata
                pending.pop(row['video_id'], None)
                stats['refreshed'] += 1
        except throttle.NotAttempted:
            break
        except media.Interrupted:
            defer(row, PENDING, time.time() + RETRY_SECONDS)
        except Exception as error:
            storage.failed(row, error)
            throttle.caught(error)
            errors.append((row, error))
            stats['failed'] += 1
    failed_ids = {row['video_id'] for row, error in errors}
    storage.defer_metadata([
        (row, throttle.notice() or (PENDING if row['entry'].get('approximate') else STALE), None)
        for row in pending.values() if row['video_id'] not in failed_ids])
    latest = storage.rows(list(source_ids))
    waiting = [row for row in latest if row['video_id'] in pending and not row.get('excluded')]
    stats['pending'] = len(waiting)
    current = [row for row in latest if row['video_id'] in accepted]
    for row in current:
        if row['video_id'] in metadata_by_id:
            row['_metadata'] = metadata_by_id[row['video_id']]
    return {'rows': current, 'stats': stats, 'errors': errors,
            'pending_sources': {owner['source_id'] for row in waiting for owner in row['owners']}}
