import hashlib
import json

from kb.feeds import registry as reg
from kb.youtube import transcripts


VERSION = 'youtube-product-v1'
PENDING = '旧版视频成品尚未通过当前完整性校验，暂不作为完整内容使用；等待重新处理。'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def certify(row, metadata, transcript, entry):
    transcripts.validate(transcript['segments'], metadata['duration'])
    if entry['url'] != row['entry']['url'] or any(not entry.get(field) for field in ('title', 'body')):
        raise ValueError('视频成品身份或正文不正确')
    inputs = {'video_id': row['video_id'], 'url': entry['url'], 'duration': metadata['duration'],
              'transcript': transcript}
    return encode({'version': VERSION, 'input': inputs, 'input_hash': digest(inputs), 'note_hash': digest(entry)})


def valid(row):
    try:
        proof = json.loads(row['validation'])
        entry = json.loads(row['note'])
        inputs = proof['input']
        transcripts.validate(inputs['transcript']['segments'], inputs['duration'])
        return (proof['version'] == VERSION and inputs['video_id'] == row['video_id'] and
                inputs['url'] == row['entry']['url'] == entry['url'] and
                proof['input_hash'] == digest(inputs) and proof['note_hash'] == digest(entry))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def replace(connection, entry):
    key = reg.item_key('youtube', entry['url'])
    content_hash = digest(entry)
    return connection.execute('''UPDATE feed_items SET title=?,body=?,author=?,published=?,
        content_kind=?,body_provenance=?,content_hash=?,lang=?,extract_stale=1,content_rev=content_rev+1,
        title_zh='',body_zh='',translated_at='',translate_error='',translate_tries=0
        WHERE key=? AND kind='youtube' AND content_hash<>?''',
        (entry['title'], entry['body'], entry.get('author', ''), entry.get('published', ''),
         entry.get('content_kind', 'markdown'), entry.get('body_provenance', ''), content_hash,
         reg.text_language(entry['title'], entry['body']), key, content_hash)).rowcount


#: 「这几条视频的成品要重新复核」的队列,一行一个链接(`youtube_videos.entry.url` 与
#: `feed_items.url` 是两张表共同的身份)。**由触发器填**,不由 Python 填 —— 写库的
#: 不止这一份代码(另一条没重启的分支、脚本、测试直改),触发器住在库里,谁写都漏不掉。
DIRTY = 'youtube_product_dirty'
_VIDEO_COLUMNS = 'state,validation,note,entry,video_id'
_ITEM_COLUMNS = 'url,title,body,author,published,body_provenance'
#: 本进程已经全量复核过的库。校验口径(`valid`/`transcripts.validate`)改了只在重启后
#: 生效,所以「每个进程首次打开某个库时全量复核一遍」就盖住了口径变化。
_SWEPT = set()


def _track(connection):
    connection.execute(f'CREATE TABLE IF NOT EXISTS {DIRTY} (url TEXT PRIMARY KEY)')
    # 触发器**不许让原本那条写语句失败**:`entry` 不是合法 JSON 时 `json_extract` 会抛,
    # 那样一次与成品无关的写入会被它连带拒掉。
    video = (f"INSERT OR IGNORE INTO {DIRTY} SELECT json_extract({{0}}.entry,'$.url') "
             "WHERE json_valid({0}.entry) AND json_extract({0}.entry,'$.url') IS NOT NULL")
    item = f'INSERT OR IGNORE INTO {DIRTY} SELECT {{0}}.url WHERE {{0}}.url IS NOT NULL'
    for name, event, body in (
            ('youtube_product_video_added', 'AFTER INSERT ON youtube_videos', video.format('NEW')),
            ('youtube_product_video_changed', f'AFTER UPDATE OF {_VIDEO_COLUMNS} ON youtube_videos',
             video.format('OLD') + '; ' + video.format('NEW')),
            ('youtube_product_video_removed', 'AFTER DELETE ON youtube_videos', video.format('OLD')),
            ('youtube_product_item_added', "AFTER INSERT ON feed_items WHEN NEW.kind='youtube'",
             item.format('NEW')),
            ('youtube_product_item_changed',
             f"AFTER UPDATE OF {_ITEM_COLUMNS} ON feed_items WHEN NEW.kind='youtube' OR OLD.kind='youtube'",
             item.format('OLD') + '; ' + item.format('NEW'))):
        connection.execute(f'CREATE TRIGGER IF NOT EXISTS {name} {event} BEGIN {body}; END')


