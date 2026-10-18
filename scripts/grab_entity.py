# -*- coding: utf-8 -*-
r"""字符引用(`&amp;`、`&#40;`)**按它所在的那套语法**解码。

从 `grab_md.py` 拆出来的(单文件要过 700 行的上限)。这一份只管「这几个字符表示
什么」,不管结构在哪儿断。

**Markdown 和 HTML 是两套规则,差别落在地址上就是另一篇文章**,所以两个出口、
各有自己的红:

| | Markdown(CommonMark) | HTML 属性值 |
|---|---|---|
| 分号 | **必须有** | 数字引用**可选**(`&#47B` = `/B`) |
| 认不出的名字 | **原样保留**(`&notanentity;` 不是实体) | 按**最长匹配**退一步(`&not` + `anentity;`) |
| 缺分号的名字 | 不是实体 | HTML5 的历史特例:后面紧跟 `=` 或 ASCII 字母数字就**不解码** |
| 反斜杠转义 | 有(在 `grab_md` 那边同一遍里解) | 没有 |

**两边都不能图省事一把 `html.unescape`** —— 它是「一般文本」那一份:对 Markdown
太宽(连 `&amp` 都解、`&notanentity;` 解成 `¬anentity;`),对属性值又缺了那个历史
特例(把 `&ampb` 解成 `&b`)。表取标准库的 `html.entities.html5`,不自己抄一份。
"""
from __future__ import annotations

import html
import html.entities
import re

#: Markdown 的字符引用(CommonMark「Entity and numeric character references」)。
#: **只认带分号的严格写法**,而且名字必须**真的在 HTML5 表里** —— 形状对就解的
#: 那一版实测:`&notanentity;` 被 `html.unescape` 按 `&not` 前缀解成
#: `¬anentity;`,`[原文](https://a/?q=&notanentity;)` 于是发出另一个地址、真落库
#: (CommonMark 对认不出的实体是**原样保留**,markdown-it 也一样)。
_MD_NUM = re.compile(r'&#(?:\d{1,7}|[xX][0-9a-fA-F]{1,6});')
_MD_NAME = re.compile(r'&[a-zA-Z][a-zA-Z0-9]{1,31};')
#: HTML 属性值里的数字引用:**分号可选**(缺分号在 HTML5 里只是个 parse error)。
_HTML_NUM = re.compile(r'&#(?:[Xx][0-9A-Fa-f]{1,6}|[0-9]{1,7});?')
#: 属性值里命名引用的**候选串**(分号也可能不在):拿它去表里从长到短试前缀,
#: 最多三十几次字典查找 —— 不许拿整张表(两千多个键)去逐个 `startswith`。
_HTML_NAME = re.compile(r'&[A-Za-z][A-Za-z0-9]{0,31};?')


def md_entity(text: str, i: int) -> tuple[str, int] | None:
    """`text[i] == '&'` 处那个 **Markdown** 字符引用:`(它表示的字符, 之后那一格)`。
    不是一个合法实体就 `None` —— 那几个字符于是是**字面文字**,一个都不许改。
    """
    if m := _MD_NUM.match(text, i):
        return html.unescape(m.group()), m.end()
    if (m := _MD_NAME.match(text, i)) and m.group()[1:] in html.entities.html5:
        return html.unescape(m.group()), m.end()
    return None


def _html_named(raw: str, i: int) -> tuple[str, int] | None:
    """属性值里 `raw[i] == '&'` 处那个**命名**引用:`(它表示的字符, 这一段多长)`。

    按 HTML5 的 tokenizer:在表里做**最长匹配**(表里既有 `amp;` 也有 `amp`),
    命中的键不带分号时再看后面那一个字符 —— 紧跟 `=` 或 ASCII 字母数字就**不解码**
    (历史特例)。认不出、或者被特例拦住都返回 `None`:那个 `&` 是字面字符。
    """
    if not (m := _HTML_NAME.match(raw, i)):
        return None
    cand = m.group()[1:]
    for k in range(len(cand), 0, -1):
        if (name := cand[:k]) not in html.entities.html5:
            continue
        tail = raw[i + 1 + k:i + 2 + k]
        if not name.endswith(';') and (tail == '='
                                       or (tail.isascii() and tail.isalnum())):
            return None
        return html.entities.html5[name], 1 + k
    return None


def _html_num(raw: str, i: int) -> tuple[str, int] | None:
    """属性值里那个**数字**引用(分号可选)。"""
    if not (m := _HTML_NUM.match(raw, i)):
        return None
    # 分号补齐之后交给标准库(数字引用那几条 HTML5 的替换也在它那儿)。
    return html.unescape(m.group().rstrip(';') + ';'), m.end() - i


def html_value(raw: str) -> str:
    """**HTML 属性值**里的字符引用 → 它表示的字符(判据见模块说明那张表)。

    **判准是真浏览器**:拿无头 Chrome 读 `a.getAttribute('href')` 核过,我们和它
    一致;标准库的 `HTMLParser` 没实现那个历史特例(它一把 `unescape`),在
    `?a&ampb=1`、`&notit`、`a&amp=b` 三处和浏览器不同,**不能拿它当判准**。
    """
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        if raw[i] == '&' and (hit := _html_num(raw, i) or _html_named(raw, i)):
            out.append(hit[0])
            i += hit[1]
        else:
            out.append(raw[i])
            i += 1
    return ''.join(out)
