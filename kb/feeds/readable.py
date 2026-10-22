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
# **这条线量的是「要建成树的那部分」,不是整份文件有多长。** `<script>`/`<style>`
# 的内容按 HTML 规范是原始文本(html.parser 把这两个列在 CDATA_CONTENT_ELEMENTS
# 里,整段只落一个文本节点),建树成本和它多长基本无关,而 `_strip` 第一件事就是
# 把它们整块扔掉 —— 把它们算进这条线,等于拿一笔根本不会花的钱去拒稿。
# 事故形态(2026-10-21 实测):绿泡泡的文章页 3.74 MB,其中 3.57 MB 是内联脚本,
# 真标记只有 0.17 MB。提取器在**解析之前**就 `return '', ''`,于是手动收录对着一篇
# 完完整整读得到的文章说「这一页没找到正文」—— 而它和真正读不到的那一档
# (登录墙、纯 JS 渲染)在界面上长得一模一样。
# 成本实测(同一台机器):解析 8 MB 全脚本 0.92s、2.7 MB 真标记 1.06s,而下面
# 这条正则扫 10 MB 只要 0.037s —— 卡住真标记那一头就够了,也只有那一头值得卡。
# **替换保留两头的标签**:`<script>x</script>` 重复几十万次的那种页面,内容虽短
# 但节点数真实存在,整段抹成空串会让它从这条线底下溜过去。
# **只在整份超线时才扫这一趟**(见 `extract_or_reason`):正常页面一个字节都不多花。
_RAW_TEXT = re.compile(r'<(script|style)\b[^>]*>.*?</\1\s*>', re.I | re.S)
MAX_CANDIDATES = 4000
# 提取结果短于这个数就当没提取到。一篇正文再短也有几百字;比这还短多半是
# 命中了摘要块、版权行或者一句"请开启 JavaScript"。
MIN_CHARS = 400
# 链接文字占比超过它 = 这是个链接堆(导航、相关阅读、归档列表),不是正文。
MAX_LINK_DENSITY = 0.4
# 授权/法律文字占到这个比例 = 这一块是版权区,不是文章(见 `boilerplate_reason`)。
# 生产 441 条补抓成品实测:真文章最高 0.11,而审查抓出来那条是 0.96 —— 中间
# 差 4.5 倍,线放在中点上。
MAX_LEGAL_SHARE = 0.5
# 作者简介抬头之后还不够长的,整块就是作者简介(同一份实测:441 条里只有审查
# 点名那一条命中,474 字)。一篇长文章末尾挂着作者简介是常态,所以这一道**只看
# 开头**、而且只在短块上成立。
MAX_BYLINE_CHARS = 1500

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
#
# `author`/`byline` 在里面:作者卡是页面外围部件,而它**足够长、链接又不多**,
# 一个没有文章正文的壳页面上它会直接赢(2026-10-09 审查实测:Stripe 那篇抽出来
# 的"正文"是 `About the authors` 加三位作者简介,474 字、7 个段落、得分 532,
# 而整页别的候选全是 0 分)。**`<article>` 里的作者署名不受影响**:`_noisy` 只
# 对 div/section/ul/ol/figure/table 调用,`<article><header>` 里那行「By Carson
# Gross, February 2026」由 `_DROP_OUTSIDE` 的位置规则保护着。
_NOISE = re.compile(
    r'(?:^|[-_\s])(?:nav|navbar|menu|sidebar|side|footer|header|masthead|topbar|'
    r'comment|comments|disqus|respond|share|sharing|social|related|recommend|'
    r'promo|promotion|advert|ads|adsense|banner|breadcrumb|crumbs|pagination|pager|'
    r'popup|modal|overlay|cookie|consent|gdpr|subscribe|newsletter|signup|'
    r'author|byline|'
    r'toolbar|widget|skip|hidden|offscreen|sr-only|screen-reader)(?:[-_\s]|$)', re.I)
