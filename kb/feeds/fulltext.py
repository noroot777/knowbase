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
订阅的内容永远进不了知识库。
"""
from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor

from kb.feeds import readable, registry as reg
from kb.feeds.fetching import FeedError, request
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


def fetch_one(url: str) -> str:
    """抓一个原文链接,返回正文纯文本。抓得到页面但读不懂正文时返回空串。

    走 `fetching.request`,所以 SSRF 防护(只连公网地址)、重定向逐跳校验、
    8 MB 上限、超时这几件事都已经有人管了 —— 这里只是换一份 Accept。
    """
    response = request(url, _HEADERS)
    return readable.extract(readable.decode(response.body, response.headers))


def _save(key: str, body: str) -> None:
    """正文落库。顺带把这一条**重新排进翻译队列**。

    旧译文译的是那份壳,正文一换它就作废了 —— 留着的话导出会把"壳的译文"当成
    这篇文章的中文正文写进 vault,而 `translated_at` 非空意味着再不会有人去
    订正它。所以 `title_zh`/`body_zh`/`translated_at` 一起清掉,当一条新内容
    重译;`translate_tries` 同理归零,新正文值得一份新的重试额度。

    `content_kind` 从 `summary` 改成 `content`:导出那边据它决定要不要写
    「以下为订阅源提供的摘要」那句注记,而现在这就是全文了。

    `content_hash` 按新正文重算、`exported_hash` 清空 —— 两件事合起来才让导出
    阶段重写这篇 md(闸门是 `exported_hash<>content_hash`)。
    """
    digest = hashlib.sha256(body.encode()).hexdigest()
    with reg.db() as c:
        row = c.execute('SELECT title FROM feed_items WHERE key=?', (key,)).fetchone()
        if not row:                      # 这期间被存量清理删掉了,没什么可写的
            return
        c.execute("""UPDATE feed_items SET body=?,content_kind='content',content_hash=?,lang=?,
            fulltext_at=?,fulltext_error='',exported_hash='',
            title_zh='',body_zh='',translated_at='',translate_error='',translate_tries=0
            WHERE key=?""",
            (body, digest, reg.text_language(row['title'], body), reg.now(), key))


def _fail(key: str, error: Exception | str) -> None:
    with reg.db() as c:
        c.execute('UPDATE feed_items SET fulltext_error=?,fulltext_tries=fulltext_tries+1'
                  ' WHERE key=?', (str(error)[:600] or type(error).__name__, key))


def fetch_pending(kind: str, limit: int | None = None, progress=None) -> dict:
    """把这个来源里待补抓的条目逐个去原文链接取正文。返回摘要给调用方汇报。

    **失败过的优先**(`ORDER BY fulltext_tries DESC`),理由同
    `translate.translate_pending`:按升序排的话,重试永远排在"从没试过"的后面,
    几百条积压下要几十轮同步才轮得到一次重试,而面板每一轮都在报同一个数字。
    `key` 收尾是为了定序 —— `fetched_at` 只精确到秒,一批抓回来的条目时间戳
    一模一样。
    """
    summary = {'done': 0, 'failed': 0, 'empty': 0, 'left': 0,
               'errors': [], 'failed_keys': [], 'stopped': False}
    cond, cargs = queued_sql()
    with reg.db() as c:
        rows = [dict(r) for r in c.execute(
            f'SELECT key,url,title FROM feed_items WHERE kind=? AND ({cond})'
            ' ORDER BY fulltext_tries DESC, fetched_at DESC, key LIMIT ?',
            (kind, *cargs, max(1, limit or MAX_ITEMS))).fetchall()]
    summary['left'] = max(0, pending(kind) - len(rows))
    if not rows:
        return summary
    deadline = time.monotonic() + ROUND_SECONDS

    def one(item):
        if _state.get('stop_requested') or time.monotonic() >= deadline:
            return item, None, None
        try:
            return item, fetch_one(item['url']), None
        except Exception as e:               # noqa: BLE001 - 逐条隔离,一个坏链接不带掉整轮
            _fail(item['key'], e)
            return item, None, e

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='fulltext') as pool:
        for item, body, error in pool.map(one, rows):
            if isinstance(error, Exception):
                summary['failed'] += 1
                summary['failed_keys'].append(item['key'])
                summary['errors'].append({'title': item['title'][:120], 'reason': str(error)[:300]})
            elif body is None:               # 停止请求 / 预算用完,留给下一轮
                summary['left'] += 1
                summary['stopped'] = True
            elif body:
                _save(item['key'], body)
                summary['done'] += 1
            else:
                # 页面抓到了,但那上面认不出一块像正文的内容(付费墙、纯 JS 渲染、
                # 一张图)。按失败计次:重试三轮之后它就离开队列,按壳入库 ——
                # 不计次的话这条永远排在队首,每一轮都重抓同一个读不懂的页面。
                _fail(item['key'], '页面上没有识别出正文，可能需要登录或由脚本渲染')
                summary['empty'] += 1
                summary['failed_keys'].append(item['key'])
            if progress:
                progress(summary['done'] + summary['failed'] + summary['empty'], len(rows))
    return summary


def summarize(summary: dict) -> str:
    if not any(summary[k] for k in ('done', 'failed', 'empty', 'left')):
        return ''
    text = f"已补抓 {summary['done']} 条原文正文"
    if summary['failed']:
        text += f",{summary['failed']} 条抓取失败"
    if summary['empty']:
        text += f",{summary['empty']} 条没能识别出正文"
    if summary['left']:
        text += f",{summary['left']} 条留到下一轮"
    return text
