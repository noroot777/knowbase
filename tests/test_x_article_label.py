# -*- coding: utf-8 -*-
"""长文地址的起点:markdown 链接的**显示文字**像一条网址时,它不是一条 URL。

**这一族管 `[text](dest)` 的前一半**(2026-10-10 自由工作流第 5 轮审查)。起点判据
那一步从候选往左逐字吃 `_URL_RUN` 字符找"包着候选的那条外层 URL 的起点",而
RFC 3986 把 `(` `)` `[` `]` 都算合法 URL 字符、feedgrab 的产物正文又是 markdown
—— 相邻几条链接之间一个空白都没有,整段连成一个 run。第 3、4 轮给**目标**那一半配
了收尾判据(`_target_closed`,见 `tests/test_x_article_markdown.py`),可它只问"紧前
是 `](`"的起手式,于是落在**显示文字**里的起手式一律当"还开着":

```text
[www.example.org](https://example.org/?q=foo)[article](<真地址>)
```

最左那个起手式是 `www.`,紧前是 `[` —— 闭合判据压根不问它,而它的 tail 里有真目标
的 `?q=foo`,于是 (b) 那条证据成立:这条推文**自己的**文章地址被判成"嵌在别人的查询
值里",`needs_article` 三条判据一条都不命中,那两条链接本身当成正文入库 ——
`added=1`、`errors=[]`、`partial=[]`、接口调用 **0** 次、水位照常推进、标题回落成
「某某 的动态」,而 `put_items` 是 `ON CONFLICT(key) DO NOTHING`:**加空格再同步
`added=0`**,库里和导出的 md 里仍然只有那两条链接。

**变量就是"显示文字像不像网址"** —— 只把它换成 `docs`、一个链接目标都不改,同一份
输入就拿得到全文。显示文字换成 `https://example.org/`、目标换成片段、两种文章路径
(`/<handle>/article/<id>` 与 `/i/article/<id>`),四种形态逐字复现同一个根因。而用
网址当链接的显示文字是极常见的写法,所以这不是边角。

修法是给另一半也配一条收尾判据(`_text_closed`):`]` 紧跟一个 `(` 时它结束的是 link
**text**,而"落单"(扫到它时 `[` 深度为 0)等价于**那个 `[` 在起手式之前** —— 也就是
起手式本身落在显示文字里面。`test_x_article_markdown.py` 第三族那条
`?text=[read](<别人的地址>)` 里的 `[` 在起手式**之后**、配对,所以不落单、外层 URL
还开着、照旧判嵌套:**两族的分界线就是这个深度计数**,这个文件两头各钉一条。

**判据一律是后果**(库里那一行到底是什么、导出的 md 里有没有全文),不是"有没有发过
那次询问":只验后者的话,一个把链接当正文存下来、却顺手问了一次的实现也能全绿;而
那一篇一旦以链接入库就永久改不回来。

**另拆一个文件**是因为 `tests/test_x_article_markdown.py` 已经 694 行、贴着仓库那条
700 行上限;而形状本来就不同 —— 那边的样本是"候选紧前那两个字符就是 `](`",这一族是
"最左那个起手式落在一段**显示文字**里"。摆盘(`FeedgrabRound`、
`ArticlePathFormTests` 那两个 round helper)仍共用 `test_x_article_gap.py` 那一份,
各写一份的话两边的前提会各漂各的。

跑:  python -m unittest tests.test_x_article_label -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import sync
from kb.feeds.x_article import lead_text, needs_article
from kb.feeds.x_article_links import _ARTICLE_LINK, _first, _text_closed
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
# **别人**那篇长文,用来钉"跳过显示文字不等于放行"那一头。
OTHER = 'https://x.com/Other/article/20002'
# 浏览器通道那条推文的规范文章地址(= `article_page('https://x.com/example/status/9')`)。
CANON_9 = 'https://x.com/example/article/9'


class LinkLabelUrlTests(FeedgrabRound):
    """**链接的显示文字像一条网址时,它压根不是一条 URL —— 旁边那篇长文还得补。**

    报告那份输入里第一条链接的**显示文字**是一个网址(`[www.example.org](…)`),
    紧跟着的才是真正的文章链接。闭合判据只问"紧前是 `](`"的起手式,于是显示文字里
    那个 `www.` 被当成一条还开着的外层 URL,它的 tail 又含着真目标的 `?q=foo` ——
    这条推文自己的地址被判成"嵌在别人的查询值里",长文一次都没问、两条链接当成
    正文入库,而 `DO NOTHING` 让它永久固定(加空格再同步 `added=0`)。

    判据一律是后果:库里那一行、**以及导出的 md**(`sync.export` 另写一遍)里要有
    全文,而前面那条参考地址逐字还在。
    """

    feedgrab_round = _gap.ArticlePathFormTests.feedgrab_round
    browser_round = _gap.ArticlePathFormTests.browser_round

    # 报告那份输入逐字:显示文字是一个裸域名,目标是一条带查询串的外部地址。
    LABEL = 'www.example.org'
    REF = 'https://example.org/?q=foo'

    def labelled(self, url, label=None, ref=None):
        """报告那份输入的形状:显示文字像网址的参考链接 + 紧跟着的真地址。"""
        return f'[{label or self.LABEL}]({ref or self.REF})[article]({url})'

    def check_feedgrab(self, body, url, ref=None):
        summary, row, calls, s = self.feedgrab_round(body)
        got = row.get('body') or ''
        self.assertIn(FULL, got, '显示文字像网址,把这一篇的长文整个漏掉了')
        self.assertNotIn(url, got, '这条推文自己那条地址照旧要摘掉')
        self.assertIn(ref or self.REF, got, '前面那条参考地址一个字都不许动')
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
        self.assertIn(FULL, md, '导出的 md 里只有那两条链接,没有全文')
        self.assertIn(ref or self.REF, md)
        return s

    def test_the_report_input_reaches_the_knowledge_base(self):
        """报告那份输入逐字,规范路径。"""
        self.check_feedgrab(self.labelled(CANON), CANON)

    def test_the_legacy_article_path_too(self):
        """换成 `/i/article/<id>` —— 报告说两种路径逐字复现同一个根因。"""
        self.check_feedgrab(self.labelled(LEGACY), LEGACY)

    def test_the_browser_channel_too(self):
        """另一条保留通道,同样是**没有卡片信号**那一档(有卡片时判据② 会把它救
        回来,漏判就看不出来了)。"""
        got, calls = self.browser_round(self.labelled(CANON_9))
        text = got['entries'][0]['body']
        self.assertIn(FULL, text, '显示文字像网址,把长文整个漏掉了')
        self.assertNotIn(CANON_9, text)
        self.assertIn(self.REF, text, '前面那条参考地址一个字都不许动')
        self.assertEqual('', got['incomplete'])
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_a_second_sync_keeps_the_full_text(self):
        """报告表格第 5 行说的是 `DO NOTHING` 让残缺正文**永久**固定(加空格再同步
        `added=0`、库里仍然只有链接)。反面:第一轮就存对了,第二轮 `added=0` 而库里
        那一行逐字不变。"""
        s = self.check_feedgrab(self.labelled(CANON), CANON)
        first = self.rows(s['id'])['items'][0].get('body') or ''

        req, calls = answering(article_payload())
        with req:
            second = self.collect()
        rows = self.rows(s['id'])['items']
        self.assertEqual([], second['errors'])
        self.assertEqual(0, second['added'])
        self.assertEqual(1, len(rows))
        self.assertEqual(first, rows[0].get('body') or '', '第二轮把库里那一行改坏了')
        self.assertIn(FULL, rows[0].get('body') or '')


class LinkLabelUrlShapeTests(unittest.TestCase):
    """两个消费方逐形态直测 —— 报告给了四种形态,而端到端一轮只摆得下一种。

    外加两条**契约**,各守着这条修法的一半:
    ①`]` 后面必须紧跟 `(`(否则路径里一个孤立 `]` 的裸 URL 被当成"显示文字",
      别人的地址照样被摘);
    ②`_text_closed` 只认**落单**的 `]`(配对的那个是 IPv6 字面量或显示文字自己的
      内容,而落单等价于"那个 `[` 在起手式之前")。
    """

    LABEL = 'www.example.org'
    REF = 'https://example.org/?q=foo'

    def hits(self, body):
        return bool(_first(_ARTICLE_LINK, body))

    def test_every_url_like_label_still_finds_the_article(self):
        """报告那四种形态 + 两条对照,真地址都要认得出、摘得掉,而参考地址一个字
        不许动。

        后两条是**本来就对的**对照(显示文字不像网址、两条链接之间加空格),它们让
        这套用例分得出"显示文字那一道失效了"和"整条判据都没了"。
        """
        cases = (
            (f'[{self.LABEL}]({self.REF})[article]({CANON})', self.REF),
            # 显示文字是一条带 scheme 的完整地址。
            (f'[https://example.org/]({self.REF})[article]({CANON})', self.REF),
            # 目标换成片段(报告说片段形态逐字相同)。
            (f'[{self.LABEL}](https://example.org/#section)[article]({CANON})',
             'https://example.org/#section'),
            # 旧式文章路径。
            (f'[{self.LABEL}]({self.REF})[article]({LEGACY})', self.REF),
            # 对照一:显示文字不像网址 —— 这一档本来就拿得到全文。
            (f'[docs]({self.REF})[article]({CANON})', self.REF),
            # 对照二:两条链接之间加空格 —— run 在空白处就断了。
            (f'[{self.LABEL}]({self.REF}) [article]({CANON})', self.REF),
        )
        for body, keep in cases:
            url = LEGACY if LEGACY in body else CANON
            with self.subTest(body=body):
                self.assertTrue(needs_article({'url': TWEET, 'body': body}),
                                '显示文字像网址,旁边那条文章地址漏认了')
                lead = lead_text(body)
                self.assertNotIn(url, lead, '真的那条地址照旧要摘掉')
                self.assertIn(keep, lead, '前面那条参考地址被一起删掉了')

    def test_a_url_like_label_does_not_unhide_a_nested_address(self):
        """**反过来钉一边**:显示文字像网址,不代表它后面那条链接目标里的地址就
        成了自己的 —— 嵌在目标查询值 / 片段 / 路径段里的那条照旧判成嵌套。

        这是跳过显示文字之后新的风险面:跳过了不等于放行,后面还有一条真的外层
        URL 等着被问。
        """
        cases = (
            f'[{self.LABEL}](https://example.org/?u={OTHER})',
            f'[{self.LABEL}](https://example.org/p#{OTHER})',
            f'[{self.LABEL}](https://example.org/go/{OTHER})',
            f'[{self.LABEL}](https://example.org/share?text=Read+{OTHER})',
            # 显示文字像网址 + 一条已经闭合的链接 + 还开着的裸 URL:容器是最后那条。
            f'[{self.LABEL}]({self.REF})https://evil.example/?u={OTHER}',
        )
        for body in cases:
            with self.subTest(body=body):
                self.assertFalse(needs_article({'url': TWEET, 'body': body}),
                                 '别人链接里那条地址不是这条推文的长文')
                self.assertEqual(body, lead_text(body).strip(),
                                 '别人那条地址被从中间剪断了')

    def test_the_label_check_requires_a_paren_right_after(self):
        """**契约①**:`]` 后面紧跟 `(` 才算显示文字收尾。

        路径里的 `]` 是合法 URL 字符 —— 落单的那种(`/a]b?u=…`)和配对的那种
        (IPv6 字面量 `https://[2001:db8::1]/docs#…`)都不是链接结构,不限定的话
        这两条**还开着**的外层 URL 会被当成"显示文字",别人那条地址被永久摘掉。
        """
        for body in (f'https://example.org/a]b?u={OTHER}',
                     f'https://[2001:db8::1]/docs#{OTHER}',
                     f'https://[2001:db8::1]:8443/docs?u={OTHER}'):
            with self.subTest(body=body):
                self.assertFalse(self.hits(body),
                                 '路径里的 `]` 不是一段显示文字的收尾')
                self.assertEqual(body, lead_text(body).strip(),
                                 '别人那条地址被剪断了')

    def test_only_an_unpaired_bracket_closes_a_label(self):
        """**契约②**:直测 `_text_closed` —— 落单的 `](` 才收尾。

        配对的那个是显示文字自己的内容或 IPv6 字面量,它意味着那个 `[` 在起手式
        **之后**,也就是第三族那条"分享查询值里装着一段 markdown" —— 那时外层
        URL 还开着。两族的分界线就在这里。
        """
        self.assertTrue(_text_closed('example.org]('))
        self.assertTrue(_text_closed('example.org/a](https://e.org/)[b]('))
        # 配对:`[` 在起手式之后 —— 第三族那条分享查询值。
        self.assertFalse(_text_closed('example.org/share?text=[read]('))
        # IPv6 字面量那对方括号同样是配对的。
        self.assertFalse(_text_closed('[2001:db8::1]/docs#'))
        # 落单但不紧跟 `(` —— 路径里一个孤立的 `]`,这条 URL 还开着。
        self.assertFalse(_text_closed('example.org/a]b?u='))
        self.assertFalse(_text_closed('example.org/?u='))

if __name__ == '__main__':
    unittest.main()
