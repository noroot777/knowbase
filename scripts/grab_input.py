# -*- coding: utf-8 -*-
r"""手动收录那条命令行入口的**输入层**:把人粘进来的一行文字变成几条地址。

从 `grab_cli.py` 拆出来的(单文件要过 700 行的上限)。**这一层只认边界,不认
地址合不合法** —— 后者只有后端 `grab.normalize` 那一份口径说得准。

**两类边界,依据完全不同,所以分两段扫:**

①**边界是给定的**:整行就是一条地址(`_WHOLE_LINE`,`grab_cli._from_stdin` 用),
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

import bisect
import html
import re

#: **整行就是一条地址** —— 这一档和显式参数走同一道:一个字符都不动。判据只看
#: 「这一行(去掉两头空白之后)整个就是一条 http(s) 地址、中间没有空白」,不看它
#: 里面有哪些字符 —— 括号、分号、全角都不作数。`pbpaste | kb.sh grab -` 和
#: `kb.sh grab - < links.txt` 喂进来的多半正是这种一行一条。
#: **协议名不分大小写**(`HTTPS://…` 是合法写法,后端 `normalize` 收下并规范化)
#: —— 漏掉它的后果是这一行既不算整行、也抽不出链接,整条输入连一行结果都没有。
_WHOLE_LINE = re.compile(r'^https?://\S+$', re.I)

#: 扫描时**下一个值得停下来看的位置**。四个结构起点加裸地址的协议名 ——
#: 逐字符推进在长剪贴板上是白跑,拿它跳过中间那些普通文字。
#: (`search` 找不到就是这一行再没有链接了。)
_NEXT = re.compile(r'''[<"']|https?://''', re.I)
#: 第一趟**只认结构**时值得停下来看的位置:方括号、反引号、反斜杠、尖括号。
_STRUCT = re.compile(r'[\\`\[\]<]')

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
#: **最后那道筛子**:结构认出来、目标也解码完了,才看它是不是我们收得了的地址。
#: `[文字](#锚点)` / `[文字](/相对路径)` 筛掉 —— 但那一段**照样整段消费**:链接的
#: 目标是锚点,显示文字里那条地址是**文字**,把它收进来就是收了另一篇(用户要的是
#: 锚点那一篇)。「结构不支持就退回裸地址拼一条出来」正是要避免的那件事。
_SCHEME = re.compile(r'https?://', re.I)
#: 字符实体(CommonMark「Entity and numeric character references」):`&#40;` 是
#: 字面的 `(`、`&amp;` 是 `&`。**目标里要按语法解码**,不然 `…/A_&#40;B&#41;`
#: 原样交出去就是另一个地址。只认**带分号**的严格写法(`html.unescape` 本身宽松到
#: 连 `&amp` 都解,那会改写用户真的写了 `&amp` 的地址)。
_ENTITY = re.compile(r'&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{1,31});')
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
        if ch == '&' and (ent := _ENTITY.match(text, i)):
            out.append(html.unescape(ent.group()))
            i = ent.end()
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
        if ch == '&' and (ent := _ENTITY.match(text, i)):
            # **实体解出来的括号同样不参与配平**(它是字面字符,不是语法)。
            # **两种解码必须在同一遍里做**:`\&#40;` 的那个 `&` 已经被上一档当成
            # 转义吃掉了,剩下的 `#40;` 只是普通文字 —— 换成「整串先解实体、再解
            # 转义」(或者反过来)都会把这五个字符变成一个 `(`,那是另一个地址。
            # 两个分支在这儿谁先谁后倒是无所谓:判据是**当前这个字符**,不冲突。
            out.append(html.unescape(ent.group()))
            i = ent.end()
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
        found.append((i, m.end(), dest if _SCHEME.match(dest) else '', False))
        return m.end()
    if m := _TAG.match(text, i):
        found.append((i, m.end(), '', True))
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
            found.append((i, at + len(tail), '', False))
            return at + len(tail)
    if _DECL.match(text, i):
        at = text.find('>', i + 2)
        budget.spend((at if at >= 0 else n) - i)
        if at < 0:
            return i + 1
        found.append((i, at + 1, '', False))
        return at + 1
    return i + 1


def _links(text: str) -> list[tuple[int, int, str, bool]]:
    """**第一趟:只认结构** —— 这一行里认出来的那几段,按**认成的次序**:
    `[(起点, 之后那一格, 目标, 这一段里还抽不抽边界给定的那两档)]`。

    目标是空串 = **这一段整段消费、不产出地址**(筛不过协议的目标、HTML 标签、
    注释那几种、以及认成结构但不该成立为链接的 `](…)` 残片)。第四位只对
    **HTML 标签**为真:属性值里那条**边界给定**的地址照旧要收,但裸地址那一档不跑
    (理由见 `_angled`)。

    **一般互不重叠,但 label 里那条 autolink 会被随后认成的外层链接包住**
    (`[<https://a/auto>](https://b/outer)` 两条都成立)。所以第二趟按**单调水位**
    拼(`_scan`),不假设区间两两不交。

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
    found: list[tuple[int, int, str, bool]] = []
    by_width, ends = _tick_runs(text)
    budget = _Budget(text)
    opens: list[int] = []
    i, n = 0, len(text)
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
                # 字符吃进来。判据和「整行就是一条地址」共用一份(`_WHOLE_LINE`)。
                inner = text[i + width:end - width].strip()
                found.append((i, end, inner if _WHOLE_LINE.match(inner) else '',
                              False))
                i = end
        elif ch == '<':
            i = _angled(text, i, found, budget)
        elif ch == '[':
            opens.append(i)
            i += 1
        else:
            # `]`:把 `](…)` **当一段来认,不管有没有配对的 `[`**。没有配对时它
            # 在 CommonMark 下是字面文字 —— 但「字面文字」不等于「该从里面抽一条
            # 裸地址」:那一段的 `)` 是 Markdown 的收尾,不是地址的一部分。交给
            # 第二趟的后果实测过:`[outer [inner](A)](B)` 里外层被「链接里不许再有
            # 链接」作废之后,残片 `](B)` 让第二趟发出 `B)` —— 多一个右括号就是
            # **另一篇文章**,而那个地址真的能存进库。所以认成结构就整段消费、
            # **一个字都不产出**(`dest` 给空串)。
            had = bool(opens)
            start = opens.pop() if had else i
            dest, end = _inline_link(text, i + 1)
            budget.spend(end - i)
            if dest is None:
                # 结构压根不成立(没有 `](`、括号不配平、title 没收尾)。那一段不是
                # 链接,`[备注] 看 https://…` 里的裸地址还该照常认出来。
                i += 1
            else:
                found.append((start, end, dest if had else '', False))
                opens.clear()       # 链接里不许再有链接:包着它的那几个一起作废
                i = end
    return found


