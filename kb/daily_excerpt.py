# -*- coding: utf-8 -*-
"""日报素材没有摘要时,拿正文开头顶上 —— 开头是 HTML 块的那些先按 Markdown 结构取字。

`summary` 是提取阶段写的,而大多数订阅条目没开条目化提取(`extract_optin`),送进
模型的就只剩正文开头。正文开头原来取自 chunk,而 chunk 是给检索切的:
`chunker.strip_markup` 只剥 md 语法,README 开头那一串 HTML(居中块、徽章、`<img>`)
原样留着,**连 `>` 都被它剥掉了**,标签只剩半截。GitHub 仓库的正文换成 README 之后
(`markdown_source.enrich`)这是常态:实测 GitHub Trending 一条的头一个 chunk 就是
一行 `<div align="center"`,模型拿到的「摘要」只有这一行,只能凭仓库名猜它是干什么的。

**哪些字算标记,由 Markdown 的结构说了算,不靠任何「长得像标签」的正则。** 两轮按
正则猜标签,每一轮都有一类技术正文被当成标记删掉(`std::vector<int>` → `<i`,
`Box<U>` → 一个完整的 `<U>`,反引号里的 `__init__` 被当成强调)—— 名单和边界再怎么
收也收不住,因为「`<U>` 是不是标签」在正则那一层本来就没有答案。CommonMark 有答案:

- **HTML 块**(一行以块级标签或独占一行的完整标签开头,规范里的 7 种起手式)是 HTML,
  交给 `fetching.plain` 这个真解析器取字 —— 和 GitHub 把 README 渲染出来给人看的是
  同一份文字。块里的写法照 HTML 的规矩解释:`<b && y>` 在 HTML 块里就是一个 `<b>`
  开始标签,浏览器和 GitHub 也都这么显示。**取出来的字就是最终文字,不许再当 Markdown
  解析一遍**:`<pre><code>std::vector&lt;unsigned int&gt;</code></pre>` 解出来的
  `std::vector<unsigned int>`、代码里写给人看的 `![badge](x.svg)`,再过一遍就会被当成
  标签和图片删掉(审查实测)。规范里 HTML 块的内容本来就不按 Markdown 处理,块里夹的 md
  徽章 GitHub 也是原样显示的,这里同样原样留着。
- **段落里的字原样保留**:行内代码去掉反引号、内容一个字不动,强调的 `**` / `__` 照原样
  留着(`__init__` 不会被改成 `init`)。只拿掉图片(徽章)和链接的地址,链接文字留着。
  行内的 `<...>` 只有**解析器认得出是标签、而且带着属性**的才拿掉(`<a href="…">`、
  `<img src="…"/>`,外加和它配对的那个闭合标签)和 HTML 注释;不带属性的一律当字面
  文字 —— `Box<U>` 和 `<u>` 在这一层长得一模一样,没有证据就不删。
- 代码块原样保留。

**只有原文开头真有 HTML 块的才走这条路**,别的一律原样取头一个 chunk(连换行都不动):
摘要进素材指纹,把干净的也重排一遍,升级那一刻今天带走的几百条全都换了指纹,面板
凭空报出一个「再生成能多带进来」的数。
"""
from __future__ import annotations

import re
from pathlib import Path

from markdown_it import MarkdownIt

from kb.feeds.fetching import plain
from kb.feeds.translate import NOTE
from kb.scanner import parse_frontmatter

RAW = 20000         # 最多读原文开头多少字符去凑摘要
PEEK = 4000         # 判「开头有没有 HTML 块」只看这么长

_MD = MarkdownIt('commonmark', {'html': True})
# 行内标签:解析器已经按规范认定它是一个标签,这里只看它带没带属性 / 是不是闭合标签。
_ATTRIBUTED = re.compile(r'<([A-Za-z][A-Za-z0-9-]*)\s+\S')
_CLOSING = re.compile(r'</([A-Za-z][A-Za-z0-9-]*)\s*>')
# 译文开头那一行说明(是个引用块,取出来的字里不带 `>`)。
_NOTE = NOTE.lstrip('> ').strip()


def _inline(token) -> str:
    """段落/标题里的字:原样留着,只拿掉图片和链接地址。"""
    out = []
    dropped = []                      # 拿掉了的开始标签名,等它的闭合标签
    for child in token.children or ():
        if child.type == 'html_inline':
            raw = child.content
            opened = _ATTRIBUTED.match(raw)
            closed = _CLOSING.fullmatch(raw)
            if raw.startswith('<!--'):
                continue
            if opened:
                if not raw.rstrip().endswith('/>'):
                    dropped.append(opened.group(1).lower())
                continue
            if closed and closed.group(1).lower() in dropped:
                dropped.remove(closed.group(1).lower())
                continue
            out.append(raw)
        elif child.type in ('text', 'code_inline'):
            out.append(child.content)
        elif child.type in ('em_open', 'em_close', 'strong_open', 'strong_close'):
            out.append(child.markup)
        elif child.type in ('softbreak', 'hardbreak'):
            out.append('\n')
        # image 整个跳过(徽章);link_open / link_close 跳过,链接文字是它们中间的 text
    return ''.join(out)


def has_html_block(markdown: str) -> bool:
    return any(t.type == 'html_block' for t in _MD.parse(markdown))


def clean(markdown: str) -> str:
    """一段 vault 原文(md + 内嵌 HTML)→ 一行可读文字。"""
    parts = []
    for token in _MD.parse(markdown):
        if token.type == 'html_block':
            parts.append(plain(token.content))
        elif token.type == 'inline':
            parts.append(_inline(token))
        elif token.type in ('fence', 'code_block'):
            parts.append(token.content)
    lines = []
    for line in '\n'.join(parts).splitlines():
        line = ' '.join(line.split())
        if line and line != _NOTE:
            lines.append(line)
    return ' '.join(lines)


def _body(root: Path, path: str) -> str | None:
    """vault 里这一篇的正文开头(最多读 `RAW` 个字符),去掉 frontmatter 和导出时加的
    那行 `# 标题`。读不到是 None。"""
    try:
        with open(root / path, encoding='utf-8') as f:
            text = f.read(RAW)
    except (OSError, UnicodeDecodeError):
        return None
    body = parse_frontmatter(text)[1].lstrip('\n')
    if body.startswith('# '):
        body = body.split('\n', 1)[1] if '\n' in body else ''
    return body


def excerpt(conn, root: Path, path: str, limit: int) -> str:
    """这一篇正文开头的可读文字,最多 `limit` 个字符;一个字都没有就是空串。

    头一个 chunk 里一个 `<` 都没有的连文件都不用读;原文读不到、或者开头没有 HTML
    块的,原样照旧取头一个 chunk(旧口径),不编一份。
    """
    row = conn.execute('SELECT text FROM chunks WHERE path=? ORDER BY seq LIMIT 1',
                       (path,)).fetchone()
    first = (row[0] if row else '').strip()
    if '<' not in first:
        return first[:limit]
    body = _body(root, path)
    if body is None or not has_html_block(body[:PEEK]):
        return first[:limit]
    return clean(body)[:limit]
