# -*- coding: utf-8 -*-
"""聚合型订阅的正文补抓:feed 只给一个壳,就去原文链接把正文取回来。

HN Buzzing 那类来源每条正文长这样 —— 标题、域名、发帖时刻、`↑ 101 HN Points`,
一共几十个字符。按它入库的话:vault 里那篇 md 没有内容,全文检索搜不到文章里
的任何一句话,提取打标拿这几十个字判不出实体,日报素材里只剩一行标题。订阅了
它等于只订阅了一份链接清单。

**这一步排在翻译之前**(见 `kb.collectors.feeds.FeedCollector.materialize`)。
顺序反过来的代价是实打实的:先按壳译一遍、补上正文再译一遍,同一条内容买两次
模型调用,而第一次买的那份译文随后就被丢掉。所以还排在抓取队列里的条目
**既不翻译也不导出**(`translate.queued_sql` 和 `sync._export` 各认一次本模块
的 `queued_sql`),等正文落下来再一次性走完后面那一段。

**开关是订阅级的,默认关**(`subscriptions.fulltext`)。只有链接聚合源需要它:
Lilian Weng、Karpathy 那类 feed 本来就给全文,对它们再抓一次是拿一份启发式
提取的结果去换作者亲手给的全文 —— 纯粹的降级。

**抓不到不挡路**:壳照旧留着、条目照旧入库,`fulltext_tries` 计次,试满
`MAX_TRIES` 就离开队列(口径同 `translate`)。一个付费墙后面的链接不该让这条
订阅的内容永远进不了知识库。**而「原文链接后面压根不是网页」要认出来**
(`readable.html_problem`):一份 PDF 论文解码出来是一段看着像文字的乱码,提取器
眼里它就是"整篇正文",长度还轻松过关 —— 不认的话补抓报成功、条目离开重试队列、
接着拿这堆乱码去买一次翻译,而且再也不会重试。

**「这一页能不能被确认为正文并写进库」那道准入和手动收录共用一份**
(`grab_page.admit`:提取 + 关卡,连先后顺序一起)。一张关卡页/登录墙自己也有
文字,够长就一路过关 —— 而这条链路写进去就是终态(`fulltext_at` 一落,条目
退出重试队列,`put_items` 的 `DO NOTHING` 之下再也换不回来)。拒绝的那一下
**一个字节都不改**:壳留着、`fulltext_at` 不写、`fulltext_tries` 照旧计次。
**而存量里已经被写死的那些,这条准入救不回来**:复查脚本
(`scripts/feed_fulltext_recheck.py`)只认 `readable.boilerplate_reason`,**这八道一道
都不借** —— 库里只剩那份纯文字,认出一张关卡页要的地址、DOM 和响应头当时就没存,拿
纯文本启发式去补那一刀,两轮审查各实测到一次误删正常内容(⑥ 判坏 Markdown 原文、
④ 判坏英文教程)。那一类的出路是人点「重新抓一次」。生产只读实测说**存量的文字里
找不出一条可识别的关卡提示**(份额最高 0.0)—— 但那推不出「存量里没有关卡页」:只靠
DOM 组件认出来的那一类,正文和普通文章逐字相同、份额同样是 0。理由和数字在那个脚本的
`_not_article` 头上。

**正文从壳换成全文之后,知识库里那份按旧壳算出来的提取结果要跟着作废**
(`retire_stale`,由 `sync._export` 在导出之后调一次)。`documents.extracted_at`
非空意味着这一篇永远不进 `extract.pending_docs`,而重导出又会把旧 `tags` 原样
写回 frontmatter —— 不作废的话摘要、主题、实体永久停在那条链接上。时点是
**新正文真的落了盘**,不是"网络抓到了":中间还隔着翻译那一段队列。
"""
from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor

from kb.feeds import grab_page, readable, registry as reg
from kb.feeds.exclusion import failure
from kb.feeds.fetching import FeedError, Gated, SOURCE_SECONDS, request
# 「这一行此刻是哪一版」那份口径摆在 `grab_store` 里(两个版本身份 ——
# 内容身份 `item_version` 和工作身份 `work_version` —— 要摆在一起对照着读),
# 本模块和手动重抓那条链共用它。再导出一次,让 `fulltext.work_version` 照旧能用。
from kb.feeds.grab_store import work_version  # noqa: F401
from kb.pipeline_state import _state

# 一轮补抓多少条。存量几百条壳的话一次全抓会把一次同步拖很久,而且每抓一条就
# 多一条待翻译 —— 分批消化,每轮啃一截。
MAX_ITEMS = int(os.environ.get('KB_FULLTEXT_MAX_ITEMS', '20'))
# 整轮的墙钟预算。单条由 `fetching.request` 自己的 SOURCE_SECONDS 兜底,这一道
# 管的是"20 条都慢"那种情况:materialize 后面还排着翻译、导出、索引。
ROUND_SECONDS = float(os.environ.get('KB_FULLTEXT_SECONDS', '300'))
WORKERS = 3
MAX_TRIES = 3

