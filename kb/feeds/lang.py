# -*- coding: utf-8 -*-
"""「这段文字要不要翻译」的判定。只看字符构成,不调模型。

从 `kb/feeds/registry.py` 拆出来(单文件 700 行上限)。**整段逐字搬过来、一个
判据都没动**;公开名字仍然从 `registry` 取(`reg.text_language`,五个模块和两组
回归都是那么用的,其中一组还打桩它),所以那边留一行 re-export —— registry 自己
的 `put_items` 也走那个模块属性,打桩照旧穿得过去。

它不碰数据库,也不认识订阅:判外文的口径只有这一份,翻译那一侧的约束在
`docs/decisions/translate.md`。
"""
from __future__ import annotations


# 非中文判定:中日韩字符占比低于这个数就当成"外文,要翻译"。纯英文条目是 0,
# 正常中文条目远高于它;夹杂大段代码/英文引文的中文条目也在它之上。
_CJK_SHARE = 0.12
# 夹着汉字的短文本:占比算不准(一个汉字 + 几个字母就能压到阈值以下),
# 这种一律不翻,留原样。
_SHORT_MIXED = 12


def text_language(*parts: str) -> str:
    """'zh' = 不用翻译;'en' = 外文(泛指非中文),要走翻译。

    只看字符构成,不调模型:翻译本身才花钱,判定不能也花钱。标题和正文合起来
    判一次——短标题单看常常判不准(全是产品名的标题一个汉字都没有)。

    **一个汉字都没有、又有字母,一律判外文,不设长度下限。** 这里原来有过一道
    "至少 6 个字母"的成本闸门,用意是别花钱去译「Bun 1.3」这种没什么可译的
    产品名;代价是「Hello」「Great」「Nice」这类 1-5 个字母的纯英文动态被
    **确定性地**标成中文、永远进不了翻译队列,而 social 那类来源的短动态整段
    都是这个形态(2026-09-30 复审实测)。产品名该保留原文,靠的是 prompt 里
    "产品名、公司名、人名、代码、命令、URL、缩写保留原文"那条译文规则——
    多花几个 token 换"库里不会躺着英文",而不是把英文冒充成中文。

    真正"判不准"的只有"有汉字但太短"那一档(占比一个汉字就能压穿),照旧留
    原样;一个字母都没有的(纯数字/表情/标点)本来也没有可译的东西。
    """
    text = ' '.join(p or '' for p in parts)
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿')
    letters = sum(1 for ch in text if ch.isalpha())
    if not letters:                       # 没字可判(纯数字/表情/标点)
        return 'zh'
    if not cjk:                           # 零汉字 + 有字母 = 不可能是中文
        return 'en'
    if letters < _SHORT_MIXED:            # 有汉字又太短:判不出来就别翻
        return 'zh'
    return 'zh' if cjk / letters >= _CJK_SHARE else 'en'
