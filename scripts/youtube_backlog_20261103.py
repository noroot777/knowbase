"""One-off, resumable production backlog audit for ash task 5aRnd8NVGVZj."""

import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from kb.config import DATA_DIR, DB_PATH
from kb.feeds import registry as reg
from kb.maintenance import maintenance_lock
from kb.youtube import admission, candidate_metadata, media, storage, throttle
from kb.youtube.ranking import stamp


AUDIT = DATA_DIR / 'audits' / 'youtube-backlog-5aRnd8NVGVZj'
START = '2026-11-03T15:41:08+08:00'
LOWER = '2026-10-29T00:00:00+08:00'


def save(name, payload):
    AUDIT.mkdir(parents=True, exist_ok=True)
    path = AUDIT / name
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def load(name):
    return json.loads((AUDIT / name).read_text())


def connection():
    current = sqlite3.connect(reg.DB_PATH.resolve().as_uri() + '?mode=ro', uri=True)
    current.row_factory = sqlite3.Row
    return current


def freeze():
    if (AUDIT / 'manifest.json').exists():
        print('FROZEN manifest already exists; keeping original scope', flush=True)
        return
    with maintenance_lock(), connection() as current:
        run = dict(current.execute('SELECT * FROM feed_runs WHERE id=219').fetchone())
        reference = datetime.fromisoformat(run['finished_at']).timestamp()
        rows = storage.rows(connection=current)
        ranked = [row for row in rows if row['score_rank'] > 0 and row['state'] != 'ready'
                  and not row['excluded']]
        targets = [row for row in ranked if not candidate_metadata.fresh(row, reference)]
        newly_stale = [row['video_id'] for row in ranked
                       if candidate_metadata.fresh(row, reference)
                       and not candidate_metadata.fresh(row)]
        if len(targets) != 699:
            raise RuntimeError(f'Expected reviewed reconstruction of 699, got {len(targets)}; no writes')
        AUDIT.mkdir(parents=True, exist_ok=True)
        for source, name in ((reg.DB_PATH, 'feeds.before.sqlite3'), (DB_PATH, 'index.before.sqlite3')):
            if source.exists():
                with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as original:
                    with sqlite3.connect(AUDIT / name) as backup:
                        original.backup(backup)
        formal = [dict(row) for row in current.execute("SELECT * FROM feed_items WHERE kind='youtube'")]
        ownership = [dict(row) for row in current.execute('SELECT * FROM youtube_video_sources')
                     if row['video_id'] in {target['video_id'] for target in targets}]
        manifest = {
            'task_id': '5aRnd8NVGVZj', 'started_at': START, 'lower': LOWER, 'upper': START,
            'frozen_at': datetime.now(timezone.utc).isoformat(), 'run': run,
            'selection': 'Current active, nonexcluded, score_rank>0, non-ready rows failing '
                         'candidate_metadata.fresh at feed_runs[219].finished_at; '
                         'run summary has no saved IDs, so this is a reconstruction, not proof '
                         'of exact historical ID identity. Newly stale rows are excluded.',
            'count': len(targets), 'approximate': sum(bool(row['entry'].get('approximate')) for row in targets),
            'states': dict(Counter(row['state'] for row in targets)), 'newly_stale_excluded': newly_stale,
            'ids_sha256': hashlib.sha256('\n'.join(sorted(row['video_id'] for row in targets)).encode()).hexdigest(),
            'targets': targets, 'ownership_before': ownership, 'formal_before': formal,
        }
        save('manifest.json', manifest)
        save('dates.json', {})
    print(f"FROZEN count={len(targets)} approximate={manifest['approximate']} newly_stale_excluded={len(newly_stale)}", flush=True)


def classify(published, precision='timestamp'):
    lower, upper = stamp(LOWER), stamp(START)
    if precision == 'upload_date':
        earliest, latest = published, published.timestamp() + 86400
        if earliest < lower <= datetime.fromtimestamp(latest, timezone.utc):
            return 'unknown'
        if earliest <= upper < datetime.fromtimestamp(latest, timezone.utc):
            return 'unknown'
    if published < lower:
        return 'old'
    return 'recent' if published <= upper else 'after_upper'


def cached_metadata(row):
    path = DATA_DIR / 'youtube' / row['video_id'] / 'metadata.json'
    metadata = media.read_json(path)
    if isinstance(metadata, dict) and metadata.get('id') == row['video_id'] and admission.moment(metadata):
        return metadata, str(path)
    return None, ''


