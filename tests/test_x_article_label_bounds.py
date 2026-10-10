# -*- coding: utf-8 -*-
"""长文地址的起点:markdown 链接的**显示文字**可以装任意字符,所以它的左边界不是
一段 URL 字符。

**这一族管第 6 轮那次修法自己开出来的缺口**(2026-10-10 自由工作流第 7 轮审查)。
起点判据那一步从候选往左逐字吃 `_URL_RUN` 字符找"包着候选的那条外层 URL 的起点";
第 5 轮给**显示文字**那一半配了收尾判据(`_text_closed`),第 6 轮又给它补上左边那
一半(「`head` 里要有一个**还开着**的 `[`」)—— 而那个 `head` 当时取的是 **run 的
左边界**,也就是用 URL 字符集给 markdown 划界:

```text
[Visit www.example.org](https://example.org/?q=foo)[article](<真地址>)
```

`_URL_RUN` 是 RFC 3986 的字符全集,里面**没有空白、没有非 ASCII** —— 于是扫描停在
第 7 个字符,排除掉的前缀恰好是 `[Visit `,最左那个起手式 `www.` 的 `head` 成了
**空的**。左边那道闸据此答"不是显示文字",它被当成一条还开着的外层 URL,而它的
tail 里有真目标的 `?q=foo`:这条推文**自己的**文章地址又一次被判成"嵌在别人的查询
值里",`needs_article` 三条判据一条都不命中,那两条链接本身当成正文入库 ——
`added=1`、`errors=[]`、`partial=[]`、**接口调用 0 次**、水位照常推进、标题回落成
「某某 的动态」,而 `put_items` 是 `ON CONFLICT(key) DO NOTHING`:两条链接之间
**加一个空格再同步 `added=0`**,库里和导出的 md 里仍然只有那两条链接(真实 SQL 调用
前的正文那一轮已经完整,写不进去)。

**变量就是"显示文字里有没有非 URL 字符"** —— 把说明改成全 ASCII 无空格的
`Visit:www.example.org`、一个链接目标都不改,同一份输入就拿得到全文;中文说明
(`阅读www.example.org`,**连空格都不需要**)、emoji 前缀(`🔗www.example.org`)、
显示文字自带 scheme(`Visit https://example.org/`)、片段容器、旧式文章路径,六种
形态逐字复现同一个根因,而上一轮版本 `7f184c5` 对同一份输入全部拿得到全文。而
「链接文字前面写一句说明」是最常见的写法之一,所以这不是边角。

修法是把左边那一半的扫描范围从 run 换成**本行**。三档里行是那个可解释的中间点:
run 的左边界漏判上面这六种(自己的长文永久漏抓);整篇正文会让正文里**任何**一个
落单的 `[` 都满足这道闸,而第 6 轮那一档的全部特征就是"`head` 为空" —— 它当场重开
(别人的地址被永久剪断);本行既覆盖这六种(markdown 产物和推文正文里一条链接必然
写在一行内),又把误判面关在同一行里。**本文件两头各钉一条**:带说明的显示文字要
认得出(族一、族二的矩阵),而**上一行**那个落单的 `[` 不算这一行显示文字的开头
(族一最后一条 + 族二那条契约)。

**判据一律是后果**(库里那一行到底是什么、导出的 md 里有没有全文),不是"有没有发过
那次询问":只验后者的话,一个把链接当正文存下来、却顺手问了一次的实现也能全绿;而
那一篇一旦以链接入库就永久改不回来。

**而范围里那个 `[` 算不算 markdown 结构字符,又是第 8 轮的根因** —— 本行里一个
**字面**的方括号(转义的、行内代码里的)不是显示文字的开头,那一族在
`tests/test_x_article_label_literal.py`。

**另拆一个文件**是因为 `tests/test_x_article_label.py` 已经 490 行,加这一族越过仓库
那条 700 行上限;而形状本来就不同 —— 那边两族的样本是"显示文字**整个**就是一个
网址"和"它只是**看着**像显示文字、其实是外层 URL 自己的内容",这一族是"显示文字里
混着 URL 字符集外面的字"。摆盘(`FeedgrabRound`、`ArticlePathFormTests` 那两个
round helper)仍共用 `test_x_article_gap.py` 那一份,各写一份的话两边的前提会各漂
各的。

跑:  python -m unittest tests.test_x_article_label_bounds -v
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
# **别人**那篇长文,用来钉"认出显示文字不等于放行"那一头。
OTHER = 'https://x.com/Other/article/20002'
# 浏览器通道那条推文的规范文章地址(= `article_page('https://x.com/example/status/9')`)。
CANON_9 = 'https://x.com/example/article/9'
# 报告那条参考链接的目标:带查询串,所以 (b) 那条证据会成立 —— 这正是误判的来源。
REF = 'https://example.org/?q=foo'
# 报告那六种显示文字。共同点:`[` 和那个起手式之间有一个 `_URL_RUN` 外面的字符。
PROSE = (
    ('english', 'Visit www.example.org', REF, CANON),
    ('chinese', '阅读www.example.org', REF, CANON),
    ('emoji', '🔗www.example.org', REF, CANON),
    ('scheme', 'Visit https://example.org/', REF, CANON),
    ('fragment', 'Visit www.example.org', 'https://example.org/#section', CANON),
    ('legacy', 'Visit www.example.org', REF, LEGACY),
)
# 上一行留下一个**落单**的 `[`。它钉住范围的上界:取整篇正文的话这一行那个起手式
# 的 head 就含着它,第 6 轮那一档(外层 URL 自己内容里的 `](`)当场重开。
OWN_BRACKET = f'https://example.org/share?text=read]({OTHER})'
STRAY = '见 [注\n分享参考 <%s>' % OWN_BRACKET
# 这个取舍的另一头:显示文字跨**软换行**时 CommonMark 照旧把它当一段 link text
# (实测渲染出两条独立链接,第一条的 text 逐字是 `注\n说明 www.example.org`),而
# 按本行取范围就判成嵌套、自己的长文在这一档漏抓。钉成**当前取舍**,改方向时要和
# 上面那条一起改。
WRAPPED = f'见 [注\n说明 www.example.org]({REF})[article]({CANON})'


def decorated(label, url=CANON, ref=REF):
    """报告那份输入的形状:显示文字里带一句说明的参考链接 + 紧跟着的真地址。"""
    return f'[{label}]({ref})[article]({url})'


class DecoratedLabelTests(FeedgrabRound):
    """**显示文字里带一句说明时,旁边那篇长文还得补得到。**

    报告那份输入里第一条链接的显示文字是「Visit www.example.org」—— 那个空格把
    `[` 截在 URL 字符段之外,左边那道闸拿不到它,于是显示文字里那个 `www.` 被当成
    一条还开着的外层 URL,它的 tail 又含着真目标的 `?q=foo`:这条推文自己的地址被
    判成"嵌在别人的查询值里",长文一次都没问、两条链接当成正文入库,而 `DO NOTHING`
    让它永久固定(加空格再同步 `added=0`)。

    判据一律是后果:库里那一行、**以及导出的 md**(`sync.export` 另写一遍)里要有
    全文,而前面那条参考地址逐字还在。
    """

    feedgrab_round = _gap.ArticlePathFormTests.feedgrab_round
    browser_round = _gap.ArticlePathFormTests.browser_round

    def check_feedgrab(self, body, url, ref=REF):
        summary, row, calls, s = self.feedgrab_round(body)
        got = row.get('body') or ''
        self.assertIn(FULL, got, '显示文字里那个非 URL 字符把 `[` 截出了扫描范围,'
                                 '这一篇的长文整个漏掉了')
        self.assertNotIn(url, got, '这条推文自己那条地址照旧要摘掉')
        self.assertIn(ref, got, '前面那条参考地址一个字都不许动')
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
        self.assertIn(ref, md)
        return s

    def test_the_report_input_reaches_the_knowledge_base(self):
        """报告那份输入逐字:英文说明 + 空格 + 一个裸域名当显示文字。"""
        self.check_feedgrab(decorated('Visit www.example.org'), CANON)

    def test_a_chinese_label_too(self):
        """中文说明 —— 报告专门记了一句「不需要空格也会失败」:汉字本来就不在
        `_URL_RUN` 里面。"""
        self.check_feedgrab(decorated('阅读www.example.org'), CANON)

    def test_an_emoji_label_too(self):
        """emoji 前缀,同样落在字符集外面。"""
        self.check_feedgrab(decorated('🔗www.example.org'), CANON)

    def test_a_label_carrying_a_full_url_too(self):
        """说明后面那个网址自带 scheme —— 换的是起手式的形状,截断的位置没变。"""
        self.check_feedgrab(decorated('Visit https://example.org/'), CANON)

    def test_a_fragment_container_too(self):
        """参考链接的目标换成片段容器(报告第五个变体)。"""
        ref = 'https://example.org/#section'
        self.check_feedgrab(decorated('Visit www.example.org', ref=ref), CANON, ref)

    def test_the_legacy_article_path_too(self):
        """换成 `/i/article/<id>` —— 报告说两种路径逐字复现同一个根因。"""
        self.check_feedgrab(decorated('Visit www.example.org', url=LEGACY), LEGACY)

    def test_the_browser_channel_too(self):
        """另一条保留通道,同样是**没有卡片信号**那一档(有卡片时判据② 会把它救
        回来,漏判就看不出来了)。"""
        got, calls = self.browser_round(decorated('Visit www.example.org', url=CANON_9))
        text = got['entries'][0]['body']
        self.assertIn(FULL, text, '显示文字里带说明,把长文整个漏掉了')
        self.assertNotIn(CANON_9, text)
        self.assertIn(REF, text, '前面那条参考地址一个字都不许动')
        self.assertEqual('', got['incomplete'])
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_a_second_sync_keeps_the_full_text(self):
        """报告表格第 3 行说的是"重试不能自愈":加空格之后第二次同步确实取到了
        全文、也确实把它交给了真实 `put_items`,可 `DO NOTHING` 之下 `added=0`,
        库里仍然逐字保留只有链接的那份正文。

        反面:第一轮就存对了,第二轮 `added=0` 而库里那一行逐字不变。
        """
        s = self.check_feedgrab(decorated('Visit www.example.org'), CANON)
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

    def test_a_stray_bracket_on_another_line_keeps_the_reference_whole(self):
        """**承重的反向钉:范围的上界。**

        上一行留着一个落单的 `[`(漏了右括号的注记、一段代码)。把左边那一半的
        范围放成整篇正文的话,这一行那个外层起手式的 `head` 就含着它 —— 第 6 轮
        那一档(外层 URL 自己内容里的 `](`)当场重开:别人那条地址被摘成
        `…?text=read]( )`,`_first` 还为它白发一次长文询问,而别人那篇的全文会被
        当成这条推文的观点存进来,`DO NOTHING` 让它永久固定。
        """
        summary, row, calls, s = self.feedgrab_round(STRAY)
        got = row.get('body') or ''
        self.assertEqual([], calls.urls, '别人的参考链接不许触发一次长文询问')
        self.assertIn(OWN_BRACKET, got, '上一行那个落单的 `[` 被当成这一行显示文字'
                                        '的开头,别人那条地址被剪断了')
        self.assertNotIn(FULL, got, '别人那篇文章的全文被当成这条推文的观点存下来了')
        self.assertEqual(1, summary['added'])
        self.assertEqual(TODAY, self.water(s['id']))


class DecoratedLabelShapeTests(unittest.TestCase):
    """两个消费方逐形态直测 —— 报告给了六种形态和五个变量对照,而端到端一轮只摆得
    下一种。

    外加两条**契约**,各守着这条修法的一头:
    ①左边那一半的扫描**不受 URL 字符集约束**(空格、中文、emoji 后面那个还开着的
      `[` 照样算显示文字的开头);
    ②它**不跨行** —— 上一行那个落单的 `[` 不是这一行显示文字的开头。
    """

    def hits(self, body):
        return bool(_first(_ARTICLE_LINK, body))

    def test_every_decorated_label_still_finds_the_article(self):
        """报告那六种形态都要认得出、摘得掉,而参考地址一个字不许动。"""
        for name, label, ref, url in PROSE:
            body = decorated(label, url, ref)
            with self.subTest(case=name, body=body):
                self.assertTrue(needs_article({'url': TWEET, 'body': body}),
                                '显示文字里带说明,旁边那条文章地址漏认了')
                lead = lead_text(body)
                self.assertNotIn(url, lead, '真的那条地址照旧要摘掉')
                self.assertIn(ref, lead, '前面那条参考地址被一起删掉了')

    def test_the_one_variable_controls_still_pass(self):
        """报告那五个**本来就对的**变量对照 —— 它们让这套用例分得出"左边那一半的
        范围写窄了"和"整条判据都没了"。

        第二条(`Visit:www.example.org`)是这个根因的判决性对照:全 ASCII、没有
        空格,`[` 留在 run 里面,所以上一轮版本对它也是对的。
        """
        cases = (
            ('bare_url_label', decorated('www.example.org')),
            ('ascii_prefix_without_space', decorated('Visit:www.example.org')),
            ('non_url_label', decorated('Visit')),
            ('spaced_links', f'[Visit www.example.org]({REF}) [article]({CANON})'),
            ('plain_target', decorated('Visit www.example.org',
                                       ref='https://example.org/')),
        )
        for name, body in cases:
            with self.subTest(case=name, body=body):
                self.assertTrue(needs_article({'url': TWEET, 'body': body}))
                lead = lead_text(body)
                self.assertNotIn(CANON, lead)

    def test_a_decorated_label_does_not_unhide_a_nested_address(self):
        """**反过来钉一边**:显示文字里带说明,不代表它后面那条链接目标里的地址就
        成了自己的 —— 嵌在目标查询值 / 片段 / 路径段里的那条照旧判成嵌套。

        这是放宽左边那一半的范围之后新的风险面:认出显示文字、跳过那个起手式,
        后面还有一条真的外层 URL 等着被问。
        """
        cases = (
            '[Visit www.example.org](https://example.org/?u=%s)' % OTHER,
            '[阅读www.example.org](https://example.org/p#%s)' % OTHER,
            '[🔗www.example.org](https://example.org/go/%s)' % OTHER,
            '[Visit www.example.org](https://example.org/share?text=Read+%s)' % OTHER,
            # 带说明的显示文字 + 一条已经闭合的链接 + 还开着的裸 URL:容器是最后那条。
            f'[Visit www.example.org]({REF})https://evil.example/?u={OTHER}',
        )
        for body in cases:
            with self.subTest(body=body):
                self.assertFalse(needs_article({'url': TWEET, 'body': body}),
                                 '别人链接里那条地址不是这条推文的长文')
                self.assertEqual(body, lead_text(body).strip(),
                                 '别人那条地址被从中间剪断了')

    def test_the_left_evidence_is_not_bounded_by_url_characters(self):
        """**契约①**:左边那一半问的是 markdown 结构,`_URL_RUN` 管不着它。

        `head` 里出现空格、汉字、emoji 之后那个 `[` 照样是显示文字的开头 ——
        第 6 轮按 run 的左边界取 `head`,这几种一律拿到空串、当场答反。
        """
        tail = 'example.org](https://example.org/?q=foo)[article]('
        for head in ('[Visit ', '[阅读', '[🔗', '[Visit https://e.org/ ',
                     '[一句话 说明 '):
            with self.subTest(head=head):
                self.assertTrue(_text_closed(head, tail),
                                'URL 字符集外面的字不该把显示文字的开头挡掉')
        # 反过来:这几种照旧不算 —— 左边压根没有 `[`,或者那个 `[` 已经配对。
        for head in ('', '参考:<', '见[1] 参考 ', '[share](',
                     '[a](https://e.org/)'):
            with self.subTest(head=head):
                self.assertFalse(_text_closed(head, tail),
                                 '左边没有还开着的 `[`,这不是一段显示文字')

    def test_the_left_evidence_stops_at_the_line_start(self):
        """**契约②**:那段范围是**本行**,不是整篇正文。

        取整篇的话正文里**任何**一个落单的 `[` 都让这道闸成立,而第 6 轮那一档的
        全部特征就是"`head` 为空" —— 它当场重开。两个消费方各验一遍。
        """
        self.assertFalse(needs_article({'url': TWEET, 'body': STRAY}),
                         '上一行那个落单的 `[` 不是这一行显示文字的开头')
        self.assertEqual(STRAY, lead_text(STRAY).strip(),
                         '别人那条地址被剪断了')
        # 同一行里真有一个还开着的 `[` 时照旧算 —— 不然这一条和"整条判据没了"
        # 分不开。而这一档**和 CommonMark 一致**:同一份输入在 `markdown-it` 的
        # `gfm-like` 方言里渲染成**一条**链接,text 是
        # `注 分享参考 https://example.org/share?text=read`、目标就是那条 X 地址
        # —— 它是一个链接**目标**、不嵌在任何别的 URL 里,照 `[article](<地址>)`
        # 同一条口径摘掉它是语法正确的那一边。
        same = f'见 [注 分享参考 {OWN_BRACKET}'
        self.assertNotEqual(same, lead_text(same).strip(),
                            '同一行里那个落单的 `[` 确实是显示文字的开头')

    def test_a_label_wrapped_across_a_soft_break_is_the_current_trade_off(self):
        """**这道缝钉成当前取舍,不是"它对"。**

        CommonMark 允许 link text 跨软换行,所以
        `见 [注\n说明 www.example.org](<参考地址>)[article](<真地址>)` 在
        `markdown-it` 的 `gfm-like` 方言里渲染出**两条**独立链接,第一条的 text
        逐字是 `注\n说明 www.example.org`(2026-10-10 实测)—— 按本行取范围时我们
        判成嵌套,自己的长文在这一档漏抓,也就是本轮那一档重现。

        不往 block(空行)放宽的理由写在 `_text_closed` 的 docstring 里:段内落单的
        `[` 之后 CommonMark 的结论和这条判据未必一致,于是第 6 轮那一档会从
        "head 为空"扩散成"段内有没有落单的 `[`"。两头都是永久损坏,线划在"一条链接
        写在一行内"上 —— 那是 markdown 产物与推文正文的事实。

        **它和上面那条契约是同一个取舍的两头,改方向时要一起改。**
        """
        self.assertFalse(needs_article({'url': TWEET, 'body': WRAPPED}),
                         '取舍变了:跨行的显示文字现在认得出来了,'
                         '那就把 `_text_closed` 的范围口径和这两条用例一起改')
        self.assertEqual(WRAPPED, lead_text(WRAPPED).strip())


if __name__ == '__main__':
    unittest.main()
