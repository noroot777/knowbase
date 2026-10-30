import json
import time
from contextlib import contextmanager

from kb.feeds import registry as reg
from kb.maintenance import maintenance_lock
from kb.youtube.diagnosis import shown


SCHEMA = '''CREATE TABLE IF NOT EXISTS youtube_videos (
 video_id TEXT PRIMARY KEY, entry TEXT NOT NULL, views INTEGER NOT NULL DEFAULT 0,
 heat REAL NOT NULL DEFAULT 0, value REAL NOT NULL DEFAULT 0,
 heat_rank INTEGER NOT NULL DEFAULT 0, value_rank INTEGER NOT NULL DEFAULT 0,
 score REAL NOT NULL DEFAULT 0, score_rank INTEGER NOT NULL DEFAULT 0,
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
            (video_id,entry,views,score,score_rank,state,reason,attempts,retry_at,duration,note,last_error,updated_at)
            SELECT video_id,entry,views,heat,heat_rank,state,reason,attempts,retry_at,duration,note,
            CASE WHEN state='failed' THEN reason ELSE '' END,updated_at FROM youtube_candidates''')
        connection.execute('''INSERT OR IGNORE INTO youtube_video_sources
            SELECT video_id,source_id,source_url,committed FROM youtube_candidates''')
    connection.execute("INSERT INTO feed_meta VALUES('youtube-ownership-v2',?)", (reg.now(),))


def initialize(connection):
    from kb.youtube.channels import seed
    from kb.youtube.products import quarantine
    seed(connection, reg.now(), reg.history_floor())
    connection.execute(SCHEMA)
    connection.execute(OWNERS)
    columns = {column['name'] for column in connection.execute('PRAGMA table_info(youtube_videos)')}
    for name, definition in (('validation', "TEXT NOT NULL DEFAULT ''"),
                             ('repair_pending', 'INTEGER NOT NULL DEFAULT 0'),
                             ('gate_code', "TEXT NOT NULL DEFAULT ''"),
                             ('score', 'REAL NOT NULL DEFAULT 0'),
                             ('score_rank', 'INTEGER NOT NULL DEFAULT 0')):
        if name not in columns:
            connection.execute(f'ALTER TABLE youtube_videos ADD COLUMN {name} {definition}')
    # 两榜合一之后 `heat`/`value`/`heat_rank`/`value_rank` **停止读写但不 DROP**
    # (主干迁移只增不减)。补列那一刻把老的热度名次搬进新列:不搬的话存量库在下一轮
    # 排名跑完之前,面板上整张榜都是「—」,`select` 看到的也全是 0 名。
    if 'score' not in columns:
        connection.execute('UPDATE youtube_videos SET score=heat, score_rank=heat_rank')
    migrate(connection)
    connection.execute("UPDATE youtube_videos SET state='deferred',gate_code='legacy',retry_at=0 "
                       "WHERE state='excluded' AND gate_code=''")
    connection.execute('''CREATE TRIGGER IF NOT EXISTS youtube_subscription_reset
        AFTER UPDATE OF url,feed_url,mode ON subscriptions
        WHEN OLD.kind='youtube' AND (NEW.url<>OLD.url OR NEW.feed_url<>OLD.feed_url OR NEW.mode<>OLD.mode)
        BEGIN DELETE FROM youtube_video_sources WHERE source_id=OLD.id; END''')
    connection.execute('''CREATE TRIGGER IF NOT EXISTS youtube_membership_removed
        AFTER DELETE ON subscription_items
        BEGIN UPDATE youtube_video_sources SET committed=0 WHERE source_id=OLD.subscription_id
        AND video_id IN (SELECT video.video_id FROM youtube_videos video JOIN feed_items item
            ON item.url=json_extract(video.entry,'$.url') WHERE item.key=OLD.item_key AND item.kind='youtube'); END''')
    quarantine(connection)


@contextmanager
def db():
    with reg.db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        yield connection


def permanently_excluded(row, connection=None):
    if connection is None:
        with db() as current:
            return permanently_excluded(row, current)
    return bool(connection.execute('SELECT 1 FROM feed_item_exclusions WHERE key=?',
                (reg.item_key('youtube', row['entry']['url']),)).fetchone())


def snapshot(entry):
    """所有精确 entry 写入口统一记录快照时间，flat 列表不续期。"""
    return entry if entry.get('approximate') else {**entry, 'metadata_checked_at': time.time()}


