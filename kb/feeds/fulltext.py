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
(`scripts/feed_fulltext_recheck.py`)只认 `readable.boilerplate_reason`,**这六道一道
都不借** —— 库里只剩那份纯文字,认出一张关卡页要的地址、DOM 和响应头当时就没存,拿
纯文本启发式去补那一刀,两轮审查各实测到一次误删正常内容(⑥ 判坏 Markdown 原文、
④ 判坏英文教程)。那一类的出路是人点「重新抓一次」;生产只读实测说这不是损失 ——
存量里一条关卡页都没有。理由和数字在那个脚本的 `_not_article` 头上。

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
from kb.feeds.fetching import FeedError, SOURCE_SECONDS, request
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


def queued_sql() -> tuple[str, list]:
    """"还排在正文补抓队列里"的判据,只有这一份。

    三个条件:①它属于某条开了补抓的订阅(一条内容可能同时属于聚合源和单号源,
    任意一条开了就抓 —— 补到的正文对另一条同样有用);②还没抓到过
    (`fulltext_at` 空);③重试额度没用完。

    队列的出口就是第三条:试满 `MAX_TRIES` 仍然抓不动的条目离开队列,按 feed
    原本那份壳走完翻译和导出。留着原文比把它永远挡在知识库外面强。

    `feed_items.key` 写全名是因为这段会被拼进别人的 WHERE 里(`registry.items`、
    `sync._export`),那些语句的 FROM 子句就是 feed_items,不带别名。
    """
    return ("EXISTS(SELECT 1 FROM subscription_items si JOIN subscriptions s"
            " ON s.id=si.subscription_id WHERE si.item_key=feed_items.key AND s.fulltext=1)"
            " AND fulltext_at='' AND fulltext_tries<?"), [MAX_TRIES]


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
    共用一份**(`grab_page.admit`:提取 + 关卡,连先后顺序一起)。原先这里直接
    拿 `extract_or_reason` 的结果当正文,**六道准入一道都不过** —— 后果是一张
    关卡页/登录墙被 `_save` 写进 `feed_items.body`、写上 `fulltext_at`、**退出
    重试队列**,而 `put_items` 的 `DO NOTHING` 之下再也换不回来(审查实测:
    1453 字节、一行脚本都没有的 Cloudflare 关卡页,`done=1`、正文被换成
    「Verify you are human…」、`pending` 归零)。**关卡一律抛**,理由同上面
    `html_problem` 那一档:那半句话明确得多,而「抛」这条路保证了
    `fetch_pending` 走 `_fail` —— 壳一个字节不动、`fulltext_at` 不写、
    `fulltext_tries` 照旧计次(留在原重试语义里:不计次它就永远排在队首,
    试满 `MAX_TRIES` 按壳入库,口径同「读不懂」那一档)。
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
    body, gate, why = grab_page.admit(response.url or url,
                                      readable.decode(response.body, response.headers))
    if gate:
        raise FeedError(gate)
    return body, why


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
            fulltext_at=?,fulltext_error='',extract_stale=1,content_rev=content_rev+1,
            title_zh='',body_zh='',translated_at='',translate_error='',translate_tries=0
            WHERE key=? AND content_hash=? AND content_rev=?""",
            (body, digest, reg.text_language(row['title'], body), reg.now(), key, *seen))
        return cur.rowcount > 0


def _fail(key: str, error: Exception | str, seen: tuple[str, int]) -> bool:
    """这一条补抓失败了。返回「记上了没有」。

    **判据同 `_save`,理由也同**:这次失败说的是**取样那一版**(`seen`)去原文
    链接没抓到。等待期间那一行被换掉时 —— 哪怕换的只是标题、哪怕改过去又改回来
    使得内容逐字复原 —— 把错误记在新那一版头上、还扣掉一次重试额度,是在给一件
    已经不存在的事记账。**这里不碰 `content_rev`**:记一笔失败不是换内容,别人
    手上的工作资格不该因此作废。
    """
    with reg.db() as c:
        cur = c.execute('UPDATE feed_items SET fulltext_error=?,fulltext_tries=fulltext_tries+1'
                        ' WHERE key=? AND content_hash=? AND content_rev=?',
                        (str(error)[:600] or type(error).__name__, key, *seen))
        return cur.rowcount > 0


def fetch_pending(kind: str, limit: int | None = None, progress=None) -> dict:
    """把这个来源里待补抓的条目逐个去原文链接取正文。返回摘要给调用方汇报。

    **失败过的优先**(`ORDER BY fulltext_tries DESC`),理由同
    `translate.translate_pending`:按升序排的话,重试永远排在"从没试过"的后面,
    几百条积压下要几十轮同步才轮得到一次重试,而面板每一轮都在报同一个数字。
    `key` 收尾是为了定序 —— `fetched_at` 只精确到秒,一批抓回来的条目时间戳
    一模一样。
    """
    summary = {'done': 0, 'failed': 0, 'empty': 0, 'left': 0, 'stale': 0,
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
                if not _fail(item['key'], error, work_version(item)):
                    summary['stale'] += 1
                else:
                    summary['failed'] += 1
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
