# -*- coding: utf-8 -*-
"""「这一轮算哪些条目」的全部判据:队列、手动重试的射程,以及一次确认的凭证。

从 `translate.py` 搬出来**只因为 700 行上限**。分界线是「问的是哪个问题」:
这里只回答「哪些条目算在里面」(纯判据 + 计数 + 点名),翻译本体、取数循环、
存盘和语言复判留在 `translate.py`。所以这里一个模型调用都没有。

**名字一律由 `translate` 原样再导出**,调用点和直测入口一个字都不用改 ——
`translate.queued_sql()` / `translate.stuck()` / `patch.object(translate, 'pending')`
照旧成立(`from … import` 在 `translate` 里建了同名模块全局,打桩改的就是那一份,
而 `translate_pending` 查的也是那一份)。**反过来,这个模块内部的互相调用打不到
桩** —— `requeue_failed` 调的 `scope_signature` 是本模块的那个,
`patch.object(translate, 'scope_signature')` 对它无效。要造「集合变了」就真的
去改库(回归里那两条就是这么摆的)。

这里没有 `MAX_BODY` / `_model`:它们被 `patch.object(translate, …)`
打过桩,而用它们的函数留在 `translate.py` —— 搬过来的话打桩只改到一份副本。
`MAX_TRIES` 跟着 `queued_sql` 走(判据的一部分),它只被读、没被打桩过。
"""

from __future__ import annotations

import hashlib

from kb.feeds import registry as reg
# 搁置那一道判据(`RETRY_SQL` 和 `manual_sql` 都要 AND 上它)。模块层 import
# 不成环:`shelf` 自己对 kb 的依赖全是函数内的延迟 import。
from kb.intake_diag import shelf


MAX_TRIES = 3


def queued_sql() -> tuple[str, list]:
    """"还排在翻译队列里"的判据,只有这一份。

    这些条目**不按英文导出**:翻译失败或同步被停止时，尚未译完的内容不能
    抢先以英文写进 vault，否则索引、提取、检索、日报读的都是那份英文。
    队列有出口——试满
    MAX_TRIES 次仍然失败的就不在这个集合里了,那时按原文导出,免得一条
    永远译不动的内容永远进不了知识库。

    判据是 `translated_at`(`_save` 只在整条译成时写它),不是"有没有 body_zh"
    ——只有标题的短动态译完之后 body_zh 仍然是空串,拿它判的话这一条译完了
    还赖在队列里,每轮重译一遍。

    **被人搁置的条目不算待翻译**(`kb.intake_diag.shelf`):「暂缓」要连这一步
    一起停住 —— 只拦补抓的话,一条正文已经试满额度、人刚点过暂缓的英文内容照旧
    被买走一次翻译。**搁置那一道加在这里而不是 `unsettled_sql` 里**,理由见
    `shelf.free_sql`。

    **正文还没定下来的条目不算待翻译**(`fulltext.unsettled_sql`,**不是**
    `queued_sql`):它们此刻手里那份正文是 feed 给的跳转壳,正文马上要被整条换掉。
    不排掉的话同一条内容买两次模型调用 —— 先按壳译一遍,补上正文再译一遍,而第
    一份译文随后就被 `fulltext._save` 丢掉。翻译是这条链上最贵的一步,顺序不能反。
    **挂起态(等人看一眼)也算「还没定下来」**,那一档压根不在补抓队列里,所以这
    里问的必须是 `unsettled_sql` —— 用 `queued_sql` 的后果实测过,见它自己的说明。
    """
    from kb.feeds import fulltext      # 函数内 import:两个模块互相认识对方的口径
    from kb.intake_diag import shelf
    cond, args = fulltext.unsettled_sql()
    return (f"lang='en' AND translated_at='' AND translate_tries<? AND NOT ({cond})"
            f" AND {shelf.free_sql()}",
            [MAX_TRIES, *args])



