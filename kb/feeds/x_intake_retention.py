"""Five-day work eligibility, a 24-hour expiry notice, and bounded terminal receipts."""
from datetime import timedelta
from zoneinfo import ZoneInfo
from kb.feeds.x_intake_capacity import WINDOW


def expire(conn, source_id, identity, floor, now):
    from kb.pipeline_retention import history_keep_days
    automatic_floor = (now - WINDOW).isoformat()
    retained_floor = (now - WINDOW - timedelta(days=history_keep_days())).isoformat()
    conn.execute('''INSERT OR IGNORE INTO x_intake_expirations(source_id,identity,post_id,expired_at)
        SELECT l.source_id,l.identity,l.post_id,? FROM x_intake_links l
        JOIN x_intake_candidates p USING(post_id) WHERE l.source_id=? AND l.identity=?
        AND l.delivered=0 AND p.published>=? AND ((p.state IN ('pending','repair','expired') AND p.published<?)
        OR (p.state='ready' AND l.confirmed=0 AND p.published<?))''',
                 (now.isoformat(), source_id, identity, retained_floor, automatic_floor, floor))
    conn.execute('''UPDATE x_intake_candidates SET state='expired',reason='超出自动追补五天窗口'
        WHERE published<? AND post_id IN (SELECT post_id FROM x_intake_links WHERE source_id=? AND identity=?)
        AND (state IN ('pending','repair') OR state='ready' AND NOT EXISTS
        (SELECT 1 FROM x_intake_links l WHERE l.post_id=x_intake_candidates.post_id AND l.confirmed=1))''',
                 (automatic_floor, source_id, identity))


def recent(conn, source_id, identity, now):
    return conn.execute('''SELECT count(*) FROM x_intake_expirations
        WHERE source_id=? AND identity=? AND expired_at>=?''',
                        (source_id, identity, (now - timedelta(hours=24)).isoformat())).fetchone()[0]


def notice(floor, count):
    date = floor.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
    return f'{date} 以前的 {count} 条待办最近24小时超出自动窗口，未计为完成' if count else ''


def prune(conn, now):
    from kb.pipeline_retention import history_keep_days
    cutoff = (now - WINDOW - timedelta(days=history_keep_days())).isoformat()
    conn.execute('DELETE FROM x_intake_links WHERE source_id NOT IN (SELECT id FROM subscriptions)')
    conn.execute('''DELETE FROM x_intake_candidates WHERE published<? AND NOT
        (state='ready' AND EXISTS (SELECT 1 FROM x_intake_links l
        WHERE l.post_id=x_intake_candidates.post_id AND l.confirmed=1 AND l.delivered=0))''', (cutoff,))
    conn.execute('DELETE FROM x_intake_links WHERE post_id NOT IN (SELECT post_id FROM x_intake_candidates)')
    conn.execute('DELETE FROM x_intake_batch_posts WHERE post_id NOT IN (SELECT post_id FROM x_intake_candidates)')
    conn.execute('''DELETE FROM x_intake_batches WHERE end<?
        AND NOT EXISTS (SELECT 1 FROM x_intake_batch_posts m WHERE m.batch_id=x_intake_batches.id)
        OR source_id NOT IN (SELECT id FROM subscriptions)''', (cutoff,))
    conn.execute('DELETE FROM x_intake_batch_posts WHERE batch_id NOT IN (SELECT id FROM x_intake_batches)')
    conn.execute('DELETE FROM x_intake_sources WHERE source_id NOT IN (SELECT id FROM subscriptions)')
    conn.execute('''DELETE FROM x_intake_expirations WHERE expired_at<?
        OR source_id NOT IN (SELECT id FROM subscriptions)''',
                 ((now - timedelta(days=history_keep_days())).isoformat(),))
