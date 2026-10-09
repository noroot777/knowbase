# -*- coding: utf-8 -*-
"""X 长文(Article)的正文补全。

**被审的那件事**:长文推文在时间线上一个字都没有(正文就是一条被 X 藏起来的
`x.com/i/article/<id>` 链接),两条登录态通道入库的都是空正文 —— 标题回落成
「某某 的动态」、vault 里那篇 md 没有内容、提取阶段每轮把它报成「正文为空」。
2026-10-09 实测:近三周 social 的 342 条里 11 条空正文,其中 9 条是长文。

**每条用例都先断言后果**(补上的正文真的进了条目、取不到的那几条下一轮还取得到),
不是"有没有调过那个接口"—— 只验后者的话,一个把回包丢掉的实现也能全绿。

样本按真实回包的形状摆(2026-10-09 抓的 9 篇真文章):`entityMap` 是
`[{key, value}, …]` **列表**而不是字典、图片按 `mediaId` 到 `media_entities` 里查、
块类型有 `unstyled / header-two / ordered-list-item / atomic` 这几种。

跑:  python -m unittest tests.test_x_article -v
"""
import contextlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import urlsplit

from kb.feeds import registry as reg, x_article, x_browser, x_feedgrab
from kb.feeds.fetching import FeedError, Response
from kb.feeds.x_cdp import CdpError
from tests.test_x_feedgrab_contract import TODAY, FeedgrabRound, tweet

FRESH = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
IMG = 'https://pbs.twimg.com/media/HUIE7DSbEAEJXcR.jpg'


def article_payload(title='X书签保存Skill x Teamily', blocks=None, media=True):
    """照真实回包的形状摆一份。"""
    blocks = blocks if blocks is not None else [
        {'type': 'unstyled', 'text': '整理书签、做学习计划，我以前都是一个人对着 AI 来回问。'},
        {'type': 'header-two', 'text': 'Teamily AI 接手取舍之后的下一步'},
        {'type': 'atomic', 'text': ' ', 'entityRanges': [{'key': 4, 'length': 1, 'offset': 0}]},
        {'type': 'ordered-list-item', 'text': '先把资料保存在自己能找到的地方'},
        {'type': 'unordered-list-item', 'text': '只选一个近期目标'},
        {'type': 'blockquote', 'text': '每天最多 20 分钟'},
    ]
    return {'tweet': {'article': {
        'title': title,
        'content': {
            'blocks': blocks,
            # **列表**,不是字典 —— 真实回包就是这样,块里的 key 却是数字。
            'entityMap': [{'key': '4', 'value': {
                'type': 'MEDIA',
                'data': {'mediaItems': [{'mediaId': '2108252988104183809'}]}}}],
        },
        'media_entities': ([{'media_id': '2108252988104183809',
                             'media_info': {'original_img_url': IMG}}] if media else []),
    }}}


def entry(url='https://x.com/Pluvio9yte/status/2108346962986909740',
          body='', title='雪踏乌云 的动态'):
    return {'url': url, 'title': title, 'body': body, 'author': '雪踏乌云',
            'published': '2026-10-09 08:01', 'content_kind': 'post'}


