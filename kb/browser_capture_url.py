"""文章网址的**唯一规范形**,和「这一页在库里可能长什么样」那两份候选。

从 `kb/browser_capture.py` 分出来的纯函数层(不碰库、不碰待办池),因为那边撞了
单文件 700 行那条硬上限(CLAUDE.md)。**判据仍然只有这一份** —— `browser_capture`
把这两个名字原样再导出一次,所有调用方照旧写 `capture.page_url(...)`。
硬约束与事故形态在 `docs/decisions/browser-capture.md` 的 `page_url()` 那两条。
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

_DEFAULT_PORTS = {'http': 80, 'https': 443}
#: WHATWG 的 path / query 百分号编码集之外的可打印 ASCII。`%` 留在里面是关键:
#: 已经编好的转义**原样透过**,所以 `/文章` 和 `/%E6%96%87%E7%AB%A0` 规范化到同一份。
_PATH_SAFE = "!$%&'()*+,-./:;=@[\\]^_|~"
_QUERY_SAFE = "!$%&()*+,-./:;=?@[\\]^_`{|}~"


def _without_dots(path: str) -> str:
    """去掉 `.` / `..` 段(RFC 3986 5.2.4,浏览器的 `new URL()` 也这么做)。

    末段是 `.` 或 `..` 时补一个空段 —— `/a/.` 在浏览器里是 `/a/`,不是 `/a`。
    """
    out: list[str] = []
    parts = path.removeprefix('/').split('/')
    for index, part in enumerate(parts):
        if part == '..' and out:
            out.pop()
        if part in ('.', '..'):
            if index == len(parts) - 1:
                out.append('')
        else:
            out.append(part)
    return '/' + '/'.join(out)


def page_url(value: str) -> str:
    """文章网址的**唯一**规范形 —— 「这两个地址是不是同一页」只认它。

    口径跟着浏览器的 `new URL().href`(扩展交回来的 `location.href` 就是那一份):
    scheme / 主机小写、非 ASCII 主机转 punycode、默认端口去掉、非 ASCII 与不安全
    字符百分号编码、已有转义原样透过、点段折叠、片段丢掉。空 query 两边都丢(比
    浏览器宽一格,宽的那头只会多认一个同页地址,不会把另一篇认成这一篇)。

    事故形态(2026-10-26 审查实测):服务端原来直接 `urlunsplit` 拼回原字符串,
    而扩展自己 `new URL()` 比一遍 —— 网址里带中文的订阅**每一次**都是人把整篇
    文章读完之后才被 409 拒,因为扩展比的是 `/文章` 对 `/文章`、服务端
    收到的是浏览器编码过的 `/%E6%96%87%E7%AB%A0`。`:443`、路径带空格、末尾 `?`
    是同一形态的另外三种。**判据不许在扩展里再写一份**,那只会开出第四第五种。

    `%2e` 这种编码过的点段不折叠(浏览器会)。两边都过本函数,所以后果止于「人
    自己填了一个点段地址、浏览器把它折叠了」→ 读之前就被告知网址不同,可改可重发。
    """
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme not in _DEFAULT_PORTS or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError
        host = parts.hostname
        if not host.isascii():
            host = host.encode('idna').decode()
        if ':' in host:
            host = f'[{host}]'
        port = parts.port
        netloc = host if port in (None, _DEFAULT_PORTS[parts.scheme]) else f'{host}:{port}'
        path = quote(_without_dots(parts.path), safe=_PATH_SAFE)
        return urlunsplit((parts.scheme, netloc, path, quote(parts.query, safe=_QUERY_SAFE), ''))
    except ValueError:
        raise ValueError('请使用不含账号密码的完整 HTTP / HTTPS 文章网址') from None


def _page_aliases(raw: str) -> list[str]:
    """这一页**在库里可能长什么样** —— 两份规范形,都由同一页推出来。

    为什么要两份:库里那一行的 `url` 是**收它的那条路**存下来的。订阅类来源原样
    存 feed 给的地址(`page_url` 那一套口径),手动收录存的是 `grab.normalize`
    (它顺手去掉 `utm_*`/`fbclid`/`gclid`、把推文地址收成 `x.com/<handle>/status/<id>`)。
    只按一份问,另一条路收的那一半永远答「不在库里」—— 而「不在库里」的下一步是
    **收录**,于是同一篇文章在库里变成两行、两篇 md、检索里两份结果。

    **这不是模糊匹配**:两个候选都是**这一页自己**的规范形,一个字符都不猜。
    `page_url` 那条底线照旧(「宽的那头只会多认一个同页地址,不会把另一篇认成这
    一篇」),所以**绝不在这里剥语言前缀、也不读网页自报的 canonical**:剥错一次
    就是把另一篇的正文写进这一条。真遇上站点按浏览器语言跳转的那一类,出路是
    `here()` 把等着的待办连同它指的地址一起交回去,由人点一下改用这一页 ——
    判据仍然在服务端,松的只是「人得自己去地址栏抄一遍」。
    """
    names = [page_url(raw)]
    try:
        from kb.feeds.grab import normalize
        alias = normalize(raw)
    except Exception:
        alias = ''
    if alias and alias not in names:
        names.append(alias)
    return names
