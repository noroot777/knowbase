# -*- coding: utf-8 -*-
"""X 长文(Article)的正文补全:时间线上只有一张卡片,正文得单独取一次。

**硬约束在 `docs/decisions/x-article.md` 第一节 —— 改这块之前先读那一份。**

**为什么需要这一步**:X 的长文(图文卡片下面带标题和摘要的那种)发出来时,推文
本身**一个字都没有** —— `text` 就是一条指向 `x.com/i/article/<id>` 的 t.co 链接,
X 在界面上把它藏起来、改成渲染一张卡片。于是浏览器通道的 `tweetText` 压根不存在、
feedgrab 那边的正文就是那条光秃秃的链接,两条路入库的都是一条空正文:标题回落成
「某某 的动态」,vault 里那篇 md 没有内容,全文检索搜不到文章里的任何一句话,
提取阶段每轮把它报成「正文为空,需回填」。2026-10-09 实测:近三周 social 的 342
条里 11 条空正文,**9 条是长文**(另两条一条是纯图推文、一条是已删除的推文)。

**取法是 FxTwitter 的公开接口**(`https://api.fxtwitter.com/<handle>/status/<id>`),
它在 `tweet.article.content` 里把整篇文章按 Draft.js 的块结构交出来。选它的三个
理由:

①**不要登录**。X 自己的那条公开接口(`cdn.syndication.twimg.com/tweet-result`)
  只给 `title` + 一段 120 字左右的 `preview_text`,没有正文;而
  `x.com/i/article/<id>` 在未登录的浏览器里是一句「该页面不存在」加登录墙
  (2026-10-09 用临时 profile 的无头 Chrome 实测)。要正文就只有「登录态 + X 内部
  GraphQL + `x-client-transaction-id` 签名」这一条路 —— 那正是 `x_feedgrab` 的模块
  注释里写明不抄进来的那份维护量。FxTwitter(开源的 FixTweet)替我们背着它。
②**四条通道通用**。它只认推文 URL,不认我们这一轮用的是浏览器、cookie 还是桥接,
  所以 `bridge` 那条没有登录态的路也补得上。
③**不开浏览器**。走登录态浏览器读文章页当然也行,但那要在用户自己的浏览器里多开
  一张标签页、等它渲染两三秒,还要押一个 X 改版就会坏的 DOM 选择器;这边是一次
  HTTP、一份结构化 JSON。所以浏览器那条做成**后路**,不做主路。

**它是一个第三方服务,所以有后路**(开源的 FixTweet 跑的公共实例;推文 URL 会发
给它,内容本来就是公开的)。它关停、限流、改接口的那一天,`browser` / `attach`
两条通道手里**正好有一个已经登录的浏览器** —— 直接把文章页读出来即可,见下面
`browser_fetch`。`bridge` / `feedgrab` 没有浏览器,那两条照旧按「补不到就不入库、
不推进水位」办,下一轮重试。

两个开关各管一条路:`KB_X_ARTICLE_API` 设成空串关掉接口(**手里有浏览器就只走
后路** —— 这正是「不想把推文 URL 发给第三方」时要的那一档),`KB_X_ARTICLE_BROWSER`
设成 '0' 关掉后路,两个都关 = 整条退回这一步出现之前。

**取不到的那一条要退出这一轮,而且不推进水位**(`fill` 返回的 note 由两条通道的
`harvest` 并进 `incomplete`)。原因是 `registry.put_items` 是
`ON CONFLICT(key) DO NOTHING` —— 空正文一旦落库就**再也不会被更新**,这一篇长文
从此永久是空的。所以宁可这一轮当它没来过:水位留在原处,下一轮重叠重抓时再试一次
(`put_items` 按 URL 去重,重叠不会产生重复条目)。**只有一种情况不这么办** ——
接口明确答复「这条推文没有长文」(纯图、纯视频推文就是这样):那是一个确定的结论,
按它原样入库,由提取阶段那套「正文为空」的终态收口(见 `kb/extract.py` 的
`body_gap`),不该为它冻住水位。
"""

from __future__ import annotations

import json
import os
import re
import time

