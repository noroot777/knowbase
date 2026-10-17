# -*- coding: utf-8 -*-
"""日报正文里那些出处,哪几条在本地读得到中文译文。

日报的出处编号指向**站外原文**(素材清单里写的就是那个地址),点开是一篇英文;
而外文内容在导出之前就已经译过一遍,vault 里躺着的正是中文那一份(口径见
`docs/decisions/translate.md`)。也就是说"这篇看不懂"在本地本来有答案,只是日报
正文里没有入口 —— 这一份就负责把那个答案找出来。

**判据只有一个:vault md 的 frontmatter 里 `translated_from` 非空。** 那一行是
`kb.feeds.sync.export` 落盘时写的,和文件里那份正文同真同假。不去问 feeds 库的
`translated_at`:公众号、视频这些不走 feeds 的来源压根不在那张表里,而这里问的是
**文件**是不是译文,不是"某个订阅条目译没译"。

**判不出来一律当成没有**(文件读不出来、frontmatter 坏了、url 在库里对不上):
少一个额外入口而已,原文链接一个字没动;反过来把一篇英文说成"本地有中文译文",
用户点开才发现还是英文 —— 那是在界面上说假话。
"""

from __future__ import annotations

from urllib.parse import unquote

from kb import store
from kb.config import PRIMARY_SOURCE_NAMES, VAULT_ROOT
from kb.scanner import parse_frontmatter, read_md_text

# 一次最多问多少条。一份日报的素材上限是 `daily.MAX_ITEMS`(260),正文里引用到的
# 只会更少 —— 这个数是防着调用方乱传,不是业务上的分档。超出的直接不看:少几个
# 入口是良性的,而每一条都要开一个文件读 frontmatter。
MAX_URLS = 500


def _translated(path: str) -> dict | None:
    """vault 里这一篇是不是译文?是就连它的原标题一起给出来。

    原标题只在"标题也跟着译了"时才写进 frontmatter(见 `sync.export`),所以它
    可能是空的 —— 空就不显示,不编一个出来。
    """
    try:
        meta, _ = parse_frontmatter(read_md_text(VAULT_ROOT / path))
    except OSError:
        return None                   # 文件不在了 / 网盘没挂上:这一条就当没有
    if not str(meta.get('translated_from') or '').strip():
        return None
    return {'original_title': str(meta.get('original_title') or '').strip()}


def translations(urls) -> dict[str, dict]:
    """这批原文地址里,哪几条在本地有中文译文(url → 那一篇)。

    回包的键是**调用方传进来的那个字符串原样**,不是库里存的那一份 —— 下面的
    宽松匹配会让两者不逐字相同,换成库里那份的话前端对不上号。
    """
    want: list[str] = []
    for u in urls or ():
        u = str(u or '').strip()
        if u and u not in want:
            want.append(u)
        if len(want) >= MAX_URLS:
            break
    if not want:
        return {}

    # 同一个 url 可能挂着好几篇(同一篇文章被两条订阅各抓一次),所以收的是列表:
    # 下面逐篇去问"是不是译文",问到一篇是就用它。只挑第一篇的话,两篇里恰好
    # 排在前面的那篇没译,这个 url 就被判成"本地没有译文" —— 而译文明明在库里。
    found: dict[str, list] = {}
    conn = store.connect()
    try:
        src = ','.join('?' * len(PRIMARY_SOURCE_NAMES))
        ph = ','.join('?' * len(want))
        # 衍生来源(条目化产物)挡在外面:它和原文共享同一个 url,但那是提取出来的
        # 条目、不是文章本身 —— 日报素材也是按这个范围取的(`daily._scope`)。
        rows = conn.execute(
            f'SELECT path,title,url FROM documents'
            f' WHERE url IN ({ph}) AND source IN ({src}) ORDER BY path',
            (*want, *PRIMARY_SOURCE_NAMES)).fetchall()
        for r in rows:
            found.setdefault(r['url'], []).append(r)
        # 没精确命中、地址里又带着 `%` 的那些再宽松对一次:页面上那个 href 是
        # marked 用 `encodeURI` 写出来的,库里存的是原样 —— 两边只差一层
        # percent-encoding 时,解码之后就对上了。**只对带 `%` 的做**:encodeURI
        # 只会**产生** %xx,不含 `%` 的 url 根本没被它改写过,精确对不上就是库里
        # 真没有,犯不上为它扫一遍 documents。
        missing = {unquote(u): u for u in want if u not in found and '%' in u}
        if missing:
            for r in conn.execute(
                    f"SELECT path,title,url FROM documents"
                    f" WHERE url<>'' AND source IN ({src}) ORDER BY path",
                    PRIMARY_SOURCE_NAMES):
                u = missing.get(unquote(r['url']))
                if u:
                    found.setdefault(u, []).append(r)
    finally:
        conn.close()

    out: dict[str, dict] = {}
    for u, rows in found.items():
        for r in rows:
            zh = _translated(r['path'])
            if zh is not None:
                out[u] = {'path': r['path'], 'title': r['title'], **zh}
                break
    return out
