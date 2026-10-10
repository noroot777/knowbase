# -*- coding: utf-8 -*-
"""日报的上报账本:「这一条被哪一份日报带走过」。

从 `kb.daily` 里拆出来**只因为行数**(那边到了 700 行上限)。这边不认识 vault
里的日报文件,也不认识取材口径 —— 它只管一张表和三件事:启用时一次性盖章、
一份日报带走了哪些、删一份日报把它们放回去。日报链(每一份覆盖哪一段)由
`kb.daily` 从各份日报的 frontmatter 读出来传进来,免得两个模块互相 import。

`STAMP` / `parse_ts` 也落在这里(账本自己要用),`kb.daily` 原样绑回它的模块
全局,所以 `daily.parse_ts` / `daily.STAMP` 这些既有写法照旧有效。
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from datetime import datetime, timedelta

from kb.config import DATA_DIR

STAMP = '%Y-%m-%d %H:%M:%S'
# 启用账本那一刻盖的那批章用的 day 值。真实日期一律是 YYYY-MM-DD,所以它不会和
# 任何一份日报撞上,也不会被 `release()` 误删。
SEED_DAY = '#seed'
EPOCH_KEY = 'carry_epoch'
# 落盘与记账、放回与删文件各自成段,两段之间要整体互斥(见 `commit_lock`)。
LOCK_NAME = 'daily-report.lock'

# 「这一条还没有被**别的**日报带走过」。`day` 是正在写的那一份自己的日期:重新
# 生成同一天的日报时,它上一版带走的那些必须重新可选 —— 同名文件是整份覆盖的,
# 排除掉自己的章就等于新版本把早上已经报过的内容整段丢掉。
UNCARRIED = (' AND NOT EXISTS(SELECT 1 FROM daily_carried c'
             ' WHERE c.path=d.path AND c.day<>?)')


def parse_ts(value) -> datetime | None:
    """把库里的时间字符串解析成本地朴素时间;认不出来给 None。

    `published` 在库里实测有四种写法:`2026-09-28 17:08`(2.1 万篇)、
    `2026-06-14T11:29:37+08:00`(1178 篇)、`2026-09-29`(92 篇)、空(1 篇)。
    **所以不能拿字符串直接比大小**:同一天里 `T`(0x54)排在空格(0x20)后面,
    `2026-09-29T07:00:00+08:00` 字典序上大于 `2026-09-29 08:00`,按字符串筛
    窗口会把早上七点发的内容算进八点才开始的窗口。带时区的先换算到本地,
    只有日期的按当天 00:00 —— 那是这种写法唯一能确定的下界。
    """
    s = str(value or '').strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def day_key(day: str | None, until: datetime) -> str:
    return day or until.date().isoformat()


@contextmanager
def commit_lock():
    """圈住「落盘 + 记账」和「放回 + 删文件」这两段,让它们整体互斥。

    两段各自内部的顺序已经各管一个方向(章盖不上只是多报一遍;放回失败时文件
    还在也只是多报一遍),但**那只保证各自单次操作的失败方向,管不住两段交错**:
    生成写完文件、还没记账的那一瞬间,另一边的删除跑完整段(放回 + 删文件),
    接着生成把章补上 —— 留下一批"记着已上报、却没有任何日报文件"的条目,它们
    从此两头落空(自由工作流审查第 1 轮用线程屏障实测到这个顺序)。阅读区那颗
    删除按钮在生成期间本来就能点,所以这不是理论上的交错。

    **不借用 `kb.maintenance` 那把维护锁**:那一把在一轮大索引里会被攥住几分
    钟,而这里要锁的是模型已经出完稿之后的最后两步 —— 等在那上面等于让花过钱
    的那一轮卡在收尾。这把锁只圈毫秒级的两段,自己一个 lockfile,不和任何别的
    锁嵌套,所以不会抱死;flock 随进程退出自动释放,持锁者崩了也堵不死别人。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = (DATA_DIR / LOCK_NAME).open('a+')
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fh.close()                       # 关闭即释放 flock


