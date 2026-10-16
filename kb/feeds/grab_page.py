# -*- coding: utf-8 -*-
"""手动收录的网页那一侧:一个地址 → 标题、作者、发布时间、正文。

**和 `kb.feeds.fulltext` 的分工**:那一条是「订阅给了壳,去原文补正文」,正文
之外的字段(标题、作者、时间)它一概不碰 —— feed 已经给过了。手动收录这一侧
**什么都没有**,所以这里多出一层元信息解析。正文提取本身仍然只有一份口径
(`readable.extract_or_reason`),这个文件一个字都不重写它。

**抓不到就抛,不许返回一个空壳**(口径同元规则「判不准一律拒」):`put_items`
是 `ON CONFLICT DO NOTHING`,一条空正文落了库就再也改不回来,而重抓一次的代价
只是再发一次 HTTP。登录墙、验证页、PDF 这几种**各给一句自己的话** —— 它们的
下一步完全不同(换个地址 / 在浏览器里打开一次 / 这不是网页),折叠成「抓取失败」
等于把人留在原地。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from kb.feeds import readable
from kb.feeds.fetching import oneline, plain

# 抓网页不是抓 feed,`fetching.request` 的默认 Accept 是 RSS 优先 —— 有些站会
# 据此回一份 XML 或者干脆 406。口径同 `fulltext._HEADERS`,多一条中文优先。
HEADERS = {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
           'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5'}
# 正文最多留多长。口径同 `x_article.MAX_CHARS`。
MAX_CHARS = 60000

#: 落到这些地址上 = 对方把内容挡在了一道人工关卡后面。**判据是最终地址**
#: (`request` 跟完重定向之后那一个),不是正文内容:正文此刻就是那张关卡页,
#: 提取器只会说「没找到正文」,而真正该说的是「它要你先过一道验证」。
#: 绿泡泡实测(2026-10-16,`mp.weixin.qq.com/s/<slug>`):302 到
#: `/mp/wappoc_appmsgcaptcha?poc_token=…&target_url=…`,18 KB 的验证页,
#: og 元信息和 `#js_content` 全都没有。
_GATE_PATHS = ('appmsgcaptcha', 'wappoc_', '/checkpoint', '/challenge',
               '/cdn-cgi/l/chk', '/sorry/index')
_GATE_WORDS = ('环境异常', '完成验证', '请在微信客户端打开', 'enable javascript',
               'verify you are human', 'checking your browser', 'captcha')

SHANGHAI = ZoneInfo('Asia/Shanghai')

#: 发布时间:结构化字段优先。**「改动时间」不在这张表里** —— 它单独排在所有
#: 发布时间之后(`_MODIFIED_META`):实测 derekthompson.org 那一篇同时给了
#: `article:modified_time`(19:17Z)和 JSON-LD 的 `datePublished`(10:02Z),把
#: 改动时间混进这张表就等于按「最后编辑于」给这一篇钉发布时间 —— 那个值会一路
#: 进日报的时间窗口,而且每次对方改一个错别字就变一次。
_TIME_META = ('meta[property="article:published_time"]', 'meta[property="og:published_time"]',
              'meta[name="article:published_time"]', 'meta[itemprop="datePublished"]',
              'meta[name="date"]', 'meta[name="pubdate"]', 'meta[name="publish_date"]')
#: 最后的回落:只有一个「改动时间」时它也比「没有时间」强(列表排序、日报窗口
#: 都按 published 走),但必须排在**所有**发布时间来源之后。
_MODIFIED_META = ('meta[property="article:modified_time"]', 'meta[itemprop="dateModified"]')
#: 作者。站点名排在人名后面:一篇文章的作者比「这是哪个站」更具体。
_AUTHOR_META = ('meta[name="author"]', 'meta[property="article:author"]',
                'meta[name="twitter:creator"]', 'meta[property="og:site_name"]',
                'meta[name="application-name"]')
_TITLE_META = ('meta[property="og:title"]', 'meta[name="twitter:title"]',
               'meta[name="title"]')
#: ISO 8601 的那一截日期时间。时区由 `_as_published` 的调用方统一补齐。
_ISO = re.compile(r'(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?')


class PageUnavailable(ValueError):
    """这一页读不出可保存的内容。消息直接面向用户,说清下一步。"""


def gate_reason(final_url: str, html: str) -> str:
    """这一页是不是一道人工关卡。空串 = 不是。

    两头各看一眼:**地址**认得出已知的那几道关卡(重定向之后才看得见),**正文**
    认得出没有重定向、就地渲染一句「请完成验证」的那种。两道都只在**提取不出
    正文**时才问 —— 一篇正常文章里出现「captcha」这个词不该被判成关卡。
    """
    low = (final_url or '').lower()
    if any(p in low for p in _GATE_PATHS):
        return '对方把这个地址挡在了一道验证页后面'
    head = (html or '')[:200000].lower()
    for word in _GATE_WORDS:
        if word in head:
            return '这一页要求先完成验证或登录'
    return ''


def _first_meta(soup, selectors) -> str:
    """按顺序取第一个非空的 meta content。

    **一律按纯文本处理**(`oneline(..., 'text')`):bs4 交出来的属性值已经把
    `&lt;` 解码成 `<` 了,再过一遍 HTML 解析会把标题里的 `<T>`、`<Button>`
    当成标签整段吃掉(口径同 `fetching._is_plain` 那段注释)。"""
    for sel in selectors:
        el = soup.select_one(sel)
        value = oneline(str(el.get('content') or ''), 'text') if el else ''
        if value:
            return value
    return ''


def _ld_json(soup) -> dict:
    """JSON-LD 里的那一篇文章。拿不到就空 dict —— 它只是补充,不是承重的那一道。

    **一个异常都不许漏出去**:这段数据来自页面,形状由对方决定(数组、嵌套
    `@graph`、裸字符串都见过),而它补的只是「作者是谁、什么时候发的」——
    为一个补充字段把整条收录带掉不划算。
    """
    for tag in soup.select('script[type="application/ld+json"]')[:8]:
        try:
            data = json.loads(tag.string or tag.get_text() or '{}')
        except (ValueError, TypeError, RecursionError):
            continue
        pool = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get('@graph'), list):
            pool = data['@graph']
        for node in pool:
            if not isinstance(node, dict):
                continue
            kind = node.get('@type') or ''
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(k).endswith(('Article', 'BlogPosting', 'NewsArticle', 'Report'))
                   for k in kinds):
                return node
    return {}


def _ld_author(node: dict) -> str:
    author = node.get('author') or node.get('creator') or ''
    if isinstance(author, list):
        author = author[0] if author else ''
    if isinstance(author, dict):
        author = author.get('name') or ''
    return oneline(str(author), 'text') if isinstance(author, str) else ''


def as_published(value: str) -> str:
    """任意写法的时间串 → `YYYY-MM-DD HH:MM`(**按项目时区**;缺时分只给日期)。

    **只认 ISO 那一截**,不去猜「October 15, 2026」这类自然语言写法:猜错一个
    月份就是给这一篇钉上一个假的发布时间,而它会一路进日报的时间窗口。认不出
    就留空 —— 面板和导出都按「没有发布时间」处理(列表排序回落到收录时刻,见
    `registry.items` 的 `COALESCE`),那是一句真话。

    **时区必须换算到 Asia/Shanghai**(元规则那一条):网页上的时间戳大多是
    `…T10:02:37Z`,照字面取就比真实时刻**早 8 小时** —— 实测
    derekthompson.org 那一篇的 `10:02Z` 其实是当天 18:02,而日报按 `published`
    做字符串比较筛窗口,差这 8 小时足够把一篇内容推到前一天去。
    **不带时区的那种按项目时区补齐**,不按宿主机(`astimezone` 对 naive 的默认
    是拿机器时区补,那会让同一个页面在不同机器上入库成不同时刻)。
    """
    m = _ISO.search(value or '')
    if not m:
        return ''
    if not m.group(2):
        return m.group(1)                # 只有日期:没有时刻可言,也就没有时区问题
    try:
        dt = datetime.fromisoformat((value or '').strip().replace('Z', '+00:00'))
    except ValueError:
        # 正则认得出、`fromisoformat` 认不出(多余的后缀、非标准分隔符)。
        # 时区未知,按「不带时区」那一档办 —— 原样留着日期和时分。
        return f'{m.group(1)} {m.group(2)}'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M')


def page_meta(html: str, final_url: str) -> dict:
    """标题 / 作者 / 发布时间。一个都没有也不算故障 —— 承重的是正文。"""
    soup = BeautifulSoup(html, 'html.parser')
    node = _ld_json(soup)
    title = _first_meta(soup, _TITLE_META)
    if not title and isinstance(node.get('headline'), str):
        title = oneline(node['headline'], 'text')
    if not title and (tag := soup.select_one('title')):
        title = oneline(tag.get_text(' ', strip=True), 'text')
    if not title and (tag := soup.select_one('h1')):
        title = oneline(tag.get_text(' ', strip=True), 'text')
    author = _first_meta(soup, _AUTHOR_META) or _ld_author(node)
    when = _first_meta(soup, _TIME_META)
    if not when:
        for key in ('datePublished', 'dateCreated', 'uploadDate'):
            if isinstance(node.get(key), str) and node[key]:
                when = node[key]
                break
    if not when and (tag := soup.select_one('time[datetime]')):
        when = str(tag.get('datetime') or '')
    # 走到这儿还是空的才认「改动时间」(见 `_MODIFIED_META` 那段注释)。
    if not when:
        when = _first_meta(soup, _MODIFIED_META) or (
            node.get('dateModified') if isinstance(node.get('dateModified'), str) else '')
    return {'title': title[:500], 'author': author[:120], 'published': as_published(when)}


def page_body(html: str) -> str:
    """这一页的正文纯文本。提取不出来就抛,**绝不返回一段短的凑数**。

    `extract_or_reason` 给的原因是面向开发的(「命中版权区」「作者简介」),
    对着用户再说一遍没有意义 —— 他能做的只有「换个地址」或者「这篇就是读不
    到」。所以这里只把它当日志级的补充附在后面。
    """
    text, reason = readable.extract_or_reason(html)
    if not text:
        raise PageUnavailable('这一页没找到正文' + (f'（{reason}）' if reason else ''))
    return text[:MAX_CHARS]


def title_fallback(soup_title: str, body: str, host: str) -> str:
    """标题一个字都没取到时的回落:正文第一行 → 站点名。

    口径同 `fetching.parse_feed`(拿正文首行当标题,截 100 字),所以同一篇内容
    从订阅进来和从手动收录进来不会长出两种标题风格。
    """
    if soup_title:
        return soup_title
    first = next((ln.strip() for ln in (body or '').splitlines() if ln.strip()), '')
    return (first[:100] or host)[:500]


def plain_text(html_fragment: str) -> str:
    """给调用方留的口子:块级边界的口径只有 `fetching.plain` 这一份。"""
    return plain(html_fragment)
