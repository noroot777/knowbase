# -*- coding: utf-8 -*-
"""手动收录的网页那一侧:一个地址 → 标题、作者、发布时间、正文。

**和 `kb.feeds.fulltext` 的分工**:那一条是「订阅给了壳,去原文补正文」,正文
之外的字段(标题、作者、时间)它一概不碰 —— feed 已经给过了。手动收录这一侧
**什么都没有**,所以这里多出一层元信息解析。正文提取本身仍然只有一份口径
(`readable.extract_or_reason`),这个文件一个字都不重写它。

**「这一页能不能被确认为正文并写进库」那一道两条链路共用**(`admit`:提取 +
关卡,连先后顺序一起)—— 补抓那一侧也 import 它。判据住在这个文件里只是因为
它的那几张表和 `_wall_reason`、`body_confirmed` 都在这儿;**它不是「手动收录
专用」**,各自在自己那头拼一遍的后果见 `admit` 的说明。

**判不准一律拒,不许返回一个空壳**(元规则):`put_items` 是
`ON CONFLICT DO NOTHING`,一条空正文落了库就再也改不回来,而重抓一次的代价
只是再发一次 HTTP。登录墙、验证页、PDF 这几种**各给一句自己的话** —— 它们的
下一步完全不同(换个地址 / 在浏览器里打开一次 / 这不是网页),折叠成「抓取失败」
等于把人留在原地。话术在调用方(`grab._grab_web` 的 `_next_step`、
`fulltext.fetch_pending` 的失败明细),**准入在这里**。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from kb.feeds import page_gate, readable
from kb.feeds.fetching import oneline, plain

# 抓网页不是抓 feed,`fetching.request` 的默认 Accept 是 RSS 优先 —— 有些站会
# 据此回一份 XML 或者干脆 406。口径同 `fulltext._HEADERS`,多一条中文优先。
#
# **手动收录这一条路自报浏览器身份,和采集那一侧分开。** 采集是我们定时去敲
# 别人的门,自报 `KnowledgeBaseFeedReader/1.0`(`fetching.request` 的默认)是
# 该有的礼貌;手动收录是人粘了一条链接正盯着按钮等,要的就是「我在浏览器里打开
# 它会看到什么」。实测证据(2026-10-21,`mp.weixin.qq.com/s/XQq1iCBn1cUhR3SLOOKBMA`,
# 其余请求头一字不改、各 3 次):自报爬虫 3/3 被 302 到
# `/mp/wappoc_appmsgcaptcha`,拿到 18 KB 的验证页;换这个 UA 3/3 直接 200 在原
# 地址上,3.74 MB 的文章页、`#js_content` 4004 字。**那道验证页是我们自报身份
# 招来的,不是这个地址本身挡着** —— 所以这不是「绕过验证」:我们一个
# token 都没解、一个 cookie 都没带,只是不再主动触发它。
# **关卡判据一个字都没松**(`gate_reason`):真回了验证页照旧拒,`_GATE_ROUTES`
# 里 `appmsgcaptcha` / `wappoc_` 那两条正是为它留的。
BROWSER_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')
HEADERS = {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
           'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5',
           'User-Agent': BROWSER_UA}
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
#: og 元信息和 `#js_content` 全都没有。**这一条照旧留着,但它不再是常态** ——
#: 2026-10-21 复测:那道关卡是自报爬虫身份招来的,换 `BROWSER_UA` 之后同一条地址
#: 直接给整篇文章(见模块顶上 `BROWSER_UA` 的注释)。对方哪天收紧风控它就回来了,
#: 而漏一道关卡的代价是把验证页永久钉成正文。
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
#: 标记表和生效性共用 page_gate;浏览器侧 x_page_state 通过 ASSETS_JS 消费同一口径。
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
#: **份额单独不是身份** —— 它成立的前提是「句子是正文的自然单元」,前提不成立
#: 的那一档由 `_mention_only` 兜(事故形态写在那儿)。
_GATE_SHARE = 0.66
#: 句界只按 CJK 标点和换行切。**`. ` 不算**:域名、缩写、小数点会把一句话切成好
#: 几截,份额跟着抖。代价是**一整段英文只算一句**,所以 ④ 还要过 `_mention_only`。
_SENTENCE = re.compile(r'[^\n。！？!?；;]+')
#: `_mention_only` 的第一问:最长的那一句占到正文这个比例,就说明这一段压根没被
#: 切开。线放在**一半**:切得开的页面里最长那句远在它下面(那张 1782 字的验证页
#: 是 0.047),而没切开的那一档必然是 1.0 —— 中间没有要分的东西。
_UNSPLIT_SHARE = 0.5
#: 只在那一档里用的细句界:**英文句号也算**。切碎域名、缩写、小数点在这里是安全
#: 的 —— 它只用来按细句去重、问「剔掉复读之后还剩多少」,切碎只会让剩下的偏少、
#: 偏向拒。**不拿它去算份额**:那正是 `_SENTENCE` 躲开的那个洞(一张英文验证页
#: 按 `. ` 切完,只有两句命中强措辞,份额从 1.0 掉到 0.29 —— 整页就漏放了)。
_FINE_SENTENCE = re.compile(r'[^\n。！？!?；;.]+')
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
#: 扩词表治不了这一类:墙前面那段话每家写法都不同。
#: **但结构也不够**:复审拿原始证据又打回来一次 —— 那张页面**连 `<form>` 都没有**,
#: 纯文字写着「登录后继续阅读」。所以这一道只管「认得出的墙」,认不出的那些交给
#: 第六道(`body_confirmed`:内容自己说了多少不重复的东西)。
#: 代价如实记着:一篇**在正文区渲染了真实、已生效的密码框**的文章(手把手教你写
#: 登录表单、demo 不放在 `<template>` 里)会被误拒。两个方向差着一个量级 ——
#: 误拒是这一篇得自己复制正文,错存是永久钉死一段不是正文的文字。
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


def _fine_substance(body: str) -> int:
    """按**细句**去重之后,这段正文还说得出多少字。

    **这一问不能交给 `body_confirmed`**:那一道按**行**去重
    (`readable.substance`,口径跟 `plain` 的分段绑在一起),而这里讨论的正是
    「整段只有一行」—— `'Please enable JavaScript. ' * 50` 在它眼里是 1300 字
    实打实的内容,而细句去重之后只剩 25 字。
    """
    seen: set[str] = set()
    total = 0
    for piece in _FINE_SENTENCE.findall(body or ''):
        piece = piece.strip()
        if piece and piece not in seen:
            seen.add(piece)
            total += len(piece)
    return total


def _mention_only(body: str) -> bool:
    """④ 的份额达线了,但它说的只是「正文里提到过」,不是「通篇就是那几句提示」。

    **份额不能单独充当身份。** 它成立的前提是「句子是正文的自然单元」,而
    `_SENTENCE` 刻意不按 `. ` 分句 —— 代价是一整段英文只算**一句**:683 字的正常
    设置教程里提一次 `enable JavaScript`,份额就是 100%。事故形态(审查实测):那
    一篇在手动收录那一侧提示「这一页要求先完成验证或登录」,在订阅补正文那一侧
    连抓三轮各扣一次额度,第三轮之后退出补抓队列(`fulltext_at` 始终空),库里和
    vault 里永久只剩 feed 给的那段摘要、还按摘要翻译导出 —— **误拒在那条路上也
    是不可逆的**,没有人在旁边看着它。

    两个条件一起成立才算「只是提到过」:
    ①这一段**压根没被切开**:最长的那一句占了正文一半以上(`_UNSPLIT_SHARE`)。
    ②把它按**细句**去重之后,仍然说得出 `readable.MIN_CHARS` 那么多不重复的东西
      —— 也就是剔掉复读之后这一页还剩一篇文章。

    **第二问是这一道的闸**:只问第一问的话,`'Please enable JavaScript. ' * 50`
    那种一整段的英文验证页也会被当成「提到过」放行,而基线靠份额 1.0 拒得掉它。
    真正的关卡页没有内容可言 —— 它要么切得开(③④ 照旧答)、要么复读(这一问
    答)、要么短(⑥ 答)。

    **知情放掉的那一档如实记着**:一张**长、句句不同、纯文字**的英文关卡说明页
    (「Access denied… Please enable JavaScript and cookies…」那种,491 字、细句去重
    480)在这一道上过得去 —— 文字上它和一篇正常英文长文**不可区分**,这正是上面那
    件事的另一面。那一类交给页面身份:①路由、②组件、③标题,实测这三样各自接得住
    它。不拿措辞去补这一刀:放宽那一侧的误判(真关卡页写死进库)和收紧那一侧的误判
    (正常文章永久退回摘要)**现在两头都不可逆**,而措辞表治不了「每家写法都不同」。

    两侧的回归在 `tests/test_fulltext_admit.py` 与 `tests/test_grab.py`。
    """
    sentences = [s.strip() for s in _SENTENCE.findall(body or '') if s.strip()]
    if not sentences:
        return False
    if len(max(sentences, key=len)) < _UNSPLIT_SHARE * sum(len(s) for s in sentences):
        return False
    return _fine_substance(body) >= readable.MIN_CHARS


#: 只有这几种 `type` 的 `<script>` 才会被浏览器**执行**(缺省也算)。
#: `application/json`、`application/ld+json`、`text/template`、`text/x-tmpl` 那些是
#: **数据和模板**:里面出现 `_cf_chl_opt` 只是一个示例参数,不是这一页挂着关卡。
#: 事故形态(2026-10-16 审查实测):一篇讲参数解析的 650 字教程,正文里带一段
#: `<script type="application/json">{"sample":"_cf_chl_opt"}</script>` —— 整篇被拒。


def _live(node) -> bool:
    """这个节点**此刻真生效**吗 —— 它不在 `<template>` 里,而且(是 `<script>`
    的话)它那个 `type` 浏览器真会执行。

    `<template>` 的内容是给 JS 以后克隆用的样板,浏览器既不渲染也不提交它。
    bs4 把它当普通子树解析,于是 `select('input[type=password]')` 照样命中 ——
    一篇「手把手教你写登录表单」的教程把 demo 放在 `<template>` 里就被判成登录墙
    (2026-10-16 审查实测:650 字正常教程被拒)。**DOM 里有某个片段不等于它在
    阻挡正文**,所以生效性要和位置(`readable.in_furniture`)分开各判一次。

    **两问合一是因为它们是同一个不变量的两端**(元规则「口径只有一份」):
    `<script src=… type="application/json">` 跟内联的数据块一样不会执行,而原先
    只有内联那一支核对了 `type`、带 `src` 的那一支漏着 —— 同一道判据分两处写,
    其中一处迟早漏掉(第 3 轮审查点的正是这处对称缺口)。
    """
    return page_gate.live(node)


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
    过关 —— 五段各不相同的会员说明实测过得了全部六道),所以它不是「确认完成」,
    只是「这一次别固化终态」那一道的判据。**剩下那一半不许再押在这道判据上**:
    它认不出的东西恰恰最需要换,所以恢复另有一个与它无关的入口 —— 人说「重新
    抓一次」就重抓(`GrabRequest.refetch` → `grab.replace`,见 `grab_api._recover`
    的两个入口)。
    """
    return readable.substance(body or '') >= readable.MIN_CHARS


