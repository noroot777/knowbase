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
from kb.source_types import link_body_refusal, link_body_sql, permits_link_body

TTL = 900
MAX_BODY = 200_000
METHODS = ('article', 'selection')
#: 内存里最多同时留几个等待中的请求。**撞上它的那一下不拒人**:同一篇文章只会有
#: 一个等待中的请求(`create` 自己把旧的顶掉),所以撞到这个数意味着另外 99 篇各占
#: 一个,而取消的出口只开在**各自那篇文章的补抓页**上 —— 拒了就得让人逐页去翻。
#: 这里改成顶掉**最老的那个**(它也最接近过期):等待中的请求手上没有任何产物,
#: 代价只是那一篇要重新点一次「发起浏览器采集」,而扩展那边收到的话正是
#: 「被新的采集请求替代」。
MAX_PENDING = 100
_lock = threading.RLock()
_intents: dict[str, dict] = {}
_NOT_PENDING = '请求已取消、被新的采集请求替代或条目已变化，请回补抓页查看当前请求'
_STALE = '条目已被其他操作修改或排除。本次没有覆盖，请重新核对并发起采集'
_OTHER_PAGE = '当前页面与指定文章网址不同。补抓页上已标出这一页的地址，可一键改用它重发，未保存正文'

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
        # 这条路也有一个**绕过界面**的入口:人手敲 `/browser-capture?item=<key>`。
        # 所以这句拒绝要把「为什么」一起说完,和「问题与待办」里那两颗灰按钮旁边
        # 那行字同源(`source_types.link_body_refusal`,判据仍只有上面这一句)。
        raise ValueError('此来源的正文不允许被原文链接中的页面替换，不能从浏览器补抓'
                         + (f'：{link_body_refusal(item["kind"])}'
                            if link_body_refusal(item['kind']) else ''))
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


def _same_life(item: dict, row) -> bool:
    """这张回执记的是不是**这一世**的这篇文章。

    `item_key` 按网址算:存量清理删掉一条、下一轮又从同一个链接收回来,key 一个字
    都没变,而库里那是另一行、另一份正文。两条判据各补对方的洞:`fetched_at` 只在
    INSERT 时写(`put_items` 撞 key 是 DO NOTHING,换内容的几个写入方一个都不碰它),
    是一世的身份;`content_rev` 在一行的生命里**只增**(见 `kb/feeds/schema.py`
    的 `_ADDED_COLUMNS`),新插进来的那一行从 0 起 —— 同一秒里删掉又收回来时
    `fetched_at` 会撞上(它只记到秒),这一下由倒退的 `content_rev` 认出来。

    **这两道组合有一个认不出来的角**:上一世的代次本来就是 0、又恰好在同一秒里被
    删掉重收时,两道一起判反。那个角由 `feed_items.life`(不可复用的生命周期标识,
    见 `kb/feeds/schema.py`)彻底关掉,搁置那一族判据已经换成它
    (`kb.intake_diag.shelf.live_sql`)。**这条回执链还没换过来** —— 它的审计表上
    没有那一列,换过去要连带改这一族的回归;记在
    `docs/decisions/intake-diagnostics.md` 的「还没做的」里。两边方向一致:认不出
    来一律当「没有回执 / 没有搁置」,不会把一个针对别的内容做过的决定搬到新内容上。

    认不出来就当**「没有回执」,不是「正文被改过」**:后者会在那篇文章的补抓页上
    常驻一句假红字,还连带藏掉回执区、禁掉继续处理那颗按钮,而重抓一次也去不掉
    (那一行审计记的是上一世,怎么比都不等)。
    """
    return (bool(row['item_fetched_at']) and item['fetched_at'] == row['item_fetched_at']
            and item['content_rev'] >= row['content_rev'])


def saved(identity: str) -> dict | None:
    with reg.db() as conn:
        row = conn.execute('SELECT * FROM browser_capture_audit WHERE id=?',
                           (identity,)).fetchone()
    if not row:
        return None
    item = target(row['item_key'])
    if not _same_life(item, row):
        return None
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
            # 取这个 key 最后那一张回执就行,**不在这里再判一次是哪一世**:
            # 判据只有 `_same_life` 一份,`saved` 认不出来时回 None,那正是
            # 「这篇文章没有回执」。
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
        waiting = sorted((other for other in _intents.values() if other['status'] == 'pending'),
                         key=lambda other: other['created_at'])
        for extra in waiting[:max(0, len(waiting) + 1 - MAX_PENDING)]:
            extra['status'] = 'superseded'
        identity = secrets.token_urlsafe(24)
        created = time.time()
        intent = dict(public(item), id=identity, page_url=expected, seen_url='',
                      content_hash=item['content_hash'], content_rev=item['content_rev'],
                      created_at=created, expires_at=created + TTL, status='pending', receipt=None)
        _intents[identity] = intent
        return view(intent)


