# -*- coding: utf-8 -*-
"""第三方长文接口给出**结构异常、但语法合法**的 HTTP 200 回包时怎么收场。

2026-10-10 第 2 轮审查的那一条:正文块是正常列表、文字也正常,只把一张媒体的
`media_info` 从字典换成**非空列表** —— `(… or {}).get(…)` 当场抛
`AttributeError: 'list' object has no attribute 'get'`,而 `render` 跑在 `fetch`
的异常转换区间**之外**,于是这个异常

  漏出 `fetch` → 漏出 `_one`/`fill`(只认 `ArticleUnavailable`)
  → 漏出 `harvest` 的账号隔离(只认 `XBrowserError`/`CdpError`)

**后面的账号一个都不同步**(报告实测:账号启动列表只剩 `['Review']`),而那一篇
明明在浏览器里打得开、后路本身在这个场景能工作。

修完之后分两道,**各有自己的用例**:
- **承重**:每一层嵌套明确判类型(`_rows` + `isinstance`),认得出的结构异常
  就地降级 —— 那张图跳过、那一块跳过,**整篇正文照旧补得到**。所以报告那份
  输入的末态比它建议的更好:不用走后路,正文直接补上。
- **兜底**:`fetch` 把 `json.loads` 和 `render` 的解析失败一起归到
  `ArticleUnavailable`(含 `RecursionError` —— 深嵌套回包抛的正是它,而它不是
  `ValueError`)。于是没想到的形状也只是「这一条问不到」:有浏览器走后路,
  没有就本轮不入库、水位留在原处、下一轮重试。

**按形状单独一个文件**:这一组的摆盘是「坏回包 + 两个账号的真实 harvest」,
判据是「账号隔离住了没有、正文补到了没有」;后路那组的判据是页面落在哪一篇上、
预算那组数的是发了几次请求,形状都不同(而 `test_x_article_fallback.py` 已经
308 行)。摆盘仍共用 `test_x_article` 那一份。

跑:  python -m unittest tests.test_x_article_malformed -v
"""
import contextlib
import json
import unittest
from unittest.mock import patch

from kb.feeds import x_article, x_browser
from kb.feeds.fetching import Response
from tests.test_x_article import (OLD_WATER, _FakeChrome, answering,
                                  article_payload, entry, snapshot)

# 报告那份最小受控输入,逐字照抄:正文块是正常列表、文字正常,只有一张媒体的
# `media_info` 是**非空列表**。`or {}` 只对假值生效,所以它正是那一版会抛的形状。
BAD_MEDIA = {'tweet': {'article': {
    'title': 'Primary article',
    'content': {'blocks': [{'type': 'unstyled',
                            'text': 'Otherwise valid article text'}]},
    'media_entities': [{'media_id': '1',
                        'media_info': ['incompatible-media-format']}]}}}

# 另一种撒谎方式:嵌套几千层的数组。`json.loads` 对它抛 `RecursionError`,而
# 那是 `RuntimeError` 的子类 —— 原来那道 `except (OSError, ValueError,
# UnicodeError)` 认不出它,同一条链照样把整批账号带掉(2026-10-10 实测)。
DEEP = '[' * 3000 + ']' * 3000


class _Deep:
    """按 `request` 的契约交出一份读不懂的回包(不是 JSON 语法错,是太深)。"""

    def __init__(self):
        self.urls = []

    def __call__(self, url, headers=None, **kw):
        self.urls.append(url)
        return Response(200, url, DEEP.encode('utf-8'), {})


