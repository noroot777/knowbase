# -*- coding: utf-8 -*-
r"""手动收录那条输入层的**第一趟:只认结构** —— 按 CommonMark 的行内解析从左到右
走一遍,认出这一段文字里哪几块是结构、各自表达的目标是什么。

从 `grab_input.py` 拆出来的(单文件要过 700 行的上限)。**两趟的分工写在那一份的
模块说明里**,这里只讲第一趟自己:

- **一条链接的边界只由它自己那一段语法决定。** 次序就是优先级:谁的起点先出现就
  先归谁,认成的那一块**整块消费**,里面再没有别的语法可认。
- **认不出来就失败关闭,不许退回去拼一条。** 第一趟认出来的那一块不交给第二趟去
  抽裸地址 —— 那一块的 `)` `]` 是 Markdown 的收尾,不是地址的一部分。
- **按所属语法解码**(两套规则在 `grab_entity.py`,那一份有张对照表):Markdown 的
  目标按 CommonMark 解(反斜杠转义 + 字符实体,同一遍里),HTML 的属性值按 HTML5 解。

对外只有 `links_in`/`whole_line` 那一侧用得到的几样:`WHOLE_LINE`(「整个就是一条
地址」那一份判据,三处共用)、`TangledInput`、`links`。
"""
from __future__ import annotations

import bisect
import re

# **解码在 `grab_entity.py`**(单文件要过 700 行的上限):Markdown 和 HTML 属性值
# 是两套规则,那一份模块说明里有张对照表。这一层只管结构在哪儿断。
from grab_entity import html_value, md_entity

#: **整行就是一条地址** —— 这一档和显式参数走同一道:一个字符都不动。判据只看
#: 「这一行(去掉两头空白之后)整个就是一条 http(s) 地址、中间没有空白」,不看它
#: 里面有哪些字符 —— 括号、分号、全角都不作数。`pbpaste | kb.sh grab -` 和
#: `kb.sh grab - < links.txt` 喂进来的多半正是这种一行一条。
#: **协议名不分大小写**(`HTTPS://…` 是合法写法,后端 `normalize` 收下并规范化)
#: —— 漏掉它的后果是这一行既不算整行、也抽不出链接,整条输入连一行结果都没有。
WHOLE_LINE = re.compile(r'^https?://\S+$', re.I)

#: 第一趟**只认结构**时值得停下来看的位置:方括号、反引号、反斜杠、尖括号,
#: 外加图片那个 `!`(`![替代文字](图)` 的开头 —— 它和链接的 `[` 必须分得开,
#: 理由见 `_links`)。
_STRUCT = re.compile(r'[\\`\[\]<!]')

#: 行内 HTML 的**开标签/闭标签**,和 autolink 那个 `<地址>`(CommonMark 的
#: 「Raw HTML」「Autolinks」,口径同 markdown-it 的 `HTML_TAG_RE` / `AUTOLINK_RE`)。
#: 这几种都是「到第一个 `>` 为止」的**有界**写法,所以一条正则就够、而且是线性的。
#:
#: **这几档里的 `[` `]` 一个都不算结构。** 漏掉它的形态实测过:
#: `见 [看 <span title="]">标签</span>](目标) 这篇` —— 属性值里那个 `]` 弹掉了显示
#: 文字的开括号,真正的 `]` 来时已经没有配对,于是整条链接退回裸地址,三个动作
#: (收录、`--state`、`--refetch`)发出去的都是 `目标)`,而那个地址**真的能存进库**。
#:
#: **email 那一档故意不写** —— 它的字符集里压根没有方括号,跳过它和不跳过它对
#: 「链接边界在哪儿」没有任何差别(唯一的作用是让这条正则长一倍)。
#: **注释、`<?…?>`、`<!…>`、CDATA 也不在这里** —— 那几种是「找收尾串」的写法,
#: 用正则的 `[\s\S]*?` 写出来在找不到收尾时会一路扫到行尾,一行里塞几百个 `<?`
#: 就是平方级。它们走 `_CLOSERS` 那张表、用 `str.find` 定位,扫过多少一目了然。
_TAG = re.compile(r'<(?:[A-Za-z][A-Za-z0-9-]*'
                  r'''(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*'''
                  r'''(?:\s*=\s*(?:[^\s"'=<>`]+|'[^']*'|"[^"]*"))?)*'''
                  r'\s*/?'
                  r'|/[A-Za-z][A-Za-z0-9-]*\s*'
                  r')>')
#: 标签里**带值的那几个属性**(`href="…"`、`src='…'`、裸值)。用户粘一段 HTML
#: 源码进来时,`href="https://…"` 就是他要的那一篇,所以这一档要收。
#: **值要按 HTML5 的规则解码**(`grab_entity.html_value`),见 `_attrs`。
_ATTR = re.compile(r'''\s+[A-Za-z_:][A-Za-z0-9_.:-]*\s*=\s*'''
                   r'''(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<uq>[^\s"'=<>`]+))''')
