# -*- coding: utf-8 -*-
"""一次同步的**范围目标**是什么身份:按编号点名,还是按名字点名。

从 `registry.py` 里分出来的(那边到了 700 行上限)。搬出来的是**整组**:两个前缀、
各自的产出/解析口径、以及唯一那份匹配 —— 分散开才是真风险,所以 registry 原地
再导出,`reg.in_scope` 那些调用一个字都不用改。

范围目标原来是一个**无类型的字符串空间**:同一个 `"63"` 既可能是某条订阅的 id、
也可能是另一条订阅的**名字**,而 `in_scope` 两种都认 —— 两个方向都实测被一起抓走:

- 按编号点名那一侧:点某一行那颗「同步」,名字叫「11」的另一条也跑了(第 1 轮
  审查 R1/R2);
- 按名字点名那一侧:面板名单上只勾了名字叫「63」那一行,编号 63 的另一条订阅也跑了
  (第 1 轮自由审查 R1 实测:请求 `accounts=["63"]`,真跑了 63 和 64 两条)。

所以**新请求里的每个目标都得带类型**(`id_target()` / `name_target()`),`in_scope`
据此只按那一种比。无类型的写法只留给**存量**:历史轮次、日账名单里存着的裸字符串,
还有缓存着的老页面发来的那份名单。

这里不认识订阅库,只认一个 `{'id', 'name'}` 的字典,所以不会和 registry 成环。

硬约束、事故形态和回归清单在 `docs/decisions/sync-entry.md` 第一节。
"""
from __future__ import annotations


#: 「这个目标点的是**编号**」的写法。产出方一律走 `id_target()`,不许自己拼。
ID_PREFIX = 'id:'
#: 「这个目标点的是**名字**」的写法。产出方一律走 `name_target()`。
NAME_PREFIX = 'name:'


def id_target(source_id) -> str:
    """把订阅 id 写成**明确的编号目标**(产出方一律走这儿,不许自己拼前缀)。"""
    return f'{ID_PREFIX}{source_id}'


def name_target(name) -> str:
    """把订阅名写成**明确的名称目标**(同上)。

    名字本身就长得像另一种写法时照样成立:真有订阅叫 `id:63`,写出来是
    `name:id:63` —— 解析只摘掉最外面那一层,剩下的整串才是要比的名字。
    """
    return f'{NAME_PREFIX}{name}'


def target_source_id(target) -> str:
    """这个目标点的是编号吗;不是就返回空串。"""
    text = str(target)
    return text[len(ID_PREFIX):] if text.startswith(ID_PREFIX) else ''


def target_source_name(target) -> str:
    """这个目标点的是名字吗;不是就返回空串。"""
    text = str(target)
    return text[len(NAME_PREFIX):] if text.startswith(NAME_PREFIX) else ''


def bare_target(target) -> str:
    """把目标写回**无类型**的形态:`id:63` → `63`、`name:博客甲` → `博客甲`。

    账本那一侧是个**无类型字符串空间**(`still_failing` 按 `str(source['id'])`
    比),带前缀存进去等于换了个键:同一条订阅留下 `id:63:error` 和 `63:ok` 两格,
    页面于是既说这一步还欠着、又说没有可补的来源。所以凡是可能变成账本键的那几个
    值都过这儿(`FeedCollector.stable_targets` 是那道边界)。
    """
    text = str(target)
    for prefix in (ID_PREFIX, NAME_PREFIX):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def in_scope(source: dict, targets) -> bool:
    """这条订阅在不在这一次的范围里。空范围 = 全都在。

    **这是范围匹配的唯一一份口径**,`sync.collect`(真去抓哪几条)和
    `FeedCollector.status(targets=...)`(这几条现在抓不抓得动)都过它 —— 两边
    岔开的后果是**用一批订阅的就绪去替另一批放行**:名字匹配那一半认不出来的话,
    「重试这一条」会拿整条通道的就绪当答案,而那一条必定再失败一次。

    **它是「选源方向」的口径。** 这一侧宽一点 = 多抓一条,代价是重抓一次;而
    **记账方向**(「这一轮真跑成了谁」那份回执)宽一点 = 拿一次没发生的成功清掉
    另一条订阅的真失败,那条订阅从此不进重试计划 —— 静默丢数据。所以**成功回执
    不许过这儿**(形态和那一侧的口径都在 `pipeline_results.cover`)。

    **但「宽」的额度只在「判不准」那一档上。** 类型写明了就不是判不准 —— 那是
    用户已经做出的精确选择,多抓一条就是跑了他明确没选的那一条(第 1 轮自由审查
    R1:面板上只勾了一条,两条都被抓)。所以带类型的那两档一律**只按那一种比**。

    三种写法:

    - `id:11` —— **明确点名编号**(`id_target()`),**只认编号**。单条同步、订阅页
      那颗同步、重试收窄都走这个形态。真有订阅叫 `id:11` 也不认 —— 要点名它得用
      名称目标(`name:id:11`),那才是「我点的是这个名字」。
    - `name:某博客` —— **明确点名名字**(`name_target()`),**只认名字**。面板上的
      订阅名单是按名字给的(`/api/pipeline/accounts` 只给名字),一行代表的就是
      **同名的那几条**,勾它的意思是它们都跑。
    - `"11"` —— **老形态**,id 和 name 两种都认。存量的历史轮次、日账名单里存着这种
      裸字符串,缓存着的老页面也还在发它,照旧要能匹配上。新请求一个都不许走这儿
      (类型在请求入口就打上:`kb.collectors.TargetTyping`)。

    判「是哪一种写法」用的是前缀本身,不是 `target_source_id()` 的真值 —— 那两个
    解析口径答的是「载荷是什么」,载荷为空串时真值会把「带了类型」说成「没带」,于是
    悄悄落回老形态那一档去比名字。
    """
    if not targets:
        return True
    sid, name = str(source['id']), source['name']
    for target in targets:
        text = str(target)
        if text.startswith(ID_PREFIX):
            if text[len(ID_PREFIX):] == sid:
                return True
        elif text.startswith(NAME_PREFIX):
            if text[len(NAME_PREFIX):] == name:
                return True
        elif sid == text or name == text:
            return True
    return False
