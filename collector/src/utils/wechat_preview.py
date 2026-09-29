# -*- coding: utf-8 -*-
"""
微信公众号文章预览（对齐 wechat-article-exporter）：

- 服务端拉取原文 HTML（带 id_info Cookie）
- 提取 #js_article，清理广告/脚本，图片走同源代理
- 前端用 iframe srcdoc 展示，**不**直接嵌 mp.weixin.qq.com（会被 X-Frame-Options 拒绝）
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from lxml import etree

from src.utils.data_manager import data_manager, headers as default_headers
from src.utils.helpers import message_unavailable
from src.utils.wechat_body import is_allowed_wechat_image_url, prepare_html_for_preview

_MP_ORIGIN = "https://mp.weixin.qq.com/"
_PREVIEW_FIRST_SCREEN_IMG_COUNT = 6


def _preview_img_aspect_ratio(img: etree._Element) -> str | None:
    """从微信 img 的 data-w / data-ratio 或 style 推断宽高比。"""
    ratio_s = (img.get("data-ratio") or "").strip()
    w_s = (img.get("data-w") or img.get("width") or "").strip()
    h_s = (img.get("data-height") or img.get("height") or "").strip()
    try:
        if w_s and h_s:
            wi, hi = int(float(w_s)), int(float(h_s))
            if wi > 0 and hi > 0:
                return f"{wi}/{hi}"
        if w_s and ratio_s:
            wi = int(float(w_s))
            r = float(ratio_s)
            hi = max(1, round(wi * r))
            if wi > 0 and hi > 0:
                return f"{wi}/{hi}"
    except (ValueError, TypeError):
        pass
    style = img.get("style") or ""
    wm = re.search(r"width:\s*(\d+(?:\.\d+)?)px", style)
    hm = re.search(r"height:\s*(\d+(?:\.\d+)?)px", style)
    if wm and hm:
        try:
            wi, hi = int(float(wm.group(1))), int(float(hm.group(1)))
            if wi > 0 and hi > 0:
                return f"{wi}/{hi}"
        except ValueError:
            pass
    return None


def _configure_preview_image(img: etree._Element, idx: int) -> None:
    src = (img.get("data-src") or img.get("src") or "").strip()
    if src:
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = urljoin(_MP_ORIGIN, src)
        from urllib.parse import quote

        if is_allowed_wechat_image_url(src):
            img.set("src", f"/api/wechat-image?url={quote(src, safe='')}")
        else:
            img.set("src", src)

    ar = _preview_img_aspect_ratio(img)
    styles: list[str] = []
    existing_style = (img.get("style") or "").strip()
    if existing_style:
        styles.append(existing_style)
    if ar:
        styles.append(f"aspect-ratio:{ar}")
    styles.extend(
        [
            "width:100%",
            "max-width:100%",
            "height:auto",
            "background:#e8e4de",
            "object-fit:contain",
        ]
    )
    img.set("style", ";".join(styles))

    classes = ["wx-preview-img"]
    prev_cls = (img.get("class") or "").strip()
    if prev_cls:
        classes.insert(0, prev_cls)
    img.set("class", " ".join(classes))

    if idx < _PREVIEW_FIRST_SCREEN_IMG_COUNT:
        img.set("loading", "eager")
        if idx < 2:
            img.set("fetchpriority", "high")
    else:
        img.set("loading", "lazy")
    img.set("decoding", "async")

    for attr in list(img.attrib):
        if attr.startswith("data-"):
            del img.attrib[attr]


def _resolve_preview_link_href(href: str, base_url: str = _MP_ORIGIN) -> str | None:
    """解析为可在浏览器新标签打开的绝对 URL；页内 # 锚点返回 None。"""
    href = (href or "").strip()
    if not href:
        return None
    lower = href.lower()
    if lower.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return None
    if href.startswith("#"):
        return None
    if href.startswith("//"):
        return "https:" + href
    if lower.startswith("http://") or lower.startswith("https://"):
        return href
    return urljoin(base_url, href)


# 标记预览 HTML 已处理外链（用于缓存失效）
_PREVIEW_LINK_MARKER = "preview-link-v1"
_PREVIEW_IMG_LIGHTBOX_MARKER = "preview-img-lightbox-v3"


def _configure_preview_links(root: etree._Element, base_url: str = _MP_ORIGIN) -> None:
    """正文链接一律新窗口打开，避免在预览 iframe 内跳转失败。"""
    for anchor in root.xpath(".//a[@href]"):
        raw = (anchor.get("href") or "").strip()
        resolved = _resolve_preview_link_href(raw, base_url)
        if not resolved:
            continue
        anchor.set("href", resolved)
        anchor.set("target", "_blank")
        anchor.set("rel", "noopener noreferrer")