#: autolink:`<协议:…>`。**它是一条链接**(所以认出来要产出目标、而且「链接里不许
#: 再有链接」对它一样管用),不像上面那几个标签只是要跳过去。
#: 地址**原样交出去**,一个字符都不解码 —— CommonMark 的 autolink 不处理反斜杠
#: 转义,这一档给的是用户画出来的边界(同模块说明最后一段)。
_AUTOLINK = re.compile(r'<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*)>')
#: `<` 起手、要**找一个收尾串**才知道边界的那几种(CommonMark「Raw HTML」的后四种)。
#: 键是起手串,值是收尾串;`<!` 后面跟字母那一档(declaration)的收尾是单个 `>`。
#: 找不到收尾时那个 `<` 就是**字面文字**(CommonMark 的口径),按一格往下走 ——
#: 不许跳到行尾,后面可能还有完整的链接。
_CLOSERS = (('<!--', '-->'), ('<?', '?>'), ('<![CDATA[', ']]>'))
#: declaration(`<!DOCTYPE …>`):`<!` + 一个字母起手,`>` 收尾。
_DECL = re.compile(r'<![A-Za-z]')

#: **链接定义行**(CommonMark「Link reference definitions」):`[标签]: 目标 "标题"`。
#: 这是**块级**结构 —— 只在一段的开头连着出现,段里一旦有过正文行,后面的
#: `[x]: y` 就只是文字了(markdown-it 的 commonmark 模式同口径)。最多三个前导空格。
#: **引用块里的定义一样算**(`> [标签]: 目标`:block quote 的内容照旧是块级序列),
#: 所以前缀允许那几个 `>` —— 不认它的那一版把那一行交给第二趟,标签像地址时按
#: 空白断出一条 `https://a/label]:`(实测真落库)。
_DEF_OPEN = re.compile(r'(?:[ \t]{0,3}>)*[ \t]{0,3}\[')

#: link label 最多 **999** 个字符(CommonMark「Link label」那一节的硬上界)。
#: **它同时是一道性能判据**:简写引用那一问要拿 `[` 和 `]` 之间那段文字去比对标签表,
#: 而那个 `[` 可能在很远的地方 —— `'[' * 20000 + 文字 + ']' * 20000` 下每个 `]` 都切
#: 一段上万字符出来归一化,又是平方级(实测 1.22 秒,原先 0.3 秒上下)。先比长度
#: (O(1))、超界的压根不是 label,所以这道界**不是**为了快而牺牲正确性:规范就是这么定的。
_LABEL_MAX = 999

#: **最后那道筛子**:结构认出来、目标也解码完了,才看它是不是我们收得了的地址。
#: `[文字](#锚点)` / `[文字](/相对路径)` 筛掉 —— 但那一段**照样整段消费**:链接的
#: 目标是锚点,显示文字里那条地址是**文字**,把它收进来就是收了另一篇(用户要的是
#: 锚点那一篇)。「结构不支持就退回裸地址拼一条出来」正是要避免的那件事。
_SCHEME = re.compile(r'https?://', re.I)
#: Markdown 语法里的空白(不含别的 Unicode 空白:那些在目标里是字面量)。
_MD_SPACE = ' \t\n'


#: Markdown 的反斜杠转义**只有 ASCII 标点可转义**(CommonMark「Backslash escapes」):
#: `\a` 里那个反斜杠是字面量,所以不能无脑把反斜杠一律删掉。
#: **解码只有一处** —— `_plain_dest` / `_angle_dest` 逐字符扫的时候,碰上转义就把被
#: 转义的那个字符本身收进结果(反斜杠压根不进去),所以后面不需要、也不许再解一遍:
#: 那会是同一个问题两处各判一遍,而两份字符集迟早对不上。
_ESCAPABLE = frozenset('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')

#: 第一趟**结构探测**的预算(见 `_Budget`):按行长的倍数给,外加一个常数项让短行
#: 也宽松。两个数都**只为挡住损坏输入**,不是在给正常输入设上限 —— 正常一行里所有
#: 探测加起来不超过行长。
_PROBE_FACTOR = 4
_PROBE_FLOOR = 4096


class TangledInput(ValueError):
    """这一行里认不全的结构残片太多(见 `_Budget`)。

    **按参数错处理**(`grab_cli.main` 那边退出 2、说一句人话),**不许当成「这一行
    没有链接」** —— 那是「确定没有」这个结论,而我们只知道「没认全」。
    """


