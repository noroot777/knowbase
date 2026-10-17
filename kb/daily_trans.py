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

    # 同一个地址可能挂着好几篇(同一篇文章被两条订阅各抓一次,url 还可能一份存
    # 编码形式一份存原样),所以收的是列表:**先把所有渲染到这个 href 的候选收齐**,
    # 再逐篇去问"是不是译文",问到一篇是就用它。只挑第一篇的话,两篇里恰好排在
    # 前面的那篇没译,这个地址就被判成"本地没有译文" —— 而译文明明在库里。
    found: dict[str, list] = {}
    seen: dict[str, set[str]] = {}    # href → 已经收进去的 path,两个阶段去重用
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
            seen.setdefault(r['url'], set()).add(r['path'])
        # 地址里带着 `%` 的那些**再扫一遍**:**把库里那一份按页面那条规则算成
        # href**(`_as_href`),算出来等于手上这个 href 就是同一篇。
        #
        # **「上面有没有字面命中」不许决定这一遍扫不扫**(2026-10-17 审查 F2 续,
        # 这一条是改过的)。原来的条件是 `u not in found` —— 把「库里有一行 url
        # 跟你逐字相同」当成了「这个地址的候选已经收齐」,而那一行**既不保证是
        # 译文、也不保证文件还在**。同一篇文章被两条订阅各抓一次、一次存编码形式
        # 一次存原样,于是:编码那一行(没译文,或者 md 已经不在了)把原样那一行
        # (躺着能读的中文译文)整个挡在候选之外,面板上那个编号没有「译」,重开
        # 日报还是没有 —— 而把那条没用的索引删掉,译文立刻就出来了(审查用隔离库
        # 实测,`reproduce.log`)。**「同一个字面 url 挂几篇就逐篇问」这条不变量,
        # 对「渲染成同一个 href 的几篇」必须一样成立**,收齐再挑,不是挑完再说。
        #
        # **只对带 `%` 的做**,这一条还站得住:`_as_href` 只会**产生** `%xx`,
        # 所以 `_as_href(库里url)` 不含 `%` 就意味着它一个字符都没被改写、等于库里
        # 那份 url 本身 —— 这种候选上面那条 `url IN (...)` 已经收干净了。不含 `%`
        # 的 href 精确对不上就是库里真没有,犯不上为它扫一遍 documents。
        #
        # 代价是:只要这批里有**一条**带 `%` 的地址,就扫一遍 documents(实测
        # 生产库 ~157ms,一次问完整份日报)。上限 `MAX_URLS` 管的是读几个文件,
        # 这里只扫一遍、不随条数涨。
        #
        # 两个阶段都往 `found[u]` 里塞,所以按 path 去重 —— 字面命中的那一行在这
        # 一遍里多半也算得出同一个 href(`%` 在 `_HREF_SAFE` 里,原样保留)。
        # **如实记着:单独去掉这个去重红 0 条** —— 下面那一步是「逐篇问,问到译文
        # 为止」,同一个 path 重复一次只是多开一次文件、答案一样。它挡的是白读文件,
        # 不是错结果。**真正定结果的是顺序**:字面命中的排在前面(更强的证据,也让
        # 单篇场景的结果跟以前一模一样),两批混在一起按 path 重排一遍就会挑错 ——
        # 那一条有红。
        #
        # **这里收的是集合、不是「归一键 → 请求地址」的单值映射。** 后者在同一批里
        # 会互相覆盖:两条请求地址一旦归一成同一个键,后写进去的那条把前一条挤掉,
        # 被挤掉的那个编号这一轮再也拿不到结果。**如实记着:那是 `unquote` 带来的**
        # —— 根因修掉之后归一键恰好等于请求地址本身,两种写法在当前调用方(页面
        # href)下等价,单独把这里换回单值映射**红 0 条**。留着是因为它在**任何**
        # 输入下都不会丢,而且把「不靠键归一」这件事写死。
        encoded = {u for u in want if '%' in u}
        if encoded:
            for r in conn.execute(
                    f"SELECT path,title,url FROM documents"
                    f" WHERE url<>'' AND source IN ({src}) ORDER BY path",
                    PRIMARY_SOURCE_NAMES):
                u = _as_href(r['url'])
                if u not in encoded or r['path'] in seen.setdefault(u, set()):
                    continue
                found.setdefault(u, []).append(r)
                seen[u].add(r['path'])
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
