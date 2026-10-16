# -*- coding: utf-8 -*-
# 这份 docstring 里写着正则(`\S*`、`\w+`),所以必须是 **raw** 字符串:普通字符串里
# 这些都是"非法转义",Python 3.12 起报 SyntaxWarning、3.15 要变成错误。
r"""长文补全的四道缝:哪些形态会让一篇长文**悄悄**落成空正文/缺字的正文。

四族各管一种,共同的要害是同一个:`put_items` 是 `ON CONFLICT(key) DO NOTHING`
—— 缺正文的条目一旦落库就**再也更新不了**,下一轮重叠重抓也救不回来。所以每一
条都**先断言后果**(这一篇的正文到底是什么、水位动没动、下一轮还取不取得到),
不是"有没有调过那个接口"。

①`TitleOnlyReplyTests`:接口只给标题、没给正文(回包被截断、`content.blocks`
  缩成空)。那一版只在"标题和正文同时为空"时才抛,于是这个形状返回成功。
②`ArticleCardTests`:推文写了一句介绍 + 一张长文卡片。X 把那条
  `/i/article/<id>` 链接藏起来改渲染成卡片,所以正文**非空**,另两条判据都不
  命中 —— 整篇文章就这么漏掉了,面板上一切正常。
③`ArticlePathFormTests`:X 的文章地址有两种路径形式,而判据只认 `/i/article/`
  那一种。`/<handle>/article/<id>`(X 自己 302 到的规范形式,也正是
  `article_page()` 生成的那个)在**三条通道**上各有一种末态:feedgrab 和
  「浏览器通道没读到卡片」是那条链接当正文入库,「浏览器通道读到了卡片」是正文
  补上了、而那条地址留在开头。
④`ProseBoundaryTests`:摘掉那条地址时把**紧挨着它**的标点和文字一起删掉。两条
  正则的尾巴原来都是"吃到下一个空白为止"(`\S*` / Unicode 的 `\w+`),而中文不带
  空格 —— 「<地址>,重点看第三节。」那句说明被整段当成地址删掉,采集照报成功。

和 `test_x_article.py` 按形状分开(那边是渲染、接口两档结局和浏览器后路):
那个文件已经 661 行,贴着仓库那条 700 行上限。摆盘共用它和 `test_x_channels`。

④族管的是地址**右边**那条缝;**左边**那条(起点必须是主机名的起点,别人家的地址
不许被当成 X 文章链接剪断)在 `test_x_article_host.py` —— 本文件已经贴着那条
700 行上限,它借③族这两个 round helper。

跑:  python -m unittest tests.test_x_article_gap -v
"""
import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import x_article, x_browser
from kb.feeds.fetching import FeedError
from tests.test_x_article import (BLANK, OLD_WATER, _FakeChrome, answering,
                                  article_payload, entry, harvest_round, snapshot)
from tests.test_x_feedgrab_contract import TODAY, FeedgrabRound, tweet

FULL = '整理书签、做学习计划，我以前都是一个人对着 AI 来回问。'


# --------------------------------------------------------------- ① 只给标题

