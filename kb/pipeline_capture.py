# -*- coding: utf-8 -*-
"""同步面板失败明细**按条目现状**改写:「从浏览器采集」标不标、人已经补好的那条还显不显示。
**出口那一刻做,不写进运行记录。**

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

import json
from datetime import datetime
from zoneinfo import ZoneInfo

#: 运行记录里的时刻按项目时区写、不带偏移(CLAUDE.md:时间一律按 Asia/Shanghai)。
TZ = ZoneInfo("Asia/Shanghai")


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


def _finished(run) -> datetime | None:
    """这一轮的收尾时刻(项目时区写的本地时间)。还在跑、或者读不出来就是 None。"""
    stamp = run.get("finished_at") if isinstance(run, dict) else None
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).replace(tzinfo=TZ)
    except ValueError:
        return None


#: 「这份正文是人补进来的」那几条路写进 `body_provenance` 的 `method`:采集抽屉
#: (`browser_capture.save` 记 `browser_<取法>`)、收录时改用这一页的正文
#: (`grab` 记 `browser_grab`)、手动粘贴(`browser_capture.save_pasted`)。
def _by_hand(provenance: str) -> datetime | None:
    try:
        said = json.loads(provenance or "")
        method = said.get("method") or ""
        if method != "manual_paste" and not method.startswith("browser_"):
            return None
        return datetime.fromisoformat(said["captured_at"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _hand_filled(refs) -> dict[tuple[str, str], datetime]:
    """这几条里**此刻那份正文是人补进来的**那几条,各自补进来的时刻。纯读。"""
    keys = sorted({key for _kind, key in refs})
    if not keys:
        return {}
    from kb.feeds import registry as reg
    slots = ",".join("?" for _ in keys)
    with reg.read_db() as c:
        rows = c.execute(f"SELECT key, kind, body_provenance FROM feed_items"
                         f" WHERE key IN ({slots})", keys).fetchall()
    out = {}
    for row in rows:
        ref = (row["kind"], row["key"])
        if ref in refs and (at := _by_hand(row["body_provenance"])):
            out[ref] = at
    return out


def mark(runs) -> None:
    """按条目现状改写这几份运行结果里的明细(**就地改**)。**库各只问一次。**

    ①**那一轮结束之后有人亲手补过正文的,那一条从明细里拿掉。** 它在那一轮确实
    失败过,可那件事人已经办完了 —— 留着它,旁边就只剩一颗「不再抓取」,指着一篇
    刚补好正文的文章让人永久删掉(实测:采集抽屉补完 OpenAI 那篇,按钮消失、整行
    还在)。判据是「补进来的时刻晚于那一轮收尾」:还在跑的那一轮、补正文早于收尾
    的(那一轮之后的失败说的就是这份新正文)一律不拿掉 —— 拿不准就照旧显示。
    阶段的结论和那句「本轮 N 条失败」是那一轮的事实,不改。
    ②剩下的按 `lanes.capturable` 标上 / 摘掉 `capture`。
    """
    runs = [run for run in runs if isinstance(run, dict)]
    refs = {ref for item in _failures(runs) if (ref := _ref(item))}
    hand = _hand_filled(refs) if refs else {}
    from kb.pipeline_failures import remove_where
    for run in runs:
        done = _finished(run)
        if not done or not hand:
            continue
        for entry in _entries(run):
            remove_where(entry, lambda f: (ref := _ref(f)) in hand and hand[ref] > done)
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