def view(intent: dict) -> dict:
    return {name: intent[name] for name in
            ('id', 'key', 'kind', 'title', 'url', 'page_url', 'seen_url',
             'created_at', 'expires_at', 'status', 'receipt')}


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


#: **换掉一条的正文时要一起复位的那几列 —— 两个入口共用这一份。**
#:
#: 两个入口:扩展在那篇文章的标签上采回来的(`save`),和人在「问题与待办」里直接
#: 粘贴的(`save_pasted`,`kb.intake_diag`)。**不许各抄一份** —— 漏掉哪一列的后果
#: 都是旧结论赖在新正文身上:漏 `title_zh`/`body_zh`/`translated_at` 是新正文配
#: 旧译文(而译文是这条链上最贵的一步),漏 `fulltext_hold` 是正文已经补上了、
#: 待办还挂着,漏 `extract_stale` 是知识库里那份旧提取结果不作废。
#:
#: 绑定的六个值按顺序是:正文、正文指纹、语言、补抓时刻、来源说明、以及
#: 调用方自己的 WHERE 参数。
_REPLACE_BODY = """body=?,content_kind='content',
 content_hash=?,content_rev=content_rev+1,lang=?,fulltext_at=?,
 fulltext_error='',fulltext_hold='',extract_stale=1,body_provenance=?,
 title_zh='',body_zh='',translated_at='',translate_error='',translate_tries=0"""


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
    actual = page_url(captured_url)
    if actual != state['page_url']:
        _seen_elsewhere(identity, actual)
        raise ValueError(_OTHER_PAGE)
    return state


def _seen_elsewhere(identity: str, actual: str) -> None:
    """把**浏览器实际停在哪一页**记在这个待办上 —— 拒绝的那一刻手上就有它。

    调用方必须持 `_lock`。**只记,不判**:改不改用这个地址仍然要人在补抓页上
    点一下,所以「扩展不会用另一篇文章静默覆盖」一个字都没松 —— 松的只是
    「人得自己去地址栏抄一遍」。

    事故形态:站点按浏览器语言跳转(`openai.com/index/x` 在中文浏览器里落到
    `openai.com/zh-Hans-CN/index/x`)之后,这一类订阅**每一次**都被拒,而人看到
    的只有一句「网址不同」:真实的最终网址服务端此刻拿着,却丢掉了。更坏的是
    拒绝语原来指向的那条出路(「回知识库核对最终网址并重新发起」)在页面上**当场
    做不到** —— 有待办时那一栏和「发起」都是禁用的,得先在下面取消待办。
    **不在这里顺手剥掉语言前缀**:那是站点自己的约定,`/zh-Hans-CN` 在别的站
    完全可能是路径的真身,剥错一次就是把**另一篇**文章的正文写进这一条,而
    `page_url` 那份口径的底线正是「宽的那头只会多认一个同页地址」。
    """
    intent = _intents.get(identity)
    if intent and intent['status'] == 'pending':
        intent['seen_url'] = actual


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
            changed = conn.execute(
                f'UPDATE feed_items SET {_REPLACE_BODY}'
                ' WHERE key=? AND kind=? AND url=? AND content_hash=? AND content_rev=?'
                f" AND content_kind<>'post' AND x_post_meta='' AND {link_body_sql()}"
                ' AND NOT EXISTS (SELECT 1 FROM feed_item_exclusions WHERE key=feed_items.key)',
                (body, fulltext.body_hash(body), reg.text_language(current['title'], body),
                 reg.now(), json.dumps(provenance, ensure_ascii=False), intent['key'],
                 intent['kind'], intent['url'], intent['content_hash'], intent['content_rev']))
            if changed.rowcount != 1:
                intent['status'] = 'stale'
                raise ValueError(_STALE)
            # 前一世的回执行在这里一并收掉:它永远不会再被认领(`_same_life`),
            # 留着只会让审计表按「同一个 key 的历次收录」一直长。判据**特意比
            # `_same_life` 窄**(只认身份这一半):这样它删掉的一定是认领不了的行,
            # 两边口径万一走岔,也不会把一张还认得出来的回执抹掉。
            conn.execute('DELETE FROM browser_capture_audit WHERE item_key=? '
                         'AND item_fetched_at<>?', (intent['key'], current['fetched_at']))
            conn.execute('INSERT INTO browser_capture_audit '
                         '(id,item_key,provenance,body_digest,content_rev,item_fetched_at) '
                         'VALUES (?,?,?,?,?,?)',
                         (identity, intent['key'], json.dumps(provenance, ensure_ascii=False),
                          digest, intent['content_rev'] + 1, current['fetched_at']))
        receipt = _receipt(intent, digest, provenance, len(body))
        intent.update(status='saved', receipt=receipt)
        return dict(receipt)


