# -*- coding: utf-8 -*-
"""不登录通道遇到读不懂的回包、慢接口时的后果(`x_guest`、`x_guest_page`)。

三件事,判据都落在「这一轮算不算读全了」「库里有哪几条」「别的号还读不读」:
- **认不出来的条目让这一轮证明不了读全**:两件工具一样,一部分条目读不懂时认得出来的照收,但
  `incomplete` 非空、五天队列不许记成完整覆盖。反过来钉一边:明确不收的(只有引用、转推、别人的)
  照旧安静地不收,覆盖照常算完整(`test_x_guest` 的 `test_reads_down_to_the_floor…` 钉着)。
- **怪形状、怪数值只坏这一个号**:游标不是对象、时间戳是 NaN / 无穷 / 超出范围,一律当成这一页
  没问到或这一条认不出来,不许抛出去打断后面的账号。
- **公开接口慢到超时,后路照样轮得上**:第一页最多问到账号时限减掉给后路留的那一份,后路用剩下
  的,不另外加时。

只替掉最外面那一次 HTTP,其余都是真的。

跑:  python -m unittest tests.test_x_guest_shape -v
"""
import json
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_channels, x_guest, x_guest_page
from kb.feeds.fetching import FeedError, Response
from tests.test_x_guest import GuestBase, status
from tests.test_x_guest_page import PAGE, PageBase, article, html

UNREADABLE = '推文认不出来'


def old(handle, *idents):
    """不新于下界的(同步那条路的下界是五天前,要比它更老),三条就证明读到了底。"""
    return [status(handle, n, 130 + n) for n in idents]


class RawApi:
    """FxTwitter 替身,原样回 `bodies[handle]`(不替它补 cursor,好让形状不对的游标漏得进来)。"""

    def __init__(self, bodies):
        self.bodies, self.asked = bodies, []

    def __call__(self, url, headers=None, *, deadline=None, **_):
        handle = url.split('/profile/')[1].split('/')[0] if '/profile/' in url else ''
        self.asked.append(handle)
        if handle not in self.bodies:
            raise FeedError('订阅服务返回 HTTP 404')
        return Response(200, url, json.dumps(self.bodies[handle]).encode(), {})


class ShapeBase(GuestBase):
    def raw(self, **bodies):
        fake = RawApi(bodies)
        p = patch.object(x_guest, 'request', fake)
        p.start(); self.addCleanup(p.stop)
        return fake

    def stored(self):
        with reg.db() as c:
            return sorted(r['url'] for r in c.execute('SELECT url FROM feed_items'))

    def progress(self):
        with reg.db() as c:
            return {r['source_id']: r for r in c.execute('SELECT * FROM x_intake_sources')}

    def subs(self):
        with reg.db() as c:
            return {r['id']: r for r in c.execute('SELECT * FROM subscriptions')}