def two_accounts(script=(), *, snaps=(), fallback=True, deep=False,
                 started=None):
    """真跑一轮 `x_browser.harvest`:两个账号 `Review → Later`。

    第一个账号有两条 —— 一篇长文(空正文)+ 一条普通推文;第二个账号只有一条
    普通推文。`started` 记下 `_harvest_one` 真的为哪几个账号开过工,**这就是
    「账号隔离住了没有」的全部依据**(报告那一局的证据正是这张列表)。

    `fill` / `_one` / `fetch` / `render` / `browser_fetch` **一行不打桩**,只替掉
    接口回包和时间线输入 —— 打桩成功正好绕开唯一要验的那件事。

    `started` 可以从外面传进来:异常漏出 `harvest` 时这个函数压根不返回,而那
    一局要看的恰恰是「出事之前第二个账号开过工吗」。
    """
    started = [] if started is None else started
    rows = {
        'Review': [entry(url='https://x.com/Review/status/100'),
                   {**entry(url='https://x.com/Review/status/101',
                            body='一条普通推文'),
                    'published': '2026-10-07 20:30'}],
        'Later': [{**entry(url='https://x.com/Later/status/200',
                           body='另一个账号的推文'),
                   'published': '2026-10-08 09:00'}],
    }

    def fake_one(page, handle, floor):
        started.append(handle)
        return [dict(e) for e in rows[handle]], ''

    _FakeChrome.reset(*snaps)
    if deep:
        calls = _Deep()
        req = patch.object(x_article, 'request', calls)
    else:
        req, calls = answering(*script)
    stack = [req, patch.object(x_browser, '_harvest_one', fake_one),
             patch.object(x_browser, 'Chrome', _FakeChrome),
             patch.object(x_article, 'PAGE_WAIT', 0.0),
             patch.object(x_article, 'BROWSER_FALLBACK', fallback)]
    with contextlib.ExitStack() as es:
        for cm in stack:
            es.enter_context(cm)
        got = x_browser.harvest([('Review', OLD_WATER), ('Later', OLD_WATER)],
                                profile='/tmp/p')
    return got, started, calls


