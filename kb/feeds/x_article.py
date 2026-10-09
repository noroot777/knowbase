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
③**不开浏览器**。走登录态浏览器读 `/i/article/<id>` 当然也行,但那要在用户自己的
  浏览器里多开一张标签页、等它渲染两三秒,还要押一个 X 改版就会坏的 DOM 选择器;
  这边是一次 HTTP、一份结构化 JSON。

**代价如实记着**:这是一个第三方服务,推文 URL 会发给它(内容本来就是公开的),
它挂了这一步就没了。`KB_X_ARTICLE_API` 设成空串可以整个关掉。

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
# 正文里那条指向长文的裸链接(feedgrab 的产物正文就是它)。
_ARTICLE_LINK = re.compile(r'(?:https?://)?(?:x|twitter)\.com/i/article/\d+\S*')
# t.co 短链:浏览器通道偶尔把它留在正文里。
_TCO = re.compile(r'https?://t\.co/\w+')
# Draft.js 的块类型 → markdown 前缀。认不出的块按普通段落走。
_PREFIX = {
    'header-one': '# ', 'header-two': '## ', 'header-three': '### ',
    'header-four': '#### ', 'header-five': '##### ', 'header-six': '###### ',
    'unordered-list-item': '- ', 'ordered-list-item': '1. ', 'blockquote': '> ',
}


class ArticleUnavailable(RuntimeError):
    """没问到。message 是成品文案。**和「它没有长文」是两回事** —— 后者返回 None。"""


def lead_text(body: str) -> str:
    """把正文里「这条推文自己说的话」摘出来:去掉转推上下文那行和光秃秃的链接。

    三样东西都不算"说了话":`_entry` 给转推加的那行 `> 上下文`、指向长文的裸链接、
    以及 t.co 短链。剩下的才是作者写在推文里的字 —— 它决定这一条要不要去补正文,
    补到之后也作为开头留在正文里。
    """
    lines = [ln for ln in (body or '').splitlines() if not ln.lstrip().startswith('>')]
    text = _ARTICLE_LINK.sub(' ', '\n'.join(lines))
    return _TCO.sub(' ', text).strip()


def needs_article(entry: dict) -> bool:
    """这一条要不要去问一次长文。两条,各管一种形态:

    ①**正文里明写着一条 `x.com/i/article/<id>`** —— 这是确定的信号,feedgrab 的
      产物正文就是它(推文的 t.co 被展开成了文章地址),哪怕作者还写了别的话。
    ②**推文自己一个字都没说** —— 浏览器通道那边 X 把那条链接藏起来改渲染成卡片,
      `tweetText` 压根不存在,于是我们手里只剩一条空正文。纯图、纯视频推文也满足
      它,那是故意的:没有别的办法分辨,而问一次的答复正好把它们分开(返回
      None = 它没有长文)。

    **不拿「正文里有 t.co 短链」当信号**:那可能是图片、引用推、任意外链,按它
    发请求等于给一大半推文各买一次调用,而换回来的多半是"它没有长文"。代价是
    一条「写了话 + 带长文卡片」的推文在浏览器通道上补不到正文(那边看不见链接)
    —— 已知的一道缝,记在这儿备查。
    """
    if not entry.get('url'):
        return False
    body = entry.get('body') or ''
    return bool(_ARTICLE_LINK.search(body)) or not lead_text(body)


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
    if not body and not title:
        raise ArticleUnavailable('长文接口给了一篇空文章')
    return {'title': title, 'body': body}


def fill(entries: list[dict]) -> tuple[list[dict], str]:
    """把这一批里的长文正文补上。返回 (留下的条目, 没补全的原因)。

    **原因非空 = 这一轮没补全**,调用方把它并进 `incomplete`,于是水位留在原处、
    下一轮重叠重抓时再试(理由见模块注释:空正文入库之后就改不回来了)。补不到的
    那几条**本轮不入库** —— 入库了下一轮的重试也更新不了它们,留着等于白等。
    """
    if not API:
        return entries, ''
    kept, missed, fails = [], 0, 0
    for entry in entries:
        if not needs_article(entry) or fails >= _GIVE_UP_AFTER:
            if fails >= _GIVE_UP_AFTER and needs_article(entry):
                missed += 1
                continue
            kept.append(entry)
            continue
        try:
            got = fetch(entry['url'])
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
        kept.append(entry)
    note = (f'有 {missed} 条推文的长文正文没取到,留到下次同步再试' if missed else '')
    return kept, note
