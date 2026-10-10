# -*- coding: utf-8 -*-
"""长文地址的起点:**字面**的方括号不是链接显示文字的开头 —— 转义的和代码里的那个
`[`,markdown 压根不拿它开启一段 link text。

**这一族管第 7 轮那次修法自己开出来的缺口**(2026-10-10 自由工作流第 8 轮审查)。
第 5 轮给**显示文字**那一半配了收尾判据(`_text_closed`),第 6 轮补上左边那一半
(「`head` 里要有一个还开着的 `[`」),第 7 轮把那一半的扫描范围从 run 的左边界扩到
**本行** —— 范围对了,可"范围里哪些 `[` 算 markdown 结构字符"一直没问:

```text
我的长文 https://x.com/Example/article/10001
分享参考 \\[<https://example.org/share?text=read](https://x.com/Other/article/20002)>]
```

`\\[` 是**字面**的方括号,尖括号包住的是一整条 `example.org` 参考地址、内部那条 X
地址是它的查询内容。可新范围一路扫过 `<` 取到了那个转义的 `[`,`_text_closed` 只数
括号、不判转义,于是宣布"外层地址已经结束",`_drop` 把内部候选摘掉:

```text
分享参考 \\[<https://example.org/share?text=read]( )>]
```

`added=1`、`errors=[]`、`partial=[]`、水位照常推进、自己的长文照旧补到了 —— 一行
提示都没有,而 `put_items` 是 `ON CONFLICT(key) DO NOTHING`:把 URL 里那个 `]` 换成
等价的 `%5D` 再同步,正确的参考地址和全文都已经传到真实 `put_items`,`added` 仍是
**0**,库里和导出的 md 里逐字还是第一次那份坏正文。**重复同步补不回来。**

**分界线是"那个 `[` 到底开不开启 link text",这件事审查者用真 `marked` /
`DOMPurify` 核过**:上面六种输入(转义、转义+中文说明、行内代码 `` `[` ``、片段
容器、旧式文章路径、t.co)**各渲染成恰好一条完整的外部链接**,没有独立的 X 或 t.co
链接,`href` 经 `unquote` 之后逐字等于输入那条完整地址。而**未转义**的
`[<…read](<X 地址>)>]` 被 `marked` 解释成**一条独立的 X 链接** —— 它真的是链接起手,
摘掉它是对的,所以审查者把它排除在问题之外,本文件也反过来钉着这一边。

修法:左边那一半扫描时跳过**不是结构字符**的 `[` / `]` —— 被奇数个反斜杠转义的,
或者落在一段行内代码跨度里的(`_inert` + `_code_spans`,判据逐字取自 CommonMark)。
**右半边(`tail`)不跟着改,而且那不是漏掉的**:它整段落在 `run` 里,而 `_URL_RUN`
是 RFC 3986 的字符表、**既没有反斜杠也没有反引号** —— `tail` 里的 `[` / `]` 在结构
上不可能被转义或在代码里。本文件有一条契约钉住这个不变量。

**判据一律是后果**(库里那一行到底是什么、导出的 md 里那条参考地址全不全),不是
"有没有发过那次询问":只验后者的话,一个把地址剪断了、却恰好没多问一次的实现也能
全绿;而剪断之后那一篇永久改不回来。本文件两头各钉一条 —— 字面方括号旁边那条参考
地址一个字不许动(族一、族二的矩阵),而**真的**链接起手照旧要认(族二那两条反向钉
和第 5~7 轮那几族的回归)。

**另拆一个文件**是因为 `tests/test_x_article_label_bounds.py` 已经 350 行、
`test_x_article_label.py` 490 行,再加这一族越过仓库那条 700 行上限;而形状本来就
不同 —— 那两个文件问的是"显示文字的范围到哪儿",这一族问的是"范围里那个 `[` 算不
算 markdown 结构"。摆盘(`FeedgrabRound`、`ArticlePathFormTests` 那两个 round
helper)仍共用 `test_x_article_gap.py` 那一份,各写一份的话两边的前提会各漂各的。

跑:  python -m unittest tests.test_x_article_label_literal -v
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import sync
from kb.feeds.x_article import lead_text, needs_article
from kb.feeds.x_article_links import _ARTICLE_LINK, _first, _text_closed, _URL_RUN
from kb.feeds.x_article_nest import _code_spans, _inert
from tests.test_x_article import OLD_WATER
from tests.test_x_article_gap import FULL, answering, article_payload
# 摆盘那两个 round helper 借 `ArticlePathFormTests` 的,但**不许把类名 import
# 进来**:它自己带 `test_*`,而 unittest 按 `dir(module)` 找 TestCase 子类 ——
# 名字落进本模块命名空间就等于让它那几条在这里再跑一遍。走模块对象取属性。
from tests import test_x_article_gap as _gap
from tests.test_x_feedgrab_contract import TODAY, FeedgrabRound

TWEET = 'https://x.com/Example/status/10001'
# 这条推文**自己**的两种文章地址形式。
CANON = 'https://x.com/Example/article/10001'
LEGACY = 'https://x.com/i/article/2108252660529049601'
# **别人**那篇长文,嵌在参考地址的查询值/片段里 —— 一个字都不许动。
OTHER = 'https://x.com/Other/article/20002'
# 浏览器通道那条推文的规范文章地址(= `article_page('https://x.com/example/status/9')`)。
CANON_9 = 'https://x.com/example/article/9'
TCO = 'https://t.co/AbCdEf'
# 报告那条外层参考地址:查询值里装着一段 markdown,所以 `tail` 里有落单的 `](`
# —— 右半边那道判据对它成立,胜负全看左边那个字面 `[` 认不认得出来。
SHARE = 'https://example.org/share?text=read'
# 报告那六种输入,逐字。共同点:`[` 和外层起手式之间只隔着一个 `<` 或空格,而那个
# `[` 是**字面**的 —— 转义的,或者在一段行内代码里。
LITERAL = (
    ('escaped_query', f'\\[<{SHARE}]({OTHER})>]', OTHER),
    ('escaped_caption', f'\\[参考 <{SHARE}]({OTHER})>]', OTHER),
    ('code_bracket', f'`[` <{SHARE}]({OTHER})>]', OTHER),
    ('escaped_fragment', f'\\[<https://example.org/a](b)#{OTHER}>]', OTHER),
    ('escaped_legacy', f'\\[<{SHARE}]({LEGACY})>]', LEGACY),
    ('escaped_tco', f'\\[<{SHARE}]({TCO})>]', TCO),
)
# 审查者那四个**单变量对照**,它们本来就对 —— 留着是为了让这套用例分得出"转义/代码
# 那一道失效了"和"整条左边判据都没了"。
CONTROLS = (
    ('no_outer_bracket', f'分享参考 <{SHARE}]({OTHER})>'),
    ('closed_label', f'分享参考 [参考] <{SHARE}]({OTHER})>]'),
    ('percent_encoded', f'分享参考 \\[<{SHARE}%5D({OTHER})>]'),
    ('no_bracket_paren', f'分享参考 \\[<https://example.org/share?text=read#{OTHER}>]'),
)
# **反过来钉的那一边**:未转义的 `[` 真的开启一段 link text,`marked` 把它解释成
# 一条独立的 X 链接(`semantic-probe.json`),所以那条地址该摘。
RAW = f'分享参考 [<{SHARE}]({OTHER})>]'


def paired(reference, url=CANON):
    """报告那份最小完整输入:自己的长文一行,带字面方括号的参考地址一行。"""
    return f'我的长文 {url}\n分享参考 {reference}'


class LiteralBracketTests(FeedgrabRound):
    """**字面方括号旁边那条完整的参考地址,一个字都不许动。**

    这一族真跑采集,判据是末态:库里那一行(**以及 `sync.export` 另写一遍的那篇
    md**)里参考地址逐字还在,而自己那条长文照旧补到了。报告第 4 步说的是重复同步
    不能自愈 —— 反面就是第一轮直接存对,所以这里每一条都先断言后果。
    """

    feedgrab_round = _gap.ArticlePathFormTests.feedgrab_round
    browser_round = _gap.ArticlePathFormTests.browser_round

    def check_feedgrab(self, reference, nested, url=CANON):
        summary, row, calls, s = self.feedgrab_round(paired(reference, url))
        got = row.get('body') or ''
        self.assertIn(reference, got, '那个字面方括号被当成显示文字的开头,'
                                      '别人那条参考地址被从中间剪断了')
        self.assertIn(nested, got, '参考地址里那条嵌套地址一个字都不许动')
        self.assertIn(FULL, got, '自己那篇长文照旧要补到')
        self.assertNotIn(f'\n我的长文 {url}', got, '自己那条地址照旧要摘掉')
        self.assertEqual('X书签保存Skill x Teamily', row.get('title'),
                         '标题回落成「某某 的动态」= 压根没去补过')
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(1, len(calls.urls), '自己那篇长文只该问一次')

        sync.export('social')
        mds = list(sync.VAULT_ROOT.rglob('*.md'))
        self.assertEqual(1, len(mds), mds)
        md = mds[0].read_text(encoding='utf-8')
        self.assertIn(reference, md, '导出的 md 里那条参考地址也被剪断了')
        self.assertIn(FULL, md)
        return s

    def test_the_report_input_reaches_the_knowledge_base(self):
        """报告那份最小完整输入逐字:转义的 `[` + 查询值里的 `](`。"""
        self.check_feedgrab(f'\\[<{SHARE}]({OTHER})>]', OTHER)

    def test_an_escaped_bracket_with_a_caption_too(self):
        """转义的 `[` 后面还写了一句中文说明。"""
        self.check_feedgrab(f'\\[参考 <{SHARE}]({OTHER})>]', OTHER)

    def test_a_bracket_inside_inline_code_too(self):
        """行内代码里那个 `[` —— CommonMark 把 code span 排在 link 之前,所以它
        压根不开启显示文字。这一条是转义那几条之外的**独立射程**:它走的是
        `_code_spans`,反斜杠那一道对它完全是瞎的。"""
        self.check_feedgrab(f'`[` <{SHARE}]({OTHER})>]', OTHER)

    def test_a_fragment_container_too(self):
        """嵌套位置换成片段(`#`)—— 那一档靠 `_SEPARATORS` 那条证据,和查询值那条
        不是同一半,所以单独走一轮。"""
        self.check_feedgrab(f'\\[<https://example.org/a](b)#{OTHER}>]', OTHER)

    def test_the_legacy_article_path_too(self):
        """嵌套的是别人的 `/i/article/<id>` —— 报告说两种路径逐字复现同一根因。"""
        self.check_feedgrab(f'\\[<{SHARE}]({LEGACY})>]', LEGACY)

    def test_a_tco_link_in_the_reference_too(self):
        """嵌套的是 t.co 短链:候选是 t.co 自己,`_ARTICLE_LINK` 压根不认它 ——
        这一条只有 `_drop(_TCO, …)` 那一路会碰,是另一个独立射程。"""
        self.check_feedgrab(f'\\[<{SHARE}]({TCO})>]', TCO)

    def test_the_browser_channel_too(self):
        """另一条保留通道,同样是**没有卡片信号**那一档(有卡片时判据② 会把自己
        那篇救回来,参考地址被剪断就看不出是哪一道失效了)。"""
        body = paired(f'\\[<{SHARE}]({OTHER})>]', CANON_9)
        got, calls = self.browser_round(body)
        text = got['entries'][0]['body']
        self.assertIn(f'\\[<{SHARE}]({OTHER})>]', text,
                      '别人那条参考地址被剪断了')
        self.assertIn(FULL, text, '自己那篇长文照旧要补到')
        self.assertNotIn(f'\n我的长文 {CANON_9}', text)
        self.assertEqual('', got['incomplete'])
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_a_reference_alone_asks_nothing(self):
        """**承重的反向钉**:推文里**只有**那条参考链接时,一次长文询问都不许发、
        那条地址一个字不动、而且**别人那篇文章的全文不许写进来**。

        最后半句最重:误判不只是改坏别人的链接,还会把别人那篇的全文当成这条推文
        的观点存进知识库(报告也单列了这一条后果,只是不另计一个问题)。
        """
        reference = f'分享参考 \\[<{SHARE}]({OTHER})>]'
        summary, row, calls, s = self.feedgrab_round(reference)
        got = row.get('body') or ''
        self.assertEqual([], calls.urls, '别人的参考链接不许触发一次长文询问')
        self.assertEqual(reference, got.strip(), '别人那条地址被剪断了')
        self.assertNotIn(FULL, got, '别人那篇文章的全文被当成这条推文的观点存下来了')
        self.assertEqual(1, summary['added'])
        self.assertEqual(TODAY, self.water(s['id']))

    def test_a_second_sync_cannot_undo_a_truncated_reference(self):
        """报告第 4 步说的是 `DO NOTHING` 让这一篇永久固定:把 URL 里那个 `]` 换成
        `%5D` 再同步,正确的正文已经交给真实 `put_items`,`added` 仍是 0。

        反面:第一轮就存对了,第二轮 `added=0` 而库里那一行逐字不变。
        """
        reference = f'\\[<{SHARE}]({OTHER})>]'
        s = self.check_feedgrab(reference, OTHER)
        first = self.rows(s['id'])['items'][0].get('body') or ''

        req, calls = answering(article_payload())
        with req:
            second = self.collect()
        rows = self.rows(s['id'])['items']
        self.assertEqual([], second['errors'])
        self.assertEqual(0, second['added'])
        self.assertEqual(1, len(rows))
        self.assertEqual(first, rows[0].get('body') or '', '第二轮把库里那一行改坏了')
        self.assertIn(reference, rows[0].get('body') or '')


class LiteralBracketShapeTests(unittest.TestCase):
    """两个消费方逐形态直测 —— 报告给了六种损坏输入、四个单变量对照和一个被证伪的
    候选,而端到端一轮只摆得下一种。

    外加三条**契约**,各守着这条修法的一头:
    ①`_code_spans` 认的是 CommonMark 那两条规则(等长闭合串、没有闭合串就是字面);
    ②`_inert` 只把**奇数个**反斜杠算成转义;
    ③右半边(`tail`)不问这一道 —— 它整段是 URL 字符,结构上不可能有反斜杠或反引号。
    """

    def hits(self, body):
        return bool(_first(_ARTICLE_LINK, body))

    def test_every_literal_bracket_keeps_the_reference_whole(self):
        """报告那六种损坏输入:那条参考地址要一个字不动,而且不许为它发询问。"""
        for name, reference, nested in LITERAL:
            body = f'分享参考 {reference}'
            with self.subTest(case=name, body=body):
                self.assertFalse(self.hits(body),
                                 '字面方括号旁边那条嵌套地址不是这条推文的长文')
                self.assertEqual(body, lead_text(body).strip(),
                                 '别人那条参考地址被从中间剪断了')
                self.assertIn(nested, lead_text(body))

    def test_the_article_next_to_a_literal_bracket_is_still_found(self):
        """同一批输入配上自己那条长文:**两件事要同时成立** —— 自己那条认得出、
        摘得掉,而参考地址逐字保留。只钉前半句的话,一个把两条都摘掉的实现也能过。
        """
        for name, reference, nested in LITERAL:
            for url in (CANON, LEGACY):
                body = paired(reference, url)
                with self.subTest(case=name, url=url):
                    self.assertTrue(needs_article({'url': TWEET, 'body': body}),
                                    '自己那条文章地址漏认了')
                    lead = lead_text(body)
                    self.assertNotIn(f'\n我的长文 {url}', lead,
                                     '自己那条地址照旧要摘掉')
                    self.assertIn(reference, lead, '参考地址被一起删掉了')

    def test_the_one_variable_controls_still_pass(self):
        """报告那四个**本来就对的**变量对照:去掉外面那个方括号、改成已经闭合的
        `[参考]` 说明、把 URL 里的 `]` 百分号编码、移除 URL 里的 `](`。

        它们让这套用例分得出"转义/代码那一道失效了"和"整条左边判据都没了"。
        """
        for name, body in CONTROLS:
            with self.subTest(case=name, body=body):
                self.assertFalse(self.hits(body))
                self.assertEqual(body, lead_text(body).strip(),
                                 '这一档本来就对,现在被剪断了')

    def test_an_unescaped_bracket_really_opens_a_label(self):
        """**反过来钉一边**:未转义的 `[` 真的开启一段 link text —— 真 `marked` 把
        同一份输入解释成**一条独立的 X 链接**(`semantic-probe.json`),所以那条地址
        该摘。审查者据此把它排除在问题之外,这里钉住我们没有顺手把它一起放行。

        少了这一条,一个"左边那一半整个不要了"的实现也能全绿。
        """
        self.assertTrue(self.hits(RAW), '未转义的 `[` 后面那条地址是一个链接目标')
        self.assertNotIn(OTHER, lead_text(RAW), '它该摘掉')
        self.assertIn('分享参考', lead_text(RAW), '作者写的那句话不许跟着走')

    def test_a_bare_tco_link_is_still_dropped(self):
        """t.co 那一侧的**阴性对照**:裸写的同一条短链照旧摘得掉 —— 不然"判据生效
        了"和"`_TCO` 那一路压根不摘"长得一模一样。"""
        bare = lead_text(f'看这个 {TCO}')
        self.assertNotIn(TCO, bare)
        self.assertIn('看这个', bare)

    def test_a_literal_bracket_does_not_unhide_a_nested_address(self):
        """**反过来钉一边**:认出"那个 `[` 是字面的"之后,后面那条**真的**外层 URL
        照旧要问 —— 嵌在它查询值 / 片段 / 路径段里的地址仍然判成嵌套。
        """
        cases = (
            f'\\[注] https://example.org/?u={OTHER}',
            f'`[` https://example.org/p#{OTHER}',
            f'\\[注] https://example.org/go/{OTHER}',
            f'\\[注] <https://example.org/share?text=Read+{OTHER}>',
        )
        for body in cases:
            with self.subTest(body=body):
                self.assertFalse(self.hits(body),
                                 '别人链接里那条地址不是这条推文的长文')
                self.assertEqual(body, lead_text(body).strip(),
                                 '别人那条地址被从中间剪断了')

    def test_the_earlier_label_families_do_not_regress(self):
        """**反过来钉一边**:第 5~7 轮那三族 —— 显示文字**整个**是一个网址、
        显示文字里带一句说明 —— 照旧要认出自己那条长文。

        跳过字面方括号的那一步不许顺手把真的显示文字也跳过去。
        """
        ref = 'https://example.org/?q=foo'
        cases = (
            ('bare_url_label', f'[www.example.org]({ref})[article]({CANON})'),
            ('decorated_label', f'[Visit www.example.org]({ref})[article]({CANON})'),
            ('chinese_label', f'[阅读www.example.org]({ref})[article]({CANON})'),
        )
        for name, body in cases:
            with self.subTest(case=name, body=body):
                self.assertTrue(needs_article({'url': TWEET, 'body': body}),
                                '显示文字那一族回退了')
                self.assertNotIn(CANON, lead_text(body))
                self.assertIn(ref, lead_text(body))

    def test_code_spans_follow_the_two_commonmark_rules(self):
        """**契约①**:`_code_spans` 认的是规范那两条 —— 一串 N 个反引号要由**恰好
        N 个**的串闭合;找不到那样的串时它就是**字面文本**、不构成跨度。

        第二条尤其要钉:把未闭合的反引号也当成"跨度开到行尾"的话,它后面那个真的
        `[` 会被当成字面字符,显示文字那一族当场回退。
        """
        tail = 'example.org](https://example.org/?q=foo)['
        self.assertEqual(((0, 3),), _code_spans('`[`'))
        self.assertEqual(((0, 6),), _code_spans('`a``b`'), '等长的那个串才闭合')
        self.assertEqual((), _code_spans('`[ 没有闭合'), '没有闭合串 = 字面文本')
        self.assertEqual((), _code_spans('\\`[`'), '转义过的反引号不开启跨度')
        self.assertEqual(((0, 3), (6, 9)), _code_spans('`a` 和 `b`'), '一行可以有几段')
        # 后果那一头:未闭合的反引号之后那个 `[` 照旧是显示文字的开头。
        self.assertTrue(_text_closed('`[ 没闭合 [www.', tail))
        self.assertFalse(_text_closed('`[` <', tail), '跨度里那个 `[` 是字面的')

    def test_only_an_odd_run_of_backslashes_escapes(self):
        """**契约②**:`\\[` 是字面左括号,`\\\\[` 是字面反斜杠加一个**真的**起手。

        数错奇偶的两个方向都坏:当成偶数 → 本轮那六种重开;当成奇数 → 一条正常的
        `\\\\[显示文字](…)` 漏抓自己的长文。
        """
        for prefix, inert in (('', False), ('\\', True), ('\\\\', False),
                              ('\\\\\\', True), ('\\\\\\\\', False)):
            line = prefix + '['
            with self.subTest(prefix=repr(prefix)):
                self.assertEqual(inert, _inert(line, len(line) - 1, ()))
        # 跨度里的那个一律算字面,反斜杠数多少都不影响。
        self.assertTrue(_inert('`[`', 1, ((0, 3),)))

    def test_a_line_full_of_code_spans_stays_linear(self):
        """跨度查询是**二分**,不是逐段扫 —— 后者是
        `O(方括号数 × 跨度数)`,而这个判定被「每个候选 × 每个起手式 × 本行每个
        方括号」各调一次。

        实测一行 20000 字符的 `` `[` ``:逐段扫 0.93 秒(10000 字符 0.23 秒,正好
        四倍 —— 二次),二分 0.007 秒。那不是真实写法,但推文正文对 premium 用户
        能写到两万字,口径同第 6 轮把往左扫描从 `re.search` 换成逐字走那次。
        余量给了近两个数量级,所以这不是一条看机器快慢的 flaky。
        """
        # 候选的 run 里要有一个起手式,否则 `_text_closed` 压根不被问到 ——
        # 那样这条用例对跨度查询的写法完全不敏感。
        body = '`[` ' * 6666 + f'www.example.org](' + CANON
        start = time.perf_counter()
        hit = needs_article({'url': TWEET, 'body': body})
        spent = time.perf_counter() - start
        self.assertTrue(hit, '这一行里确实有一条自己的文章地址')
        self.assertLess(spent, 0.2, '跨度查询退回逐段扫了:%.4f 秒' % spent)

    def test_the_right_half_cannot_carry_an_escape(self):
        """**契约③**:右半边(`tail`)不问转义/代码,而那不是漏掉的 —— 它整段落在
        `run` 里,而 `_URL_RUN` 是 RFC 3986 的字符表,**反斜杠和反引号都不在里面**。

        这一条钉住那个不变量本身:哪天有人往 `_URL_RUN` 里加了这两个字符,`tail`
        里就可能出现一个字面的 `](`,右半边那道判据跟着失准 —— 那时这条会红,而不是
        等某次同步把别人的地址剪断。
        """
        self.assertNotIn('\\', _URL_RUN)
        self.assertNotIn('`', _URL_RUN)
        for ch in '[]':
            self.assertIn(ch, _URL_RUN, 'gen-delims 那两个方括号本来就要认')


if __name__ == '__main__':
    unittest.main()