def _configure_preview_links_html(html: str, base_url: str = _MP_ORIGIN) -> str:
    if not html or not html.strip():
        return html
    wrapper = etree.HTML(
        f'<div id="wx-links-root">{html}</div>',
        parser=etree.HTMLParser(encoding="utf-8"),
    )
    if wrapper is None:
        return html
    nodes = wrapper.xpath('//div[@id="wx-links-root"]')
    if not nodes:
        return html
    node = nodes[0]
    _configure_preview_links(node, base_url)
    return "".join(
        etree.tostring(c, encoding="unicode", method="html") for c in node
    )


# CSS / JS 常量搬到了 wechat_preview_css.py(本文件曾 886 行,超 700 行上限)。
# 这里再导出一遍:别处是按 `from src.utils.wechat_preview import _PREVIEW_AI_CSS`
# 这样引的,搬走之后不接回来它们就断了。
from src.utils.wechat_preview_css import (  # noqa: E402  (常量,位置跟原来一致)
    _ARTICLE_HEADER_CSS,
    _ARTICLE_TABLE_CSS,
    _CODE_SNIPPET_CSS,
    _PREVIEW_AI_CSS,
    _PREVIEW_IMG_CSS,
    _PREVIEW_IMG_LIGHTBOX_JS,
    _PREVIEW_SURFACE_CSS,
)
_REMOVE_IDS = (
    "js_top_ad_area",
    "js_tags_preview_toast",
    "content_bottom_area",
    "js_pc_qr_code",
    "wx_stream_article_slide_tip",
    "js_row_immersive_stream_wrap",
)


def _request_headers() -> dict[str, str]:
    h = dict(default_headers)
    try:
        cookie = (data_manager.id_info or {}).get("cookie", "")
        if cookie:
            h["Cookie"] = cookie
    except Exception:
        pass
    h["Referer"] = _MP_ORIGIN
    return h


def fetch_article_page_html(url: str, num: int = 0) -> str | None:
    """拉取公众号文章页完整 HTML。"""
    try:
        response = requests.get(url, headers=_request_headers(), timeout=25)
        response.raise_for_status()
        html = response.text
    except Exception:
        if num >= 3:
            return None
        return fetch_article_page_html(url, num=num + 1)

    if message_unavailable(response=html):
        return None

    tree = etree.HTML(html, parser=etree.HTMLParser(encoding="utf-8"))
    if tree is None:
        if num >= 3:
            return None
        return fetch_article_page_html(url, num=num + 1)

    if not tree.xpath('//*[@id="js_article"]') and not tree.xpath(
        '//*[contains(@class, "rich_media_content")]'
    ):
        from src.utils.wechat_picture import is_picture_message_html

        if is_picture_message_html(html):
            return html
        data_url = tree.xpath('//div[@class="original_panel_tool"]/span/@data-url')
        if data_url:
            return fetch_article_page_html(data_url[0], num=num + 1)
        if num >= 3:
            return None
        return fetch_article_page_html(url, num=num + 1)

    return html


def _remove_by_id(root: etree._Element, element_id: str) -> None:
    for el in root.xpath(f'.//*[@id="{element_id}"]'):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _format_wechat_pub_time_unix(ts: int) -> str:
    dt = datetime.fromtimestamp(ts)
    return f"{dt.year}年{dt.month:02d}月{dt.day:02d}日 {dt.hour:02d}:{dt.minute:02d}"


def _format_wechat_pub_time_str(create_time: str) -> str | None:
    create_time = (create_time or "").strip()
    if not create_time:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(create_time, fmt)
            return _format_wechat_pub_time_unix(int(dt.timestamp()))
        except ValueError:
            continue
    return None


