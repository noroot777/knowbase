# -*- coding: utf-8 -*-
"""X 长文(Article)的正文补全:时间线上只有一张卡片,正文得单独取一次。

**为什么需要这一步**:X 的长文(图文卡片下面带标题和摘要的那种)发出来时,推文
本身**一个字都没有** —— `text` 就是一条指向 `x.com/i/article/<id>` 的 t.co 链接,
X 在界面上把它藏起来、改成渲染一张卡片。于是浏览器通道的 `tweetText` 压根不存在、
feedgrab 那边的正文就是那条光秃秃的链接,两条路入库的都是一条空正文:标题回落成
「某某 的动态」,vault 里那篇 md 没有内容,全文检索搜不到文章里的任何一句话,
提取阶段每轮把它报成「正文为空,需回填」。2026-10-09 实测:近三周 social 的 342
条里 11 条空正文,**9 条是长文**(另两条一条是纯图推文、一条是已删除的推文)。

**取法是 FxTwitter 的公开接口**(`https://api.fxtwitter.com/<handle>/status/<id>`),
它在 `tweet.article.content` 里把整篇文章按 Draft.js 的块结构交出来。选它的三个
理由:

①**不要登录**。X 自己的那条公开接口(`cdn.syndication.twimg.com/tweet-result`)
  只给 `title` + 一段 120 字左右的 `preview_text`,没有正文;而
  `x.com/i/article/<id>` 在未登录的浏览器里是一句「该页面不存在」加登录墙
  (2026-10-09 用临时 profile 的无头 Chrome 实测)。要正文就只有「登录态 + X 内部
  GraphQL + `x-client-transaction-id` 签名」这一条路 —— 那正是 `x_feedgrab` 的模块
  注释里写明不抄进来的那份维护量。FxTwitter(开源的 FixTweet)替我们背着它。
②**四条通道通用**。它只认推文 URL,不认我们这一轮用的是浏览器、cookie 还是桥接,
  所以 `bridge` 那条没有登录态的路也补得上。
③**不开浏览器**。走登录态浏览器读文章页当然也行,但那要在用户自己的浏览器里多开
  一张标签页、等它渲染两三秒,还要押一个 X 改版就会坏的 DOM 选择器;这边是一次
  HTTP、一份结构化 JSON。所以浏览器那条做成**后路**,不做主路。

**它是一个第三方服务,所以有后路**(开源的 FixTweet 跑的公共实例;推文 URL 会发
给它,内容本来就是公开的)。它关停、限流、改接口的那一天,`browser` / `attach`
两条通道手里**正好有一个已经登录的浏览器** —— 直接把文章页读出来即可,见下面
`browser_fetch`。`bridge` / `feedgrab` 没有浏览器,那两条照旧按「补不到就不入库、
不推进水位」办,下一轮重试。

两个开关各管一条路:`KB_X_ARTICLE_API` 设成空串关掉接口(**手里有浏览器就只走
后路** —— 这正是「不想把推文 URL 发给第三方」时要的那一档),`KB_X_ARTICLE_BROWSER`
设成 '0' 关掉后路,两个都关 = 整条退回这一步出现之前。

**取不到的那一条要退出这一轮,而且不推进水位**(`fill` 返回的 note 由两条通道的
`harvest` 并进 `incomplete`)。原因是 `registry.put_items` 是
`ON CONFLICT(key) DO NOTHING` —— 空正文一旦落库就**再也不会被更新**,这一篇长文
从此永久是空的。所以宁可这一轮当它没来过:水位留在原处,下一轮重叠重抓时再试一次
(`put_items` 按 URL 去重,重叠不会产生重复条目)。**只有一种情况不这么办** ——
接口明确答复「这条推文没有长文」(纯图、纯视频推文就是这样):那是一个确定的结论,
按它原样入库,由提取阶段那套「正文为空」的终态收口(见 `kb/extract.py` 的
`body_gap`),不该为它冻住水位。
"""

from __future__ import annotations

import json
import os
import re
import time

from kb.feeds.fetching import FeedError, request
from kb.feeds.x_cdp import CdpError

# 接口地址。设成空串 = 整个关掉这一步(不发请求、不拦任何条目)。
API = os.environ.get('KB_X_ARTICLE_API', 'https://api.fxtwitter.com').rstrip('/')
# 单次请求的墙钟上限。
TIMEOUT = float(os.environ.get('KB_X_ARTICLE_TIMEOUT', '20'))
# 连着几次请求失败就不再试了。防的是"接口整个挂了,而这一轮还有几十条要问",
# 每条各等满 TIMEOUT 会把整轮采集拖垮。失败的那些照旧算没读完(见模块注释)。
_GIVE_UP_AFTER = 3
# 正文最多留多长。最长的真实样本是 3 万字符(2026-10-09 实测),比这再长的多半
# 不是一篇要读的文章;翻译那一侧本来也只送前 12000 字符。
MAX_CHARS = 60000

# 推文 URL 里的 handle 和 id。
_STATUS = re.compile(r'(?:x|twitter)\.com/([A-Za-z0-9_]{1,20})/status/(\d+)')