from kb.feeds.fetching import FeedError, request
# 读文章页那段 JS 单独一个文件,**只因为行数** —— 160 多行源码会把这个文件顶出
# 700 行硬上限,而它是纯数据、没有被任何测试打桩(见那个模块的注释)。绑回本模块
# 的 `_READ_JS`,所以 dom 那组照旧按老名字把它丢给真浏览器 eval。
from kb.feeds.x_article_js import READ_JS as _READ_JS
# 地址识别与正文清理整块搬进了 `x_article_links`(见它的模块注释)。这里原样
# 再导出,所以 `x_article.lead_text` / `.quote_lines` / `.needs_article` /
# `.HINT` / `._first` 这些对外和对测试的入口一个字都没变。
from kb.feeds.x_article_links import (  # noqa: F401  (re-export)
    HINT, _ARTICLE_LINK, _ARTICLE_PATH, _BARE_HEAD, _drop, _first, _nested,
    _OUTER_START, _SEPARATORS, _strip_hint, _TCO, _URL_CHARS, _URL_HOST,
    _URL_RUN, _URL_TAIL, article_page, lead_text, needs_article, quote_lines)
# 回包的结构判定和块级零件也单独一个文件,同样**只因为行数**(见那个模块的
# 注释)。原样 import 回本模块的全局,所以 `x_article._code_body` 这类直测、
# 以及 `scripts/x_article_backfill` 里的 `x_article.ArticleUnavailable` 都照旧。
from kb.feeds.x_article_shape import (
    ArticleUnavailable, _code_body, _entities, _entity_image, _fence,
    _media_urls, _must_block, _must_text, _rows)
from kb.feeds.x_cdp import CdpError

# 接口地址。设成空串 = 整个关掉这一步(不发请求、不拦任何条目)。
API = os.environ.get('KB_X_ARTICLE_API', 'https://api.fxtwitter.com').rstrip('/')
# 单次请求的墙钟上限。
TIMEOUT = float(os.environ.get('KB_X_ARTICLE_TIMEOUT', '20'))
# 连着几次请求失败就不再试了。防的是"接口整个挂了,而这一轮还有几十条要问",
# 每条各等满 TIMEOUT 会把整轮采集拖垮。失败的那些照旧算没读完(见模块注释)。
_GIVE_UP_AFTER = 3
# 正文最多留多长。最长的真实样本是 3 万字符(2026-10-09 实测),比这再长的多半
# 不是一篇要读的文章;翻译那一侧本来也只送前 12000 字符。
MAX_CHARS = 60000

# 推文 URL 里的 handle 和 id。
_STATUS = re.compile(r'(?:x|twitter)\.com/([A-Za-z0-9_]{1,20})/status/(\d+)')

# Draft.js 的块类型 → markdown 前缀。认不出的块按普通段落走。
_PREFIX = {
    'header-one': '# ', 'header-two': '## ', 'header-three': '### ',
    'header-four': '#### ', 'header-five': '##### ', 'header-six': '###### ',
    'unordered-list-item': '- ', 'ordered-list-item': '1. ', 'blockquote': '> ',
}


def render(article: dict) -> str:
    """文章 JSON → markdown 正文。

    **只按块级结构转,不动块内偏移**(`inlineStyleRanges` 的粗体、`entityRanges`
    的行内链接一概不展开)。那些偏移是 Draft.js 的 **UTF-16 码元**下标,而 Python
    的字符串按码点索引 —— 正文里出现一个 emoji 就会把它后面所有偏移错开一位,
    拿错位的下标去插标记就是把作者写的字改坏。块级的东西(标题、列表、引用、整块
    的图)没有这个问题,它们本来就是一整块。

    标题不写进正文:`sync.export` 自己会在 md 顶上写一行 `# <title>`。

    **正文块**那一路只收合格结构,认不出来一律抛 `ArticleUnavailable`:整个成员
    不是对象(`_must_block`)、块里的必需字段不是字符串(`_must_text`)都算 ——
    丢掉它们之后剩下的正常块会拼出一段**非空**正文,那就是拿残缺内容盖「成功」
    的章。可选的那些(图片地址)认不出来只跳过那一块。
    """
    content = article.get('content')
    if not isinstance(content, dict):
        return ''
    entities, media = _entities(content), _media_urls(article)
    out: list[str] = []
    code: list[str] = []

    def flush() -> None:
        if body := _code_body(code):
            out.append(_fence(body))
        code.clear()

    for row in _rows(content.get('blocks')):
        block = _must_block(row)
        if block is None:
            continue
        kind = _must_text(block, 'type') or 'unstyled'
        if kind == 'atomic':
            flush()
            if url := _entity_image(block, entities, media):
                out.append(f'![]({url})')
            continue
        text = _must_text(block, 'text').replace('\x00', '')
        if kind == 'code-block':
            # Draft.js 的代码块是**一行一个块**(`blockRenderMap` 把相邻的几个包进
            # 同一个 `<pre>`),所以攒起来合并成**一个**围栏 —— 每行各发一个围栏的话
            # 复制出来是一堆分散的小代码块。**每一行都原样攒着,行首缩进和行尾空白
            # 都是内容**(理由见 `_code_body`)—— 占位空块的处理**只在整组边界**
            # 发生,这里一个字节都不判、不改。
            code.append(text)
            continue
        flush()
        if stripped := text.strip():
            out.append(_PREFIX.get(kind, '') + stripped)
    flush()
    return '\n\n'.join(out)[:MAX_CHARS].strip()


