"""Persistent subscriptions and fetched items, independent of the rebuildable search index."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from kb.config import DATA_DIR
from kb.source_types import feed_types

DB_PATH = DATA_DIR / 'feeds.db'
KINDS = {k: s.label for k, s in feed_types().items()}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS feed_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subscriptions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL,
 url TEXT NOT NULL, feed_url TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'feed',
 group_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
 config TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 last_attempt TEXT NOT NULL DEFAULT '', last_success TEXT NOT NULL DEFAULT '',
 last_error TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, etag TEXT NOT NULL DEFAULT '',
 modified TEXT NOT NULL DEFAULT '', resolved_url TEXT NOT NULL DEFAULT '',
 UNIQUE(kind, url)
);
CREATE TABLE IF NOT EXISTS feed_items (
 key TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
 author TEXT NOT NULL, published TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
 content_kind TEXT NOT NULL DEFAULT 'feed', fetched_at TEXT NOT NULL,
 content_hash TEXT NOT NULL, exported_hash TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS feed_items_recent ON feed_items(kind, published DESC, fetched_at DESC);
CREATE TABLE IF NOT EXISTS subscription_items (
 subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 item_key TEXT NOT NULL REFERENCES feed_items(key), PRIMARY KEY(subscription_id, item_key)
);
CREATE TABLE IF NOT EXISTS feed_settings (kind TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feed_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT NOT NULL, summary TEXT NOT NULL
);
'''


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError('未知来源模块')
    return kind


def normalize_url(value: str) -> str:
    value = value.strip()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError('链接格式不正确') from None
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('请输入完整的 http(s) 链接，不要在链接中包含账号密码')
    if len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError('链接过长或包含空格')
    host = parts.hostname.lower()
    # Public subscriptions only. A local RSSHub can be exposed through a public hostname.
    if host in ('localhost',) or host.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('订阅链接须使用公开地址')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError('订阅链接须使用公开地址')
    netloc = f'[{host}]' if ':' in host else host
    if port and not ((parts.scheme == 'https' and port == 443) or (parts.scheme == 'http' and port == 80)):
        netloc += f':{port}'
    return urlunsplit((parts.scheme, netloc, parts.path or '/', parts.query, ''))


def x_handle(url: str) -> str:
    p = urlsplit(url)
    if p.hostname not in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'):
        return ''
    path = p.path.strip('/').split('/')
    if len(path) not in (1, 2) or (len(path) == 2 and path[1] not in ('with_replies', 'media')):
        raise ValueError('请填写 X / Twitter 账号主页，不是单条推文链接')
    handle = path[0]
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', handle) or handle.lower() in ('home', 'search', 'explore', 'i', 'intent', 'settings'):
        raise ValueError('X / Twitter 账号链接不正确')
    return handle


def source_url(kind: str, value: str) -> str:
    url = normalize_url(value)
    if feed_types()[kind].adapter == 'social' and (handle := x_handle(url)):
        return 'https://x.com/' + handle.lower()
    return url


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        conn.executescript(SCHEMA)
        # One transaction: deleting a default never brings it back on restart.
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute("SELECT 1 FROM feed_meta WHERE key='defaults-v1'").fetchone():
            defaults = json.loads(Path(__file__).with_name('defaults.json').read_text())
            for s in defaults:
                conn.execute('''INSERT OR IGNORE INTO subscriptions
                    (kind,name,url,mode,group_name,config,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)''',
                    (s['kind'], s['name'], source_url(s['kind'],s['url']), s['mode'], s['group_name'],
                     '{}', now(), now()))
            conn.execute("INSERT INTO feed_meta VALUES('defaults-v1',?)", (now(),))
        conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_settings(kind: str) -> dict:
    check_kind(kind)
    with db() as c:
        row = c.execute('SELECT value FROM feed_settings WHERE kind=?', (kind,)).fetchone()
    default = {'enabled': False, 'interval_minutes': feed_types()[kind].interval_minutes,
               'next_run': 0, 'bridges': []}
    return {**default, **(json.loads(row['value']) if row else {})}


def save_settings(kind: str, values: dict) -> dict:
    cur = get_settings(kind)
    cur.update(values)
    if not isinstance(cur['interval_minutes'], int) or not 15 <= cur['interval_minutes'] <= 10080:
        raise ValueError('同步间隔须在 15 分钟至 7 天之间')
    bridges = cur.get('bridges', [])
    if not isinstance(bridges, list) or len(bridges) > 5:
        raise ValueError('最多设置 5 个 X 订阅桥接地址；留空时账号等待配置专用链接')
    for value in bridges:
        if value.count('{handle}') != 1:
            raise ValueError('桥接地址须包含一个 {handle}，用于替换账号名')
        normalize_url(value.replace('{handle}', 'example'))
    with db() as c:
        c.execute('INSERT OR REPLACE INTO feed_settings VALUES(?,?)', (kind, json.dumps(cur)))
    return cur


def uses_bridge(source: dict) -> bool:
    return (feed_types()[source['kind']].adapter == 'social' and not source['feed_url']
            and bool(x_handle(source['url'])))


def configuration_error(source: dict, settings: dict) -> str:
    if uses_bridge(source) and not settings['bridges']:
        return '待配置：请在 X 订阅桥接设置中填写可用模板，或编辑账号的专用订阅链接'
    return ''


def subscriptions(kind: str) -> list[dict]:
    check_kind(kind)
    with db() as c:
        rows = c.execute('''SELECT s.*, (SELECT count(*) FROM subscription_items si
            WHERE si.subscription_id=s.id) AS item_count FROM subscriptions s
            WHERE kind=? ORDER BY id''', (kind,)).fetchall()
    return [dict(r) for r in rows]


def subscription(kind: str, source_id: int) -> dict:
    with db() as c:
        r = c.execute('SELECT * FROM subscriptions WHERE id=? AND kind=?', (source_id, check_kind(kind))).fetchone()
    if not r:
        raise LookupError('订阅不存在或已被删除')
    return dict(r)


def save_subscription(kind: str, data: dict, source_id: int | None = None) -> dict:
    check_kind(kind)
    old = subscription(kind, source_id) if source_id else {}
    name = str(data.get('name', old.get('name', ''))).strip()
    if not name or len(name) > 120:
        raise ValueError('名称不能为空，且不能超过 120 个字符')
    url = source_url(kind,data.get('url', old.get('url', '')))
    feed_url = data.get('feed_url', old.get('feed_url', '')).strip()
    if feed_url:
        feed_url = normalize_url(feed_url)
    mode = data.get('mode', old.get('mode', 'feed'))
    if mode != 'feed':
        raise ValueError('来源类型不正确')
    group = str(data.get('group_name', old.get('group_name', ''))).strip()[:80]
    enabled = bool(data.get('enabled', old.get('enabled', True)))
    reset = any(old.get(k) != v for k, v in [('url', url), ('feed_url', feed_url), ('mode', mode)])
    try:
        with db() as c:
            if source_id:
                c.execute('''UPDATE subscriptions SET name=?,url=?,feed_url=?,mode=?,group_name=?,
                    enabled=?,updated_at=? WHERE id=?''', (name,url,feed_url,mode,group,enabled,now(),source_id))
                if reset:
                    c.execute("""UPDATE subscriptions SET etag='',modified='',resolved_url='',
                        last_error='',last_success='',last_attempt='',failures=0,retry_at=0,config='{}' WHERE id=?""", (source_id,))
                    c.execute('DELETE FROM subscription_items WHERE subscription_id=?', (source_id,))
            else:
                source_id = c.execute('''INSERT INTO subscriptions
                    (kind,name,url,feed_url,mode,group_name,enabled,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)''', (kind,name,url,feed_url,mode,group,enabled,now(),now())).lastrowid
    except sqlite3.IntegrityError:
        raise ValueError('这个链接已经在订阅列表中') from None
    return subscription(kind, source_id)


def delete_subscription(kind: str, source_id: int):
    subscription(kind, source_id)
    with db() as c:
        c.execute('DELETE FROM subscriptions WHERE id=?', (source_id,))


def put_items(source: dict, entries: list[dict]) -> int:
    added = 0
    with db() as c:
        for e in entries:
            key = hashlib.sha256((source['kind'] + ':' + e['url']).encode()).hexdigest()[:32]
            digest = hashlib.sha256(json.dumps(e, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            exists = c.execute('SELECT key FROM feed_items WHERE key=?', (key,)).fetchone()
            added += int(not exists)
            # First provenance remains stable when an aggregate also contains this URL.
            c.execute('''INSERT INTO feed_items
                (key,kind,url,title,author,published,body,content_kind,fetched_at,content_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING''',
                (key,source['kind'],e['url'],e['title'],e.get('author') or source['name'],
                 e.get('published',''),e['body'],e.get('content_kind','feed'),now(),digest))
            c.execute('INSERT OR IGNORE INTO subscription_items VALUES(?,?)', (source['id'],key))
    return added


def items(kind: str, source_id: int | None = None, offset: int = 0, limit: int = 30, q: str = '') -> dict:
    args: list = [check_kind(kind)]
    where = 'kind=?'
    if source_id:
        where += ' AND key IN (SELECT item_key FROM subscription_items WHERE subscription_id=?)'
        args.append(source_id)
    if q:
        where += ' AND (title LIKE ? OR body LIKE ? OR author LIKE ?)'
        args.extend(['%' + q + '%'] * 3)
    with db() as c:
        total = c.execute('SELECT count(*) FROM feed_items WHERE ' + where, args).fetchone()[0]
        rows = c.execute('SELECT * FROM feed_items WHERE ' + where +
                         " ORDER BY COALESCE(NULLIF(published,''),fetched_at) DESC,key DESC LIMIT ? OFFSET ?",
                         [*args,limit,offset]).fetchall()
    return {'items': [dict(r) for r in rows], 'total': total, 'offset': offset}


def latest_run(kind: str) -> dict | None:
    with db() as c:
        row = c.execute('SELECT * FROM feed_runs WHERE kind=? ORDER BY id DESC LIMIT 1', (kind,)).fetchone()
    return {**dict(row), 'summary': json.loads(row['summary'])} if row else None