# 抓的是网页不是 feed,Accept 要换 —— `fetching.request` 的默认头是 RSS 优先,
# 有些站会据此回一份 XML 或者干脆 406。
_HEADERS = {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en;q=0.9,zh-CN;q=0.8,*;q=0.5'}


class Unconfirmed(FeedError):
    """**证据不足**:这一页既没认出谁在拦、也确认不了它就是这一篇的正文。

    分档在 `fetch_pending`:它额外把这半句话记进 `fulltext_hold`,让额度用完之后
    这一条**留得下痕迹**(`held()`),而不是和「对方真拦着」一样静默退回摘要。

    **做成 `FeedError` 的子类是给将来的消费方留的安全默认,当前没有可观测后果** ——
    `fetch_one` 眼下唯一的生产消费方就是 `fetch_pending`,那里捕的是
    `except Exception`,所以子类与否都走同一条 `_fail`。如实记着这一点是因为反过来
    也有诱惑:将来谁 `except FeedError` 地消费它,漏掉这一档就该退化成「当成普通
    失败」(不写库、计次,安全的那一侧),而不是让异常逃出去带掉整轮。

    **为什么它不能和「确定关卡」合成一档**:后者是一句关于别人的事实断言(「对方
    拦着你」),而这一档的全部证据只是正文里那段字的形状 —— 一篇**真·讲拦截页**的
    技术文章在文字上和一张真的拒绝说明页不可分离(`grab_page.verdict` 的 B 档)。
    把「我确认不了」说成「对方拦着你」,面板上那句话就是在替用户下一个我们并不
    知道的判断,而他照着去「在浏览器里打开一次」只会发现那一页好得很。
    """


def _eligible(ref: str = 'feed_items.key') -> str:
    """**「这一条现在还属于某条开着补抓的订阅」—— 补抓这一族判据只有这一份。**

    一条内容可能同时属于聚合源和单号源,**任意一条开着就算有资格**(补到的正文对
    另一条同样有用);反过来,关掉最后一条开着补抓的归属、或者把订阅整条删掉
    (`registry` 删订阅时刻意保留已收录的条目),这一条就再也不该等全文了。

    **抽成函数是因为它曾经只补在三个消费方身上、漏掉了第四个。** 先补的是
    `held()` / `release()`,后来实测到的是 `unsettled_sql()` 的挂起那半边没有:
    于是关掉补抓之后那一条落进一个**没有出口的夹缝** —— 翻译那边当它「正文还没
    定下来」永久排除(英文摘要永远不译成中文),待办那边当它「没资格」不列出,
    人工恢复也以 `released=0` 拒绝。三处各判一遍就会这样,所以元规则那条「口径
    只有一份」在这里不是风格问题。

    `ref` 只有两个取值、全写死在本模块里(`feed_items.key` 给拼进别人 WHERE 的那
    几段,`fi.key` 给 `held()` 自己那条带别名的 SELECT)—— **不接外部输入**。
    """
    return ("EXISTS(SELECT 1 FROM subscription_items si JOIN subscriptions s"
            f" ON s.id=si.subscription_id WHERE si.item_key={ref} AND s.fulltext=1)")


def queued_sql() -> tuple[str, list]:
    """"还排在正文补抓队列里"的判据,只有这一份。

    四个条件:①它属于某条开了补抓的订阅(一条内容可能同时属于聚合源和单号源,
    任意一条开了就抓 —— 补到的正文对另一条同样有用);②还没抓到过
    (`fulltext_at` 空);③**没有挂起**(`fulltext_hold` 空);④确定失败的重试
    额度没用完。

    **③ 和 ④ 是两件不同的事,这是审查第 1 轮点出来的那条。** ④ 是「对方真的
    拦着/真的连不上,试到第三次就认了」的预算;③ 是「我**没能判断**这一页是不
    是正文」—— 后者一次就挂起、不扣 ④ 的预算,因为再敲同一扇门不会让我们
    知道得更多,而扣掉预算的后果是它三轮之后**静默**退回摘要、面板上再没有任何
    痕迹。挂起的出路是人看一眼(`held()` / `release()`),不是再试两次。

    队列的出口因此有两个:挂起(等人)和 ④ 试满 `MAX_TRIES`(按 feed 原本那份壳
    走完翻译和导出 —— 留着原文比把它永远挡在知识库外面强)。

    `feed_items.key` 写全名是因为这段会被拼进别人的 WHERE 里(`registry.items`、
    `sync._export`),那些语句的 FROM 子句就是 feed_items,不带别名。
    """
    return (f"{_eligible()} AND fulltext_at='' AND fulltext_hold=''"
            " AND fulltext_tries<?"), [MAX_TRIES]


def unsettled_sql() -> tuple[str, list]:
    """"这一条的正文还没定下来" —— **翻译那道闸门问的是这个,不是「还排着队吗」**。

    两者差一个挂起态:挂起的条目**不在**补抓队列里(它等的是人,不是下一轮同步),
    但它手里那份正文仍然是 feed 给的跳转壳,人点一次 `release()` 就可能被整条换
    掉。拿 `queued_sql` 当翻译闸门的后果实测到过(就在把挂起做出来的那一轮):那
    一条一挂起就当场进了翻译队列,壳被译一遍、按文章导出,而人点一次恢复、正文真
    补上之后 `_save` 把那份译文丢掉重译 —— **同一条内容买两次模型调用**,翻译是
    这条链上最贵的一步。

    **导出那一侧刻意不用它**:挂起的条目照旧按壳导出,和「试满额度降级出队」完全
    一样的待遇 —— 留着原文比把它永远挡在知识库外面强,而正文后来真补上时
    `content_rev` / `extract_stale` 会让它重新导出。

    **资格那一道排在整个并集外面,不是只排在「还排着队」那半边。** 写成
    `queued_sql() OR (at='' AND hold<>'')` 的后果实测过:关掉补抓(或者删掉最后一条
    开着补抓的归属)之后,左半边因为没资格变假、右半边只看 hold 仍然为真,于是这一条
    **永久**被判成「正文还没定下来」—— 英文摘要再也不译成中文,而 `held()` /
    `release()` 那两道已经按资格把它排除了,面板上看不到、人点恢复也拒绝。一条
    答不出任何出口的夹缝。判据同源见 `_eligible`。
    """
    return (f"{_eligible()} AND fulltext_at=''"
            " AND (fulltext_hold<>'' OR fulltext_tries<?)"), [MAX_TRIES]


def pending(kind: str) -> int:
    cond, args = queued_sql()
    with reg.db() as c:
        return c.execute(f'SELECT count(*) FROM feed_items WHERE kind=? AND {cond}',
                         (kind, *args)).fetchone()[0]


def fetch_one(url: str) -> tuple[str, str]:
    """抓一个原文链接,返回 `(正文纯文本, 读不懂的原因)`。

    抓得到页面但读不懂正文时正文是空串;**那时原因只有样板区这一档给得出来**
    (版权声明、作者简介),别的读不懂没有判据可言,两个都是空串 —— 调用方据此
    在通用文案和具体原因之间二选一。

    走 `fetching.request`,所以 SSRF 防护(只连公网地址)、重定向逐跳校验、
    8 MB 上限、超时这几件事都已经有人管了 —— 这里只是换一份 Accept。

    **先问"这是网页吗",再解码**(`readable.html_problem`)。不是网页的一律抛
    出去按抓取失败处理:PDF 字节解出来是一段 `%PDF-1.4 1 0 obj …` 加几百个
    替换字符,长度轻松越过 `readable.MIN_CHARS`,于是"补抓成功"、条目离开重试
    队列、还拿这堆乱码去买一次翻译(见 `html_problem` 的注释)。抛而不是返回
    空串,是因为空串那条路的提示写的是"可能需要登录或由脚本渲染",而这里的
    原因明确得多 —— 面板上要说得出是哪一类。

    **「拿到一页东西」不等于「拿到了这一篇的正文」,所以写库那道准入和手动收录
    共用一份**(`grab_page.admit`:提取 + 判据,连先后顺序一起)。原先这里直接
    拿 `extract_or_reason` 的结果当正文,**八道准入一道都不过** —— 后果是一张
    关卡页/登录墙被 `_save` 写进 `feed_items.body`、写上 `fulltext_at`、**退出
    重试队列**,而 `put_items` 的 `DO NOTHING` 之下再也换不回来(审查实测:
    1453 字节、一行脚本都没有的 Cloudflare 关卡页,`done=1`、正文被换成
    「Verify you are human…」、`pending` 归零)。

    **这一次响应的响应头要原样交给准入**(`headers=`):① 那一道
    (`page_gate.response_reason`)是唯一一道不靠页面文字的判据,而这个函数手里就有
    整个 `Response` —— 不传等于在两个调用方之间留一道各自实现的缝。**不传状态码**:
    关卡身份只认关卡决策头,429 / 503 / 403 说的是别的事(见下面那条和
    `page_gate.HEADERS`)。

    **两档拒绝都抛,但抛的不是同一个异常**,理由同上面 `html_problem` 那一档
    (那半句话明确得多,而「抛」这条路保证了 `fetch_pending` 走 `_fail` —— 壳一个
    字节不动、`fulltext_at` 不写):
    - **确定关卡**(对方拦着你)抛 `Gated`,留在原重试语义里 ——
      `fulltext_tries` 照旧计次(不计次它就永远排在队首、每轮重抓同一张关卡页),
      试满 `MAX_TRIES` 按壳入库,口径同「读不懂」那一档。
    - **证据不足**(我没能确认哪一段是正文)抛 `Unconfirmed`。它**不计次、一次就
      挂起**:那半句话记进 `fulltext_hold`,`queued_sql` 把它排除,等人看一眼
      (`held()`),人点一次 `release()` 放回队列。再敲同一扇门不会让我们知道得
      更多,而扣掉确定失败那份预算的后果是三轮之后**静默**退回摘要(审查第 1 轮
      实测:四轮 materialize,第三轮之后 `pending` 归零、面板上再没有任何痕迹)。

    **响应身份那一道不在这里**(`fetching.request` 认出来就抛 `Gated`,排在任何
    格式分派之前)。放在这里问的后果实测过:上面那两个 Markdown 早返回会绕过它,
    带 `cf-mitigated` 的 `text/markdown` 响应把 492 字拒绝说明收成了正文。
    """
    from kb.feeds.markdown_source import accept, fetch, from_response, target
    deadline = time.monotonic() + SOURCE_SECONDS if target(url) else None
    if markdown := fetch(url, transport=request, deadline=deadline):
        return markdown, ''
    response = request(url, accept(_HEADERS), **({'deadline':deadline} if deadline is not None else {}))
    if markdown := from_response(response):
        return markdown, ''
    if problem := readable.html_problem(response.body, response.headers):
        raise FeedError(problem)
    seen = grab_page.admit(response.url or url,
                           readable.decode(response.body, response.headers),
                           headers=response.headers)
    if seen.gate:
        raise Gated(seen.gate)
    if seen.unsure:
        raise Unconfirmed(seen.unsure)
    return seen.body, seen.why


def body_hash(body: str) -> str:
    """`feed_items.content_hash` 在这条路上的口径。

    抽出来是因为**存量复查脚本也要换 body**(`scripts/feed_fulltext_recheck.py`
    把已确认的样板文字换回壳),而 `content_hash` 决定「导出闸门开不开」——
    两处各写一遍 sha256 的话,哪天这里改了口径,那边换出来的 hash 就和重抓
    之后的对不上,一条内容会被白白多导出一轮。
    """
    return hashlib.sha256(body.encode()).hexdigest()


def _save(key: str, body: str, seen: tuple[str, int]) -> bool:
    """正文落库。顺带把这一条**重新排进翻译队列**。返回「写进去了没有」。

    **判据带上取样时那一版的身份**(`seen` = `work_version(item)`,那一刻的
    `content_hash` + `content_rev`):这一步跑在
    `ThreadPoolExecutor` 里、网络等待期间谁都可能换掉这一行 —— 手动收录那颗
    「重新抓一次」就跑在 HTTP 请求线程里、**不持这一轮同步的维护锁**
    (`grab.replace`)。无条件 `WHERE key=?` 的后果实测过:用户刚把一篇登录提示
    页换成真正文,这一轮迟到的补抓把它又盖回旧内容,导出和索引跟着落盘,最后
    面板说「这条已经在知识库里了」(审查用 Event 卡在补抓返回处确定性复现)。
    `rowcount == 0` 不是失败,是「我取样那一版已经不在了,这次结果作废」
    (元规则:0 行不是失败)。

    **比的是整条内容的版本,不是 `AND body=?`。** 「正文还一样」答不出「这一行
    还是我取样那一版吗」:替换那条路的版本身份是**整份导出负载**
    (`grab_store._VERSION_FIELDS` —— 标题、正文、作者、发布时间),于是一次
    **只改标题**的手动重抓换掉了 `content_hash` 却留着同一份正文,迟到的补抓按
    正文相等照旧获得覆盖权。后果实测过:用户重抓到的那一版标题还在、正文却变成
    补抓那一轮抓回来的另一份内容,md 和全文检索跟着落了旧正文,再贴同一链接只
    说「已经在知识库里了」。**一边按四列认定「这是新的一版」、另一边按单列相等
    认定「还是旧那一版」就是两套口径**(元规则:口径只有一份)。

    **而「正文、标题、作者、日期逐字相同」也答不出这一问** —— 内容指纹
    **可以重复**:改过去再改回来之后 `content_hash` 回到原值,而这期间两次更新都
    已经被接受。所以身份是两道一起:`content_hash` + 只增不复用的 `content_rev`,
    口径和两道各挡什么都在 `work_version` 那份说明里。比的都是库里那两列的
    **原值**(三份 hash 口径算的东西不同、不许互相比较 —— 见
    `grab_store.item_version`);而「只清导出回执」(`reclaim_export`)和「译文
    写回」(`translate._save`)两列都不动,**不会被误判成内容变过**。

    旧译文译的是那份壳,正文一换它就作废了 —— 留着的话导出会把"壳的译文"当成
    这篇文章的中文正文写进 vault,而 `translated_at` 非空意味着再不会有人去
    订正它。所以 `title_zh`/`body_zh`/`translated_at` 一起清掉,当一条新内容
    重译;`translate_tries` 同理归零,新正文值得一份新的重试额度。

    `content_kind` 从 `summary` 改成 `content`:导出那边据它决定要不要写
    「以下为订阅源提供的摘要」那句注记,而现在这就是全文了。

    **`fulltext_hold` 一起清掉**:那一列是「上一次的结论是证据不足,等人看一眼」
    这个待办,而现在正文已经确认到手了 —— 留着它会让 `held()` 把一条**已经补到
    全文**的条目列成待办,人点一次 `release()` 还会把它推回补抓队列白抓一轮。

    **重导出靠 `content_hash` 变了,不许清 `exported_hash`。** 导出闸门是
    `exported_hash<>content_hash`,正文一换前者就自然成立 —— 而 `exported_hash`
    同时是「这一篇入没入过库」的事实来源(`registry.items` 的两态、`sync._export`
    的四档都读它)。清掉它的代价实测过:一篇**已经能展开阅读**的旧条目补抓成功
    之后,面板上标题连同「展开阅读」一起消失、换成一句"译成中文之后才写进知识
    库",而 vault 里那份 md 好好地躺着 —— 而且这不是一瞬间的事,翻译排队或失败
    时它会一直这样(2026-10-09 审查实测 + 截图)。

    `extract_stale` 置 1 = 留一个待办:知识库里那份摘要/主题/实体是按**旧壳**
    算出来的,新正文落盘之后要让它作废(见 `retire_stale`)。
    """
    digest = body_hash(body)
    with reg.db() as c:
        row = c.execute('SELECT title FROM feed_items WHERE key=?', (key,)).fetchone()
        if not row:                      # 这期间被存量清理删掉了,没什么可写的
            return False
        cur = c.execute("""UPDATE feed_items SET body=?,content_kind='content',content_hash=?,lang=?,
            fulltext_at=?,fulltext_error='',fulltext_hold='',extract_stale=1,
            content_rev=content_rev+1,
            title_zh='',body_zh='',translated_at='',translate_error='',translate_tries=0
            WHERE key=? AND content_hash=? AND content_rev=?""",
            (body, digest, reg.text_language(row['title'], body), reg.now(), key, *seen))
        return cur.rowcount > 0


def _fail(key: str, error: Exception | str, seen: tuple[str, int],
          hold: str | None = None, *, count: bool = True) -> bool:
    """这一条补抓失败了。返回「记上了没有」。

    **判据同 `_save`,理由也同**:这次失败说的是**取样那一版**(`seen`)去原文
    链接没抓到。等待期间那一行被换掉时 —— 哪怕换的只是标题、哪怕改过去又改回来
    使得内容逐字复原 —— 把错误记在新那一版头上、还扣掉一次重试额度,是在给一件
    已经不存在的事记账。**这里不碰 `content_rev`**:记一笔失败不是换内容,别人
    手上的工作资格不该因此作废。

    `hold` 三个值,对应三件不同的事 —— **「这一次的结果」和「那个悬着的问题」
    是两回事,这是审查第 1 轮点出来的那条**:
    - **非空** = 这一次判出**证据不足**(`Unconfirmed`):那半句话写进
      `fulltext_hold`,这一条就此挂起(`queued_sql` 把它排除),等人看一眼;
    - **`''`** = 这一次**查清了**(`Gated`:对方自己说它拦了这一次请求)。悬着的
      问题有答案了,待办跟着清掉 —— 否则 `held()` 里会攒下一堆「其实已经查清是
      对方拦着」的行,而那正是让人不再去看那个列表的东西;
    - **`None`(缺省)** = 这一次**什么都没查清**:超时、连不上、对方 500、
      拿到一页但提取器交了白卷。那一列**原样不动**。上一版这一档也写空串,于是
      「前两轮说看不出是不是正文、第三轮刚好超时」的条目待办被抹掉、人再也看不到
      它(审查实测:超时和空页面两种第三次,hold 都归空、`held()` 变空列表、站点
      恢复之后第四轮一次请求都不发)。**一次尝试的结果不能充当正文身份的最终
      结论** —— 超时只说明这一次没拿到。

    `count=False` 只给挂起那一档用:它不扣确定失败的预算(理由在 `queued_sql`)。
    """
    sets = ['fulltext_error=?']
    args: list = [str(error)[:600] or type(error).__name__]
    if hold is not None:
        sets.append('fulltext_hold=?')
        args.append(hold[:600])
    if count:
        sets.append('fulltext_tries=fulltext_tries+1')
    with reg.db() as c:
        cur = c.execute(f'UPDATE feed_items SET {",".join(sets)}'
                        ' WHERE key=? AND content_hash=? AND content_rev=?',
                        (*args, key, *seen))
        return cur.rowcount > 0


def held(kind: str) -> list[dict]:
    """**挂起等人看一眼**的那几条:判出了证据不足,所以自己不会再抓了。

    这是「可恢复状态」那一半:上一版里这一类和「对方真拦着」走同一个出口,试满
    `MAX_TRIES` 之后**静默**离开队列、按壳入库,面板上再也没有任何痕迹 —— 而它和
    真关卡的区别恰恰是**我们并不知道**那一页是什么。所以这一列留着:这一条答得出
    「上一次为什么没收下」,人点一次 `release()` 就放回队列。

    **判据三条都要,而且和 `queued_sql` 第一条同源**:
    - `fulltext_hold<>''` —— 挂着一个没答案的问题;
    - `fulltext_at=''` —— 还没补到全文。`_save` 成功时会把 hold 清掉,这一条是
      冗余带:两处口径不一致时宁可少列一条,列出一条已经补好的只会让人白点一次;
    - **这条订阅现在还开着补抓** —— 不然 `release()` 把它放回队列是句空话
      (`queued_sql` 第一条会把它挡在外面),而面板上却显示「已恢复 1 条」。
      关掉补抓就是「这条订阅的正文我不要了」,待办跟着消失才是诚实的;**也不许
      反过来替用户把那个开关打开**。

    **不再要求额度用完。** 挂起这一档压根不扣额度(`_fail` 的 `count=False`):它
    一次就离开队列,等的就是人 —— 要求「试满三次才算待办」会让它永远不出现。
    """
    with reg.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT key,url,title,fulltext_hold,fulltext_tries FROM feed_items fi"
            f" WHERE kind=? AND fulltext_hold<>'' AND fulltext_at='' AND {_eligible('fi.key')}"
            " ORDER BY fetched_at DESC, key", (kind,)).fetchall()]