class MalformedPayloadTests(unittest.TestCase):
    """**承重那一道**:认得出的结构异常就地降级,整篇正文照旧补得到。"""

    def test_a_bad_media_table_still_yields_the_article_body(self):
        """**后果先断言**:报告那份输入的那一篇**正文真的补上了**,而且第二个
        账号照旧开工。

        被审那一版在这里抛 `AttributeError`(整轮 `harvest` 带掉,`started` 只
        剩 `['Review']`);只加兜底、不加类型检查的实现会退一档走后路 —— 那也能
        过报告的要求,但正文块本来就是好的,末态该是直接补上。
        """
        got, started, calls = two_accounts([BAD_MEDIA])
        self.assertEqual(['Review', 'Later'], started,
                         f'第二个账号没开工:{started}')
        rows = {e['url']: e for e in got['Review']['entries']}
        self.assertEqual(
            'Otherwise valid article text',
            rows['https://x.com/Review/status/100']['body'],
            '正文块是好的,这一篇该直接补上')
        self.assertEqual('Primary article',
                         rows['https://x.com/Review/status/100']['title'])
        self.assertEqual('', got['Review']['incomplete'], '没有东西该扣留')
        self.assertEqual('', got['Review'].get('error', ''))
        self.assertEqual([], _FakeChrome.visited,
                         '正文补到了,一次后路导航都不该发')
        self.assertNotEqual(OLD_WATER, got['Review']['watermark'],
                            '这一轮读全了,水位照常推进')
        self.assertEqual(1, len(got['Later']['entries']), '第二个账号的内容在')

    def test_a_malformed_payload_never_strands_the_next_account(self):
        """**报告那一局的证据形态**:第二个账号真的开过工吗。

        上面那条用例在被审那一版下是 **ERROR**(异常一路漏出 `harvest`),它
        说的是「异常跑出来了」;这一条把它收下来再数那张启动列表,失败消息
        直接就是报告里那份 `['Review']`。两条各有射程 —— 少了这一条,只能从
        traceback 去推断后面的账号有没有被带掉,而"漏出一个异常"和"后面的账号
        一个都不同步"是两件事(前者在调用链上任何一层被吞掉都会变成后者)。
        """
        started: list[str] = []
        crash = ''
        try:
            two_accounts([BAD_MEDIA], started=started)
        except Exception as e:        # noqa: BLE001 —— 要验的正是「它漏出来了」
            crash = f'{type(e).__name__}: {e}'
        self.assertEqual(['Review', 'Later'], started,
                         f'第二个账号没开工(异常:{crash or "无"})')

    def test_the_body_arrives_even_with_no_fallback_at_all(self):
        """同一份输入在**没有后路**时照旧补到 —— 这一道证明它不靠后路兜着。"""
        got, started, _ = two_accounts([BAD_MEDIA], fallback=False)
        self.assertEqual(['Review', 'Later'], started)
        rows = {e['url']: e for e in got['Review']['entries']}
        self.assertEqual('Otherwise valid article text',
                         rows['https://x.com/Review/status/100']['body'])
        self.assertEqual('', got['Review']['incomplete'])

    def test_a_broken_media_entry_only_drops_that_image(self):
        """**局部降级**:atomic 块引用的那张图认不出来时只有那一块跳过,
        其余段落逐字照旧。"""
        payload = article_payload()
        art = payload['tweet']['article']
        art['media_entities'][0]['media_info'] = ['不是字典']
        body = x_article.render(art)
        self.assertNotIn('![](', body, '认不出地址就不该编一个出来')
        self.assertIn('## Teamily AI 接手取舍之后的下一步', body)
        self.assertIn('> 每天最多 20 分钟', body)

    def test_a_normal_media_table_still_resolves(self):
        """**反过来钉一边**:正常回包照旧解出图片地址。

        少了它,一个「所有 media 一律跳过」的实现也能让上面那几条全绿。
        """
        body = x_article.render(article_payload()['tweet']['article'])
        self.assertIn('![](https://pbs.twimg.com/', body)

    def test_scalar_shapes_in_any_nested_field_do_not_raise(self):
        """每一层嵌套各换成几种标量:一个都不许抛,正文照旧出来。

        这是那张**不会收敛**的名单里今天认得出的那几格 —— 覆盖它们是承重那一
        道的全部内容,没想到的形状交给兜底。
        """
        shapes = (5, 'x', True, {'a': 1}, [], None, 1.5)
        for bad in shapes:
            for field in ('media_entities', 'entityMap', 'blocks',
                          'entityRanges', 'mediaItems', 'data'):
                with self.subTest(field=field, bad=bad):
                    art = {
                        'title': 't',
                        'content': {
                            'blocks': [{'type': 'unstyled', 'text': '正文在'},
                                       {'type': 'atomic', 'text': ' ',
                                        'entityRanges': [{'key': 4}]}],
                            'entityMap': [{'key': '4', 'value': {
                                'type': 'MEDIA',
                                'data': {'mediaItems': [{'mediaId': '7'}]}}}],
                        },
                        'media_entities': [{'media_id': '7', 'media_info': {
                            'original_img_url': 'https://e.org/a.png'}}],
                    }
                    if field == 'media_entities':
                        art['media_entities'] = bad
                    elif field == 'entityMap':
                        art['content']['entityMap'] = bad
                    elif field == 'blocks':
                        art['content']['blocks'] = [
                            {'type': 'unstyled', 'text': '正文在'},
                            {'type': 'atomic', 'entityRanges': bad}]
                    elif field == 'entityRanges':
                        art['content']['blocks'][1]['entityRanges'] = bad
                    elif field == 'mediaItems':
                        art['content']['entityMap'][0]['value']['data'] = \
                            {'mediaItems': bad}
                    else:
                        art['content']['entityMap'][0]['value']['data'] = bad
                    body = x_article.render(art)      # 抛就红
                    if field != 'blocks':
                        self.assertIn('正文在', body, '正文不该跟着一起没了')