def from_payload(article: dict) -> dict:
    """回包里**自带**的那一篇长文 → `{'title','body'}`。取不到正文一律抛。

    **两个消费方共用这一份判据**:`fetch`(专门去问一次长文接口)和手动收录
    (`kb.feeds.grab._grab_x` —— 推文回包里本来就带着 `tweet.article`,接口那一跳
    没成时用手里这一份,不是把它丢掉)。各写一份的话「只给了标题算不算补到了」
    这种判断会在两条路上各漂一次,而漂错的那一边会放行一条空正文落库。
    """
    try:
        body = render(article)
    except (AttributeError, TypeError, KeyError, IndexError, ValueError) as e:
        # **兜底,不是承重的那一道。** 承重的是两半:①`_rows` 和每一层的
        # `isinstance`(见 `_media_urls`)—— **可选**数据认不出来就地降级,那张
        # 图跳过、那一块跳过,**整篇正文照旧补得到**;②`_must_text` —— **必需**
        # 的块结构(文字、类型)只收字符串,认不出来一律抛,因为强转出来的
        # `"{'text': …}"` 非空、会被当成"正文补到了"落库,**兜底压根没有机会
        # 介入**(强转不抛异常)。这一道管没想到的形状:`render` 解析的
        # 全部输入都来自第三方回包,而「上游还能给出什么形状」是一张**不会收敛**
        # 的名单,少想一种就是本轮那场事故(异常漏出 `fill`、漏出 `harvest` 的
        # 账号隔离,**后面的账号一个都不同步**)。归到这儿之后那一条只是扣留:
        # 手里有浏览器就走后路,没有就本轮不入库、水位留在原处、下一轮重试。
        #
        # **只圈这几类数据形状异常,不写 `except Exception`**,更不在采集外层
        # 笼统吞 —— 后者会把浏览器会话中断那类真该往上报的故障一起吃掉。
        #
        # **如实记一笔:今天这一道是冗余带。** `json.loads` 产得出的类型只有
        # dict/list/str/数字/bool/None 这几种,逐处类型检查补全之后构造不出能
        # 走到这里的输入(定点注入实测 0 红,补一条**契约**用例才红)。留着它
        # 的理由是构造上的:`render` 的形状约束不该取决于一张逐处枚举的名单,
        # 而代价只有「这一条扣留 + 下一轮重试」—— 和整批中断差着一个量级。
        raise ArticleUnavailable(
            f'长文接口的回包解析不了:{type(e).__name__}: {e}') from None
    raw_title = article.get('title')
    title = raw_title.strip() if isinstance(raw_title, str) else ''
    # **只有标题不算补到了。** 要补的是正文,而空正文一旦落库就**再也改不回来**
    # (`put_items` 是 `ON CONFLICT DO NOTHING`),下一轮重叠重抓也救不回来 ——
    # 口径同 `browser_fetch` 那条「判不准一律抛」。三种形态都归这儿:回包被截断、
    # `content.blocks` 缩成空、或者真是一篇空文章。抛出去之后手里有浏览器的
    # 两条通道会接着走后路,两条都不行时这一条本轮不入库、水位留在原处。
    #
    # **不许退回「标题正文同时为空才算」**:那一版在"有标题、没正文"这个形状上
    # 返回成功,于是 `fill` 留下一条空正文、水位照常推进,而那一篇永久空着。
    if not body:
        raise ArticleUnavailable(
            f'长文接口只给了标题「{title[:40]}」、没有正文' if title
            else '长文接口给了一篇空文章')
    return {'title': title, 'body': body}