def release(keys, kind: str | None = None) -> int:
    """**人工出口**:把补抓额度还给这几条,放回队列。返回真放回去了几条。

    **判据待在 UPDATE 自己的 WHERE 里**(元规则):`fulltext_at=''` —— 这一下和
    kb-web 那边正在跑的补抓 worker 是两个写入方,先 SELECT 再 UPDATE 在
    legacy `isolation_level` 下压根不持锁。这期间那一条已经补到全文时放行不了,
    `rowcount==0` 就是「不用管了」(元规则:0 行不是失败,多半正是想要的末态)。

    **「这条订阅还开着补抓」也在同一个 WHERE 里,判据和 `held()` 同源。** 关掉补抓
    之后放行是句空话:`queued_sql` 第一条会把它挡在队列外面,而这里却报了「已恢复
    1 条」 —— 答不上「那它什么时候再抓」的承诺不如不给。**不许替用户把那个开关
    打开**:关掉补抓就是「这条订阅的正文我不要了」。

    **清的是额度和待办,一个字节的正文都不碰。** 这一列底下躺的是「壳还在、全文
    没拿到」的条目 —— 要换的只是「还能不能再试一次」。`fulltext_tries` 一起清 0:
    一条可能先网络失败两次、再判出证据不足挂起,只清 hold 的话它回到队列立刻就撞
    上满额度、又一次静默退回摘要。`fulltext_error` 一起清掉:留着它面板上会显示
    一条已经作废的失败原因。

    **已经在路上的那一轮补抓不受这一下影响,交错的后果就是人可能要再点一次。**
    `_fail` 比的是内容身份(`work_version`:hash + rev),而 `release` 不碰内容,
    所以一个在 `release` 之前就发出去的请求回来之后照旧写得进去 —— 它会把 hold
    重新写上(证据不足)或者扣一次额度(确定失败)。**这是知情的取舍**:要挡住它
    得给「人发起的这一轮」一个不可复用的代次,而那是一列新字段加一道新不变量;
    而它的后果只是「那一条又挂回去了,人再点一次」,不丢任何数据。

    **`fulltext_hold<>''` 也在 WHERE 里,否则返回的那个数是句谎话。** 元规则:统计按
    **实际写入结果**报。不带这一条时同一个 key 给三次会报 `released=3`(三次 UPDATE
    都匹配得上,因为那几列已经是目标值了,SQLite 照样算 1 行),已经恢复过的、压根
    没挂起过的也一样报「恢复成功」—— 而真放回队列的只有一条。**同一个 key 在一次
    调用里重复出现由同一条判据覆盖**(第一次把 hold 清掉,后面几次就不匹配了),所以
    这里不另做一次去重 —— 加过,反向验证红 0 条,两段代码做的是同一件事。

    **`kind` 是可选的来源范围,给 HTTP 那个调用点用。** 路由上的 `kind` 过了合法性
    校验,不等于请求体里每个 key 都属于它:实测把网页专栏那一条挂起文章的 key 提给
    RSS 的恢复端点,它照样被恢复、而 RSS 自己那条还挂着。范围要约束**写入本身**
    (元规则:判据待在写语句自己的 WHERE 里)。CLI 那一侧刻意不传 —— 它的
    `--release <key>` 文档写明按 key 跨来源,那是有意的契约。
    """
    done = 0
    with reg.db() as c:
        for key in keys:
            sql = ("UPDATE feed_items SET fulltext_tries=0,fulltext_hold='',fulltext_error=''"
                   f" WHERE key=? AND fulltext_at='' AND fulltext_hold<>'' AND {_eligible()}")
            args: list = [key]
            if kind is not None:
                sql += ' AND kind=?'
                args.append(kind)
            done += c.execute(sql, args).rowcount
    return done


