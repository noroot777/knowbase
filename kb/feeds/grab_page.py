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
from urllib.parse import urlsplit
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

# ── 访问关卡 ───────────────────────────────────────────────────────────────────
# **判据一律是「这一次响应/这一页自己的身份」,不是「它有多长、命中了几个词」。**
# 上一轮两头都踩了(2026-10-16 第二轮审查两例俱在):按「超过 1200 字就放行」漏掉
# 一张 1782 字的验证页 —— 它被 `put_items` 的 `DO NOTHING` 永久钉成了那一篇的
# 正文;按「路径里有 `challenge` 这一段」拒掉一篇正经文章 —— 这一道在成功路径上
# 也生效,误拒就是这一篇再也收不进来。**所以别再靠调字数、堆关键词来修**:要加的
# 是身份证据。身份证据四样,一条成立就算关卡,`gate_reason` 挨个问。

#: ①**路由**:各家关卡自己的那几条路径/参数,按**最终地址**(`request` 跟完
#: 重定向之后那一个)的 path + query 比,和提取器交得出什么无关。
#: 绿泡泡实测(2026-10-16,`mp.weixin.qq.com/s/<slug>`):302 到
#: `/mp/wappoc_appmsgcaptcha?poc_token=…&target_url=…`,18 KB 的验证页,
#: og 元信息和 `#js_content` 全都没有。
#: **一个通用英文词都不许进这张表**(`challenge`、`checkpoint`、`verify` 这些):
#: 正常文章的路径里就带着它们 —— 实测 developers.cloudflare.com 那篇 1277 字的
#: 正经文档落在 `/waf/reference/cloudflare-challenges/`、审查那篇落在
#: `/blog/challenge/`,两篇都该收进来。
_GATE_ROUTES = ('appmsgcaptcha', 'wappoc_', '/cdn-cgi/l/chk', '/sorry/index',
                '__cf_chl_tk')
#: ②**页面上真挂着的那套关卡组件**。这是页面身份:一篇讲 Cloudflare 的文章不会
#: 把关卡编排脚本挂在自己页上。
#: **判据是 DOM 里真实的那几个属性**(`script[src]`、`iframe[src]`、`link[href]`、
#: `form[action]`,外加 `<script>` 自己的内联代码),**不是在整份 HTML 上找子串** ——
#: 后者会把一篇**讲**关卡的教程里那段转义的示例代码当成页面身份:实测一篇 731 字
#: 的正经教程,`<pre>` 里只有一行 `&lt;script src="/cdn-cgi/challenge-platform/h/…"&gt;`,
#: 既没有生效脚本也没有验证动作,却被拒了(2026-10-16 第 3 轮审查)。而这一道在
#: 成功路径上也生效,误拒就是这一篇再也收不进来。
#: **通用的验证码小组件不许进这张表**(reCAPTCHA / hCaptcha / Turnstile 那几个
#: `api.js`):正常文章页的评论框就挂着它们,进表等于按「这站评论要过验证码」拒稿。
#: Cloudflare 那一条也只收**编排**路径(`challenge-platform/h/`)而不是整个
#: `challenge-platform/` —— 后者的 `jsd/main.js` 在普通页面上也会被注入。
_GATE_ASSETS = ('/cdn-cgi/challenge-platform/h/', '_cf_chl_opt',
                'captcha-delivery.com/captcha')
#: 去哪几个属性里找它们。**只收会让浏览器真去加载/提交的那些**。
_ASSET_ATTRS = (('script', 'src'), ('iframe', 'src'), ('link', 'href'),
                ('form', 'action'), ('img', 'src'))
#: ③**标题自己就是一句关卡提示**。全是**祈使句或状态播报** —— 正经文章的标题不
#: 这么写,所以「人机验证是怎么坏掉的」这种不在射程内。反过来说:`验证码`、
#: `captcha`、`challenge` 这类**话题词**一个都不许进这张表。
#: 标题单独不算数,要配上正文里也在说同一件事(见 `gate_reason` 的 ③)。
_GATE_TITLES = ('just a moment', 'attention required', 'access denied',
                'checking your browser', 'verify you are human',
                'are you a robot', 'security check', 'one more step',
                '环境异常', '请完成验证', '完成验证后', '安全验证', '请稍候',
                '请在微信客户端打开')
