# -*- coding: utf-8 -*-
"""长文补全的三道缝:哪些形态会让一篇长文**悄悄**落成空正文/只有一个链接。

三族各管一种,共同的要害是同一个:`put_items` 是 `ON CONFLICT(key) DO NOTHING`
—— 缺正文的条目一旦落库就**再也更新不了**,下一轮重叠重抓也救不回来。所以每一
条都**先断言后果**(这一篇的正文到底是什么、水位动没动、下一轮还取不取得到),
不是"有没有调过那个接口"。

①`TitleOnlyReplyTests`:接口只给标题、没给正文(回包被截断、`content.blocks`
  缩成空)。那一版只在"标题和正文同时为空"时才抛,于是这个形状返回成功。
②`ArticleCardTests`:推文写了一句介绍 + 一张长文卡片。X 把那条
  `/i/article/<id>` 链接藏起来改渲染成卡片,所以正文**非空**,另两条判据都不
  命中 —— 整篇文章就这么漏掉了,面板上一切正常。
③`BridgeChannelTests`:桥接通道整个没接补全,长文只存下那条文章链接。

和 `test_x_article.py` 按形状分开(那边是渲染、接口两档结局和浏览器后路):
那个文件已经 661 行,贴着仓库那条 700 行上限。摆盘共用它和 `test_x_channels`。

跑:  python -m unittest tests.test_x_article_gap -v
"""
import contextlib
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_article, x_browser, x_channels
from kb.feeds import fetching
from kb.feeds.fetching import FeedError, Response
from tests.test_x_article import (BLANK, OLD_WATER, _FakeChrome, answering,
                                  article_payload, entry, harvest_round, snapshot)
from tests.test_x_channels import ChannelBase

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
        """没有后路(feedgrab/桥接那两条):本轮不入库、水位一步不动。"""
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

    def test_the_bridge_shape_ignores_the_hint(self):
        """桥接那一档只认正文里明写的地址 —— 它手里压根没有 DOM 信号。"""
        e = {'url': 'https://x.com/example/status/9', 'body': '写了话',
             x_article.HINT: 'https://x.com/i/article/9'}
        self.assertTrue(x_article.needs_article(e))
        self.assertFalse(x_article.needs_article(e, links_only=True))
        self.assertTrue(x_article.needs_article(
            {'url': 'u', 'body': '写了话 https://x.com/i/article/9'}, links_only=True))
        self.assertFalse(x_article.needs_article({'url': 'u', 'body': ''}, links_only=True),
                         '桥接上的空正文多半是纯图推文,不值一次询问')


# --------------------------------------------------------------- ③ 桥接通道

_FRESH = datetime.now(timezone.utc) - timedelta(hours=5)


def bridge_rss(body):
    return ('''<?xml version="1.0"?><rss version="2.0"><channel><title>Example</title>
<item><guid>s900</guid><title>Example Person</title>
<link>https://x.com/Example/status/900</link>
<description>%s</description><pubDate>%s</pubDate></item></channel></rss>'''
            % (body, format_datetime(_FRESH))).encode()


class BridgeChannelTests(ChannelBase):
    """桥接通道:长文照旧要补,补不到就不入库、而且**不存 etag**。

    不存 validators 是这条路上唯一等价的扣留手段:存了的话下一轮条件请求命中
    304、整份 feed 一条都不返回,被扣下的那几条再也没有第二次机会。
    """

    def setUp(self):
        super().setUp()
        self.sub('Example')
        self.settings(x_channels.BRIDGE, bridges=['https://bridge.test/{handle}'])

    def collect(self, *script, rss=None, etag='W/"v1"'):
        calls = []

        def fake_request(url, headers=None, **kw):
            calls.append(url)
            return Response(200, url, rss if rss is not None else bridge_rss(
                '我写了一篇新文章 https://x.com/i/article/123456789'),
                {'etag': etag, 'last-modified': 'Thu, 09 Oct 2026 00:00:00 GMT'})

        req, api = answering(*script)
        with contextlib.ExitStack() as es:
            es.enter_context(patch.object(fetching, 'request', fake_request))
            es.enter_context(req)
            summary = sync.collect('social', FetchScope(force=True))
        return summary, api

    def stored(self):
        with reg.db() as c:
            return c.execute("SELECT title, body FROM feed_items WHERE kind='social'").fetchall()

    def row(self):
        with reg.db() as c:
            return c.execute("""SELECT etag, modified, last_note FROM subscriptions
                WHERE kind='social'""").fetchone()

    def test_the_bridge_fills_the_article_body(self):
        """**后果先断言**:库里是全文,不是那条链接。"""
        summary, api = self.collect(article_payload())
        self.assertEqual([], summary['errors'])
        rows = self.stored()
        self.assertEqual(1, len(rows))
        self.assertIn('整理书签', rows[0]['body'])
        self.assertTrue(rows[0]['body'].startswith('我写了一篇新文章'), rows[0]['body'][:40])
        self.assertEqual('X书签保存Skill x Teamily', rows[0]['title'])
        self.assertEqual(1, len(api.urls), '按文章地址问了一次')
        self.assertEqual('W/"v1"', self.row()['etag'], '读全了就照常存 etag')

    def test_a_held_article_is_not_stored_and_keeps_no_etag(self):
        summary, _ = self.collect(x_article.ArticleUnavailable('接口挂了'))
        self.assertEqual([], self.stored(), '补不到就不入库')
        self.assertEqual([], summary['errors'], '这不是一次失败')
        row = self.row()
        self.assertEqual('', row['etag'], '存了 etag 下一轮 304 就再也补不回来了')
        self.assertEqual('', row['modified'])
        self.assertIn('没取到', row['last_note'])
        self.assertIn('长文正文没取到', json.dumps(summary.get('partial') or [],
                                                ensure_ascii=False),
                      '面板上那行「部分同步」读的就是它')

    def test_the_next_round_brings_it_back(self):
        """扣留之后下一轮真的补回来了,而且提示被清掉。"""
        self.collect(x_article.ArticleUnavailable('接口挂了'))
        summary, _ = self.collect(article_payload())
        rows = self.stored()
        self.assertEqual(1, len(rows))
        self.assertIn('整理书签', rows[0]['body'])
        self.assertEqual('', self.row()['last_note'])

    def test_a_tweet_without_an_article_link_is_not_asked(self):
        """反过来钉一边:桥接上的普通推文一次询问都不发。"""
        summary, api = self.collect(
            article_payload(), rss=bridge_rss('随手一条 https://t.co/abcdef'))
        self.assertEqual([], api.urls)
        self.assertEqual(1, len(self.stored()))
        self.assertEqual('', self.row()['last_note'])

    def test_a_plain_rss_subscription_is_left_alone(self):
        """非 X 的订阅不走这一步 —— 判据是通道,不是"正文里有没有链接"。"""
        reg.save_subscription('rss', {'name': 'Blog', 'url': 'https://example.org/feed'})
        calls = []

        def fake_request(url, headers=None, **kw):
            calls.append(url)
            return Response(200, url, bridge_rss('一篇博客'), {})

        req, api = answering(article_payload())
        with contextlib.ExitStack() as es:
            es.enter_context(patch.object(fetching, 'request', fake_request))
            es.enter_context(req)
            sync.collect('rss', FetchScope(force=True))
        self.assertEqual([], api.urls, 'rss 订阅不许去问 X 长文接口')


if __name__ == '__main__':
    unittest.main()
