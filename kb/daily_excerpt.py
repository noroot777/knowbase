# -*- coding: utf-8 -*-
"""日报素材没有摘要时,拿正文开头顶上 —— 带着 HTML 的那些先剥成可读文字。

`summary` 是提取阶段写的,而大多数订阅条目没开条目化提取(`extract_optin`),送进
模型的就只剩正文开头。正文开头原来取自 chunk,而 chunk 是给检索切的:
`chunker.strip_markup` 只剥 md 语法,README 开头那一串 HTML(居中块、徽章、`<img>`)
原样留着,**连 `>` 都被它剥掉了**,标签只剩半截。GitHub 仓库的正文换成 README 之后
(`markdown_source.enrich`)这是常态:实测 GitHub Trending 一条的头一个 chunk 就是
一行 `<div align="center"`,模型拿到的「摘要」只有这一行,只能凭仓库名猜它是干什么的。

**剥标签不许在 chunk 上做。** 没了 `>` 之后「这是不是标签」已经判不出来:`<h1>kimi`
成了 `<h1kimi`,而 `std::vector<int>` 也成了 `std::vector<int` —— 想认出前者就得放开
标签名的边界,放开边界就把 `<int` 当成 `<i` 剥掉(审查实测 `std::vector nt`)。所以
要剥的那些回到 vault 里的原文去读,那儿标签是完整的:标签名在一份 HTML 名单里、后面
紧跟边界、**有右尖括号**,三样都对上才算标签。md 语法也不照搬
`chunker.strip_markup`:它把**所有** `>` 和 `_` 都剥掉(检索不在乎),而摘要要原样
交给模型 —— 图片和链接借它那两条正则,引用和标题只剥行首的标记,强调只剥成对的符号。
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from kb.chunker import _IMG_RE, _LINK_RE
from kb.feeds.translate import NOTE
from kb.scanner import parse_frontmatter

RAW = 20000         # 最多读原文开头多少字符去凑摘要
PEEK = 4000         # 判「开头带不带 HTML」只看这么长

_TAGS = ('a', 'abbr', 'b', 'big', 'blockquote', 'br', 'center', 'code', 'dd', 'del',
         'details', 'div', 'dl', 'dt', 'em', 'figcaption', 'figure', 'font', 'h1', 'h2',
         'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'ins', 'kbd', 'li', 'ol', 'p', 'picture',
         'pre', 's', 'small', 'source', 'span', 'strike', 'strong', 'sub', 'summary', 'sup',
         'svg', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'tt', 'u', 'ul', 'video')
_TAG = re.compile(r'</?(?:' + '|'.join(_TAGS) + r')(?![A-Za-z0-9-])(?:\s[^<>]*)?/?>', re.I)
_COMMENT = re.compile(r'<!--.*?-->', re.S)
_LEAD = re.compile(r'^\s*(?:>\s*)*(?:#{1,6}\s+)?')     # 行首的引用 / 标题标记
_EMPHASIS = re.compile(r'\*\*|__|`+')
# 译文开头那一行说明。
_NOTE = NOTE.lstrip('> ').strip()


def noisy(text: str) -> bool:
    """这一段里有没有完整的 HTML 标签或注释。"""
    return bool(_COMMENT.search(text) or _TAG.search(text))


def clean(markdown: str) -> str:
    """一段 vault 原文(md + 内嵌 HTML)→ 一行可读文字。"""
    text = _TAG.sub(' ', _COMMENT.sub(' ', markdown))
    text = _EMPHASIS.sub('', _LINK_RE.sub(r'\1', _IMG_RE.sub(' ', text)))
    lines = []
    for line in html.unescape(text).splitlines():
        line = ' '.join(_LEAD.sub('', line).split())
        if line and line != _NOTE:
            lines.append(line)
    return ' '.join(lines)


def _body(root: Path, path: str) -> str | None:
    """vault 里这一篇的正文开头,去掉 frontmatter 和导出时加的那行 `# 标题`。读不到是 None。"""
    try:
        text = (root / path).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    body = parse_frontmatter(text)[1].lstrip('\n')
    if body.startswith('# '):
        body = body.split('\n', 1)[1] if '\n' in body else ''
    return body[:RAW]


def excerpt(conn, root: Path, path: str, limit: int) -> str:
    """这一篇正文开头的可读文字,最多 `limit` 个字符;一个字都没有就是空串。

    **头一个 chunk 本来就干净的,原样照旧取它**(连换行都不动)。素材的指纹
    (`daily_gain.material_sig`)覆盖摘要,把干净的那些也重排一遍,升级那一刻今天
    已经带走的几百条全都换了指纹,面板凭空报出一个「再生成能多带进来」的数 ——
    而模型拿到的东西实质上没变。只有原文开头真带着 HTML 的那些才换一份输入;chunk
    里一个 `<` 都没有的连文件都不用读。原文读不到也退回 chunk(旧口径),不编一份。
    """
    row = conn.execute('SELECT text FROM chunks WHERE path=? ORDER BY seq LIMIT 1',
                       (path,)).fetchone()
    first = (row[0] if row else '').strip()
    if '<' not in first:
        return first[:limit]
    body = _body(root, path)
    if body is None or not noisy(body[:PEEK]):
        return first[:limit]
    return clean(body)[:limit]