def fetch(url: str, *, timeout: float = 0.0) -> dict | None:
    """问一次这条推文的长文。返回 {'title','body'};**它没有长文时返回 None**。

    两个返回值必须分得开:None 是一个确定的结论(纯图推文就是这样),调用方按它
    原样入库;问不到则抛 `ArticleUnavailable`,调用方据此不推进水位。把后者也
    当成 None 就是拿一次故障冒充"这条没有正文",而空正文一旦落库再也改不了。
    """
    if not API:
        return None
    m = _STATUS.search(url or '')
    if not m:
        return None
    endpoint = f'{API}/{m.group(1)}/status/{m.group(2)}'
    deadline = time.monotonic() + (timeout or TIMEOUT)
    try:
        resp = request(endpoint, {'Accept': 'application/json'}, deadline=deadline)
        payload = json.loads(resp.body.decode('utf-8', 'replace'))
    except FeedError as e:
        # **只取分号前那一半**:`fetching` 给的是订阅采集的成品文案,后半截
        # 「请检查来源或更换订阅桥接」在长文语境里是胡话 —— 这一步跟订阅桥接
        # 毫无关系,照原样带出去只会把人指到一个改不出结果的设置上。
        raise ArticleUnavailable(
            f'长文接口没答复:{re.split(r"[;；]", str(e))[0].strip()}') from None
    except (OSError, ValueError, UnicodeError, RecursionError) as e:
        # `RecursionError` 不是 `ValueError`(它是 `RuntimeError` 的子类):一份
        # 嵌套几千层的回包让 `json.loads` 抛的正是它,而它原来从这道 except
        # 旁边漏过去、一路把整批采集带掉(2026-10-10 实测 3000 层当场复现)。
        raise ArticleUnavailable(f'长文接口的回包读不懂:{e}') from None
    if not isinstance(payload, dict) or not isinstance(payload.get('tweet'), dict):
        raise ArticleUnavailable('长文接口的回包里没有推文')
    article = payload['tweet'].get('article')
    if not isinstance(article, dict) or not article:
        # 缺失、null、空对象(以及别的空值)都是在说「这条推文没有长文」——
        # 那是一个**确定的结论**,调用方按它原样入库、不冻水位(纯图推文就是
        # 这样)。**非空的错误类型不是**:那是回包在撒谎,而把它当成"确定没有"
        # 会放行一条**空正文**落库、水位照常推进、后路压根不被触发,之后
        # `DO NOTHING` 之下再也补不回来(2026-10-10 第 3 轮审查实测:`article`
        # 换成一个非空列表,库里那条标题是「某某 的动态」、正文是空串、一次
        # 导航都没发,而同一会话的 `browser_fetch` 读得到正文)。口径同
        # `browser_fetch` 那条「判不准一律抛,不许返回 None」—— 两个方向的代价
        # 差着一个量级:抛出去顶多重叠重抓一次(`put_items` 按 URL 去重)。
        if article:
            raise ArticleUnavailable(
                f'长文接口给的 article 不是对象:{type(article).__name__}')
        return None                       # 确定的结论:这条推文没有长文
    return from_payload(article)


# ----------------------------------------------------------------- 后路:登录态浏览器

# **后路**:接口不可用时,在一个已经登录的浏览器里直接把文章页读出来。只有
# `browser` / `attach` 两条通道手里有浏览器(前者是我们自己起的独立 profile、
# 后者是用户日常那个),`bridge` / `feedgrab` 走不到这里 —— 它们照旧按
# 「补不到就不入库、不推进水位」办。设成 '0' 关掉这条后路。
BROWSER_FALLBACK = os.environ.get('KB_X_ARTICLE_BROWSER', '1') != '0'
# 文章页等多久。X 的长文是异步渲染的,实测首屏 2~5 秒。
PAGE_WAIT = float(os.environ.get('KB_X_ARTICLE_PAGE_WAIT', '15'))
_PAGE_POLL = 0.8

