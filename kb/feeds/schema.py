# -*- coding: utf-8 -*-
"""`feeds.db` 的库结构:建表语句、后加的那些列、以及老库的补列 —— **这三件事
只有这一份**。

从 `kb/feeds/registry.py` 搬出来**只因为 700 行上限**(口径同
`kb/feeds/targets.py`、`kb/feeds/lang.py`)。分界线是「问的是哪个问题」:这里只
回答「库长什么样、老库怎么补齐」,取数、判据、默认订阅补种留在 `registry.py`。
搬出来的是**整组**:建表语句里那几句「排在最后,和老库 ALTER 补出来的列序一致」
指的就是同一份文件里的 `_ADDED_COLUMNS` —— 新加一列时两边都要动,摆在一处才看得
见漏了哪一边,而漏了的代价是老库上永远少一列(静默丢数据、不可恢复)。

**公开名字仍然从 `registry` 取**:`reg.SCHEMA` 有几组回归在建老库时用、
`reg._migrate` 还有一组在打桩,所以那边留一行原地再导出 —— registry 自己的 `db()`
也是走模块属性拿 `_migrate` 的,打桩照旧穿得过去,实现只有一份。

这里不碰连接、不开事务:`_migrate` 要求调用方已经持有写锁,那把锁和它的锁序在
`registry.db()` 里(理由写在那几行上面)。排除名单那张表的 DDL 也不在这里,它是
`kb/feeds/exclusion.py` 自己的 `SCHEMA`,这边只按 `EXCLUSION_SCHEMA` 的名字拼在
末尾;搁置和处理记录那两张同理(`kb/intake_diag/`)。

**主干的迁移只增不减**(CLAUDE.md 元规则):表和列一视同仁,要停用一列就停止
读写它,别 DROP —— 判据是「另一条分支**不重启**会不会当场坏」。
"""
from __future__ import annotations