def fetch_pending(kind: str, limit: int | None = None, progress=None) -> dict:
    """把这个来源里待补抓的条目逐个去原文链接取正文。返回摘要给调用方汇报。

    **失败过的优先**(`ORDER BY fulltext_tries DESC`),理由同
    `translate.translate_pending`:按升序排的话,重试永远排在"从没试过"的后面,
    几百条积压下要几十轮同步才轮得到一次重试,而面板每一轮都在报同一个数字。
    `key` 收尾是为了定序 —— `fetched_at` 只精确到秒,一批抓回来的条目时间戳
    一模一样。
    """
    summary = {'done': 0, 'failed': 0, 'empty': 0, 'left': 0, 'stale': 0, 'held': 0,
               'errors': [], 'failed_keys': [], 'stopped': False}
    cond, cargs = queued_sql()
    with reg.db() as c:
        rows = [dict(r) for r in c.execute(
            # `work_version` 那两列跟着取出来:它们是这一次补抓**针对哪一版**的
            # 身份,`_save` / `_fail` 的 WHERE 里比的就是它们的原值(口径和两道各
            # 挡什么见那个函数)。**不取 `body`** —— 「正文还一样」答不出「还是我
            # 取样那一版吗」,而「内容逐字相同」也答不出。
            f'SELECT key,url,title,content_hash,content_rev FROM feed_items'
            f' WHERE kind=? AND ({cond})'
            ' ORDER BY fulltext_tries DESC, fetched_at DESC, key LIMIT ?',
            (kind, *cargs, max(1, limit or MAX_ITEMS))).fetchall()]
    summary['left'] = max(0, pending(kind) - len(rows))
    if not rows:
        return summary
    deadline = time.monotonic() + ROUND_SECONDS

    def one(item):
        if _state.get('stop_requested') or time.monotonic() >= deadline:
            return item, None, '', None
        try:
            body, why = fetch_one(item['url'])
            return item, body, why, None
        except Exception as e:               # noqa: BLE001 - 逐条隔离,一个坏链接不带掉整轮
            return item, None, '', e

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='fulltext') as pool:
        for item, body, why, error in pool.map(one, rows):
            if isinstance(error, Exception):
                # **记账排在这里而不是线程里**:要按「记上了没有」统计,而
                # `_fail` 现在会在那一行已经被换掉时拒绝记(元规则:统计按实际
                # 写入结果报、跳过的如实列出)。
                # **「证据不足」照旧计进 `failed`,另外多记一笔 `held`。** 不另立
                # 一个 bucket 把它从 `failed` 里挪走:这一条确实没补到正文,面板
                # 那句「N 条抓取失败」要如实含着它,而失败明细也照旧要列出它(人
                # 要看的就是那半句话)。`held` 是**加一个说明**,不是换一种分类。
                # **三类结果对 `fulltext_hold` 做三件不同的事**(判据和实测后果在
                # `_fail` 头上):证据不足 → 写那半句话、挂起、**不扣确定失败的
                # 预算**;`Gated`(对方自己说它拦了)→ 查清了,清掉待办、照旧计次;
                # 其余(超时、连不上、对方 500)→ **什么都没查清,那一列原样不动**。
                held_say = str(error)[:600] if isinstance(error, Unconfirmed) else None
                if isinstance(error, Gated):
                    held_say = ''
                if not _fail(item['key'], error, work_version(item), held_say,
                             count=held_say in (None, '')):
                    summary['stale'] += 1
                else:
                    summary['failed'] += 1
                    if held_say:
                        summary['held'] += 1
                    summary['failed_keys'].append(item['key'])
                    summary['errors'].append(failure(item, error, kind))
            elif body is None:               # 停止请求 / 预算用完,留给下一轮
                summary['left'] += 1
                summary['stopped'] = True
            elif body:
                if _save(item['key'], body, work_version(item)):
                    summary['done'] += 1
                else:
                    # 等待期间那一行被换过了(手动重抓那颗按钮就跑在另一个线程
                    # 里)—— 这次抓到的结果作废,**不许盖回去**。
                    summary['stale'] += 1
            else:
                # 页面抓到了,但那上面认不出一块像正文的内容(付费墙、纯 JS 渲染、
                # 一张图,或者赢出来那一块判成了样板区)。按失败计次:重试三轮之后
                # 它就离开队列,按壳入库 —— 不计次的话这条永远排在队首,每一轮都
                # 重抓同一个读不懂的页面。
                # `why` 非空就用它:样板区那一档的原因是明确的,面板上要说得出是
                # 哪一类(口径同 `fetch_one` 对 `html_problem` 那段)。
                said = why or '页面上没有识别出正文，可能需要登录或由脚本渲染'
                if not _fail(item['key'], said, work_version(item)):
                    summary['stale'] += 1
                else:
                    summary['empty'] += 1
                    summary['failed_keys'].append(item['key'])
                    summary['errors'].append(failure(item, said, kind))
            if progress:
                # 作废那几条也算「处理过了」:不算的话进度条到不了头(面板上那一行
                # 会永远停在 N-1/N)。
                progress(summary['done'] + summary['failed'] + summary['empty']
                         + summary['stale'], len(rows))
    return summary


