"""「这一页的地址不在库里,但像是库里**在等正文**的那一篇」—— 扩展那一问 `absent` 那一档的候选。

人在浏览器里看到一篇好文章、点开扩展,`here()` 按网址问库里有没有这一页。问不到有两种完全不同的
情形,而它们在「按网址」这一维上长得一模一样:
- **真不在库里** —— 下一步是收录,成一条新的;
- **库里有、正在等正文,只是地址变了**(站点按浏览器语言跳转 `/index/x` → `/zh-Hans-CN/index/x`、
  改过域名、`.html` 去掉了)—— 下一步是**把正文补进那一条**。按前一种做,同一篇在库里成了两行,
  原来那一条照旧缺正文,各导出一篇 md。

`elsewhere`(人在采集抽屉里发起过的待办)只覆盖后一种里「人先去过抽屉」的那一半;他直接在新页面上
点扩展时一个待办都没有。这里补的是另一半:**按相像程度把候选交出去,由人点一下认领**。

**只交候选,不认亲。** 相像是推测,「这一页就是那一篇」只有人说得算 —— 认错的后果是把这一页的
正文写进另一篇,不可恢复;而列错一条的代价是他看一眼、点「都不是」。所以这里**宁可多列**,但
`here()` 的 `situation` 照旧是 `absent`,一个待办都不建(`adopt` 那一下才建)。
硬约束见 docs/decisions/browser-capture.md「像是库里那一篇」。
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit

#: 最多交几条。弹窗 450px 宽,再多就是让人在一列相似标题里找,而那正是网址该替他做的事。
LIMIT = 3
#: 路径末段太短、或者是站点通用的那几个词时不算数 —— 同站所有 AMP 页的末段都是 `amp`。
_MIN_SLUG = 3
_GENERIC_SLUGS = frozenset(('index', 'amp', 'default', 'home', 'main', 'page', 'post', 'posts',
                            'article', 'articles', 'blog', 'news', 'story', 'p', 'view', 'detail'))
_EXTENSION = re.compile(r'\.(?:s?html?|php|aspx?|jsp)$')
#: 标题要多长才算数(去掉空白与标点之后的字数)。短标题(「Changelog」「周报」)全站到处都是。
_MIN_TITLE = 8


def _host(url: str) -> str:
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


def _slug(url: str) -> str:
    parts = [part for part in unquote(urlsplit(url).path).split('/') if part]
    slug = _EXTENSION.sub('', parts[-1].casefold()) if parts else ''
    return '' if len(slug) < _MIN_SLUG or slug in _GENERIC_SLUGS else slug


def _plain(text: str) -> str:
    """只留字母数字(含中日韩字符),大小写与全半角归一 —— 「标题 | 站点名」和「标题」才比得上。"""
    return ''.join(ch for ch in unicodedata.normalize('NFKC', text or '').casefold() if ch.isalnum())


def score(page: str, title: str, item_url: str, item_title: str) -> int:
    """这一条有多像这一页:0 = 不像。同站同末段 2 分,标题对得上 1 分。

    **标题不看站点**:换过域名的那一类末段多半也变了,只剩标题认得出来。判据是「库里那条的标题
    整个出现在这一页的标题里」—— 浏览器标签的标题通常是「文章标题 | 站点名」,反过来包含就是拿
    站点名去匹配一切。
    """
    slug = _slug(page)
    same = 2 if slug and _host(page) == _host(item_url) and slug == _slug(item_url) else 0
    mine = _plain(item_title)
    return same + (1 if len(mine) >= _MIN_TITLE and mine in _plain(title) else 0)


def alike(conn, page: str, title: str, owed_sql: str, check) -> list[dict]:
    """库里**正等着正文**、又像这一页的那几条,最像的在前。

    `owed_sql` 是 `browser_capture._OWED`(「这一条的正文还欠着」只有那一份口径);`check` 是
    `browser_capture._checked_target`,过不了它的(不再收录、来源不许替换正文、X 观点)不列 ——
    列出来点下去也只会被拒。两样都由调用方交进来,不在这里另写一遍。
    """
    rows = conn.execute('SELECT feed_items.*, EXISTS (SELECT 1 FROM feed_item_exclusions'
                        ' WHERE key=feed_items.key) AS capture_excluded'
                        f' FROM feed_items WHERE {owed_sql}').fetchall()
    ranked = []
    for row in rows:
        points = score(page, title, row['url'], row['title'])
        if not points:
            continue
        try:
            check(dict(row))
        except (LookupError, ValueError):
            continue
        ranked.append((points, row))
    # 同分的新的在前:两次稳定排序,先按次序键、再按主键。
    ranked.sort(key=lambda one: one[1]['published'] or '', reverse=True)
    ranked.sort(key=lambda one: one[0], reverse=True)
    return [dict(key=row['key'], title=row['title'], url=row['url']) for _, row in ranked[:LIMIT]]
