# -*- coding: utf-8 -*-
"""导出稿的形状:一条订阅内容写进 vault 那一篇 md,frontmatter 之外长什么样。

**单拎出来是因为有两个消费方**:`kb.feeds.sync._export` 真把它写下去,而订阅条目面板
要按它算出「我摊的这份正文对应盘上那一版是什么指纹」—— 面板那一头在 web 层
(`kb/web/intake_api.py`),从那儿 import `sync` 不合适(它还带着整条同步流水线),
而这段形状必须**只有一份定义**:各写一遍的话两个数永远不等,面板上那几截正文的
「收藏此段」就全退成站外原文(反向注入实测:差一个换行,红 11 条)。
"""
from __future__ import annotations

from kb.feeds import translate


def item_body_md(item) -> str:
    """这一行内容导出成 md 之后,**frontmatter 之外那一截全文**(含打头那个空行)。

    **口径只有这一份**,两个消费方都过它:导出真写下去的就是它(`_export` 里
    `text += item_body_md(item)` 那一句),而订阅条目面板要按它算出「我摊的这份正文
    对应盘上那一版是什么指纹」(`kb/web/intake_api.py` 的 `list_items` →
    `scanner.body_version_of`)。各写一遍的话两个数永远不等,面板上那几截正文
    的「收藏此段」就全退成站外原文(反向注入 N3 实测:差一个换行,红 11 条)。

    打头那个 `\n` 是正文的一部分:`scanner.parse_frontmatter` 的正则吃掉
    `---\n…---\n`,导出写的是 `'---\n' + yaml + '---\n'` 加这一截,于是盘上那一
    篇拆出来的正文逐字就是本函数的返回值 —— 版本号两头才对得上。
    """
    title, body = translate.display(item)
    # 标题被译了也算「这一篇是译文」(`_export` 写 frontmatter 时同款判断)
    is_zh = body != item['body'] or title != item['title']
    notice = '\n> 以下为订阅源提供的摘要；完整内容请打开原文。\n' if item['content_kind'] == 'summary' else ''
    if is_zh:
        notice += '\n' + translate.NOTE + '\n'
    return '\n# ' + title + '\n' + notice + '\n' + body + '\n'
