"""Transactional candidate/progress writes; only proven complete bodies reach feed_items."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kb.feeds import registry as reg, x_article, x_body, x_post, x_post_meta
from kb.feeds import x_intake_batches as batches
from kb.feeds import x_intake_retention as retention
from kb.maintenance import maintenance_lock


def db():
    return reg.db(timeout=0)


def read_db(timeout=0):
    return reg.read_db(timeout=timeout)


def utc(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (stamp.replace(tzinfo=ZoneInfo('Asia/Shanghai')) if stamp.tzinfo is None else stamp).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def identity(source):
    return json.dumps([source['url'], source.get('feed_url', ''), source.get('rev', 0)])


def post_id(entry):
    match = re.search(r'/status/(\d+)(?:[/?#]|$)', entry.get('url', ''))
    return match.group(1) if match else ''


def complete(entry):
    if x_post.HINT in entry or x_article.HINT in entry:
        return False
    reason, _ = x_post_meta.eligibility(entry)
    return (reason == 'article_merged' or reason == '' and not x_article.needs_article(entry)) and x_body.parts(entry).source_known


def current(conn, source):
    row = conn.execute('SELECT * FROM subscriptions WHERE id=?', (source['id'],)).fetchone()
    return row is not None and identity(dict(row)) == identity(source)


def floor_for(source, now):
    floor = now - timedelta(days=5)
    if boundary := utc(source.get('history_floor')):
        floor = max(floor, boundary)
    return floor


def observed(entries):
    stamps = [utc(entry.get('x_observed_at') or entry.get('published')) for entry in entries]
    stamps.append(getattr(entries, 'skipped_newest', None))
    latest = max((stamp for stamp in stamps if stamp), default=None)
    return latest.isoformat() if latest else ''


def prepare(sources, channel, now):
    with maintenance_lock(wait=0), db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        retention.prune(conn, now)
        for source in sources:
            if not current(conn, source):
                continue
            floor = floor_for(source, now).isoformat()
            previous = conn.execute('SELECT * FROM x_intake_sources WHERE source_id=?',
                                    (source['id'],)).fetchone()
            if previous and (previous['identity'], previous['channel']) != (identity(source), channel):
                conn.execute('''DELETE FROM x_intake_batch_posts WHERE batch_id IN
                    (SELECT id FROM x_intake_batches WHERE source_id=?)''', (source['id'],))
                conn.execute('DELETE FROM x_intake_batches WHERE source_id=?', (source['id'],))
                conn.execute('DELETE FROM x_intake_expirations WHERE source_id=?', (source['id'],))
                conn.execute('DELETE FROM x_intake_links WHERE source_id=?', (source['id'],))
                conn.execute('DELETE FROM x_intake_sources WHERE source_id=?', (source['id'],))
                previous = None
            conn.execute('''INSERT INTO x_intake_sources(source_id,identity,channel,floor,gap)
                VALUES(?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                floor=excluded.floor,discovery='unattempted',error='' ''',
                (source['id'], identity(source), channel, floor, '最近五天尚未确认完整覆盖'))
            retention.expire(conn, source['id'], identity(source), floor, now)
            batches.expire(conn, source['id'], identity(source), floor)
            expired = retention.recent(conn, source['id'], identity(source), now)
            outside = retention.notice(utc(floor), expired)
            conn.execute('UPDATE x_intake_sources SET expired=?,outside=? WHERE source_id=?',
                         (expired, outside, source['id']))


def excluded(conn, entry):
    identifier = post_id(entry)
    return conn.execute('''SELECT 1 FROM feed_item_exclusions WHERE kind='social' AND
        (key=? OR url LIKE ?)''', (reg.item_key('social', entry['url']), f'%/status/{identifier}')).fetchone() is not None


def existing(conn, entry):
    row = conn.execute('''SELECT * FROM feed_items WHERE kind='social' AND (key=? OR url LIKE ?)''',
                       (reg.item_key('social', entry['url']), f'%/status/{post_id(entry)}')).fetchone()
    cached = conn.execute("SELECT entry FROM x_intake_candidates WHERE post_id=? AND state='ready'",
                          (post_id(entry),)).fetchone()
    proof = json.loads(cached['entry']) if cached else entry
    if row and complete(dict(row)) and complete(proof) and all(row[field] == proof.get(field) for field in
                                                               ('title', 'body', 'x_post_meta', 'body_provenance')):
        return dict(row)
    return None


def needs_repair(conn, entry):
    row = conn.execute('SELECT * FROM feed_items WHERE key=?', (reg.item_key('social', entry['url']),)).fetchone()
    return bool(row and any(row[field] != entry.get(field) for field in ('title', 'body', 'x_post_meta', 'body_provenance')))


def stage(source, entries, *, phase, incomplete, observed='', now=None, boundary='', end='', batch_id=''):
    reused = 0
    now = time.time() if now is None else now
    with maintenance_lock(wait=0), db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not current(conn, source):
            return None
        progress = conn.execute('SELECT * FROM x_intake_sources WHERE source_id=?', (source['id'],)).fetchone()
        batch_id = batches.begin(conn, source['id'], identity(source), boundary or progress['floor'],
                                 end or datetime.now(timezone.utc).isoformat(), batch_id)
        for entry in entries:
            identifier, published = post_id(entry), utc(entry.get('x_observed_at') or entry.get('published'))
            if not identifier or not published or published < utc(progress['floor']):
                continue
            cached = conn.execute('SELECT * FROM x_intake_candidates WHERE post_id=?', (identifier,)).fetchone()
            saved = existing(conn, entry)
            blocked = excluded(conn, entry)
            state = 'excluded' if blocked else 'ready' if saved or complete(entry) else 'pending'
            if state == 'ready' and needs_repair(conn, saved or entry):
                state = 'repair'
            if saved:
                entry = saved
                reused += 1
            if cached and cached['state'] == 'unavailable' and not blocked:
                reused += 1
            elif cached and cached['state'] == 'ready' and complete(json.loads(cached['entry'])) and not blocked:
                reused += not bool(saved)
            else:
                conn.execute('''INSERT INTO x_intake_candidates(post_id,entry,published,state)
                    VALUES(?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET
                    entry=excluded.entry,published=excluded.published,state=excluded.state''',
                    (identifier, json.dumps(entry, ensure_ascii=False), published.isoformat(), state))
            conn.execute('INSERT OR IGNORE INTO x_intake_links(source_id,identity,post_id) VALUES(?,?,?)',
                         (source['id'], identity(source), identifier))
            batches.add(conn, batch_id, identifier)
        latest = max(filter(None, [progress['latest'], observed]), default='')
        covered = progress['covered']
        gap = incomplete or '时间线响应尚待会话收尾确认'
        column = 'scan_at' if phase == 'discovery' else 'history_at'
        discovery = ('partial' if incomplete else 'complete') if phase == 'discovery' else progress['discovery']
        conn.execute(f'''UPDATE x_intake_sources SET latest=?,covered=?,gap=?,discovery=?,
            {column}=?,error='' WHERE source_id=?''',
            (latest, covered, gap, discovery, now, source['id']))
    return reused


def confirm(source, entries, *, phase, incomplete=None, observed, boundary, end='', coverage_complete=False,
            batch_id=''):
    with maintenance_lock(wait=0), db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not current(conn, source):
            return False
        if batch_id:
            if not batches.confirm(conn, source['id'], identity(source), batch_id):
                return False
        else:
            for entry in entries:
                conn.execute('UPDATE x_intake_links SET confirmed=1 WHERE source_id=? AND identity=? AND post_id=?',
                             (source['id'], identity(source), post_id(entry)))
        progress = conn.execute('SELECT * FROM x_intake_sources WHERE source_id=?', (source['id'],)).fetchone()
        covered = progress['covered']
        coverage_end = progress['coverage_end']
        proven = coverage_complete is True and incomplete == ''
        if proven:
            connected = coverage_end and utc(boundary) <= utc(coverage_end)
            covered = min(filter(None, [covered, boundary])) if connected else boundary
            coverage_end = max(coverage_end, end or observed or boundary)
            if phase == 'history' or phase == 'recovery':
                batches.recover(conn, source['id'], identity(source), boundary, end or observed or boundary)
        gap = incomplete or ('' if proven and covered and utc(covered) <= utc(progress['floor']) else
                             '最近五天仍有历史缺口' if proven else '时间线完整性尚未确认')
        discovery = 'complete' if proven else 'partial' if phase == 'discovery' else progress['discovery']
        conn.execute('UPDATE x_intake_sources SET covered=?,coverage_end=?,gap=?,discovery=?,latest=? WHERE source_id=?',
                     (covered, coverage_end, gap, discovery,
                      max(progress['latest'], observed), source['id']))
    return True


def progress(source):
    with read_db() as conn:
        row = conn.execute('SELECT * FROM x_intake_sources WHERE source_id=? AND identity=?',
                           (source['id'], identity(source))).fetchone()
        result = dict(row) if row else {}
        if row:
            result['unconfirmed'] = batches.count(conn, source['id'], identity(source), row['floor'])
    return result


def attempt(source, phase, reason=''):
    column = 'scan_at' if phase == 'discovery' else 'history_at'
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if current(conn, source):
            conn.execute(f'UPDATE x_intake_sources SET {column}=?,error=? WHERE source_id=? AND identity=?',
                         (time.time(), reason, source['id'], identity(source)))
            if phase == 'discovery':
                conn.execute("UPDATE x_intake_sources SET discovery='partial' WHERE source_id=?", (source['id'],))


def pending(sources, now=None):
    allowed = {(source['id'], identity(source)): source for source in sources}
    now = time.time() if now is None else now
    with read_db() as conn:
        rows = conn.execute('''SELECT p.*,l.source_id,l.identity FROM x_intake_candidates p
            JOIN x_intake_links l USING(post_id) JOIN x_intake_sources s ON s.source_id=l.source_id
            AND s.identity=l.identity WHERE p.state='pending' AND p.retry_at<=?
            AND p.published>=s.floor ORDER BY p.attempted,p.post_id''', (now,)).fetchall()
    groups, seen = {}, set()
    for row in rows:
        source = allowed.get((row['source_id'], row['identity']))
        if source and row['post_id'] not in seen:
            seen.add(row['post_id'])
            groups.setdefault(source['id'], []).append((source, dict(row)))
    result = []
    while groups:
        for source_id in list(groups):
            result.append(groups[source_id].pop(0))
            if not groups[source_id]:
                del groups[source_id]
    return result


def finish(source, task, entries, unavailable, reason):
    with maintenance_lock(wait=0), db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not current(conn, source):
            return
        progress = conn.execute('SELECT floor FROM x_intake_sources WHERE source_id=? AND identity=?',
                                (source['id'], identity(source))).fetchone()
        if not progress or utc(task['published']) < utc(progress['floor']):
            return
        entry = next((entry for entry in entries if complete(entry)), None)
        state = 'excluded' if excluded(conn, json.loads(task['entry'])) else (
            'ready' if entry else 'unavailable' if unavailable else 'pending')
        if state == 'ready' and needs_repair(conn, entry):
            state = 'repair'
            reason = '存量正文缺少同版完整证明，需走版本化回填；未覆盖旧正文'
        failures = task['failures'] + (state == 'pending')
        conn.execute('''UPDATE x_intake_candidates SET entry=?,state=?,attempted=?,failures=?,
            retry_at=?,reason=? WHERE post_id=? AND entry=? AND state='pending' AND failures=?''',
            (json.dumps(entry, ensure_ascii=False) if entry else task['entry'], state, time.time(), failures,
             time.time() + min(3600, 60 * 2 ** min(failures, 6)) if state == 'pending' else 0,
             reason[:500], task['post_id'], task['entry'], task['failures']))


def delivery(source):
    with read_db() as conn:
        rows = conn.execute('''SELECT p.*,l.delivered,l.confirmed FROM x_intake_candidates p
            JOIN x_intake_links l USING(post_id) WHERE l.source_id=? AND l.identity=?''',
            (source['id'], identity(source))).fetchall()
        progress = conn.execute('SELECT floor,expired FROM x_intake_sources WHERE source_id=? AND identity=?',
                                (source['id'], identity(source))).fetchone()
    entries, pending_count, unavailable, expired = [], 0, [], progress['expired'] if progress else 0
    for row in rows:
        entry = json.loads(row['entry'])
        if row['state'] == 'ready' and row['confirmed'] and not row['delivered'] and complete(entry):
            entries.append(entry)
        pending_count += row['state'] in ('pending', 'repair')
        if row['state'] == 'unavailable':
            unavailable.append(dict(url=entry['url'], reason=row['reason']))
    return entries, pending_count, unavailable, expired


def acknowledge(conn, source, entries):
    if not current(conn, source):
        return False
    for entry in entries:
        conn.execute('UPDATE x_intake_links SET delivered=1 WHERE source_id=? AND identity=? AND post_id=?',
                     (source['id'], identity(source), post_id(entry)))
    return True


def statuses(sources, channel='browser'):
    with read_db(timeout=30) as conn:
        rows = {row['source_id']: dict(row) for row in conn.execute('SELECT * FROM x_intake_sources')}
        counts = {(row['source_id'], row['identity']): row['pending'] for row in conn.execute('''
            SELECT l.source_id,l.identity,count(*) AS pending FROM x_intake_links l
            JOIN x_intake_candidates p USING(post_id) WHERE p.state IN ('pending','repair')
            GROUP BY l.source_id,l.identity''')}
        unconfirmed = {source_id: batches.count(conn, source_id, row['identity'], row['floor'])
                       for source_id, row in rows.items()}
        for source_id, row in rows.items():
            row['expired'] = retention.recent(conn, source_id, row['identity'], datetime.now(timezone.utc))
            row['outside'] = retention.notice(utc(row['floor']), row['expired'])
    for source in sources:
        row = rows.get(source['id'])
        if row and row['identity'] == identity(source) and row['channel'] == ('browser' if channel == 'attach' else channel):
            source['x_intake'] = {key: row[key] for key in
                                  ('discovery', 'gap', 'expired', 'error', 'floor', 'scan_at', 'history_at', 'outside')}
            source['x_intake']['pending'] = counts.get((source['id'], identity(source)), 0)
            source['x_intake']['unconfirmed'] = unconfirmed[source['id']]
            from kb.feeds.x_intake_diagnostics import issues
            source['x_intake']['issues'] = issues(source['x_intake'])


def save_metrics(started, metrics):
    with db() as conn:
        conn.execute('INSERT INTO x_intake_runs(started,metrics) VALUES(?,?)',
                     (started, json.dumps(metrics, ensure_ascii=False)))
        conn.execute('DELETE FROM x_intake_runs WHERE id NOT IN (SELECT id FROM x_intake_runs ORDER BY id DESC LIMIT 30)')


def latest_metrics(channel=None):
    with read_db(timeout=30) as conn:
        row = conn.execute('SELECT * FROM x_intake_runs ORDER BY id DESC LIMIT 1').fetchone()
    metrics = dict(started=row['started'], **json.loads(row['metrics'])) if row else None
    return metrics if metrics and (channel is None or metrics.get('channel') == channel) else None
