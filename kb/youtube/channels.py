import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit


CHANNEL_ID = re.compile(r'UC[A-Za-z0-9_-]{22}')
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
POOLS = ('creator_core', 'interview_pool', 'creator_watchlist', 'quarantine')
GROUPS = {'creator_core': '核心创作者', 'interview_pool': 'AI 访谈池',
          'creator_watchlist': '观察名单', 'quarantine': '隔离名单'}


def normalize(value):
    parts = urlsplit(str(value).strip())
    if (parts.scheme != 'https' or parts.hostname not in
            ('youtube.com', 'www.youtube.com', 'm.youtube.com') or
            parts.username or parts.password or parts.port not in (None, 443)):
        raise ValueError('请填写公开的 https://www.youtube.com 频道主页')
    path = unquote(parts.path).strip('/')
    if path == 'feeds/videos.xml':
        path = 'channel/' + parse_qs(parts.query).get('channel_id', [''])[0]
    pieces = path.split('/')
    if pieces[-1] in ('videos', 'streams', 'shorts', 'featured'):
        pieces.pop()
    if len(pieces) == 2 and pieces[0] == 'channel' and CHANNEL_ID.fullmatch(pieces[1]):
        return 'https://www.youtube.com/channel/' + pieces[1]
    if len(pieces) == 1 and pieces[0].startswith('@'):
        handle = pieces[0][1:]
        if 3 <= len(handle) <= 30 and all(char.isalnum() or char in '._-' for char in handle):
            return 'https://www.youtube.com/@' + quote(handle.lower(), safe='._-')
    raise ValueError('请填写 /channel/UC… 或 /@频道名，不是单条视频链接')


def config(source):
    raw = source.get('config') or '{}'
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def subscription_values(kind, data, old, reset):
    if kind != 'youtube':
        return None
    values = {} if reset else config(old)
    patch = data.get('youtube')
    if patch is not None:
        if not isinstance(patch, dict) or set(patch) - {'pool', 'score_multiplier'}:
            raise ValueError('YouTube 频道设置不正确')
        values.update(patch)
    pool = values.get('pool', 'creator_core')
    weight = values.get('score_multiplier', 1.0)
    if pool not in POOLS or isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0.1 <= weight <= 3:
        raise ValueError('频道分组不正确，或排名权重不在 0.1–3 之间')
    values.update(pool=pool, score_multiplier=float(weight),
                  ranking_enabled=pool in ('creator_core', 'interview_pool'))
    return values


@lru_cache(maxsize=1)
def defaults():
    return json.loads(Path(__file__).with_name('defaults.json').read_text(encoding='utf-8'))


def seed(connection, now, floor):
    payload = defaults()
    row = connection.execute("SELECT value FROM feed_meta WHERE key='youtube-defaults-rev'").fetchone()
    if row and int(row[0]) >= payload['seed_revision']:
        return
    for channel in payload['channels']:
        values = {key: channel[key] for key in
                  ('channel_id', 'pool', 'status', 'category', 'note', 'ranking_enabled', 'score_multiplier')}
        connection.execute('''INSERT OR IGNORE INTO subscriptions
            (kind,name,url,mode,group_name,enabled,config,created_at,updated_at,history_floor)
            VALUES('youtube',?,?,'feed',?,?,?,?,?,?)''',
            (channel['name'], channel['url'], GROUPS[channel['pool']],
             int(channel['ranking_enabled']), json.dumps(values, ensure_ascii=False), now, now, floor))
    connection.execute("INSERT OR REPLACE INTO feed_meta VALUES('youtube-defaults-rev',?)",
                       (str(payload['seed_revision']),))