def pending(kind: str, source_id: int | None = None,
            only_manual: bool = False, conn=None) -> int:
    """还有多少条排在翻译队列里。

    两个可选的收窄各有用处,口径和 `translate_pending` 共用同一份判据:
    `source_id` 给面板那颗按钮算射程(它承诺的是哪一条订阅),`only_manual`
    给重试那一轮算「这一轮还剩几条」。
    """
    cond, args = queued_sql()
    if only_manual:
        cond += ' AND translate_manual=1'
    extra, eargs = _retry_where(source_id)
    sql = f'SELECT count(*) FROM feed_items WHERE kind=? AND {cond}{extra}'

    def run(c):
        return c.execute(sql, (kind, *args, *eargs)).fetchone()[0]

    if conn is not None:          # 调用方已经在一个读事务里(见 `stuck`):别另开
        return run(conn)          # 连接 —— 那次读就不在它那个快照里了
    with reg.db() as c:
        return run(c)


# 「手动重试」的射程:判成外文、还没有译文、而且**试过至少一次**的条目。
#
# **为什么含试满 `MAX_TRIES` 的那一批** —— 它们正是面板上**看不见**的那一半。
# 试满额度的条目按设计离开队列、按原文导出(见 `queued_sql`),于是 vault 里
# 躺着英文、条目上一个标记都没有、顶上那句「N 条还在等翻译」也不数它们,而那
# 批英文已经被索引、被打了标签。更要紧的是**系统里原来没有任何入口能把它们放
# 回队列**:`queued_sql` 的 `translate_tries<MAX_TRIES` 把它们排除在外,所以面
# 板上的「同步」和「重试失败项」点多少次都选不到它们 —— 中转站一轮 503 就能造
# 出上百条(2026-10-13 实测 rss 42 条 + social 66 条),而这是一个**只能靠改库
# 才能解除的终态**。
#
# **判据是「试过至少一次」,不是「有错误信息」。** `translate_error` 被 `_save`
# 成功时清空过,库里也确实有 `translate_tries=3` 而错误是空串的存量条目
# (2026-10-13 实测 social 2 条);按错误信息筛会把它们永久留在库外,而那正是
# 这颗按钮唯一要救的东西。
#
# **也不含 `translate_tries=0` 的那些** —— 它们会在同步时自动取走，
# 额度一条没动过，放回队列是空操作。
#
# **也要带上「这一世没有被搁置」。** 这一段同时是三件事的判据:那颗手动重试按钮的
# 射程、`stuck()` 里 `shipped`/`exhausted` 两档的计数、以及面板上「翻译重试已停止」
# 那一档。不带的话:人点完「暂缓」,这一档照旧把它数进去,而那颗按钮一点就把它的
# 翻译额度清零、重新买一次模型调用 —— 人刚刚说的是「这条先别动」(审查第 1 轮 F01
# 的对称面)。同源见 `shelf.free_sql`。
RETRY_SQL = ("lang='en' AND translated_at='' AND translate_tries>0"
             f" AND {shelf.free_sql()}")


def _retry_where(source_id: int | None) -> tuple[str, list]:
    """手动重试的 WHERE,按订阅收窄那一半只有这一份(计数和写回共用)。

    面板上那行提示跟着当前筛选走(`registry.items` 的 `where` 带 source_id),
    所以按钮的射程也得跟着收 —— 否则筛到一条订阅时显示「3 条译不动」、点下去
    却把整个来源的上百条一起放回队列,代价是用户没点过的那一大笔模型调用。
    """
    if not source_id:
        return '', []
    return (' AND key IN (SELECT item_key FROM subscription_items'
            ' WHERE subscription_id=?)', [source_id])


