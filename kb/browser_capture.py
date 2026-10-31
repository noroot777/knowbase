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
#: 一个,而取消的出口只开在**各自那篇文章的采集抽屉**里 —— 拒了就得让人逐条去翻。
#: 这里改成顶掉**最老的那个**(它也最接近过期):等待中的请求手上没有任何产物,
#: 代价只是那一篇要重新点一次「发起浏览器采集」,而扩展那边收到的话正是
#: 「被新的采集请求替代」。
MAX_PENDING = 100
_lock = threading.RLock()
_intents: dict[str, dict] = {}
_NOT_PENDING = '请求已取消、被新的采集请求替代或条目已变化，请回采集抽屉查看当前请求'
_STALE = '条目已被其他操作修改或排除。本次没有覆盖，请重新核对并发起采集'
_OTHER_PAGE = '当前页面与指定文章网址不同。采集抽屉里已标出这一页的地址，可一键改用它重发，未保存正文'

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
    文章读完之后才被 409 拒,因为扩展比的是 `/文章` 对 `/文章`、服务端
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
        raise ValueError('此来源的正文不允许被原文链接中的页面替换，不能从浏览器采集'
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

    认不出来就当**「没有回执」,不是「正文被改过」**:后者会在那篇文章的采集抽屉里
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


def grab_owing(stored_url: str) -> str:
    """这一条还欠后半条链的哪一步 —— 一句人话,走完了是空串。

    **只是转手**:`grab_store.known` 问的是导出过没有、vault 里那一篇还在不在、
    当前那一份进没进索引,`owing` 把它们变成一句话。按**库里那一行自己的 url**
    问(不是浏览器那个地址),所以这一问和上面按别名找到的那一行永远是同一条。
    """
    try:
        from kb.feeds.grab_store import known, owing
        seen = known(stored_url)
    except Exception:
        return ''
    return owing(seen) if seen else ''


def _page_aliases(raw: str) -> list[str]:
    """这一页**在库里可能长什么样** —— 两份规范形,都由同一页推出来。

    为什么要两份:库里那一行的 `url` 是**收它的那条路**存下来的。订阅类来源原样
    存 feed 给的地址(`page_url` 那一套口径),手动收录存的是 `grab.normalize`
    (它顺手去掉 `utm_*`/`fbclid`/`gclid`、把推文地址收成 `x.com/<handle>/status/<id>`)。
    只按一份问,另一条路收的那一半永远答「不在库里」—— 而「不在库里」的下一步是
    **收录**,于是同一篇文章在库里变成两行、两篇 md、检索里两份结果。

    **这不是模糊匹配**:两个候选都是**这一页自己**的规范形,一个字符都不猜。
    `page_url` 那条底线照旧(「宽的那头只会多认一个同页地址,不会把另一篇认成这
    一篇」),所以**绝不在这里剥语言前缀、也不读网页自报的 canonical**:剥错一次
    就是把另一篇的正文写进这一条。真遇上站点按浏览器语言跳转的那一类,出路是
    `here()` 把等着的待办连同它指的地址一起交回去,由人点一下改用这一页 ——
    判据仍然在服务端,松的只是「人得自己去地址栏抄一遍」。
    """
    names = [page_url(raw)]
    try:
        from kb.feeds.grab import normalize
        alias = normalize(raw)
    except Exception:
        alias = ''
    if alias and alias not in names:
        names.append(alias)
    return names


#: **「这一条的正文还欠着」** —— 三档正文待办(`body_queued` / `body_held` /
#: `body_exhausted`)的共同前缀,不是第四份口径:资格那一半原样转
#: `fulltext._eligible()`,另一半就是「还没抓到过」。
#:
#: 刻意**不带** `shelf`(人点过暂缓):那一道管的是「面板上还列不列它」,而此刻
#: 人正站在那篇文章上点扩展 —— 他自己就是那个决定。
_OWED = f"({fulltext._eligible()} AND fulltext_at='')"


def here(url: str, intent_id: str = '') -> dict:
    """人**正站在哪一页** → 这一页此刻能做什么。一问一答。

    **这条路存在的理由是把决定搬到人所在的地方。** 原先补一篇正文要跨三个界面
    走七步:知识库面板 → 采集抽屉复制密钥 → 核对最终网址 → 发起待办 → 回到文章
    标签 → 在下拉框里挑出刚才那个待办 → 读取。而「这一页是哪一条、写不写得了」
    这个问题,扩展站在那一页上本来就问得出来 —— 地址栏里那一行就是答案,下拉框
    是让人把服务端已经知道的事再回答一遍。

    **待办没有被取消,它退到了幕后**:`situation='fill'` 时这里自己 `create` 一个
    (扩展拿到的是它的 `id`),于是 `precheck` / `save` 那一整套准入、CAS、回执与
    审计一个字都不用改 —— 新增的只有「谁来建待办」。`create` 本身幂等:同一
    (条目, 页面)上已有 pending 就复用它和它的有效期,所以反复开扩展不会堆出一串。

    **只在正文还欠着的时候自动建**(`_OWED`)。正文已经齐了的那一条照旧要人在采集
    抽屉里明确说一句「重新采集正文」—— 那是另一件事(「我手上这份是错的」),它自带
    警示和回执,不该和「把缺的那份补上」挤成同一颗按钮。

    **但人明确发起过的那一个要先兑现,排在库里那一行前面。** 「这一页是哪一条」
    有两个答案源:`feed_items.url`,和人自己指过的最终网址。只问前者的话,
    「发起浏览器采集」「改用这一页的地址」「重新采集正文」这三条路发出的待办在
    扩展那一侧**一次都不会被认到** —— 而它们存在的全部理由恰恰是「这一页的地址
    和库里那一行不一样」或者「正文已经齐了、但我手上这份是错的」。
    两种实测形态:①按语言跳转的站点上发起(或 adopt)之后再问一次,答的是
    `absent`,主按钮于是走 `/api/grab` **把同一篇收成第二行**;②正文齐了的那一条
    点过「重新采集正文」并发起,答的是 `complete`、`intent=null`,弹窗一颗按钮都
    不给 —— 而撤掉预览之后,那正是唯一的事后纠错出口。

    **人点过「这一页就是这一篇」的话,那一下就是答案,不许再按别的规则重判一次。**
    `intent_id` 就是那一下的身份(`/here/adopt` 把待办交回扩展,扩展带着它再问)。
    **选不出来就不代选**(`undecided`,不带 `intent`,扩展于是一颗采集按钮都不给):
    同一页上不止一个条目在等而人没点过,或者他点的那个已经失效而别的还在等。
    准入那几道门证明的是「这一条写得了」,**证明不了「人刚才选的就是这一条」**。
    丢掉身份、或者按创建时间代选的实测后果(正文写进另一篇、不可恢复)见
    `docs/decisions/browser-capture.md`。

    `elsewhere` 交回的是**除这一答用掉的那个之外、还等着的所有待办**(指着别页的,
    和同一页上没被选中的)。这一项是承重的:站点按浏览器语言跳转时
    (`openai.com/index/x` → `/zh-Hans-CN/index/x`),人从采集抽屉发起的待办指着前者、
    浏览器停在后者,按网址一问「这一页不在库里」—— 要是只凭这一句就请他收录,库里
    立刻多出一行同一篇文章。把那些待办摆在他眼前、各给一颗「改用这一页的地址」,
    他一眼就看出是同一篇;而 `undecided` 那一档全靠它把候选交出去。
    """
    page = page_url(url)
    columns = ('SELECT feed_items.*, EXISTS (SELECT 1 FROM feed_item_exclusions'
               ' WHERE key=feed_items.key) AS capture_excluded,'
               f' {_OWED} AS owed FROM feed_items')
    with _lock:
        _prune()
        # 指着**这一页**、而且还写得进去的那些待办。`_pending()` 这一道不能自己
        # 再写一遍:来源许可、排除、CAS 全在它的 `_refresh_status` 里,绕过去就是
        # 把一个已经作废的待办当成「人明确发起过」来兑现。
        bound = sorted((one for one in _pending() if one['page_url'] == page),
                       key=lambda one: one['created_at'])

        def answered(situation: str, detail: str,
                     item: dict | None = None, intent: dict | None = None) -> dict:
            # **`elsewhere` 一律在最后算。** 下面那个 `_create` 会把同一条目上
            # 指着别处的旧待办标成 `superseded`,先算就会把刚被顶掉的那个摆出来、
            # 请人「改用这一页的地址」,而它已经作废了。
            #
            # 排掉的只有**这一答用的那一个**:同一页上另一个条目的待办必须摆出来 ——
            # `undecided` 那一档就是靠它把候选交出去的,而人正要在里头点一下。
            return dict(ok=True, page_url=page, situation=situation, detail=detail,
                        item=public(item) if item else None, intent=intent,
                        elsewhere=[other for other in _pending()
                                   if other['id'] != (intent['id'] if intent else None)])

        # 人点过的那一下优先,而且是**唯一**的依据。
        picked = next((one for one in bound if one['id'] == intent_id), None) if intent_id else None
        if not picked:
            # **带了显式身份、却兑现不了 —— 一律不代选。** 判据不许挂在「这一页还剩
            # 几个待办」上:一个不剩时下面那条按网址查找会命中另一篇、再给它新建一个
            # 合法待办,而 precheck/CAS/upload 验的只是「这个新待办写得了」,没有一道
            # 在验「它还是人刚才选的那一篇」。形态见 docs/decisions/browser-capture.md。
            undecided = bool(intent_id) or len({one['key'] for one in bound}) > 1
            if undecided:
                return answered('undecided',
                                '你选的那个采集请求已经失效了。下面还在等的条目里点一下'
                                '要补的那一篇；一个都没有就回采集抽屉里重新发起。'
                                if intent_id else
                                f'有 {len(bound)} 个条目都在等这一页的正文，'
                                '点一下你要补的那一篇。')
            picked = bound[0] if bound else None

        with reg.db() as conn:
            if picked:
                row = conn.execute(columns + ' WHERE key=?', (picked['key'],)).fetchone()
            else:
                aliases = _page_aliases(url)
                slots = ','.join('?' for name in aliases)
                row = conn.execute(columns + f' WHERE url IN ({slots})'
                                   ' ORDER BY rowid LIMIT 1', aliases).fetchone()
        item = dict(row) if row else None
        if not item:
            return answered('absent', '这一页还不在知识库里。')
        try:
            _checked_target(item)
        except (LookupError, ValueError) as why:
            return answered('refused', str(why), item)
        if picked:
            # 人明确发起过:照它答,**不再按「正文欠不欠」重判一次**。正文已经齐了
            # 的那一条正是「重新采集正文」会走到的形态,按 owed 降级成「齐了,没有
            # 可做的事」就把那条出路堵死了。准入一道没少:这个待办要能走到这里,
            # 上面 `_pending()` 的 `writable` 已经过了来源、排除和 CAS。
            return answered('fill', '知识库里有这一条，但正文还缺着。' if item['owed']
                            else '你为这一条发起过重新采集，这一页就是它指的那一篇。',
                            item, picked)
        if not item['owed']:
            # 「在 `feed_items` 里有这一行」和「知识库里搜得到它」是两件事:导出、
            # 索引那半条链占不到管道、或者在保存之后服务重启,都会留下一条「在库
            # 里但搜不到」。那句话**只有 `grab_store.owing` 一份口径**(它自己的
            # docstring 记着为什么),这里转手,不另编 —— 各写一遍的话其中一处
            # 迟早把「文件不见了」说成「还在排队」。
            owed = grab_owing(item['url'])
            return answered('complete', '这一条已经在知识库里，正文也齐了。'
                            + (f'不过{owed}。' if owed else ''), item)
        return answered('fill', '知识库里有这一条，但正文还缺着。', item, _create(item, page))


def _prune() -> None:
    expired = [identity for identity, intent in _intents.items()
               if time.time() > intent['expires_at']]
    for identity in expired:
        del _intents[identity]


def create(key: str, expected_url: str) -> dict:
    expected = page_url(expected_url)
    with _lock:
        return _create(target(key), expected)


def _create(item: dict, expected: str) -> dict:
    """建(或复用)一个待办。**调用方必须持 `_lock`,且 `item` 已过 `_checked_target`。**

    拆出来是因为有两个发起方:补抓页上那颗「发起浏览器采集」(`create`,从 key 查
    条目),和扩展站在文章上那一问(`here`,它手上已经有那一行、也已经检过)。
    **各写一遍的话复用、顶替、额度那三段就有两份** —— 而那三段决定的是「同一篇
    文章会不会同时挂着两个待办」。
    """
    _prune()
    for intent in _intents.values():
        if intent['key'] != item['key'] or intent['status'] != 'pending':
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
        return _pending()


def _pending() -> list[dict]:
    """还等着的待办。**调用方必须持 `_lock`,且已 `_prune()`。**

    拆出来是因为 `here()` 也要这一份:它要回答「有没有待办指着另一个地址」,
    而那必须是**还写得进去的**那些 —— `_refresh_status` 这一道不能漏(条目在这
    期间被同步改过、被排除、被删掉的,照旧要当「没有待办」看)。自己再过一遍
    `_intents` 就是第二份口径,漏掉那一道的后果是把一个已经作废的待办摆在人
    眼前、请他「改用这一页的地址」,而点下去只会被拒。
    """
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
    1367 字过线、这边去重只剩 260 字,于是人把整页读完才被拒。
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
        # `confirmed` 记的是**有没有人逐字看过这份正文**,不是「这次写入合不合法」。
        # 扩展那道「我已核对」撤掉之后(取舍见 docs/decisions/browser-capture.md),
        # 这条路上确实没人看过 —— 人选了这一页、点了采集,正文是提取器读的。
        # 照旧记 True 就是第二句假证词,而 `manual_paste` 那条(人自己粘的)仍然是
        # True:**两者在知识库里要分得出来,这里就是分得出来的地方。**
        provenance = dict(method='browser_' + method, page_url=actual_url,
                          page_title=title, captured_at=reg.now(), confirmed=False)
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
    不是「我们核对过那一页」。两者在知识库里要分得出来,这就是分得出来的地方 ——
    **而且现在只有这一条路写得出 `confirmed=True`**:扩展那两条上那道「我已核对」
    撤了,它们如实记 `False`(见 `save` 里那一段)。

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
