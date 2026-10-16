# -*- coding: utf-8 -*-
"""手动收录:给一条链接,内容抓进知识库(kb/feeds/grab.py + grab_page.py)。

判据一律**先断言后果**:抓不成的那几档都要钉「库里确实没有这一条」,而不是只验
它抛了异常 —— 空正文一旦落库,`put_items` 的 `DO NOTHING` 之下永久改不回来,那
正是这一块唯一不可恢复的失败形态。反过来也钉一边:该收的照旧收得下、重复投递
不改动库里那一份。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml

from kb import config, maintenance, pipeline, pipeline_state
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
        # `config.DB_PATH` 是 `grab._indexed` 要只读打开的那个库:不指到 tmp 的话
        # 「这一篇索引进去了没有」会去问**生产那一个** kb.db —— 答案跟着本机内容
        # 变,而且这组用例从此不可重放。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(config, 'DB_PATH', self.root / 'kb.db'),
                  # `_indexed` 要读 vault 里那一篇算 hash:`config.VAULT_ROOT` 和
                  # 导出用的 `sync.VAULT_ROOT` 各打一次(生产里是同一个值)。
                  patch.object(config, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault')):
            p.start()
            self.addCleanup(p.stop)
        self.state = deepcopy(pipeline_state._state)
        pipeline_state._state.update(running=False, stop_requested=False)
        self.addCleanup(lambda: pipeline_state._state.update(self.state))

    def items(self) -> list[dict]:
        with reg.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM feed_items ORDER BY key')]

    def export_vault(self, path: str, text: str = '# A\n\n正文') -> None:
        """假装导出阶段把 vault 里那一篇写了出来(只写文件,不碰索引)。"""
        full = config.VAULT_ROOT / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding='utf-8')

    def index_doc(self, path: str, text: str = '# A\n\n正文') -> None:
        """假装导出 + 索引都走完了:vault 里有这一篇,`documents` 记着**它当前
        这一版**的 `content_hash`。

        **两边一起摆才试得出东西。** `grab._indexed` 问的是版本(口径借
        `scanner.file_hash` + `store.doc_needs_reindex`),只往 `documents` 插一行
        路径的话,「库里还是上一版」那档漏报在用例里压根显不出来 —— 第二轮审查
        逮到的正是这个形态。`store.connect()` 这条路要加载 sqlite_vec、跑一串
        迁移(还要维护锁),为了回答「搜得到吗」不该付这笔钱。
        """
        from kb.scanner import file_hash

        self.export_vault(path, text)
        conn = sqlite3.connect(self.root / 'kb.db')
        conn.execute('CREATE TABLE IF NOT EXISTS documents'
                     ' (path TEXT PRIMARY KEY, content_hash TEXT)')
        conn.execute('INSERT INTO documents (path,content_hash) VALUES (?,?)'
                     ' ON CONFLICT(path) DO UPDATE SET content_hash=excluded.content_hash',
                     (path, file_hash(text)))
        conn.commit()
        conn.close()

    def mark_exported(self, url: str, path: str) -> None:
        """假装导出阶段写过 vault 里那一篇(`sync._export` 落的就是这两列)。"""
        with reg.db() as c:
            c.execute('UPDATE feed_items SET path=?,exported_hash=content_hash'
                      ' WHERE url=?', (path, url))

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

    def test_known_separates_staged_from_searchable(self):
        """**「feed_items 里有这一行」不等于「知识库里搜得到」。**

        导出没跑(占不到管道、服务刚重启)、或者索引自己失败时,这一条在库里但
        搜不到。两件事混成一句「已经在知识库里了」就是谎报:用户搜不到,而再投
        一次只会再得到同一句话,后半条链永远没人补。
        """
        grab.save({'url': PAGE, 'title': 'A', 'body': '正文', 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.assertFalse(grab.known(PAGE)['done'])          # 刚入库:还没导出
        self.mark_exported(PAGE, 'source/inbox/abc.md')
        self.assertFalse(grab.known(PAGE)['done'])          # 导出了,索引还没有
        self.index_doc('source/inbox/abc.md')
        self.assertTrue(grab.known(PAGE)['done'])           # 这才是走完整条链
        # 索引里是**另一篇**时不算(路径要对上,不是「库里有东西」就算)
        self.mark_exported(PAGE, 'source/inbox/other.md')
        self.assertFalse(grab.known(PAGE)['done'])

    def test_done_follows_the_current_version_not_a_past_one(self):
        """**「以前走完过」不算完成事实。**

        正文补抓回来、一轮翻译回写,都会把 `feed_items.content_hash` 推新 ——
        那时 vault 里还是上一版、索引里也是上一版。按「`documents` 里有这条路径」
        答的话这一条被当成走完了:面板说「已经在知识库里了」,而用户搜到的是旧
        内容,后半条链再也没人补(2026-10-16 第二轮审查)。判据因此是**版本**:
        导出的是现在这一版,而且 vault 里当前那一份进了索引。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'A', 'body': '正文', 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        self.assertTrue(grab.known(PAGE)['done'])
        # ① 库里那一行换了版(补抓回来的新正文),vault 和索引都还是上一版
        with reg.db() as c:
            c.execute("UPDATE feed_items SET content_hash='v2' WHERE url=?", (PAGE,))
        self.assertFalse(grab.known(PAGE)['done'])
        # ② 导出跟上了:vault 里已是新正文,而索引里记着的还是上一版的 hash
        self.export_vault(path, '# A\n\n新正文')
        self.mark_exported(PAGE, path)
        self.assertFalse(grab.known(PAGE)['done'])
        # ③ 索引也跟上了,这才算走完
        self.index_doc(path, '# A\n\n新正文')
        self.assertTrue(grab.known(PAGE)['done'])
        # ④ vault 里那一篇没了(清理脚本删过/导出其实没落):照旧不算走完
        (config.VAULT_ROOT / path).unlink()
        self.assertFalse(grab.known(PAGE)['done'])

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


