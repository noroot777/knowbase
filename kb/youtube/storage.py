import json
import time
from contextlib import contextmanager

from kb.feeds import registry as reg
from kb.maintenance import maintenance_lock


SCHEMA = '''CREATE TABLE IF NOT EXISTS youtube_candidates (
 video_id TEXT PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 source_url TEXT NOT NULL, entry TEXT NOT NULL, views INTEGER NOT NULL DEFAULT 0,
 heat REAL NOT NULL DEFAULT 0, value REAL NOT NULL DEFAULT 0,
 heat_rank INTEGER NOT NULL DEFAULT 0, value_rank INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'candidate', reason TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 duration REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
 committed INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
)'''


@contextmanager
def db():
    with reg.db() as connection:
        connection.execute(SCHEMA)
        yield connection


def discover(source, entries):
    with maintenance_lock(), db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        current = connection.execute('SELECT url,enabled FROM subscriptions WHERE id=?',
                                     (source['id'],)).fetchone()
        if not current or current['url'] != source['url'] or not current['enabled']:
            return
        connection.execute('DELETE FROM youtube_candidates WHERE source_id=? AND source_url<>?',
                           (source['id'], source['url']))
        for entry in entries:
            connection.execute('''INSERT INTO youtube_candidates
                (video_id,source_id,source_url,entry,views,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(video_id) DO UPDATE SET entry=excluded.entry,
                views=excluded.views,updated_at=excluded.updated_at
                WHERE youtube_candidates.source_id=excluded.source_id
                AND youtube_candidates.source_url=excluded.source_url''',
                (entry['video_id'], source['id'], source['url'],
                 json.dumps(entry, ensure_ascii=False), entry.get('views', 0), reg.now()))


def rows(source_ids=None):
    where, arguments = '', []
    if source_ids is not None:
        if not source_ids:
            return []
        where = ' AND candidate.source_id IN (' + ','.join('?' for _ in source_ids) + ')'
        arguments = list(source_ids)
    with db() as connection:
        result = connection.execute('''SELECT candidate.*,source.name AS channel,
            source.config,source.history_floor FROM youtube_candidates candidate
            JOIN subscriptions source ON source.id=candidate.source_id
            WHERE source.enabled=1 AND source.url=candidate.source_url''' + where,
            arguments).fetchall()
    return [{**dict(row), 'entry': json.loads(row['entry'])} for row in result]


def update(video_id, **values):
    allowed = {'heat', 'value', 'heat_rank', 'value_rank', 'state', 'reason',
               'attempts', 'retry_at', 'duration', 'note', 'committed'}
    if not values or set(values) - allowed:
        raise ValueError('视频处理状态字段不正确')
    with db() as connection:
        connection.execute('UPDATE youtube_candidates SET ' +
                           ','.join(key + '=?' for key in values) + ',updated_at=? WHERE video_id=?',
                           [*values.values(), reg.now(), video_id])


def failed(row, error):
    attempts = row['attempts'] + 1
    update(row['video_id'], state='failed', attempts=attempts,
           retry_at=time.time() + min(86400, 1800 * 2 ** min(attempts - 1, 5)),
           reason=str(error)[:1000] or type(error).__name__)


def ready(source):
    return [row for row in rows([source['id']]) if row['state'] == 'ready'
            and not row['committed'] and row['source_url'] == source['url']]


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