def save_pasted(key: str, body: str, life: str = '') -> dict:
    """**人直接粘贴的正文** —— 「问题与待办」里那颗「手动补正文」的后端。

    和扩展那条路**共用准入判据和复位列**:正文够不够只有 `_admit_body` 一份
    (`grab_page.body_confirmed`,按行去重之后的字数门槛),要换的那几列只有
    `_REPLACE_BODY` 一份。所以这里不是第二套口径,而是同一套的第二个入口。

    **它拿不到、也不假装拿到「这一页就是那篇文章」的证据。** 扩展那条路有
    `_admit_page`(浏览器交回来的网址规范化之后必须等于指定的最终网址),而粘贴
    压根没有「一页」可比 —— 断言正文属于这篇文章的是**人自己**,所以
    `body_provenance` 记 `manual_paste` 并且 `confirmed=True` 指的是「人确认过」,
    不是「我们核对过那一页」。两者在知识库里要分得出来,这就是分得出来的地方。

    **互斥照旧待在 UPDATE 自己的 WHERE 里**(元规则),而且判据和扩展那条路**逐字
    相同**:key / 来源 / 原网址 / 正文指纹 / 修订号 / 非 X 观点 / 这一类来源准不准
    从链接取正文(`link_body_sql`,名单齐不齐由 `tests/test_link_body_entries.py`
    集合级地钉着)/ 未被永久排除。
    身份那几个值在**同一个写事务里现读**(`BEGIN IMMEDIATE` 之后),不在外面先
    SELECT 一次 —— 这中间 kb-web 里那一轮补抓 worker 完全可能已经把这一行换掉。
    `rowcount != 1` 就是拒绝信号。

    **`fulltext_at` 一写上,这条就退出自动补抓队列**(口径在
    `fulltext.queued_sql`)。好处是刚补的正文不会被下一轮补抓盖掉;代价是万一粘
    错了,出路是再粘一次(这条路可重复,不像导出那样不可逆)。

    **`life` 是人打开那个粘贴框时这一条在第几世**(口径在
    `kb.intake_diag.warrant.life_sql`)。上面那段「身份那几个值在同一个写事务里
    现读」防的是**并发写入方**(补抓 worker 刚把这一行换掉),它防不了**旧表单**:
    框开着的时候这条内容被清理删掉又从同一个链接收回来,现读到的是新一世的
    url / 指纹 / 修订号,于是这段正文**写得进去** —— 而人断言「这段正文属于这篇
    文章」时看着的是另一条(第 1 轮自由审查 R2)。带了就 AND 进同一条 UPDATE;
    没带照旧(扩展那条路有它自己那道 `_same_life`)。
    """
    from kb.intake_diag import warrant

    body, digest = _admit_body(body)
    life_cond, life_args = warrant.life_sql(life)
    with reg.db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = _checked_target(_items(conn, [key]).get(key))
        provenance = dict(method='manual_paste', page_url=current['url'],
                          page_title=current['title'], captured_at=reg.now(), confirmed=True)
        changed = conn.execute(
            f'UPDATE feed_items SET {_REPLACE_BODY}'
            ' WHERE key=? AND kind=? AND url=? AND content_hash=? AND content_rev=?'
            f" AND content_kind<>'post' AND x_post_meta='' AND {link_body_sql()}"
            f' AND ({life_cond})'
            ' AND NOT EXISTS (SELECT 1 FROM feed_item_exclusions WHERE key=feed_items.key)',
            (body, fulltext.body_hash(body),
             reg.text_language(current['title'], body), reg.now(),
             json.dumps(provenance, ensure_ascii=False), key, current['kind'],
             current['url'], current['content_hash'], current['content_rev'],
             *life_args))
        if changed.rowcount != 1:
            raise ValueError(_STALE)
    return {'key': key, 'characters': len(body), 'digest': digest,
            'content_rev': current['content_rev'] + 1}