class AccountIsolationTests(unittest.TestCase):
    """**兜底那一道**:没想到的形状归到「这一条问不到」,账号隔离照旧成立。"""

    def test_an_unreadable_payload_falls_back_and_the_next_account_runs(self):
        """**后果先断言**:接口的回包读不懂时走后路补到正文,而且第二个账号
        **继续处理** —— 报告那一局要的正是这两件事。"""
        got, started, calls = two_accounts(
            None, snaps=(snapshot(blocks=['后路读到的正文。']),), deep=True)
        self.assertEqual(['Review', 'Later'], started,
                         f'第二个账号没开工:{started}')
        rows = {e['url']: e for e in got['Review']['entries']}
        self.assertEqual('后路读到的正文。',
                         rows['https://x.com/Review/status/100']['body'])
        self.assertEqual('', got['Review']['incomplete'])
        self.assertEqual(1, len(_FakeChrome.visited), '后路该被走一次')
        self.assertEqual(1, len(got['Later']['entries']))

    def test_without_a_fallback_only_that_entry_is_held(self):
        """没有后路时**只扣留异常那一条**:同一个账号里的普通推文照旧留下,
        水位留在原处,第二个账号继续,而且这一轮不记成失败。"""
        got, started, _ = two_accounts(None, fallback=False, deep=True)
        self.assertEqual(['Review', 'Later'], started)
        urls = [e['url'] for e in got['Review']['entries']]
        self.assertEqual(['https://x.com/Review/status/101'], urls,
                         '长文扣留、普通推文保留')
        self.assertIn('没取到', got['Review']['incomplete'])
        self.assertEqual(OLD_WATER, got['Review']['watermark'],
                         '没读完就不许推进水位')
        self.assertEqual('', got['Review'].get('error', ''),
                         '补不到不是故障,不该记成失败')
        self.assertEqual(1, len(got['Later']['entries']))

    def test_a_deeply_nested_payload_is_an_article_unavailable(self):
        """`RecursionError` 那道的**独立射程**:它不是 `ValueError`,原来从
        `json.loads` 那道 except 旁边漏过去。"""
        with patch.object(x_article, 'request', _Deep()):
            with self.assertRaises(x_article.ArticleUnavailable) as cm:
                x_article.fetch('https://x.com/a/status/1')
        self.assertIn('读不懂', str(cm.exception))

    def test_a_render_crash_stays_inside_the_interface_boundary(self):
        """**契约**:`render` 抛出数据形状异常时 `fetch` 必须翻成
        `ArticleUnavailable`,不许让它漏出去。

        **如实记一笔:今天这一道是冗余带。** `json.loads` 产得出的类型只有
        dict/list/str/数字/bool/None 这几种,逐处类型检查补全之后构造不出能走
        到这里的真输入(定点注入实测 0 红,所以才有这条契约用例)。留着它的
        理由是构造上的:`render` 的形状约束不该取决于一张逐处枚举的名单,而
        那张名单**不会收敛** —— 少想一种就是本轮这场事故。
        """
        req, _ = answering(article_payload())

        def boom(article):
            raise AttributeError("'list' object has no attribute 'get'")

        with req, patch.object(x_article, 'render', boom):
            with self.assertRaises(x_article.ArticleUnavailable) as cm:
                x_article.fetch('https://x.com/a/status/1')
        self.assertIn('解析不了', str(cm.exception))


class BrowserSnapshotTests(unittest.TestCase):
    """后路那一侧同一条口径:它恰恰是「接口挂了」那一刻走的,自己也漏
    `TypeError` 的话同一场事故只是换了个位置。"""

    def test_a_non_list_blocks_field_is_treated_as_no_body(self):
        _FakeChrome.reset(json.dumps({'at': True, 'back': False,
                                      'path': '/a/article/1', 'blocks': 7,
                                      'title': 'T'}))
        with patch.object(x_article, 'PAGE_WAIT', 0.0):
            with self.assertRaises(x_article.ArticleUnavailable) as cm:
                x_article.browser_fetch(_FakeChrome(),
                                        'https://x.com/a/status/1')
        self.assertIn('没渲染出正文', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