def summarize(summary: dict) -> str:
    if not any(summary[k] for k in ('done', 'failed', 'empty', 'left', 'stale')):
        return ''
    text = f"已补抓 {summary['done']} 条原文正文"
    if summary['failed']:
        text += f",{summary['failed']} 条抓取失败"
    if summary['empty']:
        text += f",{summary['empty']} 条没能识别出正文"
    if summary.get('held'):
        # **「证据不足」要在这句话里露面。** 它是 `failed` 的一个子集,所以不另报
        # 一个数 —— 报的是「其中几条我没能判断」,因为它们的下一步和别的失败不同:
        # 别的失败下一轮自己会重试,这几条**已经挂起、自己不会再抓了**,要人看一
        # 眼(`held()`,面板和 `./kb.sh held` 都从那儿读)。
        text += f"（其中 {summary['held']} 条看不出是不是正文，已挂起等人看一眼）"
    if summary.get('stale'):
        # **跳过的如实列出**(元规则):这几条在补抓期间被换成了别的内容,这次
        # 结果作废 —— 说成「失败」会让人去查对方站点,说成「成功」更糟。
        text += f"，{summary['stale']} 条在补抓期间已被换新、这次结果作废"
    if summary['left']:
        text += f",{summary['left']} 条留到下一轮"
    return text