# 浏览器**此刻**停在哪一篇。`/<handle>/article/<id>` 与 `/<handle>/status/<id>`
# 两种形状各自带着那条推文的 id,而 id 是这条推文的唯一身份 —— **只比它,不比
# handle**:X 的 handle 大小写不敏感、改名之后旧地址照样跳得到。
_SPOT = re.compile(r'/(article|status)/(\d+)')

# 读一次文章页的那段脚本是 `_READ_JS`(在 `x_article_js` 里,见顶上那条 import)。


def _spot(path: str) -> tuple[str, str]:
    """一个路径(或地址)落在哪一篇上。返回 (`article`/`status`, 推文 id)。

    认不出来返回 `('', '')` —— 那是「压根不在文章页也不在推文页上」(首页、
    登录墙、`about:blank`),由调用方按"没打开"办。
    """
    m = _SPOT.search(path or '')
    return (m.group(1), m.group(2)) if m else ('', '')


def browser_fetch(session, url: str, *, wait: float | None = None,
                  deadline: float | None = None) -> dict | None:
    """后路:在 `session` 那个**已经登录**的浏览器里读这篇长文。

    返回值的两档和 `fetch` 逐字同义:`{'title','body'}` 是读到了,`None` 是
    **确定这条推文没有长文**,问不到一律抛 `ArticleUnavailable`。

    **判不准的时候一律抛,不许返回 None。** 两个方向的代价差着一个量级:把
    「有长文」说成「没有」会让一条空正文落库,而 `put_items` 是 DO NOTHING ——
    那一篇从此永久是空的、下一轮重试也救不回来;反过来把「没有长文」说成
    「问不到」只是让水位留在原处、下一轮重叠重抓一次(`put_items` 按 URL 去重)。
    所以只有 X 自己把我们弹回**这一条推文自己的**推文页那一种才算「没有」,
    登录墙、空壳、渲染超时、CDP 报错、以及**没跳过去**全部抛出去。

    `wait` 是这一页最多等多久(默认 `PAGE_WAIT`),`deadline` 是整轮预取的
    绝对截止时刻 —— 两者取先到的那个,所以一轮几十篇长文不会把整轮采集拖垮。
    """
    page = article_page(url)
    if not page:
        return None
    want = _spot(page)[1]
    limit = time.monotonic() + (PAGE_WAIT if wait is None else wait)
    if deadline is not None:
        limit = min(limit, deadline)
    try:
        nav = session.goto(page)
    except CdpError as e:
        raise ArticleUnavailable(f'长文页面打不开:{e}') from None
    # **早一道**:导航明确失败时当场说出原因,既不去读页面、也不等满那一页的
    # 上限。实测一篇 HTTP 204 的文章页会让 Chrome 的 `Page.navigate` 回包带
    # `errorText: net::ERR_ABORTED` 而**页面停在原处**(2026-10-09 第 1 轮审查
    # 用真无头浏览器复现)。**承重的是下面那道身份核对** —— 这一道只是把原因
    # 说清、省掉一次满额等待,而且一个不交回导航结果的会话(老的假会话、别的
    # 传输)下它压根不生效,所以不能靠它。
    broke = str((nav or {}).get('errorText') or '') if isinstance(nav, dict) else ''
    if broke:
        raise ArticleUnavailable(f'长文页面没打开({broke})')
    while True:
        try:
            snap = json.loads(session.eval(_READ_JS))
        except CdpError as e:
            raise ArticleUnavailable(f'长文页面读不动:{e}') from None
        except (ValueError, TypeError, UnicodeError) as e:
            raise ArticleUnavailable(f'长文页面的回包读不懂:{e}') from None
        if not isinstance(snap, dict):
            raise ArticleUnavailable('长文页面的回包读不懂')
        seen = _spot(str(snap.get('path') or ''))
        # **承重那一道:这一页到底是不是我们要的那一篇。** 导航没生效时页面停在
        # **上一篇文章**上,而那一页的正文是非空的 —— 不核对身份就会把上一篇的
        # 标题和正文写进这一条,`fill` 报成功、水位照常推进,而 `put_items` 是
        # `ON CONFLICT DO NOTHING`:**再同步也纠不回来**(2026-10-09 第 1 轮审查
        # 实测:第二篇 `/Review/article/200` 真回 HTTP 204,库里第二条存成了第一
        # 篇的标题和正文,拿正确正文重新入库返回新增 0 条)。
        #
        # 只比那个数字 id(见 `_spot`)。认不出 id 的路径(首页、登录墙、
        # `about:blank`)不在这里拦 —— 那是「没打开」,交给下面两道:正文只在
        # 身份对得上时才收,收不到就按超时抛。
        if seen[1] and seen[1] != want:
            raise ArticleUnavailable(
                f'浏览器没跳到这一篇,还停在 /{seen[0]}/{seen[1]} 上')
        if snap.get('back') and seen[1] == want:
            return None               # X 自己弹回推文页:这条确实没有长文
        # **正文只在"确实停在这一篇的文章页上"时才收。** 今天这和 JS 那边的
        # `at` 同真同假(两者都从同一份 `path` 算),分成两处是因为它们回答的
        # 不是一个问题:`at` 回答形状(还在文章页吗),这里回答身份(是哪一篇)。
        if seen == ('article', want):
            # `_rows`:非列表一律当空。同一条口径的另一侧 —— 后路恰恰是
            # 「接口挂了」那一刻走的,它自己也漏出 `TypeError` 的话,同一场
            # 事故只是换了个位置(整批账号一起带掉)。当空 = 这一页还没渲染
            # 出正文,交给下面那道超时。
            #
            # **而列表里那个成员不许丢掉** —— 口径同 `_must_block`,而且这是
            # 同一条缺陷的两个位置:丢掉它之后剩下的正常段照样拼出一段**非空**
            # 正文,于是「正文读到了」、水位照常推进,而丢掉的那一段是这篇文章
            # 的正文,`put_items` 是 `ON CONFLICT DO NOTHING`、再也补不回来。
            # 空值(`''` / `None` / `0`)跳过:它们携带不了任何内容线索。
            blocks = []
            for row in _rows(snap.get('blocks')):
                if isinstance(row, str):
                    if row.strip():
                        blocks.append(row)
                elif row:
                    raise ArticleUnavailable(
                        f'文章页读出来的正文块不是文字:{type(row).__name__}')
            if blocks:
                body = '\n\n'.join(blocks)[:MAX_CHARS].strip()
                seen_title = snap.get('title')
                title = seen_title.strip() if isinstance(seen_title, str) else ''
                return {'title': title, 'body': body}
        if time.monotonic() > limit:
            raise ArticleUnavailable(
                '长文页面没渲染出正文' if snap.get('at')
                else '长文页面没打开(多半是这个浏览器没登录 X)')
        time.sleep(_PAGE_POLL)