class TitleOnlyReplyTests(unittest.TestCase):
    """接口答了一篇"有标题、没正文"的文章 —— 那是**没取到**,不是取到了。"""

    def test_a_reply_with_a_title_but_no_body_is_unavailable(self):
        payload = article_payload(blocks=[])
        req, calls = answering(payload)
        with req:
            with self.assertRaises(x_article.ArticleUnavailable) as e:
                x_article.fetch('https://x.com/Example/status/9')
        self.assertIn('没有正文', str(e.exception))
        self.assertIn('X书签保存Skill', str(e.exception), '说清只拿到了什么')
        self.assertEqual(1, len(calls.urls))

    def test_the_browser_fallback_takes_over_a_title_only_reply(self):
        """**后果先断言**:后路读到了,所以这一轮照常推进、正文是真的。"""
        got, calls = harvest_round([article_payload(blocks=[])],
                                   snaps=[snapshot(blocks=[FULL], title='真标题')])
        body = {e['url']: e['body'] for e in got['entries']}
        self.assertEqual(FULL, body['https://x.com/example/status/9'])
        self.assertEqual('真标题', next(e['title'] for e in got['entries']
                                      if e['url'].endswith('/9')))
        self.assertEqual('', got['incomplete'], '这一轮算读全了')
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertTrue(_FakeChrome.visited, '接口没给正文 → 要走后路')

    def test_without_a_fallback_a_title_only_reply_holds_the_round(self):
        """没有后路(feedgrab 那一条):本轮不入库、水位一步不动。"""
        got, _ = harvest_round([article_payload(blocks=[])], fallback=False)
        urls = [e['url'] for e in got['entries']]
        self.assertNotIn('https://x.com/example/status/9', urls)
        self.assertIn('长文正文没取到', got['incomplete'])
        self.assertEqual(OLD_WATER, got['watermark'], '水位一步都不许动')

    def test_a_real_body_still_counts_as_a_hit(self):
        """反过来钉一边:正文在的那一轮不许被判成"没取到"。"""
        got, _ = harvest_round([article_payload()])
        body = next(e['body'] for e in got['entries'] if e['url'].endswith('/9'))
        self.assertIn('整理书签', body)
        self.assertEqual('', got['incomplete'])
        self.assertEqual([], _FakeChrome.visited, '接口给了正文就不该开浏览器')


# --------------------------------------------------------------- ② 带介绍的卡片

def card_round(script, *, hint='https://x.com/i/article/123456789', text='我写了一篇新文章'):
    """真跑一轮 `x_browser.harvest`:一条「写了介绍 + 带长文卡片」的推文。"""
    def fake_one(page, handle, floor):
        e = {**entry(url='https://x.com/example/status/9', body=text), 'title': text}
        if hint:
            e[x_article.HINT] = hint
        return [e], ''

    _FakeChrome.reset(BLANK)
    req, calls = answering(*script)
    with contextlib.ExitStack() as es:
        for cm in (req, patch.object(x_browser, '_harvest_one', fake_one),
                   patch.object(x_browser, 'Chrome', _FakeChrome),
                   patch.object(x_article, 'PAGE_WAIT', 0.0),
                   patch.object(x_article, 'BROWSER_FALLBACK', False)):
            es.enter_context(cm)
        got = x_browser.harvest([('example', OLD_WATER)], profile='/tmp/p')
    return got['example'], calls


class ArticleCardTests(unittest.TestCase):
    def test_the_timeline_keeps_the_card_link_as_a_hint(self):
        item = {'url': 'https://x.com/example/status/9', 'text': '我写了一篇新文章',
                'datetime': '2026-10-09T08:01:00Z', 'name': 'Example',
                'author': 'example', 'article': 'https://x.com/i/article/123'}
        out = x_browser._entry(item)
        self.assertEqual('https://x.com/i/article/123', out[x_article.HINT])
        self.assertTrue(x_article.needs_article(out))
        # 没有卡片的普通推文照旧不问(不拿 t.co 当信号)。
        plain = x_browser._entry({**item, 'article': '', 'text': '随手一条 https://t.co/abc'})
        self.assertNotIn(x_article.HINT, plain)
        self.assertFalse(x_article.needs_article(plain))

    def test_a_card_with_a_lead_sentence_gets_the_full_article(self):
        """**后果先断言**:介绍留在开头,全文接在后面,一个字都没丢。"""
        got, calls = card_round([article_payload()])
        body = next(e['body'] for e in got['entries'])
        self.assertTrue(body.startswith('我写了一篇新文章'), body[:40])
        self.assertIn('整理书签', body)
        self.assertEqual('', got['incomplete'])
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_the_hint_never_reaches_the_item(self):
        """信号只在采集期间有意义:`put_items` 把整份条目哈希成 content_hash。"""
        got, _ = card_round([article_payload()])
        for e in got['entries']:
            self.assertNotIn(x_article.HINT, e)

    def test_a_card_that_cannot_be_read_holds_the_round(self):
        got, _ = card_round([FeedError('HTTP 500')])
        self.assertEqual([], got['entries'], '补不到就不入库')
        self.assertIn('长文正文没取到', got['incomplete'])
        self.assertEqual(OLD_WATER, got['watermark'])

    def test_a_plain_tweet_with_a_link_is_not_asked(self):
        """反过来钉一边:没有卡片信号的推文一次请求都不发。"""
        got, calls = card_round([article_payload()], hint='',
                                text='随手一条 https://t.co/abcdef')
        self.assertEqual([], calls.urls, '一次询问都不许发')
        self.assertEqual('', got['incomplete'])
        self.assertEqual(1, len(got['entries']))


