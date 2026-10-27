# -*- coding: utf-8 -*-
"""**这一轮抓哪几条订阅** —— 范围匹配,外加「确认过的那份描述还对得上吗」。

从 `kb/feeds/sync.py` 搬出来**只因为 700 行上限**(口径同 `export_commit.py`)。
分界线是「选谁」和「怎么跑」:这里只回答前者,一次网络请求都没有、一个字节都不写。

**硬约束见 `docs/decisions/intake-diagnostics.md` 第一节**(描述怎么一路传到这里)。
"""
from __future__ import annotations

from kb.feeds import registry as reg
from kb.intake_diag import warrant


def pick_sources(kind: str, scope, settings: dict) -> tuple[list, list[str]]:
    """`(这一轮要抓的那几条, 确认之后被改掉、这一轮作废的那几条的名字)`。

    匹配判据只有一份(`reg.in_scope`):这一轮抓哪几条,和「这几条现在抓不抓得动」
    (`FeedCollector.status(targets=...)`)必须认同一批订阅。

    **带着确认描述来的那几条,这里要用同一份口径重算一次再比。** 入口那一道
    (`intake_api.start_sync`)关不掉「确认完、真读到这一条之前被改指」那个窗口
    —— 执行器手上只有编号时,选源这一句会按编号选中**改指之后**的那一条,把它当成
    旧授权来跑:实测确认文字点名来源 A、响应说「同步已开始」,而采集器选中并访问了
    `source-b.example.org`,`checked=1`、`repointed=0`(第 1 轮自由审查 R2)。

    **对不上就作废这一条**,不许认领当前版本 —— 它不进返回的名单,因此既不算检查过、
    也不会去结清谁的旧失败(契约在 `kb.collectors.Coverage`)。它**不是失败**:退避
    一格没动、水位一个字没改,下一轮按新设置照常跑。

    **没带描述的那几条照旧**(日常同步、定时那一轮、CLI):它们点的就是「现在这几
    条」,没有「确认时」可言(口径在 `warrant` 模块头)。
    """
    picked, stale = [], []
    for source in reg.subscriptions(kind):
        if not (source['enabled'] and reg.in_scope(source, scope.targets)):
            continue
        want = dict(scope.expect or ()).get(reg.id_target(source['id']))
        if want is not None and want != warrant.source_sig(source, kind, settings):
            stale.append(source['name'])
            continue
        picked.append(source)
    return picked, stale
