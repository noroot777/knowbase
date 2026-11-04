"""Complete only the recent members of the frozen 2026-11-03 backlog."""

import argparse
import atexit
import json
import time
from collections import Counter
from datetime import datetime, timezone

from youtube_backlog_20261103 import AUDIT, connection, load, save

from kb.collectors import FetchScope
from kb.config import SOURCES, VAULT_ROOT
from kb.feeds import registry as reg
from kb.feeds.sync import export
from kb.maintenance import maintenance_lock
from kb.youtube import admission, collector, media, products, storage, throttle
from kb.youtube.settings import of


def status(video_id):
    with connection() as current:
        raw = current.execute('SELECT * FROM youtube_videos WHERE video_id=?', (video_id,)).fetchone()
        if raw is None:
            return None, None
        row = dict(raw)
        row['entry'] = json.loads(row['entry'])
        formal = current.execute("SELECT * FROM feed_items WHERE kind='youtube' AND url=?",
                                 (row['entry']['url'],)).fetchone()
        return row, dict(formal) if formal else None


def publish(video_id):
    with maintenance_lock(), reg.db() as current:
        current.execute('BEGIN IMMEDIATE')
        rows = {row['video_id']: row for row in storage.rows(connection=current)}
        row = rows.get(video_id)
        if row is None or row['state'] != 'ready' or not products.valid(row):
            return 0
        added = 0
        for owner in row['owners']:
            source = current.execute('SELECT * FROM subscriptions WHERE id=? AND kind=? AND url=? AND enabled=1',
                                     (owner['source_id'], 'youtube', owner['source_url'])).fetchone()
            if source:
                added += products.publish(current, dict(source), row)
                storage.mark_committed(current, dict(source), video_id)
        return added


def summary():
    return {'sources': 0, 'checked': 0, 'added': 0, 'deferred': 0, 'unconfigured': 0,
            'skipped': 0, 'message': '', 'skip_reasons': [], 'errors': [], 'partial': [],
            'notices': [], 'repointed': 0, 'cautions': {}, 'stopped': False, 'failed_ids': [],
            'succeeded_ids': [], 'discovered': 0, 'ranked': 0, 'processed': 0, 'walls': {}}


def degraded(row):
    return bool(row and row['note'] and
                json.loads(row['note']).get('body_provenance', '').endswith('-fallback'))


#: 一批交给 `collector.process` 几条。设置里原先那个 `max_videos` 已经拆掉
#: (`settings.RETIRED`),这里沿用它当时的默认值,批次大小不变。
BATCH = 3


