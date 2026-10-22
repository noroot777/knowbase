# -*- coding: utf-8 -*-
"""手动收录的网页那一侧:一个地址 → 标题、作者、发布时间、正文。

**和 `kb.feeds.fulltext` 的分工**:那一条是「订阅给了壳,去原文补正文」,正文
之外的字段(标题、作者、时间)它一概不碰 —— feed 已经给过了。手动收录这一侧
**什么都没有**,所以这里多出一层元信息解析。正文提取本身仍然只有一份口径
(`readable.extract_or_reason`),这个文件一个字都不重写它。

**「这一页能不能被确认为正文并写进库」那一道两条链路共用**(`admit`:提取 +
判据,连先后顺序一起)—— 补抓那一侧也 import 它。判据住在这个文件里只是因为
它的那几张表和 `_wall_reason`、`body_confirmed` 都在这儿;**它不是「手动收录
专用」**,各自在自己那头拼一遍的后果见 `admit` 的说明。
**结论分三档,不是「收 / 不收」两档**(`Admission`:确认为正文 / 确定关卡 /
证据不足)。把后两档合成一档就是「没认出关卡 ⇒ 这就是正文」那个洞:一张分段
的拒绝说明页因此被永久写成正文,而一篇提到一次强措辞的正常教程因此被当成
「对方拦着你」耗尽重试额度(2026-10-22 审查第 1 轮两侧俱在)。

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
from typing import NamedTuple
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
# **关卡判据一个字都没松**(`verdict`):真回了验证页照旧拒,`_GATE_ROUTES`
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
# 是身份证据。**身份证据五样**(响应头/状态码、路由、页面真挂着的关卡组件、
# 生效的登录墙、页面自己的标题),一条成立就算**确定关卡**,`verdict` 挨个问;
# 正文里的字只答得出「证据不足」那一档,理由见 `verdict` 的 A/B 两档。

#: ②**路由**:各家关卡自己的那几条路径/参数,按**最终地址**(`request` 跟完
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
#: ③**页面上真挂着的那套关卡组件**。这是页面身份:一篇讲 Cloudflare 的文章不会
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
#: ⑤**标题自己就是一句关卡提示**。全是**祈使句或状态播报** —— 正经文章的标题不
#: 这么写,所以「人机验证是怎么坏掉的」这种不在射程内。反过来说:`验证码`、
#: `captcha`、`challenge` 这类**话题词**一个都不许进这张表。
#: 标题单独不算数,要配上正文里也在说同一件事(见 `verdict` 的 ⑤)。
_GATE_TITLES = ('just a moment', 'attention required', 'access denied',
                'checking your browser', 'verify you are human',
                'are you a robot', 'security check', 'one more step',
                '环境异常', '请完成验证', '完成验证后', '安全验证', '请稍候',
                '请在微信客户端打开')
#: 关卡措辞:**只收强措辞**(`captcha` 这类正常文章里会出现的词不在内)。
#: ⑤ 用它确认「正文里也在说这件事」,⑥ 用它算份额。
_GATE_PHRASES = ('环境异常', '完成验证', '请在微信客户端打开', 'enable javascript',
                 'verify you are human', 'checking your browser',
                 'just a moment', 'attention required')
#: ⑥**通篇就是那几句提示**:命中的句子占了正文三分之二以上。**这一道的结论是
#: 「证据不足」,不是「确定关卡」** —— 它读的是正文里的字,而字答得出的只有
#: 「这一页在说什么」,答不出「对方有没有把请求拦下来」(那一问归 ①
#: `page_gate.response_reason`)。一篇正经讲拦截页的文章就是靠这个区分活下来的。
#: **按份额算、不设字数上限** —— 上限就是上一轮那个洞:页面长一点就一路放行。
#: 实测(2026-10-16):审查那张 1782 字的验证页 0.50(它另有一句关卡标题);一篇
#: 正经讲拦截页的文章反复引那几句也只到 0.50(`tests/test_grab.py` 拿它钉着这条
#: 线的另一侧),真实的 Cloudflare 文档是 0。
#: **不许用文字量、去重长度或段落形态去给它开豁免** —— 那三样只证明得了「这里有
#: 多少字」,证明不了「这一页没在拦正文」:一张 491 字、句句不同的纯文字拒绝说明页
#: 在那种豁免下会被当成正文写进库、送去翻译、导出成文章。那一类由 ⑦ `_REFUSAL`
#: 认,也落在「证据不足」这一档。
_GATE_SHARE = 0.66
#: 句界。**CJK 句读不要求后面跟空白**(中文正文的句子之间没有空格),**英文句末
#: 标点要求后面跟空白** —— 后者是为了让域名、缩写和小数点(`example.org`、
#: `v1.2`)不把一句话切成好几截。
#: **这一道原先刻意不切 `. `,那是上一版那个缺陷的根**:英文正文没有 CJK 句读,
#: 于是「一个自然段」被当成「一句」,正文里提一次「enable JavaScript」份额就是
#: 1.0 —— 同一批文字连成一段是 1.0、每段各提一次仍是 1.0、按句号分成七段降到
#: 1/7,**结论随排版翻转**。两侧都实测到过:683 字的正常设置教程被拒三轮、永久
#: 退回摘要;而把一张纯文字拒绝说明页的六句各自分段,份额 1.0 → 1/6、当时那六道
#: 一道都不命中,496 字「访问被拒绝」当正文入库(2026-10-22 审查第 1 轮)。
#: **切了 `. ` 之后份额与排版无关**(实测:拒绝说明页原样/分段都是 0.167,设置
#: 教程原样/分段都是 0.143),代价是那张英文验证页从 0.667 掉到 0.500 落到线下
#: —— 它由 ③(标题「请完成验证后继续」)接住,而那本来就是比文字份额硬的一道。
#: **也别反过来把 `\n` 去掉**:那会让整篇算一句、份额二值化,误拒面比原来更大。
_SENTENCE = re.compile(r'(?<=[。！？；])\s*|(?<=[.!?;])\s+|\n+')
#: ⑦**这一页是一张「访问被拒绝」的说明**。口径和理由同 `readable._LEGAL`(版权区
#: 那张表):**小而极稳,而且按占比算不按命中次数** —— 一篇讲 WAF 的文章引一两句
#: 这种话是常态,按次数算会把那一整类技术文章毙掉;而一张真的拒绝说明页**通篇**
#: 就是这几句。
#: **进这张表的必须是「这一次请求被拒了」那句状态播报本身,不是话题词。**
#: `denied`、`security policy`、`captcha` 这类单词一个都不许进(⑤ 明令禁止的
#: 「话题词进表」):讲这些事的正常文章里它们到处都是。整句的样板措辞不一样 ——
#: 正常文章写的是「a managed rule can block requests」,拒绝页写的是
#: 「your request was blocked by the security policy configured for this website」。
#: **这一道单独不足以认定关卡,所以它的结论也是「证据不足」**:扣留,不写库,也
#: 不说「对方拦着你」。误判一次的代价因此是一次重抓加一条待人看的记录,不是把
#: 一篇正常正文永久换成一段拒绝说明。
#: 实测两侧(2026-10-22,`tests/test_fulltext_admit.py` 钉着):那张纯文字拒绝
#: 说明页原样和六句分段都是 **0.833**(与排版无关);而一篇**真·讲拦截页**、正文里
#: 引了「your request was blocked」和「If you think this is a mistake, contact the
#: site administrator」两整句的英文技术文章是 **0.322**,照旧收下。其余被钉住的
#: 正常样本(两篇设置教程、讲 captcha 历史的长文、引一句提示的文章、普通文章、
#: 两份 Markdown)**全部 0**。
_REFUSAL = re.compile(
    r'(?:access to (?:this|the) \S{0,24}\s*(?:has been|is|was) denied'
    r'|(?:your |the )?(?:request|access) (?:was|has been|is) blocked'
    r'|blocked by the security policy'
    r'|enable javascript and cookies'
    r'|owner of this (?:site|website|domain)'
    r'|contact the (?:site |website )?(?:administrator|owner|admin)'
    r"|if you (?:think|believe) (?:this is|you have been|you've been|there has been)"
    r'|been blocked in error'
    r'|ray id'
    r'|访问(?:被|已被)(?:拒绝|拦截|限制)'
    r'|您的(?:请求|访问)(?:被|已被)'
    r')', re.I)
_REFUSAL_SHARE = 0.5
#: 提取不出正文时才用的宽词表:那时已经没有正文可言,宁可多认一道关卡 ——
#: 这一档多拒的代价是一句话,而错存的代价不可恢复。
_GATE_WORDS = _GATE_PHRASES + ('captcha', '滑块验证', 'cf-browser-verification')
#: ④**正文挡在一道登录/订阅墙后面**。判据也是身份,两样:
#: ①DOM 里有一道**拦在正文上**的登录表单(`form` 里有 `input[type=password]`,
#:   而且它不待在页眉页脚侧栏那类外围部件里 —— 位置判据借正文提取自己那一份
#:   `readable.in_furniture`,不另写一张噪声名单);
#: ②页面自己用 schema.org 声明了内容不免费(`isAccessibleForFree: false`,
#:   那是付费墙的标准产者签名,Google 的付费墙规范要求的就是它)。
#: **不靠措辞。** 事故形态(2026-10-16 第 3 轮审查实测):一张普通的
#: 「Sign in to continue reading / subscribe to unlock this story」登录页,提取器
#: 交出 1832 字 —— 份额判据(⑥)对它压根不成立(那一页话很多),措辞也不在
#: `_GATE_PHRASES` 里,于是它被当成正文落库,`DO NOTHING` 之下永久改不回来。
#: 扩词表治不了这一类:墙前面那段话每家写法都不同。
#: **但结构也不够**:复审拿原始证据又打回来一次 —— 那张页面**连 `<form>` 都没有**,
#: 纯文字写着「登录后继续阅读」。所以这一道只管「认得出的墙」,认不出的那些交给
#: ⑧(`body_confirmed`:内容自己说了多少不重复的东西)。
#: 代价如实记着:一篇**在正文区渲染了真实、已生效的密码框**的文章(手把手教你写
#: 登录表单、demo 不放在 `<template>` 里)会被误拒。两个方向差着一个量级 ——
#: 误拒是这一篇得自己复制正文,错存是永久钉死一段不是正文的文字。
_WALL_FREE_KEYS = ('isAccessibleForFree', 'isaccessibleforfree')
#: **「确定关卡」那三句。** 地址/组件那一档和正文那一档对用户的意思不同(前者连
#: 页面都不是我们要的,后者是这一页自己说要先过验证),所以分开写;**再往下分没有
#: 意义** —— 用户能做的事一模一样。响应自己带着关卡标记那一档的话术住在
#: `page_gate.SAY_REPLY`(`fetching.request` 对 4xx/5xx 那句话也用它,口径只有一份)。
_SAY_ROUTE = '对方把这个地址挡在了一道验证页后面'
_SAY_PAGE = '这一页要求先完成验证或登录'
_SAY_WALL = '这一页的正文挡在登录或订阅后面'
#: **「证据不足」那三句,一律以「看不出哪一段是正文」收尾。** 这一档说的不是
#: 「对方拦着你」而是「我没能确认这一页哪一段是这一篇的正文」——「对方拦着」是
#: 一句关于别人的事实断言,拿一段文字的形状去下那个判断就是在说一件自己并不知道
#: 的事(那张讲拦截页的技术文章要的正是这个区分)。而**写库那一下两档一样严**:
#: 都不写。差别在落地 —— 确定关卡沿用有限重试那套语义,证据不足另外留一个
#: 「等人看一眼」的待办(`fulltext.held` / `fulltext.release`)。
_SAY_SHARE = '这一页通篇在说「请先完成验证」那几句，看不出哪一段是正文'
_SAY_DENIED = '这一页看着是一张「访问被拒绝」的说明，看不出哪一段是正文'
#: ⑧**内容本身确认不了**:交出来的文字通篇是同一段在复读。这一句和上面几句不同
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


def _sentences(body: str) -> list[str]:
    """按 `_SENTENCE` 把正文切成句子。**口径只有一份**,⑥⑦ 两道共用。"""
    return [s.strip() for s in _SENTENCE.split(body or '') if s and s.strip()]


def _gate_share(body: str) -> float:
    """⑥ 正文里「这一句就是关卡提示」的句子占多少(**按句数**)。没正文算 0。

    **按句数而不是按字数,这一条是量过的。** 换成字数占比之后,基线钉着必须放行的
    那篇「正经讲 captcha 的文章」从 0.50 涨到 **0.681** —— 它命中的那几句刚好比不
    命中的长,越过 `_GATE_SHARE` 当场误拒。⑦ 那一道反过来按字数算,理由在它自己
    头上(两道问的不是同一件事:这一道问「有几句在说别的」,那一道问「样板占了
    多少篇幅」)。
    """
    sentences = _sentences(body)
    if not sentences:
        return 0.0
    hits = sum(1 for s in sentences if any(p in s.lower() for p in _GATE_PHRASES))
    return hits / len(sentences)


def _refusal_share(body: str) -> float:
    """⑦ 正文里「拒绝说明样板」占了多少篇幅(**按字数**)。没正文算 0。

    **按字数占比,口径同 `readable.boilerplate_reason` 的版权区那一档** ——
    两道问的是同一形状的问题:「这一页是不是通篇就是这类样板」。按**命中次数**算
    会把一篇引过一两句的技术文章一起毙掉(那一篇实测 2 句命中、占比 0.322),而
    一张真的拒绝说明页是 0.833。
    """
    sentences = _sentences(body)
    total = sum(len(s) for s in sentences)
    if not total:
        return 0.0
    return sum(len(s) for s in sentences if _REFUSAL.search(s)) / total



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

    **口径只有一份**,两处消费:抓到一份新正文时(`verdict` 的 ⑧)、
    以及重复投递时拿**库里那一份**重算一次(`grab.content_owing` →
    受控重抓与条件替换)。各写一遍的话,入库那一侧挡住了的形状在恢复那一侧
    认不出来,错存的内容就永远没人去换。

    判据是 `readable.substance(body) >= readable.MIN_CHARS`:**不重复的**字数要
    过得了提取器那道门槛。为什么是这一道而不是又一张措辞表 —— 见
    `readable.substance` 和 `_SAY_THIN`:登录提示页每家的说法都不同,扩词表是
    逐个堵组合;而「通篇复读同一段」是**内容自己的**事实。

    **它不保证这一篇就是目标文章**(结构上和文章不可分的纯文字提示页仍可能
    过关 —— 五段各不相同的会员说明实测过得了全部八道),所以它不是「确认完成」,
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


def verdict(final_url: str, html: str, body: str = '', *,
            headers: dict | None = None) -> tuple[str, str]:
    """这一次响应能不能被确认为正文 —— 返回 `(确定关卡那半句, 证据不足那半句)`,
    最多一个非空;两个都空 = 没有任何一道判据拦着它。

    **不变量:已经认出拦不住正文的响应,既不许落库、也不许被说成「这一页没找到
    正文」。** 所以成功和失败两条路共用这一道 —— 每条写库链路**只有一个调用点**,
    而且排在「拿 `body` 做任何事」之前;那个顺序本身也只有一份,在 `admit` 那儿
    (手动收录和订阅正文补抓都过它)。「提取器交出了一段长文字」证明不了手里这篇
    是目标文章:关卡页和登录墙自己也有文字,够长就一路过关,然后被 `put_items` 的
    `DO NOTHING` 永久钉成这一篇的正文(三轮审查各实测到至少一例)。

    **判据分三档,不是两档 —— 「没认出关卡」和「确认是正文」是两件事。**
    把它们合成一件就是上一版那两个洞的共同根:份额未达线被直接当成「已经拿到
    正文」,于是一张分段的拒绝说明页写进了库;而份额达线被直接当成「对方拦着
    你」,于是一篇提到一次强措辞的正常教程被三轮耗尽额度(2026-10-22 审查第 1 轮
    两侧俱在)。

    **A. 确定关卡**(说得出「对方拦着你」,也说得出下一步):
    ①**这一次响应自己的身份** —— 关卡**决策**响应头(`page_gate.response_reason`;
    **这是唯一一道不靠页面文字的判据**。状态码一个都不算,理由在它自己头上);
    ②路由 `_GATE_ROUTES`(按最终地址比);③DOM 里**真挂着且已生效**的关卡组件
    `_gate_assets`;④一道**生效且拦在正文上**的登录/订阅墙 `_wall_reason`
    (**结构,不是措辞**);⑤标题自己是一句关卡提示 `_GATE_TITLES` **且**正文里
    也在说同一件事(`body` 空时不要正文佐证,另加一道宽词表 `_GATE_WORDS`)。
    这几道问的都是**这一次响应/这一页自己的身份**,不是正文里的字说了什么。

    **B. 证据不足**(只说得出「我没能确认这一页哪一段是这一篇的正文」):
    ⑥正文通篇就是那几句提示(`_GATE_SHARE`,**按份额、不按字数**);⑦正文是一张
    「访问被拒绝」的样板说明(`_REFUSAL`,**按占比、不按命中次数**);⑧内容本身
    过不了质量那一道(`body_confirmed`)。
    **这一档是整道判据的底**:没命中 A 证明不了「拿到了正文」—— 三轮审查各实测到
    至少一例漏放,其中一例(1832 字、一个表单都没有的登录提示页)把 A 全绕过去了。
    **而它一个字都不许冒充 A**:⑥⑦⑧ 读的都是正文里的字,而字答得出的只有「这一页
    在说什么」。一篇**真·讲拦截页**的技术文章会把那几句样板原样引进正文(实测
    `_REFUSAL` 占比 0.322、`_gate_share` 0.50),它和一张真的拒绝说明页在**文字上
    不可分离** —— 分得开它们的只有 ① 那一道。所以这三道的结论是扣留而不是判决:
    不写库(和 A 一样严),但不说「对方拦着你」,而且在补抓那一侧另留一个
    「等人看一眼」的待办而不是静默退回摘要(`fulltext.held` / `fulltext.release`)。

    **这八道一道都不往外搬,存量复查只取 `readable.boilerplate_reason`。** 两次搬
    过去都搬出了事故:⑧ 判坏了正常的 Markdown 原文(它允许重复),⑥ 判坏了正常的
    英文教程(那一版的句界不切 `. `,一整段只算一句)。更根本的理由是**落库之后
    身份证据就没了**:① 要的响应头、②③④ 要的地址和 DOM,库里那一列一个都没存。
    **误拒的代价按路分三档,一档都不轻**:手动收录那边用户当场看得见、能点「重新抓
    一次」;补抓那边没人在看、三次额度用完就永久退回摘要;存量复查那边是把已经在
    库里的正文换成壳、译文作废。前两条是「这一次没拿到」,第三条是「把拿到的毁掉」
    —— 同一个启发式担不起这三种后果,所以不共用。完整论证在
    `docs/decisions/fulltext.md` 第一节。

    **生效性和位置要分开各判一次**:`<template>` 里的密码框、
    `type="application/json"` 里的示例参数都**不算**这一页挂着什么
    (`_live`);`nav`/页眉页脚侧栏里的登录框不算拦在正文上
    (`readable.in_furniture`)。DOM 里有某个片段 ≠ 它在阻挡正文。

    `headers` 缺省 `None` = 调用方没有这次响应(只有一份 HTML)。
    **那时 ① 整道不成立,而且不许拿「没有响应证据」当成「这不是关卡」** ——
    它只是少了一道判据,余下七道照旧问。

    **这一道在这里是冗余带,不是唯一一道。** 真正拦住带关卡头的响应是
    `fetching.request`(认出来抛 `Gated`,排在任何格式分派之前)—— 那一层才覆盖
    得到协商 Markdown 和专用原文抓取那两个成功出口。这里留着是因为「谁交进来一份
    HTML 加一份响应头」这件事不该假设它一定经过了那一层(浏览器通道、测试夹具、
    将来的别的传输)。**两处同一张表、同一句话**(`page_gate`),不是两份判据。
    """
    if reason := page_gate.response_reason(headers):
        return reason, ''
    parts = urlsplit((final_url or '').lower())
    route = parts.path + (f'?{parts.query}' if parts.query else '')
    if any(mark in route for mark in _GATE_ROUTES):
        return _SAY_ROUTE, ''
    # **一份 soup 走完所有 DOM 判据**:这一页最多解析一次(标题、组件、登录墙
    # 问的是同一棵树),别为每个判据各解析一遍。
    # **要不要截断按「真标记」算,不按整份长度**(口径同 `readable.markup_size`):
    # 按整份长度截的那一版里,一张内联脚本 3.57 MB 打头的页面(绿泡泡的文章页就是
    # 这个形状)会让组件和登录墙这两道只看到开头那一截 —— 而这两道**漏一次就是把
    # 一道墙当正文永久钉进库里**(`put_items` 的 `DO NOTHING`),代价和误拒差着一个
    # 量级。真标记自己就超线的那种页面提取器反正交不出正文,截断只是别在它身上耗
    # 时间。
    # **也只在整份超线时才去量**(短路顺序同 `extract_or_reason`):审查第一轮
    # 这里是无条件调用的,于是每一份小页面都白扫一趟,而那一版的扫描还是平方级的。
    text = html or ''
    if len(text) > readable.MAX_HTML and readable.markup_size(text) > readable.MAX_HTML:
        text = text[:readable.MAX_HTML]
    soup = BeautifulSoup(text, 'html.parser')
    if _gate_assets(soup):
        return _SAY_ROUTE, ''
    if reason := _wall_reason(soup):
        return reason, ''
    titled = any(word in _page_title(soup).lower() for word in _GATE_TITLES)
    share = _gate_share(body)
    if titled and (share > 0 or not body):
        return _SAY_PAGE, ''
    if not body and any(word in (html or '')[:200000].lower() for word in _GATE_WORDS):
        return _SAY_PAGE, ''
    # ── 以下三道只说得出「我确认不了」。**顺序:先说得出「它像什么」的,
    # 再说「它什么都不像」。**
    if share >= _GATE_SHARE:
        return '', _SAY_SHARE
    if _refusal_share(body) >= _REFUSAL_SHARE:
        return '', _SAY_DENIED
    # ⑧**最后一道:没命中前面那七道,不等于手里这篇就是目标正文。**
    # **`body` 空的时候它压根不成立**:那时提取器自己给得出原因(版权区、作者
    # 简介),而「通篇在复读同一段」对着一份空正文是在说一件没发生的事(漏掉这道
    # 守卫当场实测到 `tests/test_fulltext_failures.py` 5 条变红)。
    if body and not body_confirmed(body):
        return '', _SAY_THIN
    return '', ''


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


class Admission(NamedTuple):
    """`admit` 的结论。**四个字段里最多一个非空**(`why` 是 `unsure` 的附注)。

    - `body` 非空 = **确认为正文**,可以写库(调用方那道 `limit` 已经生效)。
    - `gate` 非空 = **确定关卡**:对方拦着你,说得出下一步。
    - `unsure` 非空 = **证据不足**:没认出谁在拦,也确认不了这是正文 —— 一律不写
      库。补抓那一侧另留一个「等人看一眼」的待办(`fulltext.held`),不静默退回摘要。
    - `why` = 「读不懂」的那半句(样板区那一档,`readable.extract_or_reason` 给),
      只在 `unsure` 也空、`body` 也空时才有意义。

    **三个判决字段分开而不是一个「拒绝原因」**:两条链路的落地按档不同(见
    `verdict` 的 A/B 两档),合成一个字段的话调用方只能去匹配话术 —— 那就是第二份
    口径。四字段都空 = 这一页读不懂而且连判据都没有(付费墙、纯 JS 渲染、整页一张
    图),调用方按通用文案处理。
    """
    body: str
    gate: str
    unsure: str
    why: str


def admit(final_url: str, html: str, limit: int | None = None, *,
          headers: dict | None = None) -> Admission:
    """这一页**能不能被确认为这一篇的正文、写进库里**。见 `Admission`。

    **两条写库链路共用这一道**:手动收录(`grab._grab_web`)和订阅正文补抓
    (`fulltext.fetch_one`)。各自在自己那头拼一遍的话,其中一头迟早漏掉一道 ——
    这个函数就是那次事故的产物:关卡判据当初只接在手动收录那一头,于是同一张
    Cloudflare 关卡页在手动收录被认了出来、在补抓那条定时链路上被当成正文写进
    `feed_items.body`、写上 `fulltext_at`、**退出重试队列**(审查实测:一张
    1453 字节、一行脚本都没有的关卡页就够,`done=1`)。所以共用的不只是判据,
    **提取和关卡的先后顺序也只有一份**。

    **`status` / `headers` 要一路传进来,不许在调用方自己判一遍。** 两个调用方手
    里都有整个 `Response`,而 ① 那一道(响应自己带着关卡标记)是**唯一一道不靠
    页面文字的判据** —— 把它留在调用方就又回到了「两头各拼一遍、其中一头迟早漏
    掉」那个形状。缺省 `None` = 调用方确实没有响应(只有一份 HTML),那时少问一道
    而不是多放一道。

    **顺序不许反:判据那一问排在「拿 `body` 做任何事」之前。** 「提取器交出了一段
    长文字」证明不了手里这篇是目标文章,而「这一页没找到正文」对着一张明摆着的
    验证页是在少说一件已经知道的事 —— 两头的下一步完全不同(换个地址 / 在浏览器
    里打开一次)。所以这里**先把提取结果收在手里**、让 `verdict` 先答,拦住了就把
    那段文字丢掉,哪怕它长达几千字。

    **调用方那道长度上限在这里面生效(`limit`),不许拿到正文之后再截。**
    判据要落在**最后真写进库的那一份**上:准入之后还改正文,原来那个结论就不再是
    对最终内容的保证。事故形态(审查第 1 轮实测):一张开头把同一段复读了几万字、
    真内容排在 6 万字之后的页面,全文过得了 ⑧(后面那段内容各不相同),手动收录
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
    gate, unsure = verdict(final_url, html, body, headers=headers)
    if gate or unsure:
        return Admission('', gate, unsure, '')
    return Admission(body, '', '', reason)


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
