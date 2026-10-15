# -*- coding: utf-8 -*-
"""账本那几行怎么折成「今天开过哪几轮、各自新落了哪些文章、交完了没有」。

从 `kb.fetch_rounds` 里分出来的**纯投影**:没有 I/O、没有锁、不认任何模块状态,
输入是账本的行、输出是轮次列表外加一句「读全了没有」。那边剩下的是落盘协议
(文件锁、缓冲、凭证、轮换)和对外那几个动作;单文件 700 行上限见 CLAUDE.md。

**折叠必须幂等**,这是整份账本敢用「追加 + 重放」的全部前提:回执取路径并集、
子轮按集合算,所以同一行追加两遍、同一轮被两条通道各报一次,结论都一样。
改这里之前先读 `docs/decisions/daily.md`:`got` / `segs` / `ends` 三者的区别各有
一次实测事故,混起来的后果是面板笃定地报一个少记的数。

**而「第几次抓取」的先后也只由这一层定,`_order` 是唯一出处。** 折出来的轮次按
`(day, 起跑时刻, key)` 排好序再交出去,所有消费方(`today()` 的「最后一轮」、轮换
整份重写)拿到的都是这个顺序 —— **物理行序不是业务顺序**:账本是多进程追加的,
一轮因为短暂写失败晚落盘,它的行就排在后开那一轮的后面(实测形态在 `_order`)。
"""
from __future__ import annotations

from kb.pipeline_persist import _loads_json


# 没带子轮身份的结束行(存量账本、没有 run id 的调用)折成这一个身份。它**同时
# 算「登记过」和「交完了」**,所以老账本的结论一个字都不变。
_LEGACY_SEG = '-'


def _order(one: dict) -> str:
    """这一轮的**起跑时刻**,用于排序;原始行和折好的轮次都认同一套字段。

    `t` 是 `open_round` 写的微秒级时刻(定宽、零填充,所以按字符串比就是按时间比)。
    存量账本行只有分钟级的 `at`,补齐成 `:00.000000` —— 同一分钟内它排在带 `t` 的
    新行前面(那几行是更早版本写的,这个方向也对)。两样都没有的行排最前。
    """
    exact = str(one.get('t') or '')
    if exact:
        return exact
    rough = str(one.get('at') or '')
    return rough + ':00.000000' if rough else ''


def _order_key(one: dict) -> tuple[str, str, str]:
    """排序键:`(day, 起跑时刻, key)`。

    **`day` 在最前**:存量行的 `at` 万一不是完整日期(老版本、手改过),也只会乱在
    它自己那一天里面,不会跨天错位。**末位拿 `key` 兜平局**,为的是让顺序成为**数据
    的函数**而不是文件布局的函数:同一微秒开的两轮没有真实先后可恢复,但重放、补写、
    轮换重写必须给出同一个答案 —— 拿物理位置兜平局就又把它押回行序上了。
    """
    return (str(one.get('day') or ''), _order(one), str(one.get('key') or ''))


