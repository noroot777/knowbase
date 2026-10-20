# -*- coding: utf-8 -*-
r"""手动收录那条命令行入口的**输入层**:一行一条地址,原样交出去。

**这一层只认「这一行整个是不是一条地址」,不认地址合不合法** —— 后者只有后端
`grab.normalize` 那一份口径说得准。

**这里不从一段文字里挑链接。** 这条入口的调用方是**大模型**(skill `kb-grab`):
用户说「这篇存一下」,模型读懂、把地址挑出来、一条一个参数传进来 —— **挑地址是
那一步的活**,它读 Markdown 没有困难。网页端的「手动收录」同样是一个输入框、
一条链接(`kb/static/js/grab.js`),服务端 `/api/grab` 收的也是单条 `url`。
这里曾经另做过一份「吞一整段文字、自己从里面找链接」:它的正确性判据是
「这一段结构真正表达的目标是哪一条」,而那个判据**等于 CommonMark 规范**
(官方 conformance 650+ 条),同时猜错**不可恢复** —— `registry.put_items` 是
`ON CONFLICT DO NOTHING`,收错一篇写死在库里。始末见
`docs/decisions/link-grab.md` 的「为什么输入层不解析 Markdown」。

**不合格的那一行是整体报错(`NotAnAddress`),不跳过、也不原样发:**
- **跳过**等于断言「这几行没有地址」,而我们只知道「它不是一行一条」——
  用户要收的那一条可能正夹在里面,跳过就是**静默漏掉**。
- **原样发**换来一串 `failed`,而其中真是地址的那几条**真的会被收进去** ——
  部分成功,用户看不出哪条算哪条。
- **报错**的代价只是重贴一次,看得见、改得了。三者里只有它不丢东西。
"""
from __future__ import annotations

import re

#: 「这一行整个就是一条地址」。**协议名钉死 http/https**:别的后端收不了,而这一层
#: 的全部职责就是回答「它是不是一条我们能收的地址」。两处判据共用这一份。
WHOLE_LINE = re.compile(r'^https?://\S+$', re.I)

#: 回显那一行时截到多长。剪贴板里可能是一整篇文章,整行打出来会淹掉后面那句出路。
_SNIP = 120


class NotAnAddress(ValueError):
    """标准输入里有一行不是一条地址。**失败关闭:一个请求都不发。**

    带着行号和那一行的开头 —— 「第几行」是用户唯一能据以改输入的信息。
    """

    def __init__(self, line_no: int, line: str) -> None:
        self.line_no, self.line = line_no, line
        shown = line if len(line) <= _SNIP else line[:_SNIP] + '…'
        super().__init__(
            f'标准输入第 {line_no} 行不是一条地址:「{shown}」\n'
            '这条入口只收一行一条地址。要从一段文字里挑出链接,交给大模型挑'
            '(它会一条一个参数传进来),或者用网页端的「手动收录」。')


def whole_line(line: str) -> bool:
    """这一行整个就是一条地址吗(是的话原样交后端,一个字符都不动)。"""
    return bool(WHOLE_LINE.match(line))


def dedupe(items) -> list[str]:
    """按出现顺序去重,空串丢掉。

    **集合判在不在、列表保顺序。** `not in 列表` 那一版每条都跟前面所有条比一遍
    —— 一次给上万条互不相同的地址时是平方级的比较。
    """
    out: list[str] = []
    seen: set[str] = set()
    for one in items:
        if one and one not in seen:
            seen.add(one)
            out.append(one)
    return out
