"""Durable scan memberships and recovery; receipts never depend on mutable result entries."""
from uuid import uuid4


def begin(conn, source_id, identity, boundary, end, batch_id=''):
    batch_id = batch_id or uuid4().hex
    conn.execute('''INSERT INTO x_intake_batches(id,source_id,identity,boundary,end)
        VALUES(?,?,?,?,?)''', (batch_id, source_id, identity, boundary, end))
    return batch_id


def add(conn, batch_id, post_id):
    conn.execute('INSERT OR IGNORE INTO x_intake_batch_posts(batch_id,post_id) VALUES(?,?)',
                 (batch_id, post_id))


def confirm(conn, source_id, identity, batch_id):
    row = conn.execute('SELECT * FROM x_intake_batches WHERE id=? AND source_id=? AND identity=?',
                       (batch_id, source_id, identity)).fetchone()
    if not row or row['state'] == 'expired':
        return False
    conn.execute('''UPDATE x_intake_links SET confirmed=1 WHERE source_id=? AND identity=?
        AND post_id IN (SELECT post_id FROM x_intake_batch_posts WHERE batch_id=?)''',
                 (source_id, identity, batch_id))
    conn.execute("UPDATE x_intake_batches SET state='confirmed' WHERE id=?", (batch_id,))
    return True


def recover(conn, source_id, identity, boundary, end):
    conn.execute('''UPDATE x_intake_links SET confirmed=1 WHERE source_id=? AND identity=?
        AND confirmed=0 AND post_id IN (SELECT post_id FROM x_intake_candidates
        WHERE published>=? AND published<=?)''', (source_id, identity, boundary, end))
    conn.execute('''UPDATE x_intake_batches SET state='confirmed' WHERE source_id=? AND identity=?
        AND state='pending' AND NOT EXISTS (SELECT 1 FROM x_intake_batch_posts m
        JOIN x_intake_links l ON l.post_id=m.post_id AND l.source_id=? AND l.identity=?
        WHERE m.batch_id=x_intake_batches.id AND l.confirmed=0)''',
                 (source_id, identity, source_id, identity))


def expire(conn, source_id, identity, floor):
    conn.execute('''UPDATE x_intake_batches SET state='expired' WHERE source_id=? AND identity=? AND state='pending'
        AND (end<? OR EXISTS (SELECT 1 FROM x_intake_batch_posts m WHERE m.batch_id=x_intake_batches.id)
        AND NOT EXISTS (SELECT 1 FROM x_intake_batch_posts m JOIN x_intake_candidates p USING(post_id)
        WHERE m.batch_id=x_intake_batches.id AND p.state!='expired' AND p.published>=?))''',
                 (source_id, identity, floor, floor))


def count(conn, source_id, identity, floor):
    return conn.execute('''SELECT count(*) FROM x_intake_links l JOIN x_intake_candidates p USING(post_id)
        WHERE l.source_id=? AND l.identity=? AND l.confirmed=0 AND l.delivered=0
        AND p.state IN ('ready','pending','repair') AND p.published>=?''',
                        (source_id, identity, floor)).fetchone()[0]
