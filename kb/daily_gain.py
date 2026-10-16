# -*- coding: utf-8 -*-
"""面板上那个「再出一份日报能多带进来多少」,外加它赖以成立的素材版本指纹。

从 `kb.daily` 拆出来**只因为行数**(那边到了 700 行上限)。这边回答的是同一个
问题的两半:**取材那一刻**送进模型的是哪一版(`material_sig`,落盘时记进账本),
和**此刻**再生成一次会不会多出东西(`pending` / `_will_add`)。两半必须待在一起
—— 比对那一侧算指纹用的就是写进去时的同一个函数,分开就是等着各漂各的。

取材口径本身不在这里:`_select` / `_enrich` 还在 `kb.daily`,这边**现取**
(`from kb import daily` 在函数里),所以 `patch.object(daily, 'MAX_ITEMS', …)`
这类回归照旧打得中。

硬约束见 `docs/decisions/daily.md`。
"""

from __future__ import annotations

import hashlib
from datetime import datetime


def material_sig(doc: dict) -> str:
    """这一条**送进模型的那一份**的指纹(`_enrich` 出来的那个字典)。

    账本记下它(`daily_carried.material_sig`),面板据它回答「再生成一次会不会多
    出东西」。只按路径盖章答不了这个问题:同一篇文章的正文后来补全了、摘要刚提取
    出来、实体/主题变了,下一次送进模型的就是另一份内容,而"同一篇"会被当成"没有
    变化" —— 按钮跟着藏掉,人再没有入口把这些更新报出来。

    **覆盖的就是进 prompt 和素材清单的那几样**(`_material` / `_page`),不多不少:
    多算一样(比如 `indexed_at`)会让一次无关的重索引也报成新内容,少算一样就是把
    真变化说成没变。改那两处用到的字段时,这里要跟着改。
    """
    parts = [str(doc.get(k) or '')
             for k in ('source', 'account', 'title', 'published', 'url', 'excerpt')]
    parts.append('\x1e'.join((doc.get('entities') or [])[:6]))
    parts.append('\x1e'.join((doc.get('topics') or [])[:4]))
    parts.append('1' if doc.get('late') else '0')   # 两个桶的写法不同,见 `_material`
    return hashlib.blake2s('\x1f'.join(parts).encode('utf-8'),
                           digest_size=12).hexdigest()


def _will_add(conn, row, when: datetime | None, late: bool) -> bool | None:
    """再生成一次,这一条会不会给模型多带一份它没见过的内容:会 / 不会 / **不知道**。

    三档,**第三档是「不知道」,不是「不会」**:

    ① 一份日报都没进过(`mine_at` 空)—— 本来就没报过,会。
    ② 今天那一份带走过,账本里记着当时那一版的指纹 —— 和现在这一版比,答得出来。
    ③ 带走过、但账本里**没有**指纹(这一列是后加的;`seed_locked` 整表重算出来
       的那批也没有)—— **一个字都答不上来,返回 `None`**。

    第三档一度回落到「带走之后有没有重新索引过」(`indexed_at > carried_at`),
    **那是错的**:真正改变素材的那条路是提取(`extract.save_extraction` 写
    `summary` / 实体 / 主题 / `extracted_at`),它**不碰 `indexed_at`** —— 于是
    "索引时刻没动"被当成了"送进模型的内容没变",而那正是日报出完、提取才补上
    摘要的常见顺序(审查实测 12 组:旧账本行 + 真实提取,模型输入 6 组全变,
    `indexed_at` 6 组全没动,`gain` 却全是 0,按钮跟着藏掉)。

    一律算"会"也不行:升级那一刻今天带走的每一条都会被报成新内容,屏幕上凭空
    冒出一个数。**「不知道」就说不知道** —— 由 `pending` 把整个 `gain` 报成
    `None`,页面写「新内容读不出来」并把按钮留着(口径同 `pending: null`)。
    这一档是自愈的:重新生成一次今天那份,所有行就都有指纹了;到明天它们被
    `UNCARRIED` 整个排除。
    """
    if not row['mine_at']:
        return True
    if row['mine_sig']:
        from kb import daily
        return row['mine_sig'] != material_sig(daily._enrich(conn, row, when, late))
    return None


def _on_disk(day: str) -> bool | None:
    """那一天的日报文件**此刻**在不在。读不出来时答 `None`(不知道)。

    和 `gain` 同一次调用给出来,因为界面要用它俩**一起**决定"能不能把按钮藏
    掉" —— 账本盖的章和 vault 里那个文件是两头东西:在 Obsidian 里把今天那份
    移走(没走界面那条删除)时章还在,`gain` 照样是 0。界面原来问的是自己手里
    那份日报列表缓存,而空闲轮询只刷新这个接口、不刷新列表,于是"文件已经没了"
    这件事要等整页刷新才看得见,按钮一直藏着(审查实测)。
    """
    from kb import daily
    try:
        return daily._path_for(day).is_file()
    except (OSError, ValueError):
        return None