# 一条 URL 写在推文正文里时,它从哪儿**起**算。下面两条正则都用它,而它们都是拿去
# `sub` 掉的 —— 两条正则里的 `x.com` / `twitter.com` / `t.co` 都只是一段字符,
# **压根不要求它是主机名**,于是别人家的地址里出现这一段就被当成起点,后半截被
# 删掉,而 `put_items` 的 `DO NOTHING` 让这种删除**永久固定**。
#
# 实测被改坏的形态(2026-10-09,每一种都是合法的外部地址):
#   `https://fxtwitter.com/Example/article/10001` → `https://fx`(镜像站,`fx` 后面
#     那一段逐字就是 `twitter.com/<handle>/article/<id>`)
#   `https://notx.com/Example/article/10001`      → `https://not`
#   `https://help.x.com/Example/article/10001`    → `https://help.`(子域名)
#   `https://my-x.com/Example/article/10001`      → `https://my-`
#   `https://example.org/docs/x.com/Example/article/10001` → `https://example.org/docs/`
#     (真实主机名是 `example.org`,`x.com` 只是它路径里的内容)
#   `https://evil.example/?u=http://t.co/aBcD12&next=1` → 查询参数被删掉一截
# 这几条同时还让 `needs_article` 判据① 误判成"要去问一次长文",白跑一次询问。
#
# **判据按候选自己带不带 scheme 分成两档,因为这两档要排掉的东西根本不是一回事。**
# 合成一档(一律禁"URL 内部字符紧贴")的后果实测过:**省略号或波浪号紧贴的真地址
# 被拒**,`needs_article` 三条判据一条都不命中,于是那条链接本身当成正文入库 ——
# `added=1`、`errors=[]`、`partial=[]`、水位照常推进、标题回落成「某某 的动态」,
# 而 `DO NOTHING` 之下重复同步也补不回来(2026-10-09 实测
# `我写了一篇新文章...https://x.com/Example/article/10001` 与 `新文章~…`,两种路径
# 形式各一次,浏览器通道没读到卡片信号时逐字同样)。`.` 和 `~` 在这里有两个互相
# 冲突的身份:在 `help.x.com` / `my-x.com` 里它们是**域名的一部分**,在
# `新文章...<地址>` 里它们是**作者的标点** —— 单看紧贴的那一个字符分不开,而分档
# 之后压根不用分:
#
# ①**自带 scheme 的候选**(`_SCHEME_HEAD` + `_nested`)。`https://` 本身就宣告
#   "一条新 URL 从这里开始",所以域名后缀那一类从定义上就不可能 —— 要排的只有
#   "它嵌在另一条 URL 里",而嵌套只发生在外层 URL 的**结构位置**(查询值、片段、
#   路径段),那几个位置的紧前一个字符必然是 `=` `&` `?` `#` `/` 之一。正文标点
#   (`.` `~` `-` `:` 以及全部中文)一律放行。**但这五个里有两个另算**:见下面
#   `_AMBIGUOUS` 那段 —— `?` 和 `#` 同时还是作者的句读,所以只有 `=` `&` `/` 留在
#   这道定宽后视里。已知留着的一道缝:外层 URL 的路径里用 `.` 紧接一个完整 scheme
#   (`…/path.https://x.com/…`)照旧会被认下来 —— 那不是任何真实写法。
# ②**裸域名的候选**(`_BARE_HEAD`,`x.com/<handle>/article/<id>` 不带 scheme)。
#   这一档的起点就是主机名的第一个字符,所以域名后缀和路径嵌套都要排,禁止集是
#   「URL 内部字符」那一整套(`_URL_CHARS`)—— 它和分档之前那道闸**逐字相同**,
#   分档只是给①松了绑。代价是"标点 + 裸域名"一律被拒 —— `...x.com/…`、
#   `读过这篇吗?x.com/…`、`第三节#x.com/…` 都在里面,**包括下面 `_nested` 那一道
#   救得回来的那两个字符**:这一档不走它。取舍是故意的,裸写法不是 X 展开出来的
#   形态(t.co 展开必带 scheme,feedgrab 的产物也是完整 URL),而域名后缀误认会把
#   **别人的文章**记成这条推文的内容,这个方向上宁可多排(口径同 `HINT` 那条
#   "排掉引用推容器里的卡片")。要给这一档也开口子得在这里再拆一份字符集(`_URL_RUN`
#   那一份管的是另一件事,见它自己那段注释),多一份要对齐的口径换一种没有真实触发
#   路径的写法,不值。
#
# **禁止集必须逐个列出 ASCII,不许写 `\w`**:Python 的 str 模式下它是 Unicode 的
# (`re.match(r'\w+', '中文')` 当场命中),于是「我写了一篇新文章:<地址>」里那条
# **真**地址会被自己的前一个汉字挡住 —— 那是漏认,而漏认 = 空正文永久固定,
# 比多删一处还重一个量级(口径同 `_URL_TAIL` ① 那条坑,`_TCO` 原来就是这么漏的)。
# **`:` 两档都放行**:`参考:https://x.com/…/article/1` 这种英文冒号紧贴的写法里
# 那条地址是真的,禁掉它就摘不下来了。
# **没有改成「先切出 URL 再 `urlsplit` 校验主机名」**:正文是自由文本,"从哪儿到
# 哪儿是一条 URL"本来就是这条正则在回答的问题,再加一层解析只是把同一个边界问题
# 挪到切分那一步,还多一份要对齐的口径;定宽后视 + `_nested` 直接回答"这个起点是
# 不是一条 URL 的起点",判据和匹配待在同一条规则里。
_URL_CHARS = r'A-Za-z0-9._~%+/=&?#@\-'
_SCHEME_HEAD = r'(?<![=&/])'
_BARE_HEAD = r'(?<![' + _URL_CHARS + r'])'
_URL_HOST = r'(?:' + _SCHEME_HEAD + r'https?://|' + _BARE_HEAD + r')'
# `?` 和 `#` 不进上面那道后视,它们有**两个身份**:在一条已经开始的 URL 里是查询串
# /片段的分隔符(也就是上面那种嵌套),在正文里却是作者的句读。单看紧贴的那一个
# 字符分不开 —— 和 `.` `~` 是同一种二义性,只是那两个在①档里一律放行就对了,这两个
# 两头都有真实写法,所以要再问一次上下文。
#
# **一律禁掉**的后果实测过(2026-10-09):`Have you read this?<文章地址>` 和
# `读过这篇吗?<文章地址>`(中文不带空格,半角问号紧贴是常见写法)里那条**真**地址
# 被自己的句末问号挡住,`needs_article` 三条判据一条都不命中 —— 那条链接本身当成
# 正文入库,`added=1`、`errors=[]`、`partial=[]`、水位照常推进、标题回落成
# 「某某 的动态」,而 `DO NOTHING` 之下重复同步也补不回来;`What#<文章地址>` 同形,
# 两种路径形式、两条通道逐字一样。
# **反过来直接删掉也不行**:`https://example.org/#<文章地址>` 这种把地址放在别人
# 片段里的写法照旧要一个字不动(口径同上面那六种外部地址)。
#
# 判据是**紧贴它左边那一段 URL 字符里有没有另一条 URL 的起点**:有就说明这个
# `?`/`#` 属于那条 URL,没有就是作者的句读。两处细节各有理由:
# ①**只往左吃 URL 合法字符,不是"数到最近的空白"**。查询符必须紧邻它自己那条
#   URL 的 authority/path,而正文里的汉字天然落在字符集外面 —— 数到空白的话
#   `https://example.org/doc,<文章地址>`(两条地址用逗号紧连)这种**真**地址会被
#   误拒,而误拒 = 漏认 = 空正文永久固定。**而"URL 合法字符"按 RFC 3986 取全集**,
#   见 `_URL_RUN` 自己那段注释:少认一个,外层起点就丢了、别人那条地址被从中间
#   剪断。`:` 在这一段里要认(路径里的 `a:b`),它只是不进 `_BARE_HEAD` 的禁止集
#   —— 两件事。
# ②**起手式只认 `https?://` 和 `www.`**(口径同 `daily_refs._autolinked`)。于是
#   「裸域名 + `?` + 嵌一条完整 X 地址」(`example.org?<文章地址>`)这道缝留着 ——
#   那不是任何真实写法。
# **`=` `&` `/` 不走这一道**:它们在正文里压根不是标点,没有第二个身份,定宽后视
# 更便宜,而且那样就不会把上面那条"逗号紧连"的真地址连带误拒。
_AMBIGUOUS = '?#'
# 「一条**已经开始**的 URL 内部能出现的 ASCII 字符」,按 RFC 3986 取全集:
# unreserved `A-Za-z0-9-._~`、`%`、sub-delims `!$&'()*+,;=`、`:@`,外加 `/?#`。
# **和 `_URL_CHARS` 不是一份,也不许合并**:那一份是②档的禁止集(加宽它等于把
# 「英文逗号/括号 + 裸域名」也拒掉,扩大②档已记的取舍),这一份是 `_nested` 往左追
# 外层起点时能跨过的字符,少一个就追不回去。**一个字符在不在 URL 里有标准答案,
# 不靠猜** —— 原来这里复用 `_URL_CHARS` 再补一个 `:`,于是 `(` `)` `;` `$` `'`
# `,` `!` `*` 八个合法路径字符一个不认,往左一碰上就从中途重新起算、找不到
# `https://`,`_nested` 误答"不是嵌套",**别人那条地址被从中间剪断**,而
# `DO NOTHING` 让它永久固定(2026-10-09 第 5 轮审查实测
# `…/docs(v2)#https://x.com/Other/article/20002` 在库里和导出的 md 里都只剩
# `…/docs(v2)#`,而 `added=1`、`errors=[]`、`partial=[]`、水位照常推进、一行提示
# 都没有;`/path;v=2?…` 和 `/$docs#…` 逐字同样,真无头浏览器读回这两条的 hostname
# 都是 `example.org`、后面那段属于 hash/search,压根不是第二条独立链接)。
# **已知留着的一道缝:非 ASCII 的路径**(`…/文档#<文章地址>` 那条照旧被摘)。汉字
# 加不进来 —— 它和上面那条放行样本 `参考https://example.org/说明?<文章地址>`
# **完全同形**("往左吃到一个汉字就断"),而那一侧是**漏认**:两头都是永久损坏,
# 不是"哪个对"的问题。线划在 ASCII 上可解释 —— 非 ASCII 不是 URL 的原生形态
# (浏览器要先百分号编码),而编码之后的写法(`/%E6%96%87%E6%A1%A3#…`)由 `%` 和
# 十六进制字符覆盖,本来就判得对。
_URL_RUN = re.compile(r"[A-Za-z0-9\-._~%!$&'()*+,;=:@/?#]*$")
_OUTER_START = re.compile(r'https?://|www\.', re.I)
# 一条 URL 写在推文正文里时,它的尾巴能长到哪儿(查询串、片段、尾斜杠)。下面两条
# 正则都用它,而它们都是拿去 `sub` 掉的 —— 所以这里多认一个字符就是把作者写的字
# 删掉一个,而 `put_items` 的 `DO NOTHING` 让这种删除**永久固定**。
#
# **两道各挡一类,缺一不可**:
# ①**字符类只收 URL 的 ASCII 合法字符**,于是中文(逗号、括号、以及作者正文本身)
#   天然落在外面。**不许写 `\S` 或 `\w`**:`\S` 是"任意非空白",汉字就在里面;
#   而 Python 的 str 模式下 **`\w` 也是 Unicode 的**(`re.match(r'\w+', '中文')`
#   当场命中),所以换成它一样挡不住 —— `_TCO` 原来就是这么漏的。
# ②**结尾必须落在"不像句读"的那一档**(窄集比宽集少了 `.` `,` `;` `:` `!` `?`
#   和闭括号):这些字符在 URL 中间算数,贴在尾巴上的那一个却是作者的标点。
# 另外 `[/?#]` 后面要求至少一个字符(尾斜杠那一支单列),否则
# `Did you read <url>?` 句末那个问号会被当成空查询串吞掉。
#
# 实测这一道挡住的形态(2026-10-09,每一种都是真实推文写法):中文逗号/句号紧贴、
# 中文括号与方括号包住、`?s=20` 与 `#sec3` 之后紧跟中文、英文 `. ` 与 `) ` 收尾。
_URL_TAIL = r'(?:[/?#][A-Za-z0-9_\-=&%.~+/?#:;,!]*[A-Za-z0-9_\-=&%~+/]|/)?'
# 正文里那条指向长文的裸链接(feedgrab 的产物正文就是它)。**两种路径形式都要
# 认**:`/i/article/<article_id>`(t.co 展开出来的那种)和
# `/<handle>/article/<tweet_id>`(X 自己 302 到的**规范形式**,也正是
# `article_page()` 生成的那个地址 —— 见它那条注释)。
#
# 原来只认前一种,而后一种在三条通道上都会出现:**feedgrab** 的产物正文就是展开
# 后的地址(展开成哪一种由上游和 X 说了算,我们看不见也管不着),**浏览器/attach**
# 那边作者写在推文里的可见链接同样可能是它。漏认的后果是 `needs_article` 三条
# 判据一条都不命中(①看不见这个形式,③因为"正文里有字"而不命中,②的卡片信号只在
# 浏览器通道上有、而且某次滚动没渲染出来就没有)—— 于是**那条链接本身被当成正文
# 入库**,而 `put_items` 是 `ON CONFLICT DO NOTHING`,下一轮重抓也更新不了它,
# 那一篇永久只有一个链接(2026-10-09 实测:feedgrab 端到端 `added=1`、
# `errors=[]`、`partial=[]`、水位照常推进,而库里正文就是那条地址、标题回落成
# 「某某 的动态」;浏览器通道没有卡片信号时逐字同样)。
#
# **两个方向的代价差着一个量级**,所以宁可认宽:漏认一种形式 = 空正文永久固定,
# 多认一种 = 最坏一次白跑的询问(答复正好是"它没有长文",照旧入库、不冻水位),
# 口径同 `HINT` 那条注释。**但 handle 的字符类不许放宽**(同 `_STATUS` /
# `_ARTICLE_PATH` 的 `[A-Za-z0-9_]{1,20}`,`/i/` 的 `i` 本来就落在里面):这条
# 正则在 `lead_text` 里是拿去 `sub` 掉的,认宽了会把推文里真实的字删掉。
#
# **「认宽」只对地址的主体成立,尾巴反过来要收窄**(`_URL_TAIL`,原来是 `\S*`)。
# 两个消费方的方向正好相反:判据① 是 `search`,宁可认宽(漏认 = 空正文永久固定);
# `lead_text` 是 `sub`,宁可少删(多删 = 作者的字永久消失)。收窄尾巴恰好只动后者
# —— 地址主体已经足够回答"有没有一条文章地址",尾巴匹配到哪儿对 `search` 的真假
# 一点影响都没有(回归里反过来钉着这一边)。
#
# **起点那一头(`_URL_HOST` + `_nested`)例外:它两个消费方一起收窄,而这不损害
# 判据①。** 被它排掉的串本来就**不是**这条推文自己写的 X 文章地址(主机名是
# `fxtwitter.com`/`notx.com`/`example.org`,或者整条嵌在别人的查询串/片段里),
# 所以真地址的识别一个字都没变窄 —— 收窄的只有"误认"那一半,两个方向同时变好。
# **但那道闸不能是一句"禁掉 URL 内部字符紧贴"**,有两层:①按带不带 scheme 分档
# (合成一档的话 `新文章...<地址>` 这种省略号紧贴的**真**地址会被自己前面那个标点
# 挡住);②`?` 和 `#` 连分档都不够,它们在①档里也得再问一次上下文(一律禁掉的话
# `读过这篇吗?<地址>` 同样被挡住)。两种漏认的末态一模一样:判据① 一条都不命中、
# 空正文永久固定 —— 那就不再是"只收窄误认"了(两份实测见 `_SCHEME_HEAD` 和
# `_AMBIGUOUS` 那两段注释)。回归里两头都钉着:省略号/波浪号/英文冒号/开括号/
# 中文字/行首/句末问号后面的真地址照旧认得出摘得掉,别人家的地址一个字不动。
_ARTICLE_LINK = re.compile(
    _URL_HOST + r'(?:x|twitter)\.com/[A-Za-z0-9_]{1,20}/article/\d+' + _URL_TAIL)