class _Budget:
    """第一趟**结构探测**的累计预算(按字符数)。

    **「这一段是不是链接」答不上来时要扫过多少字符,是输入说了算的** ——
    `](目标)` 的括号一路不配平、title 没有收尾引号、`<?` 找不到 `?>`,都得扫到
    行尾才知道。一行里塞上千个这种残片,同一段余文就被反复扫,耗时随行长平方涨
    (实测 500/1000/2000 个 `[](`:0.019/0.065/0.258 秒;`](a (` 那一档同理)。

    **这道线给得很宽**:正常一行里每段结构只被它自己那一次探测扫过,加起来不超过
    行长,所以合法输入碰不到;碰到了就是损坏文本。**碰到了要抛** —— 「这一行我
    认不全」在终端上看得见(退出 2、人读得懂的一句),「把结构残片拼成另一个地址」
    是静默收错一篇,而 `put_items` 的 `DO NOTHING` 之下换不回来。

    **只有「扫了多远」说得准的那几处才计费**:`_inline_link` 那一族(它返回扫到
    哪儿)和 `_CLOSERS` 的 `str.find`。`_TAG` / `_AUTOLINK` 不计 —— 那两条正则里
    没有无界的 lazy 量词、交替分支的首字符互不重叠,一次匹配最多扫到下一个 `<`
    或行尾,整行加起来是线性的(这正是把注释/`<?…?>`/CDATA 排除出正则、改走
    `_CLOSERS` 的理由)。
    """

    __slots__ = ('left',)

    def __init__(self, text: str) -> None:
        self.left = len(text) * _PROBE_FACTOR + _PROBE_FLOOR

    def spend(self, chars: int) -> None:
        self.left -= chars
        if self.left < 0:
            raise TangledInput(
                '这一行里认不全的结构残片太多(没有配对的括号、引号或 `?>`?)，'
                '没法确定哪一段是链接 —— 把要收的地址单独一行贴过来')


def _tick_runs(text: str) -> tuple[dict[int, list[int]], dict[int, int]]:
    r"""一遍扫出整行所有反引号串,**给「收尾在哪儿」那一问当索引**。
    返回 `({宽度: [该宽度那些串的起点(递增)]}, {起点: 之后那一格})`。

    **配对查表,不在每个起点上重扫余文。** 「从这个串往后找等长的收尾」那一问如果
    各自往后扫一遍,一行里塞满反引号时耗时随长度平方涨(实测 1000/2000/4000 个短串:
    0.011/0.037/0.112 秒,长度翻两倍耗时约十倍)。按宽度分组之后那一问是一次二分。

    **这份表不管反斜杠转义,而这正是 CommonMark 的口径** —— 对**收尾**而言:
    `` `foo\` `` 的收尾就是那个被反斜杠挡着的反引号(spec 的「Backslash escapes do not
    work in code spans」)。**起点**那一侧反过来:`` \`` `` 里第一个反引号被转义吃掉了,
    剩下的那个才是起点、宽度按剩下的算 —— 所以起点的宽度由主扫描**就地数**
    (`_links`),不借这份表的切分。一份表两种用法,是因为这两件事在 CommonMark 里
    本来就不同;合成一份(让这份表也跳转义)会让 `` `foo\` `` 从此找不到收尾。
    """
    by_width: dict[int, list[int]] = {}
    ends: dict[int, int] = {}
    i, n = 0, len(text)
    while i < n:
        if text[i] != '`':
            i += 1
            continue
        start = i
        while i < n and text[i] == '`':
            i += 1
        by_width.setdefault(i - start, []).append(start)
        ends[start] = i
    return by_width, ends


def _code_span_end(by_width, ends, i: int, width: int) -> int | None:
    """从 `i` 起、宽度 `width` 的那串反引号当起点时,这段代码跨度到哪儿为止
    (之后那一格);没闭合就 `None`。

    收尾那串要和开头**一样长**(CommonMark 的 code span 规则),而且得在起点那一串
    **之后**(`i + width` 之后的第一个,所以同一串的后半截不会被当成自己的收尾)。
    """
    same = by_width.get(width)
    if not same:
        return None
    j = bisect.bisect_left(same, i + width)
    return ends[same[j]] if j < len(same) else None


def _angle_dest(text: str, i: int) -> tuple[str | None, int]:
    """`[文字](<目标>)` 里那个尖括号包着的目标。`text[i] == '<'`。"""
    out: list[str] = []
    n = len(text)
    i += 1
    while i < n:
        ch = text[i]
        if ch == '\\' and i + 1 < n and text[i + 1] in _ESCAPABLE:
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '&' and (ent := md_entity(text, i)):
            out.append(ent[0])
            i = ent[1]
            continue
        if ch == '>':
            return ''.join(out), i + 1
        if ch in '<\n':
            return None, i              # 这两个字符在这种写法里不合法
        out.append(ch)
        i += 1
    return None, i


