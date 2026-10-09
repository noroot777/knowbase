# -*- coding: utf-8 -*-
"""长文回包的**结构判定**,外加 Draft.js 块转 markdown 的那几个零件。

**单独一个文件只因为行数** —— 这一族连同它们的口径注释有一百七十多行,留在
`x_article.py` 里会把那个文件顶出 700 行硬上限。选它搬是因为它是里面最独立的
一块:全是纯函数、没有模块级可变状态、也没有被任何测试打桩
(`API` / `PAGE_WAIT` / `BROWSER_FALLBACK` / `MAX_CHARS` / `render` / `fetch`
都被 `patch.object(x_article, …)` 改过,所以**一个都没搬** —— 搬走之后打桩只
改到一份副本,口径同 `taxo_tables.py` 那条 `CACHE_DIR`)。`x_article` 把这里的
名字原样 import 回自己的模块全局,所以 `x_article._code_body` 这类直测照旧有效。

**这一族回答的是同一个问题:第三方回包在这个位置给的东西合不合格。** 两档,
判据是「这一处是正文,还是装饰」:
- **可选**(图片地址、entityMap 的行、media 表的行)—— 认不出来**就地降级**:
  那张图跳过、那一块跳过,**整篇正文照旧补得到**(`_rows` / `_media_urls` /
  `_entity_image` / `_entities`);
- **必需**(正文块本身、块里的文字和类型)—— 认不出来**一律抛**
  `ArticleUnavailable`(`_must_block` / `_must_text`)。丢掉它们之后剩下的正常
  块会拼出一段**非空**正文,而 `put_items` 是 `ON CONFLICT DO NOTHING`:那就是
  拿残缺内容盖「成功」的章,之后再同步、再跑空正文回填都纠不回来。

抛出去只是**扣留**:手里有浏览器的两条通道接着走后路,没有就本轮不入库、水位
留在原处、下一轮重试(`put_items` 按 URL 去重,重叠重抓是安全的)。
"""

from __future__ import annotations

import re

class ArticleUnavailable(RuntimeError):
    """没问到。message 是成品文案。**和「它没有长文」是两回事** —— 后者返回 None。"""


def _rows(value) -> list:
    """上游给的「一串东西」。**非列表一律当空。**

    这些字段全来自第三方回包,而 `for x in <标量>` 抛的是 `TypeError` —— 它会从
    `render` 一路漏出 `fetch`、漏出 `fill`(只认 `ArticleUnavailable`)、再漏出
    `harvest` 的账号隔离(只认 `XBrowserError`/`CdpError`),于是**后面的账号
    一个都不同步**(2026-10-10 第 2 轮审查实测的正是这条链,只是入口换成了
    `media_info`)。这里当空,结果就只是「这一处认不出来」:那张图跳过、那一块
    跳过,**整篇正文照旧补得到**。
    """
    return value if isinstance(value, list) else []


def _media_urls(article: dict) -> dict[str, str]:
    """`media_id` → 图片地址。文章里的图是按 id 引用的,地址在另一张表里。

    **每一层都明确判类型,不靠 `or {}` 兜底。** `x or {}` 只对**假值**生效,
    而第三方回包给的是**非空**的错类型:`media_info` 是一个非空列表时
    `(… or {}).get(…)` 当场 `AttributeError: 'list' object has no attribute
    'get'`(2026-10-10 第 2 轮审查的最小受控输入就是它)。认不出的那一条跳过去,
    和上面那条 `isinstance(media, dict)` 是同一条口径 —— 这篇文章的正文块是好的,
    末态该是「正文照旧补到、只是那张图少一个地址」。
    """
    out = {}
    for media in _rows(article.get('media_entities')):
        if not isinstance(media, dict):
            continue
        info = media.get('media_info')
        if not isinstance(info, dict):
            continue
        raw = info.get('original_img_url')
        url = raw.strip() if isinstance(raw, str) else ''
        if url and (mid := str(media.get('media_id') or '')):
            out[mid] = url
    return out


def _entity_image(block: dict, entities: dict, media: dict) -> str:
    """atomic 块指向的那张图。认不出来就返回空串(这一块整个跳过)。

    三层嵌套各判一次类型,理由同 `_media_urls`:`data` 或 `mediaItems` 里的
    元素换成别的类型时,这一块跳过去就是了,不该把整篇文章连同后面的账号带掉。
    """
    ranges = _rows(block.get('entityRanges'))
    if not ranges or not isinstance(ranges[0], dict):
        return ''
    value = entities.get(str(ranges[0].get('key')))
    if not isinstance(value, dict) or value.get('type') != 'MEDIA':
        return ''
    data = value.get('data')
    if not isinstance(data, dict):
        return ''
    for item in _rows(data.get('mediaItems')):
        if not isinstance(item, dict):
            continue
        if url := media.get(str(item.get('mediaId') or '')):
            return url
    return ''


def _entities(content: dict) -> dict:
    """`entityMap`。**它是一个列表,不是字典** —— 上游给的是 `[{key, value}, …]`,
    而块里的 `entityRanges[].key` 是数字,所以键一律按字符串比。"""
    raw = content.get('entityMap')
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    out = {}
    for row in _rows(raw):
        if isinstance(row, dict) and 'key' in row:
            out[str(row['key'])] = row.get('value')
    return out


