# -*- coding: utf-8 -*-
"""手动收录:给一条链接,把内容抓回来写进知识库。

**硬约束在 `docs/decisions/link-grab.md` 第一节 —— 改这块之前先读那一份。**

**为什么是一类来源,而不是「往某条订阅里塞一条」**:塞进 `rss` 或 `social` 的话
那条内容从此混在订阅内容里,检索按来源筛的时候分不出「我自己存的」;而给它
真·订阅的身份(水位、etag、退避、定时)更糟 —— 一条文章地址不是一个可以轮询的
源。所以它是第六类来源 `inbox`(`kb.source_types`),`pollable=False`:**crawl
阶段对它空转**,抓取发生在用户给出链接的那一刻;**后面那半条链一字不改地复用**
—— 翻译、导出 vault、索引、提取、检索、图鉴、日报全部由 `feed_types()` 自动
派生出来(见 `kb/config.py`)。

**抓不到正文一律不入库。** `registry.put_items` 是 `ON CONFLICT(key) DO NOTHING`
—— 一条空正文落了库就**再也改不回来**,而这一步失败的代价只是用户再点一次
(按 URL 去重,重抓不会产生重复条目)。所以这个模块里所有「拿不准」都抛
`GrabError`,一个字都不往库里写。口径同 `x_article` 那条「判不准一律抛」。

**两条取法,按地址分流**(`classify`):
①**X 的单条推文**走 FxTwitter 的公开接口 —— 和 `x_article` 同一个接口、同一个
  超时、同一份 article 渲染;长文的正文也顺手取回来(复用 `x_article.merge`)。
  **不需要登录态**,所以手动收录不受「X 取法」那个设置影响:哪条通道都收得了。
②**其他任何网页**走一次 HTTP + 正文提取(`grab_page`),正文提取器仍然只有
  `readable` 那一份口径。

**绿泡泡的文章和别的站点走同一条路**(②):发一次请求,读到正文就收下,**实际
撞上验证页才拒**,那一句指向真正管这件事的那条通道(读本机绿泡泡数据库,见
`kb/collectors/wechat_local.py`)。实测多数 `mp.weixin.qq.com/s/<slug>` 确实会
302 到 `/mp/wappoc_appmsgcaptcha`(og 元信息和 `#js_content` 全没有)—— 那不是
bug,是对方的反自动化关卡,我们**不绕**它;但那是**那个响应**的结论,不是这个
域名的结论(按域名先拒的代价见 `classify`)。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from kb.feeds import grab_page, readable, registry as reg, x_article
# 「库里那一条」那一族拆到了 grab_store(见那份模块说明)。**原样再导出**:
# `grab.known` / `grab.owing` / `grab.replace` / `grab.reclaim_export` / `grab.KIND`
# 是所有调用方和决策文档引用的那几个名字。
from kb.feeds.grab_store import (KIND, known, owing,  # noqa: F401
                                 reclaim_export, replace)
from kb.feeds.fetching import FeedError, body_text, canonical_url, oneline, request
from kb.feeds.grab_page import PageUnavailable

#: 整条收录的墙钟上限。X 那边最多两次接口请求(推文 + 长文),网页那边一次 HTTP;
#: 用户正盯着这颗按钮等,所以比采集那一侧(`SOURCE_SECONDS`)给得紧一些。
GRAB_SECONDS = 40
SHANGHAI = ZoneInfo('Asia/Shanghai')

#: X 的域。**只认这几个**:判据是「等于其中一个」而不是 `endswith` ——
#: 后者会把 `notx.com`、`fakex.com` 一起圈进来,然后拿一个陌生站点的地址去问
#: FxTwitter(口径同 `x_login_import` 那条域判断)。
_X_HOSTS = frozenset(('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com',
                      'mobile.twitter.com', 'mobile.x.com'))
#: 推文地址里的 handle 和 id。`x_article._STATUS` 认的是「正文里出现过」,
#: 这一条认的是「整个路径就是它」—— 手动收录拿到的是用户粘的那一条地址本身。
_STATUS_PATH = re.compile(r'^/([A-Za-z0-9_]{1,20})/status/(\d+)/?$')
#: 绿泡泡文章地址(见模块注释:这条路抓不到,要说清去处)。
_WECHAT_HOSTS = frozenset(('mp.weixin.qq.com', 'weixin.qq.com'))

#: 收件桶的名字。桶 = `inbox` 这一类下的一条订阅,**按站点一个** —— 面板上
#: 那排卡片于是读得出「这个站存了几篇」,而条目列表能按它筛。
_BUCKET_LABELS = {'x.com': 'X / Twitter', 'mp.weixin.qq.com': '绿泡泡公众号'}


class GrabError(ValueError):
    """这条链接没收下来。消息面向用户,**而且必须说清下一步**。"""


def normalize(raw: str) -> str:
    """用户粘进来的那一串 → 规范地址。

    **规范化只有 `canonical_url` 这一份口径**(它顺手去掉 utm_*/fbclid/gclid、
    把推文地址收成 `x.com/<handle>/status/<id>`):`put_items` 的键是
    `sha256(kind + ':' + url)`,两处规范化不一致时同一篇内容从两条路进来会变成
    两条记录、两篇 md、检索里两份结果。

    **`social=True` 只给 X 自己的域,这一句是承重的。** 那一档的判据只有路径
    (`/某人/status/数字`),域名一概不看 —— 它本来服务的是「这条 feed 是 X 的
    镜像桥接」那个上下文(`SocialAdapter`,nitter / xcancel 给出的链接正是要被
    改写成 x.com,`tests/test_feed_sources.py` 钉着)。手动收录拿到的是用户随手
    粘的任意一条地址,**没有这个上下文**:无条件开着它,
    `https://notx.com/alice/status/12345` 会在分流之前就被改写成
    `https://x.com/alice/status/12345`,然后拿一个陌生站点的地址去问 FxTwitter、
    把另一条推文的内容收成「用户要的那一篇」(2026-10-16 审查实测)。
    `classify` 的域白名单挡不住这一档 —— 原域名那时已经没了。

    **所以这道约束待在调用方,不在 `canonical_url` 里**:共享层那一档对它唯一
    的老消费方是真话,改成「只认 X 域名」会把桥接镜像的规范化一起停掉。
    """
    value = (raw or '').strip()
    if not value:
        raise GrabError('请先给一条链接')
    if '://' not in value:
        # 从别处复制过来的地址常常掉了协议头。补 https 再按同一套规则校验 ——
        # 补不出合法地址的照旧由 `canonical_url` 拒掉。
        value = 'https://' + value.lstrip('/')
    try:
        return canonical_url(value, social=(urlsplit(value).hostname or '').lower() in _X_HOSTS)
    except ValueError as e:
        raise GrabError(f'{e}') from None


def classify(url: str) -> str:
    """这条地址走哪条取法:`'x'`(单条推文)或 `'web'`(其他网页)。

    **只有「单条推文」算 X 那一档**:账号主页、搜索页、列表页都不是一篇内容 ——
    它们该去「大V观点」做一条订阅,而不是被收成一篇空文章。

    **绿泡泡的文章不在这里特判,和别的站点走同一条路**:发一次请求,读到正文就
    收下,**实际撞上验证页**才拒(那一句指向「公众号」那一类,见 `_gate`)。
    按域名先拒等于替对方的服务器回答 —— 转载页、镜像、以及对方哪天放开,都会被
    一句「这条路抓不到」挡在外面。代价如实写在 `link-grab.md`:真能直接读到的
    那一篇会在收件桶和公众号树下各留一份(kb.db 的 `documents.url` 没有唯一约束,
    跨树重复本来就有)—— 两处都搜得到,不是丢数据。
    """
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if host in _X_HOSTS:
        if not _STATUS_PATH.match(parts.path or ''):
            raise GrabError('这是 X 的页面但不是单条推文。想按账号持续收的话，'
                            '去「大V观点」把它加成一条订阅。')
        return 'x'
    return 'web'


def bucket_label(host: str) -> str:
    """站点 → 收件桶的名字。认识的给人话名,其余就用它的域名。

    **截到 120 字**:`save_subscription` 的名字上限正是 120,超了它抛 `ValueError`
    —— 而那一步发生在正文已经抓好之后,整条收录会以一个 500 收场(合法域名最长
    253 字节,不是构造不出来的输入)。名字只是面板上那行字,`UNIQUE(kind,url)`
    认的是地址,截短不会把两个站点合成一个桶。
    """
    host = (host or '').lower().removeprefix('www.')
    if host in _X_HOSTS or host in _BUCKET_LABELS:
        return _BUCKET_LABELS.get('x.com' if host in _X_HOSTS else host, host)
    return (host or '手动收录')[:120]


def bucket(url: str) -> dict:
    """这条地址归哪个收件桶(没有就建一个)。

    **桶是启用着的订阅**:面板要显示它、`put_items` 要挂在它身上。它不会被
    轮询 —— 这一类来源 `pollable=False`,crawl 阶段在 `sync.collect` 开头就
    空转了(那一道是承重的,见它的注释)。

    `extract=True`:手动收录正是「谁值得买一次提取」这个问题的肯定答案 ——
    用户亲手挑的内容(口径见 `kb.extract.pending_docs` 和 `extract-optin.md`)。
    `fulltext=False` 是**必须**的:那个开关会把条目扣在导出闸门外等「去原文
    补正文」,而正文此刻已经在手里了。
    """
    host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
    # IPv6 的 `hostname` 是**脱了方括号**的(`2001:db8::1`),照原样拼回去得到
    # 的是一个 `netloc` 带端口的畸形地址,`normalize_url` 当场拒掉 —— 而这一步
    # 在正文抓好之后,拒掉就是一个 500。
    home = f'https://[{host}]/' if ':' in host else f'https://{host}/'
    for source in reg.subscriptions(KIND):
        if source['url'] == home:
            return source
    try:
        return reg.save_subscription(KIND, {'name': bucket_label(host), 'url': home,
                                            'enabled': True, 'extract': True,
                                            'fulltext': False})
    except ValueError:
        # 两条投递同时撞上同一个新站点:`UNIQUE(kind,url)` 挡住后一个。判据待在
        # 写语句自己的约束里(元规则),所以这里只需要重查一次就拿到赢的那一条。
        for source in reg.subscriptions(KIND):
            if source['url'] == home:
                return source
        raise


# ------------------------------------------------------------------ X 的单条推文

def _as_published(stamp) -> str:
    """推文的 epoch 秒 → 项目里 published 的统一写法(Asia/Shanghai,分钟精度)。

    格式必须和 `fetching.parse_feed`、`x_browser._as_published` 逐字一致:日报
    按 `published` 做字符串比较筛窗口,两处格式不一样的话同一天的内容会被算进
    不同的窗口。**宿主机时区不参与** —— 口径同元规则那一条。
    """
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        return ''
    try:
        return datetime.fromtimestamp(float(stamp), SHANGHAI).strftime('%Y-%m-%d %H:%M')
    except (ValueError, OSError, OverflowError):
        return ''


def _media_lines(media) -> str:
    """纯图 / 纯视频推文的正文:媒体地址各占一行。

    **不是为了好看,是为了这一条不要变成空正文** —— 空正文一旦落库,`DO NOTHING`
    之下永久补不回来(模块注释那一条)。地址是 X 自己的 CDN,markdown 的图片语法
    让 vault 里那一篇点开就看得见。
    """
    if not isinstance(media, dict):
        return ''
    lines = []
    # **容器自己的形状也要验**:第三方回包里 `photos` 是个 dict 或数字时,
    # `(… or [])[:8]` 对前者切出一堆键名、对后者直接 TypeError —— 那会把一条
    # 文字完好的推文带成 500(端点只翻 `GrabError`)。口径同下面那两个
    # `isinstance`:**形状不对就当它没有媒体**,文字那一半照旧收得下。
    photos = media.get('photos') if isinstance(media.get('photos'), list) else []
    videos = media.get('videos') if isinstance(media.get('videos'), list) else []
    for photo in photos[:8]:
        if isinstance(photo, dict) and isinstance(photo.get('url'), str):
            lines.append(f"![图片]({photo['url']})")
    for video in videos[:4]:
        if not isinstance(video, dict):
            continue
        link = video.get('url') if isinstance(video.get('url'), str) else ''
        thumb = video.get('thumbnail_url') if isinstance(video.get('thumbnail_url'), str) else ''
        if thumb:
            lines.append(f'![视频封面]({thumb})')
        if link:
            lines.append(f'[视频]({link})')
    return '\n'.join(lines)


def _quote_line(quote) -> str:
    """被引用的那条推文:一行 `> @谁：说了什么`。

    形状同 `x_browser._entry` 给转推上下文留的那一行(`> …`)—— 下游
    (`x_article.lead_text` / `quote_lines`)按这个前缀分辨「哪一段是作者自己
    写的字」,换个写法就会把引用当成正文的一部分送去补长文。
    """
    if not isinstance(quote, dict):
        return ''
    author = quote.get('author') if isinstance(quote.get('author'), dict) else {}
    who = oneline(str(author.get('screen_name') or author.get('name') or ''), 'text')
    text = oneline(str(quote.get('text') or ''), 'text')[:280]
    if not who and not text:
        return ''
    return f"> 引用 @{who}：{text}" if who else f'> 引用：{text}'


def _tweet_payload(url: str, deadline: float) -> dict:
    """问一次这条推文。**错误分档,一档一句话。**

    这是 `x_article` 用的同一个接口(同一个 `API` 常量、同一份超时),只是取的是
    另一半字段:`tweet.text` / `author` / `created_timestamp` / `media` / `quote`。
    长文那一半照旧交给 `x_article` —— 它那套形状校验和「空正文一律抛」的口径
    一个字都不该在这儿重写。
    """
    if not x_article.API:
        raise GrabError('X 的公开接口被关掉了（KB_X_ARTICLE_API 是空的），'
                        '这条推文没法收。')
    m = _STATUS_PATH.match(urlsplit(url).path or '')
    if not m:                            # classify 已经挡过一次,这是构造上的第二道
        raise GrabError('这不是一条推文的地址')
    endpoint = f'{x_article.API}/{m.group(1)}/status/{m.group(2)}'
    try:
        resp = request(endpoint, {'Accept': 'application/json'}, deadline=deadline)
        payload = json.loads(resp.body.decode('utf-8', 'replace'))
    except FeedError as e:
        # 只取分号前那一半:后半截「请检查来源或更换订阅桥接」在这儿是胡话
        # (口径同 `x_article.fetch`)。
        raise GrabError(f'X 的公开接口没答复：{re.split(r"[;；]", str(e))[0].strip()}'
                        '；稍后再试一次') from None
    except (OSError, ValueError, UnicodeError, RecursionError) as e:
        # `RecursionError` 不是 `ValueError`(它是 `RuntimeError` 的子类),
        # 一份嵌几千层的回包让 `json.loads` 抛的正是它(见 `x_article.fetch`)。
        raise GrabError(f'X 的公开接口回包读不懂：{e}') from None
    if not isinstance(payload, dict) or not isinstance(payload.get('tweet'), dict):
        # 删掉的、仅粉丝可见的、以及 handle 打错的都落在这一档。
        raise GrabError('这条推文读不到：可能已被删除、设为仅关注者可见，'
                        '或者地址里的用户名不对。')
    return payload['tweet']


def _grab_x(url: str, deadline: float) -> dict:
    """一条推文 → 待入库的条目。

    条目形状(标题回落成「某某 的动态」、`[:500]`、`content_kind='post'`)逐字
    照 `x_browser._entry`:同一条推文从采集通道和手动收录两条路进来,库里那行
    必须长得一样 —— 先到的那一份被 `DO NOTHING` 钉死,后到的覆盖不了它。
    """
    tweet = _tweet_payload(url, deadline)
    author = tweet.get('author') if isinstance(tweet.get('author'), dict) else {}
    name = oneline(str(author.get('name') or author.get('screen_name') or ''), 'text')
    # **文字只收字符串**:强转出来的 `"{'text': …}"` 非空,会被当成「这条有正文」
    # 落库(口径同 `x_article._must_text`)。
    #
    # **正文按纯文本处理、换行一个都不许压掉**:推文的分行就是作者的分段,
    # 而 `oneline`/HTML 解析两样都会把它们抹平(`body_text(…, 'text')` 走的是
    # `_plain_lines`,不碰 BeautifulSoup)。标题那一头反过来必须是单行。
    raw = str(tweet.get('text')) if isinstance(tweet.get('text'), str) else ''
    text = body_text(raw, 'text').strip()
    pieces = [p for p in (_quote_line(tweet.get('quote')), text,
                          _media_lines(tweet.get('media'))) if p]
    entry = {'url': url, 'title': (oneline(text, 'text') or f'{name} 的动态')[:500],
             'body': '\n\n'.join(pieces), 'author': name,
             'published': _as_published(tweet.get('created_timestamp')),
             'content_kind': 'post'}
    # **回包里那个结构化的长文信号先收下,不许先丢掉、再凭展示文本去推断。**
    # `tweet.article` 是生产者自己的签名「这条推文是一篇长文」,而
    # `needs_article` 的三条判据全是**间接**信号(正文里写着文章地址 / 时间线上
    # 那张卡片 / 正文是空的)。长文推文的 `text` 完全可能是文章标题或一句介绍
    # (上游 FxEmbed 自己的渲染器就按 `text === article.title` 处理),再配一张图
    # 连「空正文」那条也被挡住 —— 于是我们连问一次都不会问,随后 `DO NOTHING` 把
    # 这份只有标题/只有图的正文**永久**钉成那一篇(2026-10-16 第 3 轮审查实测三种
    # 形态各一例,`followup_calls: 0`)。
    #
    # 信号挂成已有的那个键(`x_article.HINT`,口径同浏览器通道
    # `x_browser._entry` 读那张卡片),于是「要不要去取长文」仍然只有
    # `needs_article` 这一份判据 —— 这里不新增第二个问法。
    raw = tweet.get('article')
    article = raw if isinstance(raw, dict) and raw else None
    # **形状认不出来也算信号**(非空的非 dict):那是回包在说「有长文」而我们读不懂
    # 它,按「没有长文」放过去就是放行一条残缺正文(口径同 `x_article.fetch` 对
    # `article` 的分档)。去问一次接口,问不到就拒。
    if raw:
        entry[x_article.HINT] = url
    # **没有浏览器后路**(手动收录不持有 CDP 会话,同 `bridge` 那一档),所以取不
    # 到就抛、什么都不写。
    if x_article.needs_article(entry):
        got, problem = None, ''
        try:
            got = x_article.fetch(url, timeout=max(1.0, deadline - time.monotonic()))
        except x_article.ArticleUnavailable as e:
            problem = f'{e}'
        if got is None and article:
            # 接口那一跳没成(超时、回包坏、或者它说「没有长文」),而**手里这份
            # 回包自己就带着文章内容** —— 用它,别丢掉。判据、渲染、拼接照旧是
            # `x_article` 那一份(`from_payload` 就是 `fetch` 的尾段)。
            try:
                got = x_article.from_payload(article)
            except x_article.ArticleUnavailable as e:
                problem = problem or f'{e}'
        if got:
            entry = x_article.merge(entry, got)
        elif raw:
            # **确认是长文、正文又没拿到:一个字都不许写。** 这一条落库之后
            # `DO NOTHING` 之下再投一次同一条链接也换不回真正文。
            raise GrabError('这条推文是一篇长文，正文这次没取到：'
                            f'{problem or "接口没给出正文"}；稍后再点一次')
        elif problem:
            raise GrabError(f'这条推文是一篇长文，正文这次没取到：{problem}；'
                            '稍后再点一次')
    # 那个信号键**不进 `feed_items`**:`put_items` 把整份条目哈希成
    # `content_hash`,多一个只在抓取期间有意义的键会让同一条推文从采集通道和手动
    # 收录两条路进来算出两个哈希(剥离口径同 `fill`,共用 `x_article._strip_hint`)。
    entry = x_article._strip_hint(entry)
    if not entry['body']:
        # 走到这儿 = 接口说「它没有长文」而推文自己又一个字一张图都没有。
        # **不许入库**:那是一条永久空正文(模块注释那一条)。
        raise GrabError('这条推文没有可保存的文字或图片')
    return entry


# ---------------------------------------------------------------------- 其他网页

def _next_step(final_url: str) -> str:
    """没收下之后那半句「下一步」。**按站点分,而且只有这一份口径。**

    绿泡泡的文章这条路确实走不通(实测 302 到 `/mp/wappoc_appmsgcaptcha`,og 和
    `#js_content` 全无),而它有一条自己的通道:把人指过去。照通用那句说「在浏览
    器里打开、把正文复制出来」等于让他对着一道过不去的验证页发呆。

    **判据是实际响应,域名只决定这半句话怎么说** —— 按域名先拒掉那一版的代价写在
    `classify` 的注释里。
    """
    host = (urlsplit(final_url).hostname or '').lower()
    if host in _WECHAT_HOSTS:
        return ('绿泡泡的文章走「公众号」那一类：在绿泡泡里打开一次，'
                '它就进了本机数据库，同步公众号时会收进来。')
    return '在浏览器里打开它、把正文复制出来，或者换一个可直接访问的地址。'


def _gate(reason: str, final_url: str = '') -> GrabError:
    """被关卡挡住那一句:**为什么没收下**(判据给的那半句)**+ 下一步**。"""
    return GrabError(f'{reason}，所以正文抓不到。{_next_step(final_url)}')


def _grab_web(url: str, deadline: float) -> dict:
    """一个网页 → 待入库的条目。正文口径只有 `readable` 那一份。

    **「提取成功」证明不了「拿到了那篇文章」。** 所以关卡判据排在**所有出路的
    前面,成功和失败共用一道**(`grab_page.gate_reason`,判据和那条不变量都在它
    自己头上):一张验证页提取得出一段文字、长度轻松过关,随后被 `put_items` 的
    `DO NOTHING` 永久钉成这一篇的正文,再投一次同一条链接也换不回真文章(两轮
    审查各实测到一例:683 字那张落在 `/challenge` 上,1782 字那张谁都没拦住)。
    **这里只有一个调用点** —— 成功路和失败路各问一次的话,同一张验证页会得到
    两句不同的话,而其中一条迟早漏掉一个判据。
    """
    try:
        resp = request(url, grab_page.HEADERS, deadline=deadline)
    except FeedError as e:
        raise GrabError(f'{re.split(r"[;；]", str(e))[0].strip()}') from None
    if problem := readable.html_problem(resp.body, dict(resp.headers)):
        # 「这后面压根不是网页」要认出来:一份 PDF 解码出来是一段看着像文字的
        # 乱码,提取器眼里它就是整篇正文、长度还轻松过关(口径同 `fulltext`)。
        raise GrabError(f'{problem}')
    html = readable.decode(resp.body, dict(resp.headers))
    final = resp.url or url
    # **先把提取的结果收在手里,别急着用它答任何话。** 关卡那一问要先过:
    # 「提取失败」也不许抢在它前面答 —— 「这一页没找到正文」指不出下一步,而这
    # 一页明摆着是一道验证页。
    body, unavailable = '', ''
    try:
        body = grab_page.page_body(html)
    except PageUnavailable as e:
        unavailable = f'{e}'
    if reason := grab_page.gate_reason(final, html, body):
        raise _gate(reason, final)
    if unavailable:
        # 读不到正文这一档也要带上下一步(口径同 `_gate`:同一个 `_next_step`)。
        # 「这一页没找到正文」自己指不出任何事 —— 折叠成一句抓取失败就是把人留在
        # 原地(模块注释那一条)。
        raise GrabError(f'{unavailable}。{_next_step(final)}')
    meta = grab_page.page_meta(html, final)
    host = (urlsplit(final).hostname or '').lower().removeprefix('www.')
    return {'url': url, 'title': grab_page.title_fallback(meta['title'], body, host),
            'body': body, 'author': meta['author'] or host,
            'published': meta['published'], 'content_kind': 'content'}


# -------------------------------------------------------------------------- 入口

def grab(url: str, *, deadline: float | None = None) -> dict:
    """抓一条链接的内容。**只抓,不写库**(写库是 `save`)。

    分两步是刻意的:抓取要发 HTTP、可能慢、可能失败,而写库那一下要么全成要么
    什么都没动。中间那一刻还能回答「这条已经在库里了」(`known`)。
    """
    url = normalize(url)
    deadline = deadline if deadline is not None else time.monotonic() + GRAB_SECONDS
    return (_grab_x if classify(url) == 'x' else _grab_web)(url, deadline)


def save(entry: dict) -> dict:
    """把抓好的条目写进 `inbox`。返回 `{'added', 'source', 'item'}`。

    **`added=0` 不是失败,多半正是想要的末态**(元规则):同一条链接再投一次时
    `put_items` 的 `DO NOTHING` 挡住它,库里那一份一个字节都没动 —— 调用方据此
    对用户说「这条已经收过了」,而不是报错。
    """
    if not (entry.get('url') and entry.get('body')):
        # 构造上的最后一道:空正文永久改不回来,所以绝不让它走到 INSERT。
        raise GrabError('没有可保存的正文')
    source = bucket(entry['url'])
    added = reg.put_items(source, [entry])
    return {'added': added, 'source': source, 'item': entry}