def _plain_dest(text: str, i: int) -> tuple[str | None, int]:
    """`[文字](目标)` 里那个裸着的目标。

    **括号要数深度,深几层都认**:`…/A_(B_(C))` 是合法条目名(维基那一类),而
    只认一层的写法会让这一段结构匹配失败、退回裸地址那一档,把 Markdown 自己
    的收尾 `)` 也吃进去(实测到达服务端的是 `…/A_(B_(C)))`)。
    """
    out: list[str] = []
    depth, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '\\' and i + 1 < n and text[i + 1] in _ESCAPABLE:
            out.append(text[i + 1])     # 转义掉的括号是字面量,不参与配平
            i += 2
            continue
        if ch == '&' and (ent := md_entity(text, i)):
            # **实体解出来的括号同样不参与配平**(它是字面字符,不是语法)。
            # **两种解码必须在同一遍里做**:`\&#40;` 的那个 `&` 已经被上一档当成
            # 转义吃掉了,剩下的 `#40;` 只是普通文字 —— 换成「整串先解实体、再解
            # 转义」(或者反过来)都会把这五个字符变成一个 `(`,那是另一个地址。
            # 两个分支在这儿谁先谁后倒是无所谓:判据是**当前这个字符**,不冲突。
            out.append(ent[0])
            i = ent[1]
            continue
        if ch in _MD_SPACE:
            break                       # 目标到此为止,后面可能是 title
        if ch == '(':
            depth += 1
        elif ch == ')':
            if depth == 0:
                break                   # 这个 `)` 是链接自己的收尾
            depth -= 1
        out.append(ch)
        i += 1
    if depth:
        return None, i                  # 没配平:这不是一个内联链接
    return ''.join(out), i


def _skip_title(text: str, i: int) -> tuple[bool, int]:
    """跳过目标后面那个可选的 title(`"…"` / `'…'` / `(…)`)。

    返回 `(认成了吗, 扫到哪儿)`。**第二个数无论成不成都要给** —— 调用方拿它算
    这次探测花了多少(`_Budget`):没有收尾的 title 要一路扫到行尾才知道,而一行里
    几百个 `](a (` 会让同一段余文被反复扫(实测形态见 `_Budget`)。
    """
    closer = ')' if text[i] == '(' else text[i]
    n = len(text)
    i += 1
    while i < n:
        if text[i] == '\\' and i + 1 < n:
            i += 2
            continue
        if text[i] == closer:
            return True, i + 1
        i += 1
    return False, n


def _inline_link(text: str, close: int) -> tuple[str | None, int]:
    """认一个 Markdown 内联链接的 `(目标)` 部分。`close` = 显示文字 `]` 之后那一格。

    返回 `(目标, 扫到哪儿)`。目标那一位有三态,**三态各是一件不同的事**:
    - `None` = **这一段不是一个内联链接结构**(没有配对的 `](`、目标括号不配平、
      title 没收尾)。那时这一段压根不是链接,`[备注] 看 https://…` 里的裸地址
      还该照常认出来。
    - `''` = **结构成立,但目标筛不过协议**(`#锚点`、`/相对路径`、`ftp:`)。
      那一段**照样整段消费** —— 结构认出来了就由它说了算,「解析不了就退回裸地址」
      会从显示文字里拼出另一个地址来(而那个地址可能真的存在)。
    - 别的 = 这一段的目标。

    **第二个数无论哪一态都是「扫到哪儿」**,不是「整段之后那一格」:`None` 那一态
    也要给,调用方拿它算这次探测花了多少(`_Budget`)。成立的那两态下它就是整段
    之后那一格。
    """
    n = len(text)
    if close >= n or text[close] != '(':
        return None, close
    i = close + 1
    while i < n and text[i] in _MD_SPACE:
        i += 1
    dest, i = (_angle_dest if i < n and text[i] == '<' else _plain_dest)(text, i)
    if dest is None:
        return None, i
    while i < n and text[i] in _MD_SPACE:
        i += 1
    if i < n and text[i] in '"\'(':
        ok, i = _skip_title(text, i)
        if not ok:
            return None, i
        while i < n and text[i] in _MD_SPACE:
            i += 1
    if i >= n or text[i] != ')':
        return None, i
    # 结构成立。**最后才筛协议** —— 筛不过的交出空串(整段照样消费,见上)。
    return (dest if _SCHEME.match(dest) else ''), i + 1


