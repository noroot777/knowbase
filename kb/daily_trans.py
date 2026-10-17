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
用户点开才发现还是英文 —— 那是在界面上说假话。**比「说假话」更坏的是认错篇**:
地址匹配一旦把两个不同的 url 当成同一个,点「译」打开的是另一篇文章,而抽屉顶上
那个「原文」还指着你点的那一篇 —— 口径在下面 `_as_href` 那一段。
"""

from __future__ import annotations

from urllib.parse import quote

from kb import store
from kb.config import PRIMARY_SOURCE_NAMES, VAULT_ROOT
from kb.scanner import parse_frontmatter, read_md_text

# 页面上那个 href 不是库里存的那一份,是 marked 写出来的:它的 `cleanUrl` 做的是
# `encodeURI(href).replace(/%25/g, '%')`。所以「这一行的地址是不是库里这一篇」要
# **按页面那条规则把库里那份算成 href 再比**,方向只有这一个。
#
# **不许反过来解码。** `unquote(整个 url)` 不是文章身份:它连查询串里的 `%26`、
# `%3D` 一起还原成分隔符,于是 `?q=a%26id%3Db`(一个参数,值里带 `&`)和
# `?q=a&id=b`(两个参数)解出同一个键 —— 两篇各自入库、各自有译文的文章被当成同
# 一个地址,点「译」打开的是**另一篇文章**,而抽屉顶上那个「原文」还指着你点的
# 那一篇(2026-10-17 审查用隔离库 + 真浏览器实测)。路径、片段里被编码的保留字符
# 同理,针对某几个百分号打补丁治不了它。
#
# safe 这一串就是 encodeURI 的不编码集合(`;/?:@&=+$,` + `-_.!~*'()` + `#` + 字母
# 数字),外加 `%` —— marked 把 encodeURI 产生的 `%25` 又换了回去,等于 `%` 原样
# 保留。**两个实现逐位对齐这件事不许按规范推**(教训在 `kb/daily_refs.py` 的
# TLD 名单那一节):`tests/daily_zh_probe.mjs` 里有一条拿页面上真的 `encodeURI`
# 跟这里逐个比对的样例。
_HREF_SAFE = "!#$&'()*+,/:;=?@~%"


def _as_href(url: str) -> str:
    """库里这个 url 渲染到页面上会写成什么(marked 的 `cleanUrl`)。"""
    return quote(url, safe=_HREF_SAFE)


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
        # 没精确命中、地址里又带着 `%` 的那些再对一次:**把库里那一份按页面那条
        # 规则算成 href**(`_as_href`),算出来等于手上这个 href 才算同一篇。
        # **只对带 `%` 的做**:`_as_href` 只会**产生** `%xx`,不含 `%` 的 href 根本
        # 没被它改写过,精确对不上就是库里真没有,犯不上为它扫一遍 documents。
        #
        # **missing 是个集合,不是「归一键 → 请求地址」的单值映射。** 后者在同一批
        # 里会互相覆盖:两条请求地址一旦归一成同一个键,后写进去的那条把前一条挤掉,
        # 被挤掉的那个编号这一轮再也拿不到结果(2026-10-17 审查实测:同批问两条只
        # 回来一条,颠倒顺序消失的换成另一条)。**如实记着:那是 `unquote` 带来的**
        # —— 上面那条根因修掉之后,归一键恰好等于请求地址本身,两种写法在当前调用
        # 方(页面 href)下等价,单独把这里换回单值映射**红 0 条**。集合写法留着是
        # 因为它在**任何**输入下都不会丢(调用方传进来的未必是 href 形态),而且它
        # 把「不靠键归一」这件事写死,免得下一个人又去造一个归一键。
        # 顺带:**几篇文档映射到同一个请求地址是照收的** —— 下面逐篇问「是不是
        # 译文」那一步本来就在处理这种情况。
        missing = {u for u in want if u not in found and '%' in u}
        if missing:
            for r in conn.execute(
                    f"SELECT path,title,url FROM documents"
                    f" WHERE url<>'' AND source IN ({src}) ORDER BY path",
                    PRIMARY_SOURCE_NAMES):
                u = _as_href(r['url'])
                if u in missing:
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