# t.co 短链:浏览器通道偶尔把它留在正文里。短码本身只有 ASCII —— 原来写的 `\w+`
# 在 str 模式下是 Unicode 的,于是 `https://t.co/aBcD1234重点看第三节。` 里那句
# 说明被整段吞掉(2026-10-09 实测 `lead_text` 只剩 `短链  。`)。
_TCO = re.compile(_SCHEME_HEAD + r'https?://t\.co/[A-Za-z0-9_]+' + _URL_TAIL)
# Draft.js 的块类型 → markdown 前缀。认不出的块按普通段落走。
_PREFIX = {
    'header-one': '# ', 'header-two': '## ', 'header-three': '### ',
    'header-four': '#### ', 'header-five': '##### ', 'header-six': '###### ',
    'unordered-list-item': '- ', 'ordered-list-item': '1. ', 'blockquote': '> ',
}


class ArticleUnavailable(RuntimeError):
    """没问到。message 是成品文案。**和「它没有长文」是两回事** —— 后者返回 None。"""


def _nested(m: re.Match) -> bool:
    """这个匹配是嵌在**别人**那条 URL 的查询串/片段里,不是这条推文自己写的地址。

    只对 `?` / `#` 紧贴的那一档发问 —— 别的嵌套入口(`=` `&` `/`)已经由
    `_SCHEME_HEAD` 那道定宽后视挡住,而这两个字符同时还是作者的句读,分不开。
    判据见 `_AMBIGUOUS` 那段注释:紧贴它左边那一段 URL 字符里有没有另一条 URL 的
    起点。
    """
    text, start = m.string, m.start()
    if start == 0 or text[start - 1] not in _AMBIGUOUS:
        return False
    run = _URL_RUN.search(text[:start - 1])
    return bool(run and _OUTER_START.search(run.group(0)))


