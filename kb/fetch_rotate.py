# -*- coding: utf-8 -*-
"""账本的退役协议:哪几轮该清出去,以及**谁还有权往账本里补行**。

从 `kb.fetch_rounds` 里分出来的**写投影**:那边持着文件锁、缓冲和凭证(「此刻谁还
欠着行」由它算),这边只回答「这一份快照该重写成什么」。读投影在 `kb.fetch_fold`;
单文件 700 行上限见 CLAUDE.md,硬约束与事故形态在 `docs/decisions/daily.md`。

**成组保留、成组退役 —— 清轮次不能只看日期。** 过期那一轮的开轮行先被清走、它的
回执随后从缓冲里补写出去,盘上就留下一条**找不到轮次的孤行**:折叠据此报
`ok=False`(`fetch_fold`),而 `ok=False` 又禁止下一次轮换,那条孤行从此清不掉 ——
面板永久写「抓取次数读不出来」,刷新、重启、第二天再同步都不恢复(审查实测的 F2)。
所以保留策略对**开轮、子轮、成果、结束、缺口标记外加那笔独立凭证**一起闭合:
一轮只要还有人欠它的证据,它和它那几行一起留着;欠账落盘了、或者那笔凭证本身过了
保留窗口被清掉(`fetch_pending._prune`),才连成果带结束行一起退役。

**而退役必须对补写那一侧也闭合。** 过期那一轮被清掉之后,还攥着它的行的那个进程
(别的进程的凭证清理只看日子,见 `fetch_pending._prune`)下一次落盘就把回执补进了
一份没有父轮的账本 —— 同一条孤行,只是这回从别的方向来:它活着、却跨过整个保留窗口
没再抓也没被读一次,凭证的日子就停在那几天前。所以补写前要先问一句「我这几行的父轮
还在吗」(`disown`),不在就连那笔欠账一起退役。**丢掉的一定是过了保留窗口的轮次的
行** —— 轮换只清这种,而面板只显示今天,所以零显示损失(审查实测的 F1:两个真实进程
加一个全新进程,面板从此永久「抓取次数读不出来」,重启和第二天再同步都不恢复)。

**重写要把一轮的全部事实都抄走**:漏了 `t`,「今天第 N 次」的顺序就退回物理行序;
漏了结束行,一轮本来完整的抓取过一次台面下的维护就变成「至少 N 条」;漏了还没交完
的那几个子轮,反过来把不完整的说成完整 —— 而轮换是台面下的维护动作,用户只会看到
数字自己变了。
"""
from __future__ import annotations

import json
from pathlib import Path

from kb.pipeline_persist import _write_atomic


def _lines(r: dict) -> list[dict]:
    """把一轮折好的事实重新摊成账本那几行(开轮 / 成果 / 子轮 / 结束 / 缺口)。"""
    out = [{'day': r['day'], 'key': r['key'], 'at': r['at'],
            # `t` 要一起抄走:它是「第 N 次」的排序依据(存量行没有,照旧留空)。
            **({'t': r['t']} if r['t'] else {}),
            'ex': 1 if r['ex'] else 0}]
    if r['got']:
        out.append({'key': r['key'],
                    'items': [[c, f] for c, f in sorted(r['items'].items())]})
    for seg in sorted(r['segs'] - r['ends']):
        out.append({'key': r['key'], 'seg': seg})
    for seg in sorted(r['ends']):
        out.append({'key': r['key'], 'seg': seg, 'end': 1})
    if r['partial']:
        out.append({'key': r['key'], 'partial': 1})
    return out


def disown(held: list[dict], fresh: list[dict],
           rounds: list[dict]) -> tuple[list[dict], list[dict]]:
    """把攥着的那几行分成「父轮还在」和「父轮已经退役」两摞,后者一摞退役。

    父轮在不在要连**这一批马上要写的行**(`fresh`)一起问:开轮行和它的回执可以一起
    攥在缓冲里(两样都没落盘),那时账本里当然查不到父轮,可它跟着这一批就进去了。
    只问账本的后果是把一轮完好的抓取连开轮行一起丢掉。

    调用方保证两件事:账本**读全了**(读不全时「父轮在不在」没有答案),而且**里面
    确实还有轮次** —— 空账本更像文件被删掉而不是「过期那轮被清走了」,那时连今天的
    欠账都会被当成孤儿(判据和那一档的后路都在 `fetch_rounds._flush`)。
    """
    alive = {r['key'] for r in rounds}
    alive |= {str(o.get('key') or '') for o in list(held) + list(fresh) if 'day' in o}
    live: list[dict] = []
    gone: list[dict] = []
    for one in held:
        (live if str(one.get('key') or '') in alive else gone).append(one)
    return live, gone


def rotate(path: Path, rounds: list[dict], floor: str, owed: set[str],
           stray: set[str]) -> None:
    """把早于 `floor`、而且不在 `owed` 里的轮次清出账本(整份原子替换)。
    `stray`(找不到父轮的那几个 key)非空时**即使一轮都不该退役也要重写** ——
    那几行只有整份重写才清得掉,留着就是永久的「读不出来」。

    调用方保证三件事,缺一样都不许走到这里(判据都在 `fetch_rounds._rotate`):
    有行**压根读不出来时一个字都不动**(那时我们并不知道文件里还有什么,拿认得出来
    的那部分重写回去等于把读不出来的那几行悄悄删掉;孤行不在此列,它认得出来是谁的,
    见模块开头)、独占锁**还在手里**而且罩住了那次读(这一步是整份替换,拿一份没被
    保护的旧快照写回去会把另一个进程已经确认过的整轮抹掉,而剩下的行行行合法)、
    `owed` 是「此刻还有谁欠着行」那份**确定**答案。

    没有要清的轮次、也没有孤行时一个字都不动(整份重写本身也是一次写故障机会)。
    写失败不出声:最坏只是账本长一点,结论不受影响。
    """
    live = [r for r in rounds if r['day'] >= floor or r['key'] in owed]
    if not stray and len(live) == len(rounds):
        return
    lines = [one for r in live for one in _lines(r)]
    try:
        _write_atomic(path, ''.join(
            json.dumps(o, ensure_ascii=False) + '\n' for o in lines).encode('utf-8'))
    except OSError:
        pass
