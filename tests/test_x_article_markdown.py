# -*- coding: utf-8 -*-
"""长文地址的起点:往左扫描不许跨过一条 markdown 链接的闭合边界。

**这一族管的是「同一条 URL 内」这四个字到底怎么界定**(2026-10-10 自由工作流
第 3 轮审查)。起点判据那一步从候选往左逐字吃 `_URL_RUN` 字符,想找的是"包着
候选的那条外层 URL 的起点";可 RFC 3986 把 `(` `)` `[` `]` 也算合法 URL 字符,
而 feedgrab 的产物正文是 **markdown** —— 于是相邻的几条链接之间一个空白都没有,
整段连成一个 run:

```text
[share](https://example.org/?q=foo)[docs](https://docs.example.org/)[article](<真地址>)
```

扫描一路跨过**两条已经闭合的链接**,最左那个起手式落在第一条参考链接的
`https://` 上,它后面又有 `?q=foo` —— 于是 (b) 那条证据成立,这条推文**自己的**
文章地址被判成"嵌在别人的查询值里",`needs_article` 三条判据一条都不命中:那几条
链接本身当成正文入库,`added=1`、`errors=[]`、`partial=[]`、接口调用 **0** 次、
水位照常推进、标题回落成「某某 的动态」,而 `put_items` 是
`ON CONFLICT(key) DO NOTHING` —— **加空格再同步也补不回来**(审查实测:加空格那
一轮 `added=0`,库里和导出的 md 里仍然只有三条链接)。两种文章路径
(`/<handle>/article/<id>` 与 `/i/article/<id>`)逐字相同。

**这是上一轮"取最左起手式"自己开出来的洞**:取末项时中间那条文档链接恰好把检查
范围重置了(最后一个起手式之后只剩 `/)[article](`,没有 `?#`),同一份输入拿得到
全文。而取最左是对的 —— 查询串和片段只被空白和 `<` 终止,**在同一条 URL 里**最外
那个容器说了算;错的是把"已经闭合的另一条 markdown 链接"也算进了同一条 URL。

修法**不是**把 `](` 当扫描终止符 —— 那条路第 3 轮走过一次,它的前提
("`]` 在合法 URL 里只用于 IPv6 收尾,紧跟一个 `(` 不可能出现")被第 4 轮审查
用三项实证推翻了:同一条 `<https://example.org/share?text=[read](<别人的地址>)>`
在页面加载的真 `marked` 里渲染成**一条**指向 `example.org/share` 的链接、
Chromium 的 `new URL()` 原样保留它且 `searchParams.get('text')` 返回完整的
`[read](<别人的地址>)`、向本地回显端点发真实 HTTP 请求收到的原始查询也逐字完整。
分享按钮把一段 markdown 塞进查询值是真实写法,那个 `](` 是**外层 URL 的内容**,
不是一条新链接的起手。停在那里的后果是 `run` 丢掉外层 URL 的起点和 `?`,于是
别人那条地址被当成这条推文自己的、被 `_drop` 永久摘掉(第三族逐条钉)。

正解是把 `](` 从"终止符"降成**一条 markdown 链接目标的起手标记**:往左一路扫到
底,再逐个起手式找"还开着"的那条外层 URL —— 紧前是 `](` 的那个按 CommonMark 对
link destination 的定义判(不带尖括号的目标里括号必须成对,第一个落单的 `)`
终止它),闭合了就不可能是容器、跳过;其余起手式(裸 URL、`www.`)一律当"开着"。
两条判据都取自 CommonMark 规范原文,所以**仍然没有引入 markdown 解析**:要判的是
"这一个起点是不是一条 URL 的起点",那是切分问题、不是解析问题(口径同第 4、5、6
轮答复的那条 `urlsplit` 路线)。

**那个"紧前是 `](`"的限定不能省**:裸 URL 的路径里一个孤立的 `)` 是 sub-delims、
完全合法(`https://example.org/a)b?u=<别人的地址>`),不限定就会把一条**还开着**的
外层 URL 误判成闭合,别人那条地址照样被摘掉 —— 第四族有一条契约钉它。

**换成"闭合判据"之后第 3 轮记的那道缝顺手好了**:一条链接闭合之后紧贴一个裸 URL
(`[share](https://example.org/?q=foo)https://x.com/…`)原来会被判成嵌套(那里没有
`](` 可认),现在那条闭合的目标被正确跳过、真地址认得出来。第四族钉住它。

**边界之后那一段仍然是同一条 URL** —— `[a](https://example.org/?u=<别人的文章
地址>)` 里那条照旧判成嵌套、一个字不动。这一族两头各钉一条。

**这个文件只管 `[text](dest)` 的**后一半**(链接目标);前一半(显示文字像一条网址)
在 `tests/test_x_article_label.py` —— 第 5 轮补的那条收尾判据和它的两族。分开它们
的是"那个 `[` 在起手式的哪一边",两边的摆盘共用同一份。

**判据一律是后果**(库里那一行到底是什么、导出的 md 里有没有全文),不是"有没有
发过那次询问":只验后者的话,一个把链接当正文存下来、却顺手问了一次的实现也能
全绿;而那一篇一旦以链接入库就永久改不回来。

**另拆一个文件**是因为 `tests/test_x_article_nested.py` 已经 573 行,这一族连它的
反向钉加进去越过仓库那条 700 行上限;而形状本来就不同 —— 那边的样本是"裸 URL
用一个字符紧连",这一族是"那一段连续 URL 字符里混着 markdown 的结构标记"。摆盘
(`FeedgrabRound`、`ArticlePathFormTests` 那两个 round helper)仍共用
`test_x_article_gap.py` 那一份,各写一份的话两边的前提会各漂各的。

跑:  python -m unittest tests.test_x_article_markdown -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import sync
from kb.feeds.x_article import lead_text, needs_article
from kb.feeds.x_article_links import _ARTICLE_LINK, _first, _target_closed
from tests.test_x_article import OLD_WATER
from tests.test_x_article_gap import FULL, answering, article_payload
# 摆盘那两个 round helper 借 `ArticlePathFormTests` 的,但**不许把类名 import
# 进来**:它自己带 `test_*`,而 unittest 按 `dir(module)` 找 TestCase 子类 ——
# 名字落进本模块命名空间就等于让它那几条在这里再跑一遍。走模块对象取属性。
from tests import test_x_article_gap as _gap
from tests.test_x_feedgrab_contract import TODAY, FeedgrabRound

TWEET = 'https://x.com/Example/status/10001'
# 这条推文自己的两种文章地址形式,报告说两种都受影响,所以两种各走一轮端到端。
CANON = 'https://x.com/Example/article/10001'
LEGACY = 'https://x.com/i/article/2108252660529049601'
# **别人**那篇长文,用来钉"链接里面照旧判嵌套"那一头。
OTHER = 'https://x.com/Other/article/20002'
# 浏览器通道那条推文的规范文章地址(= `article_page('https://x.com/example/status/9')`)。
CANON_9 = 'https://x.com/example/article/9'


class MarkdownLinkBoundaryTests(FeedgrabRound):
    """**相邻的 markdown 链接之间没有空白,但它们不是同一条 URL。**

    报告那份输入里三条链接首尾相接,扫描跨过前两条、拿第一条的查询串当容器,
    于是这一篇的长文整个漏掉。判据和前几族一样是后果:库里那一行要有全文、
    前两条参考地址要逐字还在、**导出的 md 里也一样**(它由 `sync.export` 另写
    一遍,少钉一处就少一个消费方)。
    """

    feedgrab_round = _gap.ArticlePathFormTests.feedgrab_round
    browser_round = _gap.ArticlePathFormTests.browser_round

    SHARE = 'https://example.org/?q=foo'
    DOCS = 'https://docs.example.org/'

    def adjacent(self, url):
        """报告那份输入的形状:三条首尾相接的 markdown 链接,最后一条是真地址。"""
        return f'[share]({self.SHARE})[docs]({self.DOCS})[article]({url})'

    def check_feedgrab(self, body, url):
        summary, row, calls, s = self.feedgrab_round(body)
        got = row.get('body') or ''
        self.assertIn(FULL, got, '相邻 markdown 链接把这一篇的长文整个漏掉了')
        self.assertNotIn(url, got, '这条推文自己那条地址照旧要摘掉')
        self.assertIn(self.SHARE, got, '前面那条分享地址一个字都不许动')
        self.assertIn(self.DOCS, got, '中间那条文档地址一个字都不许动')
        self.assertEqual('X书签保存Skill x Teamily', row.get('title'),
                         '标题回落成「某某 的动态」= 压根没去补过')
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(1, len(calls.urls), '那一次询问根本没发出去')

        sync.export('social')
        mds = list(sync.VAULT_ROOT.rglob('*.md'))
        self.assertEqual(1, len(mds), mds)
        md = mds[0].read_text(encoding='utf-8')
        self.assertIn(FULL, md, '导出的 md 里只有三条链接,没有全文')
        self.assertIn(self.SHARE, md)
        self.assertIn(self.DOCS, md)
        return s

    def test_the_adjacent_markdown_links_from_the_report_reach_the_knowledge_base(self):
        """报告那份输入逐字,规范文章路径。"""
        self.check_feedgrab(self.adjacent(CANON), CANON)

    def test_the_legacy_article_path_reaches_it_too(self):
        """同一份输入换成 `/i/article/<id>`:报告说两种验收要求里的路径都受影响,
        只走规范形式的话"只修一半"看不出来。"""
        self.check_feedgrab(self.adjacent(LEGACY), LEGACY)

    def test_the_browser_channel_too(self):
        """另一条保留通道,而且是**没有卡片信号**那一档 —— 正文里那条地址是唯一的
        信号,认不出它就是整篇漏掉。漏哪一条通道都是那条通道上的长文永久空正文。"""
        got, calls = self.browser_round(self.adjacent(CANON_9))
        text = got['entries'][0]['body']
        self.assertIn(FULL, text, '相邻 markdown 链接把这一篇的长文整个漏掉了')
        self.assertNotIn(CANON_9, text, '这条推文自己那条地址照旧要摘掉')
        self.assertIn(self.SHARE, text, '前面那条分享地址一个字都不许动')
        self.assertIn(self.DOCS, text)
        self.assertEqual('', got['incomplete'], '补到了,这一轮算读全了')
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_a_second_sync_keeps_the_full_text(self):
        """**重复同步那一半**:报告第 4 行是"已存入失败结果后,改成补空格的输入再
        同步,`added=0`、库里仍然没有全文" —— 它说的是 `DO NOTHING` 让这一篇
        **永久**改不回来。

        修好之后的反面:第一轮就存对了,第二轮 `added=0` 而库里那一行**逐字不变**。
        """
        s = self.check_feedgrab(self.adjacent(LEGACY), LEGACY)
        first = self.rows(s['id'])['items'][0].get('body') or ''

        req, calls = answering(article_payload())
        with req:
            second = self.collect()
        rows = self.rows(s['id'])['items']
        self.assertEqual([], second['errors'])
        self.assertEqual(0, second['added'], '同一条推文不该再入库一次')
        self.assertEqual(1, len(rows))
        self.assertEqual(first, rows[0].get('body') or '', '第二轮把库里那一行改坏了')
        self.assertIn(FULL, rows[0].get('body') or '')
        self.assertEqual(1, len(calls.urls),
                         '第二轮照旧要为自己那条地址问一次(入库那一步才 DO NOTHING)')

    def test_a_nested_address_inside_one_link_is_still_left_alone(self):
        """**反过来钉一边,而且这一条承重**:边界之后那一段是**同一条链接的目标**,
        照旧要按同一条 URL 判嵌套 —— 一个"`](` 之后整个放行"的实现在这里红。

        别人那条文章地址嵌在链接目标的查询值/片段里时:一次询问都不发、那条地址
        一个字不动,而且**别人那篇文章的全文不许写进来**。最后那半句最重 —— 误认
        不只是改坏别人的链接,还会把别人那篇文章的全文当成这条推文的观点存进知识
        库,而 `DO NOTHING` 让它永久固定。
        """
        ref = f'[参考](https://example.org/?u={OTHER})'
        summary, row, calls, s = self.feedgrab_round(f'读到一篇好文章 {ref}')
        got = row.get('body') or ''
        self.assertEqual([], calls.urls, '别人的链接不许触发一次长文询问')
        self.assertIn(ref, got, '那条地址一个字都不许动')
        self.assertNotIn(FULL, got, '别人那篇文章的全文被当成这条推文的观点存下来了')
        self.assertEqual(1, summary['added'])
        self.assertEqual(TODAY, self.water(s['id']))


class MarkdownBoundaryShapeTests(unittest.TestCase):
    """两个消费方逐形态直测 —— 端到端一轮只摆得下一种,而"链接之间"有好几种写法。

    `needs_article` 是 `search` 语义(漏认 = 空正文永久固定,宁可认宽),
    `lead_text` 是 `sub` 语义(多删 = 作者的字永久消失,宁可少删),所以**每一种
    形态都要两边各问一次**:只问前者的话,一个认出来了却顺手把前面那两条参考地址
    一起删掉的实现也能全绿。
    """

    SHARE = 'https://example.org/?q=foo'
    DOCS = 'https://docs.example.org/'

    def hits(self, body):
        return bool(_first(_ARTICLE_LINK, body))

    def test_every_adjacent_link_shape_is_recognised(self):
        """五种"紧挨着上一条链接"的写法,那条真地址都要认得出、摘得掉,而前面那条
        参考地址一个字不许动。

        三条 `](` 的变体(普通链接、图片、带 title)外加一条引用式在前 —— 它们的
        共同点是候选紧前那两个字符就是 `](`,区别在更左边长什么样。最后一条是
        **报告那份输入**(两条链接连着跨)。
        """
        cases = (
            (f'[share]({self.SHARE})[article]({CANON})', (self.SHARE,)),
            (f'![图]({self.SHARE})[article]({CANON})', (self.SHARE,)),
            (f'[a](https://example.org/?q=1 "题")[article]({CANON})',
             ('https://example.org/?q=1',)),
            (f'[share][1][article]({CANON})', ('[share][1]',)),
            (f'[share]({self.SHARE})[docs]({self.DOCS})[article]({CANON})',
             (self.SHARE, self.DOCS)),
        )
        for body, keep in cases:
            with self.subTest(body=body):
                self.assertTrue(needs_article({'url': TWEET, 'body': body}),
                                '紧挨着上一条链接的那条文章地址漏认了')
                lead = lead_text(body)
                self.assertNotIn(CANON, lead, '真的那条地址照旧要摘掉')
                for part in keep:
                    self.assertIn(part, lead, '作者写在前面那一段被一起删掉了')

    def test_a_nested_address_inside_a_link_target_is_still_hidden(self):
        """**反过来钉一边**:`](` 只是边界,边界之后那一段仍然是同一条 URL ——
        嵌在链接目标的查询值、片段、路径段里的那条地址照旧判成嵌套。

        外加报告明确警告的那一条:`?target=(<地址>)` 不许因为"括号当了终止符"而
        重新破掉 —— 那是第 1 轮修好的分享参数,它的候选紧前也是一个 `(`,区别只在
        它左边是 `=` 而不是 `]`。
        """
        cases = (
            f'[a](https://example.org/?u={OTHER})',
            f'[a](https://example.org/p#{OTHER})',
            f'[a](https://example.org/go/{OTHER})',
            f'https://example.org/share?target=({OTHER})',
            f'[a](https://example.org/share?target=({OTHER}))',
            # 链接目标里的**平衡**括号是它路径的内容(CommonMark 明文允许),
            # 所以这条目标还开着、照旧是容器 —— 见不平衡那道判据的契约。
            f'[a](https://example.org/docs(v2)#{OTHER})',
            f'[a](https://example.org/p(a)/b?u={OTHER})',
        )
        for body in cases:
            with self.subTest(body=body):
                self.assertFalse(needs_article({'url': TWEET, 'body': body}),
                                 '别人链接里那条地址不是这条推文的长文')
                self.assertEqual(body, lead_text(body).strip(),
                                 '别人那条地址被从中间剪断了')

    def test_the_boundary_is_the_markdown_target_head_not_any_bracket(self):
        """直测那道边界本身的契约:认的是 `](` 这**两个字符**,不是"任何括号"。

        一律把 `(` 当边界的话上一条那两种分享参数全部重新破掉;一律把 `)` 当
        边界的话第 5 轮修好的 `…/docs(v2)#<地址>` 重新破掉。所以两种"隔着一条
        闭合链接"的写法反过来各钉一条:`](` 之前那条链接不算容器,而**同一条 URL
        的路径里**的平衡括号照旧跨得过去。
        """
        self.assertTrue(self.hits(f'[share]({self.SHARE})[article]({CANON})'),
                        '`](` 之前那条已经闭合的链接被当成了容器')
        self.assertFalse(self.hits(f'https://example.org/docs(v2)#{OTHER}'),
                         '把 `)` 一律当终止符会让外层路径里那条地址被剪断')
        self.assertFalse(self.hits(f'https://example.org/p(a)?u={OTHER}'),
                         '同一条 URL 的路径括号照旧要跨得过去')


class ShareValueBracketTests(FeedgrabRound):
    """**单条分享 URL 的查询值里写着一段 markdown,那个 `](` 是它的内容。**

    报告那份输入只有**一条**外部链接(autolink 形式),它的 `text=` 装着
    `[read](<别人的文章地址>)`。第 3 轮那版把 `](` 当扫描终止符,于是 `run` 停在
    候选紧前、丢掉了外层 `https://` 和它的 `?` —— 容器找不到,候选被当成这条推文
    自己的文章地址:`_drop` 把它摘成 `…?text=[read]( )`,而 `_first` 还会为它白
    发一次长文询问。`added=1`、`errors=[]`、`partial=[]`、水位照常推进,
    `DO NOTHING` 让这一篇**永久**停在残稿上(审查实测:把方括号改成 `%5B`/`%5D`
    再同步,真实 SQL 调用前的正文已经是完整的,`added` 仍是 0、库里那一行不变)。

    判据一律是后果:库里那一行、**以及导出的 md**(`sync.export` 另写一遍)里,
    别人那条地址要逐字还在。
    """

    feedgrab_round = _gap.ArticlePathFormTests.feedgrab_round
    browser_round = _gap.ArticlePathFormTests.browser_round

    # 报告那份输入逐字:一条完整的外部分享链接,查询值里是一段 markdown。
    SHARE = f'https://example.org/share?text=[read]({OTHER})'

    def check_feedgrab(self, body, url):
        summary, row, calls, s = self.feedgrab_round(body)
        got = row.get('body') or ''
        self.assertIn(self.SHARE, got,
                      '分享链接查询值里那条地址被删掉了(报告 F1 的末态)')
        self.assertIn(FULL, got, '这条推文自己那一篇的全文没补进来')
        self.assertNotIn(url, got, '这条推文自己那条地址照旧要摘掉')
        self.assertEqual('X书签保存Skill x Teamily', row.get('title'))
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(1, len(calls.urls), '只该为自己那条地址问一次')

        sync.export('social')
        mds = list(sync.VAULT_ROOT.rglob('*.md'))
        self.assertEqual(1, len(mds), mds)
        md = mds[0].read_text(encoding='utf-8')
        self.assertIn(self.SHARE, md, '导出的 md 里那条分享地址也缺了一截')
        self.assertIn(FULL, md)
        return s

    def test_the_share_value_from_the_report_keeps_its_inner_address(self):
        """报告那份最小输入逐字(两行:自己的长文 + 一条分享参考),规范路径。"""
        self.check_feedgrab(f'我的长文 {CANON}\n分享参考 <{self.SHARE}>', CANON)

    def test_the_legacy_article_path_too(self):
        """同一份输入换成 `/i/article/<id>` —— 报告说两种路径逐字复现同一问题。"""
        self.check_feedgrab(f'我的长文 {LEGACY}\n分享参考 <{self.SHARE}>', LEGACY)

    def test_the_browser_channel_too(self):
        """另一条保留通道,同样是**没有卡片信号**那一档。"""
        got, calls = self.browser_round(
            f'我的长文 {CANON_9}\n分享参考 <{self.SHARE}>')
        text = got['entries'][0]['body']
        self.assertIn(self.SHARE, text, '分享链接查询值里那条地址被删掉了')
        self.assertIn(FULL, text)
        self.assertNotIn(CANON_9, text)
        self.assertEqual('', got['incomplete'])
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_a_share_reference_alone_asks_nothing(self):
        """**承重的反向钉**(报告表格最后两行):推文里**只有**那条分享参考时,
        一次长文询问都不许发、那条地址一个字不动,而且**别人那篇文章的全文不许
        写进来** —— 最后半句最重:误认不只是改坏别人的链接,还会把别人那篇的全文
        当成这条推文的观点存进知识库,而 `DO NOTHING` 让它永久固定。
        """
        summary, row, calls, s = self.feedgrab_round(f'分享参考 <{self.SHARE}>')
        got = row.get('body') or ''
        self.assertEqual([], calls.urls, '别人的分享链接不许触发一次长文询问')
        self.assertIn(self.SHARE, got, '那条地址一个字都不许动')
        self.assertNotIn(FULL, got, '别人那篇文章的全文被当成这条推文的观点存下来了')
        self.assertEqual(1, summary['added'])
        self.assertEqual(TODAY, self.water(s['id']))

    def test_a_second_sync_keeps_the_reference_whole(self):
        """报告第 3 行说的是 `DO NOTHING` 让残稿**永久**固定(重新同步 `added=0`)。
        反面:第一轮就存对了,第二轮 `added=0` 而库里那一行逐字不变。"""
        s = self.check_feedgrab(f'我的长文 {CANON}\n分享参考 <{self.SHARE}>', CANON)
        first = self.rows(s['id'])['items'][0].get('body') or ''

        req, calls = answering(article_payload())
        with req:
            second = self.collect()
        rows = self.rows(s['id'])['items']
        self.assertEqual([], second['errors'])
        self.assertEqual(0, second['added'])
        self.assertEqual(1, len(rows))
        self.assertEqual(first, rows[0].get('body') or '', '第二轮把库里那一行改坏了')
        self.assertIn(self.SHARE, rows[0].get('body') or '')


class ShareValueBracketShapeTests(unittest.TestCase):
    """两个消费方逐形态直测 —— 报告说片段、t.co、编码写法都逐字复现同一问题,
    而端到端一轮只摆得下一种。

    外加两条**契约**,它们各自守着修法里的一半:
    ①闭合判据只对"紧前是 `](`"的起手式问(否则路径里一个孤立 `)` 的裸 URL 被误判
      成闭合,别人的地址照样被摘);
    ②`_target_closed` 自己只认**不平衡**的 `)`(平衡的那对是 URL 路径的内容)。
    """

    def hits(self, body):
        return bool(_first(_ARTICLE_LINK, body))

    def test_every_markdown_in_query_shape_stays_whole(self):
        """六种"外层 URL 的值里混着 markdown 标记"的写法,那条地址都要判成嵌套、
        一个字不动。

        它们的共同点是候选紧前那两个字符就是 `](`,而**它左边是一条还开着的外层
        URL** —— 和第二族那几条"隔着一条已经闭合的链接"正好相反,分开它们的就是
        那条 URL 闭合了没有。
        """
        cases = (
            f'https://example.org/share?text=[read]({OTHER})',          # 报告那条
            f'<https://example.org/share?text=[read]({OTHER})>',        # autolink 形式
            f'https://example.org/share#note=[read]({OTHER})',          # 片段里同形
            f'www.example.org/share?text=[read]({OTHER})',              # 裸域名起手式
            f'https://example.org/s?a=1&text=[read]({OTHER})',          # 第二个参数
            f'https://example.org/share?text=[a](https://docs.example.org/)+[b]({OTHER})',
        )
        for body in cases:
            with self.subTest(body=body):
                self.assertFalse(needs_article({'url': TWEET, 'body': body}),
                                 '分享值里那条地址不是这条推文的长文')
                self.assertEqual(body, lead_text(body).strip(),
                                 '分享值里那条地址被从中间剪断了')

    def test_a_tco_link_in_a_share_value_stays_whole(self):
        """t.co 那一侧的独立射程:候选是 t.co 自己,`_ARTICLE_LINK` 压根不认它,
        所以这一条只有 `_drop(_TCO, …)` 那一路会碰。配一条**阴性对照** ——
        同一条短链裸着写时照旧摘得掉,不然"判据生效了"和"t.co 那一侧压根不摘"
        长得一模一样。"""
        tco = 'http://t.co/aBcD12'
        nested = f'分享参考 <https://example.org/share?text=[read]({tco})>'
        self.assertEqual(nested, lead_text(nested).strip(),
                         '分享值里那条短链被剪断了')
        bare = lead_text(f'看这个 {tco}')
        self.assertNotIn(tco, bare, '裸写的 t.co 短链照旧要摘掉')
        self.assertIn('看这个', bare, '作者写的那句话不许跟着走')

    def test_the_closed_check_only_applies_to_a_markdown_target(self):
        """**契约①**:只有"紧前是 `](`"的起手式才问闭合。

        裸 URL 的路径里一个孤立 `)` 是 sub-delims、合法;不限定的话它会被当成
        "这条 URL 已经闭合",容器找不到、别人那条地址被 `_drop` 永久摘掉。
        反过来钉一边:紧前真是 `](` 而且真闭合了的那条,照旧不算容器。
        """
        bare = f'https://example.org/a)b?u={OTHER}'
        self.assertFalse(self.hits(bare), '裸 URL 路径里那个孤立 `)` 不是链接收尾')
        self.assertEqual(bare, lead_text(bare).strip(), '别人那条地址被剪断了')
        self.assertTrue(self.hits(f'[a](https://example.org/?q=1)[b]({CANON})'),
                        '已经闭合的 markdown 目标被当成了容器')

    def test_a_bare_url_right_after_a_closed_link_is_its_own_address(self):
        """第 3 轮记成"已知的缝"、换成闭合判据之后顺手好了的那一条:链接闭合之后
        **紧贴**一个裸 URL,那是两条并列的地址。

        钉住它是为了防回退 —— 退回"`](` 当终止符"的实现在这里红(那条路上没有
        `](` 可认,扫描一路跨到第一条链接的查询串上)。
        """
        for body in (f'[share](https://example.org/?q=foo){CANON}',
                     f'[share](https://example.org/?q=foo)({CANON})'):
            with self.subTest(body=body):
                self.assertTrue(self.hits(body), '闭合链接之后那条地址是自己的')
                self.assertNotIn(CANON, lead_text(body), '真的那条地址照旧要摘掉')
                self.assertIn('https://example.org/?q=foo', lead_text(body))

    def test_a_closed_target_does_not_stop_the_search_for_a_container(self):
        """**逐个往后找"还开着"的那条**,这是它的独占射程:最左那个起手式已经闭合
        (一条写完的 markdown 链接),而紧跟着的裸 URL 还开着、候选就嵌在它的查询值
        或片段里 —— 容器是后面那条。

        只看最左那一个起手式的实现在这里红:它判出"闭合了"就直接放行,别人那条
        地址被 `_drop` 永久摘掉。
        """
        for body in (
                f'[a](https://example.org/?q=1)https://evil.example/?u={OTHER}',
                f'[a](https://example.org/?q=1)https://evil.example/s?text=Read+{OTHER}',
                f'[a](https://example.org/?q=1)www.evil.example/go/{OTHER}'):
            with self.subTest(body=body):
                self.assertFalse(needs_article({'url': TWEET, 'body': body}),
                                 '后面那条外层 URL 才是容器')
                self.assertEqual(body, lead_text(body).strip(),
                                 '别人那条地址被从中间剪断了')

    def test_only_an_unbalanced_paren_closes_a_target(self):
        """**契约②**:直测 `_target_closed` —— 平衡的那对括号是 URL 路径的内容
        (第 5 轮那条 `…/docs(v2)#<地址>` 的全部依据),只有落单的 `)` 收尾。"""
        self.assertTrue(_target_closed('example.org/?q=foo)'))
        self.assertTrue(_target_closed('example.org/p(a)/b)'))
        self.assertFalse(_target_closed('example.org/docs(v2)#'))
        self.assertFalse(_target_closed('example.org/?u='))
        self.assertFalse(_target_closed('example.org/p(a)?u='))


if __name__ == '__main__':
    unittest.main()
