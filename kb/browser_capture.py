from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from kb.feeds import fulltext, grab_page, registry as reg
from kb.source_types import link_body_sql, permits_link_body

TTL = 900
MAX_BODY = 200_000
_lock = threading.RLock()
_intents: dict[str, dict] = {}


def page_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError
        parts.port
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or '/', parts.query, ''))
    except ValueError:
        raise ValueError('请使用不含账号密码的完整 HTTP / HTTPS 文章网址') from None


def pairing_key() -> str:
    with reg.db() as conn:
        conn.execute('INSERT OR IGNORE INTO feed_meta(key,value) VALUES (?,?)',
                     ('browser_capture_pairing', secrets.token_urlsafe(32)))
        return conn.execute('SELECT value FROM feed_meta WHERE key=?',
                            ('browser_capture_pairing',)).fetchone()[0]


def target(key: str) -> dict:
    with reg.db() as conn:
        row = conn.execute('SELECT * FROM feed_items WHERE key=? AND NOT EXISTS '
                           '(SELECT 1 FROM feed_item_exclusions WHERE key=feed_items.key)',
                           (key,)).fetchone()
    if not row:
        raise LookupError('条目不存在或已不再收录')
    item = dict(row)
    if not permits_link_body(item['kind']):
        raise ValueError('此来源的正文不允许被原文链接中的页面替换，不能从浏览器补抓')
    if item['content_kind'] == 'post' or item['x_post_meta']:
        raise ValueError('此入口用于单篇文章正文，不处理 X 观点或时间线覆盖问题')
    page_url(item['url'])
    return item


def public(item: dict) -> dict:
    return {name: item[name] for name in ('key', 'kind', 'title', 'url')}


def _prune() -> None:
    expired = [identity for identity, intent in _intents.items()
               if time.time() > intent['expires_at']]
    for identity in expired:
        del _intents[identity]


def create(key: str, expected_url: str) -> dict:
    item = target(key)
    with _lock:
        _prune()
        if len(_intents) >= 100:
            raise ValueError('等待中的采集请求过多，请完成或取消已有请求')
        identity = secrets.token_urlsafe(24)
        intent = dict(public(item), id=identity, page_url=page_url(expected_url),
                      content_hash=item['content_hash'], content_rev=item['content_rev'],
                      expires_at=time.time() + TTL, status='pending', receipt=None)
        _intents[identity] = intent
        return view(intent)


def view(intent: dict) -> dict:
    return {name: intent[name] for name in
            ('id', 'key', 'kind', 'title', 'url', 'page_url', 'expires_at', 'status', 'receipt')}


def get(identity: str) -> dict:
    with _lock:
        _prune()
        if identity not in _intents:
            raise LookupError('采集请求已过期、服务已重启或请求不存在，请重新发起')
        return view(_intents[identity])


def pending() -> list[dict]:
    with _lock:
        _prune()
        return [view(intent) for intent in _intents.values() if intent['status'] == 'pending']


def cancel(identity: str) -> dict:
    with _lock:
        get(identity)
        intent = _intents[identity]
        if intent['status'] == 'pending':
            intent['status'] = 'cancelled'
        return view(intent)


def save(identity: str, captured_url: str, title: str, body: str, method: str) -> dict:
    actual_url = page_url(captured_url)
    body = body.replace('\r\n', '\n').replace('\r', '\n').strip()
    if len(body) > MAX_BODY or not grab_page.body_confirmed(body):
        raise ValueError('正文不足 400 个不重复字符，或超过 20 万字符；请检查是否只采到了选段或提示页')
    if method not in ('article', 'selection'):
        raise ValueError('未知的采集方式')
    digest = hashlib.sha256(body.encode()).hexdigest()
    with _lock:
        get(identity)
        intent = _intents[identity]
        if actual_url != intent['page_url']:
            raise ValueError('当前页面与指定文章网址不同。请回知识库核对最终网址并重新发起，未保存正文')
        if intent['status'] == 'saved':
            if intent['receipt']['digest'] == digest:
                return dict(intent['receipt'])
            raise ValueError('此请求已提交，请勿用另一份正文重复覆盖')
        if intent['status'] != 'pending':
            raise ValueError('请求已取消或条目已变化，请重新发起')
        provenance = dict(method='browser_' + method, page_url=actual_url,
                          page_title=title, captured_at=reg.now(), confirmed=True)
        with reg.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT title FROM feed_items WHERE key=?',
                                   (intent['key'],)).fetchone()
            if not current:
                raise LookupError('条目已被删除，未写入正文')
            changed = conn.execute(f"""UPDATE feed_items SET body=?,content_kind='content',
                content_hash=?,content_rev=content_rev+1,lang=?,fulltext_at=?,
                fulltext_error='',fulltext_hold='',extract_stale=1,body_provenance=?,
                title_zh='',body_zh='',translated_at='',translate_error='',translate_tries=0
                WHERE key=? AND kind=? AND url=? AND content_hash=? AND content_rev=?
                AND content_kind<>'post' AND x_post_meta='' AND {link_body_sql()}
                AND NOT EXISTS (SELECT 1 FROM feed_item_exclusions WHERE key=feed_items.key)""",
                (body, fulltext.body_hash(body), reg.text_language(current['title'], body),
                 reg.now(), json.dumps(provenance, ensure_ascii=False), intent['key'],
                 intent['kind'], intent['url'], intent['content_hash'], intent['content_rev']))
            if changed.rowcount != 1:
                intent['status'] = 'stale'
                raise ValueError('条目已被其他操作修改或排除。本次没有覆盖，请重新核对并发起采集')
            conn.execute('CREATE TABLE IF NOT EXISTS browser_capture_audit '
                         '(id TEXT PRIMARY KEY,item_key TEXT NOT NULL,provenance TEXT NOT NULL,'
                         'body_digest TEXT NOT NULL,content_rev INTEGER NOT NULL)')
            conn.execute('INSERT INTO browser_capture_audit VALUES (?,?,?,?,?)',
                         (identity, intent['key'], json.dumps(provenance, ensure_ascii=False),
                          digest, intent['content_rev'] + 1))
        receipt = dict(saved=True, key=intent['key'], kind=intent['kind'], digest=digest,
                       captured_at=provenance['captured_at'], characters=len(body),
                       detail='正文已补入原条目；翻译、导出与索引仍待执行，不代表已经完成入库')
        intent.update(status='saved', receipt=receipt)
        return dict(receipt)