def _attrs(text: str, start: int, end: int, found: list) -> None:
    """一个标签里**属性值写着的那几条地址**,各记一笔。

    **值要按 HTML 自己那一套规则解码**(`grab_entity.html_value`)—— 属性值里 `&amp;` 是一个
    `&`,而数字引用**缺分号照样算**。原样交出去的那一版实测:
    `<a href="https://a/x?p=1&amp;q=2">` 收进库的是 `https://a/x?p=1&amp%3Bq=2`
    (后端的规范化又把那个分号编码了一道),**而原目标压根不在库里**;借 Markdown
    那一份(必须带分号)的那一版同理,`&#47B` 没解开、落库的是 `https://a/A&`。
    属性值里**没有**反斜杠转义,所以这一档只解引用。

    **判据是「解完实体之后整个值就是一条地址」**(`WHOLE_LINE`,和「整行就是一条
    地址」、「反引号里整段就是一条地址」共用那一份)。所以 `srcset="…2x.jpg 2x"`
    这种「地址 + 描述符」的值**一条都不产出** —— 值里有空白就说不清哪一段是地址,
    认不出就失败关闭(那张图本来也不是人要收的那一篇,`src` 那条照旧收)。

    记的是**属性那一段自己的区间**(它落在标签区间之内,第二趟按单调水位走,
    所以不会重复抽)。
    """
    for a in _ATTR.finditer(text, start, end):
        raw = a.group('dq') or a.group('sq') or a.group('uq') or ''
        val = html_value(raw.strip())
        if WHOLE_LINE.match(val):
            found.append((a.start(), a.end(), val))


def _angled(text: str, i: int, found: list, budget: _Budget) -> int:
    """`<` 起手那一族(CommonMark「Autolinks」「Raw HTML」):**这一段到哪儿为止**。
    `text[i] == '<'`。认不出来就是字面的 `<`,返回 `i + 1`。

    **三种各记一种账**(第四位的含义见 `_links`):
    - **autolink 是一条链接** —— 记它的目标。它**不作废**外层的 `[`:CommonMark 的
      「Links may not contain other links」管的是内联/引用链接的嵌套,autolink 在
      bracket matching 里不参与(`[<https://a/auto>](https://b/outer)` 两条都成立,
      markdown-it 的 commonmark 模式也给两条)。作废掉的那一版**漏掉 outer 那一篇**,
      而漏是静默的。
    - **开/闭标签只是跳过去,但属性值里那条地址照旧收**(`framed=True`):用户粘一段
      HTML 源码进来,`href="https://…"` 就是他要的那一篇(这一档旧版就收,别弄丢)。
      **裸地址那一档在标签内部不跑** —— `<span title="](https://a/x)">` 那种属性值里
      混着 Markdown 语法字符,按空白断会把 `)` 一起收进来,那是另一篇。
    - **注释 / `<?…?>` / CDATA / declaration 整段消费,一个字都不抽**:那几段是
      不参与渲染的文本,里面的 `[文字](地址)` 残片交给第二趟就会多带一个右括号
      (实测 `<!-- [注释里的](https://a/no) -->` 发出 `https://a/no)`)。

    里面的 `[` `]` 一个都不算结构 —— 漏掉这件事的形态见 `_TAG`。
    """
    if m := _AUTOLINK.match(text, i):
        dest = m.group(1)
        # 筛不过协议的(`<ftp://…>`、`<mailto:…>`)交空串:整段照样消费、不产出。
        found.append((i, m.end(), dest if _SCHEME.match(dest) else ''))
        return m.end()
    if m := _TAG.match(text, i):
        found.append((i, m.end(), ''))
        _attrs(text, i, m.end(), found)
        return m.end()
    n = len(text)
    for head, tail in _CLOSERS:
        if text.startswith(head, i):
            at = text.find(tail, i + len(head))
            # **找不到收尾就是字面文字**(CommonMark 的口径):往下走一格,不许跳到
            # 行尾 —— 后面可能还有完整的链接。两种情况都按真扫过的距离计费。
            budget.spend((at if at >= 0 else n) - i)
            if at < 0:
                return i + 1
            found.append((i, at + len(tail), ''))
            return at + len(tail)
    if _DECL.match(text, i):
        at = text.find('>', i + 2)
        budget.spend((at if at >= 0 else n) - i)
        if at < 0:
            return i + 1
        found.append((i, at + 1, ''))
        return at + 1
    return i + 1


def _ref_label(text: str, i: int) -> int | None:
    """引用式链接的第二对方括号(`[文字][标签]` 的 `[标签]`):**它到哪儿为止**。
    `text[i]` 不是 `[` 就 `None`。

    CommonMark 的 link label 里不许有没转义的 `[` `]`,所以一遍扫到配对的 `]`
    就够,不必数深度。
    """
    n = len(text)
    if i >= n or text[i] != '[':
        return None
    i += 1
    while i < n:
        if text[i] == '\\' and i + 1 < n:
            i += 2
            continue
        if text[i] == ']':
            return i + 1
        if text[i] == '[':
            return None             # label 里有没转义的 `[`:这不是一个 label
        i += 1
    return None


