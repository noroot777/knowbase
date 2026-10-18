# -*- coding: utf-8 -*-
r"""手动收录那条命令行入口的**输入层**:把人粘进来的一行文字变成几条地址。

从 `grab_cli.py` 拆出来的(单文件要过 700 行的上限)。**这一层只认边界,不认
地址合不合法** —— 后者只有后端 `grab.normalize` 那一份口径说得准。

**两类边界,依据完全不同,所以分两段扫:**

①**边界是给定的**:整行就是一条地址(`WHOLE_LINE`,`grab_cli._from_stdin` 用),
  以及行内**明确圈出来**的那几种 —— `<地址>`、Markdown `[文字](目标)`、双引号、
  单引号。这几种的分隔符本身一定不属于地址,所以边界不是猜的。
②**裸写在行文里的**:边界只认空白。**一个标点都不剥、一个括号都不切** ——
  剥短之后那个地址**可能照样存在**(`…/post;` 剥成 `…/post`、`…/wiki/函数，性质`
  截成 `…/wiki/函数`,后端对原地址本来是收下并保留的),收进来的于是是**另一篇
  文章**,而 `put_items` 的 `DO NOTHING` 之下换不回来;留着整串顶多抓不到 ——
  终端上那一行 `failed` 用户看得见,改写了再来。两害取轻的那个。

**为什么是「先消费结构、剩下的才抽裸地址」,不是一个正则几个分支。**
正则交替只在**同一个起点**上比较优先级,管不了「后面那个结构分支该赢过前面已经
匹配上的裸地址」。所以 `[https://a/label](https://a/target)` 这种**显示文字本身
就是地址**的写法,裸地址那一档会从 `label` 那个 `https` 起手、一路吃到
`…label](https://a/target)`(实测:到达服务端的就是这一串,收录、`--state`、
`--refetch` 三个动作一起认错对象)。

**所以认链接走两趟**(`_links` → `_scan`):第一趟**只认结构**、按 CommonMark 的
行内解析从左到右走一遍,第二趟才在**没被结构盖住**的那几段文字上抽 `<…>`、引号、
裸地址(`_loose`)。两件事都靠这个分工:
- **一条链接的边界只由它自己那一段语法决定。** 「先独立判一遍哪儿是代码跨度、
  或者哪对方括号配得平」那种预处理不知道哪一段属于目标或 title,于是邻近的文字能
  改掉另一条链接的边界(实测过两种形态:前一条链接 title 里一个字面反引号、
  以及行文里一个落单的 `[`,都让后面那条完整链接整个被跳过、退回裸地址,发出去的
  地址多带一个右括号)。
- **显示文字里的裸地址是文字。** 第二趟压根不碰结构区间,所以 label 里那条地址
  没有机会被抽出来。

两趟都**不随输入长度平方级变慢**:方括号按栈一遍配平,反引号串一遍扫出来、按宽度
分组查表(`_tick_runs`)。「某个起点配不平/找不到收尾」只说明它自己,**不许据此
剪掉后面的起点** —— 那条错推论正是上面第一件事的反面。

**Markdown 这一档要按语法解码,别的几档一个字节都不动。** 这两件事得分清:
`[原文](https://a/part\(one\))` 的 Markdown 源码里 `\(` 表示**字面**的 `(`,
所以目标是 `…/part(one)`;而 `<…>`、引号、整行那几档给的是地址**原文**,
解码它们等于改写用户给定的边界(CommonMark 的 autolink 也不处理转义)。
"""
from __future__ import annotations

import re

# **第一趟(只认结构)在 `grab_md.py`**:CommonMark 的那一套行内解析、按所属语法
# 解码、以及那份探测预算,全在那边。`TangledInput` 在本模块里**照旧可见** ——
# 它是这一层对外的失败出口,引用方不该因为它搬了家而各自去找新门。
from grab_md import (                                               # noqa: F401
    WHOLE_LINE, TangledInput, labels_in, links)

#: 扫描时**下一个值得停下来看的位置**。四个结构起点加裸地址的协议名 ——
#: 逐字符推进在长剪贴板上是白跑,拿它跳过中间那些普通文字。
#: (`search` 找不到就是这一行再没有链接了。)
_NEXT = re.compile(r'''[<"']|https?://''', re.I)
#: `<地址>` —— RFC 3986 附录 C 那个写法。允许 `< 地址 >` 中间带空白。
#:
#: **哪几档在承载行为、哪几档是冗余带**(定点注入量过的,别当成都一样):
#: - **必需**:Markdown 那一整套(`)` 是 URL 合法字符,裸地址那一档会把它一起吃进来;
#:   显示文字、嵌套括号、转义、title、`<目标>` 每一件都各有一条红)、单引号那一档
#:   (`'` 是合法的子分隔符 `sub-delims`,同理)、以及这几个正则的 `re.I`。
#: - **冗余带,但留着**:下面这个 `_ANGLE` 和 `_QUOTED` 的双引号那一半 —— 裸地址那一档
#:   排除了 `< > "`,所以它们给出的边界本来就一样。留着是因为它把意图写明了(而且允许
#:   `< 地址 >` 中间带空白),删掉之后这两种写法就只剩「恰好排除集里有那个字符」在兜着。
_ANGLE = re.compile(r'<\s*(https?://[^\s<>]+?)\s*>', re.I)
#: 引号圈起来的。**两种引号分开写**:双引号那一档里允许出现 `'`(它是 URI 的合法
#: 子分隔符),反过来也一样 —— 合成一档用反向引用就只能共用一个排除集。
_QUOTED = re.compile(r'''"\s*(?P<dq>https?://[^\s"]+?)\s*"'''
                     r"""|'\s*(?P<sq>https?://[^\s']+?)\s*'""", re.I)
