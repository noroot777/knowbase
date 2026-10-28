# -*- coding: utf-8 -*-
"""**条目级的「还有多少在等、卡在哪一环」—— 判据只有这一份。**

**硬约束在 `docs/decisions/intake-diagnostics.md` 第一节 —— 改这块之前先读那一份。**

这一份回答的问题和同步中心那个「需处理」区**不是同一个**:
- 同步中心答「**这一轮 / 今天**那几**步**(crawl / export / index / extract)欠不欠账」
  (`kb.pipeline_retry.today_failed` / `owed_now`,屏幕上的量词是**步骤**);
- 这一份答「**全库**还有多少**条内容**卡在哪一环、为什么」(量词是**条**)。

两者的数字天生不一样,所以**不许在屏幕上共用「需处理」这个词**(CLAUDE.md:那个词
只许一处念,而且要和重试那颗按钮同一个数字)。这个面板的那一组叫「待决定」,页头
那行事实数字写明「条」,并且明说另一份在哪 —— 事故形态是同一屏上两个数字配同一
句话,`kb/static/js/pipe_view.js` 的注释里有原形。

**每一档的判据都从它真正的主人那里取**(`fulltext.queued_sql` / `held_sql` /
`exhausted_sql`、`translate_scope.queued_sql` / `RETRY_SQL`),这里**只做分组和
排序**,一条新的 WHERE 都不发明 —— 发明一条的后果是面板上的数字和管道真正会取
的那一批分叉,而人照着面板做决定。

**三档必须分开,不许统称「抓取中」**(演示文档的状态契约,也是排查那天最花时间的
一件事):「还没取到全文」(等一轮调度就好)、「需人工确认」(证据不足,等人看一眼)、
「确定失败、预算耗尽」(人要决定给不给新额度)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kb.feeds import fulltext, registry as reg, translate_scope
from kb.intake_diag import shelf

#: 「今天」「最近几天」按项目时区算(CLAUDE.md),不按宿主机。
TZ = ZoneInfo('Asia/Shanghai')

#: 「已完成」那一组回看几天。**这一组必须有窗口**:库里 5000 多条内容绝大多数
#: 本来就一路顺利,把它们全列进「已完成」等于把这一屏变成一份全库清单,而人要
#: 看的是「我处理过的那几条后来怎么了」。
RESOLVED_DAYS = 7

#: 收录这条链上的四个环节,**和详情里那四步进度一一对应**。
BODY, TRANSLATE, INGEST, SOURCE = '正文抓取', '翻译', '入库处理', '来源连接'

#: 条目的四步进度(来源级问题另有自己的四步,见 `kb.intake_diag.sources`)。
ITEM_STEPS = ('发现链接', '补齐正文', '翻译处理', '写入知识库')


@dataclass(frozen=True)
class Lane:
    """一档条目级积压。`group` 是面板上的分组,`step` 是它卡在第几步(0 基)。"""

    id: str
    group: str
    stage: str
    state: str
    summary: str
    step: int
    #: 这一档可以走哪几个出口(口径在 `kb.intake_diag.actions`)。
    exits: tuple[str, ...]


#: 分组名。**「待决定」刻意不叫「需处理」**,理由见模块说明。
ATTENTION, QUEUED, RUNNING, RESOLVED, ARCHIVED = (
    'attention', 'queued', 'running', 'resolved', 'archived')

LANES: tuple[Lane, ...] = (
    Lane('body_held', ATTENTION, BODY, '待人工确认', '已挂起，不会自动重试', 1,
         ('requeue', 'body', 'relink', 'run', 'snooze', 'exclude')),
    Lane('body_exhausted', ATTENTION, BODY, '重试已停止', '重试预算已用完', 1,
         ('run', 'requeue', 'body', 'relink', 'snooze', 'exclude')),
    # **不许一概说「已按原文入库」**:这一档只说明翻译额度用完了,而「入没入库」
    # 是导出那一头的事实(`exported_hash`)—— 还没导出过的那几条照旧在这一档里,
    # 对它们说「已经按原文写进知识库」是谎报(审查第 1 轮 F06)。单条到底入没入
    # 库在详情里按 `exported_hash` 逐条说。
    Lane('translate_exhausted', ATTENTION, TRANSLATE, '重试已停止',
         '译不动，按原文继续走后面的流程', 2,
         ('run', 'requeue', 'snooze', 'exclude')),
    Lane('body_queued', QUEUED, BODY, '待执行', '未开始 · 等待调度', 1,
         ('run', 'body', 'relink', 'snooze', 'exclude')),
    Lane('translate_queued', QUEUED, TRANSLATE, '待执行', '未开始 · 等待调度', 2,
         ('run', 'snooze', 'exclude')),
    Lane('ingest_queued', QUEUED, INGEST, '待执行', '正文已定，等待写入知识库', 3,
         ('run', 'snooze', 'exclude')),
    # 两档「已完成」说的都是**已经写进知识库**(判据里那道 `exported_hash`),
    # 不是「上游那一环完成了」—— 拿环节完成推断最终交付是审查第 1 轮 F06 那一类。
    Lane('body_recent', RESOLVED, BODY, '已完成', '正文已补齐，并已写进知识库', 3, ()),
    Lane('translate_recent', RESOLVED, TRANSLATE, '已完成', '译文已写进知识库', 3, ()),
    # **第三条到达成功的路:纯入库。** 一条自带中文全文、不需要补抓也不需要翻译的
    # 内容,`fulltext_at` / `translated_at` 永远是空串 —— 上面两档认不出它,于是人
    # 在面板上对它点过「立即执行」、它也真写进了知识库,而他在「已完成」里找不到
    # 结果(审查实测:那几档「不是一个覆盖成功后状态的闭环」)。**排在上面两档之后**
    # (`probes()` 的顺序),所以补抓 / 翻译成功的那几条照旧归各自那一档,不会数两遍。
    Lane('ingest_recent', RESOLVED, INGEST, '已完成', '已写进知识库', 3, ()),
)

BY_ID = {lane.id: lane for lane in LANES}
GROUPS = (ATTENTION, QUEUED, RUNNING, RESOLVED, ARCHIVED)
STAGES = (BODY, TRANSLATE, INGEST, SOURCE)


def resolved_cutoff(now: datetime | None = None) -> str:
    """「已完成」窗口的下界,写成和 `reg.now()` **同一种形状**的字符串。

    **非得自己算不可**:库里 `fulltext_at` / `translated_at` 存的是
    `reg.now()` 的 UTC ISO 带偏移(`2026-10-09T07:56:11+00:00`),而
    `datetime('now','-7 day')` 给的是 `2026-10-19 10:52:45` —— 没有 `T`、
    没有偏移。两者做字符串比较时在日界上判反(`'T' > ' '`),而这一列上
    **没有**可用的日期函数索引可言。窗口按项目时区回看,再折回 UTC 比较。
    """
    at = (now or datetime.now(TZ)).astimezone(TZ) - timedelta(days=RESOLVED_DAYS)
    return at.astimezone(timezone.utc).isoformat(timespec='seconds')


def where(lane_id: str) -> tuple[str, list]:
    """这一档的 WHERE(拼在 `FROM feed_items` 之后)。**判据一律转手,不新发明。**"""
    if lane_id == 'body_queued':
        return fulltext.queued_sql()
    if lane_id == 'body_held':
        return fulltext.held_sql(), []
    if lane_id == 'body_exhausted':
        return fulltext.exhausted_sql()
    if lane_id == 'translate_queued':
        return translate_scope.queued_sql()
    if lane_id == 'translate_exhausted':
        # 「译不动」= 试过、试满、还没有译文,而且正文已经定下来(不然它该排在
        # 补抓那一档里等正文)。`RETRY_SQL` 是「手动重试」那颗按钮的射程,**这里
        # 复用它再 AND 上额度用完**:两处同源,面板上数的那几条正是那颗按钮会
        # 放回队列的那几条。
        # 搁置那一道**在 `RETRY_SQL` 里面**(那颗按钮的射程和这一档的计数共用
        # 它),这里不再 AND 一遍 —— 同一个问题判两遍就是两份口径。
        unsettled, uargs = fulltext.unsettled_sql()
        return (f'{translate_scope.RETRY_SQL} AND translate_tries>=?'
                f' AND NOT ({unsettled})',
                [translate_scope.MAX_TRIES, *uargs])
    if lane_id == 'ingest_queued':
        # **判据只有一句:vault 里还不是这一份内容。**
        # 上一版还要求 `fulltext_at` 非空(「必须发生过补抓」),那是拿它当排重用
        # 的 —— 怕那 212 条「还没补抓、导出闸门正扣着」的壳在这里再数一遍。排重
        # 本来就由 `exclusive_where` 按 `probes()` 的顺序做(补抓三档排在这一档
        # 前面),多那一道的代价是**漏报**:一条压根没开补抓、自带中文全文的普通
        # RSS / 网页 / 手动条目,`fulltext_at` 永远是空串,于是导出还没跑(或者
        # 跑失败、被停了)时它既不在补抓档、也不在翻译档、也进不了这一档 ——
        # 全库真实积压里最大的那一类凭空消失(审查第 1 轮 F06)。
        # **漏报是静默丢数据,多报顶多多列一条**(元规则)。
        return f'exported_hash<>content_hash AND {shelf.free_sql()}', []
    # 两档「已完成」都**自己核一遍导出回执**(`exported_hash=content_hash`)。
    # 靠 `probes()` 的顺序也能把没导出的那几条先交给 `ingest_queued`,可那是
    # 顺序的副作用、不是这一档自己说的话:判据里不写,改了一次声明顺序就会在
    # 屏幕上出现「已完成」而 vault 里什么都没有(审查第 1 轮 F06)。
    # **判据是「这一世真的成功过」+「知识库里就是这一份」,不是「还剩几次重试」。**
    # 上一版两档各 AND 了一句 `*_tries>0`(本意是「它曾经卡过,所以才该出现在这
    # 块面板的已完成里」),而那两列记的是**剩余失败次数**,不是成功历史:
    # - 「仅重新排队」把 `fulltext_tries` 清成 0 —— 人放回队列、补抓真的成功、
    #   文章也进了知识库,它却从待办里消失、又进不了已完成,**哪一档都不在**;
    # - `translate._save` 成功时必定 `translate_tries=0`,所以「译完 + 还剩次数
    #   大于 0」这个组合**压根不可能同时成立**:那一档是条死路,夹具里手写
    #   `fulltext_tries=1` 才看得见它(实测 `lanes.split` 回 `''`)。
    # 成功的事实本来就各有自己的一列(`fulltext_at` / `translated_at`,人工补正文
    # 写的也是前者),再加上这一档自己核的那道导出回执,已经是完整的「成功 + 已
    # 交付」。**用剩余次数当成功记录是拿一个会被清掉的值去记历史**(元规则:
    # 判不准宁可多报,漏报是静默丢数据)。
    if lane_id == 'body_recent':
        return ("fulltext_at<>'' AND fulltext_at>=?"
                f" AND exported_hash=content_hash AND {shelf.free_sql()}"), [resolved_cutoff()]
    if lane_id == 'translate_recent':
        return ("translated_at<>'' AND translated_at>=?"
                f" AND exported_hash=content_hash AND {shelf.free_sql()}"), [resolved_cutoff()]
    if lane_id == 'ingest_recent':
        # **判据是交付本身**(`exported_at` 由交付确认那一句和回执同一笔写下),
        # 不是上游哪一环完成了 —— 这一档底下躺的恰恰是「上游两环都没发生过」的
        # 那一类。`exported_hash=content_hash` 照旧自己核一遍:`exported_at` 记的
        # 是最近一次交付的时刻,而内容在那之后又更新过时它该回到待办里。
        # 老库那几千行补的是空串,所以它们一条都不在这个窗口里(行为逐字不变)。
        return ("exported_at<>'' AND exported_at>=?"
                f" AND exported_hash=content_hash AND {shelf.free_sql()}"), [resolved_cutoff()]
    raise ValueError('未知的积压档位')


def probes() -> list[tuple[str, str, list]]:
    """分桶的探问顺序:搁置那一档排在最前,随后按 `LANES` 的声明顺序。

    **这个顺序就是「一条内容算哪一档」的唯一答案**(`exclusive_where` 按它把前面
    各档减掉)。排序的依据是**这条链上谁先卡住**:搁置(人说过别动)→ 正文三档 →
    翻译两档 → 入库。

    **各档的原始判据彼此是会重叠的,这不是 bug。** 一条正文试满额度的英文内容,
    按设计会带着那份 feed 壳走完翻译和导出(`fulltext.unsettled_sql` 刻意放它过
    去),所以它**同时**满足「正文重试耗尽」和「翻译队列」——生产管道两步都会碰它。
    可屏幕上它只能算一条、而且只能算在**根因**那一档:人要做的是把正文弄对,不是
    再买一次翻译。原始判据照旧转手给管道(一个字不改),重叠只在这一层按顺序化解。
    """
    out = [(ARCHIVED, shelf.live_sql(), [])]
    for lane in LANES:
        cond, args = where(lane.id)
        out.append((lane.id, cond, args))
    return out


def exclusive_where(lane_id: str) -> tuple[str, list]:
    """这一档**独占**的 WHERE:自己的判据,减掉 `probes()` 里排在它前面的每一档。

    **计数、列表、分桶三处必须用同一份** —— 各用各的就是「顶上写着 403 条、
    五档加起来 405 条」,而人看不出多出来的两条是同一条内容被数了两遍
    (元规则:计数一律从库里查,口径只有一份)。
    """
    skip: list[str] = []
    for name, cond, args in probes():
        if name == lane_id:
            joined = ''.join(f' AND NOT ({earlier})' for earlier in skip)
            return f'({cond}){joined}', [*args, *_earlier_args(lane_id)]
        skip.append(cond)
    raise ValueError('未知的积压档位')


def _earlier_args(lane_id: str) -> list:
    out: list = []
    for name, _cond, args in probes():
        if name == lane_id:
            return out
        out.extend(args)
    return out


def counts(kinds=None) -> dict[str, dict[str, int]]:
    """`{kind: {lane_id: 条数}}`。**从库里查,不从展示文案反推**(元规则)。

    一档一条 `GROUP BY kind` 的聚合,而不是逐个来源发一遍:来源只有六类,可
    「这一条属不属于某条开着补抓的订阅」那一问对每一行各查一次,发六遍就是六倍。

    用的是**独占**判据(`exclusive_where`),所以各档相加**等于**顶上那个总数 ——
    原始判据之间是会重叠的,理由见 `probes`。
    """
    wanted = list(kinds or reg.KINDS)
    out: dict[str, dict[str, int]] = {kind: {} for kind in wanted}
    with reg.db() as c:
        for lane in LANES:
            cond, args = exclusive_where(lane.id)
            rows = c.execute(f'SELECT kind,count(*) FROM feed_items WHERE {cond}'
                             ' GROUP BY kind', args).fetchall()
            tally = {row[0]: row[1] for row in rows}
            for kind in wanted:
                out[kind][lane.id] = tally.get(kind, 0)
    return out


#: 一屏最多列多少条。全库积压轻松上千条,一次全交出去既慢又没人读得完。
PAGE = 40

#: 列表与详情共用的取数。**FROM 不起别名**:`where()` 里的判据写的是
#: `feed_items.` 全名(它要拼进 `registry.items` 那类不带别名的语句里),起了别名
#: 之后真名就解析不出来了。
_COLUMNS = """SELECT feed_items.key,kind,url,title,title_zh,published,fetched_at,
 content_rev,life,content_kind,exported_hash,content_hash,path,
 fulltext_at,fulltext_error,fulltext_tries,fulltext_hold,fulltext_url,
 translated_at,translate_error,translate_tries,lang,body_provenance,
 (SELECT group_concat(s.name,' / ') FROM subscription_items si
   JOIN subscriptions s ON s.id=si.subscription_id
   WHERE si.item_key=feed_items.key) AS subscriptions"""

#: 搁置那几列。**判据转手 `shelf.row_sql`**,这里不另写一遍 —— 只按 key 取的话,
#: 一条删掉重收的新内容会带着上一世的「暂缓 / 不再收录」标记出现在列表上
#: (审查第 1 轮 F07)。
_SHELF_COLUMNS = shelf.row_sql()

#: 排序:失败过的排在前面(和 `fetch_pending` 的取数顺序一致 —— 面板第一行正是
#: 下一轮最先被碰的那一条),同一批里新的在前,`key` 收尾定序(`fetched_at` 只
#: 精确到秒,一批抓回来的时间戳一模一样)。
_ORDER = (' ORDER BY fulltext_tries DESC, translate_tries DESC,'
          ' published DESC, fetched_at DESC, feed_items.key LIMIT ? OFFSET ?')


def _narrow(kind: str, query: str, columns: tuple[str, ...]) -> tuple[str, list]:
    """来源筛选 + 搜索那两段。`columns` 是这一次允许被搜的列(全写死在本模块里)。"""
    cond, params = '', []
    if kind:
        cond += ' AND kind=?'
        params.append(reg.check_kind(kind))
    if query:
        cond += ' AND (' + ' OR '.join(f'{name} LIKE ?' for name in columns) + ')'
        params.extend([f'%{query}%'] * len(columns))
    return cond, params


#: 搜索命中哪几列。**失败原因也要能搜**:人记得的常常是那半句报错,而不是标题。
_SEARCH = ('title', 'title_zh', 'url', 'fulltext_error', 'fulltext_hold', 'translate_error')


def rows(lane_id: str, kind: str = '', query: str = '',
         offset: int = 0, limit: int = PAGE) -> dict:
    """这一档的条目(带总数)。总数按**同一个 WHERE** 查,不按返回的行数推。"""
    cond, args = exclusive_where(lane_id)
    extra, more = _narrow(kind, query, _SEARCH)
    sql_where = f'{cond}{extra}'
    params = [*args, *more]
    with reg.db() as c:
        total = c.execute(f'SELECT count(*) FROM feed_items WHERE {sql_where}',
                          params).fetchone()[0]
        found = c.execute(f'{_COLUMNS} FROM feed_items WHERE {sql_where}{_ORDER}',
                          (*params, max(1, limit), max(0, offset))).fetchall()
    return {'total': total, 'items': [dict(row) for row in found]}


def shelved(kind: str = '', query: str = '', offset: int = 0, limit: int = PAGE) -> dict:
    """已搁置那一组。**暂缓和不再收录分别带回去,绝不合并**(状态契约)。"""
    extra, more = _narrow(kind, query, ('title', 'title_zh', 'url'))
    sql_where = f'{shelf.live_sql()}{extra}'
    with reg.db() as c:
        total = c.execute(f'SELECT count(*) FROM feed_items WHERE {sql_where}',
                          more).fetchone()[0]
        found = c.execute(
            f'{_COLUMNS}{_SHELF_COLUMNS} FROM feed_items WHERE {sql_where}'
            ' ORDER BY published DESC, fetched_at DESC, feed_items.key LIMIT ? OFFSET ?',
            (*more, max(1, limit), max(0, offset))).fetchall()
    return {'total': total, 'items': [dict(row) for row in found]}


def one(key: str) -> dict | None:
    """单条的全部字段(详情抽屉用)。搁置列一起带上,搁置着的也要打得开。"""
    with reg.db() as c:
        row = c.execute(f'{_COLUMNS}{_SHELF_COLUMNS} FROM feed_items'
                        ' WHERE feed_items.key=?', (key,)).fetchone()
    return dict(row) if row else None


def identities(keys) -> dict[str, tuple[str, int]]:
    """这几条此刻的生命周期身份:`{key: (life, content_rev)}`。

    给执行计划的凭证用(`kb.intake_diag.actions`):确认框弹出那一刻选集里是**这
    几世的这几条**,真要动手时再取一次比一比 —— 不比的话,一条在确认期间被清理
    删掉又从同一个链接收回来的内容会被当成同一条处理(它的 key 一个字都没变)。
    取不到的 key 不进结果,调用方据此也能看出「它已经不在了」。
    """
    wanted = list(dict.fromkeys(keys))
    if not wanted:
        return {}
    slots = ','.join('?' for _ in wanted)
    with reg.db() as c:
        rows = c.execute(f'SELECT key,life,content_rev FROM feed_items'
                         f' WHERE key IN ({slots})', wanted).fetchall()
    return {row['key']: (row['life'], row['content_rev']) for row in rows}


def split(keys, kind: str = '') -> dict[str, list[str]]:
    """把这几个 key 按**此刻真正所在的那一档**分桶。空串那一桶 = 已经不在积压里。

    **一次连接、每档一条 `IN` 查询。** 逐个 key 问的话每条都要新开一个
    `reg.db()`,而那一下连着建表、补列、种默认值和 YouTube 的初始化 —— 一次
    40 条的批量处理就是 40 遍。

    **分流认库里此刻的事实,不认前端传来的说法** —— 页面上那一份可能已经过时
    几秒了(另一轮同步刚把那一条补好),而处理出口是按档分的。

    `kind` 非空时**约束查询本身**:路由上那个 kind 过了合法性校验,不等于请求体
    里每个 key 都属于它(元规则:判据待在语句自己的 WHERE 里)。
    """
    wanted = list(dict.fromkeys(keys))
    if not wanted:
        return {}
    slots = ','.join('?' for _ in wanted)
    extra, kargs = (' AND kind=?', [reg.check_kind(kind)]) if kind else ('', [])
    left, out = set(wanted), {}
    with reg.db() as c:
        for name, _cond, _args in probes():
            if not left:
                break
            cond, cargs = (shelf.live_sql(), []) if name == ARCHIVED \
                else exclusive_where(name)
            rows = c.execute(
                f'SELECT feed_items.key FROM feed_items'
                f' WHERE feed_items.key IN ({slots}){extra} AND ({cond})',
                (*wanted, *kargs, *cargs)).fetchall()
            if hit := [row[0] for row in rows if row[0] in left]:
                out[name] = hit
                left.difference_update(hit)
    if left:
        out[''] = [key for key in wanted if key in left]
    return out
