import hashlib
import json

from kb.feeds import registry as reg
from kb.youtube import transcripts


VERSION = 'youtube-product-v1'


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
