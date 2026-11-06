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
from typing import NamedTuple
from zoneinfo import ZoneInfo


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


#: 运行记录里的时刻按项目时区写、不带偏移(CLAUDE.md:时间一律按 Asia/Shanghai)。
TZ = ZoneInfo("Asia/Shanghai")


class _Hand(NamedTuple):
    """此刻那份正文是人补进来的:补完之后的版本、补进来的时刻、补抓失败过没有。"""
    rev: int
    at: datetime | None
    tried: bool


#: 「这份正文是人补进来的」那几条路写进 `body_provenance` 的 `method`:采集抽屉
#: (`browser_capture.save` 记 `browser_<取法>`)、收录时改用这一页的正文
#: (`grab` 记 `browser_grab`)、手动粘贴(`browser_capture.save_pasted`)。
def _by_hand(provenance: str) -> dict | None:
    try:
        said = json.loads(provenance or "")
        method = said.get("method") or ""
    except (ValueError, TypeError, AttributeError):
        return None
    if isinstance(method, str) and (method == "manual_paste" or method.startswith("browser_")):
        return said
    return None


def _moment(stamp) -> datetime | None:
    """带偏移的照原样,不带的按项目时区补齐;读不出来就是 None。"""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=TZ)


def _hand_filled(refs) -> dict[tuple[str, str], _Hand]:
    """这几条里**此刻那份正文是人补进来的**那几条。纯读。"""
    keys = sorted({key for _kind, key in refs})
    if not keys:
        return {}
    from kb.feeds import registry as reg
    slots = ",".join("?" for _ in keys)
    with reg.read_db() as c:
        rows = c.execute(f"SELECT key, kind, content_rev, fulltext_tries, body_provenance"
                         f" FROM feed_items WHERE key IN ({slots})", keys).fetchall()
    out = {}
    for row in rows:
        ref = (row["kind"], row["key"])
        if ref in refs and (said := _by_hand(row["body_provenance"])) is not None:
            out[ref] = _Hand(int(row["content_rev"] or 0), _moment(said.get("captured_at")),
                             int(row["fulltext_tries"] or 0) > 0)
    return out


def _superseded(item, hand, finished) -> bool:
    """这条**正文补抓失败**说的那一版,后来被人亲手补的正文换掉了。

    带 `body_rev` 的(补抓那一步记下的,`fulltext._failure`):版本比它新就算。
    带 `step` 而没有 `body_rev` 的(翻译、导出那几类):补正文不替它们把事办完,不算。
    **两样都没有的是升级之前那几轮的旧记录**,分不出是哪一步、也没记版本,退回旧口径:
    这一条补抓确实失败过(`fulltext_tries` > 0),而且补正文晚于那一轮收尾 —— 那一轮
    里的失败说的只能是补之前那一版。还在跑的那一轮、补在收尾之前的拿不准,不拿掉。
    """
    ref = _ref(item)
    if ref not in hand:
        return False
    filled = hand[ref]
    rev = item.get("body_rev")
    if "body_rev" in item:
        return isinstance(rev, int) and not isinstance(rev, bool) and filled.rev > rev
    if "step" in item:
        return False
    return bool(filled.tried and filled.at and finished and filled.at > finished)


def mark(runs) -> None:
    """按条目现状改写这几份运行结果里的明细(**就地改**)。**库各只问一次。**

    ①**补抓失败说的那一版正文、后来被人亲手补的正文取代了的,那一条从明细里拿掉。**
    它在那一轮确实失败过,可那件事人已经办完了 —— 留着它,旁边就只剩一颗「不再
    抓取」,指着一篇刚补好正文的文章让人永久删掉(实测:采集抽屉补完 OpenAI 那篇,
    按钮消失、整行还在)。判据是版本(`content_rev` 比失败时记下的新),不是时刻:
    同步还在跑后面几步时补好的、和收尾落在同一秒补好的,一样算办完了(升级之前没记
    版本的旧记录退回按收尾时刻判,见 `_superseded`)。
    阶段的结论和那句「本轮 N 条失败」是那一轮的事实,不改。
    ②剩下的按 `lanes.capturable` 标上 / 摘掉 `capture`。
    ③**此刻正文是人补的,剩下那几条(导出、翻译失败,旧记录)照旧显示、原因照旧在,
    只是不给「不再抓取」**(摘掉 `drop`)—— 人刚在这篇上花了力气,旁边不该摆一颗
    永久删除它的按钮。
    """
    runs = [run for run in runs if isinstance(run, dict)]
    refs = {ref for item in _failures(runs) if (ref := _ref(item))}
    hand = _hand_filled(refs) if refs else {}
    if hand:
        from kb.pipeline_failures import remove_where
        for run in runs:
            done = _moment(run.get("finished_at"))
            for entry in _entries(run):
                remove_where(entry, lambda f: _superseded(f, hand, done))
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
        if ref and ref in hand:
            item.pop("drop", None)


def mark_state(state) -> None:
    """`/api/pipeline/status` 的回包:当前这一轮和 `last_run` 两处(口径同 `strip_state`)。"""
    if isinstance(state, dict):
        mark([state, state.get("last_run")])


def mark_runs(runs) -> None:
    """历史清单整份过一遍。"""
    mark(runs or [])