def _extract_page_meta(raw_html: str) -> dict[str, str]:
    """从原文页脚本提取发布时间、IP 属地（原由微信 JS 写入 DOM）。"""
    meta: dict[str, str] = {}

    ts_match = re.search(r"var\s+oriCreateTime\s*=\s*['\"](\d+)['\"]", raw_html)
    if not ts_match:
        ts_match = re.search(r"var\s+ct\s*=\s*['\"](\d+)['\"]", raw_html)
    if ts_match:
        try:
            meta["pub_time_display"] = _format_wechat_pub_time_unix(int(ts_match.group(1)))
        except (ValueError, OSError, OverflowError):
            pass
    else:
        decoded = re.search(r"create_time:\s*JsDecode\('([^']+)'\)", raw_html)
        if decoded:
            display = _format_wechat_pub_time_str(decoded.group(1).replace("/", "-"))
            if display:
                meta["pub_time_display"] = display

    ip_match = re.search(r"window\.ip_wording\s*=\s*\{(?P<body>[^}]+)\}", raw_html, re.S)
    if ip_match:
        body = ip_match.group("body")
        country_m = re.search(r"countryId:\s*['\"]?(\d+)['\"]?", body)
        province_m = re.search(r"provinceName:\s*['\"]([^'\"]*)['\"]", body)
        country_name_m = re.search(r"countryName:\s*['\"]([^'\"]*)['\"]", body)
        country_id = country_m.group(1) if country_m else ""
        province = (province_m.group(1) if province_m else "").strip()
        country_name = (country_name_m.group(1) if country_name_m else "").strip()
        if country_id == "156" and province:
            meta["ip_wording"] = province
        elif country_id and country_name:
            meta["ip_wording"] = country_name

    for pattern in (
        r'<meta\s+[^>]*property=["\']og:url["\'][^>]*content=["\']([^"\']+)',
        r'<link\s+[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)',
    ):
        m = re.search(pattern, raw_html, re.I)
        if m:
            meta["canonical_url"] = m.group(1).strip()
            break

    return meta


def _hydrate_article_meta(
    article: etree._Element,
    meta: dict[str, str],
    *,
    fallback_pub_time: str = "",
    fallback_pub_unix: int | None = None,
) -> None:
    """把发布时间、属地写入 meta 区域（替代微信页内脚本）。"""
    pub = meta.get("pub_time_display")
    if not pub and fallback_pub_unix:
        try:
            pub = _format_wechat_pub_time_unix(int(fallback_pub_unix))
        except (ValueError, OSError, OverflowError):
            pub = None
    if not pub and fallback_pub_time:
        pub = _format_wechat_pub_time_str(fallback_pub_time)
    if pub:
        for el in article.xpath('.//*[@id="publish_time"]'):
            el.text = pub

    ip = meta.get("ip_wording", "").strip()
    if ip:
        for el in article.xpath('.//*[@id="js_ip_wording"]'):
            el.text = ip
        for el in article.xpath('.//*[@id="js_ip_wording_wrp"]'):
            style = (el.get("style") or "").strip()
            if "display" not in style.lower():
                el.set("style", "display: inline-block;")
            elif "none" in style.lower():
                el.set("style", "display: inline-block;")

def _enhance_article_tables(article: etree._Element) -> None:
    """为正文表格加上滚动容器与 class，避免窄屏挤版。"""
    for table in article.xpath(".//table"):
        parent = table.getparent()
        if parent is not None and parent.tag == "div":
            pcls = parent.get("class") or ""
            if "wx_preview_table_wrap" in pcls:
                continue
        wrap = etree.Element("div")
        wrap.set("class", "wx_preview_table_wrap")
        if parent is None:
            continue
        idx = parent.index(table)
        parent.insert(idx, wrap)
        wrap.append(table)
        tcls = (table.get("class") or "").strip()
        if "wx_preview_table" not in tcls.split():
            table.set("class", f"{tcls} wx_preview_table".strip())


def _escape_html_text(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_ai_insights_html(summary: str | None, tags: list[str] | None) -> str:
    summary_text = (summary or "").strip()
    tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()][:8]
    if not summary_text and not tag_list:
        return ""
    parts = ['<div class="wx_preview_ai">', '<div class="wx_preview_ai_head">']
    parts.append('<span class="wx_preview_ai_badge">AI 阅读助手</span></div>')
    if summary_text:
        parts.append(
            f'<p class="wx_preview_ai_summary">{_escape_html_text(summary_text)}</p>'
        )
    if tag_list:
        parts.append('<div class="wx_preview_ai_tags">')
        for i, tag in enumerate(tag_list):
            parts.append(
                f'<span class="wx_preview_ai_tag wx_preview_ai_tag--{i % 4}">'
                f"{_escape_html_text(tag)}</span>"
            )
        parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


