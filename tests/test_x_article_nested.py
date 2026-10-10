# -*- coding: utf-8 -*-
"""长文地址的起点:紧贴它的那个分隔符,是作者的标点还是外层 URL 的结构字符。

**这一族管的是「起点判据从"看一个字符"改成"看上下文"」这件事**,两条缺陷都出在
同一处(2026-10-10 自由工作流第 1 轮审查),而且方向正好相反:

- **F1 漏认** —— 定宽后视一律拒 `=` `&` `/`,于是 `全文=<文章地址>`、
  `阅读地址/<文章地址>`、`Read &<文章地址>` 里那条**真**地址被自己前面那个标点
  挡在正则层面:`_nested` 压根没有机会发问,`needs_article` 三条判据一条都不命中,
  那条链接本身当成正文入库 —— `added=1`、`errors=[]`、`partial=[]`、接口调用
  **0** 次、水位照常推进、标题回落成「某某 的动态」,而 `put_items` 是
  `ON CONFLICT(key) DO NOTHING`,**重复同步也补不回来**。`=` 在 `v1?x=1` 这类
  正文里是赋值号、`/` 在「阅读地址/订阅」里是并列号、`&` 在「A &B」里是连词。
- **F2 误认** —— `?` `#` 那一档只看紧前**一个**字符,于是分享链接的查询**值**里
  嵌着的一条完整 X 文章地址认不出来(`?text=Read+<地址>` 紧前是 `+`、
  `?text=Read%20<地址>` 紧前是 `0`、`?target=(<地址>)` 紧前是 `(`):别人那条参考
  地址被从中间永久剪断,库里和导出的 md 都只剩 `…?text=Read+`,而采集照报成功。

修法是一处:①档(自带 scheme 的候选)的起点在正则里**一个字符都不限**,全部交给
`_nested` 按上下文判 —— 三步,见它那段 docstring。**两种证据缺一不可**:只有
"紧前那一个字符占着结构位置"时 F2 三种分享查询值一条都不修;只有"外层起手式之后
出现过 `?` `#`"时 `/go/<地址>` 这种**路径段**嵌套漏掉。

**判据一律是后果**(库里那一行到底是什么、下一轮还补不补得回来),不是"有没有发过
那次询问":只验后者的话,一个把链接当正文存下来、却顺手问了一次的实现也能全绿;
而那一篇一旦以链接入库就永久改不回来。每一族都**两头钉着** —— F1 这边还要确认
别人家的地址一个字不动,F2 那边还要确认正常标点后面的真地址照旧摘得掉。

**另拆一个文件**是因为 `tests/test_x_article_host.py` 已经 689 行,再加两族越过
仓库那条 700 行上限;摆盘(`FeedgrabRound`、`ArticlePathFormTests` 那两个 round
helper)仍共用 `test_x_article_gap.py` 那一份 —— 各写一份的话两边的前提会各漂各的。
`test_x_article_host.py` 的文件头列着这道闸的五族,③就是本文件的 `SeparatorLeadInTests`。

跑:  python -m unittest tests.test_x_article_nested -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import sync, x_article
from kb.feeds.x_article import lead_text, needs_article
from tests.test_x_article import OLD_WATER
from tests.test_x_article_gap import FULL, answering, article_payload, tweet
# 摆盘那两个 round helper 借 `ArticlePathFormTests` 的,但**不许把类名 import 进来**:
# 它自己带 `test_*`,而 unittest 按 `dir(module)` 找 TestCase 子类 —— 名字落进本模块
# 命名空间就等于让它那 9 条在这里再跑一遍(全族条数虚增,口径同"少了就是没被
# discover 收进去,多了就是把别人的用例又跑了一遍")。走模块对象取属性。
from tests import test_x_article_gap as _gap
from tests.test_x_feedgrab_contract import TODAY, FeedgrabRound

# 推文自己的地址,`needs_article` 要靠它才会往下判。
TWEET = 'https://x.com/Example/status/10001'
# 这条推文的两种文章地址形式,都是真要摘掉的那一条。报告两条缺陷各用了一种,
# 所以 F1 两种都走一遍端到端 —— 只走规范形式的话"只改一半"看不出来。
CANON = 'https://x.com/Example/article/10001'
LEGACY = 'https://x.com/i/article/2108252660529049601'
# **别人**那篇长文的规范地址,用来摆"嵌在外层 URL 的查询值里"那一族。
OTHER = 'https://x.com/Other/article/20002'
# 浏览器通道那条推文的规范文章地址(= `article_page('https://x.com/example/status/9')`)。
CANON_9 = 'https://x.com/example/article/9'


# ------------------------------------------------- F1:结构分隔符紧贴的真地址

class SeparatorLeadInTests(FeedgrabRound):
    """**`=` `&` `/` 紧贴时那条地址是真的,不许被挡在正则层面。**

    这三个字符原来写在一道定宽后视里一律拒(`(?<![=&/])`),理由是"它们在正文里
    压根不是标点" —— 被真实写法推翻了:`全文=<地址>`(等号当赋值/冒号用)、
    `阅读地址/<地址>`(斜杠当并列号用)、`Read &<地址>`(与号当连词用)。

    末态和前两轮那两次漏认(`新文章...<地址>` 的省略号、`读过这篇吗?<地址>` 的
    句末问号)**逐字相同**,所以这一族的判据照它们写:两条保留通道各走一轮真采集,
    断言库里那一行是全文、那条地址摘掉了、作者写的那几个字一个都没少。
    """

    feedgrab_round = _gap.ArticlePathFormTests.feedgrab_round
    browser_round = _gap.ArticlePathFormTests.browser_round

    def check_feedgrab(self, lead, url=CANON, kept=None):
        """一轮只建一个订阅,所以每个样本各走一次完整的摆盘(`subTest` 循环会撞上
        「这个链接已经在订阅列表中」)。"""
        summary, row, calls, s = self.feedgrab_round(f'{lead}{url}')
        got = row.get('body') or ''
        self.assertIn(FULL, got, '分隔符紧贴时长文全文没补进来')
        self.assertNotIn('/article/', got, '那条地址照旧要摘掉')
        self.assertIn(kept or lead, got, '作者写的那几个字一个都不许少')
        self.assertEqual('X书签保存Skill x Teamily', row.get('title'),
                         '标题回落成「某某 的动态」= 压根没去补过')
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(1, len(calls.urls), '那一次询问根本没发出去')
        return s

    def check_browser(self, lead):
        """另一条保留通道,而且是**没有卡片信号**那一档 —— 正文里那条地址是唯一
        的信号,认不出它就是整篇漏掉。"""
        got, calls = self.browser_round(f'{lead}{CANON_9}')
        text = got['entries'][0]['body']
        self.assertIn(FULL, text, '分隔符紧贴时长文全文没补进来')
        self.assertNotIn('/article/', text)
        self.assertIn(lead, text)
        self.assertEqual('', got['incomplete'], '补到了,这一轮算读全了')
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_the_equals_sign_from_the_report_reaches_the_knowledge_base(self):
        """报告那份输入逐字:`全文=` + 规范形式。"""
        self.check_feedgrab('全文=')

    def test_the_equals_sign_with_the_legacy_form_reaches_it_too(self):
        """同一个前导 + 旧式形式 —— 报告的另一半,两种路径形式各走一次。"""
        self.check_feedgrab('全文=', url=LEGACY)

    def test_a_slash_hugging_the_link_reaches_the_knowledge_base(self):
        """`阅读地址/<地址>` —— 斜杠在中文里是常见的并列号。"""
        self.check_feedgrab('阅读地址/')

    def test_an_ampersand_hugging_the_link_reaches_the_knowledge_base(self):
        """`Read &<地址>` —— 与号当连词,后面不带空格也是常见写法。"""
        self.check_feedgrab('Read &')

    def test_a_second_sync_after_the_fix_keeps_the_full_text(self):
        """**重复同步那一半**:同一个订阅第二轮换一条推文(`全文= ` 带空格),照旧
        补得到。

        报告拿"加一个空格就正常"当对照 —— 那说明缺陷只在"紧贴"这一种写法上;修完
        之后两种写法都该走同一条路,而且第二轮不许因为第一轮的水位把它跳过去。
        """
        s = self.check_feedgrab('全文=', url=LEGACY)
        self.cli(example={'display': 'Example Person', 'tweets': [
            tweet(id_='10002', title='', body=f'全文= {LEGACY}')]})
        req, calls = answering(article_payload())
        with req:
            second = self.collect()
        rows = self.rows(s['id'])['items']
        self.assertEqual([], second['errors'])
        self.assertEqual(1, second['added'], '第二轮那条新推文没进来')
        self.assertEqual(1, len(calls.urls), '第二轮同样要去问那一次')
        fresh = next(r for r in rows if r['url'].endswith('/10002'))
        self.assertIn(FULL, fresh.get('body') or '', '带空格那条也得补到全文')
        self.assertIn('全文=', fresh.get('body') or '')

    def test_the_browser_channel_too(self):
        """浏览器通道走的是另一份入口(预取里的 `fill`),漏哪一条都是那条通道上的
        长文永久空正文。"""
        self.check_browser('全文=')

    def test_an_external_address_after_the_same_separators_is_left_alone(self):
        """**反过来钉一边**:同样这几个分隔符后面跟着**别人家**的地址时,一个字
        都不许动,而且一次询问都不发。

        放宽起点那一头不许顺带放宽这一边 —— 这两件事由 `_nested` 同一道判据分开,
        它答错的代价是把别人那条地址从中间永久剪断。
        """
        for ref in ('https://fxtwitter.com/Example/article/10001',
                    'https://notx.com/Example/article/10001',
                    'https://help.x.com/Example/article/10001',
                    'https://example.org/docs/x.com/Example/article/10001'):
            for lead in ('全文=', '阅读地址/', 'Read &'):
                body = f'{lead}{ref}'
                with self.subTest(ref=ref, lead=lead):
                    self.assertFalse(needs_article({'url': TWEET, 'body': body}),
                                      '别人家的地址不许触发一次长文询问')
                    self.assertEqual(body, lead_text(body).strip(),
                                     '别人那条地址被从中间剪断了')

    def test_a_nested_x_article_after_the_same_separators_is_left_alone(self):
        """**再钉一边**:`=` `&` `/` 放行之后,真正的嵌套入口照旧要拦住。

        这四种是放宽之后新的风险点,两种证据各管一半 —— `?u=` / `&u=` 靠"紧前那
        一个字符占着结构位置",`/go/` 同理,`#` 也是;真要靠另一半的那几种在
        `ShareQueryValueTests` 里。
        """
        for body in (f'https://evil.example/?u={OTHER}',
                     f'https://evil.example/?a=1&u={OTHER}',
                     f'https://evil.example/go/{OTHER}',
                     f'https://example.org/#{OTHER}'):
            with self.subTest(body=body):
                self.assertFalse(needs_article({'url': TWEET, 'body': body}),
                                  '嵌在别人 URL 里的那条不是这条推文的长文')
                self.assertEqual(body, lead_text(body).strip(),
                                 '别人那条地址一个字都不许动')


# ------------------------------------------------- F2:分享链接的查询值里那一条

class ShareQueryValueTests(FeedgrabRound):
    """**查询串和片段是不透明的值容器,里面写着一条完整 URL 就是嵌套。**

    原来那道判据只看紧前**一个**字符是不是 `?` `#`,于是分享链接把地址塞进查询
    **值**里的那几种全漏:`?text=Read+<地址>` 紧前是 `+`、`?text=Read%20<地址>`
    紧前是 `0`、`?target=(<地址>)` 紧前是 `(`。末态和第 5/6 轮那两次(字符集少认
    路径字符 / IPv6 方括号)逐字相同 —— 别人那条参考地址在库里和**导出的 md** 里
    都只剩前半截,`added=1`、`errors=[]`、`partial=[]`、水位照常推进,一行提示都
    没有,而 `DO NOTHING` 让这种删除**永久固定**。

    所以判据补上第二种证据:**外层起手式之后出现过 `?` 或 `#`**。它和第一种缺一
    不可,`SeparatorLeadInTests` 最后那条钉的正是第一种的射程。
    """

    feedgrab_round = _gap.ArticlePathFormTests.feedgrab_round
    LEAD = '我写了一篇新文章：'

    def check_feedgrab(self, ref):
        """三件事一起断言,**连导出的 md 一起核**:外层那条参考地址逐字还在、真的
        那条摘掉了、这条推文自己的全文补上了。

        少哪一句都能被混过去 —— 少"真地址摘掉了"的话一个压根不摘地址的实现也过,
        少"全文补上了"的话把整条判据关掉的实现也过;而 md 是用户真正读到的东西,
        它由 `sync.export` 另写一遍,少钉一处就少一个消费方。
        """
        summary, row, calls, s = self.feedgrab_round(f'{self.LEAD}{CANON}\n参考：{ref}')
        got = row.get('body') or ''
        self.assertIn(ref, got, '别人那条分享地址被从中间剪断了')
        self.assertNotIn(CANON, got, '而真的那条照旧要摘掉')
        self.assertIn(FULL, got, '这条推文自己的全文还是要补上')
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(1, len(calls.urls), '只该为自己那条地址问一次')

        sync.export('social')
        mds = list(sync.VAULT_ROOT.rglob('*.md'))
        self.assertEqual(1, len(mds), mds)
        md = mds[0].read_text(encoding='utf-8')
        self.assertIn(ref, md, '导出的 md 里那条分享地址也被剪断了')
        self.assertIn(FULL, md)

    def test_the_share_query_value_from_the_report_stays_intact(self):
        """报告那份输入逐字:`https://example.org/share?text=Read+<别人的文章地址>`。"""
        self.check_feedgrab(f'https://example.org/share?text=Read+{OTHER}')

    def test_a_percent_encoded_share_value_stays_intact(self):
        """同一种容器,分隔用 `%20` —— 紧前那个字符是 `0`,第一种证据彻底是瞎的。"""
        self.check_feedgrab(f'https://example.org/share?text=Read%20{OTHER}')

    def test_a_parenthesised_share_value_stays_intact(self):
        """紧前那个字符是 `(` —— 同上,只有第二种证据救得了它。"""
        self.check_feedgrab(f'https://example.org/share?target=({OTHER})')

    def test_a_share_link_alone_is_never_asked_about(self):
        """**整条推文只有一条分享链接时**:一次询问都不发,正文一个字不动,而且
        **别人那篇文章的全文不许写进来**。

        最后那半句是这一族最重的一条 —— 误认不只是改坏别人的链接,还会把**别人
        那篇文章的全文**当成这条推文的观点存进知识库,而 `DO NOTHING` 让它永久
        固定。
        """
        ref = f'https://example.org/share?text=Read+{OTHER}'
        summary, row, calls, s = self.feedgrab_round(f'读到一篇好文章 {ref}')
        got = row.get('body') or ''
        self.assertEqual([], calls.urls, '别人的分享链接不许触发一次长文询问')
        self.assertIn(ref, got, '那条分享地址一个字都不许动')
        self.assertNotIn(FULL, got, '别人那篇文章的全文被当成这条推文的观点存下来了')
        self.assertEqual(1, summary['added'])
        self.assertEqual(TODAY, self.water(s['id']))

    def test_every_share_shape_hides_the_nested_address(self):
        """逐形态直测两个消费方 —— 端到端一轮只摆得下一种,而这几种各有一个紧前
        字符(`+` / `0` / `(` / `=` / `#` / `,`),按形态摆才看得出少认了哪一个。

        最后那条(`?x=1,<地址>`)是**一道已知的缝钉成当前取舍**:进了查询串之后再
        用内容字符紧连一条并列地址,我们按嵌套办。线划在"查询串/片段是不透明值
        容器、路径是层级结构"上 —— 它可解释,而"紧贴的那个字符是不是标点"不可解释
        (那是前三轮栽的那条路)。改方向时要和下面那条放行用例一起改。
        """
        for body in (f'https://example.org/share?text=Read+{OTHER}',
                     f'https://example.org/share?text=Read%20{OTHER}',
                     f'https://example.org/share?target=({OTHER})',
                     f'https://example.org/r?u={OTHER}',
                     f'https://example.org/p#{OTHER}',
                     f'https://a.org/?x=1,{OTHER}'):
            with self.subTest(body=body):
                self.assertFalse(needs_article({'url': TWEET, 'body': body}),
                                  '查询串/片段里那条地址不是这条推文的长文')
                self.assertEqual(body, lead_text(body).strip(),
                                 '别人那条地址被从中间剪断了')

    def test_a_real_address_in_an_outer_path_is_still_stripped(self):
        """**反过来钉一边**:外层 URL 的**路径**里用内容字符紧连的第二条地址照旧
        放行(人读的时候那就是两条并列的地址)。

        这是 `_nested` 第三步唯一的落点,收窄过头会在这里红 —— 而漏认 = 空正文
        永久固定,比多删一处还重一个量级。
        """
        for body in (f'https://example.org/doc,{CANON}',
                     f'https://example.org/doc;{CANON}',
                     f'https://a.org/?k=1,www.b.org/doc,{CANON}'):
            with self.subTest(body=body):
                self.assertTrue(needs_article({'url': TWEET, 'body': body}),
                                '并列写在外层路径后面的那条地址是真的')
                self.assertNotIn('/article/', lead_text(body),
                                 '它照旧要摘掉')


# ------------------------------------------------- 判据本身:三步各自的射程

class NestedEvidenceTests(unittest.TestCase):
    """直测那三步,**每一步和每一种证据各有自己的样本**。

    上面两族钉的是后果(库里那一行是什么),这一族钉的是判据 —— 两种证据同真同假
    的样本占了大多数,所以不单独摆独占样本的话,去掉任一种都不会红。
    """

    def hit(self, body):
        return x_article._first(x_article._ARTICLE_LINK, body)

    def test_a_lead_in_with_no_outer_url_is_never_nested(self):
        """①**紧贴左边那一段里没有另一条 URL 的起点 → 不是嵌套。**

        五个结构分隔符一个都不该在这里被拒:它们在正文里全都另有身份,而把它们写进
        一道定宽后视的实现会在这里红(连栽三次的那条路)。
        """
        for lead in ('全文=', '阅读地址/', 'Read &', '读过这篇吗?', 'What#',
                     '新文章...', '新文章~', '参考:', '', ' ', '（'):
            with self.subTest(lead=lead):
                self.assertIsNotNone(self.hit(lead + CANON),
                                     '左边没有外层 URL,这是作者自己那条地址')

    def test_the_separator_evidence_catches_a_nested_path_segment(self):
        """②(a) **独占射程**:紧前那一个字符占着结构位置,而起手式之后**没有**
        `?` `#` —— 第二种证据对它完全是瞎的。

        路径段嵌套(`/go/<地址>`、`/r/<地址>`)就是这一档,只留第二种证据的实现会
        在这里红。
        """
        for body in (f'https://evil.example/go/{OTHER}',
                     f'https://evil.example/r/{OTHER}',
                     f'www.evil.example/out/{OTHER}'):
            with self.subTest(body=body):
                self.assertIsNone(self.hit(body),
                                  '它占着外层路径的一个段位,是嵌套')

    def test_the_opaque_value_evidence_catches_share_links(self):
        """②(b) **独占射程**:紧前那一个字符**不是**分隔符,靠"外层起手式之后出现
        过 `?` `#`"认出来。

        这几种正是报告 F2 那一条,只留第一种证据的实现会在这里红。
        """
        for body in (f'https://example.org/share?text=Read+{OTHER}',
                     f'https://example.org/share?text=Read%20{OTHER}',
                     f'https://example.org/share?target=({OTHER})',
                     f'https://example.org/p#see,{OTHER}'):
            with self.subTest(body=body):
                self.assertIsNone(self.hit(body),
                                  '查询串/片段是不透明值容器,里面那条是嵌套')

    def test_the_last_outer_start_wins(self):
        """**多个起手式时取最后那一个** —— 候选只可能嵌在**紧挨着它左边**那条
        URL 里。

        拿更远那一条的查询符当证据等于张冠李戴,而这个方向上的错是**漏认**:
        `https://a.org/?k=1,www.b.org/doc,<地址>` 里那条 X 地址是真的(它并列写在
        `www.b.org` 的路径后面),取第一个起手式的实现会因为 `a.org` 那个 `?` 把它
        判成嵌套、永久删掉。反过来钉一边:换成 `www.b.org/go/<地址>` 照旧是嵌套
        —— 取最后那一个不等于放过真嵌套。
        """
        ok = f'https://a.org/?k=1,www.b.org/doc,{CANON}'
        self.assertIsNotNone(self.hit(ok), '它嵌的是紧挨着它左边那条 URL 的路径')
        bad = f'https://a.org/?k=1,www.b.org/go/{OTHER}'
        self.assertIsNone(self.hit(bad), '紧挨着它左边那条的路径段位,照旧是嵌套')

    def test_both_arms_run_through_the_same_judge(self):
        """**两档候选过的是同一份判据**,只是②档(裸域名)的正则已经把全部 URL
        内部字符挡在它前面了。

        裸域名那一档因此只可能**多拒**,不可能少拒 —— 不给它开特例,是因为"这个
        起点算不算一条新 URL 的起点"不该有两份答案。
        """
        bare = 'x.com/Example/article/10001'
        self.assertIsNotNone(self.hit(f'全文：{bare}'), '中文冒号后面的裸域名是真的')
        self.assertIsNone(self.hit(f'全文={bare}'),
                          '等号是 URL 内部字符,裸域名这一档照旧一律拒(已记的取舍)')

    def test_the_scan_only_crosses_url_characters(self):
        """①那一步**只往左吃 URL 合法字符**,不是"数到最近的空白"。

        数到空白的话 `参考 https://example.org/p#<地址>` 和
        `参考https://example.org/说明?<地址>` 这两种会给出同一个答案,而它们的正确
        答案相反 —— 后者的汉字天然落在字符集外面,所以那条 X 地址是真的。
        """
        self.assertIsNone(self.hit(f'参考 https://example.org/p#{OTHER}'),
                          '空格挡不住嵌套:结构字符紧邻外层 URL 自己的路径')
        self.assertIsNotNone(
            self.hit(f'参考https://example.org/说明?{CANON}'),
            '汉字断开了那一段,这条 X 地址是作者自己写的(已记的一道缝)')


if __name__ == '__main__':
    unittest.main(verbosity=2)