def _gate_assets(soup) -> bool:
    """这一页**真挂着**某家关卡自己的组件吗(见 `page_gate.ASSETS`)。

    只看会让浏览器真去加载或提交的那几个属性,外加 `<script>` 自己的内联代码 ——
    **页面上的文字一个字都不算**:一篇讲关卡的教程会把那段 `<script src=…>` 原样
    转义印在 `<pre>` 里,而那是内容,不是身份(第 3 轮审查实测的误拒正是它)。
    """
    return page_gate.has_assets(soup)


def _wall_reason(soup) -> str:
    """正文是不是挡在一道登录/订阅墙后面(判据和代价见 `_WALL_FREE_KEYS` 那一节)。"""
    for node in soup.select('input[type=password]'):
        form = node.find_parent('form')
        if form is None or not _live(node) or readable.in_furniture(form):
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
    正文」。** 所以成功和失败两条路共用这一道 —— 每条写库链路**只有一个调用点**,
    而且排在「拿 `body` 做任何事」之前;那个顺序本身也只有一份,在 `admit` 那儿
    (手动收录和订阅正文补抓都过它)。「提取器交出了一段长文字」
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

    **这六道一道都不往外搬,存量复查只取 `readable.boilerplate_reason`。** 两次搬
    过去都搬出了事故:⑥ 判坏了正常的 Markdown 原文(它允许重复),④ 判坏了正常的
    英文教程(`_gate_share` 不按 `. ` 分句,一整段只算一句 —— 正文里提一次
    「enable JavaScript」就是 100%;④ 现在要先过 `_mention_only` 那道前提才成立)。
    **误拒的代价按路分三档,一档都不轻**:手动收录那边用户当场看得见、能点「重新抓
    一次」;补抓那边没人在看、三次额度用完就永久退回摘要;存量复查那边是把已经在
    库里的正文换成壳、译文作废。前两条是「这一次没拿到」,第三条是「把拿到的毁掉」
    —— 同一个启发式担不起这三种后果,所以不共用。完整论证在
    `docs/decisions/fulltext.md` 第一节。

    **生效性和位置要分开各判一次**:`<template>` 里的密码框、
    `type="application/json"` 里的示例参数都**不算**这一页挂着什么
    (`_live`);`nav`/页眉页脚侧栏里的登录框不算拦在正文上
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
    # **要不要截断按「真标记」算,不按整份长度**(口径同 `readable.markup_size`):
    # 按整份长度截的那一版里,一张内联脚本 3.57 MB 打头的页面(绿泡泡的文章页就是
    # 这个形状)会让组件和登录墙这两道只看到开头那一截 —— 而这两道**漏一次就是把
    # 一道墙当正文永久钉进库里**(`put_items` 的 `DO NOTHING`),代价和误拒差着一个
    # 量级。真标记自己就超线的那种页面提取器反正交不出正文,截断只是别在它身上耗
    # 时间。
    text = html or ''
    soup = BeautifulSoup(text if readable.markup_size(text) <= readable.MAX_HTML
                         else text[:readable.MAX_HTML], 'html.parser')
    if _gate_assets(soup):
        return _SAY_ROUTE
    if reason := _wall_reason(soup):
        return reason
    titled = any(word in _page_title(soup).lower() for word in _GATE_TITLES)
    share = _gate_share(body)
    if share >= _GATE_SHARE and not _mention_only(body):
        return _SAY_PAGE
    if titled and (share > 0 or not body):
        return _SAY_PAGE
    if not body and any(word in (html or '')[:200000].lower() for word in _GATE_WORDS):
        return _SAY_PAGE
    # ⑥**最后一道:没命中前面那五道,不等于手里这篇就是目标正文。** 放在最后是
    # 因为前五道说得出「对方拦着你」,这一道只说得出「我确认不了」—— 能说清的
    # 那句话优先。**`body` 空的时候它压根不成立**:那时提取器自己给得出原因
    # (版权区、作者简介),而「通篇在复读同一段」对着一份空正文是在说一件没发生
    # 的事(漏掉这道守卫当场实测到 `tests/test_fulltext_failures.py` 5 条变红)。
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


