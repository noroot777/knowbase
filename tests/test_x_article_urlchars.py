# -*- coding: utf-8 -*-
"""长文地址的起点:往左扫描时**外层 URL 内部的字符一个都不许少认**。

**这两族是同一道判据、同一种永久损坏的两个部件**(`test_x_article_host.py` 文件头
那张索引的③④)。`_nested` 回答的是"紧贴这个 `?`/`#` 左边那一段是不是一条 URL",
办法是从候选往左吃一段 `_URL_RUN` 字符再找起手式 —— 所以**那个集合少认一个合法
URL 字符,外层起点就追不回来**:`_nested` 误答"不是嵌套",于是别人家片段/查询里那条
X 文章地址被当成这条推文自己的,`_drop` 把它删掉、别人那条参考地址从中间被剪断,而
`put_items` 的 `DO NOTHING` 让这种删除**永久固定**(同一道缺口还让判据① 把这些推文
一律误判成"要去问一次长文",每条白买一次调用)。

两族缺的字符各在一个部件上,末态逐字相同:
- `OuterPathCharTests` —— **路径字符**(sub-delims `!$&'()*+,;=`)。原来这个集合
  复用②档的禁止集再补一个 `:`,于是八个合法路径字符一个都不认(2026-10-09 第 5 轮
  审查)。
- `Ipv6OuterHostTests` —— **authority 里的 IPv6 字面量**(gen-delims 的 `[` `]`)。
  那道扫描一路跨过路径、authority 乃至 scheme,所以补完路径字符还不够
  (2026-10-10 第 6 轮审查)。

**这边钉的是后果**(真跑采集和导出,看库里那条外部地址还在不在),而**那个字符集自己
的契约在 `tests/test_x_article_urlrun.py`** —— "见一个样本补一个"已经栽过两次,所以
除了按形态摆样本,还要有一条闸门拦住"又把这张表改小了"。

**两头都要钉**:只验"外部地址还在"的话,一个压根不摘地址的实现也能全绿;而把这个
集合加宽**加过头**(把汉字收进来)会反过来让句读紧贴的真地址被判成嵌套 —— 那是漏认、
空正文永久固定,所以反钉那两条用例一条都不许少。非 ASCII 外层路径那道已知的缝也
钉成当前取舍,改方向时要和反钉那条一起改。

**另拆一个文件**是因为 `test_x_article_host.py` 的族索引加到第八族时已经越过仓库那条
700 行上限,而这两族的形状本来就自成一块(同一个字符集的两个部件,摆盘和判据共用
一份 `check_feedgrab`)。摆盘(`FeedgrabRound`、`ArticlePathFormTests` 那两个 round
helper)仍共用 `test_x_article_gap.py` 那一份,各写一份的话两边的前提会各漂各的。

跑:  python -m unittest tests.test_x_article_urlchars -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import sync, x_article
from kb.feeds.x_article import lead_text, needs_article
from tests.test_x_article import OLD_WATER
from tests.test_x_article_gap import FULL
# 摆盘那两个 round helper 借 `ArticlePathFormTests` 的,但**不许把类名 import
# 进来**:它自己带 `test_*`,而 unittest 按 `dir(module)` 找 TestCase 子类 ——
# 名字落进本模块命名空间就等于让它那几条在这里再跑一遍。走模块对象取属性。
from tests import test_x_article_gap as _gap
from tests.test_x_feedgrab_contract import TODAY, FeedgrabRound

# 推文自己的地址,`needs_article` 要靠它才会往下判。
TWEET = 'https://x.com/Example/status/10001'
# 这条推文的规范文章地址(= `article_page(TWEET)`),是真要摘掉的那一条。
CANON = 'https://x.com/Example/article/10001'
# **别人**那篇长文的规范地址:这两族摆的全是"它嵌在外层 URL 的片段/查询里"。
OTHER = 'https://x.com/Other/article/20002'


class OuterPathCharTests(FeedgrabRound):
    """第 5 轮审查那条:外层 URL 的**合法路径字符**不许让 `_nested` 失效。

    `_nested` 回答的是"紧贴这个 `?`/`#` 左边那一段是不是一条 URL",办法是往左吃
    一段 URL 合法字符再找起手式 —— 所以**那个字符集少认一个合法字符,外层起点就
    追不回来**:`_nested` 误答"不是嵌套",于是别人家片段/查询里那条 X 文章地址被
    当成这条推文自己的地址,`_drop` 把它删掉,别人那条参考地址从中间被剪断。
    `put_items` 的 `DO NOTHING` 让这种删除**永久固定**,重复同步也补不回来。

    原来这个集合复用②档的禁止集 `_URL_CHARS` 再补一个 `:`,于是 RFC 3986 里
    `(` `)` `;` `$` `\'` `,` `!` `*` 这八个合法路径字符一个都不认(2026-10-09 实测:
    `…/docs(v2)#<别人的文章地址>` 在库里和导出的 md 里都只剩 `…/docs(v2)#`,而
    `added=1`、`errors=[]`、`partial=[]`、水位照常推进、一行提示都没有)。

    **两头都要钉**:只验"外部地址还在"的话,一个压根不摘地址的实现也能全绿;而
    把这个集合加宽**加过头**(比如把汉字也收进来)会反过来让句读紧贴的真地址被判
    成嵌套 —— 那是漏认、空正文永久固定,所以反钉那条用例一条都不许少。
    """

    LEAD = '我写了一篇新文章：'
    feedgrab_round = _gap.ArticlePathFormTests.feedgrab_round
    browser_round = _gap.ArticlePathFormTests.browser_round

    # 八个合法路径字符各一条,外加三条本来就判得对的作对照(普通片段、路径里的
    # `:`、百分号编码的中文路径)—— 后三条让这套用例分得出"少认了几个字符"和
    # "整道判据没了"。
    OUTER = [
        ('https://example.org/docs(v2)#' + OTHER, '括号'),
        ('https://example.org/path;v=2?' + OTHER, '分号'),
        ('https://example.org/$docs#' + OTHER, '美元号'),
        ("https://example.org/a'q'#" + OTHER, '单引号'),
        ('https://example.org/a,b?' + OTHER, '逗号'),
        ('https://example.org/a!b#' + OTHER, '叹号'),
        ('https://example.org/a*b?' + OTHER, '星号'),
        ('https://example.org/go(1)/x?' + OTHER, '括号在路径段里'),
        ('https://example.org/p#' + OTHER, '普通片段'),
        ('https://example.org/a:b?' + OTHER, '路径里的冒号'),
        ('https://example.org/%E6%96%87%E6%A1%A3#' + OTHER, '百分号编码的路径'),
    ]

    def check_feedgrab(self, ref):
        """真跑一轮 feedgrab,**连导出的那篇 md 一起核**。

        报告那份证据两处都看了:库里那一行和 vault 里那篇 —— 后者才是用户真正
        读到的东西,而它是 `sync.export` 另写一遍的,少钉一处就少一个消费方。
        """
        summary, row, calls, s = self.feedgrab_round(f'{self.LEAD}{CANON}\n参考：{ref}')
        got = row.get('body') or ''
        self.assertIn(ref, got, '别人那条参考地址被从中间剪断了')
        self.assertNotIn(CANON, got, '而真的那条照旧要摘掉')
        self.assertIn(FULL, got, '这条推文自己的全文还是要补上')
        self.assertEqual('X书签保存Skill x Teamily', row.get('title'),
                         '标题回落成「某某 的动态」= 压根没去补过')
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(1, len(calls.urls), '只问这条推文自己那一次')
        sync.export('social')
        mds = list(sync.VAULT_ROOT.rglob('*.md'))
        self.assertEqual(1, len(mds), mds)
        md = mds[0].read_text(encoding='utf-8')
        self.assertIn(ref, md, '导出的 md 里那条参考地址也被剪断了')
        self.assertIn(FULL, md)

    def test_parentheses_in_the_outer_path_keep_the_reference_intact(self):
        """审查报告第一组输入:`…/docs(v2)#<别人的文章地址>`。"""
        self.check_feedgrab('https://example.org/docs(v2)#' + OTHER)

    def test_a_semicolon_in_the_outer_path_keeps_the_reference_intact(self):
        """审查报告第二组输入:`…/path;v=2?<别人的文章地址>`。"""
        self.check_feedgrab('https://example.org/path;v=2?' + OTHER)

    def test_the_browser_channel_keeps_it_intact_too(self):
        """另一条保留通道,而且是**没有卡片信号**那一档。"""
        ref = 'https://example.org/docs(v2)#' + OTHER
        got, calls = self.browser_round(
            f'{self.LEAD}https://x.com/example/article/9\n参考：{ref}')
        text = got['entries'][0]['body']
        self.assertIn(ref, text, '别人那条参考地址被从中间剪断了')
        self.assertNotIn('/example/article/9', text, '而真的那条照旧要摘掉')
        self.assertIn(FULL, text)
        self.assertEqual('', got['incomplete'], '补到了,这一轮算读全了')
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_every_legal_path_character_still_hides_the_nested_address(self):
        """逐个形态直测两个消费方:正文一个字不许动,判据① 也不许为真。

        后半句是同一道缺口的另一半后果 —— 误认之后每条推文白买一次询问,而答复
        会是**别人那篇文章**的全文,真写进库里就是把别人的内容记成这条推文的观点。
        """
        for url, why in self.OUTER:
            with self.subTest(why=why):
                body = f'参考：{url}'
                self.assertEqual(body, lead_text(body))
                self.assertFalse(needs_article({'url': TWEET, 'body': body}))

    def test_widening_the_run_does_not_swallow_a_real_address(self):
        """**反过来钉一边**:加宽那个集合不许把句读紧贴的真地址判成嵌套。

        这一族和 `QuestionMarkLeadInTests` 是同一道判据的两个方向:那边要的是
        "句读后面的真地址认得出",这边要的是"外层 URL 内部的地址认不出"。往这个
        集合里多塞一个**正文里也会出现**的字符(汉字、空白)就会把前者一起关掉,
        而那是漏认 = 空正文永久固定。
        """
        for lead in ('Have you read this?', '读过这篇吗?', 'What#', '第三节#',
                     'Really??', 'v1.2?', 'x=1?', '完(1)?', '见[2]#', '算了!?',
                     '参考https://example.org/说明?', 'https://example.org/doc,'):
            with self.subTest(lead=lead):
                self.assertTrue(x_article._first(x_article._ARTICLE_LINK,
                                                 lead + CANON),
                                '句读紧贴的是作者自己那条地址')
                self.assertNotIn(CANON, lead_text(lead + CANON),
                                 '认出来了就得摘掉')

    def test_a_non_ascii_outer_path_is_a_known_gap(self):
        """**已知留着的一道缝**,如实钉成当前取舍:非 ASCII 的外层路径。

        汉字加不进那个集合 —— `…/文档#<地址>`(这里)和
        `参考https://example.org/说明?<地址>`(上一条用例里那个**真**地址)
        **完全同形**:都是"往左吃到一个汉字就断"。两个方向都是永久损坏,所以这不是
        "哪个对"的问题;线划在 ASCII 上的理由是它可解释(非 ASCII 不是 URL 的原生
        形态,浏览器要先百分号编码,而编码之后的写法上一条用例里钉着)。
        **改方向时这两条用例要一起改**,只改一条就是把另一头静默打开。
        """
        body = '参考：https://example.org/文档#' + OTHER
        self.assertNotIn(OTHER, lead_text(body),
                         '这一档目前判成"不是嵌套",改了就要一起改反钉那条')


class Ipv6OuterHostTests(FeedgrabRound):
    """第 6 轮审查那条:外层 URL 的 **IPv6 主机字面量**不许让 `_nested` 失效。

    和上一族是同一道判据、同一种永久损坏,只是缺的字符在**另一个部件**上:
    `_nested` 往左的扫描一路跨过路径、authority 乃至 scheme,而上一轮只补了
    RFC 3986 的 sub-delims(路径字符),gen-delims 里的 `[` `]` 仍然不在集合里。
    于是 IPv6 主机的 `]` 把扫描截断,留下的尾部只剩 `/docs` 或 `:8443/docs` ——
    再也找不到 `https://`,`_nested` 误答"不是嵌套",`_drop` 把别人那条嵌套的 X
    文章地址删掉(2026-10-10 实测:库里和导出的 md 里都只剩
    `https://[2001:db8::1]/docs#`,而 `added=1`、`errors=[]`、`partial=[]`、水位
    照常推进、一行提示都没有;真无头浏览器读回来的 hostname 就是 `[2001:db8::1]`,
    后面那段属于 hash/search,压根不是第二条独立链接)。

    **端口那个 `:` 本来就认得**,断的是方括号 —— 第二组样本带上 `:8443` 是为了
    把"authority 整段都要跨得过去"钉死,不是因为端口另有一道缺口。

    **两头都要钉**,理由同上一族:只验"外部地址还在"的话一个压根不摘地址的实现也
    能全绿,而把集合加宽**加过头**会反过来把句读紧贴的真地址判成嵌套。
    """

    LEAD = '我写了一篇新文章：'
    feedgrab_round = _gap.ArticlePathFormTests.feedgrab_round
    browser_round = _gap.ArticlePathFormTests.browser_round
    check_feedgrab = OuterPathCharTests.check_feedgrab

    # 五种真实写法 + 两条对照(普通主机名、百分号编码),后者让这套用例分得出
    # "少认了方括号"和"整道判据没了"。
    OUTER = [
        ('https://[2001:db8::1]/docs#' + OTHER, 'IPv6 + 片段'),
        ('https://[2001:db8::1]:8443/docs?' + OTHER, 'IPv6 + 端口 + 查询'),
        ('https://[::1]/a#' + OTHER, 'IPv6 回环的短写'),
        ('https://[2001:db8::1]#' + OTHER, 'IPv6 之后直接就是片段'),
        ('https://[2001:db8::1]:8443?' + OTHER, 'IPv6 + 端口之后直接就是查询'),
        ('https://example.org/p#' + OTHER, '对照:普通主机名'),
        ('https://example.org/%E6%96%87%E6%A1%A3#' + OTHER, '对照:百分号编码'),
    ]

    def test_an_ipv6_host_keeps_the_reference_intact(self):
        """审查报告第一组输入:`https://[2001:db8::1]/docs#<别人的文章地址>`。"""
        self.check_feedgrab('https://[2001:db8::1]/docs#' + OTHER)

    def test_an_ipv6_host_with_a_port_keeps_it_intact_too(self):
        """审查报告第二组输入,顺带覆盖端口和查询那个分隔符。"""
        self.check_feedgrab('https://[2001:db8::1]:8443/docs?' + OTHER)

    def test_the_browser_channel_keeps_an_ipv6_reference_intact(self):
        """另一条保留通道,而且是**没有卡片信号**那一档。"""
        ref = 'https://[2001:db8::1]/docs#' + OTHER
        got, calls = self.browser_round(
            f'{self.LEAD}https://x.com/example/article/9\n参考：{ref}')
        text = got['entries'][0]['body']
        self.assertIn(ref, text, '别人那条参考地址被从中间剪断了')
        self.assertNotIn('/example/article/9', text, '而真的那条照旧要摘掉')
        self.assertIn(FULL, text)
        self.assertEqual('', got['incomplete'], '补到了,这一轮算读全了')
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_every_ipv6_form_still_hides_the_nested_address(self):
        """逐个形态直测两个消费方:正文一个字不许动,判据① 也不许为真。"""
        for url, why in self.OUTER:
            with self.subTest(why=why):
                body = f'参考：{url}'
                self.assertEqual(body, lead_text(body))
                self.assertFalse(needs_article({'url': TWEET, 'body': body}))

    def test_brackets_do_not_swallow_a_real_address(self):
        """**反过来钉一边**:方括号进了集合,但它不许挡住真地址。

        这几种前导里的方括号都是作者的标记(引用序号、中文方括号),往左吃到它们
        之后那一段里没有起手式 —— 所以照旧判成"不是嵌套"。**已知留着的一道缝**:
        `www.x.y[1]?<地址>` 这种"裸域名 + 方括号 + 句读 + 地址、中间一个空格都
        没有"的写法会被判成嵌套,那不是任何真实写法(同 `_SEPARATORS` 那段注释
        里记着的 `example.org?<地址>` 那道缝)。
        """
        for lead in ('见[2]#', '[1]?', '第 [3] 节?', '【注】?', '〔1〕#',
                     'see [1] again?', 'a]b?', '[?'):
            with self.subTest(lead=lead):
                self.assertTrue(x_article._first(x_article._ARTICLE_LINK,
                                                 lead + CANON),
                                '句读紧贴的是作者自己那条地址')
                self.assertNotIn(CANON, lead_text(lead + CANON),
                                 '认出来了就得摘掉')

if __name__ == '__main__':
    unittest.main()
