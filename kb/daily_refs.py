r"""日报正文里的出处编号(`[18]`、`[3][7]`、`[1、2]`)换成可点的 markdown 链接。

这段替换**写进文件**,不是渲染层的装饰:改错了就是把日报原文改坏 —— 页面、复制
全文、Obsidian 里翻回来,三处一起受影响。

**每一次替换由渲染器裁决,这里不写"哪些片段不许动"的规则。** 手写规则的路走过
三轮,每一轮都被真实渲染器找出新的漏网写法:逐行正则漏掉行内代码和已有链接的
文字(第 1 轮)、跨行代码跨度/单引号标题/引用式链接/嵌套括号目标(第 2 轮)、多行
定义/引用段里的定义/原始 HTML 的 `<a>` 与 `<code>`/独立缩进代码块/autolink 的
地址(第 3 轮)。**漏网写法的清单不会收敛**,因为它等于把一个 markdown 解析器
重写一遍。

裁决分两件事问,缺一不可:

**① 口径必须和页面那个渲染器一模一样。** 第 4 轮的两条都出在这里:后端按
CommonMark 裁决,而页面的 marked 默认开着 GFM —— 裸网址 `https://e.org/path/[1]`
在 CommonMark 眼里只是一段文字(于是放行),在页面上却是一条链接,替换完点过去
就是另一个地址;GFM 表格同理,CommonMark 压根不解析,单元格被标题里的 `|` 挤错位
也看不出来。所以这里用 `gfm-like`(linkify + 表格 + 删除线),和 marked 对齐。

**①′ 但"两个 markdown 实现"永远不可能逐位对齐,所以裸网址另有一道超集判据。**
第 5 轮实测:换上 `gfm-like` 之后 `www.example.dev/path/[1]` 照样被改坏 ——
linkify-it 只认一份有限的 TLD 名单,`dev`/`app`/`cloud`/`news` 之类都不在里面,
而 marked 对 TLD 不做任何限制。补几个后缀是在维护名单,下一个没见过的后缀照样
漏;path 边界、backpedal 这些地方还有同类缺口。所以除了把 TLD 放宽成"任意",
另加一道**不依赖任何解析器细节**的判据:marked 的 autolink 只有 `www.` 和
`scheme://` 两种起手式,尾巴是 `[^\s<]*`(不跨空白、不跨 `<`)—— 于是"这个编号落
在某条裸网址里"必然满足「往左数到最近的空白或 `<` 为止,这一段出现过 `www.`
或 `://`」。反过来不成立,所以它是超集:多拒几处(那一处留成纯文本)换"再不会有
下一个漏网的后缀"。

**② 判据是整份渲染结果逐字比,不是挑几类元素比。** 原来只收集代码/图片/链接,
于是"表格列错位、一整格内容丢了""不可见的引用定义被显示出来"这类改动全部漏过
——`_accepts` 嘴上说"这一处之外一个字都没动",实际只看了那几类。现在的做法:
把要换的地方先换成一个**哨兵链接**(私用区字符当链接文字,目标和标题跟真链接
一模一样),渲染之后把哨兵链接整个还原成 `[N]`,要求**和原文的渲染结果逐字相同**;
再确认真链接版和哨兵版只差链接文字那一处。两条都过,才谈得上"只把这一处变成了
链接"。另外单独拦一道:哨兵链接不许落在 `<a>`/`<code>`/`<pre>` 里面 —— 还原之后
那种情况也逐字相同(容器没变),可链接套链接在浏览器里会被拆开、代码里冒出链接
更是改坏了别人的命令。

判不准的一律不换 —— 那一处的编号留成纯文本,面板那边照素材清单还能补上
(`daily.js` 的 `enhanceDailyBody`),而改坏了的原文没有任何东西补得回来。
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

# 出处编号:`[18]` / `[1, 2]` / `[1，2]` / `[1、2]`。口径和前端 daily.js 的
# DAILY_REF_TEXT 同一份 —— 两边不一样的话,同一种写法在新旧日报上一个能点一个不能。
_NUMS_RE = re.compile(r'\[(\d{1,3}(?:\s*[,，、]\s*\d{1,3})*)\]')
_SEP_RE = re.compile(r'[,，、]')

# 和页面渲染日报的 marked 同一套口径:marked 默认开 GFM,所以这里也得有 linkify
# (裸网址成链接)和表格 —— 少一样,页面上被改坏的东西在这里就看着无害。
_MD = MarkdownIt('gfm-like')
# linkify-it 默认只认一份有限的 TLD 名单(com/org/net…),marked 却对 TLD 不做任何
# 限制 —— `www.example.dev/path/[1]` 因此在页面上是链接、在这里只是一段文字,替换
# 完点过去就是另一个地址(2026-09-30 第 5 轮审查实测,8 个后缀全中)。放宽成"任意
# 两字符以上的字母数字",让这里的识别范围**覆盖** marked 那一侧。不放到一字符是
# 因为那会把 `3.2`、`v1.5` 这类版本号也认成域名 —— 过度识别不改坏东西(只是那一
# 处不加链接),但没必要。真正兜底的是下面 `_autolinked` 那道超集判据。
_MD.linkify.tlds(r'[a-z0-9\-]{2,}', keep_old=False)

# marked 的 GFM autolink 只有两种起手式(`www.` 或 `scheme://`),尾巴是 `[^\s<]*`
# —— 不跨空白、不跨 `<`。所以「这个编号落在某条裸网址里」有一个**必要条件**:从它
# 往左数到最近的空白或 `<` 为止,这一段里出现过 `www.` 或 `://`。命中就不换。
# 这是超集判据,不依赖谁的 TLD 名单更全、谁的 path 正则更准 —— 上面那次放宽只是
# 让主裁决器别再明显窄于 marked,"不会再漏下一个没见过的后缀"靠的是这一道。
# **`re.I` 不能省**:marked 那条 url 规则带 `i` 标志,`WWW.` / `Www.` 照认。漏掉
# 大小写的话,起手式大写、后缀又不在后端 linkify 认得出的范围内时(实测
# `WWW.example.中国/path/[1]`、`WWW.x.y/path/[1]`),两道保护同时落空、原网址当场
# 被改坏(2026-10-01 第 6 轮审查实测)。`://` 本身没有大小写。
_URL_HEAD_RE = re.compile(r'www\.|://', re.I)

# 哨兵:私用区字符,日报正文里不会出现。拿它当链接文字,就能在渲染结果里**精确**
# 认出"哪几条是这一次新插进去的",不必靠 (文字, 地址) 去猜(原文里本来就有一条
# 一模一样的链接时,猜哪条都说不准)。
_S1, _S2 = '\ue000', '\ue001'
_SENT_RE = re.compile(f'{_S1}(\\d+)\\.(\\d+){_S2}')
# 同一组的哨兵在替换时是连着写下去的(`''.join`),所以「整组」= 组号相同的一串。
_GROUP_RE = re.compile(f'{_S1}(\\d+)\\.\\d+{_S2}(?:{_S1}\\1\\.\\d+{_S2})*')
# 新链接落进这几种元素里就是改坏了:链接套链接浏览器会拆开(点击目标错位),
# 代码里冒出链接则是把别人照着复制的命令改了。这两种情况还原之后照样逐字相同
# (容器本身没变),所以得单拦一道。
_FORBID = ('a', 'code', 'pre')


def _autolinked(body: str, at: int) -> bool:
    """这个位置有没有可能落在一条裸网址里面?(宁可多说是,见模块开头 ①′)"""
    i = at
    while i and not (body[i - 1].isspace() or body[i - 1] == '<'):
        i -= 1
    return _URL_HEAD_RE.search(body[i:at]) is not None


def _nums(m: re.Match) -> list[str]:
    return [n.strip() for n in _SEP_RE.split(m.group(1))]


def _md_link(text: str, dest: str, title: str) -> str:
    return f'[{text}]({dest} "{title}")' if title else f'[{text}]({dest})'


def _norm(html: str) -> str:
    """渲染结果的规范形式。两边都过一遍解析器再比,免得把 `<img …>` 与 `<img …/>`
    这种序列化差异当成"内容变了"(改完就是白白漏掉一条出处)。"""
    return str(BeautifulSoup(html, 'html.parser'))


def _splice(body: str, spots: list[tuple[int, re.Match]], text_of) -> str:
    """把 spots 这几处换掉。从后往前:先动靠后的,前面那些量出来的位置才还算数。"""
    out = body
    for i, m in reversed(spots):
        out = (out[:m.start()]
               + ''.join(text_of(i, k, n) for k, n in enumerate(_nums(m)))
               + out[m.end():])
    return out


def _accepts(base: str, body: str, spots: list[tuple[int, re.Match]],
             links: dict) -> str:
    """这几处换成链接之后,除了它们自己,渲染结果逐字不变吗?是就返回落盘的文本。"""
    want = sum(1 for _, m in spots for n in _nums(m) if n in links)
    raw = {i: m.group(0) for i, m in spots}
    num_at = {(i, k): n for i, m in spots for k, n in enumerate(_nums(m))}

    # 一组里没有素材的那几个编号也打上哨兵(只是不做成链接),还原时整组一起换回
    # 原文那一段。落下一个的话,`[1,9]` 会还原成 `[1,9][9]` 跟原文对不上,于是连
    # 有素材的那个编号也白白留成纯文本(2026-09-30 第 5 轮审查实测)。
    shown = _MD.render(_splice(body, spots, lambda i, k, n: (
        _md_link(f'{_S1}{i}.{k}{_S2}', *links[n]) if n in links
        else f'{_S1}{i}.{k}{_S2}')))

    soup = BeautifulSoup(shown, 'html.parser')
    got = 0
    for a in soup.find_all('a'):
        if not _SENT_RE.fullmatch(a.get_text()):
            continue
        if a.find_parent(_FORBID):      # 落进了链接或代码里面
            return ''
        got += 1
        a.replace_with(a.get_text())    # 只脱掉这层 <a>,哨兵文本留在原处
    if got != want:                     # 有的没成链接(在代码里、在 HTML 块里…)
        return ''
    # 整组哨兵换回原文那一段:合并写法 `[1、2]` 换出来是几个相邻元素,一个个还原成
    # `[1][2]` 就跟原文对不上了 —— 那个分隔符本来就该消失。
    if _GROUP_RE.sub(lambda mm: raw[int(mm.group(1))], str(soup)) != base:
        return ''                       # 别处也跟着变了(表格挤错列、定义行冒出来…)

    real = _splice(body, spots, lambda i, k, n: (
        _md_link(f'\\[{n}\\]', *links[n]) if n in links else f'[{n}]'))
    # 真链接和哨兵只该差链接文字那一处;不相等说明 `\[N\]` 这个写法本身又引起了
    # 别的解析差异,那就不是"只把这一处变成链接"了。
    swapped = _SENT_RE.sub(
        lambda mm: f'[{num_at[(int(mm.group(1)), int(mm.group(2)))]}]', shown)
    if _norm(swapped) != _norm(_MD.render(real)):
        return ''
    return real


def link_refs(body: str, links: dict[str, tuple[str, str]]) -> str:
    """把正文里的出处编号换成链接。

    `links` 是「编号 → (已转义的目标, 已转义的标题)」,转义口径在 `kb.daily`
    (`_md_dest` / `_md_title`)。渲染出来的链接文字就是 `[编号]`,方括号要转义是
    因为 Obsidian 会把裸 `[[18]]` 当 wikilink。编号对不上素材(模型偶尔会编一个
    超出范围的)、那条素材没有 url、或者整组编号一个都对不上时原样留着,不编一个
    链接出来。
    """
    if not links:
        return body
    # 一组里一个都对不上就整段别动:那多半不是出处编号(年份区间、条目序号之类)。
    hits = [m for m in _NUMS_RE.finditer(body)
            if any(n in links for n in _nums(m))
            and not _autolinked(body, m.start())]
    if not hits:
        return body

    base = _norm(_MD.render(body))
    spots = list(enumerate(hits))
    # 整篇一起试一次:一篇日报的出处多半没有一处落在代码或已有链接里,这条路只
    # 渲染三遍。逐个各试各的要渲染 2n+1 遍 —— 真遇到才付。
    whole = _accepts(base, body, spots, links)
    if whole:
        return whole

    out, taken = body, []
    for spot in reversed(spots):
        # 每一处都拿**原文**当底判:接受了才落进 out,所以前面那些位置始终有效。
        trial = _accepts(base, body, sorted(taken + [spot]), links)
        if trial:
            out, taken = trial, taken + [spot]
    return out
