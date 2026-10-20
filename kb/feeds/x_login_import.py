# -*- coding: utf-8 -*-
"""把日常浏览器里的 X 登录态搬进独立 profile —— 一次点击,之后无人值守。

**硬约束在 `docs/decisions/x-channels.md` 第一节 —— 改这块之前先读那一份。**

**它存在的理由是 Chrome 那个授权弹窗按连接弹,而且刻意不可持久化。**
`attach` 通道(`cdp_attach`)的卖点是用户日常浏览器里现成的登录态,代价是每连一次
都要他点一下「允许」:那个弹窗就是 Chrome 的设计目标本身 —— 上游那个「让授权能
记住」的请求(chrome-devtools-mcp#825)已经 closed as not planned,理由是没有一个
简单方案能同时做到「不让本机任何程序轻松拿到你 Chrome 里的数据」;企业策略
`RemoteDebuggingAllowed` 只是个开关闸门,没有「预授权」这一档。于是定时那一轮
(每天跟着日报跑)落在一个结构性矛盾上:**它无人值守,而这条通道要求人在场**
—— 没人点,90 秒后整批 X 账号记失败(`cdp_attach.ATTACH_TIMEOUT`),而那一份日报
里 X 那一段整个缺掉。

**出路是别去持久化那个授权,而是把登录态本身搬到我们自己的 profile 里。**
`browser` 通道(`x_cdp.Chrome`,CDP over 管道、独立 profile)永不弹窗、跨重启持久,
它原来唯一的代价是「要在无头壳里过一次 X 的两步验证和人机校验」。这个模块把那次
代价换成一次点击。

**只搬 x.com 的 cookie,一个别的站点都不碰**(`SCOPE`)。这条不是实现细节:远程
调试一开,那个浏览器里**所有**站点的登录态都在一条命令的射程内 —— 银行、邮箱、
公司后台都在里面。射程窄到只有这件事要用的那个域,是这个功能敢于存在的前提;
**要扩到别的域必须由用户点名**。

**读用户那个浏览器时,范围由浏览器来卡,不由我们过滤**(`read_scoped`)。这条是
上一条的实现要求,而且是两回事:`Storage.getCookies` 会把整杯 cookie 解密好交到
我们进程里,之后再怎么过滤,「没读取授权外的数据」这句话都已经不成立了 —— 过滤
只能证明「没有保存」。所以**那一侧**的读取一律走 `Network.getCookies` + 显式
`urls`,让浏览器在交出来之前就把范围卡掉;`in_scope` 退居第二道(万一哪天 `urls`
的语义变了)。

**我们自己那个 profile 是另一回事,它只能整杯读**(`read_all`):`urls` 卡得住域,
卡掉的却还有**路径** —— 验证页真正用的可能是 `ct0@/i/api`,它在两次读里同时缺席,
集合相等于是什么都证明不了。那个 profile 是我们建的、除了 x.com 哪儿都没去过,
整杯本来就只有 x.com。**两侧的射程写在两个函数里,不许互相借用**,`read_all` 自带
运行时闸,结构性那道钉在 `tests/test_x_login_persist.py`。

**不拷 profile 目录。** 社区常见做法是把 `User Data` 复制一份再挂调试端口,但
Chrome 的 app-bound encryption 让副本里的 cookie 不一定还解得开,而且副本会跟原件
漂。这里读的是**活着的浏览器已经解密好的**那一份,不碰任何文件。

**「当场能用」和「下次还在」是两件事,对外承诺的是后者**(`install`)。写完立刻
在目标 profile 里验一次(`x_browser.verify_timeline`)只能回答前者:那时 cookie
还在那个浏览器进程的内存里。而这个功能承诺的是「以后定时同步不用再点弹窗」——
那要求它**落到磁盘上**。所以收尾之后**重新打开同一个 profile 再读一次**,
`ESSENTIAL` 不齐就不是 `ok`,哪怕当场验证读到了推文。
**被比的那一方是验证结束那一刻浏览器里的状态,不是我们交进去的那份输入** ——
Chrome 会改写它(实测把有效期削到 400 天)、站点在验证期间也会换发凭证,拿输入
去比的是一条把整个功能判死的假判据(理由和实测写在 `install` 和 `fingerprint`)。

**账本永远不比 profile 旧**(`_write_ledger`)。写 cookie 之前先把「这一轮开始了」
记下来,之后每条退出路径都把它更新成真实结论。缺了这一道的后果:导航或验证中途
抛异常时 profile **已经被改了**,而面板上还挂着昨天那条「当场验证通过」—— 用户
看到的是一句关于已经不存在的那份登录态的话。

**cookie 会过期,所以这件事要能反复做。** 整条流程幂等(`Storage.setCookies` 按
name+domain+path 覆盖),界面上那颗按钮按一次就重做一遍;什么时候搬的留在 `LEDGER`
里、面板显示它 —— **但「还能不能用」不许据它回答**:cookie 过期在文件上看不出来
(口径同 `x_channels.readiness` 那条),那是「检查连接」的事。
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kb.config import (DATA_DIR, ScheduleUnreadable, read_config_file,
                       write_config_file)
from kb.feeds.x_cdp import CdpError
# 文案层(结论码 → 人话)拆在隔壁,见那个模块的注释。**用 `from` 导入** ——
# 测试对 `_detail` 一类打桩时改的是这里这张名字表,照旧生效。
from kb.feeds.x_login_words import _detail, _VERDICT_TAIL

#: 允许搬的 cookie 域。**一个别的站点都不碰**(见模块注释),扩域要用户点名。
SCOPE = ('x.com',)
#: 交给 `Network.getCookies` 的 `urls`。**这才是射程的执行点** —— 浏览器只会
#: 交出对这几个地址适用的 cookie,别的站点压根不进我们的进程。
#: 挑的是 X 真正用来发应用的那两个主机:`.x.com` 域 cookie(登录态就在这儿)对
#: 两者都适用。只属于某个别的子域、而且对这两个地址都不适用的那些带不过来 ——
#: 够不够用由紧接着那次验证当场回答,而不是我们在这儿猜。
#:
#: **验证页那个地址也要在里面,否则路径受限的凭证两边都看不见。**
#: `Network.getCookies` 交的是「对这几个地址适用的」,**不是**「这个域下所有路径
#: 的」:`ct0@/home` 这种路径受限的条目在根 URL 下压根不出现(实测:根 URL 只回
#: `path=/` 那几条;把 `/home` 加进去就回得出 `/home` 那条,而且不会重复)。
#: 而页面在 `/home` 上实际选中的是**路径更具体**那一条 —— 于是「验证时用的那一份」
#: 可以整个落在快照之外:两边都筛掉了它,集合相等却证明不了它存了下来
#: (2026-10-15 第 5 轮审查实测:`/home` 下的 `ct0` 重开后退回旧值,生产的页面
#: 分类器重开后判 `login`,而产品报 `ok`)。
#: **路径不可能枚举完**(`/i/api` 那一族随时变),所以这一道只负责「快照覆盖我们
#: 真正验证的那个地址」;看不见的那部分由 `install` 收尾那道**重开再真验一次**
#: 兜住 —— 两道判据各管一边,见 `install`。
VERIFY_URL = 'https://x.com/home'
_SCOPE_URLS = ('https://x.com/', 'https://www.x.com/', VERIFY_URL)
#: 没有它们就等于没搬到登录态。X 的会话靠 `auth_token`(httpOnly),`ct0` 是配套的
#: CSRF 令牌 —— 少了它页面会在第一次 XHR 上掉回登录页,而 Cookies 文件已经躺在
#: profile 里了(那正是 `readiness` 看得见、却答不了「还能不能用」的那一档)。
ESSENTIAL = ('auth_token', 'ct0')
#: 往目标 profile 写的时候交哪几个字段。**白名单而不是原样回灌**:
#: `getCookies` 多给的那几个(`size`、`session`、`sourcePort`……)不是 `CookieParam`
#: 的合法字段,原样交回去会让这条命令整条失败 —— 而那一下的表现是「导入失败」,
#: 看不出是我们多塞了字段。`partitionKey` 反过来**必须带上**:它在两边是同一个
#: 类型,省掉它不是「少一个无用字段」而是**把一条分区 cookie 改写成非分区的**,
#: 同名不同分区的两条还会被压到同一个键上。
#: **往这张表里加字段的人请注意:它同时是核验的投影**(`_PRINT_FIELDS` 由它派生)
#: —— 写进去却不核验,就是「局部相同冒充完整保存」那一族问题的来源。
_PARAMS = ('name', 'value', 'domain', 'path', 'secure', 'httpOnly', 'sameSite',
           'partitionKey')
#: 什么时候搬的。**它只回答「什么时候」,不回答「还能不能用」**(见模块注释)。
LEDGER = DATA_DIR / 'x-login-import.json'
_SHANGHAI = ZoneInfo('Asia/Shanghai')
#: 读 cookie 那一下的上限。一条命令,本机 loopback,慢只可能是浏览器卡住。
_READ_TIMEOUT = 30.0
#: 写 cookie 那一下的上限。一批几个到几十个,一条命令发完。
_WRITE_TIMEOUT = 60.0


def in_scope(domain: str) -> bool:
    """这个 cookie 域在射程里吗。子域算,**同名后缀不算**。

    **这是第二道,不是唯一那道。** 真正把范围卡住的是 `read_scoped` 交给浏览器的
    那个 `urls`(见模块注释);这里是万一哪天 `urls` 的语义变了、或者哪个调用方
    绕过了那条路时的兜底。

    判据是「去掉前导点之后,等于某个受权域、或者是它的**子域**」。
    `endswith('x.com')` 会把 `notx.com`、`evilx.com` 一起圈进来 —— 那是把别的站点
    的登录态也搬走,正是这个模块最不该出的错,所以这里拼上那个点再比。
    反过来 `x.com.evil.com` 也不算(它的后缀是 `evil.com`)。
    """
    host = (domain or '').lstrip('.').lower()
    return any(host == d or host.endswith('.' + d) for d in SCOPE)


def _param(cookie: dict) -> dict:
    """一条 `getCookies` 的结果 → 一条合法的 `CookieParam`。"""
    out = {k: cookie[k] for k in _PARAMS if k in cookie}
    # 会话 cookie 的 `expires` 是 -1,原样交回去等于说「1969 年就过期了」,Chrome
    # 会当场把它丢掉。**省掉这个字段**才是「关掉浏览器就失效」那个语义。
    # **不替它编一个未来的时刻**:那是把用户的会话 cookie 悄悄变成永久凭证,
    # 而且会把「下次还在不在」这个问题的答案伪造成「在」—— 真要是会话 cookie,
    # 重开那一道(`install`)会如实报出来,由用户自己决定怎么办。
    expires = cookie.get('expires')
    if isinstance(expires, (int, float)) and expires > 0:
        out['expires'] = expires
    return out


#: 算指纹要哪几个字段。**身份 + 值 + 还能用多久**:
#: - 身份(`name`/`domain`/`path`/`partitionKey`)分清「是哪一条 cookie」;
#: - `value` 分清「是哪一代」;
#: - **`expires` 和 `session` 分清「还能用多久」** —— 少了这两个,一次「同名同值、
#:   只把有效期从 28 秒续到 24 小时」的重导入会跟没存上长得一模一样:指纹相同、
#:   判「已持久化」,而盘上那条过几十秒就自己没了(2026-10-15 第 3 轮审查实测)。
#:   而「cookie 过期了再导一次」正是这个功能存在的理由之一,所以有效期不是附加
#:   属性,它就是这件事本身。
#: - **`httpOnly`/`secure`/`sameSite` 分清「还读得到、还发得出去吗」** —— 这三个
#:   不是元数据,它们决定这条 cookie 在下一次使用时**到不到得了该到的地方**:
#:   `httpOnly` 决定页面脚本还能不能读 `ct0`(X 的前端靠 `document.cookie` 取它
#:   拼 `x-csrf-token`),`sameSite` 决定跨站请求还带不带它,`secure` 决定它只跟
#:   HTTPS 走。少了这三个,「验证时读得到、重开后读不到」会跟成功长得一模一样
#:   (2026-10-15 第 4 轮审查在真浏览器上实测:同一个 origin 下验证时
#:   `document.cookie` 有 `ct0`,重开后是空串,而名/值/有效期全同、判 `ok`)。
#:   它们本来就是这次搬运的数据契约 —— `_PARAMS` 写入时一个不少地带着,凭什么
#:   核验时丢掉。
#: **这几个字段实测经「写进去 → 正常关 → 重开读出来」逐位原样往返**(持久 cookie
#: 的 `expires` 两边完全相同;三个属性连「没指定」都原样是 `null`;会话 cookie 在
#: 盘上直接消失,正是该报的那一档),所以带上它们不会造出新的假阴性。
#: **不混进元数据**:`size`、`priority` 这些跟「下一次还能不能用」无关,比它们只是
#: 多一种将来被 Chrome 规范化掉、于是永远判「没存上」的方式。
#:
#: **所以这张清单不手写第二遍,它就是 `_PARAMS` 加上持久语义。** 这一族问题被拦下来
#: 过四轮(比名字 → 比值 → 比有效期 → 比读写属性),每一次都是「我们写进去了、核验时
#: 却没看」。两张手写清单之间迟早再开一次这种缝:往 `_PARAMS` 加一个字段的人没有理由
#: 想到还要来改这里,而漏掉的后果是静默判「存上了」。`expires` 不在 `_PARAMS` 里
#: (`_param` 单独处理它,会话 cookie 要省掉这个字段),`session` 是浏览器自己报的,
#: 所以这两个显式补上。回归钉着这条结构关系。
_PRINT_FIELDS = (*_PARAMS, 'expires', 'session')
#: 没有这个字段时在指纹里占的位。**不能用空串,也不能让 `str()` 去统一** ——
#: `sameSite` 的合法取值里**正好有一个叫 `None`**,而没指定时 CDP 回的是 `null`:
#: `str()` 一视同仁的话「没指定」和「显式 None」指纹相同,而那正是这一道要分开的
#: 两种状态(跨站请求到底带不带上它)。cookie 属性里出不来这个控制字符。
_MISSING = '\x01'


def fingerprint(cookie: dict) -> str:
    """一条 cookie 的「哪一条 + 哪一代」指纹。**只活在内存里,绝不落盘。**

    这是「对外宣布跨重启成功的那一代凭证已经持久化」这句话的唯一可核验形式。
    光比名字不够 —— 而「不够」的恰恰是**重导入最常见的那一档**:cookie 过期之后
    用户再点一次,目标 profile 里早就躺着同名的旧凭证,于是「名字还在」这个判据
    对一次**什么都没存进去**的导入照旧放行,面板还会写上「重新打开确认过它们留在
    了磁盘上」(2026-10-15 审查拿真浏览器实测:注入 `Browser.close` 失败走 kill
    分支,盘上留的是 `synthetic-old-*`,而返回 `ok`)。
    **用「磁盘上存在同名的东西」推断「这一轮验证过的内容已经保存」是个不可靠判据。**

    取 sha256 而不是直接留值:这个指纹会在函数之间传递,万一哪天谁把它写进日志或
    账本,漏出去的也只是个摘要。盐没有必要(比的是同一轮里的两次读),但长度截断到
    32 位十六进制够用且短。
    """
    parts = [_MISSING if (v := cookie.get(k)) is None else str(v)
             for k in _PRINT_FIELDS]
    return hashlib.sha256('\x00'.join(parts).encode('utf-8')).hexdigest()[:32]


def read_scoped(session) -> list[dict]:
    """**读用户日常那个浏览器的唯一一处。** 范围由浏览器卡,不由我们过滤。

    `session` 要已经开好一张页面(`open_page`)—— `Network.getCookies` 是页面域的
    命令,而正是它收 `urls`;browser 级那条 `Storage.getCookies` 没有任何按域筛选
    的参数(唯一的可选参数是 `browserContextId`,那是按 profile 分,不是按站点),
    所以它一开口就是整杯。两者的差别不在「我们保存了什么」,而在**浏览器交给这个
    进程什么**,后者才是「只读 x.com」这句承诺的内容(2026-10-15 在自己的无头
    Chrome 上实测:种入 x.com + bank.example + mail.example.org 三个站点,
    `Storage.getCookies` 回五个域,`Network.getCookies` 带 `urls` 只回 x.com 那两个)。
    """
    got = session.call('Network.getCookies', {'urls': list(_SCOPE_URLS)},
                       session_id(session), timeout=_READ_TIMEOUT)
    # `in_scope` 是第二道:浏览器已经按 `urls` 筛过了,这里只拦「万一」。
    return [c for c in (got.get('cookies') or []) if in_scope(c.get('domain', ''))]


def read_all(page) -> list[dict]:
    """**读我们自己那个 profile 的唯一一处:整杯读出来,再按射程筛。**

    为什么这一侧不用 `read_scoped`:`Network.getCookies` 只交得出「对那几个地址
    适用的」,**路径**受限的条目整条看不见 —— 而「要哪些路径」这件事根本不可枚举
    (审查实测:验证页真正选中的是 `ct0@/i/api`;第二次加载把它换发成会话 cookie、
    关掉就没了,三次读两侧**同时**漏掉它,差异于是是空的,产品照报 `ok` 并承诺
    「以后不用点弹窗」,而下一次打开同一个 profile 分类器判 `login`)。
    两边同时漏掉的东西,集合相等证明不了它还在 —— 所以这一侧改成整杯读:
    **盲区是路径,不是名字**,整杯读之后「所有路径上的 `auth_token`/`ct0`」都进了
    判据,再也不靠补 URL。

    **这件事只许发生在我们自己起的那个 profile 上。** 日常浏览器里有银行、邮箱、
    公司后台,`Storage.getCookies` 一开口就是整杯 —— 那条路的射程是这个功能敢于
    存在的前提(见 `read_scoped`,别放宽)。这里的 profile 是我们自己建的、除了
    x.com 哪儿都没去过,所以「整杯」和「x.com 那几条」本来就是一回事,`in_scope`
    再筛一道把这句话钉死。
    守法靠两样,少一样都不行:**运行时**这道闸(`_BACKGROUND` 为真 = 它是开在
    **别人**浏览器里的后台标签页;没有 `profile` = 不是我们起的),和
    `tests/test_x_login_persist.py` 那条**结构性**用例(整个 `kb/` 下这个调用形式
    只许出现在这个函数里,而且只许从目标侧那三处调)。
    """
    if getattr(page, '_BACKGROUND', False) or not getattr(page, 'profile', ''):
        raise CdpError('整杯读只许用在我们自己起的那个 profile 上')
    got = page.call('Storage.getCookies', {}, timeout=_READ_TIMEOUT)
    return [c for c in (got.get('cookies') or []) if in_scope(c.get('domain', ''))]


def session_id(session) -> str:
    """那张页面的 sessionId。`open_page` 把它存在 `_sid` 上。"""
    return getattr(session, '_sid', '') or ''


def read_from_daily_browser(*, endpoint=None, timeout: float = 0.0) -> list[dict]:
    """从用户日常那个浏览器里读出射程内的 cookie。

    **开一张隐藏的后台标签页,而且只开这一张。** 要用页面级的
    `Network.getCookies` 才能把范围卡在 x.com 上(见 `read_scoped`),所以这一步
    必须有一张自己的页面 —— 它是 `hidden` + `background` 的:不进用户的标签栏、
    不抢前台、生命周期跟着这条连接,断开即消失(`Session.open_page`)。
    **不列、不碰、不接管用户已有的任何标签页**,导航目标是 `about:blank`,
    一个网络请求都不发。

    **连接会让那个浏览器弹一次授权**,所以这一步只许由用户点出来(面板上那颗
    按钮),不许挂在任何自动路径上 —— 挂上去就是把「每天 9 点没人点」那个老问题
    原样搬到这儿。
    """
    from kb.feeds.cdp_attach import ATTACH_TIMEOUT, Attached
    with Attached(endpoint, timeout=timeout or ATTACH_TIMEOUT) as browser:
        browser.open_page('about:blank')
        return read_scoped(browser)


def install(cookies: list[dict], profile: str,
            chrome_path: str = '') -> tuple[str, dict]:
    """把这批 cookie 写进独立 profile,验一次,**再重开一次确认它真落盘了**。

    返回 (结论码, 最后那份状态)。三个是这一步自己的:`unsaved`(写进去了但下次
    启动时不在)、`relogin`(重开之后 X 又判未登录)、`unconfirmed`(重开那一次没
    验成,判不准);其余沿用 `verify_timeline` 那六档。**这三个只从 `ok` 来** ——
    验证本来就没过时那才是主结论,保存差异接在后面(理由见下面那段注释)。

    **写和当场验在同一个会话里。** 起一个 Chrome 要两三秒,但更要紧的是这两件事
    必须说的是同一份 profile 状态。

    **当场验过不等于下次还在,所以收尾之后再开一次。** `Chrome.__exit__` 先发
    `Browser.close` 再 kill,而 `Browser.close` 存在的理由就是让浏览器把 profile
    存盘;收尾那一步**怎么失败都不往外抛**(它必须把 fd 和进程收干净,见
    `x_cdp.Chrome._shutdown`),于是「被 kill 掉、cookie 一个都没落盘」这件事从
    调用方这儿看跟成功一模一样 —— 而面板会照旧说「以后不用再点弹窗了」。
    `closed_cleanly` 把那次关闭的结果交出来,但**判据不押在它身上**:真正的判据是
    重新打开同一个 profile 之后,**这一轮写进去的那一代**还在不在 ——
    **比指纹,不比名字**(`fingerprint`,理由和实测写在它自己那条注释里)。
    会话 cookie 那一档也由这一道兜住(`ct0` 要是会话 cookie,正常关闭之后它不在了)。

    **比的是「验证结束那一刻浏览器里的状态」,不是我们交进去的那份输入。**
    这一条是上面那条的要害,三件事靠它一起成立:
    ① **Chrome 会改写我们交进去的值** —— 实测它把有效期**削到 400 天**(交进去 5 年,
    存下来 400.0 天)。拿原始输入去比,凡是长效 cookie 都会永远判「没存上」,而 X 的
    `auth_token` 正是多年有效期:那是个把整个功能判死的假阴性。
    ② **站点在验证期间会合法地换发凭证** —— 导航到 `x.com/home` 那一下 X 自己就会
    下新的 cookie(实测见到 `guest_id`、`__cf_bm` 一类)。拿输入去比的话,一次
    **完整存好了**的导入反而被报成「ct0 还是旧值」,而用户照着那句话会反复重导。
    ③ 浏览器允许的表示差异(浮点写法、字段规范化)在快照这一侧已经发生过一次,
    两边于是同源可比。

    两次读都是**整杯**(`read_all`),所以这个比较是对称的,而且没有路径盲区:
    `ct0@/home`、`ct0@/i/api` 这类路径受限的条目一样进得来 —— 按 URL 读的那一版
    漏的正是它们,而且是**两侧同时漏**,于是「集合相等」什么都证明不了。
    """
    from kb.feeds.x_browser import verify_timeline
    from kb.feeds.x_cdp import Chrome
    with Chrome(profile, headless=True, chrome_path=chrome_path) as page:
        # 先灌 cookie 再开页面:第一次导航就带着它们,省掉一次「先掉回登录页、
        # 再刷新」的来回,也让随后那次验证读到的就是最终状态。
        page.call('Storage.setCookies',
                  {'cookies': [_param(c) for c in cookies]}, timeout=_WRITE_TIMEOUT)
        page.open_page('about:blank')
        page.goto(VERIFY_URL)
        verdict, last = verify_timeline(page)
        # **快照:验证通过的到底是哪一份状态。** 读的是浏览器,不是我们的输入 ——
        # 理由(三条,都实测过)写在这个函数的注释里。**整杯读**:路径受限的那一族
        # (`/i/api`)也要进判据,理由见 `read_all`。
        verified = read_all(page)
    # `with` 已经收尾,`page` 还在手上 —— 现在问的是磁盘上的那一份,外加「重开之后
    # X 还认不认」。
    seen = _reopened(profile, chrome_path)
    gap = _persistence_gap(verified, seen['disk'])
    if gap:
        last = dict(last, closed_cleanly=bool(page.closed_cleanly), **gap)
    # **验证本来就没过的话,那才是主结论。** 无条件改成 `unsaved` 会把「X 当场把它
    # 判成未登录」这个已经查明的原因换成一句「没存上」—— 下一步完全不同(前者该去
    # 点「打开登录窗口」,后者是再导一次),而且站点拒绝时它多半顺手清掉了凭证,于是
    # 盘上确实对不上:一次**登录被拒**就被报成一次**存盘故障**。保存差异照旧带着
    # (`_gap_why` 会把它接在后面),不丢信息、只是不占主结论。
    if verdict != 'ok':
        return verdict, last
    if gap:
        return 'unsaved', last
    # **三道判据各管一边,都过了才算成功(这一处是整条流程的提交边界)。**
    # 快照那道管「现在一样、将来还一样吗」:有效期、属性、同名多条 —— 重开那一刻
    # 真验一次是看不出来的(28 秒后才过期的 cookie 此刻照样能用)。**它比的是整杯**
    # (`read_all`):按 URL 读的那一版漏掉的是**路径**,而验证页用的可能正是
    # `ct0@/i/api`,两侧同时漏掉时集合相等什么都证明不了(审查实测在 `read_all`)。
    # 真验那道管「快照里压根没有身份的那一类」:分区 cookie、以及站点认不认这件事
    # 本身 —— 盘上几条指纹相等,不等于 X 还认它(账号被下线、风控、换了设备指纹)。
    # 第三道(下面那个 `_settled`)管「被证明的那一份,是不是最后留下的那一份」。
    # 谁都不能替代谁,所以三道都是闸。
    if (again := seen['verdict']) != 'ok':
        last = dict(last, again=again)
        # 分两档:X **明确说未登录** = 当场能用那一份没完整留下(再导一次,或者改用
        # 登录窗口);**没验成**(限流、首页没加载完、一条推文都没读到)= 判不准 ——
        # 那就不许宣布成功,也不许说成「没存上」(元规则「判不准一律扣留」)。
        # **这一档不必再开第三次**:成功已经被拒了,没有什么可证明的。
        return ('relogin' if again == 'login' else 'unconfirmed'), last
    # **第三道:刚才那次验证之后的状态,真的落盘了吗。**
    # 上面两道比的都是「最后一个写者**之前**」的状态 —— 而重开那次导航自己就是写者:
    # 站点在那一下可以合法地换发凭证,**换来的那一份才是明早采集要用的**。
    # 事故形态(第 1 轮审查用隔离 Chrome 确定性复现):第二次首页响应下发
    # `Set-Cookie: ct0=…; Secure; SameSite=Lax`(**没有有效期 = 会话 cookie**),
    # 第二次验证照样读到推文、浏览器也**正常关闭**,盘上那条 `ct0` 随进程一起没了 ——
    # 两道旧闸全过(盘上和导航前的快照一致),产品报 `ok` 并承诺「以后不用点弹窗」,
    # 而下一次打开同一个 profile 页面分类器判 `login`。
    # **所以这一道比的是「验完那一刻浏览器里的」对「这一次关掉之后盘上的」**;
    # 读盘那次**不许再导航**(再导航就又多一个写者,缺口只是往后挪一次,见 `_settled`)。
    if final := _persistence_gap(seen['live'], _settled(profile, chrome_path)):
        return 'unsaved', dict(last, final=True, closed_cleanly=seen['clean'], **final)
    return 'ok', last


def _persistence_gap(verified: list[dict], back: list[dict]) -> dict:
    """验证结束那一刻的关键凭证状态,重开之后还**完整**在不在。空 dict = 在。

    **每一条都要在,不是「任意一条同名的在就算」。** 同一个名字在浏览器里完全可以
    有好几条(`auth_token@x.com` 和 `auth_token@.x.com` 是两条不同的 cookie,
    domain/path/分区任一不同就是一条新的),而站点依赖的是它验证时**实际有的**
    那一组 —— 留住其中一条就放行,等于用「有一条同名的」冒充「那组状态还在」
    (2026-10-15 第 3 轮审查实测:刚写的 `.x.com` 那条没了,照旧报成功)。

    **反过来也要卡住:盘上不许多出验证时没有的那一组。** 子集不等于同一份状态 ——
    上一次导入留下的 `auth_token@/i/api` 这类旧条目会跟着请求一起发出去,而
    `Storage.setCookies` 按 name+domain+path 覆盖,压根不会动它。多出来的那条
    到底会不会被站点选中,我们答不了;答不了就不该宣布「保存下来的是验证过的
    那一份」(口径同元规则「判不准一律扣留」)。

    四档缺法的下一步各不一样,所以分开记:
    - `vanished`:验证刚结束时浏览器里就没有它了(站点在那一刻把它删了)。
    - `absent`:盘上压根没有这个名字 —— 多半是会话 cookie,关掉浏览器就失效。
    - `stale`:盘上有同名的,但不是验证结束时那一份(旧值、旧有效期、属性变了,
      或者少了一条)。
    - `extra`:验证那一组都在,但盘上还多出同名的别的条目。
    """
    there = {str(c.get('name') or '') for c in back}
    vanished, absent, stale, extra = [], [], [], []
    for name in ESSENTIAL:
        mine = {fingerprint(c) for c in verified
                if str(c.get('name') or '') == name}
        disk = {fingerprint(c) for c in back
                if str(c.get('name') or '') == name}
        if not mine:
            vanished.append(name)
        elif mine - disk:                  # 差一条就算没存上
            (stale if name in there else absent).append(name)
        elif disk - mine:
            extra.append(name)
    if not (vanished or absent or stale or extra):
        return {}
    return {'vanished': vanished, 'absent': absent, 'stale': stale, 'extra': extra}


def _reopened(profile: str, chrome_path: str = '') -> dict:
    """重开这个 profile:**先读盘、再真验一次、验完再读一次浏览器**。

    返回 `{'disk': 盘上那一份, 'verdict': 验证结论码, 'live': 验完浏览器里那一份,
    'clean': 这次关闭成没成}`。

    读不出来 / 起不来返回一份空的(`verdict` 是空串)—— **按「没留下」算**。这一道是用来**拦住**
    一句承诺的(「以后不用再点弹窗」),拦不住的代价是用户第二天早上才发现 X 那一段
    缺了;而误拦的代价只是他再点一次那颗按钮(元规则「判据宁可宽、宁可多拒」)。

    **返回整条而不是名字。** 判据要比的是「哪一条 + 哪一代」,压成名字就等于把
    这一道削成「磁盘上有没有同名的东西」—— 那对一次什么都没存进去的重导入照旧
    放行(见 `fingerprint`)。

    **顺序是故意的:读在导航之前。** 这一读要回答「盘上是什么」;导航到 `VERIFY_URL`
    那一下 X 自己会下新 cookie,读在它之后就把「这次加载带来的」混进「盘上原有的」
    里,于是一次合法的换发会被判成 `stale`(第 3 轮那条反过来钉的边)。

    **再真验一次,是因为指纹相等不等于站点还认。** 盘上那几条跟验证通过时逐位相同,
    也可能被 X 那头作废(账号下线、风控、分区 cookie 这类快照里压根没有身份的东西)。
    所以这一步跑的是**采集侧明早第一件事**:重开、打开首页、让 X 自己说认不认。
    (路径盲区不在这一道的射程里 —— 那一道是 `read_all` 的整杯读。)

    **验完还要再读一次浏览器(`live`),因为这次导航本身就是个写者。** 站点在这一下
    可以下发、删除、轮换 cookie,而**下一次采集要用的是这一份,不是上面 `disk` 那一
    份** —— 拿导航之前的快照去宣布「以后不用点弹窗」,证的是最后一个写者**之前**的
    状态。`clean` 跟着交出来:这一份能不能落盘,取决于这一次关闭成没成。
    这两样由 `install` 收尾那道 `_settled` 比完(理由见那里)。
    """
    from kb.feeds.x_browser import verify_timeline
    from kb.feeds.x_cdp import Chrome
    out: dict = {'disk': [], 'verdict': '', 'live': [], 'clean': False}
    try:
        with Chrome(profile, headless=True, chrome_path=chrome_path) as page:
            page.open_page('about:blank')
            out['disk'] = read_all(page)       # 盘上那一份:还没被这次加载改过
            page.goto(VERIFY_URL)
            out['verdict'], _last = verify_timeline(page)
            out['live'] = read_all(page)       # 这次加载之后:下一次采集要用的那一份
        out['clean'] = bool(page.closed_cleanly)
    except (CdpError, EOFError, OSError):
        return out
    return out


def _settled(profile: str, chrome_path: str = '') -> list[dict]:
    """最后一次开机:**只读盘,一步都不导航**。读不出来返回 `[]`(按「没留下」算)。

    **这一开机不碰网络,是判据的一部分。** 上面那次验证是整条流程里最后一个写者,
    这儿要证明的恰恰是「它留下的那一份落到盘上了」—— 再导航一次就又多一个写者,
    同一个缺口只是被推到下一次关闭之后,永远证不完。所以这一次只开一张空页读盘。
    (事故形态与实测写在 `install` 里那道闸的注释上。)
    """
    from kb.feeds.x_cdp import Chrome
    try:
        with Chrome(profile, headless=True, chrome_path=chrome_path) as page:
            page.open_page('about:blank')
            return read_all(page)
    except (CdpError, EOFError, OSError):
        return []


@contextmanager
def _installing(profile: str):
    """把「改 profile」和「记这一轮的结论」圈成一件事,同一时间只许一个。

    `profile_busy` 那道前置检查答的是「**现在**忙不忙」—— 它是**早一道**,不是
    互斥(口径同元规则那条)。真正要串起来的是「写 cookie → 验 → 重开核验 →
    落账」这一整段:两次导入叠在一起时,profile 的 OS 独占锁在前一次 `install`
    返回那一刻就松了,而它的账还没落 —— 于是晚开工的那次可能先落账,面板上
    那行字从此说的是另一轮的事。

    拿不到锁就让调用方说「另一次正在进行」,**不等**:这是用户点出来的一次操作,
    让他对着一颗没反应的按钮等几十秒,不如直接告诉他现在有一轮在跑。

    **yield 空串 = 拿到了;非空 = 这一轮不能继续的原因(成品文案)。**
    「锁被别人占着」和「锁根本建不起来」要分开说:数据目录只读、配额满、这个文件
    系统不支持 flock,全都不是「别人在导入」—— 一律翻成那句话的话,用户会去关一个
    压根不存在的窗口,然后反复点那颗按钮(而每一次都白弹一个授权窗)。原来这几档
    直接把 `OSError` 抛给接口,最终只落成一个 500,连原因都看不到。
    """
    path = Path(str(LEDGER) + '.lock')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        yield f'建不了导入锁文件({e})—— 看一下 {path.parent} 还写得进去吗'
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # 这一档才是真的「有人在跑」(`EWOULDBLOCK`/`EAGAIN`)。
            yield '另一次导入正在进行中 —— 等它结束再试'
            return
        except OSError as e:
            # 别的 `OSError`:不支持锁、I/O 错误、锁资源耗尽 —— 都不是并发。
            yield (f'拿不到导入锁({e})—— {path} 所在的文件系统'
                   f'可能不支持文件锁')
            return
        try:
            yield ''
        finally:
            # **收尾自己抛错不许盖掉业务结果。** 这两步跑在 `finally` 里,而
            # `finally` 里冒出来的异常会**替换掉**调用方那条 `return` —— 于是一次
            # 本来有话可说的导入(成功、或者带着下一步的失败)在接口上变成一个
            # 没有原因的 500。锁和 fd 的归宿本来就不依赖这两句:进程退出时内核
            # 一并收走,而这把锁的射程只到本次导入为止。所以照旧**都试一次**,
            # 失败咽下去(口径同 `x_cdp.Chrome._shutdown`「两步都有界、都不往外抛」)。
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _write_ledger(verdict: str, count: int, names: list[str]) -> str:
    """留痕。返回写失败的原因(成功是空串)。

    **只记名字,不记值。** cookie 值就是登录凭证本身,它只该待在那个 profile 的
    加密库里;落进一份明文 JSON 等于把这个功能防的事自己做一遍。
    """
    try:
        write_config_file(LEDGER, {
            'at': datetime.now(_SHANGHAI).strftime('%Y-%m-%d %H:%M:%S'),
            'verdict': verdict, 'cookies': count, 'names': sorted(names)})
    except OSError as e:
        return f'导入记录没写进磁盘({e})'
    return ''


def state() -> dict:
    """那行字的原料。三档要分清,**最后一档不许降级成第一档**。

    - **从没导过**(文件不在):`{'known': True, 'at': ''}`。这是**确定**的事实。
    - **导过**:带上时刻、当场验成什么样、搬了几个。
    - **记录读不出来**(权限、写坏、不是 JSON):`{'known': False, 'problem': ...}`
      —— 读不出来的正确答案是「不知道」,拿它冒充「从没导过」是在报一个没查到的
      结论(口径同 `config.read_config_file`)。
    """
    try:
        data = read_config_file(LEDGER, '登录态导入记录')
    except ScheduleUnreadable as e:
        return {'known': False, 'problem': str(e)}
    count = data.get('cookies')
    return {'known': True, 'at': str(data.get('at') or ''),
            'verdict': str(data.get('verdict') or ''),
            'cookies': count if isinstance(count, int) else 0}




def summary() -> str:
    """面板上那一行人话。空串 = 没什么可说的(从没导过)。

    **文案在服务端拼。** 结论码是这一块自己的词汇表,前端照着它写一张表就等于把
    同一个判断放到两处(口径同 `x_channel.js` 顶上那条「不许把通道和它们各自要填
    什么写死成一张表」)。
    """
    got = state()
    if not got['known']:
        return got['problem']
    if not got['at']:
        return ''
    tail = _VERDICT_TAIL.get(got['verdict'], '')
    body = f"上次从日常浏览器导入：{got['at']}"
    if got['cookies']:
        body += f"，{got['cookies']} 个 cookie"
    return f'{body}（{tail}）' if tail else body


def import_login_state(profile: str, chrome_path: str = '') -> tuple[bool, str]:
    """整条流程:前置检查 → 读 → 登记 → 写 → 当场验 → 重开核验 → 落账。

    返回 (验成了没有, 成品文案)。

    **不抛**(口径同 `x_browser.probe`):每一档失败都有自己该说的下一步,翻成一句
    笼统的「导入失败」等于把人卡在原地。

    **返回的 `True` 只代表「验证通过、而且确认落盘了」。** 其余几档一律 `False`
    并说清是哪一档 —— 把「写进去了」当成「可以用了」,下一个发现真相的时刻就是
    第二天早上那一轮同步。
    """
    with _installing(profile) as blocked:
        if blocked:
            return False, blocked
        return _run_import(profile, chrome_path)


def _run_import(profile: str, chrome_path: str) -> tuple[bool, str]:
    """`import_login_state` 的本体,锁已经在手上。"""
    from kb.feeds import cdp_attach
    from kb.feeds.x_cdp import profile_busy
    # 两道前置检查判的是同一件事「现在这件事做不做得了」,但方向相反:一头是
    # 「读得到吗」(日常浏览器开着调试开关没有),一头是「写得进吗」(独立 profile
    # 有没有被登录窗口或正在跑的同步占着)。少一道的后果都是跑到一半才失败,而
    # 那时用户已经点过授权弹窗了。
    if cdp_attach.discover() is None:
        if problem := cdp_attach.explicit_problem():
            return False, f'{problem}。{cdp_attach.ENDPOINT_HINT}'
        return False, ('没有发现开着远程调试的浏览器 —— 这一步要从它里面读登录态。'
                       + cdp_attach.ENABLE_HINT)
    if profile_busy(profile):
        return False, ('独立 profile 正被另一个窗口占着（登录窗口还开着？'
                       '或者正在同步）。关掉它、等这一轮同步结束再试')
    try:
        cookies = read_from_daily_browser()
    except CdpError as e:
        return False, str(e)
    except (EOFError, OSError) as e:
        return False, f'读登录态时浏览器会话中断：{e}'
    names = {str(c.get('name') or '') for c in cookies}
    if missing := [n for n in ESSENTIAL if n not in names]:
        # **拿到了东西却没有那几个关键的 → 一个字都不往 profile 里写。**
        # 写进去的后果是目标 profile 里躺着一份残缺的登录态:`readiness` 看到
        # Cookies 文件就报「登录过」,而真抓的时候才掉回登录页 —— 而这一下还会
        # **覆盖掉**这个 profile 里原来那份可能好着的登录态(口径同元规则
        # 「拿不准就别固化终态」)。
        return False, (f'那个浏览器里没有 X 的登录态（{"、".join(missing)} 都没拿到）'
                       f'—— 先在它里面打开 x.com 登录一次，再回来导入')
    # **动 profile 之前先登记这一轮。** 从下一行起 profile 随时可能已经变了,
    # 而账本上原来那条说的是上一轮 —— 中途抛异常时不登记的后果,是面板继续
    # 展示一句关于已经不存在的那份登录态的「当场验证通过」。
    if note := _write_ledger('writing', len(cookies), list(names)):
        # **登记不上就不动 profile。** 这是唯一能让「账本不比 profile 旧」这条
        # 不变量在磁盘坏掉时仍然成立的做法:什么都没改,原来那条记录照旧准确。
        return False, f'{note} —— 记录写不进去就不动 profile，先腾出磁盘空间再试'
    try:
        verdict, last = install(cookies, profile, chrome_path)
    except (CdpError, EOFError, OSError) as e:
        # **profile 可能已经被改了一半。** `Storage.setCookies` 抛异常不等于
        # 一个 cookie 都没写进去,所以这里不许说「原样没动」—— 账本留在
        # `writing` 那一档,面板如实说「这一轮没走完、状态不确定」。
        broke = '浏览器会话中断' if isinstance(e, (EOFError, OSError)) else '出错'
        return False, (f'写进独立 profile 时{broke}：{e}。'
                       f'这一轮没走完，profile 里那份登录态状态不确定，再导入一次')
    note = _write_ledger(verdict, len(cookies), list(names))
    return verdict == 'ok', _detail(verdict, len(cookies), last, note)