# 旧提取结果里**按正文算出来的**那两类 tag(口径同 `extract.extraction_tags`)。
# 摘 frontmatter 和删 `doc_tags` 用的是这同一份:两处各写一遍的话,md 里摘掉的
# 和库里删掉的不是同一批,而下一轮索引按 frontmatter 重建 doc_tags —— 差额会被
# 原样补回去。`flag/软广` 和人工 tag 不在里面(见 `keep_tags`)。
STALE_TAG_PREFIXES = ('topic/', 'entity/')


def keep_tags(tags) -> list[str]:
    """正文被整篇换掉之后,这篇 md 的 frontmatter 里还该留下的那些 tag。

    摘掉 `topic/` 和 `entity/`:它们是提取阶段按**旧正文**算出来的,而旧正文是
    一个几十字的跳转壳。不摘的话重新提取时 `writeback_tags` 是**往后追加**的,
    `topic/link-only` 这种会永久留在这篇文章上。

    **不摘 `flag/软广`**:它由 promo 判定管,而重新提取会顺带重判一次 —— 改判
    时 `extract.sync_promo_tag` 自己会把 md 里那行摘掉(见 `docs/decisions/promo.md` 的硬约束一节)。
    人工写进来的 tag 同理留着:这里要作废的是"按旧链接算出来的结论",不是别人
    写进这篇 md 的东西(口径同 `sync._kept_meta`)。
    """
    if not isinstance(tags, list):
        tags = [tags] if tags else []
    return [s for t in tags if (s := str(t).strip()) and not s.startswith(STALE_TAG_PREFIXES)]