# **驼峰也算分隔符。** 上面那张名单要求噪声词两头是 `-`/`_`/空格,而 CSS Modules、
# styled-components 和现代框架吐出来的 class 名是驼峰 —— 于是名单里明明有
# `footer`/`related`/`share`/`nav`/`sidebar`,`siteFooter`、`relatedPosts`、
# `shareBar`、`navMenu`、`sidebarWrap` **一个都认不出**(2026-10-09 审查实测:
# Stripe 那块的 class 是 `BlogPost-module__ifVEqa__aboutAuthor`)。先在
# 「小写或数字 → 大写」的边界插一个空格再匹配,名单一个字不用改。
_CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')
# 这几个标签说的是"这里是正文",给加权。<article> 最强:HTML5 里它的定义就是
# 一篇可独立分发的内容。
_SEMANTIC = {'article': 1.6, 'main': 1.4}
_CANDIDATE_TAGS = ('article', 'main', 'section', 'div', 'td')
# 收缩包装链时的容忍度:子容器得分到了父容器的这个比例就取子的。
# `<div class=wrapper><article>…</article></div>` 这种层层包装很常见,外层
# 只多出几个字的边框文字却因为更长而得分更高 —— 取外层就会把面包屑、作者卡、
# 下一篇链接一起带进正文。
_SHRINK = 0.92

# 版权区的措辞。这张名单小而极稳 —— 这几句话几十年没变过,而且它们**不会**
# 成段出现在一篇文章里。
_LEGAL = re.compile(
    r'(?:all rights reserved|terms of (?:use|service)|privacy (?:policy|notice)'
    r'|cookie policy|do not sell|is a (?:registered )?trademark|trademarks? of'
    r'|subject to change without notice|may receive compensation|©|\(c\)\s*\d{4}'
    r'|copyright\s+\d{4}|user agreement|acceptable use|notice at collection)', re.I)
# 作者简介块的标准抬头。开头那个可选的 `/` 是面包屑残留(实测 Stripe 那块出来
# 就是 `About the authors\n\n/About the authors`)。
# **只认「这一块是作者简介区」的抬头,不认署名。** `written by` 一度在里面,
# 那是错的:它是**正常文章的标准开头**(`<article><header>` 里那行署名),而
# `boilerplate_reason` 只凭开头加一个长度上限就整篇拒绝 —— 于是一篇有署名、
# 标题和三段正文的短文(580 字)被判成「只有作者简介」,连抓三轮都只留下链接
# (2026-10-09 第 2 轮审查实测)。署名属于正文,本块的硬约束(`docs/decisions/fulltext.md`)里早就记着
# 「`<article><header>` 里那行『By Carson Gross, February 2026』」要留住。
# 剩下这几项都是在声明"下面是作者简介",不是在署名:Stripe 那页开头逐字是
# `About the authors`,照旧挡得住。
_BYLINE_HEAD = re.compile(
    r'^\s*(?:/\s*)?(?:about\s+(?:the\s+)?(?:author|contributor)s?'
    r'|author\s+bios?|作者简介|关于作者)\b', re.I)
# 断句:句末标点 + 空白,或者块级边界。`plain` 已经把块级边界变成换行了。
_SENTENCE = re.compile(r'(?<=[.!?。！？])\s+|\n+')

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
    # class 是个列表,id 是个字符串;拼成一段文字一起匹配就行。驼峰先拆开
    # (见 `_CAMEL`),否则 `siteFooter` 这类现代写法整条名单都对它失效。
    marks = ' '.join(tag.get('class') or []) + ' ' + (tag.get('id') or '')
    return bool(_NOISE.search(_CAMEL.sub(' ', marks)))


