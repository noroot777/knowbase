from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from kb.feeds import registry as reg
from kb.maintenance import maintenance_lock
from kb.youtube.ranking import stamp


INITIAL_DAYS = 3
TZ = ZoneInfo('Asia/Shanghai')


def initialize(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS youtube_sync_origins (
        source_id INTEGER PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
        source_url TEXT NOT NULL, initial_fetch_at TEXT NOT NULL)''')
    connection.execute('''CREATE TRIGGER IF NOT EXISTS youtube_sync_origin_reset
        AFTER UPDATE OF url,feed_url,mode ON subscriptions
        WHEN OLD.kind='youtube' AND (NEW.url<>OLD.url OR NEW.feed_url<>OLD.feed_url OR NEW.mode<>OLD.mode)
        BEGIN DELETE FROM youtube_sync_origins WHERE source_id=OLD.id; END''')


def prepare(source, scope, when=None):
    if scope.ranged:
        return source
    with maintenance_lock(), reg.db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        current = connection.execute('''SELECT * FROM subscriptions
            WHERE id=? AND kind='youtube' AND url=? AND enabled=1''',
            (source['id'], source['url'])).fetchone()
        if current is None:
            return source
        source = dict(current)
        origin = connection.execute('''SELECT initial_fetch_at FROM youtube_sync_origins
            WHERE source_id=? AND source_url=?''', (source['id'], source['url'])).fetchone()
        if origin and stamp(origin['initial_fetch_at']) is not None:
            return {**source, 'initial_fetch_at': origin['initial_fetch_at']}
        if stamp(source['last_success']) is not None:
            return source
        today = (when or datetime.now(timezone.utc)).astimezone(TZ).date()
        initial = datetime.combine(today - timedelta(days=INITIAL_DAYS), time.min, TZ).isoformat()
        connection.execute('''INSERT INTO youtube_sync_origins(source_id,source_url,initial_fetch_at)
            VALUES(?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                source_url=excluded.source_url,initial_fetch_at=excluded.initial_fetch_at''',
            (source['id'], source['url'], initial))
        return {**source, 'initial_fetch_at': initial}


def recent(source, entries, scope):
    floor = stamp(source.get('initial_fetch_at'))
    if scope.ranged or floor is None:
        return entries
    return [entry for entry in entries if entry.get('approximate') or
            (published := stamp(entry.get('published'))) is None or published >= floor]