from kb.feeds.exclusion import SCHEMA as EXCLUSION_SCHEMA
from kb.intake_diag.ledger import SCHEMA as CARE_LOG_SCHEMA
from kb.intake_diag.shelf import SCHEMA as SHELF_SCHEMA

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
 -- 排在最后,和老库 ALTER 补出来的列序一致(见 `_ADDED_COLUMNS`)。
 history_floor TEXT NOT NULL DEFAULT '', last_note TEXT NOT NULL DEFAULT '',
 fulltext INTEGER NOT NULL DEFAULT 0, extract INTEGER NOT NULL DEFAULT 0,
 last_notice TEXT NOT NULL DEFAULT '', rev INTEGER NOT NULL DEFAULT 0,
 UNIQUE(kind, url)
);
CREATE TABLE IF NOT EXISTS feed_items (
 key TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
 author TEXT NOT NULL, published TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
 content_kind TEXT NOT NULL DEFAULT 'feed', fetched_at TEXT NOT NULL,
 content_hash TEXT NOT NULL, exported_hash TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
 -- 排在最后,和老库 ALTER 补出来的列序一致(见 `_ADDED_COLUMNS`)。
 extract_stale INTEGER NOT NULL DEFAULT 0, fetch_round TEXT NOT NULL DEFAULT '',
 content_rev INTEGER NOT NULL DEFAULT 0, x_post_meta TEXT NOT NULL DEFAULT '',
 body_provenance TEXT NOT NULL DEFAULT '', life TEXT NOT NULL DEFAULT '',
 fulltext_gen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS feed_items_recent ON feed_items(kind, published DESC, fetched_at DESC);
CREATE TABLE IF NOT EXISTS browser_capture_audit (
 id TEXT PRIMARY KEY,item_key TEXT NOT NULL,provenance TEXT NOT NULL,
 body_digest TEXT NOT NULL,content_rev INTEGER NOT NULL,
 -- 排在最后,和老库 ALTER 补出来的列序一致(见 `_ADDED_COLUMNS`)。
 item_fetched_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS browser_capture_item ON browser_capture_audit(item_key);
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
''' + EXCLUSION_SCHEMA + SHELF_SCHEMA + CARE_LOG_SCHEMA

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
        # **「这条订阅的定义改到第几版了」—— 只增、不复用的配置代次。**
        # `save_subscription` 每次保存加一(改名、改链接、停用、换专用链接都算),
        # 存量库补成 0 = 都算第 0 版,行为逐字不变。
        # 它答的是「我确认的那一下,这条订阅还是现在这一条吗」:一张只绑订阅编号
        # 的执行凭证认不出改指 —— 人在确认框停留期间把这条订阅改到另一个账号,
        # 编号没变、问题编号也没变,点下去跑的是**另一个来源**(审查第 2 轮 R3
        # 实测)。**不拿 `url`/`updated_at` 充当这件事**:前者 A→B→A 之后完全
        # 复原,后者只记到秒 —— 口径同 `life` 那一段「本地可重复的状态不是身份」。
        ('rev', 'INTEGER NOT NULL DEFAULT 0'),
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
        # 「**上一次**补抓的结论是『证据不足』,而且那半句话是这个」。老库里的条目
        # 补成空串 = 没有这种待办,行为逐字不变。
        # 它和 `fulltext_error` 分开是因为两列答的不是同一问:那一列记「上一次为什么
        # 没成」(超时、HTTP 500、对方拦着,下一轮自己会重试),这一列记「上一次我们
        # **没能判断**这一页是什么」—— 后者试满额度之后要人看一眼而不是静默退回摘要
        # (`fulltext.held` / `fulltext.release`)。合成一列就得去匹配话术,那是第二
        # 份口径。`_save` 成功时清掉,`_fail` 每次都按最后一次的结论重写。
        ('fulltext_hold', "TEXT NOT NULL DEFAULT ''"),
        # 「这一条的正文去**哪个地址**抓」。空串 = 就去 `url`(身份那一列)。
        # 老库里的条目补成空串 = 行为逐字不变。
        # 它存在的理由:feed 给的链接常常指向摘要页、跳转页或已经失效的地址,而
        # 正文在另一个地址上 —— 人在「问题与待办」里换一个链接(`kb.intake_diag`)。
        # **身份那一列不许跟着换**:`item_key` 是按原网址算的,换掉 `url` 会让
        # `exclusion.failure()` 的 `item_key(channel,url)==key` 不再成立,失败明细里
        # 「不再抓取」那颗按钮静默消失;而把 key 一起重算就是换一条内容了。
        ('fulltext_url', "TEXT NOT NULL DEFAULT ''"),
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
        ('x_post_meta', "TEXT NOT NULL DEFAULT ''"),
        ('body_provenance', "TEXT NOT NULL DEFAULT ''"),
        # **「这一行是哪一世」—— 一个不可复用的生命周期标识。**
        # `item_key` 按网址算:存量清理删掉一条、下一轮又从同一个链接收回来,key
        # 一个字都没变,而库里那是另一行、另一份正文、另一批待办。此前认这件事靠
        # 「`fetched_at` + `content_rev`」两道组合,而那两列**都是可以重复的本地
        # 状态**:`fetched_at` 只记到秒,`content_rev` 新插进来的从 0 起 —— 同一秒
        # 里删掉又收回来、而上一世的代次也是 0 时,两道一起判反。后果是上一世的
        # 「不再收录」静默挡住一条全新的内容,而面板上什么都看不见
        # (元规则:漏报是静默丢数据)。**本地可重复的状态不是身份**,所以这里给
        # 每一次 INSERT 发一个 16 字节随机值,由 `migrate` 建的那个 AFTER INSERT
        # 触发器统一种 —— 种在触发器里而不是各个写入方那边,是因为写入方有五处
        # (`put_items`、`grab_store`、手动收录、YouTube、采集器),漏一处就退回到
        # 「认不出来」。老库补成空串,随后在同一个事务里一次性回填。
        ('life', "TEXT NOT NULL DEFAULT ''"),
        # **「这一条的补抓工作换到第几代了」—— 只增、不复用的抓取代次。**
        # 换取正文的地址(`actions.relink`)、人工放回队列(`fulltext.release` /
        # `reopen`)各加一。老库补成 0 = 都算第 0 代,行为逐字不变。
        # 它和 `content_rev` 分开,因为换地址、还额度**都不换内容** —— 别人手上
        # 那份**内容**工作资格不该因此作废(`work_version` 那段说明里写着)。
        # 它和 `fulltext_url` 也不是一件事:地址是**可以改回去**的工作输入,
        # A→B→A 之后那个元组逐字复原,旧请求重新获得写回权(审查第 2 轮 R2
        # 实测:改回 A 之后,改地址之前发出去的那次 `_save` 又写进去了)。
        # **可复原的输入值不是一次工作的身份**,所以另给这一列只增的代次。
        ('fulltext_gen', 'INTEGER NOT NULL DEFAULT 0'),
        # **这一条最近一次真交付进知识库是什么时候。** 有了它,「已完成」那一组才
        # 盖得住**纯入库**那条路:一条自带中文全文、不需要补抓也不需要翻译的内容,
        # 它的 `fulltext_at` / `translated_at` 永远是空串 —— 人在面板上对它点过
        # 「立即执行」、它也真的写进了知识库,而那两档都认不出它,于是他看不到结果。
        # 写它的只有交付确认那一句(`sync._export` 里那条带判据的 UPDATE,和
        # `exported_hash`/`path` 同一笔),所以它答的是「知识库里这一份是什么时候
        # 写下的」。老库补成空串 = 不在任何回看窗口里,行为逐字不变。
        ('exported_at', "TEXT NOT NULL DEFAULT ''"),
    ),
    # 回执记的是**哪一世**的那篇文章 —— `item_key` 按网址算,清理掉再收录回来还是
    # 同一个 key。判据、事故形态和「老库补成空串 = 说不清,按没有回执办」这一条,
    # 都在 `kb/browser_capture._same_life`。
    'browser_capture_audit': (('item_fetched_at', "TEXT NOT NULL DEFAULT ''"),),
    # 搁置那张旁表的两道身份:「哪一世的内容」和「这一世的哪一次决定」。
    # 判据、各挡什么、以及为什么不能拿一个秒级时间戳同时充当两种身份,全在
    # `kb/intake_diag/shelf.py` 的模块说明里。**空串 = 认不出来 = 当作没有搁置**。
    'feed_item_shelf': (
        ('item_life', "TEXT NOT NULL DEFAULT ''"),
        ('decision_id', "TEXT NOT NULL DEFAULT ''"),
    ),
    # 处理记录里那几个 key 各属于哪一世、那一笔搁置发的是哪几个决定编号。
    # 前者让「我对这一条做过什么」只列这一世的(删掉重收之后不该看见上一世的
    # 记录),后者让那颗「恢复」绑得住它自己那次决定(见 `shelf.release`)。
    # 都是 JSON 数组,老库补成 `[]`。
    'intake_care_log': (
        ('item_lives', "TEXT NOT NULL DEFAULT '[]'"),
        ('decisions', "TEXT NOT NULL DEFAULT '[]'"),
    ),
}


#: 一个不可复用的生命周期标识。16 字节随机 → 32 位定长 hex,和 `item_key` 同形状
#: (都是定长 hex),所以日志里按整串配子串不会互为前缀。
_LIFE = 'lower(hex(randomblob(16)))'


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
                if (table, name) == ('feed_items', 'life'):
                    # **回填只在刚补上这一列的那一次跑**,而且和 ALTER 在同一个写
                    # 事务里。放到锁外、或者每次开连接都 `WHERE life=''` 扫一遍的
                    # 写法要为**每一次** `db()` 付一次全表扫 —— 那是每个接口请求
                    # 都要付的钱。存量那几千行各拿一个随机值:它们在补列之前就已经
                    # 在库里,没有「上一世」可言,拿到什么值都行,只要彼此不同。
                    conn.execute('UPDATE feed_items SET life=' + _LIFE)
    # **触发器统一种 `life`,不靠各个写入方自觉**(理由见 `_ADDED_COLUMNS`)。
    # 建在这里而不是 `SCHEMA` 里:老库跑 `executescript(SCHEMA)` 的那一刻 `life`
    # 这一列还不存在,而 SQLite 在 `CREATE TRIGGER` 时就要解析触发器体里的列名,
    # 当场报 no such column、整个首访请求 500。
    # `WHEN new.life=''` 让显式带了值的写入方(迁移脚本、测试夹具)说了算。
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS feed_items_life AFTER INSERT ON feed_items"
        " WHEN new.life='' BEGIN"
        f"  UPDATE feed_items SET life={_LIFE} WHERE key=new.key;"
        " END")

