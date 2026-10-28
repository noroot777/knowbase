# -*- coding: utf-8 -*-
"""补抓这条链上的**人工出口**:挂起的那几条怎么看、怎么放回队列。

从 `kb/feeds/fulltext.py` 搬出来**只因为 700 行上限**(口径同
`kb/feeds/fulltext_scope.py`:那边是纯判据,这边是人按下去会改库的那三个出口)。
分界线是「谁发起的」:自动补抓那一轮(取数、抓取、写回、存量清理)留在
`fulltext.py`,**人**发起的三件事在这里 —— 看一眼挂起了哪几条、把挂起的放回
队列、把额度耗尽的额度还回去。所以这里一次网络请求都没有。

**名字一律由 `fulltext` 原样再导出**,调用点一个字都不用改 ——
`fulltext.held()` / `fulltext.release()` / `fulltext.reopen()` 照旧成立
(网页那两个端点、`./kb.sh held`、「问题与待办」那两颗按钮和管道里的点名重试
都按这几个名字调)。

**硬约束见 `docs/decisions/fulltext.md`;搁置和点名重试那两道见
`docs/decisions/intake-diagnostics.md`。**
"""
from __future__ import annotations

from kb.feeds import registry as reg
from kb.feeds.fulltext_scope import _eligible, exhausted_sql, held_sql
from kb.intake_diag import shelf


def held(kind: str) -> list[dict]:
    """**挂起等人看一眼**的那几条:判出了证据不足,所以自己不会再抓了。

    这是「可恢复状态」那一半:上一版里这一类和「对方真拦着」走同一个出口,试满
    `MAX_TRIES` 之后**静默**离开队列、按壳入库,面板上再也没有任何痕迹 —— 而它和
    真关卡的区别恰恰是**我们并不知道**那一页是什么。所以这一列留着:这一条答得出
    「上一次为什么没收下」,人点一次 `release()` 就放回队列。

    **判据三条都要,而且和 `queued_sql` 第一条同源**:
    - `fulltext_hold<>''` —— 挂着一个没答案的问题;
    - `fulltext_at=''` —— 还没补到全文。`_save` 成功时会把 hold 清掉,这一条是
      冗余带:两处口径不一致时宁可少列一条,列出一条已经补好的只会让人白点一次;
    - **这条订阅现在还开着补抓** —— 不然 `release()` 把它放回队列是句空话
      (`queued_sql` 第一条会把它挡在外面),而面板上却显示「已恢复 1 条」。
      关掉补抓就是「这条订阅的正文我不要了」,待办跟着消失才是诚实的;**也不许
      反过来替用户把那个开关打开**。

    **不再要求额度用完。** 挂起这一档压根不扣额度(`_fail` 的 `count=False`):它
    一次就离开队列,等的就是人 —— 要求「试满三次才算待办」会让它永远不出现。
    """
    with reg.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT key,url,title,fulltext_hold,fulltext_tries FROM feed_items fi"
            f" WHERE kind=? AND {held_sql('fi')}"
            " ORDER BY fetched_at DESC, key", (kind,)).fetchall()]