def _link_def(text: str, i: int, budget: _Budget) -> tuple[str, str, int] | None:
    """一段开头那几行里的**链接定义**:`(标签, 目标, 之后那一格)`;不是定义就 `None`。

    **定义行里那个目标是用户写的真实地址,要收。** 引用式链接自己
    (`[文字][标签]`)说不出要收哪一篇、整段消费,真正的地址正是写在这儿 ——
    两件事合起来,`[原文][1]` + `[1]: https://…/real` 收到的是 `…/real` 一条。
    不认它的那一版把这一行交给第二趟,于是**标签本身像地址时**(合法文档里
    `[https://a/label]: https://a/target`)按空白断出一条 `https://a/label]:`
    —— 实测它过得了后端的规范化、真落库真导出真索引,而那是凭空多出来的一篇。

    **认不成就不消费**(CommonMark 的口径:定义不合法时整行就是普通文字),所以
    `[备注]: 见 https://…/real 这篇` 里那条真地址照旧由第二趟认出来。
    反过来,**认成了的那一段整段消费** —— 目标筛不过协议(相对路径、`#锚点`)时
    一个字都不产出,同内联链接那一档。
    """
    if not (m := _DEF_OPEN.match(text, i)):
        return None
    close = _ref_label(text, m.end() - 1)
    if close is None or close >= len(text) or text[close] != ':':
        return None
    label = text[m.end():close - 1]
    if not label.strip():
        return None                     # 空 label:CommonMark 不认
    n = len(text)
    j, lines = close + 1, 0
    while j < n and text[j] in _MD_SPACE:   # 目标前最多隔一个换行
        lines += text[j] == '\n'
        if lines > 1:
            return None
        j += 1
    dest, j = (_angle_dest if j < n and text[j] == '<' else _plain_dest)(text, j)
    budget.spend(j - close)
    if not dest:
        return None
    k = j
    while k < n and text[k] in ' \t':
        k += 1
    if k < n and text[k] in '"\'(':        # 可选的 title
        ok, k = _skip_title(text, k)
        budget.spend(k - j)
        if not ok:
            return None
        while k < n and text[k] in ' \t':
            k += 1
    if k < n and text[k] != '\n':
        return None                     # 行尾还有别的东西:这不是一条定义
    return label, dest, k


def _norm_label(raw: str) -> str:
    """link label 的比较形式(CommonMark:空白折叠 + case fold)。"""
    return ' '.join(raw.split()).casefold()


def leading_defs(text: str, budget: _Budget | None = None,
                 ) -> list[tuple[int, int, str, str]]:
    """一段**开头连着**那几条链接定义行:`[(起点, 之后那一格, 标签, 目标)]`。

    **只有这一份**:`links` 拿它记结构和目标,`labels_in` 拿它收标签 —— 同一个
    「哪儿算定义」的判据不许在两处各写一遍。
    """
    budget = budget or _Budget(text)
    out: list[tuple[int, int, str, str]] = []
    i, n = 0, len(text)
    while (one := _link_def(text, i, budget)) is not None:
        label, dest, end = one
        out.append((i, end, _norm_label(label), dest))
        i = end + 1 if end < n and text[end] == '\n' else end
    return out


def labels_in(text: str) -> set[str]:
    """这一段里定义过的那几个标签(比较形式)。

    **定义是文档级的,引用可以写在定义之前** —— 所以调用方(`grab_cli._from_stdin`)
    先把**所有段**的标签收齐,再逐段做行内扫描。少了这一步,
    `[https://a/label]` 在前、定义在后那种合法文档里,前一段那个简写引用认不出来,
    于是退回裸地址、多发一条 `https://a/label]`(实测真落库真导出真索引)。
    """
    return {label for _, _, label, _ in leading_defs(text)}