def _must_text(block: dict, field: str) -> str:
    """块的**必需文字字段**。缺失或 null 当空串,**其余非字符串一律抛**。

    `str()` 强转在这里不成立:`block['text']` 是 `{'text': '原文'}` 时强转得到
    `"{'text': '原文'}"` —— **非空**,于是被当成"正文补到了"落库,而 `put_items`
    是 `ON CONFLICT DO NOTHING`:那一篇从此永久是那串带花括号的字,再同步、
    再跑空正文回填脚本(它只选正文为空的)都纠不回来(2026-10-10 第 3 轮审查
    用真 `harvest` + 真 SQLite + 真阅读页复现:库里正文是
    `{'text': 'The original article body'}`,而同一会话的 `browser_fetch`
    读得到真正文、一次导航都没发)。抛出去只是扣留:手里有浏览器走后路,没有
    就本轮不入库、水位留在原处、下一轮重试。

    **这一道是承重的,不靠 `fetch` 里那道兜底** —— 强转压根不抛异常,兜底没有
    机会介入。**只对必需的块结构这么严**:图片地址那一层是可选数据,认不出来
    跳过那一块就是了(见 `_media_urls`),而文字没了就是正文没了。
    """
    value = block.get(field)
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    raise ArticleUnavailable(
        f'长文接口给的块 {field} 不是文字:{type(value).__name__}')


def _must_block(row) -> dict | None:
    """正文块列表的**成员**。空值当「这里没东西」跳过,**非空的错类型一律抛**。

    和 `_must_text` 是同一条口径的两半:那条管「一个块里面的文字」,这条管
    「这个位置上到底是不是一个块」。**整个成员不符合格式时不许 `continue`** ——
    丢掉它之后剩下的正常块照样拼出一段**非空**正文,于是「正文补到了」、水位
    照常推进、后路压根不被触发,而丢掉的那一段是**这篇文章的正文**:`put_items`
    是 `ON CONFLICT DO NOTHING`,那一篇从此永久只剩开头,再同步返回新增 0 条,
    空正文回填脚本(它只选**正文为空**的)也不选它,真实 `/doc/` 阅读页上一直
    只有标题和第一段(2026-10-10 第 4 轮审查用真 `harvest` + 真 SQLite + 真导出
    + 真阅读页复现:第二个成员换成一个字符串,库里只剩
    `INTRODUCTION_ONLY: article opening paragraph`,而同一会话的 `browser_fetch`
    两段都读得到、一次导航都没发)。

    **这是「残缺」而不是「空」,所以 `fetch` 那道「正文为空就抛」接不住它** ——
    和 `_must_text` 那次一样,承重的判据必须自己站得住。抛出去只是扣留:手里
    有浏览器走后路,没有就本轮不入库、水位留在原处、下一轮重试。

    **空值跳过**(`None` / `''` / `0` / `[]` / `False`):它们携带不了任何内容
    线索,而真的空块是 `{}`、走正常路径留一个空行。口径同 `fetch` 里 article
    那道分支 —— **非空的错类型才是「在撒谎」**。

    **只对正文块这么严**:可选的那些(图片地址、标题)认不出来跳过那一块就是了
    (见 `_media_urls`),而正文少一段就是这篇文章少一段。
    """
    if isinstance(row, dict):
        return row
    if row:
        raise ArticleUnavailable(
            f'长文接口给的正文块不是对象:{type(row).__name__}')
    return None


def _code_body(lines: list[str]) -> str:
    """把攒起来的代码行拼成围栏内容。**行内容一律原样,一个字节都不改。**

    行尾空白**可能正是数据** —— 多行字符串、提示词模板、定宽文本、字符画都把它
    当内容。逐行 `rstrip` 的那一版实测过:一段三引号字符串里第二行末尾那两个
    空格被删掉之后,代码照旧 `compile()` 得过,而里面那句 `assert` 当场失败
    (2026-10-09 第 4 轮审查,接口、后路、导出到阅读页复制四条路各复现一次)
    —— **「编译得过」证明不了字符串的值没被改写**,所以回归的判据是执行输出。

    真实 Draft.js 的代码块是**一行一个块**,所以「每块收尾」就等于「每行收尾」:
    这件事只能在**整组边界**做。而且只削**完全空**的行:纯空格的行同样可能是
    数据,代码**中间**的空行照旧原样留着。
    """
    rows = '\n'.join(lines).split('\n')
    a, b = 0, len(rows)
    while a < b and not rows[a]:
        a += 1
    while b > a and not rows[b - 1]:
        b -= 1
    body = '\n'.join(rows[a:b])
    return body if body.strip() else ''


def _fence(code: str) -> str:
    """把一段代码包成 markdown 代码围栏。

    围栏是 markdown 里**唯一**能原样留住换行和缩进的结构:裸代码写进 md 之后,
    阅读页按普通段落渲染,HTML 的空白折叠把四格/八格缩进一起吃掉 —— 复制出来
    `compile()` 当场报错(2026-10-09 审查实测)。围栏长度避开代码里已有的反引号
    串,否则内容里一行 ``` 就把围栏提前收尾。
    """
    n = max([3] + [len(t) + 1 for t in re.findall(r'`{3,}', code)])
    bar = '`' * n
    return f'{bar}\n{code}\n{bar}'
