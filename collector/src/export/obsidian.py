# -*- coding: utf-8 -*-
"""
把库里的公众号文章导出为 Obsidian Markdown。

HTML→Markdown 转换器和图片下载移植自 wechat-decrypt/gzh_saver.py（同一作者的
实时保存脚本），保持两边产出的 md 格式一致：
- 图片下载到当天共用的 assets/ 目录，文件名 = url md5[:12] + 扩展名
- frontmatter 字段顺序稳定，url 用于跨树去重
- 文件已存在且 frontmatter.url 相同 → 跳过（幂等）

与 gzh_saver 的差异：输入不是整页 HTML 而是库里存的 rich_media_content
内层 HTML（script/iframe 已剥、img src 已从 data-src 还原），所以不需要
js_content 定位和 og:meta 解析——元数据直接来自 articles 表。
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from html.parser import HTMLParser

import requests

# 单张图超时 / 大小上限，与 gzh_saver 一致
IMAGE_TIMEOUT = 15
MAX_IMAGE_SIZE = 20 * 1024 * 1024

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def canonicalize_url(url: str) -> str:
    """mp.weixin URL 归一化：只保留 __biz/mid/idx/sn/chksm，去 tracking 参数。"""
    try:
        pu = urllib.parse.urlparse(url)
    except ValueError:
        return url
    if "mp.weixin.qq.com" not in pu.netloc:
        return url
    params = urllib.parse.parse_qs(pu.query, keep_blank_values=False)
    keep = {k: v for k, v in params.items() if k in ("__biz", "mid", "idx", "sn", "chksm")}
    return urllib.parse.urlunparse(
        pu._replace(query=urllib.parse.urlencode(keep, doseq=True), fragment="")
    )


_FILENAME_BAD = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """任意字符串 → 跨平台安全文件名（不含扩展名）。"""
    if not name:
        return "untitled"
    cleaned = _FILENAME_BAD.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or "untitled"


def _resolve_proxy_url(src: str) -> str:
    """预览功能存的 HTML 里图片走本地代理 /api/wechat-image?url=<编码后原URL>,
    导出时解回原始远程 URL。"""
    if src.startswith("/api/wechat-image"):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(src).query)
        orig = (qs.get("url") or [""])[0]
        if orig.startswith("http"):
            return orig
    return src


class _ArticleParser(HTMLParser):
    """把绿泡泡 rich_media_content HTML 转成 Markdown（移植自 gzh_saver._ArticleParser）。

    - <section> 满天飞，语义上是块级容器，当 <p> 处理
    - <img data-src> 优先（库里 html 的 src 已还原，但保留兼容）
    - <blockquote> 嵌套行都加 "> " 前缀
    """

    HEADINGS = {'h1': '# ', 'h2': '## ', 'h3': '### ',
                'h4': '#### ', 'h5': '##### ', 'h6': '###### '}
    BLOCK_TAGS = {'p', 'section', 'div', 'article',
                  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                  'ul', 'ol', 'li', 'blockquote', 'pre', 'br',
                  'hr', 'table', 'tr', 'td', 'th'}
    INLINE_SKIP = {'script', 'style', 'noscript', 'iframe'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0
        self._in_pre = 0
        self._quote_depth = 0
        self._list_stack = []
        self._link_stack = []
        self._link_text_start = []
        self._pending_bold = 0
        self._pending_italic = 0
        self.images = []

    def _emit(self, s):
        if s:
            self._parts.append(s)

    def _newline(self, count=1):
        text = ''.join(self._parts).rstrip('\n')
        self._parts = [text]
        self._parts.append('\n' * count)

    def _quote_prefix(self):
        return '> ' * self._quote_depth if self._quote_depth else ''

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_depth:
            if tag in self.INLINE_SKIP:
                self._skip_depth += 1
            return
        if tag in self.INLINE_SKIP:
            self._skip_depth = 1
            return

        attrs_d = dict(attrs)

        if tag == 'br':
            self._emit('  \n' + self._quote_prefix())
            return
        if tag == 'hr':
            self._newline(2)
            self._emit('---')
            self._newline(2)
            return
        if tag == 'img':
            src = (attrs_d.get('data-src') or attrs_d.get('data-original')
                   or attrs_d.get('src') or '')
            src = _resolve_proxy_url(src)
            alt = attrs_d.get('alt') or ''
            if src:
                self.images.append(src)
                self._emit(f'![{alt}]({src})')
            return
        if tag in self.HEADINGS:
            self._newline(2)
            self._emit(self._quote_prefix() + self.HEADINGS[tag])
            return
        if tag == 'a':
            self._link_stack.append(attrs_d.get('href', ''))
            self._link_text_start.append(len(self._parts))
            return
        if tag in ('strong', 'b'):
            self._emit('**')
            self._pending_bold += 1
            return
        if tag in ('em', 'i'):
            self._emit('*')
            self._pending_italic += 1
            return
        if tag == 'blockquote':
            self._newline(2)
            self._quote_depth += 1
            self._emit(self._quote_prefix())
            return
        if tag == 'pre':
            self._newline(2)
            self._emit('```\n')
            self._in_pre += 1
            return
        if tag == 'code' and not self._in_pre:
            self._emit('`')
            return
        if tag in ('ul', 'ol'):
            self._newline(1)
            self._list_stack.append([tag, 0])
            return
        if tag == 'li':
            self._newline(1)
            indent = '  ' * max(len(self._list_stack) - 1, 0)
            if self._list_stack and self._list_stack[-1][0] == 'ol':
                self._list_stack[-1][1] += 1
                marker = f'{self._list_stack[-1][1]}. '
            else:
                marker = '- '
            self._emit(self._quote_prefix() + indent + marker)
            return
        if tag in self.BLOCK_TAGS:
            self._newline(2)
            self._emit(self._quote_prefix())
            return

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_depth:
            if tag in self.INLINE_SKIP:
                self._skip_depth -= 1
            return

        if tag == 'a' and self._link_stack:
            href = self._link_stack.pop()
            start = self._link_text_start.pop() if self._link_text_start else len(self._parts)
            text = ''.join(self._parts[start:]).strip()
            del self._parts[start:]
            if not text:
                text = href
            if href:
                self._emit(f'[{text}]({href})')
            else:
                self._emit(text)
            return
        if tag in ('strong', 'b') and self._pending_bold > 0:
            self._emit('**')
            self._pending_bold -= 1
            return
        if tag in ('em', 'i') and self._pending_italic > 0:
            self._emit('*')
            self._pending_italic -= 1
            return
        if tag == 'blockquote' and self._quote_depth > 0:
            self._quote_depth -= 1
            self._newline(2)
            return
        if tag == 'pre' and self._in_pre > 0:
            self._newline(1)
            self._emit('```')
            self._newline(2)
            self._in_pre -= 1
            return
        if tag == 'code' and not self._in_pre:
            self._emit('`')
            return
        if tag in ('ul', 'ol') and self._list_stack:
            self._list_stack.pop()
            self._newline(1)
            return
        if tag in self.HEADINGS or tag in self.BLOCK_TAGS:
            self._newline(2)
            return

    def handle_data(self, data):
        if self._skip_depth or not data:
            return
        if self._in_pre:
            self._emit(data)
            return
        if self._quote_depth and '\n' in data:
            data = data.replace('\n', '\n' + self._quote_prefix())
        self._emit(data)

    def get_markdown(self):
        text = ''.join(self._parts)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = '\n'.join(line.rstrip() for line in text.split('\n'))
        # 空 <li>/<blockquote> 会留下光杆列表符或引用符,整行删掉
        text = re.sub(r'^\s*(?:>\s*)*(?:[-*]|\d+\.)\s*$\n?', '', text, flags=re.M)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip() + '\n'


def html_to_markdown(html: str) -> tuple[str, list[str]]:
    """转换正文 HTML。返回 (markdown, 远程图片 URL 列表)。"""
    parser = _ArticleParser()
    parser.feed(html)
    parser.close()
    return parser.get_markdown(), parser.images


# ---- 图片下载 ----------------------------------------------------------------

def _guess_ext(content_type: str | None, url: str) -> str:
    ct = (content_type or "").lower()
    for key, ext in (("jpeg", ".jpg"), ("jpg", ".jpg"), ("png", ".png"),
                     ("gif", ".gif"), ("webp", ".webp"), ("svg", ".svg")):
        if key in ct:
            return ext
    fmt_m = re.search(r"wx_fmt=([a-z]+)", url, re.I)
    if fmt_m:
        f = fmt_m.group(1).lower()
        return ".jpg" if f == "jpeg" else f".{f}"
    return ".jpg"


def download_images(image_urls: list[str], assets_dir: str) -> dict[str, str]:
    """下载图片到 assets_dir。返回 {原URL: 文件名}；失败的不在 mapping 里。

    文件名 = md5(url)[:12] + 扩展名，同 URL 幂等复用，按天共用 assets/ 时
    多篇文章引用同一张图只存一份。
    """
    os.makedirs(assets_dir, exist_ok=True)
    mapping: dict[str, str] = {}
    seen: set[str] = set()
    ordered = [u for u in image_urls if u and not (u in seen or seen.add(u))]

    for url in ordered:
        try:
            ext = _guess_ext(None, url)
            h = hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
            fname = f"{h}{ext}"
            fpath = os.path.join(assets_dir, fname)
            if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
                mapping[url] = fname
                continue
            resp = requests.get(
                url,
                headers={"User-Agent": _UA, "Accept": "image/*,*/*;q=0.8"},
                timeout=IMAGE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.content
            if len(data) > MAX_IMAGE_SIZE:
                print(f"  [export] skip oversize image: {url[:80]} ({len(data)} B)", flush=True)
                continue
            real_ext = _guess_ext(resp.headers.get("Content-Type"), url)
            if real_ext != ext:
                fname = f"{h}{real_ext}"
                fpath = os.path.join(assets_dir, fname)
            with open(fpath, "wb") as f:
                f.write(data)
            mapping[url] = fname
        except Exception as e:
            print(f"  [export] image fail: {url[:80]} → {e}", flush=True)
    return mapping


def replace_images_in_markdown(md: str, url_to_local: dict[str, str], assets_rel_dir: str) -> str:
    """把 md 里成功下载的远程图片 URL 换成 assets 相对路径,失败的保留原 URL。"""
    def sub(m):
        alt, url = m.group(1), m.group(2)
        local = url_to_local.get(url)
        return f"![{alt}]({assets_rel_dir}/{local})" if local else m.group(0)
    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", sub, md)


# ---- Frontmatter / 写文件 ------------------------------------------------------

def _yaml_escape(v) -> str:
    if v is None:
        return '""'
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", " ").replace("\r", " ")
    return f'"{s}"'


def build_frontmatter(scalars: list[tuple[str, str]], tags: list[str]) -> str:
    """生成 YAML frontmatter。scalars 顺序即输出顺序,tags 输出为 YAML 列表。"""
    lines = ["---"]
    for k, v in scalars:
        lines.append(f"{k}: {_yaml_escape(v)}")
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {t}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def read_frontmatter_url(md_path: str) -> str:
    """读已存在 .md 的 frontmatter url 字段,找不到返回 ''。"""
    try:
        with open(md_path, encoding="utf-8") as f:
            head = f.read(2048)
    except OSError:
        return ""
    m = re.search(r'^url:\s*"([^"]+)"', head, re.M)
    return m.group(1) if m else ""


def unique_md_path(day_dir: str, base_name: str, canonical_url: str) -> tuple[str, bool]:
    """挑一个不冲突的 md 路径。返回 (path, skip)——skip=True 表示同 URL 已存在。"""
    path = os.path.join(day_dir, f"{base_name}.md")
    if not os.path.exists(path):
        return path, False
    if read_frontmatter_url(path) == canonical_url:
        return path, True
    for i in range(1, 20):
        p = os.path.join(day_dir, f"{base_name}-{i}.md")
        if not os.path.exists(p):
            return p, False
        if read_frontmatter_url(p) == canonical_url:
            return p, True
    return path, False


def account_tag_slug(name: str) -> str:
    """公众号名 → tag 片段:小写、空白转连字符,中文原样保留。"""
    slug = re.sub(r"\s+", "-", name.strip().lower())
    # obsidian tag 里不允许的字符去掉
    return re.sub(r"[#,\[\]{}|^]", "", slug) or "unknown"
