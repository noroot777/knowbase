# -*- coding: utf-8 -*-
"""本日的抓取账本:今天抓了第几次、每一次往库里**新加**了多少篇。

日报面板那颗按钮上报的就是这个事实。它和日报的取材结果是两件事,**不许互相
顶替**:一次抓取抓了多少,是抓取自己的事,生成日报、重新生成、删掉日报都改变
不了它;而「这一次生成能选进去几条」受名额(`daily.MAX_ITEMS`/`LATE_ITEMS`)和
发布时间排序左右,算出来的数和「抓了多少」可以差两个数量级。拿后者冒充前者的
后果是:第二次同步真抓回来 9 条、按钮上写 3 条;抓回来的那条发布得早、抢不到
席位时按钮写「没有新内容」—— 而用户正是靠这个数决定要不要再花一次模型钱。

**「一次业务抓取」的身份是业务批次,不是管道运行。** 一轮日报 = N+1 次管道运行
(每个来源各一次),它们共用一个 `pipeline_batch.open_batch()` 发的身份;勾了三个
来源手动同步也是一次抓取、三次运行。按运行记的话面板上会写成「今天第 7 次抓取」
而用户只点过两次。没有批次身份的独立运行(单来源同步、重试失败项)各算一次,
那时它自己的 `run_id` 就是身份。

**先有 `open_round` 才认 `note_added`。** 轮次在**抓取阶段**开,新增数在**索引阶段**累加
—— 只勾了索引/提取的那种运行压根没抓过,它往库里写进去的东西(比如上一轮抓回来
还没索引的)不该凭空变出一个「第 N 次抓取」。`note_added` 找不到今天开过的那一轮就什么
都不做。

**只数日报认的那些来源**(`counts_as_new`)。条目化衍生树(`dr-gzh-projects` 等)
和原文是同一批内容,日报取材本来就把它们排除在外(`daily._scope`);把它们也算成
「抓到的新内容」,一轮条目化就能让按钮写出三位数,而日报那边一条新的都没有。

面板那一侧怎么显示(三种说法、自定义区间、tooltip 里那句队列积压)写在
`docs/decisions/daily.md` 的「这个计数长在生成按钮里」那一节。

**跨自然日自己归零**,按项目时区(CLAUDE.md 那条:一律 Asia/Shanghai,不按宿主
机)—— 这份账本只回答「今天」,昨天那几轮留着没有意义,也省掉一个清理入口。
"""
from __future__ import annotations

import json
from datetime import datetime

from kb.config import DATA_DIR, PRIMARY_SOURCE_NAMES
from kb.pipeline_persist import _write_atomic

FILE_NAME = 'fetch_rounds.json'
# 一天里真有这么多轮的话,多出来的只是不再单独留一行(计数照旧往上走);
# 上限存在只为挡住"某个循环把这份文件撑到几十兆"那一类失控。
_KEEP = 200


def _file():
    return DATA_DIR / FILE_NAME


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now()).date().isoformat()


def counts_as_new(source: str) -> bool:
    """这个来源的新文档算不算「抓到的新内容」。**口径只有这一份。**

    和日报取材同一个范围(`daily._scope` 用的也是 `PRIMARY_SOURCE_NAMES`):
    两处各判一遍的话,按钮上那个数和日报里能看到的内容迟早对不上。
    """
    return source in PRIMARY_SOURCE_NAMES


def _load() -> dict:
    """读这份账本。**读不出来一律当「今天还没有记录」**,不抛。

    它是个只读展示的事实,读失败不该让抓取或者面板挂掉;而写是幂等的,下一轮
    照样把今天的轮次重新记上 —— 最坏是这一天的计数从头算,不会把别的东西弄坏。
    """
    try:
        raw = json.loads(_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _rounds(raw: dict, day: str) -> list[dict]:
    """这份账本里属于 `day` 的那几轮(不是那一天就当空的)。"""
    if raw.get('day') != day:
        return []
    got = raw.get('rounds')
    return [r for r in got if isinstance(r, dict) and r.get('key')] \
        if isinstance(got, list) else []


def _save(day: str, rounds: list[dict]) -> None:
    """落盘。**写失败不出声** —— 同 `pipeline_state._save_history` 那条:这一份是
    只读展示,它写不写得上不该影响这一轮抓取的结论。"""
    try:
        _write_atomic(_file(), json.dumps(
            {'day': day, 'rounds': rounds[-_KEEP:]},
            ensure_ascii=False).encode('utf-8'))
    except OSError:
        pass


def open_round(key: str, now: datetime | None = None) -> None:
    """抓取阶段开始时报到:今天又开了一次抓取(同一个 `key` 重复调是空操作)。

    多来源同步时每个来源各调一次,但它们带着同一个批次身份,所以只开一轮 ——
    这正是「不许把多来源算成多次抓取」那条落地的地方。
    """
    if not key:
        return
    now = now or datetime.now()
    day = _today(now)
    raw = _load()
    rounds = _rounds(raw, day)
    if any(r.get('key') == key for r in rounds):
        return
    rounds.append({'key': key, 'at': now.strftime('%Y-%m-%d %H:%M'), 'added': 0})
    _save(day, rounds)


def note_added(key: str, count: int, now: datetime | None = None) -> None:
    """给今天这一轮抓取累加 `count` 篇新文档。**没开过轮次就什么都不做。**

    索引阶段可能跑好几次(管道编排里 crawl+extract 并行那条路),所以是累加而不是
    赋值;同一轮里重复调的结果是加总,不是互相覆盖。
    """
    if not key or count <= 0:
        return
    day = _today(now)
    raw = _load()
    rounds = _rounds(raw, day)
    for r in rounds:
        if r.get('key') == key:
            r['added'] = int(r.get('added') or 0) + int(count)
            _save(day, rounds)
            return


def today(now: datetime | None = None) -> dict:
    """今天抓了第几次、最后那一次抓了多少 —— 面板那颗按钮要的就是这个。

    `{'n': 今天第几次, 'added': 那一次新加了多少篇, 'at': 'HH:MM'}`。
    今天还没抓过时 `n` 为 0 —— 那是一句事实(「今天还没有抓取」),和「读不出来」
    不是一回事,后者由调用方自己兜(读失败这里按没记录算,见 `_load`)。
    """
    rounds = _rounds(_load(), _today(now))
    if not rounds:
        return {'n': 0, 'added': 0, 'at': ''}
    last = rounds[-1]
    return {'n': len(rounds), 'added': int(last.get('added') or 0),
            'at': str(last.get('at') or '')[-5:]}
