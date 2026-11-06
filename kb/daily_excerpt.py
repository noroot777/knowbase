# -*- coding: utf-8 -*-
"""日报素材没有摘要时,拿正文开头顶上 —— 先把标记噪音剥掉。

`summary` 是提取阶段写的,而大多数订阅条目没开条目化提取(`extract_optin`),送进
模型的就只剩正文开头。正文开头取自 chunk,而 chunk 是给检索用的:
`chunker.strip_markup` 只剥 md 语法,README 开头那一串 HTML(居中块、徽章、`<img>`)
原样留着,**连 `>` 都被它剥掉了**,标签只剩半截。GitHub 仓库的正文换成 README 之后
(`markdown_source.enrich`)这是常态:实测 GitHub Trending 一条的头一个 chunk 就是
一行 `<div align="center"`,模型拿到的「摘要」只有这一行,只能凭仓库名猜它是干什么的。

所以这里**不截头一个 chunk 了事**:按顺序读前几个 chunk、逐个剥掉标签,攒够上限为止。
剥标签只认一份有名有姓的 HTML 标签名单,而且**区分大小写** —— 正文里的 `List<T>`、
`Vec<String>` 不是标签,不该被吃掉。chunk 里没有 `>`,所以 `<h1>kimi` 到这儿成了
`<h1kimi`:标签名后面紧跟的就是正文,只能按名单里**最长的那个名字**认(`<sub`
不能先被 `<s` 认走)。属性必须以空白起头,只能吃到下一个 `<` 或行尾为止:同一行标签
后面紧跟的正文会跟着丢一截,对一段只拿来给模型看个大意的摘要来说可以接受。
"""
from __future__ import annotations

import html
import re

from kb.feeds.translate import NOTE

CHUNKS = 3          # 最多往后读几个 chunk 去凑摘要

_TAGS = ('a', 'abbr', 'b', 'big', 'blockquote', 'br', 'center', 'code', 'dd', 'del',
         'details', 'div', 'dl', 'dt', 'em', 'figcaption', 'figure', 'font', 'h1', 'h2',
         'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'ins', 'kbd', 'li', 'ol', 'p', 'picture',
         'pre', 's', 'small', 'source', 'span', 'strike', 'strong', 'sub', 'summary', 'sup',
         'svg', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'tt', 'u', 'ul', 'video')
_TAG = re.compile('</?(?:' + '|'.join(sorted(_TAGS, key=len, reverse=True))
                  + r')(?:\s[^<>\n]*)?/?>?')
# md 图片(徽章)整个去掉,链接留文字。chunker 那一道认不全:徽章常是 data: 地址或
# 套在链接里的图片(`[![许可证](徽章)](LICENSE)`),长地址还会被 chunk 从中间切断,
# 所以右括号可以没有(吃到行尾)。HTML 注释同理没了 `>`,只认到 `--` 为止。
_IMAGE = re.compile(r'!\[[^\]\n]*\]\([^)\n]*\)?')
_LINK = re.compile(r'\[([^\]\n]*)\]\([^)\n]*\)?')
_COMMENT = re.compile(r'<!--.*?--', re.S)
# 一长串不带空白的 ASCII:徽章的 base64、被 chunk 切到下一段的长地址。只认 ASCII ——
# 中文正文本来就不带空格,按 `\S` 判会把整段中文当成噪音吃掉。
_RUN = re.compile(r'[!-~]{60,}')
# 译文开头那一行说明(chunk 里它的 `>` 已经被剥掉了)。
_NOTE = NOTE.lstrip('> ').strip()


def clean(text: str) -> str:
    """一段 chunk → 一行可读文字。标签、实体、译文说明行和空行都去掉。"""
    lines = []
    text = _TAG.sub(' ', _COMMENT.sub(' ', text or ''))
    text = _RUN.sub(' ', _LINK.sub(r'\1', _IMAGE.sub(' ', text)))
    for line in html.unescape(text).splitlines():
        line = ' '.join(line.split())
        if line and line != _NOTE:
            lines.append(line)
    return ' '.join(lines)


def noisy(text: str) -> bool:
    """这一段里有没有标记噪音。译文说明那一行不算:它只占几十个字,为它换掉所有
    译文素材的指纹不值(理由见 `from_chunks`);真要清理的那些顺手把它一起去掉。"""
    return any(r.search(text) for r in (_COMMENT, _TAG, _IMAGE, _LINK, _RUN))


def from_chunks(conn, path: str, limit: int) -> str:
    """这一篇正文开头的可读文字,最多 `limit` 个字符;一个字都没有就是空串。

    **头一个 chunk 本来就干净的,原样照旧取它**(连换行都不动)。素材的指纹
    (`daily_gain.material_sig`)覆盖摘要,把干净的那些也重排一遍,升级那一刻今天
    已经带走的几百条全都换了指纹,面板凭空报出一个「再生成能多带进来」的数 ——
    而模型拿到的东西实质上没变。只有带着标记噪音的那些才真换了一份输入。
    """
    rows = [text for (text,) in conn.execute(
        'SELECT text FROM chunks WHERE path=? ORDER BY seq LIMIT ?', (path, CHUNKS))]
    if not rows:
        return ''
    if not noisy(rows[0]):
        return rows[0].strip()[:limit]
    out = ''
    for text in rows:
        piece = clean(text)
        if piece:
            out = f'{out} {piece}' if out else piece
        if len(out) >= limit:
            break
    return out[:limit]