#: 关卡措辞:**只收强措辞**(`captcha` 这类正常文章里会出现的词不在内)。
#: ③ 用它确认「正文里也在说这件事」,④ 用它算份额。
_GATE_PHRASES = ('环境异常', '完成验证', '请在微信客户端打开', 'enable javascript',
                 'verify you are human', 'checking your browser',
                 'just a moment', 'attention required')
#: ④**通篇就是那几句提示**:命中的句子占了正文三分之二以上。
#: **按份额算、不设字数上限** —— 上限就是上一轮那个洞:页面长一点就一路放行。
#: 实测两侧(2026-10-16):审查那张 1782 字的验证页 67%;而一篇正经讲拦截页的
#: 文章反复引那几句也只到 50%(`tests/test_grab.py` 拿它钉着这条线的另一侧),
#: 真实的 Cloudflare 文档是 0%。
_GATE_SHARE = 0.66
#: 句界只按 CJK 标点和换行切。**`. ` 不算**:域名、缩写、小数点会把一句话切成好
#: 几截,份额跟着抖。
_SENTENCE = re.compile(r'[^\n。！？!?；;]+')
#: 提取不出正文时才用的宽词表:那时已经没有正文可言,宁可多认一道关卡 ——
#: 这一档多拒的代价是一句话,而错存的代价不可恢复。
_GATE_WORDS = _GATE_PHRASES + ('captcha', '滑块验证', 'cf-browser-verification')
#: ⑤**正文挡在一道登录/订阅墙后面**。判据也是身份,两样:
#: ①DOM 里有一道**拦在正文上**的登录表单(`form` 里有 `input[type=password]`,
#:   而且它不待在页眉页脚侧栏那类外围部件里 —— 位置判据借正文提取自己那一份
#:   `readable.in_furniture`,不另写一张噪声名单);
#: ②页面自己用 schema.org 声明了内容不免费(`isAccessibleForFree: false`,
#:   那是付费墙的标准产者签名,Google 的付费墙规范要求的就是它)。
#: **不靠措辞。** 事故形态(2026-10-16 第 3 轮审查实测):一张普通的
#: 「Sign in to continue reading / subscribe to unlock this story」登录页,提取器
#: 交出 1832 字 —— 份额判据(④)对它压根不成立(那一页话很多),措辞也不在
#: `_GATE_PHRASES` 里,于是它被当成正文落库,`DO NOTHING` 之下永久改不回来。
#: 扩词表治不了这一类:墙前面那段话每家写法都不同,而**墙本身是结构**。
#: 代价如实记着:一篇**渲染了真实密码框**的文章(比如手把手教你写登录表单、且
#: 把 demo 放在正文区里)会被误拒。两个方向差着一个量级 —— 误拒是这一篇得自己
#: 复制正文,错存是永久钉死一段不是正文的文字。
_WALL_FREE_KEYS = ('isAccessibleForFree', 'isaccessibleforfree')
#: 两句话,四个判据共用。地址/组件那一档和正文那一档对用户的意思不同(前者连页面
#: 都不是我们要的,后者是这一页自己说要先过验证),所以分开写;**再往下分没有意义**
#: —— 用户能做的事一模一样。
_SAY_ROUTE = '对方把这个地址挡在了一道验证页后面'
_SAY_PAGE = '这一页要求先完成验证或登录'
_SAY_WALL = '这一页的正文挡在登录或订阅后面'
#: ⑥**内容本身确认不了**:交出来的文字通篇是同一段在复读。这一句和上面三句不同
#: ——它不说「对方拦着」,只说「我没能确认哪一段是这一篇的正文」,因为确实可能是
#: 对方的模板怪、也可能是一道我们认不出的墙。**判不准一律扣留**(元规则)。
_SAY_THIN = '这一页交出来的文字通篇在复读同一段，看不出哪一段是正文'

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
#: 作者。**人名在前、站点名在后,而且 JSON-LD 的作者插在两者中间** ——
#: 一篇文章的作者比「这是哪个站」更具体,而同时给了 `og:site_name` 和
#: Article JSON-LD 作者的页面很常见:两个名单合成一张表的话,站点名会在
#: JSON-LD 还没问过的时候就先把位置占掉,已知的真实作者被丢掉。
_AUTHOR_META = ('meta[name="author"]', 'meta[property="article:author"]',
                'meta[name="twitter:creator"]')