class ApiShapeTests(ShapeBase):
    def test_one_unreadable_post_among_good_ones_voids_the_coverage_proof(self):
        """时间戳换成了数字字符串:认得出来的那条照收,但这一轮不许算读全。"""
        bad = status('alice', 8, 2, '读不懂的那条')
        bad['created_timestamp'] = str(bad['created_timestamp'])
        self.api(alice=[[status('alice', 9, 1, '好的'), bad, *old('alice', 3, 2, 1)]])
        got = self.scan('alice')
        self.assertEqual('', got['error'])
        self.assertEqual(['好的'], [e['body'] for e in got['entries']])
        self.assertIn(f'1 条{UNREADABLE}', got['incomplete'])

    def test_his_own_post_without_an_address_is_unreadable_not_a_quote(self):
        """地址认不出来跟「只有引用」不是一回事:前者让覆盖失效,也不记进「引用没收」。"""
        nameless = status('alice', 8, 2, '没有地址') | {'id': 'x8', 'url': ''}
        self.api(alice=[[status('alice', 9, 1, '好的'), nameless, *old('alice', 3, 2, 1)]])
        got = self.scan('alice')
        self.assertIn(f'1 条{UNREADABLE}', got['incomplete'])
        self.assertEqual(0, got['skipped_quotes'])

    def test_odd_numbers_in_timestamps_are_unreadable_not_a_crash(self):
        """NaN、无穷、超出能表示的范围、布尔:都是这一条认不出来,不许把异常抛出这个号。"""
        odd = [status('alice', 20 + n, 1) | {'created_timestamp': value}
               for n, value in enumerate([float('nan'), float('inf'), 1e300, 10 ** 30, True, 10 ** 400])]
        self.api(alice=[[status('alice', 9, 1, '好的'), *odd, *old('alice', 3, 2, 1)]])
        got = self.scan('alice')
        self.assertEqual(['好的'], [e['body'] for e in got['entries']])
        self.assertIn(f'6 条{UNREADABLE}', got['incomplete'])

    def test_an_author_that_names_no_valid_account_is_unreadable_not_someone_else(self):
        """作者账号名是空串、不合法,或者转推者形状不对:说不清是谁,就不许凭它判「不是他本人、不收」。"""
        unclear = [status('alice', 20, 1) | {'author': {'screen_name': '', 'name': 'alice'}},
                   status('alice', 21, 1) | {'author': {'screen_name': 'a l i c e', 'name': 'alice'}},
                   status('alice', 22, 1, reposted_by='alice'),
                   status('alice', 23, 1, reposted_by={'screen_name': ''})]
        self.api(alice=[[status('alice', 9, 1, '好的'), *unclear, *old('alice', 3, 2, 1)]])
        got = self.scan('alice')
        self.assertEqual(['好的'], [e['body'] for e in got['entries']])
        self.assertIn(f'4 条{UNREADABLE}', got['incomplete'])

    def test_only_a_null_bottom_cursor_means_the_last_page(self):
        """0、false、[]、{}、空串、缺键、缺 cursor 都不是「翻到尽头」:第一页按没问到、改读主页。
        反过来钉一边:`bottom` 是 null 就是读完了,覆盖照常成立、不碰主页。"""
        self.pages.pages[PAGE.format('alice')] = html(article('alice', 9, 1, '主页上的'),
                                                      *[article('alice', n, 130 + n) for n in (3, 2, 1)])
        cursors = {'0': {'bottom': 0}, 'false': {'bottom': False}, '[]': {'bottom': []}, '{}': {'bottom': {}},
                   '空串': {'bottom': ''}, '缺键': {}, '缺 cursor': None}
        for name, cursor in cursors.items():
            with self.subTest(name):
                body = {'code': 200, 'results': [status('alice', 9, 1, '接口给的')]}
                self.raw(alice=body if cursor is None else body | {'cursor': cursor})
                got = self.scan('alice')
                self.assertIn('翻页标记认不出来', got['notice'])
                self.assertEqual(['主页上的'], [e['body'] for e in got['entries']])
        self.pages.asked.clear()
        self.raw(alice={'code': 200, 'results': [status('alice', 9, 1, '接口给的')], 'cursor': {'bottom': None}})
        got = self.scan('alice')
        self.assertEqual(('', '', ['接口给的']), (got['notice'], got['incomplete'], [e['body'] for e in got['entries']]))
        self.assertEqual([], self.pages.asked)

    def test_a_falsy_cursor_on_a_later_page_leaves_the_round_incomplete(self):
        """第二页游标给个 0:收下前两页,但不许把它当成末页宣布读完。"""
        bodies = iter([{'code': 200, 'results': [status('alice', 9, 1)], 'cursor': {'bottom': 'p1'}}]
                      + [{'code': 200, 'results': [status('alice', 8, 2)], 'cursor': {'bottom': 0}}] * x_guest.RETRIES)

        def reply(url, headers=None, *, deadline=None, **_):
            return Response(200, url, json.dumps(next(bodies)).encode(), {})

        with patch.object(x_guest, 'request', reply):
            got = self.scan('alice')
        self.assertEqual(['https://x.com/alice/status/9'], [e['url'] for e in got['entries']])
        self.assertIn('第 2 页', got['incomplete'])

    def test_a_cursor_of_the_wrong_shape_is_an_unanswered_page_not_a_crash(self):
        """游标是个数组:第一页就当成没问到(于是改读 X 主页),不许 `.get` 炸在外面。"""
        self.raw(alice={'code': 200, 'results': [status('alice', 9, 1)], 'cursor': ['p1']})
        self.pages.pages[PAGE.format('alice')] = html(article('alice', 9, 1, '主页上的'),
                                                      *[article('alice', n, 130 + n) for n in (3, 2, 1)])
        got = self.scan('alice')
        self.assertEqual('', got['error'])
        self.assertIn('翻页标记认不出来', got['notice'])
        self.assertEqual(['主页上的'], [e['body'] for e in got['entries']])

    def test_the_connection_check_reports_a_malformed_reply_instead_of_raising(self):
        self.raw(alice={'code': 200, 'results': [], 'cursor': {'bottom': 7}})
        self.pages.pages[PAGE.format('alice')] = html(article('alice', 9, 1))
        ok, text = x_guest.probe('alice')
        self.assertTrue(ok)
        self.assertIn('翻页标记认不出来', text)