def in_furniture(node) -> bool:
    """这个节点待在**页面外围部件**里吗 —— 页眉、页脚、侧栏、导航那一类。

    判据和正文提取剔噪那一套**同一份**(`_noisy` + `_DROP_OUTSIDE` 的位置规则):
    手动收录那边要分清「这道登录墙拦在正文上」还是「页眉里常驻的一个登录框」
    (`kb.feeds.grab_page`),问的正是同一个问题。两处各写一份名单的话,一处认得出
    `siteFooter`、另一处认不出,而那种差别只在某些页面上看得见。

    **按节点自己声明的身份判,不看内容**(同 `_noisy`)。

    **三类都要覆盖,少一类就和提取器的结论不一致**:`nav` 在 `_DROP_TAGS` 里被
    **无条件**丢掉(它从来不是正文,不看位置),`_DROP_OUTSIDE` 那三个看位置,
    其余按 `_noisy` 的 class/id/role。漏掉 `nav` 的后果实测过:一篇正常技术文章
    的导航条里常驻一个登录框,手动收录那边照「有一道登录表单」把整篇拒了
    (2026-10-16 审查实测),而提取器自己压根不把那段当正文。
    """
    for parent in node.parents:
        if getattr(parent, 'name', None) is None:
            continue
        if parent.name == 'nav':
            return True
        if parent.name in _DROP_OUTSIDE and not parent.find_parent(['article', 'main']):
            return True
        if parent.name in ('div', 'section', 'ul', 'ol', 'figure', 'table') and _noisy(parent):
            return True
    return False


def substance(text: str) -> int:
    """这段正文里**不重复**的字数 —— 「它到底说了多少东西」。

    按**行**(提取器输出的段)去重再数:同一段铺满整页的模板文字在这里当一段算。
    行内的重复不算(真实文章的段落不会复读,夹具里那种 `'…' * 12` 只是省事写法)。

    **这不是提取器自己的门槛**(`MIN_CHARS` 那一道照旧按原始长度算,
    `extract_or_reason` 的两档一个字都没改):它给的是手动收录那边
    「这一次响应能不能当成这一篇的正文落库」那一问的内容质量判据
    (`grab_page.verdict` 的 ⑧)。放在这里是因为它和 `plain` 的输出格式
    绑在一起 —— 换了分段方式这一问就得跟着改,两处各写一份迟早漂。

    事故形态(2026-10-16 审查实测):一张「Sign in to continue reading /
    subscribe to unlock this story」的登录提示页,同一段铺了 7 遍 —— 提取器
    交出 1832 字,份额判据对它不成立(那一页话很多)、措辞不在关卡表里、DOM 里
    **一个表单都没有**,于是被当成正文落库,`DO NOTHING` 之下永久改不回来。
    按行去重之后它只剩 260 字,连提取器那道 400 的门槛都过不了。
    """
    seen: set[str] = set()
    total = 0
    for line in (text or '').splitlines():
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            total += len(line)
    return total


def markup_size(html: str) -> int:
    """这份 HTML 里**要建成树**的那部分有多大(扣掉脚本和样式的正文)。

    判据和代价写在 `_RAW_TEXT` 的注释里。两头都要钉:脚本撑大的页面照旧解析,
    真标记超线的页面照旧拒。
    """
    return len(_RAW_TEXT.sub(lambda m: f'<{m.group(1)}></{m.group(1)}>', html))


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


