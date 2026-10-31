from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from kb.feeds import registry as reg
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
    if not connection.execute("SELECT 1 FROM feed_meta WHERE key='youtube-discovery-window-v2'").fetchone():
        connection.execute('''UPDATE youtube_videos SET state=CASE WHEN last_error<>'' THEN 'failed'
            ELSE 'candidate' END,reason=CASE WHEN last_error<>'' THEN last_error ELSE '等待排名与处理预算准入' END,
            score_rank=0 WHERE state='filtered' AND reason='早于首次同步起点（默认从今天回看 3 天）' ''')
        connection.execute("INSERT INTO feed_meta VALUES('youtube-discovery-window-v2',?)", (reg.now(),))


def prepare(source, scope, when=None):
    if scope.ranged:
        return source
    with reg.db() as connection:
        current = connection.execute('''SELECT * FROM subscriptions
            WHERE id=? AND kind='youtube' AND url=? AND enabled=1''',
            (source['id'], source['url'])).fetchone()
        if current is None:
            return source
        source = dict(current)
        origin = connection.execute('''SELECT initial_fetch_at FROM youtube_sync_origins
            WHERE source_id=? AND source_url=?''', (source['id'], source['url'])).fetchone()
        now = when or datetime.now(timezone.utc)
        previous = stamp(source['last_success'])
        if previous is not None and previous <= now:
            floor = previous - timedelta(days=1)
        else:
            initial = origin['initial_fetch_at'] if origin else ''
            if stamp(initial) is None:
                initial = datetime.combine(now.astimezone(TZ).date() - timedelta(days=INITIAL_DAYS),
                                           time.min, TZ).isoformat()
            source['initial_fetch_at'] = initial
            floor = stamp(initial)
        known = connection.execute('''SELECT video_id FROM youtube_video_sources
            WHERE source_id=? AND source_url=?''', (source['id'], source['url'])).fetchall()
        return {**source, 'discovery_after': floor.isoformat(),
                'known_video_ids': tuple(row['video_id'] for row in known)}


def record(connection, source):
    if source.get('initial_fetch_at'):
        connection.execute('''INSERT INTO youtube_sync_origins(source_id,source_url,initial_fetch_at)
            VALUES(?,?,?) ON CONFLICT(source_id) DO NOTHING''',
            (source['id'], source['url'], source['initial_fetch_at']))


def recent(source, entries, scope):
    floor = stamp(source.get('discovery_after'))
    if scope.ranged or floor is None:
        return entries
    known = set(source.get('known_video_ids', ()))
    return [entry for entry in entries if entry['video_id'] in known or entry.get('approximate') or
            (published := stamp(entry.get('published'))) is None or published >= floor]
