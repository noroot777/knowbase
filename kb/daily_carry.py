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
from typing import NamedTuple

from kb.config import DATA_DIR
# 叶子模块(只认字符串),直接取口径不会绕回 `kb.daily`
from kb.daily_page import flat as page_flat

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
# 版本 2 = 归属只认素材清单那一版;3 = 清单解析顶住了「裸方括号开头的无链接标题」
# 和「标题末尾的成对反斜杠」两种真实写法;4 = 清单起点按抬头声明的条数挑,不再被
# 标题里那几个字带偏;5 = 没有 url 的那条归属匹配要求标题在库里独一份(同名的两篇
# 原来共享一个身份,新那篇被旧日报抢走归属、永久漏报);6 = 那条无 url 的匹配
# 另加「到达不晚于分界线」和「发布不晚于那一份的截止」两道 —— 旧那篇被清理之后新
# 来的同名文章在库里成了独一份,只凭"标题唯一"又会被旧日报抢走归属;7 = 那两道
# 改成问**所匹配的那一份自己的**时刻(生成时刻 + 窗口截止),不再拿整条链的分界
# 当证据 —— 分界只回答"账本启用之后才到的",答不了"那一份生成时它在不在库里";
# 8 = 上界换成**取材快照**(`collected`)而不是出稿完成的 `generated`(差着一次模型
# 请求,那中间入库的文章一个都没被这一份看见过),另加第四道:清单那一行记的
# 「来源·账号」和这篇文档明确冲突时一律拒 —— 前三道都只能排除,这一道是正面证据。
# **解析器也是盖章算法的一部分** —— 版本不跟着动的话,前一版真实盖错过的那些库
# 命中快路径,修好的解析器一次都跑不到。**上面那条按名单钉住的回归只覆盖解析器那
# 一半**,改这个函数自己的分类判据时要自己记得把版本加一。
SEED_REV = '8'
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

    `taken` 是**取材快照**时刻(frontmatter 的 `collected`,秒精度),`made` 是
    **出稿完成**时刻(`generated`,只到分钟)—— 两件事,差着一次模型请求,判据要的
    是前者(见 `latest_pickup`)。`credits` 是清单里那些无链接条目各自记下的
    「来源·账号」,`_match_holds` 第四道拿它做唯一的**正面**身份证据。

    `rows` 是那份清单一条一条的**原文**(归一过),`text` 是整份正文(同样归一
    过)。解析不出身份的条目靠这两样兜一道:在这一份上露过面就不许判成"链条
    之前的历史存量"(理由见 `daily_page.material_rows`)。**两样分开取**,
    `text` 不经过任何切分 —— 起点选错时切出来的东西和归属一起错,兜底再共用它
    就什么都拦不住了。
    """
    since: datetime
    until: datetime
    day: str
    keys: frozenset
    rows: tuple = ()
    text: str = ''
    made: str = ''
    taken: str = ''
    credits: tuple = ()

    def latest_pickup(self) -> str:
        """这一份最晚可能带走东西的那一刻(拿来和 `indexed_at` 比字符串)。

        三档,按证据强弱取:

        ①**取材快照 `collected`** —— 这一份的输入定下来的那一刻,秒精度。**只有
          它能证明"那篇文章当时在不在库里"**:出稿要先读素材、再等模型跑几十秒到
          几分钟、最后才落盘,所以 `generated` 记的是**出稿完成**,拿它当上界会把
          那中间入库的文章一并算进来 —— 审查实测:14:07:16 模型已拿到输入(里面
          没有 B),B 在那之后入库,14:08 出稿写下 `generated: 14:08`;清单里只有
          A,而升级重算把 B 盖成那一份的已报道素材,待报道 1 → 0,当天生成直接答
          「没有未上报过的新内容」。
        ②**出稿完成 `generated`** —— 老日报只有这一行。它比取材快照晚,所以那一段
          窗口里的漏报风险仍在(由第四道「来源·账号」兜住大半);**精度只到分钟,
          字符串比较等于按那一分钟的开头算**,于是同一分钟入库、确实被带走过的
          文章会判成"到得太晚"、归属放回 —— 多报一遍,是安全的那一边,知情保留。
        ③**窗口截止 `until`** —— 连 `generated` 都读不出时的兜底,最严。

        不能反过来优先 `until`:自定义历史区间的 until 可能比真正取材早好几个月
        (覆盖 2026-01-01→01-02 的那一份是今天生成的),而它确实带走了今天入库的
        补录 —— 一律拿 until 当上界会把那条合法归属判成"到得太晚"。
        """
        return self.taken or self.made or self.until.strftime(STAMP)

    def credit_of(self, key: str) -> str:
        """清单里那一条记下的「来源·账号」(那一份没记下就空串)。"""
        return dict(self.credits).get(key, '')


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


def ensure_ledger(conn, chain_of, key_of, credit_of,
                  fallback_days: int, now: datetime) -> str:
    """账本首次启用时一次性盖章,返回那条分界线。幂等。本体见 `seed_locked`。

    常态只花一句 SELECT:盖过章就直接返回,连锁都不取 —— 这个函数坐在面板每一跳
    那条路上(`daily.pending`)。
    """
    done = _seeded(_state(conn))
    return done or _lock_and_seed(conn, chain_of, key_of, credit_of,
                                  fallback_days, now)


def _lock_and_seed(conn, chain_of, key_of, credit_of, fallback_days, now) -> str:
    with commit_lock():
        return seed_locked(conn, chain_of, key_of, credit_of, fallback_days, now)


def seed_locked(conn, chain_of, key_of, credit_of,
                fallback_days: int, now: datetime) -> str:
    """盖章本体。**调用方必须已经持有 `commit_lock()`**(别在里面再取一次)。

    `chain_of` 是一个**取已有日报的函数**(每一份一条 `PastReport`,按截止时刻
    升序),只有真要盖章那一次才调用它 —— 取它要把 vault 里每一份日报整份读一遍。
    `key_of(url, title)` 是素材身份的口径,和各份日报的素材清单那一侧共用;
    `credit_of(source, account)` 拼出这篇文档在清单里该写的「来源·账号」,口径
    同样和写日报那一侧共用(`daily._doc_credit` / `_page` 的收尾)。

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
    # 记的是**那一份本身**:没有 url 的那条匹配要逐项问它自己的窗口和生成时刻
    # (见 `_match_holds`),拿整条链的分界去问是另一个问题的答案。
    owner = {k: c for c in chain for k in c.keys}

    # 先在锁内把章算好(两万行读一遍、纯内存分类),再一个事务落盘。
    # `indexed_at` 库里只有 `YYYY-MM-DD HH:MM:SS` 一种写法,可以直接比字符串;
    # `published` 有四种写法,必须 parse_ts 之后再比(见那个函数的说明)。
    listed = _listing(chain)
    # 库里独一份的标题。同稿副本不许靠标题兜底(见 `_on_a_list`)
    seen_titles: dict[str, int] = {}
    for row in conn.execute('SELECT title FROM documents'):
        key = page_flat(row['title'])
        seen_titles[key] = seen_titles.get(key, 0) + 1
    once = {k for k, n in seen_titles.items() if n == 1}
    marked, at = [], datetime.now().strftime(STAMP)
    for row in conn.execute('SELECT path,url,title,source,account,published,'
                            'indexed_at FROM documents'):
        url = str(row['url'] or '').strip()
        when = parse_ts(row['published'])
        doc = Candidate(key_of(row['url'], row['title']), row['title'], when,
                        str(row['indexed_at'] or ''),
                        credit_of(row['source'], row['account']))
        past = owner.get(doc.key)
        if past and past.day:      # 盖章要落到某一天上,没有日期的那一份不作数
            # **没有 url 的那条匹配不是「实际带走过」那种硬证据**,要过
            # `_match_holds` 的四道(理由和各自的射程写在那个函数里)。证据不够就
            # **一个章都不盖**:既不记这一份的归属,也不往下走到 `#seed` —— 它在
            # 某份清单上露过面,判"链条之前的历史存量"同样是永久写死。留在队列里
            # 最多多报一遍,看得见、补得回。
            if url or _match_holds(past, doc, once):
                marked.append((row['path'], past.day, at))
            continue
        if doc.arrived > stamp:
            continue                  # 到得比分界线还晚 ⇒ 不可能被哪一份带走过
        if when is None or when <= start:
            if _on_a_list(row['url'], row['title'], listed, once):
                continue      # 露过面 ⇒ 证据不全,不许下"历史存量"这个终态
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