def release(keys, kind: str | None = None, lives=None) -> int:
    """**人工出口**:把补抓额度还给这几条,放回队列。返回真放回去了几条。

    **判据待在 UPDATE 自己的 WHERE 里**(元规则):`fulltext_at=''` —— 这一下和
    kb-web 那边正在跑的补抓 worker 是两个写入方,先 SELECT 再 UPDATE 在
    legacy `isolation_level` 下压根不持锁。这期间那一条已经补到全文时放行不了,
    `rowcount==0` 就是「不用管了」(元规则:0 行不是失败,多半正是想要的末态)。

    **「这条订阅还开着补抓」也在同一个 WHERE 里,判据和 `held()` 同源。** 关掉补抓
    之后放行是句空话:`queued_sql` 第一条会把它挡在队列外面,而这里却报了「已恢复
    1 条」 —— 答不上「那它什么时候再抓」的承诺不如不给。**不许替用户把那个开关
    打开**:关掉补抓就是「这条订阅的正文我不要了」。

    **清的是额度和待办,一个字节的正文都不碰。** 这一列底下躺的是「壳还在、全文
    没拿到」的条目 —— 要换的只是「还能不能再试一次」。`fulltext_tries` 一起清 0:
    一条可能先网络失败两次、再判出证据不足挂起,只清 hold 的话它回到队列立刻就撞
    上满额度、又一次静默退回摘要。`fulltext_error` 一起清掉:留着它面板上会显示
    一条已经作废的失败原因。

    **已经在路上的那一轮补抓写不回来了 —— 这一下给它发一个新的代次。**
    `fulltext_gen=fulltext_gen+1`:放回队列 = 发了一轮**新的**工作机会,而
    `work_version` 的第五道比的就是这一列。在它之前发出去的那个请求回来之后
    `rowcount==0`,于是不会把刚清掉的 hold 重新写上、也不会扣掉刚还回去的额度
    (上一版这里是知情的取舍:那时还没有这一列,代价是「那一条又挂回去了,人再
    点一次」)。代次只增不复用 —— 还了额度再还一次也不会让某个旧请求复活。

    **`fulltext_tries` / `fulltext_hold` 这几列都不能充当那个代次**:它们正是
    这一句要清成目标值的东西,清完之后和「从没挂起过」逐字相同(口径同
    `work_version` 那段「可复原的输入值不是一次工作的身份」)。

    **`fulltext_hold<>''` 也在 WHERE 里,否则返回的那个数是句谎话。** 元规则:统计按
    **实际写入结果**报。不带这一条时同一个 key 给三次会报 `released=3`(三次 UPDATE
    都匹配得上,因为那几列已经是目标值了,SQLite 照样算 1 行),已经恢复过的、压根
    没挂起过的也一样报「恢复成功」—— 而真放回队列的只有一条。**同一个 key 在一次
    调用里重复出现由同一条判据覆盖**(第一次把 hold 清掉,后面几次就不匹配了),所以
    这里不另做一次去重 —— 加过,反向验证红 0 条,两段代码做的是同一件事。

    **`kind` 是可选的来源范围,给 HTTP 那个调用点用。** 路由上的 `kind` 过了合法性
    校验,不等于请求体里每个 key 都属于它:实测把网页专栏那一条挂起文章的 key 提给
    RSS 的恢复端点,它照样被恢复、而 RSS 自己那条还挂着。范围要约束**写入本身**
    (元规则:判据待在写语句自己的 WHERE 里)。CLI 那一侧刻意不传 —— 它的
    `--release <key>` 文档写明按 key 跨来源,那是有意的契约。

    **`lives` 是「人看着的那一世」**(`{key: life}`,口径在
    `kb.intake_diag.warrant.life_sql`):页面上那颗按钮、或者占着管道等了十几分钟
    的那份点名意图,说的都是**当时**那一条。中间它可能被清理删掉又从同一个链接收
    回来 —— key 一个字没变,而放回队列这一下会作用在新一世上。带了就 AND 进这条
    UPDATE 自己的 WHERE(元规则:判据待在写语句自己的 WHERE 里),对不上
    `rowcount==0`(0 行不是失败,正是想要的末态)。**没带照旧**(CLI、存量调用方)。
    """
    from kb.intake_diag import warrant

    done = 0
    with reg.db() as c:
        for key in keys:
            life_cond, life_args = warrant.life_sql(warrant.life_of(lives, key))
            sql = ("UPDATE feed_items SET fulltext_tries=0,fulltext_hold='',fulltext_error='',"
                   'fulltext_gen=fulltext_gen+1'
                   f" WHERE key=? AND fulltext_at='' AND fulltext_hold<>'' AND {_eligible()}"
                   f" AND {shelf.free_sql()} AND ({life_cond})")
            args: list = [key, *life_args]
            if kind is not None:
                sql += ' AND kind=?'
                args.append(kind)
            done += c.execute(sql, args).rowcount
    return done


def reopen(keys, kind: str, lives=None) -> dict:
    """**把点名的这几条放回补抓队列** —— 挂起的解挂、额度耗尽的还额度。

    「问题与待办」那两颗按钮(「仅重新排队」和「立即执行」)共用这一份,所以
    「哪几条够资格被放回去」只有一个答案(元规则:口径只有一份)。各写一遍的后果
    是面板上「放回了 2 条」而真跑那一轮里一条都没被取到。

    **两档走各自的既有通道,这里不发明判据:**
    - 挂起(证据不足)→ `release()`:那条路的 WHERE 里已经钉着「还没补到全文」
      「还挂着」「这条订阅现在还开着补抓」「没被搁置」四道;
    - 额度耗尽 → 把额度还回去,**资格那一道转手 `exhausted_sql`**。

    **判据一律待在 UPDATE 自己的 WHERE 里**(元规则),不在 Python 那边先分一遍
    档再写:这一下可能跑在导出阶段的执行体里,而那之前那之后都有别的写入方。
    返回**实际写入结果**:给了 5 个 key 只放回 3 条就说 3。

    **`lives` 是确认那一刻这几条各在第几世**(`{key: life}`):「立即执行」那条路上
    这一份意图要等占住管道、跑到导出阶段才被消费,中间这条内容可能被清理删掉又从同
    一个链接收回来。**两档都要带上它** —— 只给一档带等于另一档照旧拿旧授权动新一世。
    """
    from kb.intake_diag import warrant

    reg.check_kind(kind)
    wanted = list(dict.fromkeys(keys))
    done = {'released': 0, 'body': 0}
    if not wanted:
        return done
    done['released'] = release(wanted, kind=kind, lives=lives)
    cond, cargs = exhausted_sql()
    with reg.db() as c:
        for key in wanted:
            life_cond, life_args = warrant.life_sql(warrant.life_of(lives, key))
            done['body'] += c.execute(
                # 代次加一,理由同 `release`:还额度 = 一轮新的工作机会,那之前
                # 发出去的请求回来不许扣掉刚还回去的这一次。
                "UPDATE feed_items SET fulltext_tries=0,fulltext_error='',"
                'fulltext_gen=fulltext_gen+1'
                f' WHERE key=? AND kind=? AND ({cond}) AND ({life_cond})',
                (key, kind, *cargs, *life_args)).rowcount
    return done