def stuck(kind: str, source_id: int | None = None) -> dict:
    """译不动的内容有多少条、其中几条已经按原文入库、最近一次是什么原因。

    给面板那行提示用。它原来只说「N 条还在等翻译」,对「刚抓回来还没轮到」和
    「这一批已经失败两次、再失败一次就永久按英文入库」说的是同一句话,而两者
    的下一步完全相反:前者等下一轮就好,后者得有人去看一眼为什么。

    `shipped` 单独数一档:那批条目**不在**「还在等翻译」的计数里(它们已经离开
    队列、按英文入库了),不单独说出来的话面板上没有任何东西提到它们的存在。

    **`exhausted` 和 `shipped` 是两个问题,别合成一个。** 前者问「射程里有几条
    已经离开队列」(`queued` 选不到它们,所以总量要把它们加回来),后者问「其中
    几条**真的按原文落盘了**」—— 面板上那句「已按原文写进知识库」只能用后者。
    两者在「最后一次翻译失败之后、导出之前」那个窗口里不同(第 1 轮审查实测:
    `translate_tries` 满、`exported_hash=''`,而临时 vault 里一篇文章都没有),
    混用就是对一篇压根不在知识库里的文章说它已经入库了。
    反过来**射程不许改用 `shipped`**:那一档照样会被 `requeue_failed` 放回去
    (`manual_sql` 不看导出状态,那是对的),按它算就又变成「说 3 条、动 4 条」。
    """
    extra, eargs = _retry_where(source_id)
    # `exhausted` 要和点名集合(`manual_sql`)逐字同一个射程 —— 它是确认框那个
    # 数字的第二段,而点名集合决定实际译谁。两边口径一漂就是「说一个数、动另一
    # 批」:排除补抓队列而确认里算了它,就会少译一条(如实,但数字不准);反过来
    # 算了而不排除,就是多花一笔用户没确认的钱。
    from kb.feeds import fulltext
    body_cond, body_args = fulltext.unsettled_sql()
    with reg.db() as c:
        # **四样东西必须出自同一个数据库快照:两个数字、那句原因、那份指纹。**
        # 「同一个连接」给不了这个保证 —— `sqlite3` 在 legacy `isolation_level`
        # 下 SELECT **不开事务、不持任何锁**(元规则第一条说的就是它),于是几次
        # 读之间能插进别人的提交。后果不是「数字晚一点更新」:确认框按前两个数字
        # 说「1 条」,而带回去的指纹是那**两条**的,于是两道指纹校验都正确匹配
        # 那两条、都把这个错误的确认当成有效授权 —— 确认 1 条、译 2 条
        # (2026-10-14 第 6 轮审查实测:在数量读完、指纹未读的那一刻并发
        # `PATCH {"fulltext": false}`,回包是 `exhausted=1` + `sig='2:…'`)。
        # `BEGIN DEFERRED` 一上来就把这个连接钉在一个快照上(WAL 下读不挡写,
        # 别人照旧提交得了,只是这一份回包不会半新半旧)。
        c.execute('BEGIN DEFERRED')
        row = c.execute(
            f'SELECT count(*) AS failed,'
            f' sum(translate_tries>=? AND NOT ({body_cond})) AS exhausted,'
            f" sum(translate_tries>=? AND exported_hash<>'') AS shipped"
            f' FROM feed_items WHERE kind=? AND ({RETRY_SQL}){extra}',
            (MAX_TRIES, *body_args, MAX_TRIES, kind, *eargs)).fetchone()
        # 原因取**最近抓回来的那一条**的非空错误:一轮整批失败时它们的原因
        # 一模一样,而要紧的是最近一次发生了什么,不是最早那次。
        why = c.execute(
            f'SELECT translate_error FROM feed_items WHERE kind=? AND ({RETRY_SQL})'
            f"{extra} AND translate_error<>'' ORDER BY fetched_at DESC, key LIMIT 1",
            (kind, *eargs)).fetchone()
        # 确认框那个数字和这份指纹要出自同一个快照 —— 分两次读的话,中间那一下
        # 变化会让前端拿着「1 条」的文案和「2 条」的指纹,后端必然拒绝,而用户
        # 看到的是一颗点不动的按钮。
        sig = scope_signature(kind, source_id, conn=c)
        # 还排在队列里的有几条。**按订阅收窄、不跟搜索词走** —— 它是面板上
        # 那颗按钮的射程说明,而顶上那个 `translating` 跟着搜索词变(它回答
        # 的是「这一屏列出来的内容里有几条扣着没入库」,不是按钮会动谁)。
        # **`conn=c` 不能省**:它原来在这个 `with` 之外、另开一个连接读,于是
        # 哪怕上面那几次读锁在一个快照里,这一个数仍然可能来自另一个时点。
        queued = pending(kind, source_id, conn=c)
    return {'failed': row['failed'] or 0, 'shipped': row['shipped'] or 0,
            'exhausted': row['exhausted'] or 0, 'sig': sig,
            'reason': (why['translate_error'] if why else '')[:300],
            'queued': queued}


