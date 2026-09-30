"""日报正文里的出处编号(`[18]`、`[3][7]`、`[1、2]`)换成可点的 markdown 链接。

这段替换**写进文件**,不是渲染层的装饰:改错了就是把日报原文改坏 —— 页面、复制
全文、Obsidian 里翻回来,三处一起受影响。

所以"哪些片段不许动"按 markdown 的构造扫,不靠逐行正则。2026-09-30 连着两轮审查
各抓出一批合法写法被改坏,而正则每补一种就还剩下一种:第 1 轮是行内代码和已有
链接的文字,第 2 轮是跨行的代码跨度、单引号标题、引用式链接、两层嵌套的括号目标,
外加「见到三个反引号就翻转一次」认错的多层围栏。补到第三轮也一样 —— 逐行正则
原理上就表达不了「跨行」和「任意深度嵌套」。

扫描器只认三类会把 `[1]` 吞进去的构造:围栏代码块、代码跨度、链接/图片(行内式、
引用式,连同它们的定义行)。其余构造(强调、原始 HTML、autolink)碰不到方括号,
不必管。规则按 CommonMark,和前端渲染日报用的 marked 是同一套口径。

**缩进四空格的代码块故意不认**:日报正文以列表为主,列表项的续行本来就缩进四格,
按代码块保护会让那些行里的出处全部漏掉链接 —— 那是比误改更常见的形态。
"""
from __future__ import annotations

import re

# 出处编号:`[18]` / `[1, 2]` / `[1，2]` / `[1、2]`。口径和前端 daily.js 的
# DAILY_REF_TEXT 同一份 —— 两边不一样的话,同一种写法在新旧日报上一个能点一个不能。
_NUMS_RE = re.compile(r'\[(\d{1,3}(?:\s*[,，、]\s*\d{1,3})*)\]')
_SEP_RE = re.compile(r'[,，、]')
_FENCE_RE = re.compile(r'^( {0,3})(`{3,}|~{3,})(.*)$')
_DEF_RE = re.compile(r'^ {0,3}\[(?P<label>(?:[^\[\]\\]|\\.)+)\]:[ \t]*(?=\S)')
_SPACE = ' \t'


def _run(text: str, i: int, ch: str) -> int:
    """从 i 起连着几个 ch。"""
    j = i
    while j < len(text) and text[j] == ch:
        j += 1
    return j - i


def _blank_after_newline(text: str, j: int, hi: int) -> bool:
    """text[j] 是换行时,它后面那一行是不是空行。

    代码跨度和链接文字都不能跨空行(跨了就是两个段落),拿它当搜索的终点,
    免得一个落单的反引号把后面整篇正文都吞进"代码"里。
    """
    k = j + 1
    while k < hi and text[k] in _SPACE:
        k += 1
    return k >= hi or text[k] == '\n'


def _code_span(text: str, i: int, hi: int) -> int:
    """text[i] 是反引号时,返回这段代码跨度的结束位置;不成立返回 i。

    CommonMark:N 个反引号由**恰好** N 个反引号关闭,中间可以换行。
    所以「``code `x` items[1]``」是一整段,而落单的反引号只是个普通字符。
    """
    n = _run(text, i, '`')
    j = i + n
    while j < hi:
        if text[j] == '`':
            m = _run(text, j, '`')
            if m == n:
                return j + m
            j += m
            continue
        if text[j] == '\n' and _blank_after_newline(text, j, hi):
            return i
        j += 1
    return i


def _brackets(text: str, i: int, hi: int) -> int:
    """text[i] 是 `[` 时,返回配对的 `]` 之后的位置;配不上返回 -1。

    方括号可以嵌套(`[版本 [1]](…)` 的文字里就有一层),转义的 `\\[` 不算,
    代码跨度整段跳过(`[见 `a[1]` 的说明](…)`)。
    """
    depth, j = 0, i
    while j < hi:
        c = text[j]
        if c == '\\':
            j += 2
            continue
        if c == '`':
            end = _code_span(text, j, hi)
            j = end if end > j else j + _run(text, j, '`')
            continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return j + 1
        elif c == '\n' and _blank_after_newline(text, j, hi):
            return -1
        j += 1
    return -1


def _skip_ws(text: str, k: int, hi: int) -> int:
    while k < hi and text[k] in ' \t\n':
        k += 1
    return k


def _inline_tail(text: str, j: int, hi: int) -> int:
    """text[j] 是 `(` 时,吃掉 `(目标 "标题")` 这一段,返回它之后的位置;不成立返回 -1。

    目标有两种写法:`<…>` 包起来,或者裸写 —— 裸写时圆括号只要**成对**就还在目标
    里,深度不限(`https://example.org/paper(a(b))` 是一个合法地址)。标题三种引法
    都算(`"…"` / `'…'` / `(…)`),只认双引号会把单引号标题的链接拆掉。
    """
    k = _skip_ws(text, j + 1, hi)
    if k < hi and text[k] == '<':
        end = text.find('>', k, hi)
        if end < 0 or '\n' in text[k:end]:
            return -1
        k = end + 1
    else:
        depth = 0
        while k < hi:
            c = text[k]
            if c == '\\':
                k += 2
                continue
            if c in ' \t\n':
                break
            if c == '(':
                depth += 1
            elif c == ')':
                if depth == 0:
                    break
                depth -= 1
            k += 1
    k = _skip_ws(text, k, hi)
    if k < hi and text[k] in '"\'(':
        close = ')' if text[k] == '(' else text[k]
        k += 1
        while k < hi and text[k] != close:
            k += 2 if text[k] == '\\' else 1
        if k >= hi:
            return -1
        k = _skip_ws(text, k + 1, hi)
    return k + 1 if k < hi and text[k] == ')' else -1


