"""Persistent subscriptions and fetched items, independent of the rebuildable search index.

这个模块被好几块的硬约束管着,改之前先读对应那一份 `docs/decisions/`:默认订阅补种
(`defaults-backfill.md`)、新订阅的历史下界(`history-depth.md`)、语言判定与翻译队列
(`translate.md`)、`uses_channel` 与通道就绪(`x-channels.md`)。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from contextlib import contextmanager, suppress
# 直接按名字拿:`sqlite3` 这个模块名在测试里会被换成一个只有 connect/Row 的替身,
# 而异常类不随连接工厂变 —— 走 `sqlite3.OperationalError` 会在那种替身下 AttributeError。
from sqlite3 import OperationalError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from kb.config import DATA_DIR
from kb.feeds.exclusion import SCHEMA as EXCLUSION_SCHEMA, item_key
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
 history_floor TEXT NOT NULL DEFAULT '', last_note TEXT NOT NULL DEFAULT '',
 fulltext INTEGER NOT NULL DEFAULT 0, extract INTEGER NOT NULL DEFAULT 0,
 last_notice TEXT NOT NULL DEFAULT '',
 UNIQUE(kind, url)
);
CREATE TABLE IF NOT EXISTS feed_items (
 key TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
 author TEXT NOT NULL, published TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
 content_kind TEXT NOT NULL DEFAULT 'feed', fetched_at TEXT NOT NULL,
 content_hash TEXT NOT NULL, exported_hash TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
 -- 排在最后,和老库 ALTER 补出来的列序一致(见 _ADDED_COLUMNS)。
 extract_stale INTEGER NOT NULL DEFAULT 0, fetch_round TEXT NOT NULL DEFAULT '',
 content_rev INTEGER NOT NULL DEFAULT 0, body_provenance TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS feed_items_recent ON feed_items(kind, published DESC, fetched_at DESC);
CREATE TABLE IF NOT EXISTS subscription_items (
 subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 item_key TEXT NOT NULL REFERENCES feed_items(key), PRIMARY KEY(subscription_id, item_key)
);
-- 主键是 (subscription_id, item_key),**按 item_key 查用不上它**。而正文补抓的
-- 队列判据正是"这个条目属不属于某条开了补抓的订阅"(见 `fulltext.queued_sql`),
-- 它对每一行各查一次:没有这条索引就是逐行全表扫 subscription_items,而那张表
-- 和条目数同量级 —— 面板翻一页、导出选一次待导出集合都要付这笔钱。
CREATE INDEX IF NOT EXISTS subscription_items_item ON subscription_items(item_key);
CREATE TABLE IF NOT EXISTS feed_settings (kind TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feed_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT NOT NULL, summary TEXT NOT NULL
);
''' + EXCLUSION_SCHEMA

# 译文列是后加的(见 kb/feeds/translate.py)。老库里已有几千条英文条目,建表语句
# 的 IF NOT EXISTS 对它们不生效,只能逐列 ALTER 补——补不上就不是"少个字段",
# 而是整个翻译流程在老库上永远报 no such column。
_ADDED_COLUMNS = {
    # 这一条是新订阅的历史下界(见 `history_floor`)。老库里的订阅补成空串 =
    # 不设下界:它们的存档早就抓完了,回头给它们钉一个下界只会在源站补发旧文
    # 时静默丢内容,挡不住任何已经发生的事。
    'subscriptions': (
        ('history_floor', "TEXT NOT NULL DEFAULT ''"),
        # 上一次同步**成功但没读完**的原因(登录态通道滚不到上次的位置)。
        # 它不是 `last_error`:抓到的条目照旧入库,只是这一轮没读到水位 ——
        # 记成失败会让面板显示"连接失败"并触发退避重试,而连接好得很。
        # 每次成功同步都重写它(读完了就写空串),所以不会留下过期的提示。
        ('last_note', "TEXT NOT NULL DEFAULT ''"),
        # 「去原文链接把正文抓回来」(见 kb/feeds/fulltext.py)。**独立一列、
        # 不放 `config`**:换链接时 `save_subscription` 的 reset 会把 config
        # 整个清成 '{}' —— 那是采集进度(水位、etag)该有的下场,而这是用户对
        # 这条订阅的配置,不该因为改一次链接就被静默关掉。
        ('fulltext', 'INTEGER NOT NULL DEFAULT 0'),
        # 「这条订阅的内容要不要买一次大模型提取(实体/主题/摘要)」。
        # **默认 0,老库里的订阅一律补 0** —— 这是一次有意的口径收紧:聚合类
        # 来源的长文章每篇能抽出一堆只出现过一次的细碎实体,实测 rss 那 778 篇
        # 贡献了 1024 种全库独有的标签,把标签检索和主题页稀释成噪声。谁值得
        # 提取改由用户逐条拍板(见 `kb.extract.pending_docs` 的判据)。
        # 存量已经提取过的不回滚:钱已经付过,标签也已经在图谱和检索里。
        # **独立一列、不放 `config`**,理由同 `fulltext`。
        ('extract', 'INTEGER NOT NULL DEFAULT 0'),
        ('last_notice', "TEXT NOT NULL DEFAULT ''"),
    ),
    'feed_items': (
        ('lang', "TEXT NOT NULL DEFAULT ''"),              # '' 未判 / zh 无需翻译 / en 待翻译
        ('title_zh', "TEXT NOT NULL DEFAULT ''"),
        ('body_zh', "TEXT NOT NULL DEFAULT ''"),
        ('translated_at', "TEXT NOT NULL DEFAULT ''"),
        ('translate_error', "TEXT NOT NULL DEFAULT ''"),
        ('translate_tries', 'INTEGER NOT NULL DEFAULT 0'),
        # 「用户在面板上点过重试」。老库里的条目补成 0 = 没人点过,取数顺序
        # 逐字不变。它存在的理由只有一个:让手动放回队列的条目排在**队首**
        # —— 额度清零会把它们变成「从没试过」,而取数是「失败过的优先」
        # (见 `translate.requeue_failed`)。`_save` 译成之后清掉。
        ('translate_manual', 'INTEGER NOT NULL DEFAULT 0'),
        # 正文补抓的三列。老库里的条目补成空串 = 还没抓过:订阅开了补抓开关
        # 之后它们会自动排进队列,一轮 MAX_ITEMS 条慢慢补齐 —— 那正是打开这个
        # 开关的人想要的(存量那几百条壳才是他点开面板时看见的东西)。
        ('fulltext_at', "TEXT NOT NULL DEFAULT ''"),
        ('fulltext_error', "TEXT NOT NULL DEFAULT ''"),
        ('fulltext_tries', 'INTEGER NOT NULL DEFAULT 0'),
        # 「这一条的正文被整篇换过,知识库里那份旧提取结果作废」。老库里的条目
        # 补成 0 = 没有待作废的东西,行为逐字不变。
        # 它是一个**待办**而不是历史:由 `fulltext._save` 置 1,等新正文真的落进
        # vault 之后由 `fulltext.retire_stale` 做完清理再放下 —— 所以这一步失败
        # 时标记留在原处,下一轮导出会再来一次。
        ('extract_stale', 'INTEGER NOT NULL DEFAULT 0'),
        # 「**哪一次业务抓取**头一回发现这一条」(`fetch_rounds.round_key()`)。
        # 导出那一步把它当成回执里的抓取身份交上去,tooltip 背后那句分解据此分开
        # 「这一次新抓到的」和「之前抓到、这一次才补导进来的」。
        # **老库里的条目补成空串 = 说不清**,不是「不是本次」:它们入库时压根
        # 没有这个概念,硬塞一个值等于凭空造事实。空串落在 `unsure` 那一档,
        # 面板上那句分解自己会闭嘴(见 `fetch_rounds.today`)。
        ('fetch_round', "TEXT NOT NULL DEFAULT ''"),
        # 「这一行的内容换到第几版了」——**只增、不复用**的代次,换内容的两个写入方
        # 自己加一(`grab_store.replace`、`fulltext._save`)。它答的是「异步工作还有
        # 没有交付权」:为什么内容指纹答不了、两道判据各挡什么,见
        # `fulltext.work_version`。老库补成 0 = 都算第 0 版,行为逐字不变 —— 判据比
        # 的是**取样那一刻的值**,起点是几无关。
        ('content_rev', 'INTEGER NOT NULL DEFAULT 0'),
        ('body_provenance', "TEXT NOT NULL DEFAULT ''"),
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


# 「这段文字要不要翻译」搬去了 `kb/feeds/lang.py`(700 行上限)。**公开名字仍然
# 从这里取** —— `reg.text_language` 有五个模块在用、两组回归在打桩,而 `put_items`
# 自己也走这个模块属性,所以这一行既是出口也是打桩点。
from kb.feeds.lang import text_language  # noqa: E402,F401


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


# 默认订阅的修订号。**新增一条默认订阅时 +1,并给那一条标上 `since`。**
# 原来只有 `defaults-v1` 一个"种过没有"的标记,于是往 defaults.json 里加来源
# 这件事对**所有存量库永久不生效** —— 升级重启之后面板上那一类来源是 0 个
# 订阅、「同步全部」还点不了,而新装的机器上它好好的(2026-10-10 审查实测)。
DEFAULTS_REV = 2


def _seed_defaults(conn) -> None:
    """按修订号补种默认订阅。**可重入**:号推到当前值之后这段再也不跑。

    删掉的默认订阅不会在重启后回来 —— 判据是"这个库种到第几号了",不是
    "这条订阅还在不在"。用户停用、改名、删除都是他的选择,补种只认号。
    """
    rows = dict(conn.execute(
        "SELECT key,value FROM feed_meta WHERE key IN ('defaults-v1','defaults-rev')"))
    seeded = 'defaults-v1' in rows
    # 老库只有 `defaults-v1`、没有号:它种的是第 1 版。全新库两个都没有,从 0 起
    # 种 —— 于是"全新安装"和"升级补种"是同一段代码的两个起点。
    rev = int(rows.get('defaults-rev') or (1 if seeded else 0))
    if rev >= DEFAULTS_REV:
        return
    defaults = json.loads(Path(__file__).with_name('defaults.json').read_text())
    # 补种的这几条对**这个库**也是新的:照样只收最近几天,否则第一次同步就把
    # 这个源的整个存档拖进来(口径同全新安装)。
    floor = history_floor()
    for s in defaults:
        if int(s.get('since', 1)) <= rev:
            continue
        # `fulltext` 只有链接聚合源标了(默认不标,见 kb/feeds/fulltext.py)。
        # `INSERT OR IGNORE`:用户自己手工加过同一个地址时不重复插。
        conn.execute('''INSERT OR IGNORE INTO subscriptions
            (kind,name,url,mode,group_name,config,created_at,updated_at,history_floor,fulltext,extract)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (s['kind'], s['name'], source_url(s['kind'],s['url']), s['mode'], s['group_name'],
             '{}', now(), now(), floor, int(bool(s.get('fulltext'))),
             int(bool(s.get('extract')))))
    if not seeded:
        conn.execute("INSERT INTO feed_meta VALUES('defaults-v1',?)", (now(),))
    conn.execute("INSERT OR REPLACE INTO feed_meta VALUES('defaults-rev',?)", (str(DEFAULTS_REV),))


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    # 切 journal 模式要的是**排他锁**,而 SQLite 对这条语句**不走 busy handler**
    # (`timeout=30` 管不着它):两个连接同时首访一个还是 rollback-journal 的库时,
    # 输的那个当场 `database is locked` —— 而它想做的事已经由赢的那个做掉了,
    # WAL 是写在库头里的持久状态。所以这里丢掉 BUSY 继续往下走:真写不进去,
    # 两行之后的 BEGIN IMMEDIATE 会报一个说得清的错。
    with suppress(OperationalError):
        conn.execute('PRAGMA journal_mode=WAL')
    try:
        conn.executescript(SCHEMA)
        # One transaction: deleting a default never brings it back on restart.
        # 补列也在这把写锁里:它先读列集合再 ALTER,锁外跑的话两个首访连接会
        # 读到同一份旧列集合,后一个 ALTER 报 duplicate column(见 `_migrate`)。
        conn.execute('BEGIN IMMEDIATE')
        _migrate(conn)
        _seed_defaults(conn)
        conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 设置的修订号。**存量设置里的旧默认值靠它迁移**:改 `SourceType.interval_minutes`
# 只影响"压根没有设置行"的新库 —— 已经保存过的那份 JSON 在 `get_settings` 里盖在
# 默认值上面,面板保存通道时又把旧间隔原样带回去,于是"social 改成每天一次"对所有
# 老用户都没有发生(2026-10-08 审查实测:存量 `interval_minutes=60` 的设置行,真跑
# 一次 `schedule.tick` 仍然安排一小时后再来)。
_SETTINGS_REV = 1
# rev → {kind: {键: (旧默认值, 新默认值)}}。**只改仍然等于旧默认值的那些**:
# 用户自己挑过的间隔不是我们该动的东西,而"他挑的恰好等于旧默认值"这一档没有
# 办法和"从没碰过"分开 —— 那就按从没碰过办,代价是他下次打开面板改回去一次,
# 而反过来(谁都不迁)的代价是这条需求对存量库永久不生效。
_SETTINGS_FIXUPS = {
    1: {'social': {'interval_minutes': (60, 1440)}},
}


def _upgrade_settings(kind: str, settings: dict) -> dict:
    """把存量设置里没人碰过的旧默认值迁到当前默认值。

    **读的时候归一,写的时候盖章**(`save_settings` 把 `settings_rev` 一起存下去,
    所以升级之后用户自己挑的值不会被再迁一次)。

    反过来写 —— 首次访问这个库时就落一条"已迁移"的标记 —— 会在标记落下之后、
    设置行写进来之前留一个窗口,那时这份设置永远不会被迁移;而这正是升级的常态:
    老库里设置行早就有了,标记却是新代码第一次碰库才写的。读侧归一和顺序无关。
    """
    for rev in range(int(settings.get('settings_rev') or 0) + 1, _SETTINGS_REV + 1):
        for key, (was, now_) in _SETTINGS_FIXUPS.get(rev, {}).get(kind, {}).items():
            if settings.get(key) == was:
                settings[key] = now_
    settings['settings_rev'] = _SETTINGS_REV
    return settings


def get_settings(kind: str) -> dict:
    check_kind(kind)
    with db() as c:
        row = c.execute('SELECT value FROM feed_settings WHERE kind=?', (kind,)).fetchone()
    # `enabled` / `interval_minutes` / `next_run` 是**按来源类型自动同步**那一档
    # 留下的,那一档整条拆掉了(见 `kb.pipeline._scheduler_tick`),现在没有任何
    # 地方读它们。**键一个都不删**(元规则「只增不减」):盘上存量里就有,摘掉
    # 之后另一条分支或者还没重启的进程读到的就是一份缺字段的设置。
    default = {'enabled': False, 'interval_minutes': feed_types()[kind].interval_minutes,
               'next_run': 0}
    if feed_types()[kind].adapter == 'social':
        # X 的取法是个设置 —— 存量库里这一段压根没有这个键,
        # 读出来必须和升级前逐字同义。
        from kb.feeds import x_channels
        default |= {'x_channel': x_channels.DEFAULT, 'x_feedgrab_bin': '',
                    'x_chrome_path': ''}
    settings = {**default, **(json.loads(row['value']) if row else {})}
    if 'x_channel' in settings:
        from kb.feeds import x_channels
        # 归一留在这一层:退役名翻成现用名,两条可执行文件位置去掉首尾空白。
        # **各处读设置的地方都从这里拿**,所以口径只有一份(`x_channels.config_of`)。
        # 放在面板那个 HTTP 入口里不够 —— 采集(`sync.collect` → `x_channels.prefetch`)
        # 读的是同一份设置的**原值**,于是「面板比的、面板跑的」和「同步真正跑的」
        # 会是两个文件:审查实测,存着末尾带一个空格的那份、面板发来不带空格的,
        # 面板这边判"对得上"、检查也说就绪,而同步执行的是另一个可执行文件。
        settings |= x_channels.config_of(settings)
    return _upgrade_settings(kind, settings)


def save_settings(kind: str, values: dict) -> dict:
    cur = get_settings(kind)
    cur.update(values)
    if not isinstance(cur['interval_minutes'], int) or not 15 <= cur['interval_minutes'] <= 10080:
        raise ValueError('同步间隔须在 15 分钟至 7 天之间')
    if feed_types()[kind].adapter == 'social':
        from kb.feeds import x_channels
        x_channels.validate(cur)
    with db() as c:
        c.execute('INSERT OR REPLACE INTO feed_settings VALUES(?,?)', (kind, json.dumps(cur)))
    return cur


def uses_channel(source: dict) -> bool:
    """这条订阅的内容是不是要走「X 通道」拿(而不是它自己那条 feed 地址)。

    判据:social 来源 + 没填专用订阅链接 + 是个 X 账号。
    (原名 `uses_bridge` —— 桥接那一档删掉之后名字就只剩误导了。)
    """
    return (feed_types()[source['kind']].adapter == 'social' and not source['feed_url']
            and bool(x_handle(source['url'])))


def configuration_error(source: dict, settings: dict) -> str:
    """这条订阅现在能不能开工。空串 = 能。

    判据按当前通道走:feedgrab 要有可执行文件和登录态,独立 profile 要在那里
    登录过,直连那档要有一个开着调试开关的浏览器。判断本身一律交给
    `x_channels.readiness`,这里不自己再写一份 —— 新增一条通道时这个函数一个
    字都不用改。**问的必须是当前通道**:按别的档判的话,用户会看到一条跟他这
    一轮采集毫无关系的「待配置」。
    """
    if not uses_channel(source):
        return ''
    from kb.feeds import x_channels
    ready, why, _hint = x_channels.readiness(settings)
    return '' if ready else f'待配置：{why}'


def in_scope(source: dict, targets) -> bool:
    """这条订阅在不在这一次的范围里。空范围 = 全都在。

    **这是范围匹配的唯一一份口径**,`sync.collect`(真去抓哪几条)和
    `FeedCollector.status(targets=...)`(这几条现在抓不抓得动)都过它 —— 两边
    岔开的后果是**用一批订阅的就绪去替另一批放行**:名字匹配那一半认不出来的话,
    「重试这一条」会拿整条通道的就绪当答案,而那一条必定再失败一次。

    id 和 name 两种都认:名单由各处自己产出(`still_failing` 给的是 id 的字符串,
    面板上点的是名字),而"这条订阅"在库里的稳定身份是 id、在档案里是 name。
    """
    return (not targets or str(source['id']) in targets
            or source['name'] in targets)


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
    fulltext = bool(data.get('fulltext', old.get('fulltext', False)))
    # 「这条订阅要不要买提取」。默认关,理由见 `_ADDED_COLUMNS` 里那一段。
    extract = bool(data.get('extract', old.get('extract', False)))
    reset = any(old.get(k) != v for k, v in [('url', url), ('feed_url', feed_url), ('mode', mode)])
    try:
        with db() as c:
            if source_id:
                c.execute('''UPDATE subscriptions SET name=?,url=?,feed_url=?,mode=?,group_name=?,
                    enabled=?,fulltext=?,extract=?,updated_at=? WHERE id=?''',
                    (name,url,feed_url,mode,group,enabled,int(fulltext),int(extract),
                     now(),source_id))
                if reset:
                    # 换了链接 = 这个订阅从头开始,历史下界也跟着重算:指向一个
                    # 全新的源时,旧订阅那个两年前的下界等于没有下界。
                    # **上一轮那条「没读完」的提示也要一起清掉**(`last_note`):
                    # 它说的是旧账号读到哪儿了,留着就是在新账号那一行显示
                    # 「部分同步」——前端的状态链在 `last_success` 之前先看它,
                    # 而 `last_success` 这里已经清了(2026-10-08 审查实测)。
                    # `fulltext` 和 `extract` 都不在这一排里:它们是用户对这条
                    # 订阅的配置,换个链接不该把它们静默改掉(上面那句 UPDATE
                    # 已经按入参存过了)。
                    # 被清掉的都是采集进度 —— 指向新的源之后它们本来就不作数。
                    c.execute("""UPDATE subscriptions SET etag='',modified='',resolved_url='',
                        last_error='',last_success='',last_attempt='',failures=0,retry_at=0,config='{}',
                        last_note='',last_notice='',history_floor=? WHERE id=?""", (history_floor(), source_id))
                    c.execute('DELETE FROM subscription_items WHERE subscription_id=?', (source_id,))
            else:
                source_id = c.execute('''INSERT INTO subscriptions
                    (kind,name,url,feed_url,mode,group_name,enabled,fulltext,extract,
                     created_at,updated_at,history_floor)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (kind,name,url,feed_url,mode,group,enabled,
                    int(fulltext),int(extract),now(),now(),history_floor())).lastrowid
    except sqlite3.IntegrityError:
        raise ValueError('这个链接已经在订阅列表中') from None
    return subscription(kind, source_id)


def delete_subscription(kind: str, source_id: int):
    subscription(kind, source_id)
    with db() as c:
        c.execute('DELETE FROM subscriptions WHERE id=?', (source_id,))


@contextmanager
def _writing(conn=None):
    """复用调用方的事务,没给就自己开一个。

    存盘那条路要把"条目 + 水位 + 成功时间戳"放进同一个事务 —— 各自开一个的话
    任意一步失败都会留下半个结果,而最坏的那半正是"水位推进了、条目没进去"
    (见 `x_channels.watermark_patch`)。
    """
    if conn is not None:
        yield conn
        return
    with db() as c:
        yield c


def merge_config(source_id: int, patch: dict, conn=None) -> None:
    """把几个键并进 `subscriptions.config`,别的键原样留着。

    整份覆盖会把另一条通道的水位、以及将来别人加的字段一起抹掉;所以是读-改-写,
    而且要和调用方在同一个事务里(`conn`)——否则读到的 config 可能已经过时。
    """
    if not patch:
        return
    with _writing(conn) as c:
        row = c.execute('SELECT config FROM subscriptions WHERE id=?', (source_id,)).fetchone()
        if not row:
            return
        try:
            cfg = json.loads(row['config'] or '{}')
            cfg = cfg if isinstance(cfg, dict) else {}
        except (TypeError, ValueError):
            cfg = {}
        cfg.update(patch)
        c.execute('UPDATE subscriptions SET config=? WHERE id=?',
                  (json.dumps(cfg, ensure_ascii=False), source_id))


def put_items(source: dict, entries: list[dict], conn=None) -> int:
    # 这一条是哪一次业务抓取发现的。**口径只有一份**(`fetch_rounds.round_key`),
    # 别在这里另写一遍读 `_state` 的逻辑 —— 两份迟早漂,而漂的后果是 tooltip 把本次
    # 新抓到的算成补导的。管道没在跑时它是空串 = 说不清,照旧入库。
    from kb.fetch_rounds import round_key

    round_id = round_key()
    added = 0
    with _writing(conn) as c:
        for e in entries:
            key = item_key(source['kind'], e['url'])
            digest = hashlib.sha256(json.dumps(e, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            # First provenance remains stable when an aggregate also contains this URL.
            # `DO NOTHING` 顺手把 fetch_round 钉成**首次发现**那一轮:聚合源里
            # 同一个 URL 后面还会再出现,重抓也按 URL 去重,改写它等于把首次发现
            # 的事实改成最后一次看见的时刻。
            inserted = c.execute('''INSERT INTO feed_items
                (key,kind,url,title,author,published,body,content_kind,fetched_at,content_hash,lang,
                 fetch_round,body_provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO NOTHING''',
                (key,source['kind'],e['url'],e['title'],e.get('author') or source['name'],
                 e.get('published',''),e['body'],e.get('content_kind','feed'),now(),digest,
                 text_language(e['title'], e['body']),round_id,e.get('body_provenance', '')))
            added += inserted.rowcount
            c.execute('INSERT OR IGNORE INTO subscription_items SELECT ?,? '
                      'WHERE EXISTS (SELECT 1 FROM feed_items WHERE key=?)', (source['id'],key,key))
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
    from kb.feeds import fulltext, translate
    queued, qargs = translate.queued_sql()
    holding = translate.available()
    # **正文还没补抓回来**(见 `kb.feeds.fulltext`)。它排在翻译之前,所以这一档
    # 的条目手里那份正文是 feed 给的跳转壳 —— 面板照它显示就是一行
    # "标题(域名)↑ 101 HN Points",而知识库里马上要落的是整篇正文。不标的话
    # 用户看到的是一条内容空空如也的条目,和它一分钟后的样子完全不同,却没有
    # 任何东西解释这中间发生了什么。
    #
    # **「在队列里」和「没入库」是两件事,按 `exported_hash` 分两态** —— 口径
    # 同下面翻译那两档,理由也一模一样。打开这个开关最常见的时机正是"已经同步
    # 过、面板上几百条壳就摆在眼前",那批壳**早就在 vault 里了**(重新导出会
    # 覆盖它们):对它们说"抓到之后才写进知识库"和文件、索引的事实正相反,而且
    # 那句文案配着"不给展开阅读",等于把一批本来读得到的条目一起藏起来。
    #   4 `fetching`    —— `exported_hash=''`,导出闸门确实扣着它(新条目)
    #   5 `refetching`  —— 已经导出过,库里此刻就是那份壳:如实显示、可以展开
    fetch_cond, fargs = fulltext.queued_sql()
    # 半成品那一档排在最前面,而且要求它**已经导出过**:库里此刻那篇 md 就是
    # "中文正文 + 原文标题",如实显示、可以展开,页脚写清标题还在等补译。清回
    # 队列之后它同时满足 queued,可"外文原文已入库"那句话对它就不准了(正文已经
    # 是中文)。没导出过的(极少见)照旧按队列那两档走——那时库里确实没有这一条,
    # 不能给展开。
    partial = f"({translate.PARTIAL_SQL}) AND exported_hash<>''"
    # 补抓那一档排在最前面:它在流水线上也排在翻译前面,而且 `translate.queued_sql`
    # 已经把这批条目排除在翻译队列之外 —— 两处顺序一致,同一条内容不会既说
    # "等翻译"又说"等正文"。
    tail = ((f"WHEN ({partial}) THEN 3 WHEN ({queued}) THEN"
             " (CASE WHEN exported_hash='' THEN 1 ELSE 2 END)"), qargs) if holding else (
            f'WHEN ({partial}) THEN 3', [])
    state_sql = (f"CASE WHEN ({fetch_cond}) THEN"
                 " (CASE WHEN exported_hash='' THEN 4 ELSE 5 END) "
                 f'{tail[0]} ELSE 0 END')
    sargs = [*fargs, *tail[1]]
    with db() as c:
        total = c.execute('SELECT count(*) FROM feed_items WHERE ' + where, args).fetchone()[0]
        waiting = (c.execute(f'SELECT count(*) FROM feed_items WHERE {where}'
                             f" AND ({queued}) AND exported_hash=''",
                             [*args, *qargs]).fetchone()[0] if holding else 0)
        # 顶上那句计数只数"还没入库"那一档(同 `waiting`):另一档已经读得到了,
        # 把它算进"正在补抓、抓到才写进知识库"就是又说了一遍那句不准的话。
        fetching = c.execute(f'SELECT count(*) FROM feed_items WHERE {where}'
                             f" AND ({fetch_cond}) AND exported_hash=''",
                             [*args, *fargs]).fetchone()[0]
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
        d['fetching'] = state == 4
        d['refetching'] = state == 5
        out.append(d)
    # 「译不动的有几条」**不跟搜索词走,只跟选中的订阅走**(见
    # `translate._retry_where`):它是面板上那颗重试按钮的射程说明,而按钮的
    # 射程必须可预测 —— 跟着搜索框里打了什么变的话,用户看着「3 条」点下去,
    # 放回队列的是这条订阅的全部二十几条。上面那个 `waiting` 跟着 `where`
    # (含 q)走是对的:它说的是「这一屏列出来的内容里有几条扣着没入库」。
    return {'items': out, 'total': total, 'offset': offset, 'translating': waiting,
            'fetching': fetching, 'translate_ready': holding,
            'stuck': translate.stuck(kind, source_id)}


def latest_run(kind: str) -> dict | None:
    with db() as c:
        row = c.execute('SELECT * FROM feed_runs WHERE kind=? ORDER BY id DESC LIMIT 1', (kind,)).fetchone()
    return {**dict(row), 'summary': json.loads(row['summary'])} if row else None