def _inject_ai_insights(
    article: etree._Element,
    summary: str | None,
    tags: list[str] | None,
) -> None:
    """在标题/公众号 meta 下方插入 AI 摘要与标签。"""
    summary_text = (summary or "").strip()
    tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()][:8]
    if not summary_text and not tag_list:
        return

    anchor = article.xpath('.//*[@id="meta_content"]')
    insert_after = anchor[0] if anchor else None
    if insert_after is None:
        titles = article.xpath('.//*[@id="activity-name"]')
        insert_after = titles[0] if titles else None
    if insert_after is None:
        return
    parent = insert_after.getparent()
    if parent is None:
        return

    panel = etree.Element("div")
    panel.set("class", "wx_preview_ai")

    head = etree.SubElement(panel, "div")
    head.set("class", "wx_preview_ai_head")
    badge = etree.SubElement(head, "span")
    badge.set("class", "wx_preview_ai_badge")
    badge.text = "AI 阅读助手"

    if summary_text:
        para = etree.SubElement(panel, "p")
        para.set("class", "wx_preview_ai_summary")
        para.text = summary_text

    if tag_list:
        tags_wrap = etree.SubElement(panel, "div")
        tags_wrap.set("class", "wx_preview_ai_tags")
        for i, tag in enumerate(tag_list):
            chip = etree.SubElement(tags_wrap, "span")
            chip.set("class", f"wx_preview_ai_tag wx_preview_ai_tag--{i % 4}")
            chip.text = tag

    parent.insert(parent.index(insert_after) + 1, panel)


def _preview_body_attrs(tree: etree._Element) -> str:
    """预览面板为白底，强制浅色主题，避免代码块被微信暗色规则染成白字。"""
    theme = "light"
    for xpath in ("//html/@data-weui-theme", "//body/@data-weui-theme"):
        vals = tree.xpath(xpath)
        if vals and str(vals[0]).strip():
            theme = str(vals[0]).strip()
            break
    # 抽屉预览固定浅色，避免 body:not([data-weui-theme=light]) 下代码块不可读
    if theme != "dark":
        theme = "light"
    return f'data-weui-theme="{theme}"'


def _collect_styles(tree: etree._Element) -> str:
    chunks: list[str] = []
    for href in tree.xpath("//link[@rel='stylesheet']/@href"):
        if not href:
            continue
        full = urljoin(_MP_ORIGIN, href)
        chunks.append(f'<link rel="stylesheet" href="{full}">')
    for style in tree.xpath("//style"):
        text = etree.tostring(style, encoding="unicode", method="html")
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def build_preview_document(
    raw_html: str,
    title: str = "",
    *,
    ai_summary: str | None = None,
    ai_tags: list[str] | None = None,
    fallback_pub_time: str = "",
    fallback_pub_unix: int | None = None,
    fallback_digest: str = "",
    item_show_type: int = 0,
    account_name: str = "",
) -> str:
    """
    从微信原文页构建可 srcdoc 嵌入的完整 HTML（参考 exporter normalizeHtml）。
    """
    from src.utils.wechat_picture import (
        build_picture_preview_document,
        is_picture_message_html,
    )

    if is_picture_message_html(raw_html):
        picture_html = build_picture_preview_document(
            raw_html,
            title=title,
            ai_summary=ai_summary,
            ai_tags=ai_tags,
            fallback_pub_time=fallback_pub_time,
            fallback_pub_unix=fallback_pub_unix,
            digest=fallback_digest,
            account_name=account_name,
        )
        if picture_html.strip():
            return picture_html

    tree = etree.HTML(raw_html, parser=etree.HTMLParser(encoding="utf-8"))
    if tree is None:
        return ""

    page_meta = _extract_page_meta(raw_html)

    article_nodes = tree.xpath('//*[@id="js_article"]')
    if article_nodes:
        article = article_nodes[0]
    else:
        rich = tree.xpath('//*[contains(@class, "rich_media_content")]')
        if not rich:
            return ""
        inner = prepare_html_for_preview(
            "".join(
                etree.tostring(c, encoding="unicode", method="html") for c in rich[0]
            )
        )
        inner = _configure_preview_links_html(
            inner, page_meta.get("canonical_url") or _MP_ORIGIN
        )
        return _wrap_preview_fragment(
            inner, title, "", ai_summary=ai_summary, ai_tags=ai_tags
        )

    for js_content in article.xpath('.//*[@id="js_content"]'):
        if "style" in js_content.attrib:
            del js_content.attrib["style"]

    for eid in _REMOVE_IDS:
        _remove_by_id(article, eid)

    _hydrate_article_meta(
        article,
        page_meta,
        fallback_pub_time=fallback_pub_time,
        fallback_pub_unix=fallback_pub_unix,
    )
    _inject_ai_insights(article, ai_summary, ai_tags)
    _enhance_article_tables(article)

    for script in article.xpath(".//script"):
        parent = script.getparent()
        if parent is not None:
            parent.remove(script)

    for bad in article.xpath(".//iframe"):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)

    for idx, img in enumerate(article.xpath(".//img")):
        _configure_preview_image(img, idx)

    page_url = page_meta.get("canonical_url") or _MP_ORIGIN
    _configure_preview_links(article, page_url)

    body_cls_list = tree.xpath("//body/@class")
    body_cls = body_cls_list[0] if body_cls_list else ""
    body_attrs = _preview_body_attrs(tree)

    page_content = etree.tostring(article, encoding="unicode", method="html")
    bottom_nodes = tree.xpath('//*[@id="js_article_bottom_bar"]')
    bottom_html = ""
    if bottom_nodes:
        bottom_html = etree.tostring(bottom_nodes[0], encoding="unicode", method="html")

    ext_styles = _collect_styles(tree)
    safe_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") or "文章预览"
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <!-- {_PREVIEW_LINK_MARKER} -->
  <!-- {_PREVIEW_IMG_LIGHTBOX_MARKER} -->
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=0,viewport-fit=cover">
  <meta name="referrer" content="no-referrer">
  <meta name="color-scheme" content="light">
  <title>{safe_title}</title>
  {ext_styles}
  <style>
    #js_article_bottom_bar, .__page_content__ {{ max-width: 667px; margin: 0 auto; }}
    img {{ max-width: 100% !important; height: auto !important; }}
    {_PREVIEW_SURFACE_CSS}
    {_PREVIEW_IMG_CSS}
    {_PREVIEW_AI_CSS}
    {_CODE_SNIPPET_CSS}
    {_ARTICLE_HEADER_CSS}
    {_ARTICLE_TABLE_CSS}
  </style>
