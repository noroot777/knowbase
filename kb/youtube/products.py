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


def quarantine(connection):
    approved = {}
    for candidate in connection.execute('SELECT * FROM youtube_videos').fetchall():
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
    for item in connection.execute("SELECT * FROM feed_items WHERE kind='youtube'").fetchall():
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