def _norm_label(raw: str) -> str:
    return ' '.join(raw.split()).lower()


def _link(text: str, i: int, hi: int, labels: set[str]) -> int:
    """text[i] 是 `[` 或 `![` 时,返回整条链接/图片之后的位置;不是链接返回 i。

    三种写法都要认,漏掉哪种,那种写法的文字里的数字方括号就会被当成出处改写,
    一条链接当场裂成两个不同目的地:
      行内式 `[文字](目标)`、完整/折叠引用 `[文字][标签]` `[文字][]`、
      简写引用 `[标签]`(后两种得真有对应的定义行才算链接)。
    """
    start = i
    if text[i] == '!':
        i += 1
        if i >= hi or text[i] != '[':
            return start
    end_text = _brackets(text, i, hi)
    if end_text < 0:
        return start
    inner = text[i + 1:end_text - 1]
    if end_text < hi and text[end_text] == '(':
        tail = _inline_tail(text, end_text, hi)
        return tail if tail > 0 else start
    if end_text < hi and text[end_text] == '[':
        end_label = _brackets(text, end_text, hi)
        if end_label < 0:
            return start
        label = text[end_text + 1:end_label - 1].strip() or inner
        return end_label if _norm_label(label) in labels else start
    return end_text if _norm_label(inner) in labels else start


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """围栏代码块各占哪一段。

    记下开启的字符和长度,只有**同字符、不短于它**的围栏才关得掉。「见到三个以上
    反引号就翻转一次」会把四反引号围栏里那行三反引号当成结束:围栏中间的编号被
    改写,而真正结束之后的出处反倒漏掉(2026-09-30 第 2 轮审查的不拦项)。
    反引号围栏的信息串里不能再有反引号,否则那行压根不是围栏。
    """
    spans: list[tuple[int, int]] = []
    pos, open_at, char, size = 0, -1, '', 0
    for line in text.split('\n'):
        nxt = pos + len(line) + 1
        m = _FENCE_RE.match(line)
        if open_at < 0:
            if m and (m.group(2)[0] == '~' or '`' not in m.group(3)):
                open_at, char, size = pos, m.group(2)[0], len(m.group(2))
        elif (m and m.group(2)[0] == char and len(m.group(2)) >= size
                and not m.group(3).strip()):
            spans.append((open_at, min(nxt, len(text))))
            open_at = -1
        pos = nxt
    if open_at >= 0:        # 没关上的围栏一直管到结尾,CommonMark 就是这么定的
        spans.append((open_at, len(text)))
    return spans


def _gaps(spans: list[tuple[int, int]], n: int):
    pos = 0
    for lo, hi in spans:
        if lo > pos:
            yield pos, lo
        pos = hi
    if pos < n:
        yield pos, n


def _definitions(text: str, fenced: list[tuple[int, int]]) -> tuple[set[str], list[tuple[int, int]]]:
    """收集链接引用定义(`[product]: https://…`)的标签,并把这些行一起保护起来。

    引用式链接要靠它才认得出来;定义行自己也不能被改写 —— 标签是数字时
    (`[1]: https://…`)整行就是一条定义,把它换成链接会把定义弄没。
    """
    labels: set[str] = set()
    spans: list[tuple[int, int]] = []
    blocked = [(lo, hi) for lo, hi in fenced]
    pos = 0
    for line in text.split('\n'):
        nxt = pos + len(line) + 1
        if not any(lo <= pos < hi for lo, hi in blocked):
            m = _DEF_RE.match(line)
            if m:
                labels.add(_norm_label(m.group('label')))
                spans.append((pos, min(nxt, len(text))))
        pos = nxt
    return labels, spans


def protected_spans(text: str) -> list[tuple[int, int]]:
    """正文里**一个字都不许改**的区间,按起点排序、互不重叠。"""
    fenced = _fenced_spans(text)
    labels, defs = _definitions(text, fenced)
    spans = sorted(fenced + defs)
    out: list[tuple[int, int]] = []
    for lo, hi in spans:                 # 定义行可能落在围栏里,合掉重叠
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    found = list(out)
    for lo, hi in _gaps(out, len(text)):
        i = lo
        while i < hi:
            c = text[i]
            if c == '\\':
                i += 2
            elif c == '`':
                end = _code_span(text, i, hi)
                if end > i:
                    found.append((i, end))
                    i = end
                else:
                    i += _run(text, i, '`')
            elif c == '[' or (c == '!' and text.startswith('![', i)):
                end = _link(text, i, hi, labels)
                if end > i:
                    found.append((i, end))
                    i = end
                else:
                    i += 1
            else:
                i += 1
    found.sort()
    return found


def link_refs(body: str, links: dict[str, str]) -> str:
    """把正文里的出处编号换成 `links` 里那条链接。

    `links` 是「编号 → 整条 markdown 链接文本」,由调用方拼好(转义、尖括号目标
    那些口径在 `kb.daily`)。编号对不上素材(模型偶尔会编一个超出范围的)、那条
    素材没有 url、或者整组编号一个都对不上时原样留着,不编一个链接出来。
    """
    if not links:
        return body
    spans = protected_spans(body)

    def _sub(m) -> str:
        nums = [n.strip() for n in _SEP_RE.split(m.group(1))]
        # 一个都对不上就整段别动:那多半不是出处编号(年份区间、条目序号之类)。
        if not any(n in links for n in nums):
            return m.group(0)
        return ''.join(links.get(n) or f'[{n}]' for n in nums)

    out, pos = [], 0
    for lo, hi in spans:
        out.append(_NUMS_RE.sub(_sub, body[pos:lo]))
        out.append(body[lo:hi])
        pos = hi
    out.append(_NUMS_RE.sub(_sub, body[pos:]))
    return ''.join(out)