def discover(source, entries):
    with maintenance_lock(), db() as connection:
        current = connection.execute('SELECT url,enabled FROM subscriptions WHERE id=?',
                                     (source['id'],)).fetchone()
        if not current or current['url'] != source['url'] or not current['enabled']:
            return
        connection.execute('DELETE FROM youtube_video_sources WHERE source_id=? AND source_url<>?',
                           (source['id'], source['url']))
        for entry in entries:
            if permanently_excluded({'entry': entry}, connection):
                continue
            entry = snapshot(entry)
            # **估算数据不许覆盖已有的精确数据。** 发现有两条路:RSS 带真实发布时间和播放量,
            # yt-dlp 的后路只有列表顺序估出来的时间和 views=0(见 `discovery.approximate`)。
            # 判据待在这条语句自己的 CASE 里 —— 先 SELECT 一次再决定要不要写,中间那一下
            # 没有锁,而写错的后果是一条已经排好名的候选被降级成「没有播放量」,下一轮
            # 候选榜上就找不到它了。反过来钉一边:精确数据照旧覆盖估算数据。
            # **播放量按「这一份是不是估算来源」判,不按正负判**:0 既可能是「不知道」
            # (后路一律给 0)也可能是「确实没人看」(刚发布的视频),按正负判就让后者
            # 永远盖不住一个过时的正数 —— 判据跟 entry 那一行同源,口径只有一份。
            connection.execute('''INSERT INTO youtube_videos
                (video_id,entry,views,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(video_id) DO UPDATE SET
                    entry=CASE WHEN json_extract(excluded.entry,'$.approximate') IS NOT 1
                                 OR json_extract(youtube_videos.entry,'$.approximate') IS 1
                               THEN excluded.entry ELSE youtube_videos.entry END,
                    views=CASE WHEN json_extract(excluded.entry,'$.approximate') IS NOT 1
                               THEN excluded.views ELSE youtube_videos.views END,
                    updated_at=excluded.updated_at''',
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
            owner.committed AND EXISTS(SELECT 1 FROM subscription_items link JOIN feed_items item ON item.key=link.item_key
                WHERE link.subscription_id=owner.source_id AND item.kind='youtube'
                AND item.url=json_extract(candidate.entry,'$.url')) AS committed,
            EXISTS(SELECT 1 FROM feed_item_exclusions removed WHERE removed.kind='youtube'
                AND removed.url=json_extract(candidate.entry,'$.url')) AS excluded,
            source.name AS channel,source.config,source.history_floor,
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


def recalibrate(video_id, entry):
    """把一条估算候选的 `entry` 换成校准过的那一份(事实日期、真实播放量)。

    **判据待在语句自己的 WHERE 里** —— 只改还标着 `approximate` 的那一条。先 SELECT
    一次再决定的话,中间那一下没有锁(legacy `isolation_level` 下 SELECT 不开事务),
    而另一轮可能正好在同一刻校准同一条。**0 行不是失败**:那说明别人已经校准过了,
    正是想要的末态。

    不走 `update()`:它的白名单是无条件覆盖那一类字段,`entry` 不能那样写。
    """
    payload = json.dumps(snapshot(entry), ensure_ascii=False)
    views = entry.get('views')
    with db() as connection:
        connection.execute("""UPDATE youtube_videos
            SET entry=?, views=CASE WHEN ?>=0 THEN ? ELSE views END, updated_at=?
            WHERE video_id=? AND json_extract(entry,'$.approximate') IS 1""",
            (payload, views if isinstance(views, int) else -1,
             views if isinstance(views, int) else 0, reg.now(), video_id))


def refresh_metadata(row, entry):
    """仅更新尚未被替换、未被永久移除且仍有有效归属的元数据快照。"""
    with maintenance_lock(), db() as connection:
        changed = connection.execute('''UPDATE youtube_videos SET entry=?,views=?,updated_at=?
            WHERE video_id=? AND entry=?
            AND NOT EXISTS(SELECT 1 FROM feed_item_exclusions removed
                WHERE removed.kind='youtube' AND removed.url=json_extract(youtube_videos.entry,'$.url'))
            AND EXISTS(SELECT 1 FROM youtube_video_sources owner JOIN subscriptions source
                ON source.id=owner.source_id WHERE owner.video_id=youtube_videos.video_id
                AND source.enabled=1 AND source.url=owner.source_url)''',
            (json.dumps(snapshot(entry), ensure_ascii=False), entry['views'], reg.now(), row['video_id'],
             json.dumps(row['entry'], ensure_ascii=False)))
        return changed.rowcount == 1


def defer_metadata(updates):
    """同一连接批量挂起待更新候选，保留既有失败、成品和永久移除状态。"""
    if not updates:
        return
    with db() as connection:
        connection.executemany('''UPDATE youtube_videos SET
            score_rank=CASE WHEN state IN ('ready','excluded','failed') THEN score_rank ELSE 0 END,
            state=CASE WHEN state IN ('ready','excluded','failed') THEN state ELSE 'deferred' END,
            reason=CASE WHEN state IN ('ready','excluded','failed') OR (state='deferred' AND reason<>'')
                THEN reason ELSE ? END,
            retry_at=CASE WHEN state IN ('ready','excluded','failed') THEN retry_at ELSE COALESCE(?,retry_at) END
            WHERE video_id=? AND NOT EXISTS(SELECT 1 FROM feed_item_exclusions removed
                WHERE removed.kind='youtube' AND removed.url=json_extract(youtube_videos.entry,'$.url'))''',
            [(reason, retry_at, row['video_id']) for row, reason, retry_at in updates])


def update(video_id, **values):
    allowed = {'score', 'score_rank', 'state', 'reason',
               'attempts', 'retry_at', 'duration', 'note', 'last_error', 'validation', 'repair_pending', 'gate_code'}
    if not values or set(values) - allowed:
        raise ValueError('视频处理状态字段不正确')
    with db() as connection:
        connection.execute('UPDATE youtube_videos SET ' +
                           ','.join(key + '=?' for key in values) + ',updated_at=? WHERE video_id=?',
                           [*values.values(), reg.now(), video_id])


def failed(row, error):
    """这条视频这一轮失败了。**落库那份文本一律由 `diagnosis.shown` 出**,这儿不自己裁。

    这就是人在候选排名里读到的那一行(`listing` 只交 `reason`、面板直接显示它),
    所以它是**整条诊断交付链的末端**。原先这儿取 `str(error)[:1000]`,把权威诊断
    换成了被裁过的摘要 —— 事故形态和判据见 `diagnosis.shown`。
    """
    attempts = row['attempts'] + 1
    told = shown(error, 1000)
    update(row['video_id'], state='failed', attempts=attempts,
           retry_at=time.time() + min(86400, 1800 * 2 ** min(attempts - 1, 5)),
           reason=told, last_error=told)


def mark_committed(connection, source, video_id):
    connection.execute('''UPDATE youtube_video_sources SET committed=EXISTS(
        SELECT 1 FROM subscription_items link JOIN feed_items item ON item.key=link.item_key
        JOIN youtube_videos video ON item.url=json_extract(video.entry,'$.url')
        WHERE link.subscription_id=? AND item.kind='youtube' AND video.video_id=?)
        WHERE video_id=? AND source_id=? AND source_url=?''',
        (source['id'], video_id, video_id, source['id'], source['url']))


def ready(source):
    from kb.youtube.products import valid
    return [row for row in rows([source['id']]) if row['state'] == 'ready'
            and not row['committed'] and not row['excluded'] and row['source_url'] == source['url'] and valid(row)]


def listing(source_id=None, limit=100):
    candidates = rows([source_id] if source_id is not None else None)
    candidates.sort(key=lambda row: (row['score_rank'] or 100000, -row['score']))
    counts = {}
    for row in candidates:
        counts[row['state']] = counts.get(row['state'], 0) + 1
    result = []
    for row in candidates[:limit]:
        entry = row['entry']
        result.append({key: row[key] for key in ('video_id', 'source_id', 'channel', 'views',
                      'score', 'score_rank', 'state', 'reason', 'duration')}
                      | {key: entry.get(key, '') for key in ('title', 'url', 'published')}
                      # **估出来的那条要带上这面旗。** 后路发现时 `views` 一律写 0
                      # (列表页读不到播放量),面板不知道这回事就会把「没数据」显示成
                      # 「0 次观看」—— 那是在替 YouTube 说一句它没说过的话。
                      | {'approximate': bool(entry.get('approximate'))})
    return {'items': result, 'total': len(candidates), 'counts': counts}