def manual_sql() -> tuple[str, list]:
    """「这一轮用户点名要译的」的射程,只有这一份。

    这个来源(或这一条订阅)里**所有还没有译文、而且现在就译得了的**外文条目。

    **比 `RETRY_SQL` 宽,这是有意的 —— 它决定的是「这一轮译谁」,不只是「谁要
    回到队列」。** 按钮有两种点法:失败过的那批要放回队列(额度清零),而只是还
    没轮到的普通积压(`translate_tries=0`)要的是「现在就译,别等下一轮」。后者
    额度没动过、清零是空操作,可**标记**一点都不是空操作:`translate_pending`
    在重试那条路上只译带标记的条目(见 `only_manual`),不标就是一条都不译。

    **但正文还在补抓队列里的条目一个都不许标记** —— 这是付费边界的一半。
    原来含它们,依据是"取数那一刻选不到,标记无害"。那个依据**错了**:补抓和
    翻译在**同一轮**里先后跑(见 `kb/collectors/feeds.py` 的 `materialize`),
    补抓成功的那一下它就离开补抓队列、进了翻译队列,于是带着标记在同一轮被取走
    —— 而确认框那个数字是按翻译队列算的,压根没算它(2026-10-14 第 2 轮审查实测:
    确认 1 条、实际调了 2 次模型,两条都译成了中文)。不需要并发、不需要上一轮
    失败就能发生。

    补抓成功之后 `fulltext._save` 会把译文和额度一起清掉,所以它是一条全新的
    待译条目 —— 下一轮常规同步正常轮到它。**少译一条是如实的**(确认框说的
    就是 1 条),多译一条是花用户没确认过的钱。

    **搁置的那几条一个都不许标记,理由和 `RETRY_SQL` 那一段一样** —— 这是付费边界
    的另一半:`manual_sql` 同时是 `scope_signature`(确认框的指纹)和
    `requeue_failed`(真正点名那一下)的射程。不带搁置判据的话,确认框里那个数字把
    人刚点过「暂缓」的条目算了进去,点下去也真的会去译它(审查第 1 轮 F01 的对称
    面)。同源见 `shelf.free_sql`。
    """
    from kb.feeds import fulltext      # 函数内 import:两个模块互相认识对方的口径
    cond, args = fulltext.unsettled_sql()
    return (f"lang='en' AND translated_at='' AND NOT ({cond})"
            f" AND {shelf.free_sql()}"), list(args)


# 点名标记的清场语句。`requeue_failed`(开工前作废上一轮)和 `clear_manual`
# (收工后收掉这一轮)共用这一份 —— 两头问的是同一个问题:「库里还留着谁的标记」。
_CLEAR_MANUAL = ('UPDATE feed_items SET translate_manual=0'
                 ' WHERE kind=? AND translate_manual=1')


class ScopeChanged(Exception):
    """确认那一刻的候选集合,和真要动手时的不是同一批。"""


