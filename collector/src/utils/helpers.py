# -*- coding: utf-8 -*-
"""两个通用函数:当前时间的统一写法,和"这篇原文是不是永久读不到了"。

原来这里还有九个,其中七个在旧前端和原凭证通道撤掉之后再没有调用者
(jstime2realtime / realtime2jstime / time_delta / url2text /
message_is_delete / check_text_ratio / nunjucks_escape),连同它们一起删了。
"""

from __future__ import annotations

import re
from datetime import datetime

from lxml import etree


def time_now() -> str:
    """当前时间,精确到分钟。

    库里所有时间列都用这一种写法(`YYYY-MM-DD HH:MM`):秒那一位对"上次采集是
    什么时候"没有意义,而少一个量级能让时间串直接按字典序比大小。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# 原文永久不可读的页面提示。只收有实样的三种:作者删除、平台违规下架、
# 每天白打一次请求、还永远挂在面板的「正文待重试」里。
#
# 这张表要保守:命中即判「不可用」、退避拉到一周,误判会让一篇本可抓到的
# 文章一周才试一次。所以只写这几条特异的整句短语,不做「无法查看」这种
# 宽泛匹配 —— 认不出的形态落回 request_failed,顶多多试几次,方向是安全的。
_UNAVAILABLE_HINTS = (
    '已被发布者删除',
    '因违规无法查看',
    '涉嫌侵权',
)

# 第四种形态:作者把文章设成了隐私可见。这一种页面上没有任何提示文字 ——
# 服务端只回一个空壳(<div id="app"></div> + 公众号信息),「根据作者隐私设置,
# 无法查看该内容」那句是前端渲染出来的,抓下来的 HTML 里一个字都没有。
# 能认的只有入口 bundle:隐私页加载 private.<hash>.js,正常文章页加载
# appmsg.<hash>.js。按整条资源路径匹配,别拿 'private' 裸串去搜正文。
_PRIVATE_ENTRY_RE = re.compile(r'/assets/private(?:-legacy)?\.[0-9a-z]+\.js')
_PRIVATE_HINT = '根据作者隐私设置，无法查看该内容'


def message_unavailable(response: str) -> str:
    """原文是否永久不可读;是则返回那句原因,否则空串。

    只看调用方已经拿到的那一份 HTML(空串也是「这次拿到的就是空页面」):在这里
    自己补一次请求的话,同一篇文章会被抓两遍,而第二遍还可能拿到不一样的页面。

    提示节点有 div.weui-msg__title.warn(删除)和 h3.weui-msg__title(违规、
    侵权)两种形态,所以按 class 包含匹配、取全部文本,别再只认 div.warn。
    隐私文章没有提示节点,只能认入口 bundle(见 _PRIVATE_ENTRY_RE)。
    """
    tree = etree.HTML(response, parser=etree.HTMLParser(encoding='utf-8'))
    if tree is None:
        return ''
    for raw in tree.xpath('//*[contains(@class, "weui-msg__title")]//text()'):
        hint = ' '.join(str(raw).split())
        if any(kw in hint for kw in _UNAVAILABLE_HINTS):
            return hint
    if _PRIVATE_ENTRY_RE.search(response):
        return _PRIVATE_HINT
    return ''