def _over(deadline: float | None, should_stop=None) -> str:
    """还能不能再为一条长文发请求。返回拦下的原因,空串 = 可以继续。

    **这一步跑在账号的时间线已经读完之后**,所以它既不在 `_harvest_one` 那个
    账号级上限(`ACCOUNT_SECONDS`)里,也不在整轮那道「进下一个账号之前检查」
    里 —— 一个发了几十篇长文的账号会在这两道闸之间串行启动几十次新请求,而
    `_GIVE_UP_AFTER` 只数**连续失败**:每一次成功都把它清零,所以它限不住
    「大量慢但成功」的请求(2026-10-09 第 1 轮审查实测:预算 0.020 秒的一轮,
    停止请求在第一条之后就置位了,后面照样新发 7 次请求、总耗时 0.323 秒,
    8 条全部留下、`incomplete` 是空的、水位照常前进)。

    两件事按同一个口径答,因为调用方的下一步是同一个:把这一条扣留下来。
    """
    if should_stop and should_stop():
        return '已按停止请求中断'
    if deadline is not None and time.monotonic() > deadline:
        return '本轮时间预算用完'
    return ''


def _slice(deadline: float | None, cap: float) -> float:
    """这一次请求最多还能花多久。没有整轮预算时就是 `cap`。

    `_over` 已经保证进到这里时预算还没过,所以这个差值是正的;下界只是防
    浮点边界,不是一个有意义的"最少给它这么久"。
    """
    if deadline is None:
        return cap
    return min(cap, max(0.01, deadline - time.monotonic()))


