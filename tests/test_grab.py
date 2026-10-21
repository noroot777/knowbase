# -*- coding: utf-8 -*-
"""手动收录的**抓取**那一半:地址分流、推文形态、网页形态与关卡判据。

入库、端点和管道那一半在 `tests/test_grab_store.py`,夹具共用
`tests/grab_fixtures.py`。判据一律先断言后果(见夹具模块的说明)。
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from kb.feeds import grab, grab_page, readable, x_article
from kb.feeds.fetching import Response
from tests.grab_fixtures import (GrabCase, PAGE, TWEET, html_resp, page_html,
                                 tweet_payload)


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

    def test_another_site_is_never_rewritten_into_a_tweet(self):
        """**走真入口**(`normalize` 之后再 `classify`),不是直接调 `classify`。

        `canonical_url(social=True)` 那一档只看路径、不看域名:无条件开着它,
        这几条地址在分流**之前**就被改写成了 x.com,`classify` 的域白名单那时
        已经无从谈起 —— 后果是拿一个陌生站点的地址去问 FxTwitter,把另一条推文
        的内容收成「用户要的那一篇」。
        """
        for url in ('https://notx.com/example/status/12345',
                    'https://example.org/alice/status/12345',
                    'https://mastodon.social/@alice/status/12345'):
            with self.subTest(url=url):
                norm = grab.normalize(url)
                # 比的是 host 本身:`assertNotIn('x.com', …)` 对 notx.com 是假红。
                self.assertEqual(urlsplit(norm).hostname, urlsplit(url).hostname)
                self.assertEqual(grab.classify(norm), 'web')
        # 反过来钉一边:X 自己的域照旧收成那条规范永久链接。
        self.assertEqual(grab.normalize('https://mobile.twitter.com/Example/status/12345'),
                         'https://x.com/example/status/12345')

    def test_refusals_say_what_to_do_next(self):
        for url, word in [('https://x.com/example', '大V观点'),
                          ('https://x.com/example/status/12345/photo/1', '单条推文')]:
            with self.assertRaises(grab.GrabError) as caught:
                grab.classify(grab.normalize(url))
            self.assertIn(word, str(caught.exception))

    def test_a_wechat_link_is_not_refused_by_its_domain(self):
        """**反过来钉一边(R2-F3)。** 绿泡泡的文章和别的站点走同一条路:分流这一步
        不许按域名先拒 —— 那等于替对方的服务器回答,转载页、镜像、以及对方哪天
        放开都会被一句「这条路抓不到」挡在外面。真拒它的是**实际响应**
        (`PageTests` 里那两条)。"""
        for url in ('https://mp.weixin.qq.com/s/abc123', 'https://weixin.qq.com/s/abc'):
            with self.subTest(url=url):
                self.assertEqual(grab.classify(grab.normalize(url)), 'web')

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

    def test_the_structured_article_signal_is_consumed_not_inferred(self):
        """**回包里的 `tweet.article` 就是「这是一篇长文」的生产者签名。**

        `needs_article` 那三条判据全是间接信号(正文里写着文章地址 / 时间线上那张
        卡片 / 正文是空的)。长文推文的 `text` 完全可能是文章标题或一句介绍(上游
        自己的渲染器就按 `text === article.title` 处理),再配一张图连「空正文」
        那条也被挡住 —— 那一版连问一次都不会问,随后 `DO NOTHING` 把只有标题/只有
        图的正文永久钉成那一篇(2026-10-16 第 3 轮审查实测三种形态 0 次询问)。
        """
        article = {'title': '长文标题', 'content': {'blocks': [
            {'type': 'unstyled', 'text': '长文正文确实回来了'}]}}
        got = {'title': '长文标题', 'body': '长文正文确实回来了'}
        for tweet in ({'text': article['title']},
                      {'text': '今天写了一篇新文章'},
                      {'text': '', 'media': {'photos': [{'url': 'https://pbs.twimg.com/a.jpg'}]}}):
            with self.subTest(text=tweet['text']):
                with patch.object(x_article, 'fetch', return_value=got) as followup:
                    entry = self.grab_tweet(article=article, **tweet)
                self.assertEqual(followup.call_count, 1)     # 问了一次
                self.assertIn('长文正文确实回来了', entry['body'])
                # 那个内部信号键**不许进 feed_items**:`put_items` 把整份条目哈希成
                # `content_hash`,多一个键会让同一条推文从采集通道和手动收录两条路
                # 进来算出两个哈希,而先到的那一份被 `DO NOTHING` 钉死。
                self.assertNotIn(x_article.HINT, entry)
        # 反过来钉一边:没有这个签名的普通推文照旧一次都不问(别给一大半推文各买
        # 一次调用)。
        with patch.object(x_article, 'fetch', return_value=None) as quiet:
            entry = self.grab_tweet(text='随手记一笔')
        self.assertEqual((quiet.call_count, entry['body']), (0, '随手记一笔'))

    def test_a_confirmed_long_form_without_a_body_is_never_stored(self):
        """**确认是长文、正文又没拿到:一个字都不许写。** 落库之后 `DO NOTHING`
        之下再投一次同一条链接也换不回真正文。

        三种形态:接口说「没有长文」(和签名矛盾)、接口只给了标题、签名本身的形状
        读不懂(那也是在说「有长文」,按「没有」放过去就是放行一条残缺正文)。
        """
        empty = {'title': '只有标题', 'content': {'blocks': []}}
        for article, fetched in ((empty, None), (empty, None), (['坏形状'], None)):
            with self.subTest(article=str(article)[:24]):
                with patch.object(x_article, 'fetch', return_value=fetched):
                    with self.assertRaises(grab.GrabError) as caught:
                        self.grab_tweet(text='一句介绍', article=article)
                self.assertIn('长文', str(caught.exception))
                self.assertEqual(self.items(), [])           # 后果:库里一行都没有

    def test_the_article_in_hand_is_used_when_the_followup_fails(self):
        """接口那一跳没成,而**这一份回包自己就带着文章内容** —— 用它,别丢掉。

        渲染和拼接照旧只有 `x_article` 那一份口径(`from_payload` 就是 `fetch` 的
        尾段),所以同一篇长文从两条路进来长得一样。
        """
        article = {'title': '长文标题', 'content': {'blocks': [
            {'type': 'unstyled', 'text': '回包里自带的正文'}]}}
        with patch.object(x_article, 'fetch',
                          side_effect=x_article.ArticleUnavailable('长文接口没答复:超时')):
            entry = self.grab_tweet(text='今天写了一篇新文章', article=article)
        self.assertIn('回包里自带的正文', entry['body'])
        self.assertEqual(entry['title'], '长文标题')

    def test_photo_only_tweet_is_stored_as_its_media(self):
        entry = self.grab_tweet(text='', media={'photos': [{'url': 'https://pbs.twimg.com/a.jpg'}]})
        self.assertIn('https://pbs.twimg.com/a.jpg', entry['body'])
        self.assertEqual(entry['title'], '某某 的动态')

    def test_a_malformed_media_container_does_not_take_the_tweet_down(self):
        # 第三方回包里 `photos` 是 dict / 数字时,切片要么切出一堆键名要么
        # TypeError —— 后者会把一条文字完好的推文带成 500。
        for media in ({'photos': {'url': 'x'}}, {'photos': 7, 'videos': 'no'},
                      {'videos': {'url': 'x'}}):
            with self.subTest(media=media):
                entry = self.grab_tweet(text='文字还在', media=media)
                self.assertEqual(entry['body'], '文字还在')
        # 反过来钉一边:形状对的照旧收进来。
        entry = self.grab_tweet(text='', media={'photos': [{'url': 'https://pbs.twimg.com/a.jpg'}]})
        self.assertIn('a.jpg', entry['body'])

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
    def page(self, resp: Response, url: str = PAGE):
        with patch.object(grab, 'request', return_value=resp):
            return grab.grab(url)

    def save_page(self, resp: Response, url: str = PAGE):
        """走**真入口**:抓完就存。关卡那几档要钉的是「库里确实没有这一条」——
        只验它抛了异常的话,`DO NOTHING` 之下那条空壳正文永久改不回来。"""
        with patch.object(grab, 'request', return_value=resp):
            return grab.save(grab.grab(url))

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

    def test_a_wechat_gate_is_refused_and_points_at_its_own_channel(self):
        """①路由那一道,外加 R2-F3 的下一步。

        实测形态(2026-10-16):`mp.weixin.qq.com/s/<slug>` 302 到
        `/mp/wappoc_appmsgcaptcha`,18 KB 的验证页、og 和 `#js_content` 全无、
        提取 0 字。这一档要拒,而且下一步要指向「公众号」那一类 —— 照通用那句
        「在浏览器里打开、把正文复制出来」说,等于让人对着一道过不去的验证页发呆。

        **这一档现在是少数情况,但判据一个字都不许松**:2026-10-21 复测证明那道
        关卡是自报爬虫身份招来的(见 `test_the_web_path_asks_like_a_browser`),
        换身份之后同一条地址直接给整篇文章。对方哪天收紧风控它就回来,而漏一次
        就是把验证页永久钉成这一篇的正文。
        """
        gate = ('https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=x'
                '&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Fabc')
        with self.assertRaises(grab.GrabError) as caught:
            self.save_page(html_resp('<html><body><p>环境异常</p></body></html>', url=gate),
                           url='https://mp.weixin.qq.com/s/abc')
        self.assertIn('验证页', str(caught.exception))
        self.assertIn('公众号', str(caught.exception))
        self.assertEqual(self.items(), [])
        # 反过来钉一边:别人家的页面给的是浏览器那条路,不是公众号那一类。
        with self.assertRaises(grab.GrabError) as other:
            self.save_page(html_resp('<html><body><p>环境异常</p></body></html>',
                                     url='https://example.org/cdn-cgi/l/chk_jschl?x=1'))
        self.assertNotIn('公众号', str(other.exception))
        self.assertIn('浏览器', str(other.exception))

    def test_a_wechat_article_that_reads_is_stored_like_any_other_page(self):
        """**反过来钉一边(R2-F3)。** 真能读到正文的那一篇照旧收下,桶名是它自己
        那个 —— 按域名先拒的那一版里,这一条连 HTTP 都发不出去。"""
        wx = 'https://mp.weixin.qq.com/s/abc123'
        saved = self.save_page(html_resp(page_html(title='一篇公众号文章'), url=wx), url=wx)
        self.assertEqual(saved['added'], 1)
        self.assertEqual(saved['source']['name'], '绿泡泡公众号')
        self.assertIn('这是正文第一段', self.items()[0]['body'])

    def test_the_web_path_asks_like_a_browser(self):
        """**不自报爬虫身份这一条是承重的。**

        实测(2026-10-21,`mp.weixin.qq.com/s/XQq1iCBn1cUhR3SLOOKBMA`,其余请求头
        一字不改、两边各 3 次):自报 `KnowledgeBaseFeedReader/1.0` 3/3 被 302 到
        `/mp/wappoc_appmsgcaptcha`、拿到 18 KB 的验证页;换成浏览器 UA 3/3 直接
        200 在原地址上、拿到 3.74 MB 的文章页。**那道验证页是自报身份招来的,
        不是这个地址本身挡着** —— 这个键掉了,用户看到的是一句「对方把这个地址挡
        在了一道验证页后面」,而文章本身好好的,他也没有别的出路。

        判据落在**真发出去的那份请求头**上:`grab_page.HEADERS` 里有这个键不算数,
        `accept()` 要把它带过去、`request` 的位置参数要接得住。
        """
        sent = {}

        def capture(url, headers=None, **kwargs):
            sent.update(headers or {})
            return html_resp(page_html())

        with patch.object(grab, 'request', side_effect=capture):
            grab.grab(PAGE)
        self.assertIn('Chrome/', sent.get('User-Agent', ''))
        self.assertNotIn('KnowledgeBaseFeedReader', sent.get('User-Agent', ''))
        # 反过来钉一边:换身份没把别的请求头挤掉(少了 Accept 有些站回 XML 或 406)。
        self.assertIn('text/html', sent.get('Accept', ''))
        self.assertIn('zh-CN', sent.get('Accept-Language', ''))

    def test_a_page_padded_with_inline_script_is_still_read(self):
        """**事故形态(2026-10-21 实测)。** 绿泡泡的文章页 3.74 MB,其中 3.57 MB
        是内联脚本,真标记只有 0.17 MB。`extract_or_reason` 按**整份长度**卡
        `MAX_HTML`,于是在解析之前就交白卷 —— 手动收录对着一篇完完整整读得到的
        文章说「这一页没找到正文」,而它和真正读不到的那一档(登录墙、纯 JS 渲染)
        在界面上长得一模一样。`_strip` 第一件事就是把脚本整块扔掉,这笔钱根本不会花。
        """
        page = page_html(extra='<script>var x = "%s";</script>' % ('a' * readable.MAX_HTML))
        self.assertGreater(len(page), readable.MAX_HTML)          # 整份确实超线
        self.assertLess(readable.markup_size(page), readable.MAX_HTML)  # 真标记没有
        self.assertIn('这是正文第一段', self.page(html_resp(page))['body'])

    def test_a_page_whose_real_markup_is_too_big_is_still_refused(self):
        """**反过来钉一边。** 那条线量的是「要建成树的那部分」,不是「扣掉脚本就
        放行」:真标记自己超线的照旧交白卷,而 `<script>x</script>` 重复几十万次的
        那种页面(内容短、节点多)也不许从底下溜过去 —— 替换保留两头的标签正是
        为它留的。
        """
        self.assertEqual('', readable.extract(page_html() + '<p>x</p>' * 400000))
        self.assertEqual('', readable.extract(page_html() + '<script>x</script>' * 200000))

    def test_a_gate_component_past_the_old_truncation_point_is_still_caught(self):
        """**漏一道关卡就是把一道墙当正文永久钉进库里**(`put_items` 的
        `DO NOTHING`),代价和误拒差着一个量级。

        `gate_reason` 原先把 HTML 截到 `MAX_HTML` 再解析,于是组件和登录墙那两道
        在一张内联脚本打头的页面上只看得到开头那一截。上一条让这种页面**反而**
        交得出正文了,这道截断跟着从「反正也存不进去」变成真的放行口。
        """
        padding = '<script>var x = "%s";</script>' % ('a' * readable.MAX_HTML)
        orchestrate = ('<script src="https://t.test/cdn-cgi/challenge-platform'
                       '/h/b/orchestrate/chl_page/v1"></script>')
        page = page_html(extra=padding + orchestrate)
        # 先钉住这道关卡确实落在旧截断点之后(否则这条用例测不到那个洞)
        self.assertGreater(page.index('orchestrate'), readable.MAX_HTML)
        with self.assertRaises(grab.GrabError) as caught:
            self.save_page(html_resp(page))
        self.assertIn('验证', str(caught.exception))
        self.assertEqual(self.items(), [])

    def test_a_long_gate_page_on_an_ordinary_address_is_refused(self):
        """**上一轮漏掉的那一张**(第二轮审查实测 `SAVED added=1`)。

        1782 字、地址干净(`/article`)、提取器轻松交出一段"正文" —— 按「超过
        1200 字就放行」那一版它被当成正文落了库,而 `put_items` 是 `DO NOTHING`:
        再投一次同一条链接也换不回真文章。判据因此改成**页面身份**:标题自己就是
        一句祈使的关卡提示,而正文通篇就是那几句话。
        """
        gate = page_html(title='请完成验证后继续', body=[
            '请完成验证。Checking your browser before accessing the site. '
            '您需要启用 JavaScript 和 Cookie 后重试。在通过验证前文章内容不可见。' * 18])
        # 先钉住它确实落在旧判据的放行区里(否则这条用例测不到那个洞)
        self.assertGreater(len(grab_page.page_body(gate)), 1200)
        with self.assertRaises(grab.GrabError) as caught:
            self.save_page(html_resp(gate, url='https://example.org/article'))
        self.assertIn('验证', str(caught.exception))
        self.assertEqual(self.items(), [])

    def test_ordinary_addresses_that_sound_like_gates_are_still_stored(self):
        """**反过来钉一边(R2-F1 的另一头)。** 关卡路由只收各家关卡自己的那几条
        串,`challenge` / `checkpoint` / `verify` 这类通用英文词一个都不许进去 ——
        它们正是正常文章路径里的词。第二轮审查实测:一篇 650 字的正经文章(标题
        「挑战：一次研究记录」、地址 `/blog/challenge/`)被「整段相等」那一版拒掉,
        而这一道在成功路径上也生效:拒掉就是拒掉,再点一次也换不回来。
        """
        for url in ('https://example.org/blog/challenge/',
                    'https://example.org/checkpoint',
                    'https://example.org/waf/reference/cloudflare-challenges/',
                    'https://example.org/posts/verify-your-email'):
            with self.subTest(url=url):
                entry = self.page(html_resp(page_html(title='挑战：一次研究记录'), url=url))
                self.assertIn('这是正文第一段', entry['body'])
        # 反过来:各家关卡自己的路由照旧拒掉(①,path 和 query 都算)。
        for gate in ('https://example.org/cdn-cgi/l/chk_jschl?x=1',
                     'https://example.org/sorry/index?continue=x',
                     'https://example.org/a?__cf_chl_tk=abc'):
            with self.subTest(gate=gate):
                with self.assertRaises(grab.GrabError):
                    self.page(html_resp(page_html(), url=gate))

    def test_the_gate_component_counts_but_a_comment_captcha_does_not(self):
        """②页面挂着的那套关卡组件 —— 一篇讲 Cloudflare 的文章不会把关卡编排
        脚本挂在自己页上,所以它是页面身份。

        **反过来钉一边(两条)**:通用验证码小组件(正常文章页的评论框就挂着
        它们)和 Cloudflare 在普通页面上也会注入的那个检测脚本都不算 —— 进表
        等于按「这站评论要过验证码」拒稿。
        """
        chl = '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>'
        with self.assertRaises(grab.GrabError):
            self.page(html_resp(page_html(extra=chl)))
        for innocent in ('<script src="https://www.google.com/recaptcha/api.js"></script>',
                         '<script src="https://hcaptcha.com/1/api.js"></script>',
                         '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'):
            with self.subTest(innocent=innocent[:48]):
                entry = self.page(html_resp(page_html(extra=innocent)))
                self.assertIn('这是正文第一段', entry['body'])

    def test_a_tutorial_that_quotes_gate_markup_is_still_stored(self):
        """**反过来钉一边:页面上的文字不是页面身份。**

        一篇讲关卡的教程会把那行 `<script src="/cdn-cgi/challenge-platform/…">`
        原样转义印在 `<pre>` 里。按整份 HTML 找子串的那一版把它当成「这一页挂着
        关卡组件」拒掉了(2026-10-16 第 3 轮审查实测:731 字的正经教程,既没有生效
        脚本也没有验证动作)—— 而这一道在成功路径上也生效,拒掉就是这一篇再也收
        不进来。所以判据是 DOM 里真实的那几个属性。
        """
        code = ('<pre>&lt;script src="/cdn-cgi/challenge-platform/h/g/orchestrate/'
                'chl_page/v1"&gt;&lt;/script&gt;</pre>')
        html = page_html(title='Cloudflare 接入教程').replace('</article>', code + '</article>')
        entry = self.page(html_resp(html, url='https://example.org/posts/guide'))
        self.assertIn('这是正文第一段', entry['body'])
        # 反过来:真挂着那个编排脚本的页面照旧拒掉(②这一道还在)。
        real = page_html(extra='<script src="/cdn-cgi/challenge-platform/h/b/'
                               'orchestrate/chl_page/v1"></script>')
        with self.assertRaises(grab.GrabError):
            self.page(html_resp(real))

    def test_a_login_wall_is_refused_even_though_it_extracts_a_lot(self):
        """**登录提示页不许被当成文章落库 —— 哪怕它一个表单都没有。**

        这是三轮都栽在同一处的那个形状(2026-10-16 审查实测):一张
        「Sign in to continue reading / subscribe to unlock this story」的提示页,
        提取器交出 **1832 字** —— 份额判据对它不成立(那一页话很多)、措辞不在
        关卡表里。**原始证据里它连 `<form>` 都没有**,所以「密码框」这类结构判据
        也抓不住它;上一版拿「补一个密码表单」的变形当用例,守住的是那个变形,
        不是这份证据。

        分得开它的是**内容自己**:整页就是同一段铺了 7 遍,按行去重只剩 260 字 ——
        连提取器那道 400 的门槛都过不了(`readable.substance`)。这一道不认措辞,
        所以墙前面那段话每家怎么写都不影响结论。
        """
        said = ('Sign in to continue reading. Enter the email address associated with your '
                'subscription. Members have access to the full archive and may manage their '
                'account and password. New readers can subscribe to unlock this story. ')
        bare = page_html(title='Sign in to read Example Journal', body=[said] * 7)
        self.assertGreater(len(grab_page.page_body(bare)), 1500)   # 提取器交得出一大段
        self.assertLess(readable.substance(grab_page.page_body(bare)), readable.MIN_CHARS)
        # 三种形态同一句话:原始那张(一个表单都没有)、补一个邮箱表单的、补一个
        # 密码表单的。前两档靠内容质量那一道,第三档靠结构那一道 —— 两道都要在。
        for form in ('',
                     '<form action="/login"><input type="email"><button>Continue</button></form>',
                     '<form action="/login"><input type="password"></form>'):
            with self.subTest(form=form[:34] or '(一个表单都没有)'):
                html = bare.replace('</article>', form + '</article>')
                with self.assertRaises(grab.GrabError) as caught:
                    self.save_page(html_resp(html))
                self.assertRegex(str(caught.exception), '登录|正文')
                self.assertEqual(self.items(), [])
        # 付费墙自己的那张标准声明(schema.org)同样算一道墙。
        paid = page_html(extra='<script type="application/ld+json">'
                               '{"@type":"Article","isAccessibleForFree":false}</script>')
        with self.assertRaises(grab.GrabError):
            self.page(html_resp(paid))

    def test_a_page_that_only_repeats_one_paragraph_is_never_stored(self):
        """**反过来钉一边:说了足够多不重复的东西才算确认得了。**

        第六道判据自己的两侧 —— 同一段复读的拒掉、两段不同的照旧收下。
        **这一道不许回到原始长度**:复读那张比正常文章长得多。
        """
        said = ('这一段会被整页复读，它自己的长度足够让整页过得了提取器那道门槛，'
                '可整页加起来也只说了这一件事。')
        once = page_html(title='复读页', body=[said] * 15)
        with self.assertRaises(grab.GrabError) as caught:
            self.save_page(html_resp(once))
        self.assertIn('复读', str(caught.exception))
        self.assertEqual(self.items(), [])
        self.assertGreater(len(grab_page.page_body(once)),
                           len(grab_page.page_body(page_html())))   # 它更长,却该被拒
        entry = self.page(html_resp(page_html()))                   # 两段不同的照旧收下
        self.assertIn('这是正文第一段', entry['body'])

    def test_an_inert_fragment_is_not_this_page_identity(self):
        """**DOM 里有某个片段 ≠ 它在阻挡正文。** 两种「还没生效」的形状各一例
        (2026-10-16 审查实测:两篇 650 字的正经教程都被拒了)。

        ①`<template>` 里的登录表单:那是给 JS 以后克隆用的样板,浏览器既不渲染
        也不提交(「手把手教你写登录表单」的教程就这么写);②
        `<script type="application/json">` 里的示例参数:数据块不会被执行,
        里面出现 `_cf_chl_opt` 只是一段**内容**;③**同一道判据的另一端**:带
        `src` 的脚本也要看那个 `type` —— 不执行的类型哪怕 `src` 指着某家关卡的
        地址,它一样不是这一页的身份(`_live` 把两端并成一处口径,原先只有内联
        那一支核对了类型;第 3 轮审查点的正是这处对称缺口)。
        """
        tmpl = page_html(title='模板编程教程').replace(
            '</article>', '<template id="demo"><form action="/login">'
                          '<input type="password"></form></template></article>')
        self.assertIn('这是正文第一段', self.page(html_resp(tmpl))['body'])
        data = page_html(title='参数解析教程',
                         extra='<script type="application/json">{"sample":"_cf_chl_opt"}</script>')
        self.assertIn('这是正文第一段', self.page(html_resp(data))['body'])
        src = page_html(title='关卡脚本是怎么装上的', extra=(
            '<script type="application/json" '
            'src="https://cdn.example.org/cdn-cgi/challenge-platform/h/b/demo.json">'
            '</script>'))
        self.assertIn('这是正文第一段', self.page(html_resp(src))['body'])
        # 反过来钉一边:真挂在那儿、会被执行的那两种照旧拒掉(内联 + 带 src)。
        live = page_html(extra='<script>var o = {"k":"_cf_chl_opt"};</script>')
        with self.assertRaises(grab.GrabError):
            self.page(html_resp(live))
        hooked = page_html(extra=(
            '<script src="https://cdn.example.org/cdn-cgi/challenge-platform/h/b/x.js">'
            '</script>'))
        with self.assertRaises(grab.GrabError):
            self.page(html_resp(hooked))

    def test_shared_gate_components_do_not_store_real_gates_or_refuse_inert_examples(self):
        for extra in [
                '<script src="/CDN-CGI/Challenge-Platform/h/flow"></script>',
                '<script type="TEXT/JAVASCRIPT">window._CF_CHL_OPT = {};</script>',
                '<form action="/CDN-CGI/CHALLENGE-PLATFORM/H/flow"></form>']:
            with self.subTest(extra=extra), self.assertRaises(grab.GrabError):
                self.save_page(html_resp(page_html(extra=extra)))
            self.assertEqual([], self.items())
        for index, extra in enumerate([
                '<script type="application/json">{"endpoint":"/cdn-cgi/challenge-platform/h/b/"}'
                '</script>',
                '<script type="application/json" src="/cdn-cgi/challenge-platform/h/flow"></script>',
                '<script src="" type="module">window._CF_CHL_OPT = {};</script>',
                '<template><script src="/cdn-cgi/challenge-platform/h/flow"></script></template>']):
            with self.subTest(extra=extra):
                try:
                    self.save_page(html_resp(page_html(extra=extra)), PAGE + f'?example={index}')
                except grab.GrabError as exc:
                    self.fail(f'inert example incorrectly refused: {exc}')
                self.assertEqual(index + 1, len(self.items()))
                self.assertTrue(all('这是正文第一段' in item['body'] for item in self.items()))

    def test_a_login_box_in_the_page_furniture_is_not_a_wall(self):
        """**反过来钉一边。** 页眉里常驻一个登录框的站点多得是 —— 那不是拦在正文
        上的墙。位置判据借正文提取自己那一份(`readable.in_furniture`),不另写一张
        噪声名单:两处各写一份的话,一处认得出 `siteFooter`、另一处认不出。
        """
        form = ('<form action="/login"><input type="email">'
                '<input type="password"></form>')
        for furniture in (f'<header>{form}</header>',
                          f'<nav>{form}</nav>',
                          f'<div class="site-footer">{form}</div>',
                          f'<div class="sidebar">{form}</div>'):
            with self.subTest(furniture=furniture[:28]):
                html = page_html().replace('<nav>导航 首页 关于</nav>', furniture)
                entry = self.page(html_resp(html))
                self.assertIn('这是正文第一段', entry['body'])

    def test_a_gate_title_needs_the_body_to_agree(self):
        """③标题是一句关卡提示 —— 单独不算数,要配上正文里也在说同一件事。

        **反过来钉一边**:同一份正文,标题换成一个**话题词**式的标题就该收下。
        不然这一道就从「拦住关卡」变成「拦住一类话题」。
        """
        body = ['请完成验证后才能看到这篇文章。',
                '这里是一段足够长的正文，讲的是别的事情，长度要过得了提取器那道门槛。' * 14]
        self.assertLess(grab_page._gate_share(grab_page.page_body(
            page_html(title='x', body=body))), grab_page._GATE_SHARE)   # ④ 这一道不成立
        with self.assertRaises(grab.GrabError) as caught:
            self.page(html_resp(page_html(title='Just a moment…', body=body)))
        self.assertIn('验证', str(caught.exception))
        entry = self.page(html_resp(page_html(title='人机验证是怎么坏掉的', body=body)))
        self.assertIn('这里是一段足够长的正文', entry['body'])

    def test_a_page_that_is_all_gate_lines_is_refused_whatever_its_title(self):
        """④通篇就是那几句提示:**按份额算、不设字数上限** —— 上限就是上一轮
        那个洞(页面长一点就一路放行)。标题人畜无害时这一道单独成立。"""
        say = '请完成验证之后才能继续访问这篇文章的正文内容，这是站点的防护要求。'
        html = page_html(title='站点公告', body=[say * 16 + '本页由系统生成。' * 4])
        self.assertGreaterEqual(grab_page._gate_share(grab_page.page_body(html)),
                                grab_page._GATE_SHARE)
        with self.assertRaises(grab.GrabError) as caught:
            self.save_page(html_resp(html))
        self.assertIn('验证', str(caught.exception))
        self.assertEqual(self.items(), [])

    def test_an_inline_gate_that_extracts_a_few_lines_is_refused(self):
        """没有重定向、就地渲染几句「请完成验证」的那种:路由那一道看不见它,
        而提取器可能刚好交得出一段 400 字出头的文字。"""
        gate = page_html(title='Just a moment…', body=[
            '请完成验证后继续。Checking your browser before accessing the site. ' * 9])
        with self.assertRaises(grab.GrabError) as caught:
            self.page(html_resp(gate))
        self.assertIn('验证', str(caught.exception))
        self.assertEqual(self.items(), [])

    def test_an_article_that_merely_talks_about_captchas_is_still_stored(self):
        """**反过来钉一边。** 一篇正经讲验证码的长文章照旧收得下。

        它的命中句占比是 **50%**(实测),刻意贴在 `_GATE_SHARE` 下方一侧:有人
        把那条线调到 0.5 这一条就红。真实样本离线更远 —— developers.cloudflare.com
        那篇 1277 字的文档是 0%。
        """
        entry = self.page(html_resp(page_html(title='聊聊 captcha 的历史', body=[
            '这一段在正经讨论 captcha 与 verify you are human 这类拦截页的来历。' * 14,
            '第二段继续讲它们对可访问性的影响，篇幅要够长。' * 14])))
        self.assertIn('captcha', entry['title'])
        self.assertIn('captcha', entry['body'])

    def test_a_page_without_a_body_is_not_stored(self):
        with self.assertRaises(grab.GrabError) as caught:
            self.page(html_resp('<html><body><p>太短了</p></body></html>'))
        self.assertIn('正文', str(caught.exception))
        self.assertEqual(self.items(), [])

    def test_a_structured_author_beats_the_site_name(self):
        # 同时给了 og:site_name 和 Article JSON-LD 作者的页面很常见:站点名抢在
        # 前面就等于把已知的真实作者丢掉。
        extra = ('<meta property="og:site_name" content="某站">'
                 '<script type="application/ld+json">'
                 '{"@type":"Article","author":{"name":"某记者"}}</script>')
        self.assertEqual(self.page(html_resp(page_html(extra=extra)))['author'], '某记者')
        # 反过来钉一边:实在没有人名时,站点名比空着强。
        site = '<meta property="og:site_name" content="某站">'
        self.assertEqual(self.page(html_resp(page_html(extra=site)))['author'], '某站')

    def test_an_impossible_date_is_left_empty(self):
        # 形状对不等于是个日期。假时刻会一路进列表排序和日报的时间窗口。
        for bad in ('2026-99-99', '2026-02-30T10:00', '2026-10-15T10:77'):
            with self.subTest(bad=bad):
                extra = f'<meta property="article:published_time" content="{bad}">'
                self.assertEqual(self.page(html_resp(page_html(extra=extra)))['published'], '')

    def test_a_date_at_the_edge_of_time_does_not_take_the_grab_down(self):
        """`9999-12-31T23:30:00Z` 本身是个合法日期,换算到上海(+8)就出了
        `datetime` 的头 —— 一个**补充字段**不许把整条收录带掉(承重的是正文,
        而「没有发布时间」是一句真话)。"""
        extra = '<meta property="article:published_time" content="9999-12-31T23:30:00Z">'
        entry = self.page(html_resp(page_html(extra=extra)))
        self.assertEqual(entry['published'], '')
        self.assertIn('这是正文第一段', entry['body'])

    def test_an_ipv6_site_and_a_very_long_host_still_get_a_bucket(self):
        """抓完了才发现存不下去 = 一个没有原因的 500(端点只翻 `GrabError`)。"""
        # 文档用的 2001:db8:: 过不了公开地址那道闸,所以用一个真·全局地址。
        long_host = 'a' * 60 + '.' + 'b' * 60 + '.example.org'
        for url in (f'https://{long_host}/post', 'https://[2606:4700:4700::1111]/post'):
            with self.subTest(url=url):
                with patch.object(grab, 'request', return_value=html_resp(page_html(), url=url)):
                    entry = grab.grab(url)
                source = grab.save(entry)['source']
                self.assertLessEqual(len(source['name']), 120)
                self.assertTrue(source['url'].startswith('https://'))

    def test_a_pdf_behind_the_link_is_refused(self):
        resp = Response(200, PAGE, b'%PDF-1.4 1 0 obj << /Type /Catalog >>' + b'x' * 900,
                        {'Content-Type': 'application/pdf'})
        with self.assertRaises(grab.GrabError):
            self.page(resp)
        self.assertEqual(self.items(), [])

if __name__ == '__main__':
    unittest.main()
