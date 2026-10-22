# -*- coding: utf-8 -*-
"""列表页用例共用的样本、构造函数和小工具。

两份真实样本都在这里:判据得同时接住"日期嵌在链接里"(旧)和"日期跟链接是
兄弟"(改版后)两种形态,所以两边的用例共用同一组构造函数 —— 口径只有一份。
"""

from __future__ import annotations

import time
from pathlib import Path

from kb.feeds.fetching import Response

SAMPLE = (Path(__file__).parent / 'page_list_sample.html').read_bytes()
# 改版之后的同一张页面(2026-10-21 抓的)。**两份都留着**:旧版是「日期嵌在
# 链接里」,新版是「日期和链接是兄弟、同一篇文章被链四次」—— 判据得同时接住
# 两种,只留新的那份就没人钉住旧形态还走得通(见 GroupTests 两条真实页面用例)。
ROWS = (Path(__file__).parent / 'page_list_rows_sample.html').read_bytes()
BASE = 'https://www.anthropic.com/news'
ARTICLE = ('<html><body><nav><a href="/">Home</a><a href="/news">News</a></nav>'
           '<article><h1>Introducing the Anthropic Cyber Mission</h1>'
           '<p>We are launching a program to put frontier models in the hands of '
           'cyber defenders, because defense is where the asymmetry can be flipped. '
           'The gap between attacker and defender tooling has widened for a decade.</p>'
           '<p>The first cohort covers critical infrastructure operators and the '
           'open-source maintainers who carry a disproportionate share of the load. '
           'Each participant gets model access plus a dedicated review channel.</p>'
           '</article><footer>© 2026 Anthropic</footer></body></html>')


def resp(body: str | bytes, url: str = BASE, status: int = 200) -> Response:
    raw = body.encode() if isinstance(body, str) else body
    return Response(status, url, raw, {'content-type': 'text/html; charset=utf-8'})


def listing(rows, *, noise: int = 20, head: str = '') -> bytes:
    """一张最小列表页。

    `noise` 条**不带日期**的页脚链接刻意排在前面、而且数量远多于正文列表 ——
    这正是 Anthropic 那一页的真实形态(90 条页脚菜单 vs 10 条新闻),用来钉住
    判据是"带日期",不是"哪一组链接最多"。
    """
    menu = ''.join(f'<li><a href="/p/{i}">Pricing {i}</a></li>' for i in range(noise))
    items = ''.join(
        f'<li><a href="{href}"><div><time{datetime_attr}>{shown}</time>'
        f'<span>{subject}</span></div><span>{title}</span></a></li>'
        for href, shown, subject, title, datetime_attr in rows)
    return (f'<html><body>{head}<footer><ul>{menu}</ul></footer>'
            f'<main><ul>{items}</ul></main></body></html>').encode()


def row(href, shown, title, *, subject='Announcements', attr=''):
    return (href, shown, subject, title, f' datetime="{attr}"' if attr else '')


def today(offset: int = 0) -> str:
    """样本日期跟着今天走:写死的日期过几天就被 history_floor 挡住,而红的会是
    一堆跟它毫无关系的用例(口径同 `test_feed_sources` 的 `_FRESH`)。"""
    return time.strftime('%Y-%m-%d', time.localtime(time.time() - offset * 86400))


def rows_listing(rows, *, badge_first: bool = False) -> bytes:
    """改版后那种行:**标题链接、分类角标、日期三者是平级的兄弟**。

    形态照抄 Anthropic 现在的 `<li data-listing-row>`:同一篇文章在一行里被
    链两次(标题一次、摘要一次),分类角标自己一个 `<p>`,日期在第四个兄弟
    节点上。于是"只属于标题链接的最后一个祖先"读不到日期,而角标吸上来的
    容器跟标题链接是同一个 —— 两道判定各管一头。
    """
    out = []
    for href, shown, subject, title, _ in rows:
        badge = f'<p><a href="/news/{subject.lower()}">{subject}</a></p>'
        body = (f'<div><a href="{href}"><span><h3>{title}</h3></span></a></div>'
                f'<div><a href="{href}"><span><p>A blurb for {title}.</p></span></a></div>')
        parts = (badge + body) if badge_first else (body + badge)
        out.append(f'<li><div>{parts}<div><time>{shown}</time></div></div></li>')
    return (f'<html><body><main><ul>{"".join(out)}</ul></main></body></html>').encode()


def card_listing(rows, *, header_first: bool = True) -> bytes:
    """featured 卡片那种形态:角标**自己那一小块里就带着日期**。

    `<header>` 里是「分类链接 + 日期」,文章链接是 `<header>` 的兄弟。角标
    因此够不着"同一个容器"那一道 —— 靠的是"容器互相嵌套时外层才是记录"。
    """
    out = []
    for href, shown, subject, title, _ in rows:
        head = (f'<header><p><a href="/news/{subject.lower()}">{subject}</a></p>'
                f'<time>{shown}</time></header>')
        link = f'<a href="{href}"><h2>{title}</h2></a>'
        out.append(f'<article>{head + link if header_first else link + head}</article>')
    return (f'<html><body><main>{"".join(out)}</main></body></html>').encode()