# ------------------------------------------------------- ③ 文章地址的两种形式

class ArticlePathFormTests(FeedgrabRound):
    """**X 的文章地址有两种路径形式,只认一种就会漏掉另一种的全文。**

    `/i/article/<article_id>` 是 t.co 展开出来的那种,`/<handle>/article/<tweet_id>`
    是 X 自己 302 到的**规范形式** —— 后者正是本实现 `article_page()` 生成的那个
    地址,不是一种我们不支持的 URL 规范。原来 `_ARTICLE_LINK` 只认前一种,于是后
    一种在**三条保留通道**上各有一种末态(2026-10-09 实测,下面每条用例各钉一个):

    - **feedgrab**:`needs_article` 三条判据一条都不命中(①看不见这个形式,③因为
      "正文里有字"而不命中,②的卡片信号只有浏览器通道才有)—— 那条链接本身当成
      正文入库,`added=1`、`errors=[]`、`partial=[]`、水位照常推进,标题回落成
      「某某 的动态」;而 `put_items` 是 `DO NOTHING`,**下一轮重抓也更新不了它**。
    - **浏览器 / attach,没有卡片信号**(某次滚动没渲染出来、或者那张卡片被引用推
      容器那道排除规则挡掉):末态和上面逐字相同。
    - **浏览器 / attach,有卡片信号**:卡片把这一条救回来了,可 `lead_text` 同样
      认不出这个形式,于是那条地址原样留在库里那篇正文的开头。

    判据只有 `_ARTICLE_LINK` 那一条正则(`needs_article` 判据① 和 `lead_text`
    共用),所以这一族里既有真走一轮采集的末态,也有直接钉那条正则两头的 —— **认宽
    了同样有代价**:它在 `lead_text` 里是拿去 `sub` 掉的,会删掉推文里真实的字。
    """

    LEGACY = 'https://x.com/i/article/2108252660529049601'
    # = article_page('https://x.com/Example/status/10001'),feedgrab 那条推文的 id
    CANON = 'https://x.com/Example/article/10001'

    # --- feedgrab:真跑一轮 sync.collect(),一行不打桩 -------------------------

    def feedgrab_round(self, body):
        """真跑一轮 feedgrab 通道:只替掉那个外部程序本身和长文接口的回包。"""
        s = self.start_from()
        self.cli(example={'display': 'Example Person',
                          'tweets': [tweet(title='', body=body)]})
        req, calls = answering(article_payload())
        with req:
            summary = self.collect()
        rows = self.rows(s['id'])['items']
        return summary, (rows[0] if rows else {}), calls, s

    def test_the_canonical_form_reaches_the_knowledge_base(self):
        """**后果先断言**:库里那一行是全文,不是一条光秃秃的链接。

        只验"问过那次接口"的话,一个把链接当正文存下来、却顺手问了一次的实现也能
        过;而这一篇一旦以链接入库就永久改不回来。
        """
        summary, row, calls, s = self.feedgrab_round(self.CANON)
        self.assertIn(FULL, row.get('body') or '')
        self.assertNotIn('/article/', row.get('body') or '',
                         '那条地址不是正文,它是 X 替作者渲染成卡片的链接')
        self.assertEqual('X书签保存Skill x Teamily', row.get('title'),
                         '标题回落成「某某 的动态」= 这一条压根没去补过')
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(['https://api.fxtwitter.com/Example/status/10001'],
                         calls.urls)

    def test_the_legacy_form_still_reaches_it(self):
        """**反过来钉一边**:为了认规范形式而把旧式弄坏的实现会在这里红。"""
        summary, row, calls, _ = self.feedgrab_round(self.LEGACY)
        self.assertIn(FULL, row.get('body') or '')
        self.assertEqual(1, summary['added'])
        self.assertEqual(1, len(calls.urls))

    def test_a_tweet_without_any_article_link_is_not_asked_about(self):
        """**再钉一边**:认宽了会给一大半普通推文各买一次询问。"""
        summary, row, calls, _ = self.feedgrab_round('今天读到一篇好文章，推荐大家看。')
        self.assertEqual([], calls.urls, '普通推文不许去问长文接口')
        self.assertEqual('今天读到一篇好文章，推荐大家看。', row.get('body'))
        self.assertEqual(1, summary['added'])

    # --- 浏览器通道:真跑一轮 harvest ---------------------------------------

    def browser_round(self, body, *, hint=False):
        """一条推文,正文由调用方给;`hint` 决定时间线上那张卡片读到没有。"""
        def fake_one(page, handle, floor):
            e = {**entry(url='https://x.com/example/status/9'), 'body': body}
            if hint:
                e[x_article.HINT] = 'https://x.com/example/article/9'
            return [e], ''

        _FakeChrome.reset(BLANK)
        req, calls = answering(article_payload())
        with contextlib.ExitStack() as es:
            for cm in (req, patch.object(x_browser, '_harvest_one', fake_one),
                       patch.object(x_browser, 'Chrome', _FakeChrome),
                       patch.object(x_article, 'PAGE_WAIT', 0.0)):
                es.enter_context(cm)
            got = x_browser.harvest([('example', OLD_WATER)], profile='/tmp/p')
        return got['example'], calls

    # 这条推文的规范文章地址(= article_page('https://x.com/example/status/9'))。
    CANON_9 = 'https://x.com/example/article/9'

    def test_the_canonical_form_is_filled_without_a_card_hint(self):
        """**没有卡片信号时,正文里那条地址是唯一的信号** —— 认不出它就是漏掉整篇。

        卡片信号不是永远在:某次滚动它可能没渲染出来(所以 `_COLLECT_JS` 那边是
        「只补不清」),引用推容器里的那张还会被故意排掉。判据① 本来就该是独立的
        第二道。
        """
        got, calls = self.browser_round(f'我写了一篇新文章\n\n{self.CANON_9}')
        body = got['entries'][0]['body']
        self.assertIn(FULL, body)
        self.assertNotIn('/article/', body)
        self.assertEqual('X书签保存Skill x Teamily', got['entries'][0]['title'])
        self.assertEqual('', got['incomplete'], '补到了,这一轮算读全了')
        self.assertNotEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(calls.urls))

    def test_the_canonical_link_is_not_left_in_the_lead(self):
        """卡片信号救回来的那一条:正文开头不许留着那条地址。

        这一条钉的是 `lead_text` 那一半 —— 判据① 换了口径而 `lead_text` 没换时,
        上面那条会绿而这条红。
        """
        got, _ = self.browser_round(f'我写了一篇新文章\n\n{self.CANON_9}', hint=True)
        body = got['entries'][0]['body']
        self.assertTrue(body.startswith('我写了一篇新文章'), body[:60])
        self.assertNotIn('/article/', body)
        self.assertIn(FULL, body)

    def test_the_author_s_own_words_are_still_kept(self):
        """**反过来钉一边**:作者写的那句介绍不许跟着链接一起被 `sub` 掉。"""
        got, _ = self.browser_round(f'我写了一篇新文章\n\n{self.LEGACY}', hint=True)
        self.assertTrue(got['entries'][0]['body'].startswith('我写了一篇新文章'))

    # --- 判据本身:两头各钉一道 ---------------------------------------------

    def test_both_path_forms_are_recognised(self):
        """两种形式、四种写法(带/不带 scheme、x.com/twitter.com、带查询串)。"""
        tw = 'https://x.com/Example/status/900'
        for body in (self.LEGACY, 'x.com/i/article/900',
                     x_article.article_page(tw), 'x.com/Example/article/900',
                     'https://twitter.com/Example/article/900?s=20',
                     f'我写了一篇新文章\n\n{x_article.article_page(tw)}'):
            with self.subTest(body=body):
                self.assertTrue(x_article.needs_article({'url': tw, 'body': body}),
                                '漏认 = 这条地址当正文入库,DO NOTHING 之下改不回来')

    def test_what_is_not_an_article_link_is_left_alone(self):
        """**认宽了会删掉推文里真实的字** —— 这条正则在 `lead_text` 里是拿去
        `sub(' ')` 的,所以 handle 的字符类不许放宽(同 `_STATUS`)。"""
        tw = 'https://x.com/Example/status/900'
        for body in ('今天读到一篇好文章，推荐给大家。',
                     'https://x.com/Example/status/900',
                     'https://example.org/Example/article/900',
                     'https://x.com/i/articles/900',
                     'https://x.com/' + 'a' * 21 + '/article/900',
                     'https://x.com/Example/article/abc',
                     'https://x.com/a/b/article/900'):
            with self.subTest(body=body):
                self.assertFalse(x_article._ARTICLE_LINK.search(body))
                self.assertEqual(body, x_article.lead_text(body),
                                 '一个字都不许被当成文章链接删掉')

    def test_lead_text_strips_both_path_forms(self):
        """`lead_text` 和判据① 共用那条正则,所以两种形式都该被摘掉。"""
        for link in (self.LEGACY, 'https://x.com/Example/article/900'):
            with self.subTest(link=link):
                self.assertEqual('我写了一篇新文章',
                                 x_article.lead_text(f'我写了一篇新文章\n\n{link}'))
                self.assertEqual('', x_article.lead_text(link))