def _first(pat: re.Pattern, text: str) -> re.Match | None:
    """第一个**不是**嵌在别人 URL 里的匹配。`needs_article` 判据① 的入口。"""
    return next((m for m in pat.finditer(text) if not _nested(m)), None)


def _drop(pat: re.Pattern, text: str) -> str:
    """只摘掉**不是**嵌在别人 URL 里的那些匹配,别人的地址一个字不动。

    和 `_first` 共用 `_nested`:两个消费方的方向相反(`search` 宁可认宽、`sub`
    宁可少删),但"这个起点算不算一条新 URL 的起点"只能有一份口径。
    """
    return pat.sub(lambda m: ' ' if not _nested(m) else m.group(0), text)


def lead_text(body: str) -> str:
    """把正文里「这条推文自己说的话」摘出来:去掉转推上下文那行和光秃秃的链接。

    三样东西都不算"说了话":`_entry` 给转推加的那行 `> 上下文`、指向长文的裸链接、
    以及 t.co 短链。剩下的才是作者写在推文里的字 —— 它决定这一条要不要去补正文,
    补到之后也作为开头留在正文里。

    **「指向长文的裸链接」认的是 `_ARTICLE_LINK` 那两种形式,和 `needs_article`
    判据① 同一份口径**(就是同一条正则 —— 两处各写一份必然漂)。两处都要对是因为
    它们各管一头:判据① 决定"这一条要不要去问",这里决定"问到之后正文开头留什么"。
    少认一种形式时,即使别的判据救回了这一条(浏览器通道的卡片信号),那条地址也会
    原样留在库里那篇正文的开头 —— 它不是作者写的字,是 X 替他渲染成卡片的那条链接
    (2026-10-09 实测:规范形式下库里那篇正文是「我写了一篇新文章」、紧跟着那条
    `https://x.com/…/article/9`、然后才是全文)。

    **摘掉的只能是那条地址本身,紧挨着它的标点和文字一个都不许动**(尾巴的口径在
    `_URL_TAIL`)。两条正则的尾巴原来都是"吃到下一个空白为止"(`\S*` / Unicode 的
    `\w+`),而中文不带空格:`…/article/10001，重点看第三节。` 整句都被当成地址
    删掉,`added=1`、`errors=[]`、水位照常推进,而 `DO NOTHING` 让这一篇**永久**
    只剩前半句 —— 重复同步也补不回来(2026-10-09 实测,中文括号/方括号、
    `?s=20` 或 `#sec3` 之后紧跟中文、以及英文的 `. ` `) ` 收尾逐字同样;两种路径
    形式和 t.co 那条都有这个病)。

    **左边同理:起点必须是一条 URL 的起点**(`_URL_HOST` + `_nested`)。两条正则里
    的 `x.com` / `twitter.com` / `t.co` 只是一段字符,不加这道闸时
    `https://fxtwitter.com/Example/article/10001` 这类**合法的外部地址**会从中间
    被当成起点、后半截删掉,末态和上面逐字相同(2026-10-09 实测:库里和导出的 md
    里那条参考地址只剩 `https://fx`,而 `added=1`、`errors=[]`、`partial=[]`、
    水位照常推进)。整条嵌在别人查询串/片段里的那种(`https://example.org/#<地址>`)
    同理一个字不动,而**句末问号紧贴的那一条是作者自己的地址,照摘** —— 这两件事
    靠的是 `_nested` 看一眼那个 `?`/`#` 左边是不是一条 URL,不是看它是哪个字符。
    """
    lines = [ln for ln in (body or '').splitlines() if not ln.lstrip().startswith('>')]
    return _drop(_TCO, _drop(_ARTICLE_LINK, '\n'.join(lines))).strip()