def _loose(part: str, *, bare: bool = True) -> list[str]:
    """**没被结构盖住**的那段文字里的地址:`<…>`、引号、裸写的这三档。

    裸地址那一档按空白断、**一个标点都不剥**(理由见模块说明第②条),所以它会把
    `)` `]` `;` 这些一起收进来 —— 这一档只在结构之外跑,不会因此吞掉一条链接。

    **`bare=False` 给 HTML 标签的内部用**(`_angled`):那里只收**边界给定**的那两档
    (`href="https://…"` 是用户要的那一篇),裸地址那一档不跑 —— 属性值里混着
    Markdown 语法字符时(`<span title="](https://a/x)">`)按空白断会把 `)` 一起收进来。
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
        elif bare and (m := _BARE.match(part, i)):
            out.append(m.group())
            i = m.end()
            continue
        i += 1                      # 这个起点没成,往后一格接着找
    return out


def _scan(text: str) -> list[str]:
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
    for start, end, dest, framed in _links(text):
        # **水位只许往前**:label 里那条 autolink 会被随后认成的外层链接包住
        # (见 `_links`),那时 `start` 落在水位之前 —— 照 `at = end` 走会让已经
        # 走过的那一段再抽一遍。
        if start > at:
            out.extend(_loose(text[at:start]))
        if dest:
            out.append(dest)
        elif framed:
            out.extend(_loose(text[max(at, start):end], bare=False))
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


def links_in(text: str) -> list[str]:
    """**夹着叙述文字的那一行**里的链接,按出现顺序去重。

    只有 `grab_cli._targets` 判出「这一行不是一条干净地址」时才走这里。
    """
    return dedupe(_scan(text or ''))


def whole_line(line: str) -> bool:
    """这一行整个就是一条地址吗(那一档原样交后端,一个字符都不动)。"""
    return bool(_WHOLE_LINE.match(line))