class ShapeCollectTests(ShapeBase):
    def collect(self):
        return sync.collect('social', FetchScope())

    def test_partial_unreadable_is_not_recorded_as_complete_coverage(self):
        """判据落在五天队列那一行:认不出来一条的号不是 complete;同一轮干净的号照旧是。"""
        alice, bob = self.sub('alice'), self.sub('bob')
        self.settings(x_channels.GUEST)
        bad = status('alice', 8, 2) | {'author': {'name': '没有账号名'}}
        self.api(alice=[[status('alice', 9, 1, '甲'), bad, *old('alice', 3, 2, 1)]],
                 bob=[[status('bob', 19, 1, '乙'), *old('bob', 13, 12, 11)]])
        self.collect()
        self.assertEqual(['https://x.com/alice/status/9', 'https://x.com/bob/status/19'], self.stored())
        rows = self.progress()
        self.assertNotEqual('complete', rows[alice['id']]['discovery'])
        self.assertEqual('complete', rows[bob['id']]['discovery'])

    def test_one_account_with_a_malformed_reply_does_not_stop_the_others(self):
        """排在前面的号一个游标怪、一个时间戳是几百位的整数,后面的号照样读、照样入库。"""
        alice, carol, bob = self.sub('alice'), self.sub('carol'), self.sub('bob')
        self.settings(x_channels.GUEST)
        giant = status('carol', 28, 1) | {'created_timestamp': 10 ** 400}
        self.raw(alice={'code': 200, 'results': [status('alice', 9, 1)], 'cursor': ['p1']},
                 carol={'code': 200, 'results': [status('carol', 29, 1, '丙'), giant], 'cursor': {'bottom': None}},
                 bob={'code': 200, 'results': [status('bob', 19, 1, '乙')], 'cursor': {'bottom': None}})
        self.collect()
        self.assertEqual(['https://x.com/bob/status/19', 'https://x.com/carol/status/29'], self.stored())
        subs, rows = self.subs(), self.progress()
        self.assertIn('翻页标记认不出来', subs[alice['id']]['last_error'])
        self.assertNotEqual('complete', rows[carol['id']]['discovery'])
        self.assertTrue(subs[bob['id']]['last_success'])
        # 区间补抓那条路(`harvest` 逐号读)同一个口径:坏的那个号如实记,后面的号照样有结果。
        got = x_guest.harvest([('carol', ''), ('bob', '')], complete_articles=False)
        self.assertIn(UNREADABLE, got['carol']['incomplete'])
        self.assertEqual(['https://x.com/bob/status/19'], [e['url'] for e in got['bob']['entries']])


class PageShapeTests(PageBase):
    def test_an_unreadable_article_among_readable_ones_voids_the_proof(self):
        """主页上一条新推配不上时间,另外三条老推照样能凑够「到下界」——这一轮不许因此算读全。"""
        self.fx()
        no_stamp = article('alice', 10, 1, '看不出时间').replace('__xClientTextFormatters', '__other')
        self.page('alice', no_stamp, *[article('alice', n, 130 + n) for n in (3, 2, 1)])
        shown, unreadable = x_guest_page.posts(self.pages.pages[PAGE.format('alice')])
        self.assertEqual((3, 1), (len(shown), unreadable))
        got = self.scan('alice')
        self.assertEqual('', got['error'])
        self.assertIn(f'X 未登录主页上有 1 条{UNREADABLE}', got['incomplete'])

    def test_out_of_range_timestamps_are_unreadable_not_a_crash(self):
        def stamped(ident, value):
            return re.sub(r'"timestamp":\d+', f'"timestamp":{value}', article('alice', ident, 1))
        page = html(article('alice', 9, 1), stamped(8, '1e300'), stamped(7, 10 ** 30), stamped(6, 'NaN'),
                    stamped(5, 10 ** 400))
        shown, unreadable = x_guest_page.posts(page)
        self.assertEqual((['9'], 4), ([p.ident for p in shown], unreadable))
        with self.assertRaisesRegex(x_guest_page.PageError, '换了形状'):
            x_guest_page.posts(html(stamped(8, '1e300')))


class BudgetTests(GuestBase):
    def test_a_slow_api_still_leaves_the_page_its_turn_within_the_account_limit(self):
        """接口每次都慢到截止才放弃;X 主页能打开。后路要真的问得到,而且用的是同一个账号截止。"""
        def slow(url, headers=None, *, deadline=None, **_):
            time.sleep(max(0.0, deadline - time.monotonic()))
            raise FeedError('订阅读取超时')

        seen = []

        def page(url, deadline):
            seen.append(deadline)
            if time.monotonic() >= deadline:              # 同生产传输:截止已过就不连了
                raise x_guest_page.PageError('X 页面没答复(订阅读取超时)')
            return html(article('alice', 9, 1, '主页上的'), *[article('alice', n, 130 + n) for n in (3, 2, 1)])

        with patch.object(x_guest, 'request', slow), patch.object(x_guest_page, 'fetch', page), \
                patch.object(x_guest, 'PAGE_RESERVE', 1.0):
            start = time.monotonic()
            limit = start + 3.0
            got = x_guest.scan('alice', x_guest._floor(''), deadline=limit)
            spent = time.monotonic() - start
        self.assertEqual('', got['error'])
        self.assertEqual(['主页上的'], [e['body'] for e in got['entries']])
        self.assertEqual([limit], seen)                   # 不另外加时
        self.assertLess(spent, 3.0)


if __name__ == '__main__':
    unittest.main()
