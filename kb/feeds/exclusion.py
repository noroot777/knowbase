from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from kb import config
from kb.collectors import CollectorError, DropIncomplete
from kb.feeds.paths import export_path
from kb.maintenance import LockBusy, maintenance_lock

SCHEMA = '''
CREATE TABLE IF NOT EXISTS feed_item_exclusions (
 key TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL,
 path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS feed_items_excluded_insert
BEFORE INSERT ON feed_items
WHEN EXISTS (SELECT 1 FROM feed_item_exclusions WHERE key=NEW.key)
BEGIN SELECT RAISE(IGNORE); END;
'''


def item_key(kind: str, url: str) -> str:
    return hashlib.sha256((kind + ':' + url).encode()).hexdigest()[:32]


def failure(item, reason, kind: str | None = None) -> dict:
    item = dict(item)
    result = {'title': str(item.get('title') or '未命名内容')[:120],
              'reason': str(reason)[:300]}
    url = item.get('url') or ''
    if url:
        result['url'] = url
    channel = kind or item.get('kind')
    key = item.get('key')
    if channel and key and url and item_key(channel, url) == key:
        result['drop'] = {'channel': channel, 'id': key, 'link': url}
    return result


def _export_path(kind: str, relative: str) -> Path | None:
    if not relative:
        return None
    vault = config.VAULT_ROOT.resolve()
    root = (vault / 'source' / kind).resolve()
    path = (config.VAULT_ROOT / relative).resolve()
    if (Path(relative).is_absolute() or not root.is_relative_to(vault)
            or not path.is_relative_to(root) or path.suffix != '.md'):
        raise CollectorError('导出路径不属于这一来源，未执行移除')
    return path


def _cleanup(kind: str, relatives: list[str]) -> None:
    for relative in relatives:
        _export_path(kind, relative).unlink(missing_ok=True)
    if config.DB_PATH.is_file():
        from kb import store
        conn = store.connect(config.DB_PATH)
        try:
            store.remove_documents(conn, relatives)
            conn.commit()
        finally:
            conn.close()


def _drop(kind: str, ref: dict) -> str:
    from kb.feeds import registry as reg

    reg.check_kind(kind)
    key = str(ref.get('id') or '').strip()
    link = str(ref.get('link') or '').strip()
    if not key:
        raise CollectorError('这一条没有可用的标识，无法移除')
    with reg.db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM feed_items WHERE key=?', (key,)).fetchone()
        previous = conn.execute('SELECT * FROM feed_item_exclusions WHERE key=?', (key,)).fetchone()
        known = row or previous
        if known:
            if known['kind'] != kind or (link and link != known['url']):
                raise CollectorError('条目身份与这一来源或原文链接不符，未执行移除')
            link = known['url']
        if not link or item_key(kind, link) != key:
            raise CollectorError('缺少可核实的原文身份，未执行移除')
        try:
            reg.normalize_url(link)
        except ValueError as error:
            raise CollectorError(str(error)) from None
        relative = known['path'] if known else ''
        relatives = list(dict.fromkeys(filter(None, [str(export_path(kind, key)), relative])))
        for candidate in relatives:
            _export_path(kind, candidate)
        conn.execute('INSERT OR IGNORE INTO feed_item_exclusions VALUES(?,?,?,?,?)',
                     (key, kind, link, relative, reg.now()))
        conn.execute('DELETE FROM subscription_items WHERE item_key=?', (key,))
        conn.execute('DELETE FROM feed_items WHERE key=?', (key,))
    try:
        _cleanup(kind, relatives)
    except (OSError, sqlite3.Error) as error:
        raise DropIncomplete(f'已永久排除，但已导出内容清理未完成，请重试：{error}') from None
    return '已移除这一条内容；以后从此来源的其他订阅再次发现同一篇，也不再收录。'


def drop_item(kind: str, ref: dict) -> str:
    try:
        with maintenance_lock(wait=5):
            return _drop(kind, ref)
    except LockBusy:
        raise CollectorError('正在导出或建立索引，尚未移除；请稍后重试') from None