class ProseBoundaryTests(FeedgrabRound):
    """**摘掉那条地址时,紧挨着它的标点和文字一个都不许跟着走。**

    两条正则的尾巴原来都是"吃到下一个空白为止"(`_ARTICLE_LINK` 的 `\\S*`、`_TCO`
    那条在 str 模式下是 Unicode 的 `\\w+`)—— **而中文不带空格**。于是
    「我写了一篇新文章：<地址>，重点看第三节。」里那句说明被整段当成地址删掉,
    `added=1`、`errors=[]`、`partial=[]`、水位照常推进,一行提示都没有;而
    `put_items` 是 `DO NOTHING`,**重复同步也补不回来**。

    ③族把识别口径扩到规范形式之后,同一条尾巴在那条新路径上跟着发作;
    旧式 `/i/article/` 和 t.co 那条在它之前就坏着。修法是两条正则共用一份
    尾巴口径(`_URL_TAIL`):字符类只收 URL 的 ASCII 合法字符(中文天然落在外面),
    结尾必须落在"不像句读"的那一档(英文的 `. ` `) ` 收尾同理)。

    **两头都要钉**:只验"那句说明还在"的话,一个压根不摘地址的实现也能全绿
    —— 所以每条用例同时断言那条地址**确实被摘掉了**,另有一条反过来钉住
    判据① 不受收窄影响（它是 `search`,方向和 `lead_text` 正好相反）。
    """

    LEAD = '我写了一篇新文章：'
    PROSE = '重点看第三节。'
    CANON = 'https://x.com/Example/article/10001'
    LEGACY = 'https://x.com/i/article/2108252660529049601'

    # 跟③族逐字相同的两个摆盘,直接借过来而不是复制一份 —— 它们只用到
    # `FeedgrabRound` 的能力和模块级摆盘。
    feedgrab_round = ArticlePathFormTests.feedgrab_round
    browser_round = ArticlePathFormTests.browser_round

    def test_prose_glued_to_a_canonical_link_survives_feedgrab(self):
        """**后果先断言**:库里那一行还带着作者写在地址后面的那句话。

        这是审查那份证据的回归版:真跑一轮 `sync.collect()`,外部程序和入库
        都是真的。水位照常推进这一点尤其要紧 —— 它意味着下一轮不会再碰这一条。
        """
        body = f'{self.LEAD}{self.CANON}，{self.PROSE}'
        summary, row, calls, s = self.feedgrab_round(body)
        got = row.get('body') or ''
        self.assertIn(self.PROSE, got, '作者写在地址后面的说明被当成地址删掉了')
        self.assertIn(self.LEAD, got)
        self.assertIn(FULL, got, '全文还是要补上')
        self.assertNotIn('/article/', got, '而那条地址本身照旧要摘掉')
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(1, len(calls.urls))

    def test_prose_glued_to_a_legacy_link_survives_feedgrab(self):
        """旧式形式在③族之前就有这个病,两种形式共用一条尾巴口径。"""
        body = f'{self.LEAD}{self.LEGACY}，{self.PROSE}'
        summary, row, calls, _ = self.feedgrab_round(body)
        got = row.get('body') or ''
        self.assertIn(self.PROSE, got)
        self.assertIn(FULL, got)
        self.assertNotIn('/article/', got)
        self.assertEqual(1, summary['added'])

    def test_prose_glued_to_a_link_survives_the_browser_channel(self):
        """另一条保留通道:同一条尾巴口径,所以两条各钉一条端到端。"""
        body = f'{self.LEAD}https://x.com/example/article/9，{self.PROSE}'
        got, calls = self.browser_round(body)
        text = got['entries'][0]['body']
        self.assertIn(self.PROSE, text)
        self.assertIn(FULL, text)
        self.assertNotIn('/article/', text)
        self.assertEqual('', got['incomplete'], '补到了,这一轮算读全了')
        self.assertNotEqual(OLD_WATER, got['watermark'])

    def test_the_boundary_matrix(self):
        """八种真实推文写法,每一种都在 2026-10-09 复现过丢字。

        判据是**两半同时成立**:作者那句话逐字还在,而地址确实没了。
        """
        A = 'https://x.com/Example/article/10001'
        cases = [
            ('中文逗号紧贴', f'{self.LEAD}{A}，{self.PROSE}'),
            ('中文句号紧贴', f'{self.LEAD}{A}。{self.PROSE}'),
            ('中文括号包住', f'{self.LEAD}（{A}）{self.PROSE}'),
            ('中文方括号', f'{self.LEAD}【{A}】{self.PROSE}'),
            ('查询串后紧跟中文', f'{self.LEAD}{A}?s=20，{self.PROSE}'),
            ('片段后紧跟中文', f'{self.LEAD}{A}#sec3，{self.PROSE}'),
            ('尾斜杠后紧跟中文', f'{self.LEAD}{A}/，{self.PROSE}'),
            ('旧式形式同样', f'{self.LEAD}{self.LEGACY}，{self.PROSE}'),
        ]
        for name, body in cases:
            with self.subTest(name):
                got = x_article.lead_text(body)
                self.assertIn(self.PROSE, got, f'{name}:作者的说明被删掉了')
                self.assertIn(self.LEAD, got, f'{name}:开头那句也没了')
                self.assertNotIn('/article/', got, f'{name}:地址没摘掉')

    def test_ascii_sentence_punctuation_is_not_part_of_the_url(self):
        """英文推文同理:句末句号、右括号不属于那条地址。

        最后两条是**结尾那一道自己的射程**:中文标点压根不在字符类里,所以前面那些
        用例靠字符类就绿了;而 `.` `,` 在 URL 中间是合法的(`?t=a.b`),贴在查询串
        尾巴上的那一个却是作者的句读 —— 只有"结尾必须落在窄集"这一道分得开。
        """
        A = 'https://x.com/Example/article/10001'
        self.assertEqual('Read it:  . Worth it.',
                         x_article.lead_text(f'Read it: {A}. Worth it.'))
        self.assertEqual('(see  ) ok', x_article.lead_text(f'(see {A}) ok'))
        self.assertEqual('Read it:  . Worth it.',
                         x_article.lead_text(f'Read it: {A}?s=20. Worth it.'))
        self.assertEqual('Read it:  , really.',
                         x_article.lead_text(f'Read it: {A}?t=a.b&s=09, really.'))

    def test_a_sentence_final_question_mark_is_not_an_empty_query_string(self):
        """`<地址>?` 里那个问号是作者的标点,不是一个空查询串。"""
        A = 'https://x.com/Example/article/10001'
        self.assertEqual('Did you read  ?', x_article.lead_text(f'Did you read {A}?'))

    def test_a_t_co_link_does_not_eat_the_chinese_after_it(self):
        """`_TCO` 原来是 `\\w+`,而 Python 的 str 模式下它是 Unicode 的。

        它和上面那条在同一个函数的下一行、是同一次"把链接从正文里摘掉"的
        动作,后果逐字相同(实测 `lead_text` 只剩 `短链  。`)。
        """
        for link in ('https://t.co/aBcD1234', 'https://t.co/aBcD1234?s=20'):
            with self.subTest(link=link):
                got = x_article.lead_text(f'短链 {link}{self.PROSE}')
                self.assertIn(self.PROSE, got)
                self.assertNotIn('t.co', got, '短链本身照旧要摘掉')

    def test_the_first_criterion_still_matches_a_link_with_a_tail(self):
        """**反过来钉一边**:收窄尾巴不许影响"有没有一条文章地址"。

        两个消费方的方向正好相反:判据① 是 `search`,宁可认宽(漏认 = 空正文
        永久固定);`lead_text` 是 `sub`,宁可少删。地址主体已经足够回答前者,所以
        收窄尾巴只该动后者 —— 把尾巴收得连主体一起认不出来的实现会在这里红。
        """
        tw = 'https://x.com/Example/status/10001'
        A = 'https://x.com/Example/article/10001'
        for body in (A, f'{A}?s=20', f'{A}#sec3', f'{A}/', f'{A}/?s=20&t=xy',
                     f'{self.LEAD}{A}，{self.PROSE}',
                     f'{self.LEAD}{A}?s=20，{self.PROSE}'):
            with self.subTest(body=body):
                self.assertTrue(x_article.needs_article({'url': tw, 'body': body}),
                                '漏认 = 这条地址当正文入库,DO NOTHING 之下改不回来')

    def test_a_link_with_a_tail_is_still_stripped_clean(self):
        """**再钉一边**:带尾巴的地址要摘干净,不许在正文里留残片。

        收窄过头的实现会把 `?s=20` 或者尾斜杠留在库里那篇正文的开头。
        """
        A = 'https://x.com/Example/article/10001'
        for tail in ('', '/', '?s=20', '#sec3', '/?s=20&t=xy', '?t=a.b&s=09'):
            with self.subTest(tail=tail):
                got = x_article.lead_text(f'A {A}{tail} B')
                self.assertEqual('A   B', got, '地址连尾巴要整条摘掉')


if __name__ == '__main__':
    unittest.main()