def _listing(chain) -> tuple:
    """把各份清单预处理成"查得快"的样子:(全文, 按首字符分桶的条目)。

    标题那一支要逐条 `startswith`,而库里两万多篇 × 六百多条就是一千四百万次
    比较(实测 1.6 秒,全程攥着提交锁)。标题只可能命中**首字符相同**的那几条,
    分桶之后每篇只比几条。
    """
    buckets: dict[str, list[str]] = {}
    for c in chain:
        for row in c.rows:
            buckets.setdefault(row[:1], []).append(row)
    return '\n'.join(c.text for c in chain), buckets


class Candidate(NamedTuple):
    """一篇文档拿去和某一份日报的清单对质时要用到的几样东西。"""
    key: str
    title: str
    when: datetime | None
    arrived: str
    credit: str


def _match_holds(past: PastReport, doc: Candidate, once: set) -> bool:
    """没有 url 时,那条按标题匹配上的归属算不算数。

    `doc_key` 在 url 为空时退化成标题,于是**两篇同名文档共享一个身份** ——
    真正新抓到的那一篇被旧日报抢走归属,从此 `UNCARRIED` 永远排除它(实测:同名
    无 url 的新文章 `pending.items=0`,生成直接答"没有未上报过的新内容")。所以
    这条匹配要三道各排除一种「它不可能是那一篇」,**缺一不可**:

    ①**库里只有这一篇叫这个名字**(口径同 `_on_a_list` 的 `once`)。同名的两篇
      都还在库里、又都发布在那一份的窗口里时,只有这一道分得开"该盖的只有一篇"。
    ②**到得不比那一份生成的时刻晚**(`latest_pickup`)。一份日报只可能带走它生成
      那一刻已经在库里的东西。**判据必须是那一份自己的时刻,不是整条链的分界**
      —— 拿分界来问,一篇"晚于它匹配的那份日报、却早于后面那几份"的补抓文章三道
      全过,照旧被抢走归属(实测:发布 09-27 20:00、入库 09-28 13:00 的新来源同名
      文章被盖上 `2026-09-28`,待报道归零、重新生成也找不回来)。反过来,一篇
      确实被后来某份日报带走过的文章也不该因为"到得比分界晚"就被摘掉归属。
    ③**发布不晚于那一份自己的窗口截止**。日报筛的是 `published`,比窗口截止还晚
      才发布的内容不可能出现在它的清单里。**这一道是 ① 的独立补位**:旧那篇被
      `remove_missing_documents` 清掉之后,新来的同名文章在库里就成了独一份、①
      当场失效(实测:旧文章已清理时新文章被盖上 `2026-09-28`、待处理归零)。
    ④**清单那一行记的「来源·账号」没有和这篇文档冲突**。前三道都只能排除「它不
      可能是那一篇」,这一道是唯一的**正面**身份证据 —— 清单里本来就写着来源和
      账号(`daily._page` 的收尾),丢掉它等于自愿只拿间接证据判身份。**两边都
      读得出、而且明确不同时才拒**:那一份没记下(老格式、解析不出)就不问这一道
      —— 宁可少拒一次,也不拿一个没读准的来源去否决真实归属。代价知情:公众号
      改名、来源标签改写之后它会判成冲突、把归属放回,那是多报一遍,安全的一边。

    四道都站在"宁可不盖"那一边:认不出就留在队列里,最多多报一遍;盖错是永久
    写死。反过来,真的是那一篇旧素材重新导出时四道全过,照旧盖章 —— 不是把所有
    无链接素材一概判新。
    """
    recorded = past.credit_of(doc.key)
    return (page_flat(doc.title) in once
            and not (doc.arrived > past.latest_pickup())
            and not (doc.when is not None and doc.when > past.until)
            and not (recorded and doc.credit and recorded != doc.credit))