def _drop_extraction(paths: list[str]) -> None:
    """让这几篇在知识库里的旧提取结果作废,重新排进提取队列。

    清 `documents.extracted_at`(置 NULL 才进 `extract.pending_docs`)、
    `summary`、`entities`,以及 `doc_tags` 里按正文算出来的那两类。

    **不清 `promo_level`**:重新提取时 `save_extraction` 会拿新判定覆盖它,
    真改判了 `sync_promo_tag` 还会把 md 里那行标记一起摘掉 —— 在这里先清成
    `''` 只会让它多走一趟存量补判。

    kb.db 的连接现开现关:`kb/feeds` 这一侧平时压根不碰它(`store.connect` 还
    要加载 sqlite_vec 扩展),只有真有条目换过正文时才付这笔钱。`config.DB_PATH`
    现读而不是用 `connect` 的默认参数 —— 后者在函数定义时就绑死了,测试没法把
    它指到临时库上去。
    """
    from kb import config, store        # 延迟导入:别让 kb/feeds 在导入期拖进 sqlite_vec
    like = ' OR '.join(['tag LIKE ?'] * len(STALE_TAG_PREFIXES))
    conn = store.connect(config.DB_PATH)
    try:
        for i in range(0, len(paths), 400):
            batch = paths[i:i + 400]
            ph = ','.join('?' * len(batch))
            conn.execute(f'UPDATE documents SET extracted_at=NULL,summary=\'\''
                         f' WHERE path IN ({ph})', batch)
            conn.execute(f'DELETE FROM entities WHERE path IN ({ph})', batch)
            conn.execute(f'DELETE FROM doc_tags WHERE path IN ({ph}) AND ({like})',
                         [*batch, *(p + '%' for p in STALE_TAG_PREFIXES)])
        conn.commit()
    finally:
        conn.close()


