"""Persistent subscriptions and fetched items, independent of the rebuildable search index."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

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
 -- 排在最后,和老库 ALTER 补出来的列序一致(见 _ADDED_COLUMNS)。
 history_floor TEXT NOT NULL DEFAULT '',
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

# 译文列是后加的(见 kb/feeds/translate.py)。老库里已有几千条英文条目,建表语句
# 的 IF NOT EXISTS 对它们不生效,只能逐列 ALTER 补——补不上就不是"少个字段",
# 而是整个翻译流程在老库上永远报 no such column。
_ADDED_COLUMNS = {
    # 这一条是新订阅的历史下界(见 `history_floor`)。老库里的订阅补成空串 =
    # 不设下界:它们的存档早就抓完了,回头给它们钉一个下界只会在源站补发旧文
    # 时静默丢内容,挡不住任何已经发生的事。
    'subscriptions': (
        ('history_floor', "TEXT NOT NULL DEFAULT ''"),
    ),
    'feed_items': (
        ('lang', "TEXT NOT NULL DEFAULT ''"),              # '' 未判 / zh 无需翻译 / en 待翻译
        ('title_zh', "TEXT NOT NULL DEFAULT ''"),
        ('body_zh', "TEXT NOT NULL DEFAULT ''"),
        ('translated_at', "TEXT NOT NULL DEFAULT ''"),
        ('translate_error', "TEXT NOT NULL DEFAULT ''"),
        ('translate_tries', 'INTEGER NOT NULL DEFAULT 0'),
    ),
}


def _migrate(conn) -> None:
    """补上后加的列。调用方必须已经持有写锁(见 `db()`)。

    "读一遍列集合 → 逐列 ALTER"这两步分开跑的话,两个连接同时首访老库时会读到
    同一份旧列集合,后一个 ALTER 撞上 `duplicate column name`,那一个请求当场
    500(2026-09-30 审查实测:升级后的第一次并发访问,一个成功一个报错)。
    """
    for table, columns in _ADDED_COLUMNS.items():
        have = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
        for name, decl in columns:
            if name not in have:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')


# 非中文判定:中日韩字符占比低于这个数就当成"外文,要翻译"。纯英文条目是 0,
# 正常中文条目远高于它;夹杂大段代码/英文引文的中文条目也在它之上。
_CJK_SHARE = 0.12
# 夹着汉字的短文本:占比算不准(一个汉字 + 几个字母就能压到阈值以下),
# 这种一律不翻,留原样。
_SHORT_MIXED = 12


def text_language(*parts: str) -> str:
    """'zh' = 不用翻译;'en' = 外文(泛指非中文),要走翻译。

    只看字符构成,不调模型:翻译本身才花钱,判定不能也花钱。标题和正文合起来
    判一次——短标题单看常常判不准(全是产品名的标题一个汉字都没有)。

    **一个汉字都没有、又有字母,一律判外文,不设长度下限。** 这里原来有过一道
    "至少 6 个字母"的成本闸门,用意是别花钱去译「Bun 1.3」这种没什么可译的
    产品名;代价是「Hello」「Great」「Nice」这类 1-5 个字母的纯英文动态被
    **确定性地**标成中文、永远进不了翻译队列,而 social 那类来源的短动态整段
    都是这个形态(2026-09-30 复审实测)。产品名该保留原文,靠的是 prompt 里
    "产品名、公司名、人名、代码、命令、URL、缩写保留原文"那条译文规则——
    多花几个 token 换"库里不会躺着英文",而不是把英文冒充成中文。

    真正"判不准"的只有"有汉字但太短"那一档(占比一个汉字就能压穿),照旧留
    原样;一个字母都没有的(纯数字/表情/标点)本来也没有可译的东西。
    """
    text = ' '.join(p or '' for p in parts)
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿')
    letters = sum(1 for ch in text if ch.isalpha())
    if not letters:                       # 没字可判(纯数字/表情/标点)
        return 'zh'
    if not cjk:                           # 零汉字 + 有字母 = 不可能是中文
        return 'en'
    if letters < _SHORT_MIXED:            # 有汉字又太短:判不出来就别翻
        return 'zh'
    return 'zh' if cjk / letters >= _CJK_SHARE else 'en'



def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# 新订阅第一次抓取最多带回多少天以前的内容。一个刚添加的源常常把自己的整个
# 存档一次交出来(实测 Hugging Face Blog 的 feed 一口气给了 500 条、最早到
# 2024-04-10),这些旧文随后逐批翻译、逐批重新导出,每重新导出一次 `indexed_at`
# 就刷新一次,于是连着十几天以"补录"的形态占满当天日报——而它们并不是那几天
# 发生的事(2026-10-01 那份日报 46 条素材里补录占了 30 条)。
HISTORY_DAYS = 5


def history_floor(when: datetime | None = None) -> str:
    """新订阅的历史下界(含当天),`YYYY-MM-DD`。

    按 Asia/Shanghai 算,因为被比较的 `published` 就是 `fetching.parse_feed`
    按这个时区格式化出来的——两头不同时区的话,每天有 8 小时窗口会差一整天。
    """
    at = (when or datetime.now(timezone.utc)).astimezone(ZoneInfo('Asia/Shanghai'))
    return (at.date() - timedelta(days=HISTORY_DAYS)).isoformat()


# 存量清理与订阅写入之间的互斥靠 `kb.maintenance.maintenance_lock()` —— 那把锁
# 不止管这两边,索引(管道 index 阶段与 `kb index`)也认它,原委和取舍都在
# kb/maintenance.py 的模块说明里。这里不再留第二个名字:一把锁两个叫法,下一个
# 写入方照哪一个都说不清。


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
        # 补列也在这把写锁里:它先读列集合再 ALTER,锁外跑的话两个首访连接会
        # 读到同一份旧列集合,后一个 ALTER 报 duplicate column(见 `_migrate`)。
        conn.execute('BEGIN IMMEDIATE')
        _migrate(conn)
        if not conn.execute("SELECT 1 FROM feed_meta WHERE key='defaults-v1'").fetchone():
            defaults = json.loads(Path(__file__).with_name('defaults.json').read_text())
            # 全新安装 = 这 60 个默认订阅全是新的,同样只收最近几天,否则第一次
            # 同步就把几十个源的存档一起拖进来。
            floor = history_floor()
            for s in defaults:
                conn.execute('''INSERT OR IGNORE INTO subscriptions
                    (kind,name,url,mode,group_name,config,created_at,updated_at,history_floor)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                    (s['kind'], s['name'], source_url(s['kind'],s['url']), s['mode'], s['group_name'],
                     '{}', now(), now(), floor))
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
                    # 换了链接 = 这个订阅从头开始,历史下界也跟着重算:指向一个
                    # 全新的源时,旧订阅那个两年前的下界等于没有下界。
                    c.execute("""UPDATE subscriptions SET etag='',modified='',resolved_url='',
                        last_error='',last_success='',last_attempt='',failures=0,retry_at=0,config='{}',
                        history_floor=? WHERE id=?""", (history_floor(), source_id))
                    c.execute('DELETE FROM subscription_items WHERE subscription_id=?', (source_id,))
            else:
                source_id = c.execute('''INSERT INTO subscriptions
                    (kind,name,url,feed_url,mode,group_name,enabled,created_at,updated_at,history_floor)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''', (kind,name,url,feed_url,mode,group,enabled,
                    now(),now(),history_floor())).lastrowid
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
                (key,kind,url,title,author,published,body,content_kind,fetched_at,content_hash,lang)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING''',
                (key,source['kind'],e['url'],e['title'],e.get('author') or source['name'],
                 e.get('published',''),e['body'],e.get('content_kind','feed'),now(),digest,
                 text_language(e['title'], e['body'])))
            c.execute('INSERT OR IGNORE INTO subscription_items VALUES(?,?)', (source['id'],key))
    return added


def items(kind: str, source_id: int | None = None, offset: int = 0, limit: int = 30, q: str = '') -> dict:
    args: list = [check_kind(kind)]
    where = 'kind=?'
    if source_id:
        where += ' AND key IN (SELECT item_key FROM subscription_items WHERE subscription_id=?)'
        args.append(source_id)
    if q:
        # 译文也要能搜到:面板上显示的就是译文,搜屏幕上看得见的词却搜不出来
        # 是最难解释的一种"没结果"。
        where += (' AND (title LIKE ? OR body LIKE ? OR author LIKE ?'
                  ' OR title_zh LIKE ? OR body_zh LIKE ?)')
        args.extend(['%' + q + '%'] * 5)
    # 「还在等翻译」的条目要在面板上标出来:它们被扣在导出闸门外,vault 里
    # 还没有这一份,而没有译文时展示逻辑会回退成原文——不标的话用户看到的就
    # 是一条英文内容,和"英文先译成中文再入库"直接矛盾(2026-09-30 审查实测
    # 一次进来 41 条时第 41 条整条英文出现在面板上)。
    # 口径必须和导出那道闸门(`sync.export(hold_untranslated=)`)严格一致:
    # 那边扣着不入库、这边才说"等翻译";没配 key 时那边按原文入库,这边也就
    # 照原文显示——两边各判各的话,面板上写着"等翻译"的条目其实早已以英文
    # 躺在 vault 里了。
    #
    # **"在队列里"和"没入库"是两件事,分成两态**:没配 key 那阵子英文原文是
    # 按原样进过 vault 的(`exported_hash` 有值),补上 key 之后这一条重新排进
    # 翻译队列,但说它"暂未写入知识库"和文件、索引的事实正相反
    # (2026-09-30 复审实测)。所以按 `exported_hash` 再分一次:
    #   translating    还没入库,在等译文(导出闸门确实扣着它)
    #   retranslating  原文已经入库,在等中文译文覆盖
    # 顶上那个"还有几条在等翻译"的计数只数前者——后者已经能读了。
    #
    # 还有第三档 `partial`:**不在队列里、可正文是中文而标题还是原文**。这批是
    # 老解析器留下的半成品(`translate.PARTIAL_SQL`),同步跑起来时
    # `_requeue_partial` 会把它们清回队列,但在那之前打开面板也不能说它"已译为
    # 中文"——页脚这么写而标题明明是英文,是三轮审查里同一处谎报的最后一块
    # (第 3 轮实测)。"完成"的判据前后端必须是同一个:正文有译文、而且标题
    # (只要原标题不空)也有译文。
    from kb.feeds import translate
    queued, qargs = translate.queued_sql()
    holding = translate.available()
    # 半成品那一档排在最前面,而且要求它**已经导出过**:库里此刻那篇 md 就是
    # "中文正文 + 原文标题",如实显示、可以展开,页脚写清标题还在等补译。清回
    # 队列之后它同时满足 queued,可"外文原文已入库"那句话对它就不准了(正文已经
    # 是中文)。没导出过的(极少见)照旧按队列那两档走——那时库里确实没有这一条,
    # 不能给展开。
    partial = f"({translate.PARTIAL_SQL}) AND exported_hash<>''"
    state_sql, sargs = ((f"CASE WHEN ({partial}) THEN 3 WHEN ({queued}) THEN"
                         " (CASE WHEN exported_hash='' THEN 1 ELSE 2 END) ELSE 0 END"),
                        qargs) if holding else (
                        f'CASE WHEN ({partial}) THEN 3 ELSE 0 END', [])
    with db() as c:
        total = c.execute('SELECT count(*) FROM feed_items WHERE ' + where, args).fetchone()[0]
        waiting = (c.execute(f'SELECT count(*) FROM feed_items WHERE {where}'
                             f" AND ({queued}) AND exported_hash=''",
                             [*args, *qargs]).fetchone()[0] if holding else 0)
        rows = c.execute(f'SELECT *, {state_sql} AS translate_state FROM feed_items WHERE ' + where +
                         " ORDER BY COALESCE(NULLIF(published,''),fetched_at) DESC,key DESC LIMIT ? OFFSET ?",
                         [*sargs,*args,limit,offset]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        state = d.pop('translate_state')
        d['translating'] = state == 1
        d['retranslating'] = state == 2
        d['partial'] = state == 3
        out.append(d)
    return {'items': out, 'total': total, 'offset': offset, 'translating': waiting}


def latest_run(kind: str) -> dict | None:
    with db() as c:
        row = c.execute('SELECT * FROM feed_runs WHERE kind=? ORDER BY id DESC LIMIT 1', (kind,)).fetchone()
    return {**dict(row), 'summary': json.loads(row['summary'])} if row else None