def _one(entry: dict, session, *, deadline: float | None = None,
         should_stop=None) -> dict | None:
    """一条推文的长文:先问接口,接口不可用就走浏览器后路。

    **两条路的「没有长文」都是确定结论**(返回 None),两条路都问不到才抛
    `ArticleUnavailable`。接口答了「没有长文」时**不再走后路** —— 那是一个
    确定的答复,再开一张标签页只是白等。
    """
    if API:
        try:
            return fetch(entry['url'], timeout=_slice(deadline, TIMEOUT))
        except ArticleUnavailable:
            if session is None or not BROWSER_FALLBACK:
                raise
            # **接口切到浏览器之前再问一次。** 开一张标签页、等它渲染是这一步
            # 最贵的动作(默认上限 15 秒),预算已经用完或用户已经按了停止时
            # 别再启动它 —— 那一条照旧扣留,下一轮重试。
            if _over(deadline, should_stop):
                raise
        return browser_fetch(session, entry['url'], deadline=deadline)
    # 接口被整个关掉(`KB_X_ARTICLE_API=''`):手里有浏览器就只走后路,没有就
    # 什么都不做 —— 后者的行为逐字退回这一步出现之前。
    if session is None or not BROWSER_FALLBACK:
        return None
    return browser_fetch(session, entry['url'], deadline=deadline)


def merge(entry: dict, got: dict) -> dict:
    """把取到的长文并进这一条推文。**口径只有这一份**,两个消费方都过它。

    三段按原顺序:转推上下文(`> …`)→ 推文自己写在推文里的介绍 → 文章正文。
    上下文那一段**不能漏**,理由见 `quote_lines`;介绍那一段靠 `lead_text` 摘,
    它顺手去掉那条指向长文的裸链接(那不是作者写的字)。

    消费方:定时采集那条路(`fill`)和手动收录(`kb.feeds.grab`)。各写一份的话
    同一篇长文从两条路进来会长出两种正文 —— 而先到的那一份被 `put_items` 的
    `DO NOTHING` 钉死,另一条路再也覆盖不了它。
    """
    src = entry.get('body') or ''
    parts = (quote_lines(src), lead_text(src), got['body'])
    return {**entry, 'title': (got['title'] or entry.get('title') or '')[:500],
            'body': '\n\n'.join(p for p in parts if p)}


def fill(entries: list[dict], *, session=None, links_only: bool = False,
         deadline: float | None = None, should_stop=None) -> tuple[list[dict], str]:
    """把这一批里的长文正文补上。返回 (留下的条目, 没补全的原因)。

    **原因非空 = 这一轮没补全**,调用方把它并进 `incomplete`,于是水位留在原处、
    下一轮重叠重抓时再试(理由见模块注释:空正文入库之后就改不回来了)。补不到的
    那几条**本轮不入库** —— 入库了下一轮的重试也更新不了它们,留着等于白等。

    `session` 是一个还活着的 CDP 会话(`browser` / `attach` 两条通道手里有),
    接口不可用时拿它走后路。给 None 就只有接口这一条路(桥接、feedgrab)。
    `links_only` 见 `needs_article`:桥接那一档只认正文里明写的文章地址。

    `deadline`(整轮预取的绝对截止时刻)和 `should_stop` 由两条预取通道按它们
    自己那份预算传进来,**在每一条新请求之前各问一次**(见 `_over`):超限或
    已按停止时,补到的照旧留下、没补到的扣留、原因并进 note —— 于是水位留在
    原处,下一轮接着补。不传两者 = 没有上限,行为逐字同从前。
    """
    if not API and (session is None or not BROWSER_FALLBACK):
        return [_strip_hint(e) for e in entries], ''
    kept, missed, fails, halt = [], 0, 0, ''
    for entry in entries:
        want = needs_article(entry, links_only=links_only)
        # **每一条之前都重新问一次**,不是进这个函数时问一回:停止请求和预算
        # 都可能在这一批跑到一半时才成立,而这一批正是几十次串行请求。
        if want and not halt:
            halt = _over(deadline, should_stop)
        if not want or halt or fails >= _GIVE_UP_AFTER:
            if want:
                missed += 1          # 该补而没补:扣留,下一轮重试
                continue
            kept.append(_strip_hint(entry))
            continue
        try:
            got = _one(entry, session, deadline=deadline, should_stop=should_stop)
            fails = 0
        except ArticleUnavailable:
            fails += 1
            missed += 1
            continue
        if got:
            entry = merge(entry, got)
        kept.append(_strip_hint(entry))
    note = ''
    if missed:
        note = f'有 {missed} 条推文的长文正文没取到,留到下次同步再试'
        if halt:
            note = f'{note}({halt})'
    return kept, note