#: 兜底:实在找不到人名时,「这是哪个站」比空着强。
_SITE_META = ('meta[property="og:site_name"]', 'meta[name="application-name"]')
_TITLE_META = ('meta[property="og:title"]', 'meta[name="twitter:title"]',
               'meta[name="title"]')
#: ISO 8601 的那一截日期时间。**认得出形状之后还要过一道 `_real_clock`** ——
#: `2026-99-99` 这种它照样匹配。时区的换算在 `as_published` 自己那儿。
_ISO = re.compile(r'(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?')


class PageUnavailable(ValueError):
    """这一页读不出可保存的内容。消息直接面向用户,说清下一步。"""


def _page_title(soup) -> str:
    """这一页**自称**的标题:`<title>` 和 og/twitter 那几个一起算。

    **两处都要看,而且不回落到正文。** 关卡页一定有 `<title>`(浏览器标签上那行
    字),但有些站点的关卡壳子只给 og —— 判据宁可宽:这一道多认一次的代价是一
    句话,而错存不可恢复。反过来,`page_meta` 的回落链(正文第一行 → 站点名)在
    这里**不能用**:那等于拿正文冒充标题,③ 就退化成"正文里有这句话"。
    候选拼成一串交出去:判据是子串命中,拼起来不影响结论。
    """
    parts = [_first_meta(soup, _TITLE_META)]
    if tag := soup.select_one('title'):
        parts.append(oneline(tag.get_text(' ', strip=True), 'text'))
    return ' | '.join(part for part in parts if part)


def _gate_share(body: str) -> float:
    """正文里「这一句就是关卡提示」的句子占多少。没正文算 0。"""
    sentences = [s.strip() for s in _SENTENCE.findall(body or '') if s.strip()]
    if not sentences:
        return 0.0
    hits = sum(1 for s in sentences if any(p in s.lower() for p in _GATE_PHRASES))
    return hits / len(sentences)


#: 只有这几种 `type` 的 `<script>` 才会被浏览器**执行**(缺省也算)。
#: `application/json`、`application/ld+json`、`text/template`、`text/x-tmpl` 那些是
#: **数据和模板**:里面出现 `_cf_chl_opt` 只是一个示例参数,不是这一页挂着关卡。
#: 事故形态(2026-10-16 审查实测):一篇讲参数解析的 650 字教程,正文里带一段
#: `<script type="application/json">{"sample":"_cf_chl_opt"}</script>` —— 整篇被拒。
_EXEC_TYPES = ('', 'text/javascript', 'application/javascript', 'module',
               'text/ecmascript', 'application/ecmascript', 'text/jscript')


def _inert(node) -> bool:
    """这个节点**还没生效** —— 它待在 `<template>` 里。

    `<template>` 的内容是给 JS 以后克隆用的样板,浏览器既不渲染也不提交它。
    bs4 把它当普通子树解析,于是 `select('input[type=password]')` 照样命中 ——
    一篇「手把手教你写登录表单」的教程把 demo 放在 `<template>` 里就被判成登录墙
    (2026-10-16 审查实测:650 字正常教程被拒)。**DOM 里有某个片段不等于它在
    阻挡正文**,所以生效性要和位置(`readable.in_furniture`)分开各判一次。
    """
    return node.find_parent('template') is not None