def boilerplate_reason(text: str) -> str:
    """这段文字是页面样板区而不是文章主体吗 —— 是就返回一句人话,否则空串。

    **缺这一道的后果不是"少补一篇",是把版权声明当文章正文覆盖掉原内容,而且
    永久不再纠正。** `_score` 只问"够长吗、链接多吗、分了几段",那回答得了
    "拿到了一段网页文字",回答不了"拿到的是文章主体"。于是纯 JS 渲染的页面上
    (文章正文只在 `<script>` 的 flight payload 里、DOM 里压根没有)剩下的那点
    边角文字成了唯一候选,长度又刚好越过 `MIN_CHARS` —— 补抓**报成功**、
    `fulltext_at` 落下、条目离开重试队列,接着拿它去买一次翻译,页面上还标着
    「订阅正文 · 已译为中文」(2026-10-09 审查在生产库里抓到两条成品:Fortune
    那篇 455 字全是版权和隐私政策,Stripe 那篇 474 字是三位作者的简介)。

    两道,**各管一类、各有真实样本,不是一道的两种写法**:
    ①**版权区**:授权措辞占到句子的 `MAX_LEGAL_SHARE`。判据是**占比**而不是
      命中次数 —— 长文章末尾挂一段版权声明是常态,按次数算会把它们一起毙掉,
      按占比算那一档天然很低(生产 441 条实测:真文章最高 0.11)。
    ②**作者简介区**:开头就是作者简介的抬头,而整块还不到 `MAX_BYLINE_CHARS`。
      只看开头,因为一篇长文章**末尾**带作者简介同样是常态。

    **为什么内容判据和 `_NOISE` 那道 class 判据都要有**:Stripe 那块的 class
    自己报了身份(`aboutAuthor`),删掉它整页就只剩 306 字、全部 0 分 —— 那条路
    更可靠,因为是页面自己说的。可 Fortune 那块是 `<div id="S:1">`,没有 class、
    id 是 React 的 slot 编号,class 判据对它完全是瞎的。反过来,**存量修复手里
    只有 `body`、没有 HTML**(`scripts/feed_fulltext_recheck.py`),那时只有这份
    纯文本判据用得上。两道的射程不重叠。

    判中之后 `extract` 返回空串,调用方按"这一页读不懂"处理:壳照旧留着、
    `fulltext_tries` 计次、试满 `MAX_TRIES` 出队(口径同注释顶上那段)。
    **宁可少补一篇**,也不能把版权声明译了写进知识库。
    """
    parts = [s for s in _SENTENCE.split(text) if s and s.strip()]
    total = sum(len(s) for s in parts)
    if not total:
        return ''
    legal = sum(len(s) for s in parts if _LEGAL.search(s))
    if legal / total >= MAX_LEGAL_SHARE:
        return '原文页面上只有版权与授权声明，没有文章正文'
    if len(text) <= MAX_BYLINE_CHARS and _BYLINE_HEAD.match(text):
        return '原文页面上只有作者简介，没有文章正文'
    return ''


def extract_or_reason(html: str) -> tuple[str, str]:
    """`(正文纯文本, 读不懂的原因)`。正文非空时原因必为空,反之不一定。

    **带理由的那一半只有样板区这一档给得出来**(版权声明、作者简介),别的
    读不懂(付费墙、纯 JS 渲染、整页一张图)压根没有判据可言,两个都是空串。
    理由存在的意义是**面板上要说得出是哪一类** —— 口径同 `fetch_one` 对
    `html_problem` 那段:"可能需要登录或由脚本渲染"这句通用文案对一个明确
    判出"这页上只有版权声明"的页面是在少说一件已经知道的事。

    最后那道 `boilerplate_reason` 判的是「赢出来这一块是不是文章主体」——
    **判中就整个作废、不退而取次优**:次优候选在同一个页面上多半是同一摊边角
    文字(Fortune 那页的次优是 `<body>` 自己,481 字里 455 字正是那段版权声明),
    挑它只是把同一个错误换个来源。
    """
    if not html or (len(html) > MAX_HTML and markup_size(html) > MAX_HTML):
        return '', ''
    soup = BeautifulSoup(html, 'html.parser')
    _strip(soup)
    root = soup.find('body') or soup
    candidates = root.find_all(_CANDIDATE_TAGS)
    if len(candidates) > MAX_CANDIDATES:
        return '', ''
    # root 自己也要参赛:很多页面清掉外围部件之后 <body> 下面就直接是 <p> 了,
    # 压根没有一个包住正文的容器。
    scored = [(root, _score(root)), *((c, _score(c)) for c in candidates)]
    best, score = max(scored, key=lambda pair: pair[1])
    if score <= 0:
        return '', ''
    text = plain(str(_shrink(best, score)))
    if len(text) < MIN_CHARS:
        return '', ''
    if why := boilerplate_reason(text):
        return '', why
    return text, ''


def extract(html: str) -> str:
    """返回正文纯文本;提取不出来返回空串(调用方据此保留 feed 原本那份)。"""
    return extract_or_reason(html)[0]