#: 裸地址:按空白断,外加那几个一定不属于地址的字符(`< > "` 在 URI 里必须编码)。
#: **`)` `]` `;` 这些一个都不排除** —— 理由见模块说明第②条。
_BARE = re.compile(r'https?://[^\s<>"]+', re.I)
def _loose(part: str) -> list[str]:
    """**没被结构盖住**的那段文字里的地址:`<…>`、引号、裸写的这三档。

    裸地址那一档按空白断、**一个标点都不剥**(理由见模块说明第②条),所以它会把
    `)` `]` `;` 这些一起收进来 —— 这一档只在结构之外跑,不会因此吞掉一条链接。

    **标签内部不走这里**:那一段由 `_attrs` 按属性语法取值、按 HTML 规则解码 ——
    裸地址那一档在属性值里会出事(`<span title="](https://a/x)">` 按空白断会把 `)`
    一起收进来),而引号那一档取不到实体解码。
    """
    out: list[str] = []
    i, n = 0, len(part)
    while i < n:
        nxt = _NEXT.search(part, i)
        if not nxt:
            break
        i = nxt.start()
        ch = part[i]
        if ch == '<':
            if m := _ANGLE.match(part, i):
                out.append(m.group(1))
                i = m.end()
                continue
        elif ch in '"\'':
            if m := _QUOTED.match(part, i):
                out.append(m.group('dq') or m.group('sq'))
                i = m.end()
                continue
        elif m := _BARE.match(part, i):
            out.append(m.group())
            i = m.end()
            continue
        i += 1                      # 这个起点没成,往后一格接着找
    return out


def _scan(text: str, labels: set[str]) -> list[str]:
    """**第二趟:按位置拼** —— 结构化区域交出它自己的目标,剩下的文字才抽裸地址。

    **两趟不能合成一趟。** 正则交替只在**同一个起点**上比较优先级,管不了「后面
    那个结构分支该赢过前面已经匹配上的裸地址」:`[https://a/label](https://a/target)`
    这种**显示文字本身就是地址**的写法,裸地址那一档会从 `label` 那个 `https` 起手、
    一路吃到 `…label](https://a/target)`(实测:到达服务端的就是这一串,收录、
    `--state`、`--refetch` 三个动作一起认错对象)。先把结构认完、再在**剩下的文字**上
    抽裸地址,显示文字就落不进那一档了。
    """
    out: list[str] = []
    at = 0
    for start, end, dest in links(text, labels):
        # **水位只许往前**:label 里那条 autolink 会被随后认成的外层链接包住
        # (见 `_links`),那时 `start` 落在水位之前 —— 照 `at = end` 走会让已经
        # 走过的那一段再抽一遍。
        if start > at:
            out.extend(_loose(text[at:start]))
        if dest:
            out.append(dest)
        at = max(at, end)
    out.extend(_loose(text[at:]))
    return out


def dedupe(items) -> list[str]:
    """按出现顺序去重,空串丢掉。

    **集合判在不在、列表保顺序。** `not in 列表` 那一版每条都跟前面所有条比一遍
    —— 一次粘进来上万条互不相同的地址时是平方级的比较。**两个调用点共用这一份**
    (行内抽取 `links_in`、整批参数 `grab_cli._targets`),所以这个判据只有一处、
    也只需要一处回归。
    """
    out: list[str] = []
    seen: set[str] = set()
    for one in items:
        if one and one not in seen:
            seen.add(one)
            out.append(one)
    return out


def links_in(text: str, labels: set[str] | None = None) -> list[str]:
    """**夹着叙述文字的那一段**里的链接,按出现顺序去重。

    `labels` 是**整份输入**里定义过的那几个标签(简写引用 `[标签]` 要靠它判):
    定义是文档级的、引用可以写在定义之前,所以跨段那一份由调用方收齐传进来
    (`grab_cli._from_stdin`)。不传就按这一段自己算 —— 单段调用照旧正确。
    """
    text = text or ''
    return dedupe(_scan(text, labels if labels is not None else labels_in(text)))


def whole_line(line: str) -> bool:
    """这一行整个就是一条地址吗(那一档原样交后端,一个字符都不动)。"""
    return bool(WHOLE_LINE.match(line))
