# -*- coding: utf-8 -*-
"""手动收录:给一条链接,内容抓进知识库(kb/feeds/grab.py + grab_page.py)。

判据一律**先断言后果**:抓不成的那几档都要钉「库里确实没有这一条」,而不是只验
它抛了异常 —— 空正文一旦落库,`put_items` 的 `DO NOTHING` 之下永久改不回来,那
正是这一块唯一不可恢复的失败形态。反过来也钉一边:该收的照旧收得下、重复投递
不改动库里那一份。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml

from kb import maintenance, pipeline_state
from kb.collectors import FetchScope
from kb.feeds import grab, grab_page, registry as reg, sync, x_article
from kb.feeds.fetching import Response
from kb.source_types import feed_types

TWEET = 'https://x.com/example/status/12345'
PAGE = 'https://example.org/posts/hello'


def tweet_payload(**tweet) -> bytes:
    base = {'url': TWEET, 'text': '随手记一笔', 'created_timestamp': 1792063812,
            'author': {'name': '某某', 'screen_name': 'example'}}
    return json.dumps({'code': 200, 'tweet': {**base, **tweet}}).encode()


def page_html(*, title='Hello world', body=None, extra='') -> str:
    # 段落要够长:`readable.MIN_CHARS` 是 400,比它短的一律算「没找到正文」。
    paras = body or ['这是正文第一段，长度要过得了提取器那道门槛。' * 12,
                     '第二段同样要够长，否则 readable 会判成"没找到正文"。' * 12]
    return ('<html><head><meta charset="utf-8">'
            f'<meta property="og:title" content="{title}">{extra}'
            '</head><body><nav>导航 首页 关于</nav><article>'
            + ''.join(f'<p>{p}</p>' for p in paras)
            + '</article><footer>版权所有</footer></body></html>')


def html_resp(html: str, url: str = PAGE) -> Response:
    return Response(200, url, html.encode('utf-8'),
                    {'Content-Type': 'text/html; charset=utf-8'})


class GrabCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # 维护锁的 lockfile 在 DATA_DIR 下:不指到 tmp 的话这些用例会去 flock
        # 生产那一个(口径同 tests/test_feed_sources.py)。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault')):
            p.start()
            self.addCleanup(p.stop)
        self.state = deepcopy(pipeline_state._state)
        pipeline_state._state.update(running=False, stop_requested=False)
        self.addCleanup(lambda: pipeline_state._state.update(self.state))

    def items(self) -> list[dict]:
        with reg.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM feed_items ORDER BY key')]

    def grab_tweet(self, **tweet):
        """抓一条推文:`request` 打桩成 fxtwitter 的回包。

        **打的是 `grab.request`,不是 `fetching.request`** —— grab 在模块顶层
        import 了它;长文那一步走的是 `x_article.request`,各打各的,于是"主路和
        后路各注入一次"分得开(元规则「两份实现各有自己的回归」)。
        """
        with patch.object(grab, 'request', return_value=Response(
                200, TWEET, tweet_payload(**tweet), {'Content-Type': 'application/json'})):
            return grab.grab(TWEET)


class AddressTests(GrabCase):
    def test_normalize_keeps_one_identity_for_the_same_content(self):
        # 键是 sha256(kind:url):规范化漂一点就会变成两条记录、两篇 md。
        self.assertEqual(grab.normalize('https://x.com/Example/status/12345?s=20&t=x'),
                         'https://x.com/example/status/12345')
        self.assertEqual(grab.normalize('example.org/posts/hello?utm_source=rss'),
                         'https://example.org/posts/hello')
        self.assertEqual(grab.normalize(' https://example.org/a?id=7 '),
                         'https://example.org/a?id=7')

    def test_classify_sends_tweets_and_pages_down_their_own_path(self):
        self.assertEqual(grab.classify(grab.normalize(TWEET)), 'x')
        self.assertEqual(grab.classify(grab.normalize(PAGE)), 'web')
        # `endswith` 式的域判断会把它也当成 X(口径同 x_login_import 那条)
        self.assertEqual(grab.classify('https://notx.com/example/status/12345'), 'web')

    def test_refusals_say_what_to_do_next(self):
        for url, word in [('https://x.com/example', '大V观点'),
                          ('https://mp.weixin.qq.com/s/abc123', '公众号'),
                          ('https://x.com/example/status/12345/photo/1', '单条推文')]:
            with self.assertRaises(grab.GrabError) as caught:
                grab.classify(grab.normalize(url))
            self.assertIn(word, str(caught.exception))

    def test_private_addresses_never_leave_the_machine(self):
        for bad in ('http://127.0.0.1/a', 'http://192.168.1.9/x', 'http://box.local/a'):
            with self.assertRaises(grab.GrabError):
                grab.normalize(bad)


class TweetTests(GrabCase):
    def test_plain_tweet_keeps_its_line_breaks_and_shanghai_clock(self):
        entry = self.grab_tweet(text='第一行\n\n第二行', created_timestamp=1792063812)
        self.assertEqual(entry['body'], '第一行\n\n第二行')   # 分行就是作者的分段
        self.assertEqual(entry['title'], '第一行 第二行')      # 标题反过来必须单行
        self.assertEqual(entry['published'], '2026-10-15 19:30')  # UTC 11:30 + 8
        self.assertEqual(entry['author'], '某某')
        self.assertEqual(entry['content_kind'], 'post')

    def test_long_form_tweet_brings_the_article_body(self):
        link = 'https://x.com/i/article/999'
        article = {'title': '一篇长文', 'content': {'blocks': [
            {'text': '长文正文第一段', 'type': 'unstyled'}]}}
        with patch.object(x_article, 'request', return_value=Response(
                200, TWEET, json.dumps({'tweet': {'article': article}}).encode(), {})):
            entry = self.grab_tweet(text=f'我写了一篇新文章 {link}')
        self.assertEqual(entry['title'], '一篇长文')
        self.assertIn('长文正文第一段', entry['body'])
        # 推文自己写的那句介绍留在开头,而那条裸链接不许留下(lead_text 的口径)
        self.assertTrue(entry['body'].startswith('我写了一篇新文章'))
        self.assertNotIn(link, entry['body'])

    def test_a_long_form_tweet_whose_body_is_missing_is_not_stored(self):
        # **后果断言**:取不到正文时库里一行都不许有 —— 空正文永久改不回来。
        with patch.object(x_article, 'request', side_effect=OSError('接口挂了')):
            with self.assertRaises(grab.GrabError) as caught:
                self.grab_tweet(text='新文章 https://x.com/i/article/999')
        self.assertIn('长文', str(caught.exception))
        self.assertEqual(self.items(), [])

    def test_photo_only_tweet_is_stored_as_its_media(self):
        entry = self.grab_tweet(text='', media={'photos': [{'url': 'https://pbs.twimg.com/a.jpg'}]})
        self.assertIn('https://pbs.twimg.com/a.jpg', entry['body'])
        self.assertEqual(entry['title'], '某某 的动态')

    def test_a_tweet_with_nothing_in_it_is_refused(self):
        with self.assertRaises(grab.GrabError):
            self.grab_tweet(text='', media={})
        self.assertEqual(self.items(), [])

    def test_deleted_or_protected_tweets_say_so(self):
        with patch.object(grab, 'request', return_value=Response(
                200, TWEET, b'{"code":404,"message":"NOT_FOUND"}', {})):
            with self.assertRaises(grab.GrabError) as caught:
                grab.grab(TWEET)
        self.assertIn('删除', str(caught.exception))

    def test_text_is_never_coerced_from_another_type(self):
        # 强转出来的 "{'text': …}" 非空,会被当成「这条有正文」落库
        # (口径同 x_article._must_text)。
        with self.assertRaises(grab.GrabError):
            self.grab_tweet(text={'text': '不是字符串'}, media={})
        self.assertEqual(self.items(), [])

    def test_quoted_tweet_stays_a_quote_line(self):
        entry = self.grab_tweet(text='同意', quote={'text': '原话', 'author': {'screen_name': 'who'}})
        self.assertTrue(entry['body'].startswith('> 引用 @who：原话'))


class PageTests(GrabCase):
    def page(self, resp: Response):
        with patch.object(grab, 'request', return_value=resp):
            return grab.grab(PAGE)

    def test_a_normal_page_brings_body_title_author_and_a_shanghai_clock(self):
        extra = ('<meta name="author" content="某记者">'
                 '<meta property="article:published_time" content="2026-10-15T10:02:37Z">')
        entry = self.page(html_resp(page_html(extra=extra)))
        self.assertEqual(entry['title'], 'Hello world')
        self.assertEqual(entry['author'], '某记者')
        # UTC 10:02 是上海 18:02。照字面取会把这一篇推到前一天的日报窗口里。
        self.assertEqual(entry['published'], '2026-10-15 18:02')
        self.assertEqual(entry['content_kind'], 'content')
        self.assertIn('这是正文第一段', entry['body'])
        self.assertNotIn('导航 首页 关于', entry['body'])

    def test_modified_time_never_wins_over_the_publish_time(self):
        extra = ('<meta property="article:modified_time" content="2026-10-16T19:17:00Z">'
                 '<script type="application/ld+json">'
                 '{"@type":"NewsArticle","datePublished":"2026-10-15T10:02:37+00:00"}</script>')
        self.assertEqual(self.page(html_resp(page_html(extra=extra)))['published'],
                         '2026-10-15 18:02')

    def test_only_a_modified_time_still_beats_having_no_time(self):
        extra = '<meta property="article:modified_time" content="2026-10-16T19:17:00Z">'
        self.assertEqual(self.page(html_resp(page_html(extra=extra)))['published'],
                         '2026-10-17 03:17')

    def test_a_title_with_angle_brackets_survives(self):
        # bs4 已经把 &lt; 解码了:再按 HTML 解析一遍会把 <T> 整段吃掉。
        entry = self.page(html_resp(page_html(title='List&lt;T&gt; 的用法')))
        self.assertEqual(entry['title'], 'List<T> 的用法')

    def test_a_verification_gate_is_named_as_one(self):
        gate = ('https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=x'
                '&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Fabc')
        with self.assertRaises(grab.GrabError) as caught:
            self.page(html_resp('<html><body><p>环境异常</p></body></html>', url=gate))
        self.assertIn('验证页', str(caught.exception))
        self.assertEqual(self.items(), [])

    def test_a_page_without_a_body_is_not_stored(self):
        with self.assertRaises(grab.GrabError) as caught:
            self.page(html_resp('<html><body><p>太短了</p></body></html>'))
        self.assertIn('正文', str(caught.exception))
        self.assertEqual(self.items(), [])

    def test_a_pdf_behind_the_link_is_refused(self):
        resp = Response(200, PAGE, b'%PDF-1.4 1 0 obj << /Type /Catalog >>' + b'x' * 900,
                        {'Content-Type': 'application/pdf'})
        with self.assertRaises(grab.GrabError):
            self.page(resp)
        self.assertEqual(self.items(), [])


class SaveTests(GrabCase):
    def test_one_bucket_per_site_with_extraction_on_and_refetch_off(self):
        grab.save({'url': PAGE, 'title': 'A', 'body': '正文', 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        buckets = reg.subscriptions(grab.KIND)
        self.assertEqual([b['name'] for b in buckets], ['example.org'])
        self.assertEqual(buckets[0]['url'], 'https://example.org/')
        self.assertEqual(buckets[0]['extract'], 1)   # 手动挑的内容值得买一次提取
        # `fulltext` 必须是 0:那个开关会把条目扣在导出闸门外等「去原文补正文」,
        # 而正文此刻已经在手里了。
        self.assertEqual(buckets[0]['fulltext'], 0)
        self.assertEqual(buckets[0]['enabled'], 1)

    def test_the_same_site_reuses_its_bucket_and_x_gets_a_readable_name(self):
        for url in ('https://example.org/a', 'https://www.example.org/b'):
            grab.save({'url': url, 'title': 'A', 'body': '正文', 'author': 'x',
                       'published': '', 'content_kind': 'content'})
        grab.save({'url': TWEET, 'title': 'T', 'body': '推文', 'author': 'x',
                   'published': '', 'content_kind': 'post'})
        self.assertEqual(sorted(b['name'] for b in reg.subscriptions(grab.KIND)),
                         ['X / Twitter', 'example.org'])

    def test_storing_the_same_link_twice_changes_nothing(self):
        entry = {'url': PAGE, 'title': '原标题', 'body': '原正文', 'author': 'x',
                 'published': '', 'content_kind': 'content'}
        self.assertEqual(grab.save(entry)['added'], 1)
        again = grab.save({**entry, 'title': '新标题', 'body': '新正文'})
        # **0 不是失败,正是想要的末态**:库里那一份一个字节都没动。
        self.assertEqual(again['added'], 0)
        rows = self.items()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['title'], rows[0]['body']), ('原标题', '原正文'))

    def test_an_empty_body_never_reaches_the_insert(self):
        with self.assertRaises(grab.GrabError):
            grab.save({'url': PAGE, 'title': 'A', 'body': '', 'author': 'x',
                       'published': '', 'content_kind': 'content'})
        self.assertEqual(self.items(), [])

    def test_known_answers_across_sources(self):
        # 一条推文已经被「大V观点」收过了:再收一遍只会在另一棵树下多一篇一样的。
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        reg.put_items(social, [{'url': grab.normalize(TWEET), 'title': '旧标题',
                                'body': '正文', 'published': ''}])
        seen = grab.known(grab.normalize(TWEET))
        self.assertEqual((seen['kind'], seen['title']), ('social', '旧标题'))
        self.assertIsNone(grab.known('https://example.org/never-seen'))


class PipelineTests(GrabCase):
    def test_this_kind_is_registered_everywhere_that_derives_from_feed_types(self):
        from kb.collectors import collection_channels
        from kb.config import SOURCE_GROUPS, SOURCES
        self.assertIn(grab.KIND, feed_types())
        self.assertFalse(feed_types()[grab.KIND].pollable)
        self.assertIn(grab.KIND, SOURCE_GROUPS)
        self.assertIn(grab.KIND, [s.name for s in SOURCES])
        self.assertIn(grab.KIND, [c['id'] for c in collection_channels()])

    def test_the_crawl_stage_spins_idle_instead_of_fetching_a_bucket(self):
        grab.save({'url': PAGE, 'title': 'A', 'body': '正文', 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        # 收件桶是启用着的订阅。放过去就会拿站点首页去发一次 HTTP,而
        # `ADAPTERS` 里压根没有 `manual` 这个适配器。
        with patch.object(sync, 'fetch_source', side_effect=AssertionError('不该抓')) as spy:
            summary = sync.collect(grab.KIND, FetchScope())
        spy.assert_not_called()
        self.assertEqual((summary['sources'], summary['added']), (0, 0))

    def test_a_grabbed_link_lands_in_the_vault_as_one_article(self):
        # 整条链的后果:收下 → 导出 → vault 里那一篇 md 带着 inbox 的身份。
        grab.save({'url': PAGE, 'title': '标题', 'body': '正文内容', 'author': '某记者',
                   'published': '2026-10-15 18:02', 'content_kind': 'content'})
        sync.export(grab.KIND)
        files = list((self.root / 'vault' / 'source' / grab.KIND).glob('*.md'))
        self.assertEqual(len(files), 1)
        text = files[0].read_text()
        meta = yaml.safe_load(text.split('---')[1])
        self.assertEqual(meta['source'], grab.KIND)
        self.assertEqual(meta['url'], PAGE)
        self.assertEqual(meta['account'], '某记者')
        self.assertIn('正文内容', text)


if __name__ == '__main__':
    unittest.main()