def process(budget, max_attempts):
    manifest, dates = load('manifest.json'), load('dates.json')
    results = load('processing.json') if (AUDIT / 'processing.json').exists() else {}
    target_ids = {video_id for video_id, fact in dates.items() if fact['classification'] == 'recent'}
    frozen = {row['video_id']: row for row in manifest['targets']}
    settings = reg.get_settings('youtube')
    policy = of(settings)
    scope = FetchScope(since=manifest['lower'][:10], until=manifest['upper'][:10], force=True)
    deadline = time.monotonic() + budget
    queue = sorted(target_ids, key=lambda video_id: (-frozen[video_id]['score'], video_id))
    notice = ''
    with throttle.opened():
        while queue and deadline - time.monotonic() >= 30 and not throttle.notice():
            selected, queue = queue[:BATCH], queue[BATCH:]
            work = []
            for video_id in selected:
                previous = results.get(video_id, {})
                if previous.get('complete') or previous.get('terminal') or previous.get('attempts', 0) >= max_attempts:
                    continue
                row, formal = status(video_id)
                if row and row['state'] == 'ready' and products.valid(row):
                    publish(video_id)
                    fallback = degraded(row)
                    results[video_id] = {**previous, 'complete': not fallback, 'terminal': fallback,
                                         'translation_degraded': fallback, 'deduplicated': bool(formal),
                                         'error': 'Published explicit translation fallback; full Chinese content unresolved' if fallback else '',
                                         'attempts': previous.get('attempts', 0)}
                    save('processing.json', results)
                    continue
                if row is None:
                    results[video_id] = {**previous, 'terminal': True, 'error': 'Frozen recent candidate missing'}
                    continue
                if formal:
                    results[video_id] = {**previous, 'terminal': True,
                                         'error': 'Existing formal item lacks a valid current certificate; preserved'}
                    continue
                work.append(video_id)
            if not work:
                save('processing.json', results)
                continue
            started = time.monotonic()
            report = summary()
            with maintenance_lock():
                current_rows = {row['video_id']: row for row in storage.rows()}
                batch = []
                for video_id in work:
                    row = current_rows.get(video_id)
                    if row is None:
                        continue
                    try:
                        metadata = media.probe(row, settings, min(deadline, time.monotonic() + 30), lambda: False)
                        published = admission.moment(metadata)
                        if published is None or not datetime.fromisoformat(manifest['lower']) <= published <= datetime.fromisoformat(manifest['upper']):
                            raise ValueError('Fresh metadata no longer proves membership in the frozen window')
                        if row['entry'].get('approximate'):
                            entry = admission.calibrate(row, metadata)
                            storage.recalibrate(video_id, entry)
                            row['entry'] = entry
                        row.update(_metadata=metadata, score_rank=1, retry_at=0)
                        batch.append(row)
                    except Exception as error:
                        throttle.caught(error)
                        previous = results.get(video_id, {})
                        results[video_id] = {**previous, 'attempts': previous.get('attempts', 0) + 1,
                                             'error': str(error)[:1200], 'retryable': True}
                failures = collector.process(batch, settings, scope, report,
                                             min(deadline, time.monotonic() + policy['run_seconds'])) if batch else {}
                for row in batch:
                    video_id = row['video_id']
                    current, formal = status(video_id)
                    previous = results.get(video_id, {})
                    complete = bool(current and current['state'] == 'ready' and products.valid(current))
                    if complete:
                        publish(video_id)
                    fallback = complete and degraded(current)
                    deferred = bool(current and current['state'] == 'deferred' and
                                    ('本轮数量预算' in current['reason'] or
                                     '本轮累计视频时长预算' in current['reason'] or
                                     '时间预算用尽' in current['reason']))
                    terminal = bool(current and current['gate_code'] in
                                    ('duration_budget', 'replay', 'shorts')) or fallback
                    error = ('Published explicit translation fallback; full Chinese content unresolved' if fallback else
                             '' if complete else current['reason'] if deferred else
                             (current['last_error'] or current['reason']) if current else 'missing')
                    results[video_id] = {'complete': complete and not fallback, 'terminal': terminal,
                                         'translation_degraded': fallback, 'scheduling_deferred': deferred,
                                         'attempts': previous.get('attempts', 0) + (0 if deferred else 1),
                                         'state': current['state'] if current else 'missing',
                                         'gate_code': current['gate_code'] if current else '',
                                         'error': error,
                                         'retry_at': current['retry_at'] if current else 0,
                                         'checked_at': datetime.now(timezone.utc).isoformat()}
                save('processing.json', results)
            record = {'ids': work, 'seconds': round(time.monotonic() - started, 2),
                      'summary': report, 'failures': failures}
            with (AUDIT / 'processing-batches.jsonl').open('a') as output:
                output.write(json.dumps(record, ensure_ascii=False) + '\n')
            print('PROCESS_BATCH ' + json.dumps({'ids': work, 'processed': report['processed'],
                                                'seconds': record['seconds']}), flush=True)
            deliver()
            if queue:
                time.sleep(1)
        notice = throttle.notice()
    save('processing-status.json', {'known_recent': len(target_ids),
                                   'complete': sum(bool(item.get('complete')) for item in results.values()),
                                   'remaining_unvisited': len(target_ids - results.keys()),
                                   'unresolved': len(target_ids) - sum(bool(item.get('complete')) for item in results.values()),
                                   'throttle': notice})
    print('PROCESS_DONE ' + json.dumps(load('processing-status.json')), flush=True)


def deliver():
    manifest = load('manifest.json')
    target_urls = {row['entry']['url'] for row in manifest['targets']}
    with maintenance_lock(), connection() as current:
        pending = current.execute("SELECT url FROM feed_items WHERE kind='youtube' AND exported_hash<>content_hash").fetchall()
        unrelated = [row['url'] for row in pending if row['url'] not in target_urls]
        if unrelated:
            raise RuntimeError('Unrelated YouTube exports pending; refusing a broad export')
        written = export('youtube', hold_untranslated=True)
    return len(written or ())


