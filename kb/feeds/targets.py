# -*- coding: utf-8 -*-
"""一次同步的**范围目标**是什么身份:按编号点名,还是按名字点名。

从 `registry.py` 里分出来的(那边到了 700 行上限)。搬出来的是**整组**:前缀、
两个产出/解析口径、以及唯一那份匹配 —— 分散开才是真风险,所以 registry 原地
再导出,`reg.in_scope` 那些调用一个字都不用改。

这里不认识订阅库,只认一个 `{'id', 'name'}` 的字典,所以不会和 registry 成环。
"""
from __future__ import annotations


#: 「这个目标点的是**编号**」的写法。范围目标原来是一个无类型的字符串空间:
#: 同一个 `"11"` 既可能是某条订阅的 id、也可能是另一条订阅的**名字**,而
#: `in_scope` 两种都认 —— 于是点一条的「同步」会把名字叫「11」的那条一起抓走
#: (第 1 轮审查 R1 实测:请求 `accounts=["11"]`,真跑了 11 和 12 两条)。
#: 所以**凡是按编号点名的产出方都写成这个形态**,`in_scope` 据此只按编号匹配。
ID_PREFIX = 'id:'


def id_target(source_id) -> str:
    """把订阅 id 写成**明确的编号目标**(产出方一律走这儿,不许自己拼前缀)。"""
    return f'{ID_PREFIX}{source_id}'


def target_source_id(target) -> str:
    """这个目标点的是编号吗;不是就返回空串。"""
    text = str(target)
    return text[len(ID_PREFIX):] if text.startswith(ID_PREFIX) else ''


def in_scope(source: dict, targets) -> bool:
    """这条订阅在不在这一次的范围里。空范围 = 全都在。

    **这是范围匹配的唯一一份口径**,`sync.collect`(真去抓哪几条)和
    `FeedCollector.status(targets=...)`(这几条现在抓不抓得动)都过它 —— 两边
    岔开的后果是**用一批订阅的就绪去替另一批放行**:名字匹配那一半认不出来的话,
    「重试这一条」会拿整条通道的就绪当答案,而那一条必定再失败一次。

    **它是「选源方向」的口径,只有这个方向才允许宽。** 这一侧宽一点 = 多抓一条,
    代价是重抓一次;而**记账方向**(「这一轮真跑成了谁」那份回执)宽一点 = 拿一次
    没发生的成功清掉另一条订阅的真失败,那条订阅从此不进重试计划 —— 静默丢数据。
    所以**成功回执不许过这儿**(形态和那一侧的口径都在 `pipeline_results.cover`),
    下面那几句「宁可多抓」只在选源方向上成立,别当成全局规则搬走。

    三种写法:

    - `id:11` —— **明确点名编号**(`id_target()`),只认编号。单条同步、订阅页那颗
      同步、重试收窄都走这个形态,所以「只同步我点的这一条」不再被同名/同数字的
      名字扩大范围。
    - `"某博客"` —— 名字。面板上的订阅名单是按名字给的(`/api/pipeline/accounts`
      只给名字),一行代表的就是**同名的那几条**,勾它的意思是它们都跑。
    - `"11"` —— **老形态**,id 和 name 两种都认。存量的历史轮次、日账名单里存着
      这种裸字符串,照旧要能匹配上。

    点名编号时**额外放行「真有订阅叫 `id:11`」那一档**:这种名字几乎不可能出现,
    真出现了就按两边都算 —— 在选源方向上多抓一条顶多重抓一次,漏抓是静默丢数据。
    """
    if not targets:
        return True
    sid, name = str(source['id']), source['name']
    for target in targets:
        text = str(target)
        want = target_source_id(text)
        if want:
            if want == sid or name == text:
                return True
            continue
        if sid == text or name == text:
            return True
    return False
