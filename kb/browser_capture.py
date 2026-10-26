"""浏览器补抓的准入判据 —— **「这一次写不写得了」只有这一份**。

硬约束、事故形态与回归清单在 `docs/decisions/browser-capture.md`;**改本文件的
`page_url` / `_admit_*` 之前先读那一份**。扩展侧(`kb/static/browser-extension/`)
不许再自己判网址、正文长度或条目版本:它只负责展示,判据一律问 `precheck`,
`precheck` 和 `save` 调的是同一组 `_admit_*`。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit

from kb.feeds import fulltext, grab_page, readable, registry as reg
from kb.source_types import permits_link_body

TTL = 900
MAX_BODY = 200_000
METHODS = ('article', 'selection')
_lock = threading.RLock()
_intents: dict[str, dict] = {}
_NOT_PENDING = '请求已取消、被新的采集请求替代或条目已变化，请回补抓页查看当前请求'
_STALE = '条目已被其他操作修改或排除。本次没有覆盖，请重新核对并发起采集'
_OTHER_PAGE = '当前页面与指定文章网址不同。请回知识库核对最终网址并重新发起，未保存正文'

_DEFAULT_PORTS = {'http': 80, 'https': 443}
#: WHATWG 的 path / query 百分号编码集之外的可打印 ASCII。`%` 留在里面是关键:
#: 已经编好的转义**原样透过**,所以 `/文章` 和 `/%E6%96%87%E7%AB%A0` 规范化到同一份。
_PATH_SAFE = "!$%&'()*+,-./:;=@[\\]^_|~"
_QUERY_SAFE = "!$%&()*+,-./:;=?@[\\]^_`{|}~"


def _without_dots(path: str) -> str:
    """去掉 `.` / `..` 段(RFC 3986 5.2.4,浏览器的 `new URL()` 也这么做)。

    末段是 `.` 或 `..` 时补一个空段 —— `/a/.` 在浏览器里是 `/a/`,不是 `/a`。
    """
    out: list[str] = []
    parts = path.removeprefix('/').split('/')
    for index, part in enumerate(parts):
        if part == '..' and out:
            out.pop()
        if part in ('.', '..'):
            if index == len(parts) - 1:
                out.append('')
        else:
            out.append(part)
    return '/' + '/'.join(out)


def page_url(value: str) -> str:
    """文章网址的**唯一**规范形 —— 「这两个地址是不是同一页」只认它。

    口径跟着浏览器的 `new URL().href`(扩展交回来的 `location.href` 就是那一份):
    scheme / 主机小写、非 ASCII 主机转 punycode、默认端口去掉、非 ASCII 与不安全
    字符百分号编码、已有转义原样透过、点段折叠、片段丢掉。空 query 两边都丢(比
    浏览器宽一格,宽的那头只会多认一个同页地址,不会把另一篇认成这一篇)。

    事故形态(2026-10-26 审查实测):服务端原来直接 `urlunsplit` 拼回原字符串,
    而扩展自己 `new URL()` 比一遍 —— 网址里带中文的订阅**每一次**都是人把整篇
    文章读完、勾完确认之后才被 409 拒,因为扩展比的是 `/文章` 对 `/文章`、服务端
    收到的是浏览器编码过的 `/%E6%96%87%E7%AB%A0`。`:443`、路径带空格、末尾 `?`
    是同一形态的另外三种。**判据不许在扩展里再写一份**,那只会开出第四第五种。

    `%2e` 这种编码过的点段不折叠(浏览器会)。两边都过本函数,所以后果止于「人
    自己填了一个点段地址、浏览器把它折叠了」→ 读之前就被告知网址不同,可改可重发。
    """
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme not in _DEFAULT_PORTS or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError
        host = parts.hostname
        if not host.isascii():
            host = host.encode('idna').decode()
        if ':' in host:
            host = f'[{host}]'
        port = parts.port
        netloc = host if port in (None, _DEFAULT_PORTS[parts.scheme]) else f'{host}:{port}'
        path = quote(_without_dots(parts.path), safe=_PATH_SAFE)
        return urlunsplit((parts.scheme, netloc, path, quote(parts.query, safe=_QUERY_SAFE), ''))
    except ValueError:
        raise ValueError('请使用不含账号密码的完整 HTTP / HTTPS 文章网址') from None


def pairing_key() -> str:
    with reg.db() as conn:
        conn.execute('INSERT OR IGNORE INTO feed_meta(key,value) VALUES (?,?)',
                     ('browser_capture_pairing', secrets.token_urlsafe(32)))
        return conn.execute('SELECT value FROM feed_meta WHERE key=?',
                            ('browser_capture_pairing',)).fetchone()[0]


def _items(conn, keys: list[str]) -> dict[str, dict]:
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}
    slots = ','.join('?' for key in keys)
    rows = conn.execute(f'SELECT feed_items.*, EXISTS '
                        f'(SELECT 1 FROM feed_item_exclusions WHERE key=feed_items.key) '
                        f'AS capture_excluded FROM feed_items WHERE key IN ({slots})', keys)
    return {row['key']: dict(row) for row in rows}


def _checked_target(item: dict | None) -> dict:
    if not item or item['capture_excluded']:
        raise LookupError('条目不存在或已不再收录')
    if not permits_link_body(item['kind']):
        raise ValueError('此来源的正文不允许被原文链接中的页面替换，不能从浏览器补抓')
    if item['content_kind'] == 'post' or item['x_post_meta']:
        raise ValueError('此入口用于单篇文章正文，不处理 X 观点或时间线覆盖问题')
    page_url(item['url'])
    return item


def target(key: str) -> dict:
    with reg.db() as conn:
        return _checked_target(_items(conn, [key]).get(key))


def writable(intent: dict, item: dict | None) -> bool:
    try:
        _checked_target(item)
    except (LookupError, ValueError):
        return False
    return all(intent[field] == item[field] for field in
               ('key', 'content_hash', 'content_rev', 'kind', 'url'))


def _refresh_status(intent: dict, item: dict | None) -> bool:
    if intent['status'] == 'pending' and not writable(intent, item):
        intent['status'] = 'stale'
    return intent['status'] == 'pending'


def public(item: dict) -> dict:
    return {name: item[name] for name in ('key', 'kind', 'title', 'url')}


def _receipt(item: dict, digest: str, provenance: dict, characters: int) -> dict:
    return dict(saved=True, key=item['key'], kind=item['kind'], digest=digest,
                captured_at=provenance['captured_at'], characters=characters,
                detail='正文已补入原条目；翻译、导出与索引仍待执行，不代表已经完成入库')


def saved(identity: str) -> dict | None:
    with reg.db() as conn:
        row = conn.execute('SELECT * FROM browser_capture_audit WHERE id=?',
                           (identity,)).fetchone()
    if not row:
        return None
    item = target(row['item_key'])
    digest = hashlib.sha256(item['body'].encode()).hexdigest()
    if item['content_rev'] != row['content_rev'] or digest != row['body_digest']:
        raise ValueError('本次保存后正文已被修改，请到同步中心处理最新内容，或重新发起采集')
    provenance = json.loads(row['provenance'])
    return dict(public(item), id=identity, page_url=provenance['page_url'],
                expires_at=0, status='saved',
                receipt=_receipt(item, digest, provenance, len(item['body'])))


def item_state(key: str) -> dict:
    with _lock:
        item = target(key)
        _prune()
        waiting = []
        for intent in _intents.values():
            if intent['key'] == key and _refresh_status(intent, item):
                waiting.append(view(intent))
        with reg.db() as conn:
            row = conn.execute('SELECT id FROM browser_capture_audit WHERE item_key=? '
                               'ORDER BY rowid DESC LIMIT 1', (key,)).fetchone()
        receipt, error = None, ''
        if row:
            try:
                receipt = saved(row['id'])
            except ValueError as problem:
                error = str(problem)
        return dict(public(item), pending=waiting, saved=receipt, saved_error=error)


def _prune() -> None:
    expired = [identity for identity, intent in _intents.items()
               if time.time() > intent['expires_at']]
    for identity in expired:
        del _intents[identity]


def create(key: str, expected_url: str) -> dict:
    expected = page_url(expected_url)
    with _lock:
        item = target(key)
        _prune()
        for intent in _intents.values():
            if intent['key'] != key or intent['status'] != 'pending':
                continue
            if intent['page_url'] == expected and writable(intent, item):
                return view(intent)
            intent['status'] = 'superseded'
        if sum(intent['status'] == 'pending' for intent in _intents.values()) >= 100:
            raise ValueError('等待中的采集请求过多，请完成或取消已有请求')
        identity = secrets.token_urlsafe(24)
        created = time.time()
        intent = dict(public(item), id=identity, page_url=expected,
                      content_hash=item['content_hash'], content_rev=item['content_rev'],
                      created_at=created, expires_at=created + TTL, status='pending', receipt=None)
        _intents[identity] = intent
        return view(intent)


def view(intent: dict) -> dict:
    return {name: intent[name] for name in
            ('id', 'key', 'kind', 'title', 'url', 'page_url', 'created_at', 'expires_at', 'status', 'receipt')}


def get(identity: str) -> dict:
    with _lock:
        _prune()
        if identity in _intents and _intents[identity]['status'] != 'saved':
            return view(_intents[identity])
        restored = saved(identity)
        if restored:
            return restored
        raise LookupError('采集请求已过期、服务已重启或请求不存在，请重新发起')


def pending() -> list[dict]:
    with _lock:
        _prune()
        waiting = [intent for intent in _intents.values() if intent['status'] == 'pending']
        if not waiting:
            return []
        with reg.db() as conn:
            items = _items(conn, [intent['key'] for intent in waiting])
        return [view(intent) for intent in waiting
                if _refresh_status(intent, items.get(intent['key']))]


def cancel(identity: str) -> dict:
    with _lock:
        state = get(identity)
        if state['status'] != 'pending':
            return state
        _intents[identity]['status'] = 'cancelled'
        return view(_intents[identity])


def _admit_body(body: str) -> tuple[str, str]:
    """正文**够不够** —— 唯一判据,返回规整过的正文和它的摘要。

    门槛是 `grab_page.body_confirmed`(按**行**去重之后的字数,见
    `readable.substance`)。扩展侧一度自己数原始长度:复读的登录提示页在那边
    1367 字过线、这边去重只剩 260 字,于是人把整页读完、勾完确认才被拒。
    **不许在扩展里再数一遍**。
    """
    body = body.replace('\r\n', '\n').replace('\r', '\n').strip()
    if len(body) > MAX_BODY or not grab_page.body_confirmed(body):
        raise ValueError(f'正文不足 {readable.MIN_CHARS} 个不重复字符，或超过 '
                         f'{MAX_BODY // 10000} 万字符；请检查是否只采到了选段或提示页')
    return body, hashlib.sha256(body.encode()).hexdigest()


def _admit_page(identity: str, captured_url: str, method: str) -> dict:
    """这一页**写不写得了** —— 请求在不在、来源允不允许、是不是指定的那一页。

    调用方必须持 `_lock`。`precheck` 和 `save` 走的是同一份;扩展**一处都不判**。
    """
    state = get(identity)
    if method not in METHODS:
        raise ValueError('未知的采集方式')
    if not permits_link_body(state['kind']):
        raise ValueError('此来源不允许从原文链接替换正文，未保存')
    if page_url(captured_url) != state['page_url']:
        raise ValueError(_OTHER_PAGE)
    return state


def _admit_item(intent: dict) -> None:
    """条目**还是**请求绑定的那一版吗 —— 读之前就问,不等人读完。

    这只是**早一道**:真正的互斥在 `save` 那条 UPDATE 自己的 WHERE 里。
    """
    with reg.db() as conn:
        if not _refresh_status(intent, _items(conn, [intent['key']]).get(intent['key'])):
            raise ValueError(_STALE)


def precheck(identity: str, captured_url: str, body: str, method: str) -> dict:
    """读正文之前(以及渲染预览之前)先问一次,判据和 `save` 同一组,只是不落库。

    事故形态:扩展信的是上一次刷新下拉时缓存的那份清单,于是「已经写不进去的
    请求」要等人读完整页、核对完预览、点下提交才被 409 拒 —— 白读一整页。
    """
    characters = len(_admit_body(body)[0]) if body else 0
    with _lock:
        state = _admit_page(identity, captured_url, method)
        if state['status'] != 'pending':
            raise ValueError(_NOT_PENDING)
        _admit_item(_intents[identity])
        return dict(ok=True, id=identity, key=state['key'], title=state['title'],
                    page_url=state['page_url'], characters=characters)


def save(identity: str, captured_url: str, title: str, body: str, method: str) -> dict:
    body, digest = _admit_body(body)
    with _lock:
        state = _admit_page(identity, captured_url, method)
        if state['status'] == 'saved':
            if state['receipt']['digest'] == digest:
                return dict(state['receipt'])
            raise ValueError('此请求已提交，请勿用另一份正文重复覆盖')
        if state['status'] != 'pending':
            raise ValueError(_NOT_PENDING)
        intent = _intents[identity]
        actual_url = state['page_url']
        provenance = dict(method='browser_' + method, page_url=actual_url,
                          page_title=title, captured_at=reg.now(), confirmed=True)
        with reg.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = _items(conn, [intent['key']]).get(intent['key'])
            allowed = _refresh_status(intent, current)
            if not current:
                raise LookupError('条目已被删除，未写入正文')
            if not allowed:
                raise ValueError(_STALE)
            changed = conn.execute("""UPDATE feed_items SET body=?,content_kind='content',
                content_hash=?,content_rev=content_rev+1,lang=?,fulltext_at=?,
                fulltext_error='',fulltext_hold='',extract_stale=1,body_provenance=?,
                title_zh='',body_zh='',translated_at='',translate_error='',translate_tries=0
                WHERE key=? AND kind=? AND url=? AND content_hash=? AND content_rev=?
                AND content_kind<>'post' AND x_post_meta=''
                AND NOT EXISTS (SELECT 1 FROM feed_item_exclusions WHERE key=feed_items.key)""",
                (body, fulltext.body_hash(body), reg.text_language(current['title'], body),
                 reg.now(), json.dumps(provenance, ensure_ascii=False), intent['key'],
                 intent['kind'], intent['url'], intent['content_hash'], intent['content_rev']))
            if changed.rowcount != 1:
                intent['status'] = 'stale'
                raise ValueError(_STALE)
            conn.execute('INSERT INTO browser_capture_audit '
                         '(id,item_key,provenance,body_digest,content_rev) VALUES (?,?,?,?,?)',
                         (identity, intent['key'], json.dumps(provenance, ensure_ascii=False),
                          digest, intent['content_rev'] + 1))
        receipt = _receipt(intent, digest, provenance, len(body))
        intent.update(status='saved', receipt=receipt)
        return dict(receipt)
