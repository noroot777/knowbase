# -*- coding: utf-8 -*-
"""「重试失败项」现在还该抓哪几条:**那一轮失败之后,它是不是已经不用抓了。**

**硬约束在 `docs/decisions/retry-scope.md` —— 改这块之前先读那一份。**

从 `kb/feeds/sync.py` 拆出来的(那份越过了 700 行硬上限,同 `history` / `exported` /
`export_meta` 那几次)。口径一个字没动,`sync` 原地再导出这两个名字。

这一份答的是**执行端**那个问题,而「页面上今天还欠哪几步」必须和它读同一份派生
(`pipeline_retry.owed_now`)—— 两处各判一遍就是「页面说还欠一步、点下去又说没有
可补」那个死局(审查 R2)。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from kb.feeds import registry as reg

#: 「今天」按项目时区算(CLAUDE.md 那条),不按宿主机、更不按 `last_success` 自己
#: 那个 UTC 日期 —— 两者差 8 小时,截字符串比日期会在每天 08:00 之前判反(见
#: `_succeeded_today`)。
TZ = ZoneInfo('Asia/Shanghai')


def _succeeded_today(stamp: str, now: datetime | None = None) -> bool:
    """这条订阅**今天**成功同步过(项目时区)。空串 / 读不懂一律 False。

    **必须换算时区再取日期,不许截 `stamp` 的前 10 个字符**:`last_success` 是
    `reg.now()` 的 UTC ISO,日界比 Asia/Shanghai 早 8 小时,截字符串会在每天
    08:00 之前判反 —— 昨天上午成功过的订阅被当成「今天成过」而跳过重抓,**静默
    丢数据**。**读不懂的时刻算「没成过」,不按项目时区补齐**:补齐猜错是少抓,
    当成没成过顶多多抓一次。两条的完整形态在 `docs/decisions/retry-scope.md`。
    """
    if not stamp:
        return False
    try:
        at = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    if at.tzinfo is None:
        return False
    return at.astimezone(TZ).date() == (now or datetime.now(TZ)).astimezone(TZ).date()


def still_failing(kind, note):
    """上一轮失败的那几条订阅里,现在仍然需要重抓的那些(返回订阅 id)。

    判据**不是** `last_error` 还在不在:改链接时 `save_subscription` 的 reset 会把
    它连同退避一起清空,照它筛就把「已改正、还没验证过」的那条当成「已经恢复」漏掉
    —— 用户修完地址点「重试失败项」,那个账号一条都不抓(见 `note_retry_targets`)。

    摘掉的有两档,都是「再抓一遍也没有新东西」:

    - **那一轮失败之后成功同步过**:`last_success` 比名单的时刻新(比如用户在
      「我的订阅」里单独同步过一次)。改过链接的那条 `last_success` 被 reset 清成
      了空串,所以它留在名单里。
    - **今天已经成功同步过**(`_succeeded_today`),哪怕后来又失败了一次 —— 这一档
      就是「继承」。**继承只在当日内**,跨天由日期判据自动清零。这颗按钮的射程是
      「把上一轮没成的补上」,不是「再取一次新内容」;当天想再取新的走「我的订阅」
      里单独同步、或者一次完整同步。事故形态与边界见 `docs/decisions/retry-scope.md`。

    订阅被删掉、停用、或者名单里那个 id 压根不存在时一并摘掉:没有可抓的东西。
    """
    at = note.get('at') or ''
    ids = {str(t) for t in (note.get('targets') or [])}
    # 名单是空的(这个来源那一轮扫完了、没有要重抓的)就别去开订阅库:答案只能是
    # 空集,而读库会在库打不开时抛 —— 那个故障跟同一轮里的 index/extract 毫无
    # 关系,不该由它们承担(调用方另有一道按来源的隔离,见 `retry_crawl_scopes`)。
    if not ids:
        return []
    live = []
    for source in reg.subscriptions(kind):
        if str(source['id']) not in ids or not source['enabled']:
            continue
        if at and source['last_success'] and source['last_success'] > at:
            continue
        if _succeeded_today(source['last_success']):
            continue
        live.append(str(source['id']))
    return live
