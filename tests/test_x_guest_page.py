# -*- coding: utf-8 -*-
"""不登录通道的第二件工具:X 自己的未登录页面(`x_guest_page`),和 FxTwitter 问不到时它接手的那一段。

页面是照 2026-11-03 实测的服务端直出 HTML 搭的最小骨架:时间不在 `<time>` 里,在一段内联脚本
`__xClientTextFormatters?.run("<元素 id>", "format…Timestamp", {"timestamp": 毫秒})` 的参数里;正文在
`div.whitespace-pre-wrap` 里;链接的完整地址在 `title` 上;截断的带一颗「显示更多」按钮。

只替掉最外面那一次 HTTP(`x_guest.request`、`x_guest_page.fetch`),其余都是真的。钉的是后果:
正文里是全文还是预览、库里落了哪几条、覆盖有没有被宣布读完。

跑:  python -m unittest tests.test_x_guest_page -v
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_channels, x_guest, x_guest_page
from kb.feeds.fetching import Response
from tests.test_x_guest import HOUR, HTTP_404, GuestBase

PAGE = 'https://x.com/{}'
DETAIL = 'https://x.com/{}/status/{}'


def article(handle, ident, hours_ago, text='一条推文', *, more=False, pinned=False, edited=False,
            links=(), card='', quote=''):
    """一条未登录页面上的推文。`links` 是 (显示文字, 完整地址) 对,接在正文后面。"""
    stamp = f'T{ident}'
    if edited:                                    # 编辑过的:时间挂在指向 /history 的链接里
        when = f'<a href="/{handle}/status/{ident}/history"><time id="{stamp}">Nov 1</time></a>'
    else:
        when = f'<a href="/{handle}/status/{ident}" id="{stamp}">Nov 1</a>'
    ms = int((time.time() - hours_ago * HOUR) * 1000)
    script = (f'<script>window.__xClientTextFormatters?.run("{stamp}", "formatRelativePastTimestamp", '
              f'{{"timestamp":{ms},"lang":"zh"}});</script>')
    body = f'<span>{text}</span>' + ''.join(
        f'<span><a href="https://t.co/{n}" target="_blank" title="{url}">{shown}</a></span>'
        for n, (shown, url) in enumerate(links))
    if more:
        body += ' <button type="button">显示更多</button>'
    card_html = f'<a href="{card}" target="_blank"><div>example.com</div></a>' if card else ''
    return (f'<article><div>{"<span>已置顶</span>" if pinned else ""}'
            f'<a href="/{handle}"><div>{handle} 本人</div></a><a href="/{handle}"><span>@{handle}</span></a>'
            f'<span>{when}{script}</span></div>'
            f'<div class="max-w-full whitespace-pre-wrap" dir="auto">{body}</div>{card_html}{quote}</article>')


def html(*articles):
    return f'<html><body><main>{"".join(articles)}</main></body></html>'


class Api:
    """FxTwitter 替身。`timeline[handle]` 是一页 results 或异常;`single[(handle, id)]` 是全文或异常。"""

    def __init__(self, timeline=None, single=None):
        self.timeline, self.single = timeline or {}, single or {}

    def __call__(self, url, headers=None, *, deadline=None, **_):
        if '/profile/' in url:
            page = self.timeline.get(url.split('/profile/')[1].split('/')[0], HTTP_404)
            if isinstance(page, Exception):
                raise page
            body = {'code': 200, 'results': page, 'cursor': {'bottom': None}}
        else:
            handle, _, ident = url.rsplit('/', 3)[-3:]
            text = self.single.get((handle, ident), HTTP_404)
            if isinstance(text, Exception):
                raise text
            body = {'code': 200, 'tweet': {'id': ident, 'text': text}}
        return Response(200, url, json.dumps(body).encode(), {})


class PageBase(GuestBase):
    def fx(self, **kw):
        p = patch.object(x_guest, 'request', Api(**kw))
        p.start(); self.addCleanup(p.stop)

    def page(self, handle, *articles):
        self.pages.pages[PAGE.format(handle)] = html(*articles)

    def detail(self, handle, ident, *articles):
        self.pages.pages[DETAIL.format(handle, ident)] = html(*articles)


class ParseTests(unittest.TestCase):
    def test_a_post_reads_identity_time_name_and_text_with_full_links(self):
        """显示文字是缩短的地址,完整的在 `title` 上;「显示更多」只是个标记,不算正文。"""
        got = x_guest_page.posts(html(article('alice', 7, 2, '看这个 ', more=True,
                                              links=[('example.com/a…', 'https://example.com/a/long')])))
        self.assertEqual(1, len(got))
        post = got[0]
        self.assertEqual(('alice', '7', 'alice 本人'), (post.author, post.ident, post.name))
        self.assertEqual('https://x.com/alice/status/7', post.url)
        self.assertEqual('看这个 https://example.com/a/long', post.text)
        self.assertTrue(post.truncated)
        self.assertAlmostEqual(time.time() - 2 * HOUR, post.when.timestamp(), delta=5)
        self.assertFalse(post.pinned)

    def test_an_edited_post_is_still_recognised(self):
        """编辑过的推文,时间挂在 `/history` 链接里的 `<time>` 上。认不出来它就从主页上悄悄消失了。"""
        post = x_guest_page.posts(html(article('alice', 8, 1, '改过的', edited=True), article('alice', 9, 2)))[0]
        self.assertEqual(('8', '改过的'), (post.ident, post.text))

    def test_a_link_shown_only_as_a_card_is_put_back_into_the_text(self):
        """末尾链接被渲染成卡片时正文里就没有它了;原文里写着的那条 t.co 要接回去,而且只接一次。"""
        got = x_guest_page.posts(html(article('alice', 7, 1, '新文章', card='https://t.co/CARD'),
                                      article('alice', 6, 2, '已带 https://t.co/CARD', card='https://t.co/CARD')))
        self.assertEqual(['新文章 https://t.co/CARD', '已带 https://t.co/CARD'], [p.text for p in got])

    def test_a_quote_does_not_lend_its_identity_or_its_words(self):
        quoted = article('bob', 3, 30, '别人的话')
        post = x_guest_page.posts(html(article('alice', 7, 1, '我的评论', quote=quoted)))[0]
        self.assertEqual(('alice', '7', '我的评论'), (post.author, post.ident, post.text))
        self.assertTrue(post.quoted)

    def test_the_pinned_mark_is_read(self):
        self.assertTrue(x_guest_page.posts(html(article('alice', 7, 500, pinned=True)))[0].pinned)

    def test_a_page_that_changed_shape_is_a_failure_not_an_empty_timeline(self):
        """有推文却一条都配不上时间,就是页面换了形状;一条推文都没有就是登录墙或限流。都不许返回空。"""
        no_stamp = article('alice', 7, 1).replace('__xClientTextFormatters', '__other')
        with self.assertRaisesRegex(x_guest_page.PageError, '换了形状'):
            x_guest_page.posts(html(no_stamp))
        with self.assertRaisesRegex(x_guest_page.PageError, '一条推文都没有'):
            x_guest_page.posts(html())


class FallbackScanTests(PageBase):
    def test_a_quiet_account_is_fully_covered_by_the_page_alone(self):
        """FxTwitter 问不到时,低频的号光凭主页露出来的那几条就能证明读到了下界。"""
        self.fx()
        self.page('alice', article('alice', 9, 1, '新的'), article('alice', 8, 3, '也新'),
                  article('alice', 7, 60), article('alice', 6, 61), article('alice', 5, 62))
        got = self.scan('alice')
        self.assertEqual(('', ''), (got['error'], got['incomplete']))
        self.assertEqual(['也新', '新的'], [e['body'] for e in got['entries']])
        self.assertIn('改读了 X 未登录主页', got['notice'])
        self.assertIn('HTTP 404', got['notice'])

    def test_an_old_pinned_post_is_not_proof_of_reaching_the_floor(self):
        """置顶的老推不计数:只有它加两条老推,还差一条才够,不许宣布读完(中间不夹新推 ——
        新推会把计数清零,那样置顶算不算都一样)。"""
        self.fx()
        self.page('alice', article('alice', 1, 900, '置顶', pinned=True),
                  article('alice', 7, 60), article('alice', 6, 61))
        got = self.scan('alice')
        self.assertIn('没读到这一轮的下界', got['incomplete'])
        self.assertEqual([], got['entries'])

    def test_a_busy_account_keeps_what_was_shown_and_says_it_is_incomplete(self):
        self.fx()
        self.page('alice', *[article('alice', 20 - n, n + 1, f'第 {n} 条') for n in range(5)])
        got = self.scan('alice')
        self.assertEqual('', got['error'])
        self.assertIn('只露出最新 5 条', got['incomplete'])
        self.assertEqual(5, len(got['entries']))

    def test_a_truncated_preview_is_replaced_by_the_detail_page(self):
        """预览落了库就 `DO NOTHING` 改不回来:截断的那条要拿详情页的全文。"""
        self.fx()
        self.page('alice', article('alice', 9, 1, '开头……', more=True),
                  *[article('alice', n, 60 + n) for n in (5, 4, 3)])
        self.detail('alice', 9, article('alice', 9, 1, '开头,然后是完整的后半段'), article('bob', 100, 0.5, '回复'))
        got = self.scan('alice')
        self.assertEqual('', got['incomplete'])
        self.assertEqual(['开头,然后是完整的后半段'], [e['body'] for e in got['entries']])

    def test_the_api_single_post_fills_in_when_the_detail_page_does_not(self):
        self.fx(single={('alice', '9'): '接口给的全文'})
        self.page('alice', article('alice', 9, 1, '开头……', more=True),
                  *[article('alice', n, 60 + n) for n in (5, 4, 3)])
        got = self.scan('alice')
        self.assertEqual(['接口给的全文'], [e['body'] for e in got['entries']])

    def test_with_neither_full_text_the_post_is_held_not_stored_as_a_preview(self):
        """两头都拿不到全文:这一条不收、写进 incomplete(水位不动,下一轮重扫时再补),别的照收。"""
        self.fx()
        self.page('alice', article('alice', 9, 1, '开头……', more=True), article('alice', 8, 2, '完整的短推'),
                  *[article('alice', n, 60 + n) for n in (5, 4, 3)])
        got = self.scan('alice')
        self.assertEqual(['完整的短推'], [e['body'] for e in got['entries']])
        self.assertIn('1 条推文的全文没取到', got['incomplete'])

    def test_with_both_tools_down_the_account_fails_and_both_reasons_are_kept(self):
        self.fx()
        got = self.scan('alice')
        self.assertEqual([], got['entries'])
        self.assertIn('公开接口没答复(订阅服务返回 HTTP 404)', got['error'])
        self.assertIn('改读 X 未登录主页也没读成', got['error'])

    def test_the_page_is_not_asked_while_the_api_answers(self):
        """反过来钉一边:主路好好的,不去敲 X 的页面。"""
        self.fx(timeline={'alice': [{'type': 'status', 'id': '9', 'url': 'https://x.com/alice/status/9',
                                     'text': '主路', 'created_timestamp': int(time.time() - HOUR),
                                     'author': {'screen_name': 'alice', 'name': 'alice'}}]})
        got = self.scan('alice')
        self.assertEqual((['主路'], ''), ([e['body'] for e in got['entries']], got['notice']))
        self.assertEqual([], self.pages.asked)


class FallbackCollectTests(PageBase):
    def test_a_sync_with_the_api_down_stores_from_the_page_and_tells_coverage_apart(self):
        quiet, busy = self.sub('quiet'), self.sub('busy')
        self.settings(x_channels.GUEST)
        self.fx()
        # 同步那条路的下界是五天前,老推要比它更老。
        self.page('quiet', article('quiet', 9, 1, '低频'), *[article('quiet', n, 130 + n) for n in (5, 4, 3)])
        self.page('busy', *[article('busy', 20 - n, n + 1, f'高频 {n}') for n in range(5)])
        summary = sync.collect('social', FetchScope())
        self.assertEqual([], summary['errors'])
        with reg.db() as c:
            stored = sorted(r['url'] for r in c.execute('SELECT url FROM feed_items'))
            rows = {r['source_id']: r for r in c.execute('SELECT * FROM x_intake_sources')}
        self.assertEqual(sorted(['https://x.com/quiet/status/9'] +
                                [f'https://x.com/busy/status/{20 - n}' for n in range(5)]), stored)
        self.assertEqual('complete', rows[quiet['id']]['discovery'])
        self.assertNotEqual('complete', rows[busy['id']]['discovery'])


class ProbeTests(PageBase):
    def test_either_tool_working_is_ready_and_both_verdicts_are_reported(self):
        self.fx()
        self.page('alice', article('alice', 9, 1))
        ok, text = x_guest.probe('alice')
        self.assertTrue(ok)
        self.assertIn('HTTP 404', text)
        self.assertIn('X 未登录主页可用(露出 1 条)', text)
        self.pages.pages.clear()
        ok, text = x_guest.probe('alice')
        self.assertFalse(ok)
        self.assertIn('X 页面没答复', text)


if __name__ == '__main__':
    unittest.main()