def quarantine(connection):
    """把没通过当前完整性校验的成品撤下来,换成明确的待校验说明。

    **只复核变过的那几条。** 它跑在 `registry.db()` 的初始化事务里,也就是**每开一个
    写连接跑一次**;上一版每次都把全部 `ready` 成品的字幕重新逐字校验一遍(实测
    82 条成品、一次约 0.4 秒且一直攥着写锁),一个要开几十次连接的接口(「问题与
    待办」的总览)因此要 10–20 秒,整个服务跟着卡住。结果只取决于两张表上那几列,
    所以由触发器把改过的链接记进 `DIRTY`,这里只复核它们;每个进程首次打开某个库
    时照旧全量复核一遍。整段在调用方的 `BEGIN IMMEDIATE` 里,自己写出去的那几行
    触发器也会记进来,处理完一并清掉 —— 撤下来的状态本身就是不动点。
    """
    fresh = not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (DIRTY,)).fetchone()
    _track(connection)
    where = connection.execute('PRAGMA database_list').fetchone()[2]
    whole = fresh or not where or where not in _SWEPT
    if not whole and not connection.execute(f'SELECT 1 FROM {DIRTY} LIMIT 1').fetchone():
        return
    videos = 'SELECT * FROM youtube_videos'
    items = "SELECT * FROM feed_items WHERE kind='youtube'"
    if not whole:
        videos += f" WHERE json_extract(entry,'$.url') IN (SELECT url FROM {DIRTY})"
        items += f' AND url IN (SELECT url FROM {DIRTY})'
    _settle(connection, videos, items)
    connection.execute(f'DELETE FROM {DIRTY}')
    if where:
        _SWEPT.add(where)


def _settle(connection, videos, items):
    approved = {}
    for candidate in connection.execute(videos).fetchall():
        row = dict(candidate)
        row['entry'] = json.loads(row['entry'])
        if row['state'] == 'ready':
            if valid(row):
                approved[row['entry']['url']] = json.loads(row['note'])
            else:
                connection.execute('''UPDATE youtube_videos SET state='candidate',repair_pending=1,
                    reason=?,last_error=?,validation='',retry_at=0 WHERE video_id=?''',
                    (PENDING, PENDING, row['video_id']))
                connection.execute('UPDATE youtube_video_sources SET committed=0 WHERE video_id=?', (row['video_id'],))
    for item in connection.execute(items).fetchall():
        note = approved.get(item['url'])
        if note and all(item[field] == note.get(field, '') for field in ('title', 'body', 'author', 'published')):
            continue
        if item['body'] == PENDING and item['body_provenance'] == 'youtube:pending-validation':
            continue
        replace(connection, {field: item[field] for field in
                ('url', 'title', 'author', 'published', 'content_kind', 'body_provenance')}
                | {'body': PENDING, 'body_provenance': 'youtube:pending-validation'})
        if not note:
            connection.execute('''UPDATE youtube_videos SET repair_pending=1,
                last_error=CASE WHEN last_error='' THEN ? ELSE last_error END
                WHERE json_extract(entry,'$.url')=?''', (PENDING, item['url']))
        connection.execute('''UPDATE youtube_video_sources SET committed=0 WHERE video_id IN
            (SELECT video_id FROM youtube_videos WHERE json_extract(entry,'$.url')=?)''', (item['url'],))


def publish(connection, source, row):
    from kb.youtube.storage import permanently_excluded
    if permanently_excluded(row, connection):
        return 0
    current = connection.execute('SELECT * FROM youtube_videos WHERE video_id=?', (row['video_id'],)).fetchone()
    if not current:
        raise ValueError('视频成品已不存在')
    snapshot = {**dict(current), 'entry': json.loads(current['entry'])}
    if current['state'] != 'ready' or current['validation'] != row['validation'] or not valid(snapshot):
        raise ValueError('视频成品尚未通过当前完整性校验或处理版本已改变')
    entry = json.loads(current['note'])
    added = reg.put_items(source, [entry], conn=connection)
    replace(connection, entry)
    return added