# 时间线采集给这条推文挂的「它带着一张长文卡片」信号(`x_browser._COLLECT_JS`
# 从 DOM 里读那张卡片的链接)。**它只是信号,地址不用它** —— 文章页地址由推文
# 自己的 URL 推出来(见 `article_page`),"有没有长文"的结论由接口/页面那一侧给。
# 所以信号误判的代价只是一次白跑的询问(答复正好是"它没有长文"、照旧入库),
# 绝不会把别人的文章记到这条推文头上。
HINT = 'article_hint'


def _strip_hint(entry: dict) -> dict:
    """摘掉那个内部信号键 —— 它不进 `feed_items`:`put_items` 把**整份条目**
    哈希成 `content_hash`,多一个只有采集期间才有意义的键会让同一条推文在两条
    通道上算出不同的哈希。"""
    return {k: v for k, v in entry.items() if k != HINT} if HINT in entry else entry


def needs_article(entry: dict, *, links_only: bool = False) -> bool:
    """这一条要不要去问一次长文。三条,各管一种形态:

    ①**正文里明写着一条文章地址** —— 这是确定的信号,feedgrab 的产物正文就是它
      (推文的 t.co 被展开成了文章地址),哪怕作者还写了别的话。`/i/article/<id>`
      和 `/<handle>/article/<id>` **两种形式都算**,见 `_ARTICLE_LINK`:展开成哪
      一种由上游和 X 说了算,只认一种时另一种会把那条链接本身当正文入库、而且
      `DO NOTHING` 之下永久改不回来。**紧贴在它前面的标点一个都不许挡住它**
      (省略号、波浪号、英文冒号、句末问号…… 口径在 `_URL_HOST` 和 `_nested`):
      挡住的末态和少认一种形式逐字相同。整条嵌在别人 URL 里的那种不算 —— 那是
      别人的文章,见 `_nested`。
    ②**时间线上看见了那张长文卡片**(`HINT`)—— 浏览器通道那边 X 把链接藏起来
      改渲染成卡片,所以「写了一句介绍 + 一张卡片」的推文正文**非空**,①②之外
      没有任何信号;而卡片本身在 DOM 里是一条 `/i/article/<id>` 或
      `/<handle>/article/<id>` 链接,读出来当信号正好。
    ③**推文自己一个字都没说** —— 同样是卡片那条路,只是作者没写介绍,于是我们
      手里只剩一条空正文。纯图、纯视频推文也满足它,那是故意的:没有别的办法
      分辨,而问一次的答复正好把它们分开(返回 None = 它没有长文)。

    **不拿「正文里有 t.co 短链」当信号**:那可能是图片、引用推、任意外链,按它
    发请求等于给一大半推文各买一次调用,而换回来的多半是"它没有长文"。

    `links_only` 是桥接通道那一档:它拿到的是桥接实例给的推文文本,长文在那儿
    的形态**只有** ① 一种(正文就是展开后的文章地址),而 ③ 在那条路上多半是纯
    图推文 —— 一轮几十条各买一次询问换不回东西,所以只认 ①。
    """
    if not entry.get('url'):
        return False
    body = entry.get('body') or ''
    if _first(_ARTICLE_LINK, body):
        return True
    if links_only:
        return False
    return bool(entry.get(HINT)) or not lead_text(body)