def _on_a_list(url, title, listed: tuple, once: set) -> bool:
    """这一篇在某一份日报的素材清单上露过面吗(按原文找,写法差异先归一掉)。

    只用来**否决** `#seed` 这个终态,不用来盖归属 —— 方向见
    `daily_page.material_text`。空值不参与:空串在任何字符串里都"找得到",
    那会把整库豁免掉。

    **标题这一支只认库里独一份的标题**(`once`)。同一份稿子在库里有一堆副本
    (实测:每天重发的「量子位编辑作者招聘」一类,324 篇里 323 篇是这个形态),
    其中一份被某日报带走过,剩下几份是**另外的文档**、确实从没报道过 ——
    `#seed` 对它们是正确结论,拿标题兜底只会把几百条同名稿子倒灌进后面几份
    日报。url 那一支不设这道:它本来就是身份。
    """
    joined, buckets = listed
    u = page_flat(url)
    if u and u in joined:
        return True
    t = page_flat(title)
    if not t or t not in once:
        return False
    # 标题**总在一条的开头**(链接那一支多一个 `[`)。裸子串会被短标题
    # (「git」「测试」「-」)在任意一条的中间撞上,实测 7 条里 6 条如此。
    return any(row.startswith(t) for row in buckets.get(t[:1], ())) or \
        any(row.startswith('[' + t) for row in buckets.get('[', ()))


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