class _Calls:
    """记下发了几次请求、都发给谁,顺带按剧本回答。"""

    def __init__(self, script):
        self.script, self.urls = list(script), []

    def __call__(self, url, headers=None, **kw):
        self.urls.append(url)
        answer = self.script[min(len(self.urls) - 1, len(self.script) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return Response(200, url, json.dumps(answer).encode('utf-8'), {})


def answering(*script):
    calls = _Calls(script)
    return patch.object(x_article, 'request', calls), calls


class RenderTests(unittest.TestCase):
    """文章 JSON → markdown。"""

    def body(self, **kw):
        return x_article.render(article_payload(**kw)['tweet']['article'])

    def test_block_kinds_become_markdown(self):
        text = self.body()
        self.assertIn('## Teamily AI 接手取舍之后的下一步', text)
        self.assertIn('1. 先把资料保存在自己能找到的地方', text)
        self.assertIn('- 只选一个近期目标', text)
        self.assertIn('> 每天最多 20 分钟', text)

    def test_paragraphs_are_separated_by_a_blank_line(self):
        """段落边界是项目里反复栽过的那件事(见 CLAUDE.md「订阅正文转纯文本」):
        单换行在 vault md 里会被 markdown 合并成一段,翻译那边也按 `\\n\\n` 切块。"""
        text = self.body()
        self.assertIn('来回问。\n\n## Teamily', text)

    def test_an_atomic_block_becomes_the_image_it_points_at(self):
        """`entityMap` 是**列表**、块里的 key 是数字、图片地址还在另一张表里 ——
        三者任何一处对不上都只会少一张图,所以直接断言那张图在正文里。"""
        self.assertIn(f'![]({IMG})', self.body())

    def test_a_dict_entity_map_is_read_too(self):
        """上游换回 Draft.js 原生的字典形状时不该整篇掉图。"""
        payload = article_payload()
        content = payload['tweet']['article']['content']
        content['entityMap'] = {'4': content['entityMap'][0]['value']}
        self.assertIn(f'![]({IMG})', x_article.render(payload['tweet']['article']))

    def test_an_image_that_cannot_be_resolved_is_skipped_not_faked(self):
        text = self.body(media=False)
        self.assertNotIn('![]', text)
        self.assertIn('## Teamily AI 接手取舍之后的下一步', text)

    def test_inline_offsets_are_never_applied_to_the_text(self):
        """**不展开块内偏移。** Draft.js 的 offset 是 UTF-16 码元下标,而 Python
        按码点索引 —— 正文里一个 emoji 就把后面所有偏移错开一位,拿错位的下标去
        插标记就是把作者写的字改坏。这里钉的是「原文一个字都没动」。"""
        raw = '看看这个 🚀 工具 List<T> 很好用'
        text = self.body(blocks=[{'type': 'unstyled', 'text': raw,
                                  'entityRanges': [{'key': 0, 'length': 2, 'offset': 5}],
                                  'inlineStyleRanges': [{'style': 'Bold', 'length': 2,
                                                         'offset': 5}]}])
        self.assertEqual(raw, text)

    def test_the_title_is_not_repeated_in_the_body(self):
        """`sync.export` 自己会在 md 顶上写一行 `# <title>`。"""
        self.assertNotIn('X书签保存Skill', self.body())

    def test_a_very_long_article_is_capped(self):
        with patch.object(x_article, 'MAX_CHARS', 20):
            self.assertEqual(20, len(self.body(blocks=[
                {'type': 'unstyled', 'text': 'x' * 500}])))


class NeedsArticleTests(unittest.TestCase):
    """哪些条目要去问一次长文。"""

    def test_an_empty_body_is_asked_about(self):
        self.assertTrue(x_article.needs_article(entry()))

    def test_a_bare_article_link_is_asked_about(self):
        """feedgrab 的产物正文就是这条被展开的链接。"""
        self.assertTrue(x_article.needs_article(
            entry(body='https://x.com/i/article/2108252660529049601')))

    def test_a_bare_tco_link_is_asked_about(self):
        self.assertTrue(x_article.needs_article(entry(body='https://t.co/FurxwSLp20')))

    def test_a_retweet_context_line_alone_is_not_saying_anything(self):
        """`_entry` 给转推加的那行 `> 上下文` 让正文"非空"了,可作者一个字没说。"""
        self.assertTrue(x_article.needs_article(entry(body='> 雪踏乌云 转推了')))

    def test_a_tweet_that_actually_says_something_is_left_alone(self):
        self.assertFalse(x_article.needs_article(entry(body='Grok Bot接入微信看起来很丝滑啊')))

    def test_an_explicit_article_link_counts_even_next_to_real_text(self):
        self.assertTrue(x_article.needs_article(
            entry(body='我的新文章 https://x.com/i/article/2108252660529049601')))

    def test_a_tco_link_next_to_real_text_is_not_a_signal(self):
        """**反过来钉一边**:拿 t.co 当信号会给一大半推文各买一次调用,而换回来的
        多半是"它没有长文"。"""
        self.assertFalse(x_article.needs_article(
            entry(body='看这张图 https://t.co/SskpMm0usL')))

    def test_an_entry_without_a_url_is_left_alone(self):
        self.assertFalse(x_article.needs_article({'url': '', 'body': ''}))


class FetchTests(unittest.TestCase):
    """「问不到」和「它没有长文」必须分得开。"""

    def test_an_article_comes_back_with_a_title_and_a_body(self):
        req, calls = answering(article_payload())
        with req:
            got = x_article.fetch('https://x.com/Pluvio9yte/status/2108346962986909740')
        self.assertEqual('X书签保存Skill x Teamily', got['title'])
        self.assertIn('整理书签', got['body'])
        self.assertEqual(['https://api.fxtwitter.com/Pluvio9yte/status/2108346962986909740'],
                         calls.urls)

    def test_a_tweet_without_an_article_answers_none_not_an_error(self):
        """纯图、纯视频推文就是这样 —— 那是一个**确定的结论**,不该冻住水位。"""
        req, _ = answering({'tweet': {'text': 'https://t.co/x'}})
        with req:
            self.assertIsNone(x_article.fetch('https://x.com/thenanyu/status/2105049816388755620'))

    def test_the_reason_does_not_talk_about_the_rss_bridge(self):
        """`fetching` 的成品文案后半截说的是订阅桥接 —— 跟这一步毫无关系,带出去
        只会把人指到一个改不出结果的设置上。"""
        req, _ = answering(FeedError('订阅服务返回 HTTP 500;请检查来源或更换订阅桥接'))
        with req, self.assertRaises(x_article.ArticleUnavailable) as caught:
            x_article.fetch(entry()['url'])
        self.assertIn('HTTP 500', str(caught.exception))
        self.assertNotIn('桥接', str(caught.exception))

    def test_an_http_error_is_unavailable_not_none(self):
        req, _ = answering(FeedError('订阅服务返回 HTTP 500'))
        with req:
            with self.assertRaises(x_article.ArticleUnavailable):
                x_article.fetch('https://x.com/fchollet/status/2106140143933628675')

    def test_a_reply_that_is_not_json_is_unavailable(self):
        with patch.object(x_article, 'request',
                          lambda *a, **k: Response(200, a[0], b'<html>nope</html>', {})):
            with self.assertRaises(x_article.ArticleUnavailable):
                x_article.fetch('https://x.com/a/status/1')

    def test_a_reply_without_a_tweet_is_unavailable(self):
        req, _ = answering({'code': 401, 'message': 'nope'})
        with req:
            with self.assertRaises(x_article.ArticleUnavailable):
                x_article.fetch('https://x.com/a/status/1')

    def test_an_empty_article_is_unavailable(self):
        req, _ = answering({'tweet': {'article': {'title': '', 'content': {'blocks': []}}}})
        with req:
            with self.assertRaises(x_article.ArticleUnavailable):
                x_article.fetch('https://x.com/a/status/1')

    def test_a_url_that_is_not_a_status_link_is_never_asked_about(self):
        req, calls = answering(article_payload())
        with req:
            self.assertIsNone(x_article.fetch('https://example.org/post/1'))
        self.assertEqual([], calls.urls)

    def test_turning_the_endpoint_off_sends_nothing(self):
        req, calls = answering(article_payload())
        with req, patch.object(x_article, 'API', ''):
            self.assertIsNone(x_article.fetch('https://x.com/a/status/1'))
        self.assertEqual([], calls.urls)


class FillTests(unittest.TestCase):
    """一整批的补全:补上了什么、留下了什么、没补上的怎么交代。"""

    def test_the_article_replaces_the_placeholder_title_and_the_empty_body(self):
        req, _ = answering(article_payload())
        with req:
            kept, note = x_article.fill([entry()])
        self.assertEqual('', note)
        self.assertEqual('X书签保存Skill x Teamily', kept[0]['title'])
        self.assertIn('整理书签', kept[0]['body'])

    def test_what_the_tweet_itself_said_stays_at_the_top(self):
        req, _ = answering(article_payload())
        with req:
            kept, _ = x_article.fill([entry(
                body='我的新文章 https://x.com/i/article/2108252660529049601')])
        self.assertTrue(kept[0]['body'].startswith('我的新文章'))
        self.assertIn('整理书签', kept[0]['body'])

    def test_a_tweet_with_no_article_is_kept_untouched_and_costs_no_watermark(self):
        """**反过来钉一边**:一律当成"没补到"的实现会把这条也拦下、冻住水位。"""
        req, _ = answering({'tweet': {'text': 'x'}})
        with req:
            kept, note = x_article.fill([entry()])
        self.assertEqual('', note)
        self.assertEqual([''], [e['body'] for e in kept])

    def test_an_unavailable_article_is_held_back_and_reported(self):
        """补不到的**本轮不入库**:入库了下一轮的重试也更新不了它
        (`put_items` 是 `ON CONFLICT DO NOTHING`),留着等于白等。"""
        req, _ = answering(FeedError('HTTP 500'))
        with req:
            kept, note = x_article.fill([entry(), entry(url='https://x.com/a/status/2',
                                                        body='一条普通推文')])
        self.assertEqual(['https://x.com/a/status/2'], [e['url'] for e in kept])
        self.assertIn('长文正文没取到', note)

    def test_a_dead_endpoint_is_not_asked_fifty_times(self):
        req, calls = answering(FeedError('HTTP 500'))
        with req:
            kept, note = x_article.fill([entry(url=f'https://x.com/a/status/{i}')
                                         for i in range(10)])
        self.assertEqual(x_article._GIVE_UP_AFTER, len(calls.urls))
        self.assertEqual([], kept)
        self.assertIn('10', note)

    def test_a_success_resets_the_give_up_counter(self):
        req, calls = answering(FeedError('x'), FeedError('x'), article_payload(),
                               FeedError('x'), FeedError('x'), article_payload())
        with req:
            kept, _ = x_article.fill([entry(url=f'https://x.com/a/status/{i}')
                                      for i in range(6)])
        self.assertEqual(6, len(calls.urls))
        self.assertEqual(2, len(kept))

    def test_turning_the_endpoint_off_keeps_everything_as_it_is(self):
        req, calls = answering(FeedError('x'))
        with req, patch.object(x_article, 'API', ''):
            kept, note = x_article.fill([entry()])
        self.assertEqual(([entry()], '', []), (kept, note, calls.urls))


def snapshot(*, at=True, back=False, blocks=(), title='', path=None):
    """一份文章页的 DOM 快照,形状照 `_READ_JS` 真正返回的那个。

    `path` 默认 `None` = **由假会话按形状自己填**(落在刚 goto 过去的那一篇上),
    所以绝大多数用例压根不用管它;要摆「页面停在别的一篇上」那种局就显式给。
    """
    return json.dumps({'at': at, 'back': back, 'path': path,
                       'blocks': list(blocks), 'title': title})


# 后路读不到的那种形态:停在文章页上,一个块都没渲染出来(X 改版、没登录、
# 渲染超时都长这样)。默认用它,所以**摆盘默认是"接口和后路都问不到"**。
BLANK = snapshot(blocks=())


class _FakeChrome:
    """假的 CDP 会话。`script` 是 `eval` 要依次交出来的快照(或要抛的异常),
    用完之后一直重复最后那一份;`visited` 记下 `goto` 去过哪儿 —— 「接口答了
    没有长文就不该开浏览器」那几条用例数的就是它。

    **它自己维护「页面现在停在哪儿」**(`where`),因为导航失败时页面**留在
    原处**才是真实形态(实测:一个 HTTP 204 就让 `Page.navigate` 回
    `errorText: net::ERR_ABORTED` 而页面一动不动)。`stuck` 摆的就是这一档:

    - `''`      正常导航,返回 `Page.navigate` 的正常回包;
    - `'abort'` 导航失败,**回包带 `errorText`**、页面停在原处;
    - `'silent'` 导航失败,**什么都不回**(老会话、别的实现都可能这样)——
      这一档专门钉「身份核对必须自己承重」:errorText 那道早一道在这里是瞎的。
    """

    script: list = [BLANK]
    visited: list = []
    where: str = ''          # 页面此刻停在哪个路径上
    stuck = ''               # '' / 'abort' / 'silent';给 list 则按 goto 次序逐档

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def open_page(self, url='about:blank'):
        return 'sid'

    def goto(self, url):
        type(self).visited.append(url)
        mode = type(self).stuck
        if isinstance(mode, list):           # 一条一档:['', 'abort'] = 第二次才失败
            mode = mode.pop(0) if mode else ''
        if mode == 'silent':
            return None
        if mode == 'abort':
            return {'frameId': 'f1', 'errorText': 'net::ERR_ABORTED'}
        type(self).where = urlsplit(url).path
        return {'frameId': 'f1', 'loaderId': 'l1'}

    def eval(self, expression, timeout=0.0):
        got = self.script[0] if len(self.script) == 1 else self.script.pop(0)
        if isinstance(got, Exception):
            raise got
        return self._with_path(got)

    @classmethod
    def _with_path(cls, got):
        """快照没显式写 `path` 时按形状填成「页面此刻在哪儿」。"""
        try:
            snap = json.loads(got)
        except (TypeError, ValueError):
            return got
        if not isinstance(snap, dict) or snap.get('path') is not None:
            return got
        if snap.get('back'):
            snap['path'] = cls.where.replace('/article/', '/status/')
        elif snap.get('at'):
            snap['path'] = cls.where
        else:
            snap['path'] = '/i/flow/login'     # 登录墙那一档
        return json.dumps(snap)

    @classmethod
    def reset(cls, *script, stuck=''):
        cls.script = list(script) or [BLANK]
        cls.visited = []
        cls.where = ''
        cls.stuck = list(stuck) if isinstance(stuck, (list, tuple)) else stuck


OLD_WATER = '2026-10-01T00:00:00+00:00'


def harvest_round(script, *, water=OLD_WATER, snaps=(), fallback=True, api=None):
    """真跑一轮 `x_browser.harvest`:一条长文(空正文)+ 一条普通推文。

    `script` 是长文接口依次要给的回答,`snaps` 是浏览器后路依次要交出的 DOM
    快照。两条路**都在真实位置上**(`fill` / `browser_fetch` 一行不打桩),
    所以这组用例对"后路接没接上"是敏感的。
    """
    def fake_one(page, handle, floor):
        return [entry(url='https://x.com/example/status/9'),
                {**entry(url='https://x.com/example/status/8', body='一条普通推文'),
                 'published': '2026-10-07 20:30'}], ''

    _FakeChrome.reset(*snaps)
    req, calls = answering(*script)
    stack = [req, patch.object(x_browser, '_harvest_one', fake_one),
             patch.object(x_browser, 'Chrome', _FakeChrome),
             patch.object(x_article, 'PAGE_WAIT', 0.0),
             patch.object(x_article, 'BROWSER_FALLBACK', fallback)]
    if api is not None:
        stack.append(patch.object(x_article, 'API', api))
    with contextlib.ExitStack() as es:
        for cm in stack:
            es.enter_context(cm)
        got = x_browser.harvest([('example', water)], profile='/tmp/p')
    return got['example'], calls


class WatermarkTests(unittest.TestCase):
    """整条链上那一半:补不到长文的那一轮算「没读完」,水位一步都不许动。

    **后果先断言** —— 水位推过去之后那条长文比旧水位新、又比新水位老,从此任何
    一轮增量都选不到它,而它入库的那份正文是空的、再也更新不了。
    """

    OLD = '2026-10-01T00:00:00+00:00'

    def harvest(self, script, **kw):
        return harvest_round(script, water=self.OLD, **kw)

    def test_an_article_we_could_not_read_freezes_the_watermark(self):
        """接口和后路**都**问不到:默认摆盘里浏览器停在文章页、一个块都没渲染
        出来,所以这一条验的是两条路都用尽之后的末态。"""
        got, _ = self.harvest([FeedError('HTTP 500')])
        self.assertEqual(self.OLD, got['watermark'])
        self.assertIn('长文正文没取到', got['incomplete'])
        self.assertEqual(['https://x.com/example/status/8'],
                         [e['url'] for e in got['entries']])
        self.assertEqual('', got['error'], '这不是一次失败,只是没读完')

    def test_a_round_that_read_every_article_advances_as_usual(self):
        """**反过来钉一边**:一律报「没取到」的实现会把水位永久冻住。"""
        got, _ = self.harvest([article_payload()])
        self.assertEqual('', got['incomplete'])
        self.assertEqual(datetime(2026, 10, 9, 0, 1, tzinfo=timezone.utc),
                         x_browser._utc(got['watermark']),
                         '补全成功的那一轮水位照旧跟到这一批里最新那条')
        body = [e['body'] for e in got['entries']
                if e['url'].endswith('/9')][0]
        self.assertIn('整理书签', body)


class FeedgrabEndToEndTests(FeedgrabRound):
    """feedgrab 那条通道同样挂着这一步,而且**要真跑一轮 `harvest`**。

    只测 `fill` 的话,这条通道上那个钩子摘掉一条用例都不红(自查发现:注入
    「整档去掉」时只有浏览器那两条水位用例红)—— 而两条通道各挂一次,漏哪一条
    都是那条通道上的长文永久空正文。这里一行不打桩,只替掉那个外部程序本身和
    长文接口的回包:子进程调用、前言解析、归属过滤、整批预取、存盘事务,一路
    到 feeds.db。
    """

    ARTICLE = 'https://x.com/i/article/2108252660529049601'

    def test_the_article_body_reaches_the_knowledge_base(self):
        s = self.start_from()
        self.cli(example={'display': 'Example Person',
                          'tweets': [tweet(title='', body=self.ARTICLE)]})
        req, calls = answering(article_payload())
        with req:
            summary = self.collect()
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        row = self.rows(s['id'])['items'][0]
        # 后果先断言:库里那一行是长文正文,不是一条光秃秃的链接。
        self.assertIn('整理书签', row['body'])
        self.assertNotIn('/i/article/', row['body'])
        self.assertEqual('X书签保存Skill x Teamily', row['title'])
        # 补全了的这一轮照旧推进水位,而且确实问过那条推文。
        self.assertEqual([], summary['partial'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(['https://api.fxtwitter.com/Example/status/10001'], calls.urls)

    def test_an_article_we_could_not_read_freezes_the_watermark(self):
        """取不到的那一轮:水位一步不动、面板上说得出原因、下一轮还取得到。"""
        s = self.start_from()
        self.cli(example={'display': 'Example Person',
                          'tweets': [tweet(title='', body=self.ARTICLE)]})
        req, _ = answering(FeedError('HTTP 500'))
        with req:
            summary = self.collect()
        self.assertEqual(self.day(4), self.water(s['id']),
                         '动了的话这条长文从此任何一轮增量都选不到')
        self.assertEqual(0, summary['added'])
        self.assertEqual(0, self.rows(s['id'])['total'],
                         '空正文入库之后 put_items 再也更新不了它')
        self.assertEqual([], summary['errors'], '这不是一次失败,只是没读完')
        self.assertIn('长文正文没取到', summary['partial'][0]['reason'])
        page = self.client.get('/api/intake/social').json()
        rows = {row['name']: row for row in page['sources']}
        self.assertIn('长文正文没取到', rows['Example']['last_note'])

    def test_the_article_comes_back_once_the_endpoint_recovers(self):
        """上一条留着的旧水位就是这一条的全部依据。"""
        s = self.start_from()
        self.cli(example={'display': 'Example Person',
                          'tweets': [tweet(title='', body=self.ARTICLE)]})
        with answering(FeedError('HTTP 500'))[0]:
            self.collect()
        with answering(article_payload())[0]:
            summary = self.collect()
        self.assertEqual(1, summary['added'])
        self.assertIn('整理书签', self.rows(s['id'])['items'][0]['body'])
        self.assertEqual(TODAY, self.water(s['id']))
        page = self.client.get('/api/intake/social').json()
        self.assertEqual('', {r['name']: r for r in page['sources']}['Example']['last_note'],
                         '读完的那一轮把提示清掉')


class FeedgrabTests(unittest.TestCase):
    """它的产物正文就是那条文章链接 —— 这里钉 `_entry` 交出来的形状。"""

    def test_the_article_link_body_is_replaced_by_the_article(self):
        meta = {'source': 'https://x.com/pluvio9yte/status/2108346962986909740',
                'author_name': '雪踏乌云', 'title': '', 'published': '2026-10-09'}
        got = x_feedgrab._entry(meta, 'https://x.com/i/article/2108252660529049601',
                                'pluvio9yte')
        req, _ = answering(article_payload())
        with req:
            kept, note = x_article.fill([got])
        self.assertEqual('', note)
        self.assertEqual('X书签保存Skill x Teamily', kept[0]['title'])
        self.assertIn('整理书签', kept[0]['body'])


if __name__ == '__main__':
    unittest.main()