def scope_signature(kind: str, source_id: int | None = None, conn=None) -> str:
    """「这一轮会动哪几条」的指纹。确认框弹出时算一次,真要动手时再算一次。

    **口径就是 `manual_sql()`** —— 确认框那个数字(`stuck` 的 `queued` +
    `exhausted`)数的也正是它那一批(见 `stuck` 里那段说明),所以两边天然对齐,
    不会出现「指纹变了而数字没变」这种自相矛盾的状态。

    **为什么是候选集合而不只是条数。** 用户确认的是「这个范围还没译的 N 条」,
    而范围固定住之后集合**仍然会变**:它的判据里有订阅自己的设置 ——
    `manual_sql` 排除「正文还在补抓队列里」的条目,而那一档取决于
    `subscriptions.fulltext`。确认框开着的时候把那个开关关掉,等正文的那几条
    当场变成可译的(2026-10-14 第 5 轮审查实测:确认框说 1 条,另一个窗口
    `PATCH {"fulltext": false}`,点确认之后译了 2 条 —— 订阅没换、请求里的
    `source_id` 也没丢,漂的是集合本身)。按条数比能抓住这一例,但抓不住
    「一条译完了、同时一条新抓回来」—— 那时条数一样而动的是另一批内容,所以
    判据落在**成员**上:宁可多拒一次(他重点一下就是了),不许多译一条。

    指纹不进 `feed_items`、不落盘:它是一次确认的凭证,活不过这一轮。
    """
    cond, cargs = manual_sql()
    extra, eargs = _retry_where(source_id)
    sql = f'SELECT key FROM feed_items WHERE kind=? AND ({cond}){extra} ORDER BY key'
    args = (kind, *cargs, *eargs)

    def run(c):
        keys = [r[0] for r in c.execute(sql, args)]
        # 带上条数:它是人读得懂的那一半(日志里能看出是 1 变 2 还是 2 变 1)。
        return f"{len(keys)}:{hashlib.sha1('|'.join(keys).encode()).hexdigest()[:16]}"

    if conn is not None:          # 调用方已经在一个(写)事务里:别另开连接 ——
        return run(conn)          # 那次 SELECT 就和这次写入不在同一个快照里了
    with reg.db() as c:
        return run(c)


