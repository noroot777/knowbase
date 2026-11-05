# -*- coding: utf-8 -*-
"""同步面板失败明细上那颗「从浏览器采集」:**出口那一刻按条目现状标,不写进运行记录。**

明细是一轮运行的事实,落了盘就不再变;而「这一条现在还能不能从浏览器补正文」是
条目此刻的状态 —— 正文后来补上了、被搁置了、不再收录了,答案都会变。把它冻进
那一轮的记录里,两头都错:升级之前跑的那几轮一颗按钮都没有(实测:合并之前那一轮
的七条补抓失败全部只剩「不再抓取」),补成之后按钮还一直挂在那儿。

所以跟 `kb.pipeline_failures.strip` 同一个位置:**回包拼完了、送出去之前**,按每条
明细自带的身份(`drop` 里的 `channel` / `id`)问一次 `intake_diag.lanes.capturable`
—— 和「问题与待办」那颗按钮同一份判据 —— 问得到就标 `capture`(那一条真实的
`feed_items.key`),问不到就摘掉。只认 `drop` 带来的身份,不按标题猜:来源给不出
身份的那几条照旧两样都没有(`docs/decisions/failure-drop.md`)。
"""
from __future__ import annotations


def _entries(run):
    """一份运行结果里所有带明细的格子:阶段卡片,和结果账里逐来源那几格。"""
    if not isinstance(run, dict):
        return
    stages = run.get("stages")
    for stage in stages.values() if isinstance(stages, dict) else []:
        if isinstance(stage, dict):
            yield stage
    results = run.get("results")
    for owned in results.values() if isinstance(results, dict) else []:
        for cell in owned.values() if isinstance(owned, dict) else []:
            if isinstance(cell, dict):
                yield cell


def _failures(runs):
    for run in runs:
        for entry in _entries(run):
            items = entry.get("failures")
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    yield item


def _ref(item) -> tuple[str, str] | None:
    drop = item.get("drop")
    if not isinstance(drop, dict):
        return None
    channel, key = drop.get("channel"), drop.get("id")
    if isinstance(channel, str) and isinstance(key, str) and channel and key:
        return channel, key
    return None


def mark(runs) -> None:
    """给这几份运行结果里的明细就地标上 / 摘掉 `capture`。**库只问一次。**"""
    items = list(_failures(runs))
    refs = {ref for item in items if (ref := _ref(item))}
    if refs:
        from kb.intake_diag.lanes import capturable
        able = capturable(refs)
    else:
        able = set()
    for item in items:
        ref = _ref(item)
        # 整对比,不只比 key:另一个来源的档案 ID 撞上这条 key 不许借走按钮。
        if ref and ref in able:
            item["capture"] = ref[1]
        else:
            item.pop("capture", None)


def mark_state(state) -> None:
    """`/api/pipeline/status` 的回包:当前这一轮和 `last_run` 两处(口径同 `strip_state`)。"""
    if isinstance(state, dict):
        mark([state, state.get("last_run")])


def mark_runs(runs) -> None:
    """历史清单整份过一遍。"""
    mark(runs or [])