def body_confirmed(body: str) -> bool:
    """这段正文**确认得了**吗 —— 「它说了足够多不重复的东西」。

    **口径只有一份**,两处消费:抓到一份新正文时(`gate_reason` 的第六道)、
    以及重复投递时拿**库里那一份**重算一次(`grab.content_owing` →
    受控重抓与条件替换)。各写一遍的话,入库那一侧挡住了的形状在恢复那一侧
    认不出来,错存的内容就永远没人去换。

    判据是 `readable.substance(body) >= readable.MIN_CHARS`:**不重复的**字数要
    过得了提取器那道门槛。为什么是这一道而不是又一张措辞表 —— 见
    `readable.substance` 和 `_SAY_THIN`:登录提示页每家的说法都不同,扩词表是
    逐个堵组合;而「通篇复读同一段」是**内容自己的**事实。

    **它不保证这一篇就是目标文章**(结构上和文章不可分的纯文字提示页仍可能
    过关),所以它不是「确认完成」,只是「这一次别固化终态」那一道的判据;
    剩下的那一半由重复投递时的重抓与条件替换兜(`grab.replace`)。
    """
    return readable.substance(body or '') >= readable.MIN_CHARS


def _gate_assets(soup) -> bool:
    """这一页**真挂着**某家关卡自己的组件吗(见 `_GATE_ASSETS`)。

    只看会让浏览器真去加载或提交的那几个属性,外加 `<script>` 自己的内联代码 ——
    **页面上的文字一个字都不算**:一篇讲关卡的教程会把那段 `<script src=…>` 原样
    转义印在 `<pre>` 里,而那是内容,不是身份(第 3 轮审查实测的误拒正是它)。
    """
    for tag, attr in _ASSET_ATTRS:
        for node in soup.find_all(tag):
            value = str(node.get(attr) or '').lower()
            if value and any(mark in value for mark in _GATE_ASSETS) and not _inert(node):
                return True
    for node in soup.find_all('script'):
        if node.get('src') or _inert(node):
            continue
        # **只有会被执行的那些才算挂着**(`_EXEC_TYPES`):数据块和模板块里
        # 出现那几个串是内容,不是身份。
        if str(node.get('type') or '').strip().lower() not in _EXEC_TYPES:
            continue
        if any(mark in (node.string or '').lower() for mark in _GATE_ASSETS):
            return True
    return False


def _wall_reason(soup) -> str:
    """正文是不是挡在一道登录/订阅墙后面(判据和代价见 `_WALL_FREE_KEYS` 那一节)。"""
    for node in soup.select('input[type=password]'):
        form = node.find_parent('form')
        if form is None or _inert(node) or readable.in_furniture(form):
            continue                     # 还没生效 / 长在页面家具里:都不是拦在正文上
        return _SAY_WALL
    free = _ld_json(soup)
    for key in _WALL_FREE_KEYS:
        if key in free:
            value = free[key]
            if value is False or (isinstance(value, str)
                                  and value.strip().lower() in ('false', 'no')):
                return _SAY_WALL
    return ''