def _fold(lines) -> tuple[list[dict], bool]:
    """把账本每一行折成「开过哪几轮、各自新落了哪些文章、走完了没有」,外加一句
    「读全了没有」。

    五种行:开轮(带 `day`/`at`/`ex`)、回执(带 `paths`)、不可确认标记(带
    `partial`)、子轮登记(带 `seg`)、子轮交完了(带 `seg`+`end`)。**回执取并集、
    子轮按集合算**,所以同一行追加两遍、同一轮被两条通道各报一次,折出来的结论都
    一样(见模块开头那条)——「交完了几次」用计数的话,一次 fsync 重放就能把它
    推成完整。

    **`got`(收到过回执)和「交完了」是两件事,不许互相顶替。** 上一版只有 `got`:
    导出阶段进门先落一张空回执,于是「刚开始」在盘上和「全部交完」长得一模一样
    —— 随后生产者真写出了文章、进程却没了,盘上的结论永久是**确定的 0 条**
    (第 1 轮审查实测两个分支:预置空回执、以及多来源里第一个交完回执后退出)。

    **而「交完了」是 `segs == ends`,不是「有谁交过」。** 一次业务抓取的物化由 N 个
    子轮各跑一段,第一个子轮的结束行证明不了后面那几个也交完(第 2 轮审查实测)。
    结束行**同时**把自己记进 `segs`:只跑导出的那种运行(重试导出阶段)压根没开过
    轮、不会登记,它交完了也该算完,否则那一轮永远「不可确认」。

    `ok=False` 表示有行读不出来(半截行、被改坏、成果对不上轮次)。那时这份账本
    只能说「不可确认」—— 拿认得出来的那部分冒充完整事实,就又回到了把写失败说成
    「今天还没有抓取」。

    **交出去之前按业务起跑顺序排一次**(`_order`):调用方不许再按行序理解先后。
    同一轮重复开的那几条开轮行里**起跑时刻取最早那一条** —— 读失败之后同一个 key
    会再写一条开轮行,折叠本来就按先见到的那条算,而「先见到」又是行序。
    """
    rounds: list[dict] = []
    index: dict[str, dict] = {}
    ok = True
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        # 半截行(写一半断电)、被人改坏、根不是带 key 的字典 —— 都是「这一行
        # 读不出来」。**解析口径不在这里按异常类型列一份**(`_loads_json`):深嵌套
        # JSON 抛的 `RecursionError` 不在原来那张单子上,它从这里逃出去就把整次
        # 抓取拦在采集器之前(第 4 轮审查实测,形态在 `_loads_json`)。
        obj = _loads_json(raw)
        if not isinstance(obj, dict) or not obj.get('key'):
            ok = False
            continue
        key = str(obj['key'])
        if {'paths', 'partial', 'end', 'seg'} & obj.keys():
            got = index.get(key)
            if got is None:
                ok = False              # 有成果、却找不到它那一轮:记不全
                continue
            if obj.get('partial'):
                got['partial'] = True
            if obj.get('end'):
                seg = str(obj.get('seg') or _LEGACY_SEG)
                got['segs'].add(seg)    # 见上:交完了的子轮一定算登记过
                got['ends'].add(seg)
            elif 'seg' in obj:
                got['segs'].add(str(obj['seg'] or _LEGACY_SEG))
            if 'paths' in obj:
                if not isinstance(obj['paths'], list):
                    ok = False
                    continue
                got['paths'].update(str(p) for p in obj['paths'])
                got['got'] = True
            continue
        if key in index:
            # 同一轮重复开:幂等,不是第二次抓取。但起跑时刻取**最早**那一条,
            # 免得「这一轮几点开的」跟着行序变(读失败后同一个 key 会再写一条)。
            got = index[key]
            if _order(obj) < _order(got):
                got['at'] = str(obj.get('at') or '')
                got['t'] = str(obj.get('t') or '')
            continue
        got = {'key': key, 'day': str(obj.get('day') or ''),
               'at': str(obj.get('at') or ''),
               # 微秒级起跑时刻(`fetch_rounds.open_round` 写的),排序用。存量行
               # 没有这一笔,`_order` 退回分钟级的 `at`。
               't': str(obj.get('t') or ''),
               # 这次运行会不会物化。读不到(存量账本行)按会物化算 —— 那一侧
               # 的结论是「不可确认」,比笃定地说 0 条安全。
               'ex': bool(obj.get('ex', 1)), 'paths': set(), 'got': False,
               # 参与这一轮物化的子轮,以及其中**明确交完了**的那几个。
               # 两个集合相等才算这一轮交完(见 `today`);存量账本行里一个
               # 子轮都没有,那几轮只能说「至少 N 条」—— 新代码确实证明不了
               # 它们完整,这比继续笃定地报一个数安全。
               'segs': set(), 'ends': set(), 'partial': False}
        rounds.append(got)
        index[key] = got
    rounds.sort(key=_order_key)
    return rounds, ok