class EndpointTests(GrabCase):
    """收录端点:**重复投递时按真实进度回答**,别把暂存说成已完成。"""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from kb.server import app
        self.client = self.enterContext(TestClient(app))
        self.runs: list[list[str]] = []

    def pipeline_patch(self, started=True, reason='其他同步正在运行'):
        """**打桩也要还原那条契约**:`start_pipeline` 返回的是**这一轮的 id**,
        不是 True —— 前端要拿它核对那一轮 export/index 的结果。回 True 的打桩会
        让「端点把 run 原样交出去」这件事测不出来。"""
        def fake(stages, refused=None, crawl_filter=None, **kwargs):
            self.runs.append((list(stages), (crawl_filter or {}).get('channel')))
            if not started and refused is not None:
                refused.append(reason)
            return 'run-7' if started else ''
        return patch.object(pipeline, 'start_pipeline', fake)

    def post(self, url=PAGE):
        return self.client.post('/api/grab', json={'url': url})

    def test_a_staged_duplicate_gets_the_rest_of_the_chain_run_again(self):
        grab.save({'url': PAGE, 'title': 'A', 'body': '正文', 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        with self.pipeline_patch():
            r = self.post()
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual((data['duplicate'], data['done']), (True, False))
        self.assertEqual(data['pipeline'], 'started')
        # **那一轮的身份要原样交给前端**:没有它,面板只能按「run_id 变了」猜 ——
        # 别人插在中间的一轮、以及我们这一轮红了,都会被写成「已写入知识库」。
        self.assertEqual(data['run'], 'run-7')
        self.assertIn('还没写进知识库', data['detail'])
        # **后果断言**:后半条链真被跑起来了 —— 只改一句文案的话这里红。
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_a_finished_duplicate_is_idempotent_and_costs_no_run(self):
        grab.save({'url': PAGE, 'title': 'A', 'body': '正文', 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, 'source/inbox/abc.md')
        self.index_doc('source/inbox/abc.md')
        with self.pipeline_patch():
            data = self.post().json()
        self.assertEqual((data['duplicate'], data['done']), (True, True))
        self.assertIn('已经在知识库里了', data['detail'])
        self.assertNotIn('pipeline', data)
        # 绝大多数重复投递都落在这一档:不许顺手占一轮管道。
        self.assertEqual(self.runs, [])

    def test_a_cross_source_item_is_written_through_under_its_own_source(self):
        """一条推文已经被「大V观点」收过、但还没导出:起一轮 `inbox` 的 export
        一个字都不会动它(export 按通道筛),而回答里却写着「正在写入知识库」。"""
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        reg.put_items(social, [{'url': grab.normalize(TWEET), 'title': '旧标题',
                                'body': '正文', 'published': ''}])
        with self.pipeline_patch():
            data = self.post(TWEET).json()
        self.assertEqual((data['duplicate'], data['done'], data['kind']),
                         (True, False, 'social'))
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'social')])
        self.assertIn('大V观点', data['detail'])

    def test_a_busy_pipeline_names_the_button_of_the_owning_source(self):
        """顺延那一句里的**按钮名按归属来源给**:来源页工具栏那颗按钮在可轮询的
        来源里写「同步全部」,只有手动收录写「写入知识库」(判据就是 `pollable`,
        和 `sources.js` 的 `manual` 分支同一个)。一条推文被「大V观点」收过、这一轮
        又没占到管道时,照手动收录那套说就是指着一颗那一页上不存在的按钮。"""
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        reg.put_items(social, [{'url': grab.normalize(TWEET), 'title': '旧标题',
                                'body': '正文', 'published': ''}])
        with self.pipeline_patch(started=False):
            data = self.post(TWEET).json()
        self.assertEqual(data['pipeline'], 'busy')
        self.assertIn('在「大V观点」里点一次「同步全部」', data['detail'])
        self.assertNotIn('写入知识库', data['detail'])

    def test_a_fresh_link_that_cannot_get_the_pipeline_says_what_to_do(self):
        with patch.object(grab, 'request', return_value=html_resp(page_html())), \
             self.pipeline_patch(started=False):
            data = self.post().json()
        self.assertEqual((data['added'], data['pipeline']), (1, 'busy'))
        self.assertEqual(data['run'], '')          # 没起跑就没有可核对的那一轮
        self.assertIn('其他同步正在运行', data['detail'])
        self.assertIn('写入知识库', data['detail'])
        # 占不到管道不是失败:内容已经在库里了,下一轮 export 会兜住它。
        self.assertEqual(len(self.items()), 1)

    def test_a_refusal_comes_back_as_a_readable_400(self):
        """**一个真实请求都不许发。** 这一条原先拿一条公众号地址当素材,而
        `classify` 不再按域名拒它了(R2-F3)—— 于是用例会**真的去连
        mp.weixin.qq.com**:结论跟着本机网络走,不可重放。地址那一档照旧不发
        HTTP(X 的账号页),响应那一档把 `request` 打桩成那张验证页。
        """
        r = self.client.post('/api/grab', json={'url': 'https://x.com/example'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('大V观点', r.json()['detail'])
        gate = ('https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=x'
                '&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Fabc')
        with patch.object(grab, 'request', return_value=html_resp(
                '<html><body><p>环境异常</p></body></html>', url=gate)):
            r = self.client.post('/api/grab', json={'url': 'https://mp.weixin.qq.com/s/abc'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('公众号', r.json()['detail'])
        self.assertEqual(self.items(), [])


if __name__ == '__main__':
    unittest.main()
