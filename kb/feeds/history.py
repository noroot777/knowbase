"""新订阅的历史下界:那道「只收最近几天」的闸,以及它现读的那个下界。

从 `kb.feeds.sync` 拆出来的 —— 合并主干之后 `sync.py` 到了 703 行,越过本仓
700 行硬上限,而这一块是它里面最独立的一个职责(硬约束和事故形态都在
`docs/decisions/history-depth.md`):只依赖
`registry`,不碰 `_stage` / `_state` / `VAULT_ROOT` 这些**被测试打桩的模块
变量** —— 那种名字搬走之后 `patch.object(sync, ...)` 只改到一份副本,所以
预取和 `_repointed` 那两组都留在原处。

`sync` 把 `_within_history` 原地再导出,全仓注释和回归里那些
`sync._within_history` 的指向照旧成立(其中一条是 `feed_trim_history` 那道
清理口径的互为补集回归,它直接调这个名字)。
"""

from __future__ import annotations

from kb.feeds import registry as reg


def _within_history(source, entries):
    """新订阅只收最近几天的内容,不把源站的整个存档一起拖进来。

    下界是订阅创建(或改链接重置)那一刻钉死的一个日期,**不是"距今 N 天"的
    滑动窗口**。两点理由,缺一条这道闸就从防线变成漏斗:

    ①滑动窗口会在源站停更几天后恢复时,把那几天的内容静默丢掉——而钉死的下界
      对一个已经建立起来的订阅永远是过去式,只在它自己最前面那几次抓取起作用。
    ②只拦"第一次抓取"同样不行:第一次之后 etag 命中 304、什么都不返回,等源站
      下次更新时整份 feed(连同那 500 条存档)又回来了,那时闸门已经撤了。
      按固定日期判则与抓第几次无关。

    没有发布时间的条目照收:它身上没有任何"很旧"的证据,丢掉是不可恢复的静默
    损失,而日报那边本来就按"补录"接住这一档(只会出现一次)。区间补抓
    (`scope.ranged`)不受这道闸限制——那是用户点名要的那段日期,拿它当逃生口。

    **下界现读,不信手里那份订阅快照**:快照是发请求**之前**读的,而这中间
    `feed_trim_history` 可能刚给这个订阅钉上下界、把存档清了出去——按旧快照
    (空下界)存结果就是把刚删掉的旧文原样再入库一遍,文件和归属一起回来
    (2026-10-01 审查实测 1 条)。订阅行已经没了的时候才退回快照那一份。
    """
    floor = _stored_floor(source)[:10]
    if not floor:
        return entries
    return [e for e in entries if not e['published'] or e['published'][:10] >= floor]


def _stored_floor(source) -> str:
    with reg.db() as c:
        row = c.execute('SELECT history_floor FROM subscriptions WHERE id=?',
                        (source['id'],)).fetchone()
    return (row['history_floor'] if row else source.get('history_floor')) or ''