def _media_urls(article: dict) -> dict[str, str]:
    """`media_id` → 图片地址。文章里的图是按 id 引用的,地址在另一张表里。"""
    out = {}
    for media in article.get('media_entities') or []:
        if not isinstance(media, dict):
            continue
        url = ((media.get('media_info') or {}).get('original_img_url') or '').strip()
        if url and (mid := str(media.get('media_id') or '')):
            out[mid] = url
    return out


def _entity_image(block: dict, entities: dict, media: dict) -> str:
    """atomic 块指向的那张图。认不出来就返回空串(这一块整个跳过)。"""
    ranges = block.get('entityRanges') or []
    if not ranges or not isinstance(ranges[0], dict):
        return ''
    value = entities.get(str(ranges[0].get('key')))
    if not isinstance(value, dict) or value.get('type') != 'MEDIA':
        return ''
    for item in (value.get('data') or {}).get('mediaItems') or []:
        if url := media.get(str((item or {}).get('mediaId') or '')):
            return url
    return ''


def _entities(content: dict) -> dict:
    """`entityMap`。**它是一个列表,不是字典** —— 上游给的是 `[{key, value}, …]`,
    而块里的 `entityRanges[].key` 是数字,所以键一律按字符串比。"""
    raw = content.get('entityMap')
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    out = {}
    for row in raw or []:
        if isinstance(row, dict) and 'key' in row:
            out[str(row['key'])] = row.get('value')
    return out


def render(article: dict) -> str:
    """文章 JSON → markdown 正文。

    **只按块级结构转,不动块内偏移**(`inlineStyleRanges` 的粗体、`entityRanges`
    的行内链接一概不展开)。那些偏移是 Draft.js 的 **UTF-16 码元**下标,而 Python
    的字符串按码点索引 —— 正文里出现一个 emoji 就会把它后面所有偏移错开一位,
    拿错位的下标去插标记就是把作者写的字改坏。块级的东西(标题、列表、引用、整块
    的图)没有这个问题,它们本来就是一整块。

    标题不写进正文:`sync.export` 自己会在 md 顶上写一行 `# <title>`。
    """
    content = article.get('content')
    if not isinstance(content, dict):
        return ''
    entities, media = _entities(content), _media_urls(article)
    out: list[str] = []
    for block in content.get('blocks') or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get('type') or 'unstyled')
        if kind == 'atomic':
            if url := _entity_image(block, entities, media):
                out.append(f'![]({url})')
            continue
        text = str(block.get('text') or '').replace('\x00', '').strip()
        if text:
            out.append(_PREFIX.get(kind, '') + text)
    return '\n\n'.join(out)[:MAX_CHARS].strip()


def fetch(url: str, *, timeout: float = 0.0) -> dict | None:
    """问一次这条推文的长文。返回 {'title','body'};**它没有长文时返回 None**。

    两个返回值必须分得开:None 是一个确定的结论(纯图推文就是这样),调用方按它
    原样入库;问不到则抛 `ArticleUnavailable`,调用方据此不推进水位。把后者也
    当成 None 就是拿一次故障冒充"这条没有正文",而空正文一旦落库再也改不了。
    """
    if not API:
        return None
    m = _STATUS.search(url or '')
    if not m:
        return None
    endpoint = f'{API}/{m.group(1)}/status/{m.group(2)}'
    deadline = time.monotonic() + (timeout or TIMEOUT)
    try:
        resp = request(endpoint, {'Accept': 'application/json'}, deadline=deadline)
        payload = json.loads(resp.body.decode('utf-8', 'replace'))
    except FeedError as e:
        # **只取分号前那一半**:`fetching` 给的是订阅采集的成品文案,后半截
        # 「请检查来源或更换订阅桥接」在长文语境里是胡话 —— 这一步跟订阅桥接
        # 毫无关系,照原样带出去只会把人指到一个改不出结果的设置上。
        raise ArticleUnavailable(
            f'长文接口没答复:{re.split(r"[;；]", str(e))[0].strip()}') from None
    except (OSError, ValueError, UnicodeError) as e:
        raise ArticleUnavailable(f'长文接口的回包读不懂:{e}') from None
    if not isinstance(payload, dict) or not isinstance(payload.get('tweet'), dict):
        raise ArticleUnavailable('长文接口的回包里没有推文')
    article = payload['tweet'].get('article')
    if not isinstance(article, dict) or not article:
        return None                       # 确定的结论:这条推文没有长文
    body = render(article)
    title = str(article.get('title') or '').strip()
    # **只有标题不算补到了。** 要补的是正文,而空正文一旦落库就**再也改不回来**
    # (`put_items` 是 `ON CONFLICT DO NOTHING`),下一轮重叠重抓也救不回来 ——
    # 口径同 `browser_fetch` 那条「判不准一律抛」。三种形态都归这儿:回包被截断、
    # `content.blocks` 缩成空、或者真是一篇空文章。抛出去之后手里有浏览器的
    # 两条通道会接着走后路,两条都不行时这一条本轮不入库、水位留在原处。
    #
    # **不许退回「标题正文同时为空才算」**:那一版在"有标题、没正文"这个形状上
    # 返回成功,于是 `fill` 留下一条空正文、水位照常推进,而那一篇永久空着。
    if not body:
        raise ArticleUnavailable(
            f'长文接口只给了标题「{title[:40]}」、没有正文' if title
            else '长文接口给了一篇空文章')
    return {'title': title, 'body': body}