def admit(final_url: str, html: str, limit: int | None = None) -> tuple[str, str, str]:
    """这一页**能不能被确认为这一篇的正文、写进库里**。

    `(正文, 关卡原因, 读不懂的原因)`。正文非空时另两个必为空;三个都空 = 读不懂
    而且没有判据可言(付费墙、纯 JS 渲染、整页一张图 —— 见 `extract_or_reason`)。

    **两条写库链路共用这一道**:手动收录(`grab._grab_web`)和订阅正文补抓
    (`fulltext.fetch_one`)。各自在自己那头拼一遍的话,其中一头迟早漏掉一道 ——
    这个函数就是那次事故的产物:关卡判据当初只接在手动收录那一头,于是同一张
    Cloudflare 关卡页在手动收录被认了出来、在补抓那条定时链路上被当成正文写进
    `feed_items.body`、写上 `fulltext_at`、**退出重试队列**(审查实测:一张
    1453 字节、一行脚本都没有的关卡页就够,`done=1`)。所以共用的不只是判据,
    **提取和关卡的先后顺序也只有一份**。

    **顺序不许反:关卡那一问排在「拿 `body` 做任何事」之前。** 「提取器交出了一段
    长文字」证明不了手里这篇是目标文章,而「这一页没找到正文」对着一张明摆着的
    验证页是在少说一件已经知道的事 —— 两头的下一步完全不同(换个地址 / 在浏览器
    里打开一次)。所以这里**先把提取结果收在手里**、让 `gate_reason` 先答,关卡
    成立就把那段文字丢掉,哪怕它长达几千字。

    **调用方那道长度上限在这里面生效(`limit`),不许拿到正文之后再截。**
    判据要落在**最后真写进库的那一份**上:准入之后还改正文,原来那个结论就不再是
    对最终内容的保证。事故形态(审查第 1 轮实测):一张开头把同一段复读了几万字、
    真内容排在 6 万字之后的页面,全文过得了 ⑥(后面那段内容各不相同),手动收录
    随后截到 `MAX_CHARS` —— 落库那 60000 字**不重复的只有 202 字**,收录当场完成、
    页面紧接着提示「这一篇收下的内容看不出是正文」,而重抓只会截出同一个前缀。
    基线那一版是先截后判、这一页直接拒掉的,所以这是重构带出来的回退。
    `limit=None` = 不截(补抓那一侧原样入库,按 `fulltext.MAX_TRIES` 计次)。

    **话术仍然留给调用方**:手动收录每一档配一句下一步(`grab._next_step`),
    补抓那一侧进面板失败明细。两头的落地不同,**准入相同**。
    """
    body, reason = readable.extract_or_reason(html)
    if limit is not None:
        body = body[:limit]
    if gate := gate_reason(final_url, html, body):
        return '', gate, ''
    return body, '', reason


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
