# -*- coding: utf-8 -*-
"""回执上那两个身份怎么读:**稳定内容身份**和**发现它的那一次抓取**。

两头各有一份形状:盘上那份账本里是 `[[内容身份, 抓取身份], …]`(`_pairs`),
进程内是采集器交回来的 `kb.collectors.Fetched` 三元组(`_identity`)。两头的
「什么叫读得懂」只有一份(`text_field`),所以单放一个模块 —— 投影层
(`kb.fetch_fold`)、落盘协议(`kb.fetch_rounds`)和公众号那个采集器
(`kb.collectors.wechat_local`)都从这里导,免得几处各判一遍。

改这里之前先读 `docs/decisions/daily.md`:为什么路径当不了内容身份、为什么空串
是「说不清」而不是「不是这一次」,各有一次实测事故。
"""
from __future__ import annotations

def text_field(value, *, missing_ok: bool = True) -> str | None:
    """协议里一个文本字段的**唯一判据**:读得懂给它,读不懂给 `None`。

    读不懂的处理由调用方决定(整份作废 / 记一笔缺口 / 报不可确认),但「什么叫读得
    懂」只有这一份:**不是字符串就是读不懂,不许 `str()` 一下糊过去。**
    `str(None)` 是 `'None'`、`str({...})` 是 `"{'a': 1}"` —— 都是**非空**字符串,
    当抓取身份用时会被归进「确定不是这一轮抓的」(`carried`),当路径用时不在取材
    范围里、被当成正常筛掉。两样都把一个缺口伪装成确定答案,而那正是整条链上最该
    拦住的东西。

    `missing_ok`(默认):字段没给(`None`)算空串 —— 空串是「说不清」,本来就是个
    合法答案(存量内容、手工导入的链接、还没记过的那一轮)。要求字段必须在场时传
    `False`,那时 `None` 也是读不懂(`Fetched` 三元组就是这一档:它的字段标注是
    `str`,给个 `None` 是生产者自己的 bug,不是「说不清」)。

    消费方:`_identity`(进程内的三元组)、`_pairs`(盘上那份账本)、
    `kb.collectors.wechat_local._receipt`(子进程交上来的 JSON)。采集器那侧的
    `collector/cli.py` 的 `cmd_export` 另有一份同口径的副本 —— 它是独立进程里的
    另一个包,导不过来,改这里记得同步改它。
    """
    if value is None:
        return '' if missing_ok else None
    if not isinstance(value, str):
        return None
    return value


def _pairs(obj: dict) -> list[tuple[str, str]] | None:
    """一条回执行里的 `(内容身份, 抓取身份)`。读不出来返回 None(= 记不全)。

    认两种形状:`items` 是 `[[内容身份, 抓取身份], …]`,`paths` 是旧形状的
    `[路径, …]` —— 后者的抓取身份一律空串,因为那时还没记过它。
    """
    if 'items' in obj:
        raw = obj['items']
        if not isinstance(raw, list):
            return None
        out = []
        for one in raw:
            if not isinstance(one, (list, tuple)) or not one:
                return None
            cid = text_field(one[0], missing_ok=False)
            fid = text_field(one[1]) if len(one) > 1 else ''
            if not cid or fid is None:
                return None
            out.append((cid, fid))
        return out
    raw = obj.get('paths')
    if not isinstance(raw, list):
        return None
    out = []
    for x in raw:
        one = text_field(x, missing_ok=False)
        if one is None:
            return None     # 盘上这一行被改坏了:整行读不出来,报不可确认
        if one:
            out.append((f'path:{one}', ''))
    return out


def _identity(one) -> tuple[str, str, str]:
    """一条回执成果的 `(内容身份, 抓取身份, 路径)`。

    只认 `kb.collectors.Fetched` 那个三元形状。**裸路径也照收**(身份退成路径
    自己、抓取身份说不清):一个还按旧契约返回路径集合的采集器(第三方的、
    没跟着升的)交上来的清单,拒掉就是把真落进库的那几篇静默少记成确定值,
    而收下来只损失那句分解 —— 条数照旧确定,`unsure` 那一档如实说「说不清是
    哪一次抓到的」。

    形状读不出来时返回三个空串,**由调用方记成这一轮的缺口**(`note_new` 的
    `torn`),不在这里悄悄吞掉。
    """
    if isinstance(one, str):
        return f'path:{one}', '', one
    try:
        cid, fid, path = one
    except (TypeError, ValueError):
        return '', '', ''
    # 字段不是字符串就算读不懂(为什么不许 `str()` 糊过去见 `text_field`)。
    # 这里要求字段必须在场:`Fetched` 的三个字段都标注成 `str`,给个 `None` 是
    # 生产者自己的 bug,不是「说不清」。
    got = [text_field(x, missing_ok=False) for x in (cid, fid, path)]
    if any(x is None for x in got):
        return '', '', ''
    return got[0], got[1], got[2]