def gate_reason(final_url: str, html: str, body: str = '') -> str:
    """这一次响应是不是一道关卡 —— 验证页、登录墙、付费墙。空串 = 不是,其余是
    给用户的那半句话。

    **不变量:已经认出是关卡的响应,既不许落库、也不许被说成「这一页没找到
    正文」。** 所以成功和失败两条路共用这一道 —— 调用方只有一个调用点,而且排
    在「拿 `body` 做任何事」之前(`grab._grab_web`)。「提取器交出了一段长文字」
    证明不了手里这篇是目标文章:关卡页和登录墙自己也有文字,够长就一路过关,
    然后被 `put_items` 的 `DO NOTHING` 永久钉成这一篇的正文(三轮审查各实测到
    至少一例)。

    判据分两类,一条成立就算(每张表各自的理由和误拒代价写在它们自己头上):

    **A. 对方拦着你**(说得出下一步):①路由 `_GATE_ROUTES`;②DOM 里**真挂着且
    已生效**的关卡组件 `_gate_assets`;③标题是一句关卡提示 `_GATE_TITLES`
    **且**正文里也在说同一件事;④正文通篇就是那几句提示(`_GATE_SHARE`,
    **按份额、不按字数**);⑤一道**生效且拦在正文上**的登录/订阅墙
    `_wall_reason`(**结构,不是措辞**)。

    **B. 我确认不了**:⑥内容本身过不了质量那一道(`body_confirmed`)。
    **这一类是本道判据的底**:前五道是「认出了对方的拦截」,而「没认出拦截」
    证明不了「拿到了正文」—— 三轮审查各实测到至少一例漏放,最后那一例
    (1832 字、一个表单都没有的登录提示页)把前五道全绕过去了。

    **生效性和位置要分开各判一次**:`<template>` 里的密码框、
    `type="application/json"` 里的示例参数都**不算**这一页挂着什么
    (`_inert`);`nav`/页眉页脚侧栏里的登录框不算拦在正文上
    (`readable.in_furniture`)。DOM 里有某个片段 ≠ 它在阻挡正文。

    `body` 空 = 提取器什么都没交出来。那时 ③ 不再要正文佐证,另外还加一道宽词
    表(`_GATE_WORDS`):已经没有正文可言了,宁可多认一道关卡也别把人留在
    「这一页没找到正文」上 —— 那句话指不出下一步。
    """
    parts = urlsplit((final_url or '').lower())
    route = parts.path + (f'?{parts.query}' if parts.query else '')
    if any(mark in route for mark in _GATE_ROUTES):
        return _SAY_ROUTE
    # **一份 soup 走完所有 DOM 判据**:这一页最多解析一次(标题、组件、登录墙
    # 问的是同一棵树),别为每个判据各解析一遍。
    soup = BeautifulSoup((html or '')[:readable.MAX_HTML], 'html.parser')
    if _gate_assets(soup):
        return _SAY_ROUTE
    if reason := _wall_reason(soup):
        return reason
    titled = any(word in _page_title(soup).lower() for word in _GATE_TITLES)
    share = _gate_share(body)
    if share >= _GATE_SHARE:
        return _SAY_PAGE
    if titled and (share > 0 or not body):
        return _SAY_PAGE
    if not body and any(word in (html or '')[:200000].lower() for word in _GATE_WORDS):
        return _SAY_PAGE
    # ⑥**最后一道:没命中前面那五道,不等于手里这篇就是目标正文。** 放在最后是
    # 因为前五道说得出「对方拦着你」,这一道只说得出「我确认不了」—— 能说清的
    # 那句话优先。
    if body and not body_confirmed(body):
        return _SAY_THIN
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


def _real_clock(day: str, clock: str | None) -> bool:
    """`2026-10-15` / `18:02` 这两截是不是真存在的日期和时刻。"""
    try:
        datetime.strptime(f'{day} {clock}' if clock else day,
                          '%Y-%m-%d %H:%M' if clock else '%Y-%m-%d')
    except ValueError:
        return False
    return True


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
    if not m or not _real_clock(m.group(1), m.group(2)):
        # **形状对不等于是个日期**:`2026-99-99`、`2026-02-30`、`10:77` 都过得了
        # 那个正则。放过去的话这个假时刻会一路进列表排序和日报的时间窗口(两处
        # 都按 `published` 做字符串比较),而「没有发布时间」是一句真话。
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
    try:
        return dt.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M')
    except (OverflowError, OSError, ValueError):
        # 日期本身合法、换算时却溢出(`9999-12-31T23:30:00Z` 加 8 小时就出了
        # `datetime` 的头):**别让一个补充字段带掉整条收录** —— 承重的是正文,
        # 而「没有发布时间」是一句真话(口径同上面认不出就留空那一档)。
        return ''


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
    author = (_first_meta(soup, _AUTHOR_META) or _ld_author(node)
              or _first_meta(soup, _SITE_META))
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
