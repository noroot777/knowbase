import json
import time
from contextlib import contextmanager

from kb.feeds import registry as reg
from kb.maintenance import maintenance_lock


SCHEMA = '''CREATE TABLE IF NOT EXISTS youtube_videos (
 video_id TEXT PRIMARY KEY, entry TEXT NOT NULL, views INTEGER NOT NULL DEFAULT 0,
 heat REAL NOT NULL DEFAULT 0, value REAL NOT NULL DEFAULT 0,
 heat_rank INTEGER NOT NULL DEFAULT 0, value_rank INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'candidate', reason TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 duration REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
 last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
 validation TEXT NOT NULL DEFAULT '', repair_pending INTEGER NOT NULL DEFAULT 0,
 gate_code TEXT NOT NULL DEFAULT ''
)'''
OWNERS = '''CREATE TABLE IF NOT EXISTS youtube_video_sources (
 video_id TEXT NOT NULL REFERENCES youtube_videos(video_id) ON DELETE CASCADE,
 source_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 source_url TEXT NOT NULL, committed INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(video_id,source_id)
)'''


def migrate(connection):
    if connection.execute("SELECT 1 FROM feed_meta WHERE key='youtube-ownership-v2'").fetchone():
        return
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='youtube_candidates'").fetchone():
        connection.execute('''INSERT OR IGNORE INTO youtube_videos
            (video_id,entry,views,heat,value,heat_rank,value_rank,state,reason,attempts,retry_at,duration,note,last_error,updated_at)
            SELECT video_id,entry,views,heat,value,heat_rank,value_rank,state,reason,attempts,retry_at,duration,note,
            CASE WHEN state='failed' THEN reason ELSE '' END,updated_at FROM youtube_candidates''')
        connection.execute('''INSERT OR IGNORE INTO youtube_video_sources
            SELECT video_id,source_id,source_url,committed FROM youtube_candidates''')
    connection.execute("INSERT INTO feed_meta VALUES('youtube-ownership-v2',?)", (reg.now(),))


@contextmanager
def db():
    with reg.db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(SCHEMA)
        connection.execute(OWNERS)
        columns = {column['name'] for column in connection.execute('PRAGMA table_info(youtube_videos)')}
        for name, definition in (('validation', "TEXT NOT NULL DEFAULT ''"),
                                 ('repair_pending', 'INTEGER NOT NULL DEFAULT 0'),
                                 ('gate_code', "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                connection.execute(f'ALTER TABLE youtube_videos ADD COLUMN {name} {definition}')
        migrate(connection)
        connection.execute("UPDATE youtube_videos SET state='deferred',gate_code='legacy',retry_at=0 "
                           "WHERE state='excluded' AND gate_code=''")
        yield connection


def discover(source, entries):
    with maintenance_lock(), db() as connection:
        current = connection.execute('SELECT url,enabled FROM subscriptions WHERE id=?',
                                     (source['id'],)).fetchone()
        if not current or current['url'] != source['url'] or not current['enabled']:
            return
        connection.execute('DELETE FROM youtube_video_sources WHERE source_id=? AND source_url<>?',
                           (source['id'], source['url']))
        for entry in entries:
            connection.execute('''INSERT INTO youtube_videos
                (video_id,entry,views,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(video_id) DO UPDATE SET entry=excluded.entry,
                views=excluded.views,updated_at=excluded.updated_at''',
                (entry['video_id'], json.dumps(entry, ensure_ascii=False), entry.get('views', 0), reg.now()))
            connection.execute('''INSERT INTO youtube_video_sources(video_id,source_id,source_url)
                VALUES(?,?,?) ON CONFLICT(video_id,source_id) DO UPDATE SET source_url=excluded.source_url,
                committed=CASE WHEN source_url=excluded.source_url THEN committed ELSE 0 END''',
                (entry['video_id'], source['id'], source['url']))


def rows(source_ids=None, connection=None):
    where, arguments = '', []
    if source_ids is not None:
        if not source_ids:
            return []
        where = ' AND owner.source_id IN (' + ','.join('?' for _ in source_ids) + ')'
        arguments = list(source_ids)
    if connection is None:
        with db() as current:
            return rows(source_ids, connection=current)
    result = connection.execute('''SELECT candidate.*,owner.source_id,owner.source_url,
            owner.committed,source.name AS channel,source.config,source.history_floor,
            source.id AS owner_id
            FROM youtube_videos candidate JOIN youtube_video_sources owner ON owner.video_id=candidate.video_id
            JOIN subscriptions source ON source.id=owner.source_id
            WHERE source.enabled=1 AND source.url=owner.source_url''' + where + ' ORDER BY owner.source_id',
            arguments).fetchall()
    grouped = {}
    for item in result:
        row = dict(item)
        owner = {key: row[key] for key in ('source_id', 'source_url', 'committed', 'channel', 'config', 'history_floor')}
        candidate = grouped.setdefault(row['video_id'], {**row, 'entry': json.loads(row['entry']), 'owners': []})
        candidate['owners'].append(owner)
    return list(grouped.values())


def update(video_id, **values):
    allowed = {'heat', 'value', 'heat_rank', 'value_rank', 'state', 'reason',
               'attempts', 'retry_at', 'duration', 'note', 'last_error', 'validation', 'repair_pending', 'gate_code'}
    if not values or set(values) - allowed:
        raise ValueError('视频处理状态字段不正确')
    with db() as connection:
        connection.execute('UPDATE youtube_videos SET ' +
                           ','.join(key + '=?' for key in values) + ',updated_at=? WHERE video_id=?',
                           [*values.values(), reg.now(), video_id])


def failed(row, error):
    attempts = row['attempts'] + 1
    update(row['video_id'], state='failed', attempts=attempts,
           retry_at=time.time() + min(86400, 1800 * 2 ** min(attempts - 1, 5)),
           reason=str(error)[:1000] or type(error).__name__,
           last_error=str(error)[:1000] or type(error).__name__)


def mark_committed(connection, source, video_id):
    connection.execute('''UPDATE youtube_video_sources SET committed=1
        WHERE video_id=? AND source_id=? AND source_url=?''',
        (video_id, source['id'], source['url']))


def ready(source):
    from kb.youtube.products import valid
    return [row for row in rows([source['id']]) if row['state'] == 'ready'
            and not row['committed'] and row['source_url'] == source['url'] and valid(row)]


def listing(source_id=None, limit=100):
    candidates = rows([source_id] if source_id is not None else None)
    candidates.sort(key=lambda row: (min(row['heat_rank'] or 100000, row['value_rank'] or 100000), -row['value']))
    counts = {}
    for row in candidates:
        counts[row['state']] = counts.get(row['state'], 0) + 1
    result = []
    for row in candidates[:limit]:
        entry = row['entry']
        result.append({key: row[key] for key in ('video_id', 'source_id', 'channel', 'views',
                      'heat', 'value', 'heat_rank', 'value_rank', 'state', 'reason', 'duration')}
                      | {key: entry.get(key, '') for key in ('title', 'url', 'published')})
    return {'items': result, 'total': len(candidates), 'counts': counts}