def ensure_ledger(conn, chain_of, fallback_days: int, now: datetime) -> str:
    """账本首次启用时一次性盖章,返回那条分界线(链头的截止时刻)。幂等。

    `chain_of` 是一个**取日报链的函数**(每一份一条 (窗口起, 窗口止, 日期),按截止
    时刻升序),只有真要盖章那一次才调用它 —— 取链要把 vault 里每一份日报的
    frontmatter 都读一遍,而这个函数坐在面板每一跳那条路上(`daily.pending`),
    常态只该花一句 SELECT。

    不盖的话账本一启用,库里两万多篇历史文章全都算"没上报过",第一份日报的补录
    候选是上千条、要几十份日报才排得空 —— 而那些内容在旧实现下早就永久落选了,
    翻出来不是修复、是倒灌。

    **盖不盖,判据是「它到得比最后一份日报还晚吗」**(`indexed_at > 链头的
    window_until`),不是只看发布时间。这一道承重:账本是**懒初始化**的,第一次
    真正跑到它可能已经是升级之后好几天 —— 用户先点「重试失败项」补抓一批、再
    打开日报面板,那一批的发布时间多半早于链头,只看发布时间就会把它们一律当成
    历史存量盖掉,于是**正好是这次要救的那批内容被永久写死**(自由工作流审查
    第 1 轮实测:补抓一篇发布于前天的文章,首次初始化后 pending=0、generate 抛
    「没有未上报过的新内容」,之后每一天也都选不到)。同一条路还覆盖定时那条
    入口 ——「先同步再生成」是同步**跑完**才进 generate(),初始化必然晚于补抓。
    `indexed_at` 只会往前走,所以"到得晚"证得死;反过来老文章重新导出也会刷新
    它,那只是多报一遍,方向是安全的那一边。

    **盖章要落到「当初是哪一份带走的」那一天上,不能一律 `#seed`。** 同一天重新
    生成时 `UNCARRIED` 放行的是**这一份自己**盖的章;统一记成 `#seed` 的话,把
    区间改回某份旧日报再重新生成,它原有的报道会被整份覆盖掉、一条不剩(同一轮
    审查实测:甲文在原文件里,覆盖后只剩新补抓的乙文)。所以按窗口归属:落在某
    一份窗口里的记那一份,比链起点还早的(以及没有发布时间的)记 `#seed`,
    **发布在链头之后的不盖**(它属于下一份日报的要点桶),**落在链条缺口里的也
    不盖**(那一份已经被删了,它本就该重新回到队列)。

    一份日报都没有时没有"链头"可言:分界线取 `fallback_days` 天前(那正是第一份
    日报的窗口起点),到达分界取"此刻"—— 库里现有的一律算存量。那时也确实没有
    任何证据说明哪一条被报道过,而让头一份日报去回顾整个存档只会是一份噪音。
    """
    row = conn.execute('SELECT value FROM daily_state WHERE key=?',
                       (EPOCH_KEY,)).fetchone()
    if row:
        return str(row['value'])
    chain = chain_of()
    head = chain[-1][1] if chain else now
    start = chain[0][0] if chain else now - timedelta(days=max(1, fallback_days))
    stamp = head.strftime(STAMP)

    # 先在锁外把章算好(两万行读一遍、纯内存分类),锁里只落盘。
    # `indexed_at` 库里只有 `YYYY-MM-DD HH:MM:SS` 一种写法,可以直接比字符串;
    # `published` 有四种写法,必须 parse_ts 之后再比(见那个函数的说明)。
    marked, at = [], datetime.now().strftime(STAMP)
    for row in conn.execute('SELECT path,published,indexed_at FROM documents'):
        if str(row['indexed_at'] or '') > stamp:
            continue                      # 到得比链头还晚 ⇒ 不可能被哪一份带走过
        when = parse_ts(row['published'])
        if when is None or when <= start:
            marked.append((row['path'], SEED_DAY, at))
            continue
        if when > head:
            # 发布在链头之后 ⇒ 属于下一份日报的要点桶。**这一档是冗余带**
            # (定点注入实测红 0 条):链条最后一个窗口就止于 head,所以这种
            # 条目在下面那句 next(...) 里本来也找不到归属、落 None 不盖章。
            # 留着是构造上的理由 —— 这一档该被显式处理过,而不是靠读代码的人
            # 去推 next() 的默认值。
            continue
        day = next((d for s, u, d in chain if s < when <= u), None)
        if day:
            marked.append((row['path'], day, at))

    if conn.in_transaction:
        conn.commit()
    conn.execute('BEGIN IMMEDIATE')
    try:
        # 锁里再查一次:两个进程同时首访时,后一个在这里就看得见前一个盖的章。
        row = conn.execute('SELECT value FROM daily_state WHERE key=?',
                           (EPOCH_KEY,)).fetchone()
        if row:
            conn.commit()
            return str(row['value'])
        conn.executemany('INSERT OR IGNORE INTO daily_carried(path,day,carried_at)'
                         ' VALUES(?,?,?)', marked)
        conn.execute('INSERT INTO daily_state(key,value) VALUES(?,?)',
                     (EPOCH_KEY, stamp))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return stamp


def mark_carried(conn, day: str, paths) -> int:
    """记下这一份日报带走了哪些条目;重新生成同一天时整批替换。返回条数。

    **只能在日报文件真的落盘之后调,而且两步要一起待在 `commit_lock()` 里。**
    反过来的话,模型钱花掉、文件没写成,这批素材已经被盖成"报过了" —— 下一份
    日报再也选不到它们,不可恢复。顺序不许反,锁也不能省(见 `commit_lock`)。
    """
    if day == SEED_DAY:
        raise ValueError('不能用启用时那批章的 day 值记一份日报')
    rows = [(p, day, datetime.now().strftime(STAMP))
            for p in dict.fromkeys(paths) if p]
    if conn.in_transaction:
        conn.commit()
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('DELETE FROM daily_carried WHERE day=?', (day,))
        conn.executemany('INSERT OR IGNORE INTO daily_carried(path,day,carried_at)'
                         ' VALUES(?,?,?)', rows)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return len(rows)


def release(conn, day: str) -> int:
    """删掉一份日报 ⇒ 它带走的那些条目回到队列,下一份会重新覆盖它们。"""
    if day == SEED_DAY:
        raise ValueError('启用时那批章不随日报删除')
    cur = conn.execute('DELETE FROM daily_carried WHERE day=?', (day,))
    conn.commit()
    return cur.rowcount
