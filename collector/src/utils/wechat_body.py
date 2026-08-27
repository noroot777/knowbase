# -*- coding: utf-8 -*-
"""微信公众号正文：抓取 rich_media_content 的纯文本与 HTML。"""

from __future__ import annotations

import re
from typing import Any
from html import unescape as html_unescape
from urllib.parse import quote, urlparse

import requests
from lxml import etree

from src.utils.data_manager import headers
from src.utils.helpers import message_is_delete

_RICH_MEDIA_XPATHS = (
    '//div[@class="rich_media_content js_underline_content\n                       autoTypeSetting24psection\n            "]',
    '//div[@class="rich_media_content js_underline_content\n                       defaultNoSetting\n            "]',
    '//div[contains(@class, "rich_media_content")]',
)

_ALLOWED_IMAGE_HOSTS = frozenset(
    {
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
    }
)


def _find_rich_media_div(tree: etree._Element) -> list:
    for xp in _RICH_MEDIA_XPATHS:
        div = tree.xpath(xp)
        if div:
            return div
    return []


def _fetch_page(url: str) -> tuple[str, etree._Element | None]:
    from src.utils.wechat_preview import _request_headers

    response = requests.get(url, headers=_request_headers(), timeout=20).text
    tree = etree.HTML(response, parser=etree.HTMLParser(encoding="utf-8"))
    if tree is None:
        return response, None
    div = _find_rich_media_div(tree)
    if div:
        return response, div[0]
    data_url = tree.xpath('//div[@class="original_panel_tool"]/span/@data-url')
    if data_url:
        response = requests.get(data_url[0], headers=_request_headers(), timeout=20).text
        tree = etree.HTML(response, parser=etree.HTMLParser(encoding="utf-8"))
        if tree is not None:
            div = _find_rich_media_div(tree)
            if div:
                return response, div[0]
    return response, None


def _div_to_text_list(div: etree._Element) -> list[str]:
    s_p = [p for p in div.iter() if p.tag in ("section", "p")]
    text_list: list[str] = []
    tag: list[Any] = []
    filter_char = ["\xa0", "\u200d", "&nbsp;", "■", " "]
    pattern = "|".join(filter_char)
    for s in s_p:
        text = "".join(
            re.sub(pattern, "", i) for i in s.xpath(".//text()") if i != "\u200d"
        )
        if not text:
            continue
        if text_list and text in text_list[-1]:
            parent_tag = []
            tmp = s
            while tmp.tag != "div":
                tmp = tmp.getparent()
                parent_tag.append(tmp)
            if tag[-1] in parent_tag:
                del text_list[-1]
        tag.append(s)
        text_list.append(text)
    return text_list


def _div_to_html(div: etree._Element) -> str:
    parts = []
    for child in div:
        parts.append(etree.tostring(child, encoding="unicode", method="html"))
    html = "".join(parts)
    # 去掉正文里的 script / iframe
    wrapper = etree.HTML(
        f'<div id="wx-root">{html}</div>',
        parser=etree.HTMLParser(encoding="utf-8"),
    )
    if wrapper is None:
        return html
    root = wrapper.xpath('//div[@id="wx-root"]')
    if not root:
        return html
    node = root[0]
    for bad in node.xpath(".//script | .//iframe"):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)
    for img in node.xpath(".//img"):
        src = (img.get("data-src") or img.get("src") or "").strip()
        if src:
            img.set("src", src)
        for attr in ("data-src", "data-s", "data-type"):
            if attr in img.attrib:
                del img.attrib[attr]
        img.set("loading", "lazy")
        img.set("referrerpolicy", "no-referrer")
    return "".join(
        etree.tostring(c, encoding="unicode", method="html") for c in node
    )


# 正文页里公众号当前昵称的位置。按可靠度排序:
# - `var nickname = htmlDecode("…")` 是页面自己给本号用的,三篇实测页都有;
# - `id="js_name"` 是页头那个署名链接,同样只指本号;
# - 不要用裸的 `nickname: '…'` ——页里还塞着推荐位/关联号的昵称,实测
#   GitHub猿 那篇的第一个 `nickname:` 是"编程技术圈",抓到的是别人家的号。
_NICKNAME_PATTERNS = (
    r'var\s+nickname\s*=\s*htmlDecode\(\s*(["\'])(?P<v>.*?)\1\s*\)',
    r'var\s+nickname\s*=\s*(["\'])(?P<v>.*?)\1',
    r'id=["\']js_name["\'][^>]*>\s*(?P<v>[^<]{1,64}?)\s*<',
)


def extract_account_nickname(raw_html: str) -> str:
    """从正文页里读出这篇文章所属公众号的当前昵称。读不到返回空串。"""
    if not raw_html:
        return ""
    for pattern in _NICKNAME_PATTERNS:
        m = re.search(pattern, raw_html, re.S)
        if m:
            value = html_unescape(m.group("v")).strip()
            if value:
                return value
    return ""


def fetch_article_body_full(url: str, num: int = 0) -> tuple[Any, str | None, str]:
    """一次请求解析纯文本、HTML 和公众号当前昵称。

    返回 (text, html, nickname):
    - text: list[str] 或 '已删除' / '请求错误'
    - html: 正文 HTML；失败时为 None
    - nickname: 公众号当前昵称；读不到为空串

    昵称是顺手捡的:抓正文本来就要把整页拉下来,而公众号改名后只有这一页
    带着新名字(列表接口 getmsg 的返回里没有,wxdown 截的凭证里 name 恒为 null)。
    """
    try:
        response, div = _fetch_page(url)
    except Exception:
        if num >= 3:
            return "请求错误", None, ""
        return fetch_article_body_full(url, num=num + 1)

    nickname = extract_account_nickname(response)
    if div is None:
        if message_is_delete(response=response):
            return "已删除", None, nickname
        if num >= 3:
            return "请求错误", None, nickname
        return fetch_article_body_full(url, num=num + 1)

    return _div_to_text_list(div), _div_to_html(div), nickname


def fetch_article_body(url: str, num: int = 0) -> tuple[Any, str | None]:
    """
    一次请求同时解析纯文本与 HTML。

    返回 (text, html):
    - text: list[str] 或 '已删除' / '请求错误'
    - html: 正文 HTML；失败时为 None
    """
    text, body_html, _nickname = fetch_article_body_full(url, num=num)
    return text, body_html


def url2html(url: str, num: int = 0) -> str | None:
    """仅抓取 HTML；错误时返回 None。"""
    text, html = fetch_article_body(url, num=num)
    if isinstance(text, str) and text in ("已删除", "请求错误"):
        return None
    return html


def prepare_html_for_preview(html: str) -> str:
    """展示用：图片走同源代理，避免防盗链。"""
    if not html or not html.strip():
        return ""

    wrapper = etree.HTML(
        f'<div id="wx-preview">{html}</div>',
        parser=etree.HTMLParser(encoding="utf-8"),
    )
    if wrapper is None:
        return html
    root = wrapper.xpath('//div[@id="wx-preview"]')
    if not root:
        return html
    node = root[0]

    for img in node.xpath(".//img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if not src.startswith("http"):
            continue
        host = urlparse(src).hostname or ""
        if host in _ALLOWED_IMAGE_HOSTS:
            img.set("src", f"/api/wechat-image?url={quote(src, safe='')}")
        else:
            img.set("src", src)
        for attr in list(img.attrib):
            if attr.startswith("data-"):
                del img.attrib[attr]

    return "".join(
        etree.tostring(c, encoding="unicode", method="html") for c in node
    )


def is_allowed_wechat_image_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return host in _ALLOWED_IMAGE_HOSTS