def retire_stale(kind: str) -> int:
    """把「新正文已经落进 vault、知识库里那份旧提取还没作废」的那几篇收掉。

    **时点是「新正文真的落了盘」,不是「网络抓到了正文」。** 补抓成功之后这一条
    要先等翻译成功，翻译失败或同步被停止时 vault 里还是旧
    壳 —— 在补抓那一刻就清掉 `extracted_at` 的话,提取阶段会拿**旧壳**再买一次
    提取、写回一个同样过时的结论,而 `extracted_at` 跟着又非空了:等正文译完
    导出之后,摘要和主题照旧是壳的。所以判据是 `exported_hash=content_hash`
    (这篇 md 已经是补抓来的那份全文了),由 `sync._export` 在导出之后调一次。

    **做完才把标记放下,顺序不能反。** 这一步失败(kb.db 连不上、磁盘满)时
    `extract_stale` 留在原处,下一轮导出再来一次;反过来写的话这几篇会永久停在
    「摘要还是那个跳转壳」的状态 —— 它们的 `exported_hash` 已经等于
    `content_hash`、压根不在待导出集合里,没有任何路径回来重试。

    不漏:这里**不限于本轮刚落盘的那几篇**,所以上一轮失败留下的待办也一起收。
    常态下那条查询是空集合,一次 kb.db 连接都不开。

    **放下标记时要核对版本**(`AND content_hash=?`,取样那一版)。这两步之间那一
    行可能被换掉 —— 手动收录那颗「重新抓一次」跑在 HTTP 请求线程里、不持这一轮
    的维护锁(`grab.replace`),而它每次都会重新置上 `extract_stale=1`。无条件按
    key 清的后果实测过(第 4 轮审查,Event 卡在 `_drop_extraction` 之前):清掉的
    是**新版本**的待办,于是下一轮导出不再摘旧的 `topic/`、`entity/`,索引按残留
    标签走 `parse_pretagged`、`extracted_at` 跟着非空 —— **新正文永远不进重新提取
    队列,旧主题永久留在那一篇上**,再同步多少轮都不变。`rowcount == 0` 不是失败
    (元规则),是「那一版已经不在了,它的待办不该由我来放下」。

    **`_drop_extraction` 照旧对整批做**:清掉的是知识库里那份按旧内容算出来的
    摘要/主题,对已经换了版的那几篇也只是让它们重新提取一次 —— 多花一次提取,
    而反过来留着是「永久错」。
    """
    with reg.db() as c:
        rows = c.execute("SELECT key,path,content_hash FROM feed_items"
                         " WHERE kind=? AND extract_stale=1"
                         " AND exported_hash=content_hash AND path<>''", (kind,)).fetchall()
    if not rows:
        return 0
    _drop_extraction([r['path'] for r in rows])
    done = 0
    with reg.db() as c:
        for r in rows:
            cur = c.execute('UPDATE feed_items SET extract_stale=0'
                            ' WHERE key=? AND content_hash=? AND extract_stale=1',
                            (r['key'], r['content_hash']))
            done += cur.rowcount
    return done