def pending(first_days: int | None = None,
            now: datetime | None = None) -> dict:
    """面板右上角那两个数,零模型调用。

    - `items` / `late` / 两者之和:**队列里一共还积着多少一份日报都没进过的**。
      给 tooltip 用 —— 它答的是"攒了多少",而不是"点下去会得到什么":名额
      (`MAX_ITEMS` / `LATE_ITEMS`)每一轮只带得走固定条数,生产库实测积压 1097 条
      而重新生成一次只多出 11 条。把这个数写到按钮边上,用户会照着它花一次模型钱。
    - `gain`:**再生成一次会给模型多带几条它没见过的内容** —— 没进过任何日报的,
      **加上进过、但当时带走的是另一个版本的**(正文后来补全了、摘要刚提取出来、
      实体/主题变了,判据在 `_will_add`)。这个数才上按钮(`daily_chip.js`)。
      **有一条答不上来(账本里没记当时那一版),整个 `gain` 就是 `None`** ——
      不是 0:0 是"确定没有",而这时我们确定不了,页面据此写「新内容读不出来」
      并把按钮留着。取材口径必须和 `collect` 逐字一致(同一个 `_select`、同样先截
      `MAX_ITEMS` / `LATE_ITEMS` 再数),两处各判一遍必然各漂各的。
    - `day` / `has_report`:这一次会写到哪一天、**那一天的日报文件此刻在不在**。
      藏按钮要两个条件一起成立(`gain == 0` **而且** `has_report is True`),所以
      这两件事必须**同一次读、同一个回包**给出去 —— 分开问就会出现"章还在、文件
      已经被移走"而界面按旧缓存把唯一的重建入口藏掉(见 `_on_disk`)。

    **这几个数来自同一次读。** `_select` 传的是**今天**(和 `collect` 同一个
    `day_key`):今天那一份上一版带走的内容必须重新算进候选,否则 `gain` 答的是
    另一个问题。"今天自己盖的章、盖的是哪一版"由 `mine_at` / `mine_sig` 两列在同
    一条 SQL 里给出,**指纹也在同一个快照里算** —— **不许再补一次「今天那份带走了
    谁」的查询去扣减或比对**:候选和扣除项来自两个读取时刻,中间别人提交一次,算出
    来的是两个版本拼起来的数。

    **误差方向只许倒向「多报」。** 账本是**允许**出现「日报文件写好了、记账没写
    上」的(那一档故意倒向多报一遍,见 `daily_carry`),那时今天带走的那几条看着
    像没进过日报 —— `gain` 跟着偏大,按钮多显示一次(代价:用户白花一次模型钱,
    看得见)。反过来偏小就是把按钮藏掉、而确实有新内容(静默丢掉一次出报机会),
    所以盖章一律排在落盘**之后**那条顺序在这里也承重。版本那一档没有"更宽的判据"
    可退 —— 账本里没记指纹时**一个字都答不上来**,那就整份报「不知道」
    (`gain=None`):按钮照样留着,只是旁边那句话从「有 N 条」换成「读不出来」。
    """
    from kb import daily, store
    # 缺省值现取:`FIRST_DAYS` 在 `kb.daily` 那边,写成形参默认值就得在模块加载
    # 时反向 import 它(循环),而且也挡住了回归改它。
    days = first_days or daily.FIRST_DAYS
    since, until = daily.next_window(days, now)
    conn = store.connect()
    try:
        daily._ensure_ledger(conn, days, until)     # 这一步可能要写,排在读事务外面
        # 两个桶是**两条 SELECT**,而 legacy `isolation_level` 下 SELECT 自己不开
        # 事务 —— 中间夹进别人一次 `mark_carried` 提交的话,折出来的是两个版本拼
        # 起来的数(上一版正是这么报出过一个凭空的新增:实测线程屏障下 `0 → 1 → 0`,
        # 期间一篇新文章都没加)。WAL 下一次 `BEGIN` 就是一份只读快照,拦不住写者。
        # 先收干净上一步可能留下的事务(同 `mark_carried` 那道):嵌套 BEGIN 会抛,
        # 而这条路每 15 秒走一次,抛一次面板上那两个数就整段没了。
        if conn.in_transaction:
            conn.commit()
        conn.execute('BEGIN')
        try:
            picked, late = daily._select(conn, since, until, daily._day_key(None, until))
            # 「队列里还积着多少」只问一件事:**一份日报都没进过**。它和 gain 不是
            # 同一个问题 —— 后者还要认出"带走过、但带走的是另一个版本",所以两处
            # 各用各的判据,别图省事合成一个。
            waiting = [sum(1 for _, row in rows if not row['mine_at'])
                       for rows in (picked, late)]
            # 指纹在这同一个快照里算(`_will_add` 要现读这一条的实体/主题/摘要)。
            said = [_will_add(conn, row, w, False)
                    for w, row in picked[:daily.MAX_ITEMS]]
            said += [_will_add(conn, row, w, True)
                     for w, row in late[:daily.LATE_ITEMS]]
            # **一条「不知道」就让整个数变成「不知道」。** 把它当 0 加进去等于
            # 拿一条答不上来的充成"确定没变",而这个数唯一的用处就是决定要不要
            # 把按钮藏掉。
            gain = None if any(v is None for v in said) else sum(said)
        finally:
            conn.rollback()                         # 只读,没什么要提交的
    finally:
        conn.close()

    day = daily._day_key(None, until)
    return {'since': since, 'until': until,
            'items': waiting[0], 'late': waiting[1], 'gain': gain,
            'day': day, 'has_report': _on_disk(day)}