</head>
<body class="{body_cls}" {body_attrs}>
{page_content}
{bottom_html}
<script>{_PREVIEW_IMG_LIGHTBOX_JS}</script>
</body>
</html>"""


def _wrap_preview_fragment(
    inner: str,
    title: str,
    body_cls: str,
    *,
    ai_summary: str | None = None,
    ai_tags: list[str] | None = None,
) -> str:
    safe_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") or "文章预览"
    )
    ai_html = _build_ai_insights_html(ai_summary, ai_tags)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <!-- {_PREVIEW_LINK_MARKER} -->
  <!-- {_PREVIEW_IMG_LIGHTBOX_MARKER} -->
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <meta name="color-scheme" content="light">
  <title>{safe_title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; }}
    img {{ max-width: 100% !important; height: auto !important; }}
    section {{ margin: 1em 0; }}
    #js_article {{ max-width: 667px; margin: 0 auto; padding: 18px 16px 24px; background: #fdfbf7; border-radius: 12px; box-shadow: 0 2px 14px rgba(26,18,46,.07); }}
    {_PREVIEW_SURFACE_CSS}
    {_PREVIEW_AI_CSS}
    {_CODE_SNIPPET_CSS}
    {_ARTICLE_HEADER_CSS}
    {_ARTICLE_TABLE_CSS}
  </style>
</head>
<body class="{body_cls}" data-weui-theme="light">
<div id="js_article"><div id="js_content">{ai_html}{inner}</div></div>
<script>{_PREVIEW_IMG_LIGHTBOX_JS}</script>
</body>
</html>"""


def is_full_preview_document(html: str | None) -> bool:
    if not html or not html.strip():
        return False
    from src.utils.wechat_picture import is_picture_preview_document

    if is_picture_preview_document(html):
        return True
    s = html.lstrip().lower()
    if not (s.startswith("<!doctype") or s.startswith("<html")):
        return False
    # 旧缓存缺少主题/头部样式时，代码块与 meta 行显示异常
    has_theme = 'data-weui-theme="light"' in html or "data-weui-theme='light'" in html
    has_header = "preview-header-v1" in html
    has_table = "preview-table-v1" in html
    has_surface = "preview-surface-v3" in html
    has_ai = "preview-ai-v1" in html
    has_img = "preview-img-v2" in html
    has_img_lightbox = _PREVIEW_IMG_LIGHTBOX_MARKER in html
    has_link = "preview-link-v1" in html
    return (
        has_theme
        and has_header
        and has_table
        and has_surface
        and has_ai
        and has_img
        and has_img_lightbox
        and has_link
    )