# ----------------------------------------------------------------- 后路:登录态浏览器

# **后路**:接口不可用时,在一个已经登录的浏览器里直接把文章页读出来。只有
# `browser` / `attach` 两条通道手里有浏览器(前者是我们自己起的独立 profile、
# 后者是用户日常那个),`bridge` / `feedgrab` 走不到这里 —— 它们照旧按
# 「补不到就不入库、不推进水位」办。设成 '0' 关掉这条后路。
BROWSER_FALLBACK = os.environ.get('KB_X_ARTICLE_BROWSER', '1') != '0'
# 文章页等多久。X 的长文是异步渲染的,实测首屏 2~5 秒。
PAGE_WAIT = float(os.environ.get('KB_X_ARTICLE_PAGE_WAIT', '15'))
_PAGE_POLL = 0.8

# 文章页的地址 = 推文地址把 `/status/` 换成 `/article/`。**这是 X 自己的规范形式,
# 不是我们凑的**:`/i/article/<article_id>` 实测 302 到
# `/<handle>/article/<tweet_id>`(2026-10-09)。这件事是后路成立的前提 ——
# `article_id` 只出现在那个接口的回包里,而后路要在接口**已经不可用**的时候跑,
# 手里只有推文 URL;要是非得先知道 article_id,这条路压根立不起来。
_ARTICLE_PATH = re.compile(r'^(https://(?:x|twitter)\.com/[A-Za-z0-9_]{1,20})/status/(\d+)')

# 读一次文章页。三件事一起回答,因为它们在同一个 DOM 快照里才自洽:
# ①**我们还在文章页上吗**(`at`),还是被 X 弹回了推文页(`back`);
# ②正文块(`blocks`);③标题(`title`)。
#
# **`back` 是「这条没有长文」的判据,而且它不押任何 CSS 选择器** —— 实测一条纯图
# 推文的 `/article/<id>` 会被 X 自己 302 回 `/status/<id>`(2026-10-09),那是它的
# 路由行为,比任何 testid 都稳。有长文的那条则老老实实停在 `/article/` 上。
#
# 正文容器认 `[data-contents="true"]` 里的 `[data-block="true"]`(Draft.js 的块),
# 所以**段落边界是按块拿的、不是切 innerText** —— 实测 47 块、2752 字纯正文,
# 比整页 innerText 少掉 234 字的导航、作者简介和「升级 Premium」。**块内按子节点
# 顺序拼**,理由见 `textOf` 上面那段:行内表情是 `<img>`,一块里既有文字又有图
# 是常态。
#
# 标题取 read view 里排除正文之后**字号最大的那个叶子**(实测标题 34px、其余
# ≤15px),拿不到按 read view 的第一行 —— 实测两者是同一个值,互为冗余。
_READ_JS = r'''(() => {
  const path = location.pathname;
  const rich = document.querySelector('[data-testid="twitterArticleRichTextView"]');
  const read = document.querySelector('[data-testid="twitterArticleReadView"]');
  const root = document.querySelector('[data-contents="true"]');
  // 一块的内容按**子节点顺序**拼出来,`<img>` 分两种:带 alt 的是行内表情
  // (X 把 emoji 渲染成 twemoji 图片,alt 就是那个字符本身),按 alt 还原;
  // 没有 alt 的是真·配图,写成 markdown 图片。
  //
  // **不许写成「这一块里有 img 就只要图片」**:段落里插一个表情图是极常见的
  // 形态(emoji 全是这样),那一版会把**整段文字丢掉**只留一张小图,而且照样
  // 报成功 —— 空/缺字的正文一旦落库就改不回来(`put_items` 是 DO NOTHING)。
  // 也不能用 `innerText`:它压根不包含 img 的 alt,表情会静默消失。
  const textOf = (node) => {
    let s = '';
    for (const n of node.childNodes) {
      if (n.nodeType === 3) { s += n.nodeValue; continue; }
      if (n.nodeType !== 1) continue;
      if (getComputedStyle(n).display === 'none') continue;
      const tag = n.tagName.toLowerCase();
      if (tag === 'br') { s += '\n'; continue; }
      if (tag === 'img') {
        const alt = (n.getAttribute('alt') || '').trim();
        s += alt || (n.src ? '![](' + n.src + ')' : '');
        continue;
      }
      s += textOf(n);
    }
    return s;
  };
  const blocks = [];
  if (root) for (const b of root.querySelectorAll('[data-block="true"]')) {
    const txt = textOf(b).replace(/[\u200b\u200c\u200d\ufeff]/g, '')
                         .replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').trim();
    if (txt) blocks.push(txt);
  }
  let title = '', big = 0;
  if (read) {
    for (const e of read.querySelectorAll('*')) {
      if (e.children.length) continue;
      if (rich && rich.contains(e)) continue;
      const t = (e.innerText || '').trim();
      if (!t || t.length > 300) continue;
      const fs = parseFloat(getComputedStyle(e).fontSize) || 0;
      if (fs > big) { big = fs; title = t; }
    }
    if (!title) title = (read.innerText || '').split('\n')[0].trim();
  }
  return JSON.stringify({
    at: /\/article\/\d+\/?$/.test(path),
    back: /\/status\/\d+\/?$/.test(path),
    blocks: blocks, title: title,
  });
})()'''