def requeue_failed(kind: str, source_id: int | None = None,
                   scope_sig: str = '') -> int:
    """把这个来源(或这一条订阅)里还没译的条目点名放到队首。返回点了几条。

    三件事缺一不可:
    ①**额度清零** —— 试满 `MAX_TRIES` 的那批否则永远选不到(见 `RETRY_SQL`);
    ②**置 `translate_manual`** —— 它同时承担两件事:
      - **排序**:取数是「失败过的优先」(`translate_tries DESC`),而第①步恰好
        把这一批变成「从没试过」、排到队尾。队列里积压超过一轮上限
        (显式限量调用)时,用户点完按钮、等完一整轮同步,库里一个字都不会变:
        实测点按钮那一刻队列里 246 条,恢复的 108 条排在第 139 位往后,下一轮
        取的 40 条里一条都没有它们。那是「按钮没用」里最难查的一种 —— 它什么
        都不报错。
      - **射程**:重试那一轮只译带标记的条目。**这一半是付费边界**:标记是唯一
        跟得到 `translate_pending` 取数那一步的东西 —— 面板上筛着一条订阅、
        确认框承诺「这条订阅的 1 条」,而取数按整个 kind 取 40 条,于是另一条
        订阅的内容也被译了,实际调用和费用超出用户确认的范围
        (2026-10-13 第 1 轮审查实测:`requeued=1` 而 `pending=2`,两条都拿到
        了译文)。`crawl_filter` 到不了那一步 —— `crawl_channels` 只留
        `channel`/`accounts` 两个键,而 `materialize()` 调的是整个 kind 的
        `translate_pending`。

    ③**作废上一轮留下的点名** —— 见下面那段。

    **不动 `exported_hash`**:那批英文此刻确实就在 vault 里,清掉它等于让一篇
    **已经能读、能搜到**的文章连同入口一起消失,而译文还没到手。等 `_save`
    真拿到译文时它自己会清(口径同 `fulltext._save`)。
    """
    extra, eargs = _retry_where(source_id)
    cond, cargs = manual_sql()
    with reg.db() as c:
        # **判据和写入在同一个写事务里。** 先 SELECT 算一遍指纹、再 UPDATE 的
        # 写法在两个执行体之间压根不成立:`sqlite3` 在 legacy `isolation_level`
        # 下 SELECT 不开事务、不持任何锁,那次预检和随后的写入之间没有东西把
        # 它们绑在一起。`BEGIN IMMEDIATE` 一上来就拿写锁,于是这中间没有别的
        # 写入方 —— 校验和点名同生共死。
        c.execute('BEGIN IMMEDIATE')
        # **先把上一轮的点名作废,而且和这一次的点名在同一个事务里。**
        # 标记是一次性的,可收它的那句 `finally`(`clear_manual`)只在进程活着时
        # 跑得到 —— 进程被 kill、机器断电的那一轮,标记照旧留在库里,而且和本次
        # 写下的那些**长得一模一样**。取数那一侧要的是「标记为 1 且属于本次范围」,
        # 同一条订阅下的残留**两个条件都满足**。
        #
        # 单靠 `manual_sql` 排除补抓队列挡不住它:那一道只管「这一次标记谁」,
        # 管不了「上一次标记过谁」。按本次 `source_id` 收窄也只挡得住**跨订阅**的
        # 残留(2026-10-14 第 3 轮审查实测:在 `fetch_pending` 处注入
        # `os._exit(73)` —— 那一刻两条都还是可译的、都被合法标记了 —— 重开后给
        # 这条订阅打开「补抓原文正文」,其中一条进了补抓队列,于是确认框按翻译
        # 队列算出「1 条」;而补抓就排在翻译前面,它补完正文离开补抓队列的那一下,
        # 旧标记还在身上,两条一起被取走译掉)。
        #
        # 清场的射程是**整个 kind**,不是本次那个范围 —— 范围之外的残留同样是
        # 上一轮的遗物,留着只会让下一次常规同步继续把它排在队首。射程不含
        # `kind` 之外的行:别的来源各有自己的那一轮。
        #
        # 这一道让「库里的 `translate_manual=1` 全都是本轮写的」成为一个不变量,
        # 所以不需要再存一份运行身份或候选 key 集合 —— 管道是单例,而这两句和
        # 紧跟着的取数在同一个执行体里,中间没有别的写入方。
        c.execute(_CLEAR_MANUAL, (kind,))
        # **确认那一刻的那一批,和此刻的是不是同一批。** 范围(哪条订阅)由前端
        # 在打开确认框之前定死,可**范围里的集合**还会变 —— 见 `scope_signature`。
        # 不是同一批就一条都不点名:清场照旧落盘(旧标记一个都不许留着,否则紧
        # 跟着那次取数会把它们当成本次授权),然后把这一轮交回调用方处理。
        #
        # **空指纹是「调用方没有确认快照」那一档**(curl / 脚本直调),放行:
        # 没有确认框就没有"承诺过的数字"这回事,拒绝一个什么都没被承诺的调用方
        # 没有意义。面板那条路一定带着指纹。
        if scope_sig:
            now = scope_signature(kind, source_id, conn=c)
            if now != scope_sig:
                c.commit()
                raise ScopeChanged(
                    f'这个范围里等着译的内容在确认之后变过了（确认时 {scope_sig}'
                    f'，现在 {now}），这一轮一条都没译。请回面板看一眼新的条数，'
                    '再点一次。')
        cur = c.execute(
            "UPDATE feed_items SET translate_tries=0, translate_error='',"
            f' translate_manual=1 WHERE kind=? AND ({cond}){extra}',
            (kind, *cargs, *eargs))
        return cur.rowcount or 0


def clear_manual(kind: str) -> int:
    """把这个来源的点名标记全部收掉。返回收了几条。

    标记是**一次性**的:它说的是「用户点过那一下」,不是这条内容的属性。一轮重试
    跑完就该收掉 —— 不收的话下一次常规同步仍然只盯着这批(排序上压过所有新内容),
    而且**付费边界会漂**:那时筛着另一条订阅点重试,上一轮残留的标记会把别人的
    条目一起带进 `only_manual` 的射程。

    译成的那条由 `_save` 自己清;这里收的是**没译成**的那些(失败、作废、这一轮
    没轮到)。所以它必须排在整轮翻译之后,而且无论成败都要跑。

    **它不是唯一的清场,也挡不住进程被 kill 那一档** —— 那时这一句压根跑不到,
    开工前 `requeue_failed` 里那次清场才是兜底的那一道。两头共用 `_CLEAR_MANUAL`。
    """
    with reg.db() as c:
        cur = c.execute(_CLEAR_MANUAL, (kind,))
        return cur.rowcount or 0