def links(text: str, labels: frozenset[str] | set[str] = frozenset(),
          ) -> list[tuple[int, int, str]]:
    """**第一趟:只认结构** —— 这一段文字里认出来的那几块,按**认成的次序**:
    `[(起点, 之后那一格, 目标)]`。

    目标是空串 = **这一块整块消费、不产出地址**:筛不过协议的目标、HTML 标签
    本身、注释那几种、图片、引用式链接、以及认成结构但不该成立为链接的 `](…)`
    残片。

    **开头那几行先按块级认一次**(`_link_def`:`[标签]: 目标 "标题"`) —— 引用式
    链接自己说不出要收哪一篇,真正的地址写在定义行里,两件事合起来才完整。

    **区间会重叠**,所以第二趟按**单调水位**拼(`_scan`),不假设两两不交:标签
    属性里那条地址落在标签自己的区间内(`_attrs`),而 label 里那条 autolink 会
    被随后认成的外层链接包住(`[<https://a/auto>](https://b/outer)` 两条都成立)。

    **这一趟的次序就是优先级,跟 CommonMark 的行内解析同一个口径**:谁的起点先出现
    就先归谁,被整段消费掉的那一段里面再没有别的语法可认。所以
    ``[一](… "反引号 ` 的用法") 和 [二](…)`` 里 title 那个反引号**不是**代码跨度起点
    (`[一](…)` 整段先被消费了),第二条链接照旧认得出来;反过来
    ``[看 `](x)` 这个](目标)`` 里那个反引号先出现,它那段代码跨度先成立,里面的 `](`
    于是是字面文字。**先独立判一遍「哪儿是代码跨度」做不到这件事** —— 那份表不知道
    哪一段属于目标或 title,实测形态:前一条链接 title 里一个字面反引号会让后面那条
    完整链接整个被跳过、退回裸地址,发出去的地址多带一个右括号(收录、`--state`、
    `--refetch` 三个动作一起认错对象)。

    **链接里不许再有链接**(CommonMark 的「Links may not contain other links」):
    `[outer [inner](A)](B)` 成立的是 inner 那一条,包着它的那几个 `[` 随之作废。不作废
    的话选出来的是 B —— 那是**另一篇文章**,不是「多带了一个字符」那种错。
    """
    found: list[tuple[int, int, str]] = []
    by_width, ends = _tick_runs(text)
    budget = _Budget(text)
    #: 栈里每一项是 `(那个 `[` 的位置, 它是图片的开头吗, 入栈时的世代)`。
    #: **「链接里不许再有链接」靠世代号惰性作废,不许每次成立都过滤一遍整个栈** ——
    #: 图片的开括号不被作废(它不占那个名额),所以 `'![' * n + '[x](#a)' * n` 这种
    #: 输入下栈长一直是 n,每条链接过滤一次就是平方级(实测长度翻倍耗时约 3.5 倍,
    #: 而且这笔工作**不在探测预算里**:它不是「扫了多远」,所以计不进去,只能让它
    #: 本身是线性的)。世代号那一版:成立一条链接就 `gen += 1`,弹栈时丢掉世代过期
    #: 的非图片项 —— **每一项最多被丢一次,整趟摊销线性**。
    opens: list[tuple[int, bool, int]] = []
    gen = 0
    i, n = 0, len(text)
    # **块级那一档先走:段开头那几条链接定义行**(`[标签]: 目标`)。它是块级结构,
    # 判据是「这一段里它前面只有定义行」—— 所以在行内扫描**之前**、而且只在开头
    # 连着认。认不成就一个字都不消费,余文照旧按行内规则走(见 `_link_def`)。
    for start, end, _label, dest in leading_defs(text, budget):
        found.append((start, end, dest if _SCHEME.match(dest) else ''))
        i = end
    while i < n:
        nxt = _STRUCT.search(text, i)
        if not nxt:
            break                   # 这一行再没有方括号/反引号/反斜杠了
        i = nxt.start()
        ch = text[i]
        if ch == '\\':
            # **转义掉的那个字符不参与任何结构** —— `\[`、`` \` `` 都是字面量。
            # 这一步本身是承重的(删掉它,`` \`` `` 的第一个反引号就成了起点,宽度
            # 也跟着错);**而「只有 ASCII 标点转得义」这条判据此刻是冗余带**,定点
            # 注入量过:跳过的那个字符要么本来就是标点(确实不参与结构)、要么压根
            # 不是结构字符(跳过它和逐格走没有差别),所以改成一律跳两格一条用例都
            # 不红。留着是因为它是 CommonMark 的判据(「Backslash escapes」),而且
            # 跟 `_plain_dest` 那两处解码共用同一份字符集 —— 口径只有一份。
            i += 2 if i + 1 < n and text[i + 1] in _ESCAPABLE else 1
        elif ch == '`':
            # **起点的宽度就地数** —— 不借 `_tick_runs` 那份切分:`` \`` `` 里第一个
            # 反引号已经被上面那一档当转义吃掉了,剩下的那个才是起点(宽度 1,不是
            # 那份表记的 2)。借表的那一版在这种输入上直接 `KeyError`:一行带
            # `` \`` `` 的文字会让整条命令崩掉,同一批后面几条压根没被处理。
            width = 1
            while i + width < n and text[i + width] == '`':
                width += 1
            end = _code_span_end(by_width, ends, i, width)
            if end is None:
                # 没闭合的那一串是字面文字:跳过**这一串**接着往下扫(别跳到末尾)。
                i += width
            else:
                # **代码跨度的内容是字面代码,里面的 `[文字](地址)` 不是链接。**
                # 只跳过、不记一笔的那一版,第二趟会在这一段上抽裸地址 —— 实测
                # `` `[不是](…/no)` `` 发出的是 `…/no)` 再加一个反引号,那是**另一篇**。
                # **但反引号也是一道给定的边界**(同 `<…>`、引号那一档):整段就是
                # 一条地址时照旧收 —— 技术文档和聊天里 `` `https://…` `` 是常见写法,
                # 漏掉它是静默丢数据,而这一档的边界是用户自己画的,不会把语法
                # 字符吃进来。判据和「整行就是一条地址」共用一份(`WHOLE_LINE`)。
                inner = text[i + width:end - width].strip()
                found.append((i, end, inner if WHOLE_LINE.match(inner) else ''))
                i = end
        elif ch == '<':
            i = _angled(text, i, found, budget)
        elif ch == '!':
            # **`![…](…)` 是图片,不是链接。** 跟 `[` 分开记:图片成立之后**不许
            # 作废外层那条链接**(「链接里不许再有链接」管的是链接,CommonMark 的
            # 行内解析里图片不占那个名额)。混在一起的那一版,
            # `[![封面](图)](文章)` 只产出那张图、**把文章那一篇静默漏掉**。
            # 走到这一档的 `!` 一定没被转义(转义的那个已经被上面 `\\` 那一档
            # 跳过了),所以这里不必再判一次。
            if i + 1 < n and text[i + 1] == '[':
                opens.append((i + 1, True, gen))
                i += 2
            else:
                i += 1
        elif ch == '[':
            opens.append((i, False, gen))
            i += 1
        else:
            # `]`:把 `](…)` **当一段来认,不管有没有配对的 `[`**。没有配对时它
            # 在 CommonMark 下是字面文字 —— 但「字面文字」不等于「该从里面抽一条
            # 裸地址」:那一段的 `)` 是 Markdown 的收尾,不是地址的一部分。交给
            # 第二趟的后果实测过:`[outer [inner](A)](B)` 里外层被「链接里不许再有
            # 链接」作废之后,残片 `](B)` 让第二趟发出 `B)` —— 多一个右括号就是
            # **另一篇文章**,而那个地址真的能存进库。所以认成结构就整段消费、
            # **一个字都不产出**(`dest` 给空串)。
            had, start, image = False, i, False
            while opens:
                pos, img, born = opens.pop()
                if img or born == gen:
                    had, start, image = True, pos, img
                    break
                # 非图片、而且世代过期:它早被某条成立的链接作废了。丢掉接着往下
                # 找 —— 每一项最多被丢一次,所以这一圈总共摊销线性。
            dest, end = _inline_link(text, i + 1)
            budget.spend(end - i)
            if dest is not None:
                # **图片不产出地址,也不作废外层那条链接。** 它表达的是「这儿嵌
                # 一张图」,不是「收这一篇」—— 而外层那个 `[` 还等着它自己的 `](…)`。
                found.append((start, end, '' if (image or not had) else dest))
                if not image:
                    # 链接里不许再有链接:包着它的那几个一起作废。**图片的开括号
                    # 留着** —— 它不占那个名额(`![a [b](c)](d)` 里 image 照旧成立)。
                    gen += 1
                i = end
            elif (ref := _ref_label(text, i + 1)) is not None:
                # **引用式链接**(`[文字][标签]`)。我们不解引用定义 —— 但它**是
                # 一段结构**,交给第二趟就会把显示文字和 `][标签]` 残片拼成一个
                # 地址(实测 `[https://a/label][ref]` 发出 `https://a/label][ref]`,
                # 而那个地址过得了后端的规范化)。所以整段消费、**一个字都不产出**:
                # 认不出目标时**失败关闭**,不许退回去拼一条。真正的地址写在
                # **定义行**里,那一档由 `_link_def` 按块级语法认 —— 交给第二趟的
                # 那一版,标签本身像地址时会多发一条 `https://a/label]:`。
                budget.spend(ref - i)
                found.append((start, ref, ''))
                if not image:
                    gen += 1
                i = ref
            elif (had and i - start - 1 <= _LABEL_MAX
                    and _norm_label(text[start + 1:i]) in labels):
                # **简写引用**(`[标签]`,CommonMark 的 shortcut reference):后面既没
                # `(` 也没第二对方括号,但**这个标签有定义** —— 那它是一条链接,
                # 目标在定义行里(定义行自己那一档已经产出了它)。所以这一段整段
                # 消费、**一个字都不产出**;交给第二趟的那一版按空白断出一条
                # `https://a/label]`(实测它和真目标两条都真写入真导出真索引,
                # 命令对两条都报 `saved`)。
                # **判据是「这个标签真有定义」,不是「看起来像个标签」** ——
                # 否则 `[备注] 看 https://…` 那一行会被整段消费掉,而那上面正写着
                # 用户要收的地址(反过来钉一边的那条用例)。
                found.append((start, i + 1, ''))
                if not image:
                    gen += 1
                i += 1
            else:
                # 结构压根不成立(没有 `](`、括号不配平、title 没收尾、标签没定义)。
                # 那一段不是链接,`[备注] 看 https://…` 里的裸地址还该照常认出来。
                i += 1
    return found
