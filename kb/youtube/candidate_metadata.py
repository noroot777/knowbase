"""频道列表完成后按预算补元数据；未轮到的候选保留预排资格。"""

import contextlib
import math
import time
from datetime import datetime, timedelta, timezone

from kb.youtube import admission, login, media, ranking, storage, throttle
from kb.youtube.settings import of


TTL_SECONDS = 6 * 3600
RUN_SECONDS = 120.0
VIDEO_SECONDS = 30.0
RETRY_SECONDS = 300
PENDING = storage.METADATA_PENDING
STALE = storage.METADATA_STALE


def fresh(row, now=None):
    checked = row['entry'].get('metadata_checked_at')
    return (not row.get('metadata_pending') and not row['entry'].get('approximate') and isinstance(checked, (int, float))
            and not isinstance(checked, bool) and math.isfinite(checked)
            and 0 <= (time.time() if now is None else now) - checked <= TTL_SECONDS)


def defer(row, reason, retry_at=None):
    storage.defer_metadata([(row, reason, retry_at)], attempted=True)


def calibrated(row, metadata):
    count = metadata.get('view_count')
    if (isinstance(count, bool) or not isinstance(count, (int, float))
            or not math.isfinite(count) or not 0 <= count <= 2 ** 63 - 1):
        return None
    original = row['entry']
    entry = admission.calibrate({**row, 'entry': {**original, 'views': 0, 'description': ''}}, metadata)
    if entry is None:
        return None
    entry.update(views=int(count))
    if isinstance(metadata.get('title'), str) and metadata['title'].strip():
        entry['title'] = metadata['title'].strip()
    return entry


def source_failures(errors, eligible):
    failed_ids = {row['video_id'] for row, error in errors}
    failures = {}
    for row, error in errors:
        for owner in row['owners']:
            source_id = owner['source_id']
            if (login.coded(error) in login.AUTH_CODES or
                    eligible.get(source_id) and eligible[source_id] <= failed_ids):
                failures.setdefault(source_id, []).append(error)
    return failures


def refresh(rows, settings, scope, deadline, stopped, observe=None, progress=None):
    policy = of(settings)
    now = datetime.now(timezone.utc)
    floor = now - timedelta(days=policy['window_days'])
    stats = {'reused': 0, 'refreshed': 0, 'pending': 0, 'failed': 0, 'ttl_seconds': TTL_SECONDS}
    queue, errors, metadata_by_id = [], [], {}
    positions, eligible = {}, {}
    source_ids = {owner['source_id'] for row in rows for owner in row['owners']}
    for row in rows:
        if row.get('excluded'):
            continue
        representative, _, _ = ranking.qualify(row, policy, scope, now, floor)
        if representative is None or row['state'] == 'excluded':
            continue
        for source_id in representative['eligible_sources']:
            eligible.setdefault(source_id, set()).add(row['video_id'])
        if row['state'] == 'ready':
            continue
        if fresh(row):
            stats['reused'] += 1
        else:
            source_id = representative['source_id']
            position = row['entry'].get('listing_position', positions.get(source_id, 0))
            positions[source_id] = positions.get(source_id, 0) + 1
            queue.append((not row['entry'].get('approximate', False), position, source_id, row))
    pending = {item[3]['video_id']: item[3] for item in queue}
    unfinished = {video_id for video_id, row in pending.items()
                  if row.get('metadata_pending') or row['last_error'] or row['state'] in ('failed', 'processing')}
    not_reached, cancelled = set(pending) - unfinished, False
    queue.sort(key=lambda item: (*item[:3], item[3]['video_id']))
    for position, (_, _, _, row) in enumerate(queue, 1):
        cancelled |= stopped()
        if cancelled or throttle.notice():
            unfinished.update(pending)
            not_reached.clear()
            break
        if time.monotonic() >= deadline:
            break
        if row['retry_at'] > time.time() and not scope.force:
            continue
        if storage.permanently_excluded(row):
            pending.pop(row['video_id'], None)
            continue
        not_reached.discard(row['video_id'])
        unfinished.add(row['video_id'])
        if progress:
            progress(position, len(queue))
        try:
            with observe(row) if observe else contextlib.nullcontext():
                metadata = media.probe(row, settings, min(deadline, time.monotonic() + VIDEO_SECONDS),
                                       stopped, force=scope.force)
            cancelled |= stopped()
            if cancelled:
                break
            if storage.permanently_excluded(row):
                pending.pop(row['video_id'], None)
                continue
            entry = calibrated(row, metadata)
            if entry is None:
                defer(row, PENDING, time.time() + RETRY_SECONDS)
                continue
            if storage.refresh_metadata(row, entry):
                metadata_by_id[row['video_id']] = metadata
                pending.pop(row['video_id'], None)
                stats['refreshed'] += 1
        except throttle.NotAttempted:
            unfinished.update(pending)
            not_reached.clear()
            break
        except media.Interrupted:
            defer(row, PENDING, time.time() + RETRY_SECONDS)
        except Exception as error:
            storage.failed(row, error)
            throttle.caught(error)
            errors.append((row, error))
            stats['failed'] += 1
    cancelled |= stopped()
    if cancelled or throttle.notice():
        unfinished.update(pending)
        not_reached.clear()
    failed_ids = {row['video_id'] for row, error in errors}
    storage.defer_metadata([
        (row, throttle.notice() or (PENDING if row['entry'].get('approximate') else STALE), None)
        for row in pending.values() if row['video_id'] not in failed_ids])
    latest = storage.rows(list(source_ids))
    waiting = [row for row in latest if row['video_id'] in pending and not row.get('excluded')
               and row['state'] not in ('ready', 'excluded')]
    waiting_ids = {row['video_id'] for row in waiting}
    unfinished.update(row['video_id'] for row in waiting
                      if row.get('metadata_pending') or row['last_error'] or row['state'] in ('failed', 'processing'))
    unfinished &= waiting_ids
    not_reached &= waiting_ids - unfinished
    stats['pending'] = len(waiting)
    current = [row for row in latest if not row.get('excluded')]
    for row in current:
        if row['video_id'] in metadata_by_id:
            row['_metadata'] = metadata_by_id[row['video_id']]
        elif row['video_id'] in pending:
            row['_metadata_reason'] = PENDING if row['entry'].get('approximate') else STALE
    failures = source_failures(errors, eligible)
    return {'rows': current, 'stats': stats, 'errors': errors,
            'pending_ids': waiting_ids, 'not_reached_ids': not_reached,
            'unfinished_ids': unfinished, 'stopped': cancelled,
            'source_errors': failures, 'pending_sources': set(failures)}
