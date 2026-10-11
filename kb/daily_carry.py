# -*- coding: utf-8 -*-
"""日报的上报账本:「这一条被哪一份日报带走过」。

从 `kb.daily` 里拆出来**只因为行数**(那边到了 700 行上限)。这边不认识 vault
里的日报文件,也不认识取材口径 —— 它只管一张表和三件事:启用时一次性盖章、
一份日报带走了哪些、删一份日报把它们放回去。已有的那几份日报(各自的窗口和
**实际带走的素材清单**)由 `kb.daily` 从文件里读出来传进来,免得两个模块互相
import。

`STAMP` / `parse_ts` 也落在这里(账本自己要用),`kb.daily` 原样绑回它的模块
全局,所以 `daily.parse_ts` / `daily.STAMP` 这些既有写法照旧有效。
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from kb.config import DATA_DIR

STAMP = '%Y-%m-%d %H:%M:%S'
# 启用账本那一刻盖的那批章用的 day 值。真实日期一律是 YYYY-MM-DD,所以它不会和
# 任何一份日报撞上,也不会被 `release()` 误删。
SEED_DAY = '#seed'
EPOCH_KEY = 'carry_epoch'
# 盖章算法的版本。**改了算法必须跟着加一**:账本是懒初始化的,已经盖过章的库
# 会直接走快路径返回,于是修好的归属只对"从空库开始"的人成立,而最需要修的恰好
# 是已经按旧算法盖错过的那些库(自由工作流审查第 2 轮点出这一条)。版本不一致时
# 整张表按当前算法重算 —— 证据(每一份日报自己的素材清单)一直在文件里,所以
# 重算得到的归属和当初 `mark_carried` 记下的是同一份。
SEED_REV_KEY = 'carry_seed_rev'
SEED_REV = '2'
# 落盘与记账、放回与删文件、启用时盖章,三段之间要整体互斥(见 `commit_lock`)。
LOCK_NAME = 'daily-report.lock'

# 「这一条还没有被**别的**日报带走过」。`day` 是正在写的那一份自己的日期:重新
# 生成同一天的日报时,它上一版带走的那些必须重新可选 —— 同名文件是整份覆盖的,
# 排除掉自己的章就等于新版本把早上已经报过的内容整段丢掉。
UNCARRIED = (' AND NOT EXISTS(SELECT 1 FROM daily_carried c'
             ' WHERE c.path=d.path AND c.day<>?)')


@dataclass(frozen=True)
class PastReport:
    """一份已经生成过的日报:窗口、日期,和它**实际带走的**那些素材。

    `keys` 是素材身份的集合,由 `kb.daily` 从那一份自己的「素材清单」里读回来
    (口径是 `daily._doc_key`,盖章和读清单两侧共用同一个函数)。读不出清单的
    那一份交一个空集合 —— 它对归属一个字的证据都提供不了,不是"它什么都没带走"。
    """
    since: datetime
    until: datetime
    day: str
    keys: frozenset


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


def _state(conn) -> dict:
    return {r['key']: str(r['value'])
            for r in conn.execute('SELECT key,value FROM daily_state')}


def _seeded(state: dict) -> str:
    """已经按**当前**算法盖过章了吗 —— 是就给那条分界线,否则空串。"""
    return state.get(EPOCH_KEY, '') if state.get(SEED_REV_KEY) == SEED_REV else ''


def ensure_ledger(conn, chain_of, key_of, fallback_days: int, now: datetime) -> str:
    """账本首次启用时一次性盖章,返回那条分界线。幂等。本体见 `seed_locked`。

    常态只花一句 SELECT:盖过章就直接返回,连锁都不取 —— 这个函数坐在面板每一跳
    那条路上(`daily.pending`)。
    """
    done = _seeded(_state(conn))
    return done or _lock_and_seed(conn, chain_of, key_of, fallback_days, now)


def _lock_and_seed(conn, chain_of, key_of, fallback_days, now) -> str:
    with commit_lock():
        return seed_locked(conn, chain_of, key_of, fallback_days, now)


def seed_locked(conn, chain_of, key_of, fallback_days: int, now: datetime) -> str:
    """盖章本体。**调用方必须已经持有 `commit_lock()`**(别在里面再取一次)。

    `chain_of` 是一个**取已有日报的函数**(每一份一条 `PastReport`,按截止时刻
    升序),只有真要盖章那一次才调用它 —— 取它要把 vault 里每一份日报整份读一遍。
    `key_of(url, title)` 是素材身份的口径,和各份日报的素材清单那一侧共用。

    不盖的话账本一启用,库里两万多篇历史文章全都算"没上报过",第一份日报的补录
    候选是上千条、要几十份日报才排得空 —— 而那些内容在旧实现下早就永久落选了,
    翻出来不是修复、是倒灌。

    **归属只认「那一份自己的素材清单」,不按发布时间推断。** 清单就在每一份日报
    的文件里(`daily._page` 写的),而推断在两头各错一次,两处都不可恢复:
    ①**补录**的发布时间本来就早于它那一份的窗口,按窗口推断会把它当成"链条之前
    的存量"记成 `#seed`,于是重新生成那一份时它一条都选不回来、已经交付的内容
    被整份覆盖掉;②窗口里**当时装不下**的那些反倒被记成"报过了",从此任何一份
    日报都选不到 —— 那正是这次要修的那条缝,按窗口推断等于原地复发。清单里认不出
    的条目一律不盖章:方向按元规则取宽的那一边,放回队列最多多报一遍,盖错了是
    永久写死。

    **盖不盖,另一道判据是「它到得比分界线还晚吗」**(`indexed_at > 分界线)。这
    一道承重:账本是**懒初始化**的,第一次真正跑到它可能已经是升级之后好几天 ——
    用户先点「重试失败项」补抓一批、再打开日报面板,那一批的发布时间多半早于链头,
    只看发布时间就会把它们一律当成历史存量盖掉,于是**正好是这次要救的那批内容被
    永久写死**。`indexed_at` 只会往前走,所以"到得晚"证得死;反过来老文章重新导出
    也会刷新它,那只是多报一遍,方向是安全的那一边。**清单上有它就不问这一道** ——
    实际带走过是正面证据,比任何推断都硬。

    **分界线一旦钉下就不再往前挪**(`prior or …`)。算法升版重算时如果改用当下的
    链头,那些"在旧分界之后到达、还排在队列里等补录名额"的文章就会突然变成"到得
    不晚",再被发布时间判成历史存量、永久写死 —— 实测:61 篇补抓回来的文章里 60
    篇被一份日报带走,剩下 1 篇在升级后彻底消失(自由工作流审查第 3 轮反例)。
    重算要修的只是**归属**,分界线那一维没有任何理由跟着动。

    一份日报都没有时没有"链头"可言:分界线取 `fallback_days` 天前(那正是第一份
    日报的窗口起点),到达分界取"此刻"—— 库里现有的一律算存量。那时也确实没有
    任何证据说明哪一条被报道过,而让头一份日报去回顾整个存档只会是一份噪音。
    """
    # 取锁之后先落盘:下面那次状态读要看得见别人刚提交的,后面还要 BEGIN
    # IMMEDIATE(攥着没提交的写事务去等锁会把持锁者卡在它自己的写上)。
    if conn.in_transaction:
        conn.commit()
    # 锁内再问一次:两个进程同时首访时,后一个在这里就看得见前一个盖的章 ——
    # 不问的话它会把前一个盖完、之后又真生成过日报的那些章一起 DELETE 掉。
    state = _state(conn)
    done = _seeded(state)
    if done:
        return done
    chain = chain_of()
    head = chain[-1].until if chain else now
    start = chain[0].since if chain else now - timedelta(days=max(1, fallback_days))
    stamp = state.get(EPOCH_KEY, '') or head.strftime(STAMP)
    # 同一条素材出现在两份清单里时按**最后**带走它的那一份记(链条按截止时刻
    # 升序,所以后面的盖掉前面的)。记两份也救不了更早那一份的重新生成
    # (`UNCARRIED` 排除的是"别的日报盖的章"),白多一行。
    owner = {k: c.day for c in chain for k in c.keys}

    # 先在锁内把章算好(两万行读一遍、纯内存分类),再一个事务落盘。
    # `indexed_at` 库里只有 `YYYY-MM-DD HH:MM:SS` 一种写法,可以直接比字符串;
    # `published` 有四种写法,必须 parse_ts 之后再比(见那个函数的说明)。
    marked, at = [], datetime.now().strftime(STAMP)
    for row in conn.execute('SELECT path,url,title,published,indexed_at'
                            ' FROM documents'):
        day = owner.get(key_of(row['url'], row['title']))
        if day:
            marked.append((row['path'], day, at))
            continue
        if str(row['indexed_at'] or '') > stamp:
            continue                  # 到得比分界线还晚 ⇒ 不可能被哪一份带走过
        when = parse_ts(row['published'])
        if when is None or when <= start:
            marked.append((row['path'], SEED_DAY, at))
        # 剩下这一档不盖:到得不晚、发布在链条覆盖的那段里、却不在任何一份的
        # 清单上 —— 那就是当时名额装不下、被静默丢掉的那批,留在队列里等下一
        # 份接着带。落在链条缺口里(那一份已经被删了)的也走这里。

    conn.execute('BEGIN IMMEDIATE')
    try:
        # 版本升级那一次整张表重算。清掉的那些章全都能从日报文件里再读回来
        # (包括启用之后真生成的那几份 —— `mark_carried` 记的就是清单里那批),
        # 读不回来的按不盖章办,方向同上:多报一遍,不写死。
        conn.execute('DELETE FROM daily_carried')
        conn.executemany('INSERT OR IGNORE INTO daily_carried(path,day,carried_at)'
                         ' VALUES(?,?,?)', marked)
        conn.executemany('INSERT OR REPLACE INTO daily_state(key,value)'
                         ' VALUES(?,?)',
                         ((EPOCH_KEY, stamp), (SEED_REV_KEY, SEED_REV)))
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