def index():
    from kb import store
    from kb.chunker import split_chunks
    from kb.embedder import embed_texts, shutdown
    from kb.scanner import _doc_from_file
    from kb.search import Filters, search

    atexit.register(shutdown)
    manifest = load('manifest.json')
    target_urls = {row['entry']['url'] for row in manifest['targets']}
    source = next(source for source in SOURCES if source.name == 'youtube')
    results = {}
    with maintenance_lock(), connection() as current, store.connect() as catalog:
        items = [dict(row) for row in current.execute("SELECT * FROM feed_items WHERE kind='youtube'")
                 if row['url'] in target_urls]
        for item in items:
            if not item['path'] or item['exported_hash'] != item['content_hash']:
                results[item['url']] = {'error': 'Formal item is not fully exported'}
                continue
            doc = _doc_from_file(VAULT_ROOT / item['path'], source)
            if not store.observed_document_is_current(catalog, doc):
                store.upsert_document(catalog, doc, split_chunks(doc.body), reg.now())
            catalog.commit()
            pending = catalog.execute('''SELECT id,text FROM chunks WHERE path=?
                AND id NOT IN (SELECT chunk_id FROM chunk_vec) ORDER BY id''', (doc.path,)).fetchall()
            for start in range(0, len(pending), 8):
                batch = pending[start:start + 8]
                vectors = embed_texts([chunk['text'] for chunk in batch], batch_size=8)
                store.insert_vectors(catalog, [chunk['id'] for chunk in batch], vectors)
                catalog.commit()
            hits = search(catalog, doc.title, filters=Filters(source='youtube'), top_k=100, use_vector=False)
            found = any(hit.path == doc.path for hit in hits)
            if not found:
                raise RuntimeError(f'FTS search failed for {doc.path}')
            results[item['url']] = {'path': doc.path, 'fts_found': found, 'vectors_added': len(pending),
                                    'body_chars': len(doc.body), 'export_hash_matches': True}
            save('index-verification.json', results)
    print(f'INDEX_DONE verified={len(results)}', flush=True)


def reconcile():
    manifest, dates = load('manifest.json'), load('dates.json')
    cleanup = load('cleanup.json') if (AUDIT / 'cleanup.json').exists() else {}
    processing = load('processing.json') if (AUDIT / 'processing.json').exists() else {}
    index_results = load('index-verification.json') if (AUDIT / 'index-verification.json').exists() else {}
    outcomes = {}
    with connection() as current:
        formal = {row['url']: dict(row) for row in current.execute("SELECT * FROM feed_items WHERE kind='youtube'")}
        before = {row['url']: row for row in manifest['formal_before']}
        for row in manifest['targets']:
            video_id, url = row['video_id'], row['entry']['url']
            fact = dates.get(video_id, {})
            deleted = cleanup.get(video_id, {}).get('outcome') == 'deleted'
            item = formal.get(url)
            candidate, _ = status(video_id)
            certified = bool(candidate and candidate['state'] == 'ready' and products.valid(candidate))
            if deleted:
                assert not current.execute('SELECT 1 FROM youtube_videos WHERE video_id=?', (video_id,)).fetchone()
                assert not current.execute('SELECT 1 FROM youtube_video_sources WHERE video_id=?', (video_id,)).fetchone()
                outcome = 'deleted_old'
            elif certified and not degraded(candidate) and item and item['path'] and item['exported_hash'] == item['content_hash'] and index_results.get(url, {}).get('fts_found'):
                outcome = 'already_formal' if url in before else 'new_formal'
            else:
                outcome = 'unresolved'
            outcomes[video_id] = {'outcome': outcome, 'url': url, 'title': row['entry']['title'],
                                  'formal_present': bool(item), 'translation_degraded': degraded(candidate) if certified else False,
                                  'date': fact, 'processing': processing.get(video_id, {}),
                                  'cleanup': cleanup.get(video_id, {})}
        for url, original in before.items():
            assert url in formal and all(formal[url][key] == original[key] for key in
                                         ('key', 'body', 'content_hash', 'exported_hash', 'path'))
    counts = dict(Counter(item['outcome'] for item in outcomes.values()))
    assert sum(counts.values()) == manifest['count']
    report = {'lower': manifest['lower'], 'upper': manifest['upper'], 'total': manifest['count'],
              'counts': counts, 'date_counts': dict(Counter(item.get('classification', 'unvisited') for item in dates.values())),
              'unknown_dates': sum(not item['date'] or item['date'].get('classification') == 'unknown' for item in outcomes.values()),
              'published_translation_fallbacks': sum(item['translation_degraded'] and item['formal_present'] for item in outcomes.values()),
              'formal_youtube_total': len(formal), 'protected_formal_before': len(before), 'outcomes': outcomes}
    save('reconciliation.json', report)
    print('RECONCILE ' + json.dumps({key: value for key, value in report.items() if key != 'outcomes'}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('process', 'deliver', 'index', 'reconcile'))
    parser.add_argument('--budget', type=float, default=3000)
    parser.add_argument('--attempts', type=int, default=2)
    args = parser.parse_args()
    if load('manifest.json')['task_id'] != '5aRnd8NVGVZj':
        raise RuntimeError('Unexpected audit scope')
    if args.phase == 'process':
        process(args.budget, args.attempts)
    else:
        globals()[args.phase]()


if __name__ == '__main__':
    main()
