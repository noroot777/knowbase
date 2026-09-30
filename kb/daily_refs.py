"""日报正文里的出处编号(`[18]`、`[3][7]`、`[1、2]`)换成可点的 markdown 链接。

这段替换**写进文件**,不是渲染层的装饰:改错了就是把日报原文改坏 —— 页面、复制
全文、Obsidian 里翻回来,三处一起受影响。

**每一次替换由渲染器裁决,这里不写"哪些片段不许动"的规则。** 手写规则的路走过
三轮,每一轮都被真实渲染器找出新的漏网写法:逐行正则漏掉行内代码和已有链接的
文字(第 1 轮)、跨行代码跨度/单引号标题/引用式链接/嵌套括号目标(第 2 轮);改成
手写的 CommonMark 扫描器之后又漏掉多行定义、引用段里的定义、原始 HTML 的
`<a>` 与 `<code>`、独立缩进代码块、autolink 的地址(第 3 轮)。**漏网写法的清单
不会收敛**,因为它等于把一个 markdown 解析器重写一遍。

所以换个判据:把某处换成链接,**渲染一遍看结构变没变**。逐个候选各试各的,
接受的条件是「这一处之外一个字都没动」:
  · 代码元素(行内、围栏、缩进、HTML 写的)连内部结构逐字不变;
  · 原有的链接一条不少、文字和目的地都不变(autolink 也是链接,一并管住);
  · 图片的替代文字和地址不变;
  · 多出来的只能是文字为 `[N]` 的出处链接,数量还得对得上。
判不准的一律不换 —— 那一处的编号留成纯文本,面板那边照素材清单还能补上
(`daily.js` 的 `enhanceDailyBody`),而改坏了的原文没有任何东西补得回来。

代价是每个候选渲染一遍(实测一篇日报几十毫秒),换来的是正确性不再取决于谁
更懂 CommonMark。
"""
from __future__ import annotations

import re
from collections import Counter

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

# 出处编号:`[18]` / `[1, 2]` / `[1，2]` / `[1、2]`。口径和前端 daily.js 的
# DAILY_REF_TEXT 同一份 —— 两边不一样的话,同一种写法在新旧日报上一个能点一个不能。
_NUMS_RE = re.compile(r'\[(\d{1,3}(?:\s*[,，、]\s*\d{1,3})*)\]')
_SEP_RE = re.compile(r'[,，、]')

# 按 CommonMark 渲染,和前端渲染日报的 marked 是同一套规范;`html=True` 也跟它一致
# ——原始 HTML 被转义成文本的话,`<code>items[1]</code>` 这种写法在这里看着无害,
# 到了页面上却是代码里塞进一条链接。
_MD = MarkdownIt('commonmark')


def _shape(text: str) -> dict:
    """渲染一遍,把**不许被这次替换动到**的东西取出来。

    代码取整段 `str(e)` 而不是 `textContent`:往 `<code>` 里塞一个 `<a>` 并不改变
    文本,拿文本比的话这道闸对"代码内部别建链接"根本不敏感(第 3 轮审查实测)。
    链接连文字一起记,只比 href 的话「一条链接裂成两半」看不出来——那正是
    `[版本 [1]](…)` 被改坏时的形态。
    """
    soup = BeautifulSoup(_MD.render(text), 'html.parser')
    links = soup.find_all('a')
    return {
        'code': [str(e) for e in soup.find_all(['code', 'pre'])],
        'img': [(e.get('alt') or '', e.get('src') or '') for e in soup.find_all('img')],
        'a': Counter((e.get_text(), e.get('href') or '') for e in links),
        # 链接套链接在 HTML 里是非法的,浏览器会把内层提升成兄弟 —— 原来那条的
        # 文字当场被切掉一截,点击目标跟着错位。而这里的解析器(和 bs4 一样宽松的
        # 那一类)会如实嵌套,外层 get_text() 连内层一起算,看着逐字没变:
        # `<a href="…">版本 [1]</a>` 被改坏时就长这样(第 3 轮审查实测点击 [1] 跳
        # 到了素材而不是产品页)。所以单独数一道。
        'nest': sum(1 for e in links if e.find_parent('a')),
    }


def _accepts(base: dict, trial: dict, want: Counter) -> bool:
    """trial 相对 base 只多出了 `want` 这几条出处链接,别处一个字没动。"""
    if base['code'] != trial['code'] or base['img'] != trial['img']:
        return False
    if base['nest'] != trial['nest']:   # 新链接落进了另一条链接里
        return False
    if base['a'] - trial['a']:          # 原有链接少了一条,或者文字/目的地被改了
        return False
    added = trial['a'] - base['a']
    return Counter(t for t, _ in added.elements()) == want


def _nums(m: re.Match) -> list[str]:
    return [n.strip() for n in _SEP_RE.split(m.group(1))]


def _apply(body: str, hits: list[re.Match], links: dict[str, str]) -> tuple[str, Counter]:
    """把 hits 这几处都换掉。从后往前:先动靠后的,前面那些量出来的位置才还算数。"""
    out, want = body, Counter()
    for m in reversed(hits):
        nums = _nums(m)
        out = (out[:m.start()]
               + ''.join(links.get(n) or f'[{n}]' for n in nums)
               + out[m.end():])
        want += Counter(f'[{n}]' for n in nums if n in links)
    return out, want


def link_refs(body: str, links: dict[str, str]) -> str:
    """把正文里的出处编号换成 `links` 里那条链接。

    `links` 是「编号 → 整条 markdown 链接文本」,由调用方拼好(转义、尖括号目标
    那些口径在 `kb.daily`),渲染出来的链接文字就是 `[编号]`。编号对不上素材
    (模型偶尔会编一个超出范围的)、那条素材没有 url、或者整组编号一个都对不上时
    原样留着,不编一个链接出来。
    """
    if not links:
        return body
    # 一组里一个都对不上就整段别动:那多半不是出处编号(年份区间、条目序号之类)。
    hits = [m for m in _NUMS_RE.finditer(body)
            if any(n in links for n in _nums(m))]
    if not hits:
        return body

    base = _shape(body)
    # 整篇一起试一次:一篇日报的出处多半没有一处落在代码或已有链接里,这条路
    # 只渲染两遍。逐个各试各的是 O(n) 遍渲染 —— 实测 21 KB / 80 处要 760 ms,
    # 真遇到才付。
    whole, want = _apply(body, hits, links)
    if _accepts(base, _shape(whole), want):
        return whole

    out, want = body, Counter()
    for m in reversed(hits):
        trial, more = _apply(out, [m], links)
        more += want
        if _accepts(base, _shape(trial), more):
            out, want = trial, more
    return out
