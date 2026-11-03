# -*- coding: utf-8 -*-
"""同一轮里几个来源**同时**跑时,「这句话是哪一路说的」。

几个来源各开一条线程跑自己的抓取 → 导出(`pipeline_sources`),而汇报那几处
(`pipeline_state._stage` / `_log`、分段卡片、结果账)原来都假定**同一时刻只有一个
来源在跑**:「当前那一段」「当前那个来源」各只记一格,谁最后写谁就是当前。两路同时
跑时,RSS 那一路的结论会写进公众号那一段、记到公众号那一格账上 —— 面板上张冠李戴,
当日账本拿 RSS 的绿去结清公众号的旧失败(静默丢数据)。

所以归属改成**按线程**认:跑某个来源的那条线程进门时 `bind(通道)`,之后它报的每一句
都知道自己是哪一路的。没绑过的线程(管道主线程跑全库那几步、存量提取那条线程)不属于
任何一路,它们说的话归「全库」。

**采集器自己开的工作线程不继承绑定**(`threading.local` 不跨线程)。眼下所有往面板
报进度的调用都在来源那条线程上(订阅源 / 翻译 / 正文补抓的线程池只在调用方那条线程
上汇总进度,YouTube 的元数据进度同理),所以不需要继承;将来有人从工作线程里报进度,
分段与结果账那两处各有一道「只有一路开着就算它的」兜底,日志则归「全库」—— 宁可归错
到全库,不许归到另一个来源头上。

日志按来源各留一份上限(`trim`):几路同时跑时一路刷屏(YouTube 的元数据进度、X 的
逐账号进度)会把别的来源仅有的几行挤出去,而那几行往往就是它的失败原因。
"""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager

#: 日志每一类(每个来源各一类,全库那几步合一类)各留多少行。
LOG_KEEP = 40

_local = threading.local()

# 日志行里来源标记的写法:`[11-03 09:00:02] <rss> 检查 9/9 个来源`。时间戳由
# `pipeline_state._stamp` 打在最前面,标记紧跟其后;前端 `pipeLogLine` 按同一个式样拆。
_TAG = re.compile(r"^(\[[^\]]*\] )<([A-Za-z0-9_.:-]+)> ")


def current() -> str:
    """这条线程绑着哪一路(通道名);没绑就是空串 = 全库。"""
    return getattr(_local, "channel", "")


@contextmanager
def bind(channel: str):
    """这一段时间里,这条线程上的汇报都算 `channel` 那一路的。可嵌套,出来时还原。"""
    prev = current()
    _local.channel = channel or ""
    try:
        yield
    finally:
        _local.channel = prev


def tag(msg: str, channel: str | None = None) -> str:
    """给一行日志打上来源标记(不带时间戳)。全库那几步不打。"""
    who = current() if channel is None else channel
    return f"<{who}> {msg}" if who else msg


def who(line: str) -> str:
    """这一行日志是哪一路说的;没有标记(全库、或者升级前的老日志)就是空串。"""
    hit = _TAG.match(line or "")
    return hit.group(2) if hit else ""


def trim(lines: list[str], keep: int = LOG_KEEP) -> list[str]:
    """每一类各留最近 `keep` 行,顺序不变。

    **按类各留,不按总数留**:总数 80 行的老口径在几路同时跑时,刷得最快的那一路会把
    别的来源整段挤掉 —— 而被挤掉的常常正是那一路唯一一句失败原因。
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for line in reversed(lines):
        k = who(line)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] <= keep:
            out.append(line)
    out.reverse()
    return out
