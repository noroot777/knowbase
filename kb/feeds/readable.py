# -*- coding: utf-8 -*-
"""网页正文提取:把一个完整的 HTML 页面收窄成「文章正文那一块」的纯文本。

只在 `kb.feeds.fulltext` 那条路上用 —— 聚合型订阅(HN Buzzing 那类)的 feed
正文是个跳转壳,真正的内容在原文链接后面。

**为什么自己写而不是装 readability/trafilatura。** 两条理由,缺一条都不够:
①我们要的输出是**纯文本**,而且块级边界的口径已经由 `fetching.plain` 定死了
  (索引、翻译切块、vault md 的分段全按它来)。那些库输出的是 HTML 片段,拿回来
  还得再过一遍 `plain` —— 真正用得上的只是「选出正文那个容器」这一步,也就是
  下面这几十行打分。
②`pyproject.toml` 一动 `uv run` 就要重建 editable 包,而那一步会去 pypi 问
  hatchling 的最新版(见 CLAUDE.md「起服务那条命令一律 uv run --offline」:
  2026-10-08 网络一抖,服务整天起不来)。为一个"挑容器"的启发式付这个代价
  不划算。

**提取不出来不是故障,是「这一页我们读不懂」**:调用方保留 feed 原本那份壳,
条目照旧入库。宁可少补一篇,也不能把导航栏当正文译了写进知识库 —— 后者要花
模型钱,还会污染检索。
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from kb.feeds.fetching import plain

# 页面大小上限。打分要对每个候选各 `get_text()` 一次,成本是 O(节点数 × 深度);
# 正常文章页几百 KB,而 `fetching.MAX_BYTES` 放到 8 MB —— 真遇到那种页面宁可
# 直接说"读不懂"也不要把一轮同步卡在一次提取上。
MAX_HTML = 3 * 1024 * 1024
MAX_CANDIDATES = 4000
# 提取结果短于这个数就当没提取到。一篇正文再短也有几百字;比这还短多半是
# 命中了摘要块、版权行或者一句"请开启 JavaScript"。
MIN_CHARS = 400
# 链接文字占比超过它 = 这是个链接堆(导航、相关阅读、归档列表),不是正文。
MAX_LINK_DENSITY = 0.4

# 整块扔掉:它们要么不是内容(脚本、样式),要么在任何位置上都不是正文。
# <form>/<button>/<select> 一起扔是因为订阅框、搜索框常常夹在正文中间;
# <title> 是给没有 <body> 的页面片段兜底的(那时 root 会退回整份文档)。
_DROP_TAGS = ('script style noscript iframe nav form button select option textarea '
              'svg canvas video audio source track map area link meta title').split()
# **这三个要看位置**:`<article><header>` 里装的是文章自己的标题、作者和日期,
# 无条件删掉就是从正文顶上切掉一截(实测这一条会让「By Carson Gross, February
# 2026」整行消失);`<article><aside>` 多半是题外话或者引文块,也是内容。只有
# 落在 <article>/<main> 外面的那些才是站点页眉、页脚和侧栏。
_DROP_OUTSIDE = ('header', 'footer', 'aside')
# ARIA 角色比 class 名可靠:写了它的页面基本都写对了。
_DROP_ROLES = frozenset('navigation banner contentinfo complementary search form menu menubar'.split())
# class / id 里的噪声词。连字符、下划线、空格都算分隔符,所以 `site-footer`、
# `footer_wrap`、`post footer` 都认得出,而 `footerball` 这种词中词不会误伤。
_NOISE = re.compile(
    r'(?:^|[-_\s])(?:nav|navbar|menu|sidebar|side|footer|header|masthead|topbar|'
    r'comment|comments|disqus|respond|share|sharing|social|related|recommend|'
    r'promo|promotion|advert|ads|adsense|banner|breadcrumb|crumbs|pagination|pager|'
    r'popup|modal|overlay|cookie|consent|gdpr|subscribe|newsletter|signup|'
    r'toolbar|widget|skip|hidden|offscreen|sr-only|screen-reader)(?:[-_\s]|$)', re.I)
# 这几个标签说的是"这里是正文",给加权。<article> 最强:HTML5 里它的定义就是
# 一篇可独立分发的内容。
_SEMANTIC = {'article': 1.6, 'main': 1.4}
_CANDIDATE_TAGS = ('article', 'main', 'section', 'div', 'td')
# 收缩包装链时的容忍度:子容器得分到了父容器的这个比例就取子的。
# `<div class=wrapper><article>…</article></div>` 这种层层包装很常见,外层
# 只多出几个字的边框文字却因为更长而得分更高 —— 取外层就会把面包屑、作者卡、
# 下一篇链接一起带进正文。
_SHRINK = 0.92

_META_CHARSET = re.compile(rb'<meta[^>]+charset=["\']?\s*([\w.-]+)', re.I)
_HTTP_CHARSET = re.compile(r'charset=\s*["\']?\s*([\w.-]+)', re.I)

# 能当网页解析的 content-type。`text/plain` 留在里面是因为实践中大量小站把
# HTML 页配成了它,而真正的纯文本页经提取器出来仍是可读文字(只是段落按 HTML
# 规则折叠了)——比一个跳转壳强。真正要挡的是二进制,那由下面两道管。
_TEXT_TYPES = frozenset({'text/html', 'application/xhtml+xml', 'application/xhtml',
                         'application/html', 'text/plain'})
# 已知的二进制文件头。content-type 可以撒谎、也可以压根不发,而这些字节不会
# 出现在一个网页的开头。
_MAGIC = ((b'%PDF', 'PDF'), (b'%!PS', 'PostScript'), (b'\x89PNG', 'PNG 图片'),
          (b'GIF8', 'GIF 图片'), (b'\xff\xd8\xff', 'JPEG 图片'), (b'BM', 'BMP 图片'),
          (b'II*\x00', 'TIFF 图片'), (b'MM\x00*', 'TIFF 图片'), (b'\x00\x00\x01\x00', 'ICO 图标'),
          (b'PK\x03\x04', 'ZIP 或 Office 文件'), (b'\x1f\x8b', 'gzip 压缩包'),
          (b'7z\xbc\xaf', '7z 压缩包'), (b'Rar!', 'RAR 压缩包'),
          (b'\xd0\xcf\x11\xe0', '旧版 Office 文档'), (b'{\\rtf', 'RTF 文档'),
          (b'RIFF', 'RIFF 媒体'), (b'OggS', 'Ogg 媒体'), (b'\x1aE\xdf\xa3', 'Matroska 媒体'),
          (b'ID3', 'MP3 音频'), (b'\x7fELF', '可执行文件'), (b'\xca\xfe\xba\xbe', '可执行文件'))
_SNIFF = 4096
# BOM 定下的编码。**UTF-32 必须排在 UTF-16 前面**:`\xff\xfe\x00\x00`(UTF-32 LE)
# 以 `\xff\xfe`(UTF-16 LE)开头,反过来排的话每一篇 UTF-32 页面都被当成 UTF-16
# 解,解出来是一堆 CJK 乱码。顺序照 WHATWG 那套编码嗅探:BOM 比 HTTP charset
# 和 `<meta>` 都更可信 —— 它是内容自己带上来的。
_BOM_CODECS = ((b'\x00\x00\xfe\xff', 'utf-32-be'), (b'\xff\xfe\x00\x00', 'utf-32-le'),
               (b'\xef\xbb\xbf', 'utf-8-sig'),
               (b'\xff\xfe', 'utf-16-le'), (b'\xfe\xff', 'utf-16-be'))


def html_problem(body: bytes, headers: dict | None = None) -> str:
    """这份响应能不能当网页解析 —— 不能就返回一句人话,否则返回空串。

    **缺这一道的后果不是"少补一篇",是把文件语法当正文写进知识库。** 原文是
    PDF 论文时,`decode` 按 `errors='replace'` 把那堆字节解成一段 `%PDF-1.4
    1 0 obj << /Type /Catalog …` 加几百个问号,`extract` 眼里它只是一份没有
    标签的文档、整篇就是"正文",长度又轻松越过 `MIN_CHARS` —— 于是补抓报成功、
    `fulltext_at` 落下、条目离开重试队列,接着拿这堆乱码去买一次翻译
    (2026-10-09 审查实测:1149 字正文、364 个替换字符,论文一个字都没有)。
    **可以不支持 PDF,但不能把"没读到论文"说成"已取得正文"。**

    三道,各管一类撒谎方式:
    ①**声明的类型**不在名单里 —— 站点自己说了这不是网页,照它说的办;
    ②**文件头**命中已知魔数 —— content-type 配错或压根没发时(小站常态)靠它;
    ③头部出现 **NUL 字节** —— 没见过的二进制格式兜底。文本里不会有它,
      `fetching.plain` 的入口本来也要把 `\x00` 清掉(那是它的占位符)。
      **UTF-16/32 的正常文本每隔一个字节就是 `\x00`,所以这一道不能裸着问
      「有没有 NUL」** —— 那是拿编码本身的产物当二进制的证据,一篇正常的宽编码
      网页会因此永远补不到正文(第 2 轮审查实测 914 字的正文被判成「像是二进制
      文件」)。

      **判据因此是「拿真正会用的那个解码器解一次,NUL 还在才算二进制」。**
      上一版是另开一张名单(`_wide_text`:BOM 或声明的 charset 命中就放行),
      而 `decode` 压根没有 BOM 分支 —— 两张名单一漂,**只有 BOM、不声明 charset**
      的 UTF-16/32 页面就从「安全失败」变成了「错误覆盖 + 成功退出队列」:放行
      之后按 UTF-8 解成乱码、NUL 留在里面,HTML 解析认不出结构,`plain` 清掉 NUL
      之后反倒把整段 HTML 源码当成了正文,长度轻松过门槛 —— 补抓**报成功**、
      `fulltext_at` 落下、这一篇**永远**离不开壳(2026-10-09 第 3 轮审查实测:
      中文标题丢失、导航和评论全混进正文、7 个替换字符,UTF-16/UTF-32 各一次)。
      直接调 `decode` 就没有"两张名单"可言:解对了没有 NUL,解不出来 NUL 还在。

    调用方把它当抓取失败处理(计次、保留 feed 原本那份壳):PDF 这种链接重试
    三轮都不会变成网页,而那正是 `fulltext.MAX_TRIES` 这个出口要收的东西 ——
    比起"成功了"这个结论,多两次请求便宜得多。
    """
    ctype = ''
    for name, value in (headers or {}).items():
        if str(name).lower() == 'content-type':
            ctype = str(value)
    mime = ctype.split(';')[0].strip().lower()
    if mime and mime not in _TEXT_TYPES:
        return f'原文返回的不是网页（{mime}），没有可提取的正文'
    head = body[:_SNIFF].lstrip()
    for magic, label in _MAGIC:
        if head.startswith(magic):
            return f'原文返回的不是网页（{label}），没有可提取的正文'
    if b'\x00' in head and '\x00' in decode(body[:_SNIFF], headers):
        return '原文返回的像是二进制文件，没有可提取的正文'
    return ''


def decode(body: bytes, headers: dict | None = None) -> str:
    """按 BOM → HTTP 头 → <meta charset> → UTF-8 的顺序解码,一律不抛。

    `errors='replace'` 而不是 `strict`:猜错编码只会让几个字变成问号,而抛出去
    就是整篇正文都没有 —— 那一条会记成抓取失败、重试三轮,最后还是壳。

    **BOM 排在最前面,而且这是 `html_problem` 那道 NUL 判据的前提**:它就是拿
    这个函数解一次来回答「这堆 NUL 是编码的一部分还是二进制」。少了 BOM 这一
    分支,只带 BOM、不声明 charset 的 UTF-16/32 页面会被 UTF-8 解成乱码(NUL
    照旧留着)—— 见 `html_problem` 第三道那段实测。顺带认 UTF-8 的 BOM:不认
    的话正文开头会多一个 `\ufeff`,而 `fetching.plain` 的空白折叠不吃它。
    """
    for bom, codec in _BOM_CODECS:
        if body.startswith(bom):
            return body.decode(codec, errors='replace')
    names = []
    if m := _HTTP_CHARSET.search((headers or {}).get('content-type', '')):
        names.append(m[1])
    if m := _META_CHARSET.search(body[:4096]):
        names.append(m[1].decode('ascii', 'ignore'))
    for name in [*names, 'utf-8']:
        try:
            return body.decode(name, errors='replace')
        except (LookupError, ValueError):       # 编码名是页面写的,可能根本不存在
            continue
    return body.decode('utf-8', errors='replace')


def _noisy(tag) -> bool:
    """这个容器是页面外围部件吗 —— 只看它自己声明的身份,不看内容。"""
    if (tag.get('role') or '').strip().lower() in _DROP_ROLES:
        return True
    if tag.get('aria-hidden') == 'true':
        return True
    # class 是个列表,id 是个字符串;拼成一段文字一起匹配就行。
    marks = ' '.join(tag.get('class') or []) + ' ' + (tag.get('id') or '')
    return bool(_NOISE.search(marks))


def _gone(node) -> bool:
    """这个节点已经被别人连根拔掉了吗。

    `find_all` 交出来的是一份**快照**,而 `decompose()` 会把整棵子树一起销毁
    (bs4 把被销毁节点的 `attrs` 置成 None)。于是列表里排在后面的那些后代再被
    碰一下就是 `AttributeError: 'NoneType' object has no attribute 'get'` ——
    一个 `<div class="sidebar">` 里套着另一个 `<div class="share">` 就够了,
    而这在真实页面上是常态:实测 ken.arneson.name 和 frontiersin.org 那两篇
    当场抛出来,整条补抓记成失败、重试三轮之后按壳入库 —— 而页面本身好好的。
    `decomposed` 是 bs4 自己的标记,`getattr` 兜一道是防它哪个版本没有这个
    属性,那时退回看有没有父节点。"""
    if getattr(node, 'decomposed', False):
        return True
    return node.parent is None and node.name not in ('html', '[document]')


def _strip(soup) -> None:
    for node in soup(_DROP_TAGS):
        if not _gone(node):
            node.decompose()
    for node in soup.find_all(_DROP_OUTSIDE):
        if not _gone(node) and not node.find_parent(['article', 'main']):
            node.decompose()
    # 语义标签不参与噪声剔除:`<article class="post-header">` 里的 header 说的是
    # "这是文章的开头",不是"这是站点页眉" —— 按 class 名把它删掉就会从正文顶上
    # 切掉一截。页面外围部件用的是 div/section,它们才是这条规则的射程。
    for node in soup.find_all(['div', 'section', 'ul', 'ol', 'figure', 'table']):
        if not _gone(node) and _noisy(node):
            node.decompose()


def _link_density(tag, length: int) -> float:
    if not length:
        return 1.0
    link = sum(len(a.get_text(' ', strip=True)) for a in tag.find_all('a'))
    return min(1.0, link / length)


def _score(tag) -> float:
    """这个容器有多像文章正文。0 = 不是。

    三个因子:**纯文本长度**(正文总比边框长)、**链接密度的补**(链接堆要压下去)、
    **段落数**(一篇文章会被切成很多 <p>,一个侧栏不会)。段落那一项封顶,免得
    一个有几百条列表项的归档页靠数量把真正文顶下去。
    """
    text = tag.get_text(' ', strip=True)
    length = len(text)
    if length < MIN_CHARS:
        return 0.0
    density = _link_density(tag, length)
    if density > MAX_LINK_DENSITY:
        return 0.0
    paragraphs = len(tag.find_all(['p', 'pre', 'blockquote', 'h2', 'h3']))
    return length * (1 - density) * (1 + min(paragraphs, 20) * 0.08) * _SEMANTIC.get(tag.name, 1.0)


def _shrink(best, score: float):
    """沿着包装链往里走,找最深的那个"还是同一块内容"的容器。

    判据是得分**不低于**父容器的 `_SHRINK` —— 不是"更高"。包装层通常只多出
    几个字(面包屑、分享按钮),得分比里面那层略高却不多,按"更高才换"永远
    收缩不动,而那几个字正是我们不想要的东西。
    """
    node = best
    while True:
        inner = [(c, _score(c)) for c in node.find_all(_CANDIDATE_TAGS, recursive=False)]
        picked = [(c, s) for c, s in inner if s >= score * _SHRINK]
        if len(picked) != 1:
            # 0 个 = 已经到正文了;2 个以上 = 这一层真的在并列几块内容,再往里
            # 走就只能二选一,那是在猜。两种都该停在当前这一层。
            return node
        node, score = picked[0]


def extract(html: str) -> str:
    """返回正文纯文本;提取不出来返回空串(调用方据此保留 feed 原本那份)。"""
    if not html or len(html) > MAX_HTML:
        return ''
    soup = BeautifulSoup(html, 'html.parser')
    _strip(soup)
    root = soup.find('body') or soup
    candidates = root.find_all(_CANDIDATE_TAGS)
    if len(candidates) > MAX_CANDIDATES:
        return ''
    # root 自己也要参赛:很多页面清掉外围部件之后 <body> 下面就直接是 <p> 了,
    # 压根没有一个包住正文的容器。
    scored = [(root, _score(root)), *((c, _score(c)) for c in candidates)]
    best, score = max(scored, key=lambda pair: pair[1])
    if score <= 0:
        return ''
    text = plain(str(_shrink(best, score)))
    return text if len(text) >= MIN_CHARS else ''
