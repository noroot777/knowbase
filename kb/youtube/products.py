import hashlib
import json

from kb.feeds import registry as reg
from kb.maintenance import maintenance_lock
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


def prepare(source_ids):
    from kb.youtube import storage
    with maintenance_lock(), storage.db() as connection:
        for row in storage.rows(source_ids, connection=connection):
            if row['state'] != 'ready' or valid(row):
                continue
            connection.execute('''UPDATE youtube_videos SET state='candidate',repair_pending=1,
                reason=?,last_error=?,validation='',retry_at=0 WHERE video_id=?''',
                (PENDING, PENDING, row['video_id']))
            connection.execute('UPDATE youtube_video_sources SET committed=0 WHERE video_id=?', (row['video_id'],))
            existing = connection.execute('SELECT * FROM feed_items WHERE key=? AND kind=\'youtube\'',
                                          (reg.item_key('youtube', row['entry']['url']),)).fetchone()
            if existing:
                replace(connection, {field: existing[field] for field in
                        ('url', 'title', 'author', 'published', 'content_kind', 'body_provenance')}
                        | {'body': PENDING, 'body_provenance': 'youtube:pending-validation'})


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