def dates(budget):
    manifest, results = load('manifest.json'), load('dates.json')
    settings = reg.get_settings('youtube')
    deadline = time.monotonic() + budget
    with throttle.opened():
        for row in manifest['targets']:
            video_id = row['video_id']
            if video_id in results:
                continue
            if time.monotonic() >= deadline or throttle.notice():
                break
            record = {'checked_at': datetime.now(timezone.utc).isoformat()}
            try:
                metadata, evidence = cached_metadata(row)
                if metadata is None and not row['entry'].get('approximate'):
                    published = stamp(row['entry'].get('published'))
                    evidence = 'frozen non-approximate entry from production exact discovery'
                    precision = 'timestamp'
                else:
                    if metadata is None:
                        metadata = media.probe(row, settings, min(deadline, time.monotonic() + 30), lambda: False)
                        evidence = str(DATA_DIR / 'youtube' / video_id / 'metadata.json')
                    published = admission.moment(metadata)
                    precision = 'timestamp' if metadata.get('timestamp') or metadata.get('release_timestamp') else 'upload_date'
                if published is None:
                    raise ValueError('No reliable publication date in video metadata')
                record.update(published=published.isoformat(), precision=precision,
                              classification=classify(published, precision), evidence=evidence)
                if record['classification'] == 'unknown':
                    record['error'] = 'Day-only metadata overlaps frozen boundary; exact timestamp required'
            except Exception as error:
                throttle.caught(error)
                record.update(classification='unknown', error=str(error)[:1200], retryable=True)
            results[video_id] = record
            save('dates.json', results)
            if len(results) % 50 == 0:
                print(f'DATE_PROGRESS {len(results)}/{manifest["count"]}', flush=True)
            time.sleep(1)
    save('date-status.json', {'checked': len(results), 'total': manifest['count'],
                             'counts': dict(Counter(item['classification'] for item in results.values())),
                             'throttle': throttle.notice()})
    print('DATES_DONE ' + json.dumps(load('date-status.json'), ensure_ascii=False), flush=True)


def clean():
    manifest, verified = load('manifest.json'), load('dates.json')
    journal = load('cleanup.json') if (AUDIT / 'cleanup.json').exists() else {}
    if (AUDIT / 'cleanup.precommit.json').exists():
        with connection() as current:
            for video_id, record in load('cleanup.precommit.json').items():
                if record['outcome'] == 'deleted' and not current.execute(
                        'SELECT 1 FROM youtube_videos WHERE video_id=?', (video_id,)).fetchone():
                    journal[video_id] = record
    candidates = {row['video_id']: row for row in manifest['targets']}
    with maintenance_lock(), connection() as before:
        backup_name = 'feeds.pre-clean.sqlite3'
        if not (AUDIT / backup_name).exists():
            with sqlite3.connect(AUDIT / backup_name) as backup:
                before.backup(backup)
        with reg.db() as current:
            current.execute('BEGIN IMMEDIATE')
            for video_id, fact in verified.items():
                if fact['classification'] != 'old' or video_id in journal:
                    continue
                frozen = candidates[video_id]
                row = current.execute('SELECT * FROM youtube_videos WHERE video_id=?', (video_id,)).fetchone()
                if row is None:
                    journal[video_id] = {'outcome': 'missing_before_cleanup', **fact}
                    continue
                formal = current.execute("SELECT key FROM feed_items WHERE kind='youtube' AND url=?",
                                         (frozen['entry']['url'],)).fetchone()
                if row['state'] == 'ready' or formal or row['note'] or row['validation']:
                    journal[video_id] = {'outcome': 'protected_asset', **fact}
                    continue
                owners = current.execute('SELECT * FROM youtube_video_sources WHERE video_id=?', (video_id,)).fetchall()
                current.executemany('''INSERT INTO youtube_discovery_history(source_id,source_url,video_id,published)
                    VALUES(?,?,?,?) ON CONFLICT(source_id,video_id) DO UPDATE SET
                    source_url=excluded.source_url,published=excluded.published''',
                    [(owner['source_id'], owner['source_url'], video_id, fact['published']) for owner in owners])
                current.execute('DELETE FROM youtube_video_sources WHERE video_id=?', (video_id,))
                current.execute('DELETE FROM youtube_candidates WHERE video_id=?', (video_id,))
                current.execute('DELETE FROM youtube_videos WHERE video_id=?', (video_id,))
                journal[video_id] = {'outcome': 'deleted', 'owners': [dict(owner) for owner in owners], **fact}
            save('cleanup.precommit.json', journal)
        save('cleanup.json', journal)
    print('CLEAN_DONE ' + json.dumps(dict(Counter(row['outcome'] for row in journal.values()))), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('freeze', 'dates', 'clean'))
    parser.add_argument('--budget', type=float, default=3300)
    arguments = parser.parse_args()
    if not reg.DB_PATH.samefile(Path('/Users/fjh/code/knowledge-base/data/feeds.db')):
        raise RuntimeError('Explicit production KB_DATA_DIR is required')
    if arguments.phase == 'dates':
        dates(arguments.budget)
    else:
        globals()[arguments.phase]()


if __name__ == '__main__':
    main()