def article_page(url: str) -> str:
    """推文 URL → 文章页 URL。认不出来返回空串。"""
    m = _ARTICLE_PATH.match((url or '').strip())
    return f'{m.group(1)}/article/{m.group(2)}' if m else ''


def browser_fetch(session, url: str, *, wait: float = 0.0) -> dict | None:
    """后路:在 `session` 那个**已经登录**的浏览器里读这篇长文。

    返回值的两档和 `fetch` 逐字同义:`{'title','body'}` 是读到了,`None` 是
    **确定这条推文没有长文**,问不到一律抛 `ArticleUnavailable`。

    **判不准的时候一律抛,不许返回 None。** 两个方向的代价差着一个量级:把
    「有长文」说成「没有」会让一条空正文落库,而 `put_items` 是 DO NOTHING ——
    那一篇从此永久是空的、下一轮重试也救不回来;反过来把「没有长文」说成
    「问不到」只是让水位留在原处、下一轮重叠重抓一次(`put_items` 按 URL 去重)。
    所以只有 X 自己把我们弹回推文页那一种才算「没有」,登录墙、空壳、渲染超时、
    CDP 报错全部抛出去。
    """
    page = article_page(url)
    if not page:
        return None
    deadline = time.monotonic() + (wait or PAGE_WAIT)
    try:
        session.goto(page)
    except CdpError as e:
        raise ArticleUnavailable(f'长文页面打不开:{e}') from None
    while True:
        try:
            snap = json.loads(session.eval(_READ_JS))
        except CdpError as e:
            raise ArticleUnavailable(f'长文页面读不动:{e}') from None
        except (ValueError, TypeError, UnicodeError) as e:
            raise ArticleUnavailable(f'长文页面的回包读不懂:{e}') from None
        if not isinstance(snap, dict):
            raise ArticleUnavailable('长文页面的回包读不懂')
        if snap.get('back'):
            return None               # X 自己弹回推文页:这条确实没有长文
        blocks = [str(b) for b in (snap.get('blocks') or []) if str(b).strip()]
        if blocks:
            body = '\n\n'.join(blocks)[:MAX_CHARS].strip()
            title = str(snap.get('title') or '').strip()
            return {'title': title, 'body': body}
        if time.monotonic() > deadline:
            raise ArticleUnavailable(
                '长文页面没渲染出正文' if snap.get('at')
                else '长文页面没打开(多半是这个浏览器没登录 X)')
        time.sleep(_PAGE_POLL)


def _one(entry: dict, session) -> dict | None:
    """一条推文的长文:先问接口,接口不可用就走浏览器后路。

    **两条路的「没有长文」都是确定结论**(返回 None),两条路都问不到才抛
    `ArticleUnavailable`。接口答了「没有长文」时**不再走后路** —— 那是一个
    确定的答复,再开一张标签页只是白等。
    """
    if API:
        try:
            return fetch(entry['url'])
        except ArticleUnavailable:
            if session is None or not BROWSER_FALLBACK:
                raise
        return browser_fetch(session, entry['url'])
    # 接口被整个关掉(`KB_X_ARTICLE_API=''`):手里有浏览器就只走后路,没有就
    # 什么都不做 —— 后者的行为逐字退回这一步出现之前。
    if session is None or not BROWSER_FALLBACK:
        return None
    return browser_fetch(session, entry['url'])


def fill(entries: list[dict], *, session=None,
         links_only: bool = False) -> tuple[list[dict], str]:
    """把这一批里的长文正文补上。返回 (留下的条目, 没补全的原因)。

    **原因非空 = 这一轮没补全**,调用方把它并进 `incomplete`,于是水位留在原处、
    下一轮重叠重抓时再试(理由见模块注释:空正文入库之后就改不回来了)。补不到的
    那几条**本轮不入库** —— 入库了下一轮的重试也更新不了它们,留着等于白等。

    `session` 是一个还活着的 CDP 会话(`browser` / `attach` 两条通道手里有),
    接口不可用时拿它走后路。给 None 就只有接口这一条路(桥接、feedgrab)。
    `links_only` 见 `needs_article`:桥接那一档只认正文里明写的文章地址。
    """
    if not API and (session is None or not BROWSER_FALLBACK):
        return [_strip_hint(e) for e in entries], ''
    kept, missed, fails = [], 0, 0
    for entry in entries:
        want = needs_article(entry, links_only=links_only)
        if not want or fails >= _GIVE_UP_AFTER:
            if fails >= _GIVE_UP_AFTER and want:
                missed += 1
                continue
            kept.append(_strip_hint(entry))
            continue
        try:
            got = _one(entry, session)
            fails = 0
        except ArticleUnavailable:
            fails += 1
            missed += 1
            continue
        if got:
            lead = lead_text(entry.get('body') or '')
            entry = {**entry,
                     'title': (got['title'] or entry.get('title') or '')[:500],
                     'body': f"{lead}\n\n{got['body']}".strip() if lead else got['body']}
        kept.append(_strip_hint(entry))
    note = (f'有 {missed} 条推文的长文正文没取到,留到下次同步再试' if missed else '')
    return kept, note
