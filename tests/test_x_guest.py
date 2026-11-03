# -*- coding: utf-8 -*-
"""不登录通道(`x_guest`):经 FxTwitter 公开接口读时间线,一路到 feeds.db。

只替掉最外面那一次 HTTP(`x_guest.request`,和长文那一步的 `x_article.fetch`),
翻页、归属、「读到下界」的证明、五天队列的暂存与确认、存盘事务都是真的。

钉的是后果:库里落了哪几条、这条订阅算不算成功、覆盖有没有被宣布读完 ——
不是「翻了几页」「调了几次」。

跑:  python -m unittest tests.test_x_guest -v
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_article, x_channels, x_guest
from kb.feeds.fetching import FeedError, Response
from tests.test_x_channels import ChannelBase

HOUR = 3600


def status(handle, ident, hours_ago, text='一条推文', **extra):
    """一条 FxTwitter 形状的推文。`ident` 用小整数,时间按「几小时前」给。"""
    return {'type': 'status', 'id': str(ident), 'url': f'https://x.com/{handle}/status/{ident}',
            'text': text, 'created_timestamp': int(time.time() - hours_ago * HOUR),
            'author': {'screen_name': handle, 'name': f'{handle} 本人'}} | extra


def repost(handle, other, ident, hours_ago):
    return status(other, ident, hours_ago, text='别人的话',
                  reposted_by={'screen_name': handle})


class FakeApi:
    """按 cursor 给页。`pages[i]` 是第 i 页的 results;值是异常时那一页每次都抛。"""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def __call__(self, url, headers=None, *, deadline=None, **_):
        handle = url.split('/profile/')[1].split('/')[0]
        index = int(url.split('cursor=p')[1]) if 'cursor=' in url else 0
        self.asked.append((handle, index))
        page = self.pages[handle][index]
        if isinstance(page, Exception):
            raise page
        more = index + 1 < len(self.pages[handle])
        body = {'code': 200, 'results': page, 'cursor': {'bottom': f'p{index + 1}' if more else None}}
        return Response(200, url, json.dumps(body).encode(), {})


HTTP_404 = FeedError('订阅服务返回 HTTP 404；请检查来源或填写专用订阅链接')


class GuestBase(ChannelBase):
    def setUp(self):
        super().setUp()
        for p in (patch.object(x_guest, 'RETRY_PAUSE', 0), patch.object(x_guest, 'PAGE_PAUSE', 0)):
            p.start(); self.addCleanup(p.stop)

    def api(self, **pages):
        fake = FakeApi(pages)
        p = patch.object(x_guest, 'request', fake)
        p.start(); self.addCleanup(p.stop)
        return fake

    def scan(self, handle, floor_hours=48):
        floor = x_guest._floor(time.strftime('%Y-%m-%dT%H:%M:%S+00:00',
                                             time.gmtime(time.time() - floor_hours * HOUR)))
        return x_guest.scan(handle, floor, deadline=time.monotonic() + 30)


class NoLoginTests(GuestBase):
    def test_the_channel_is_ready_with_no_login_anywhere(self):
        """**这一档存在的全部理由**:没有 profile、没有 cookie、没有调试端口,照样就绪;
        订阅也不会被记成「待配置」跳过。反过来钉一边:同样的空环境下默认那档仍要登录。"""
        source = self.sub('alice')
        self.settings(x_channels.GUEST)
        settings = reg.get_settings('social')
        self.assertEqual((True, '', ''), x_channels.readiness(settings))
        self.assertEqual('', reg.configuration_error(source, settings))
        self.settings(x_channels.BROWSER)
        self.assertFalse(x_channels.readiness(reg.get_settings('social'))[0])

    def test_the_panel_lists_it_and_offers_no_login_step(self):
        self.settings(x_channels.GUEST)
        data = self.client.get('/api/intake/social/x-channel').json()
        self.assertIn(x_channels.GUEST, [c['id'] for c in data['channels']])
        self.assertEqual(x_channels.GUEST, data['current'])
        self.assertTrue(data['ready'])
        self.assertEqual('', data['login_command'])
        self.assertFalse(data['needs_login_window'])


class ScanTests(GuestBase):
    def test_reads_down_to_the_floor_and_keeps_only_his_own_new_posts(self):
        self.api(alice=[
            [status('alice', 30, 1, '最新'), repost('alice', 'bob', 29, 2),
             status('bob', 28, 3, '推荐位'), status('alice', 27, 5, '', quote={'text': '别人'}),
             status('alice', 26, 6, '', article={'id': '900'})],
            [status('alice', 25, 10, '第二页'), status('alice', 24, 60), status('alice', 23, 61),
             status('alice', 22, 62)],
            [status('alice', 21, 1, '不该翻到这一页')],
        ])
        got = self.scan('alice')
        self.assertEqual('', got['error'])
        self.assertEqual('', got['incomplete'])
        urls = [e['url'] for e in got['entries']]
        self.assertEqual(['https://x.com/alice/status/25', 'https://x.com/alice/status/26',
                          'https://x.com/alice/status/30'], urls)
        self.assertEqual(1, got['skipped_quotes'])
        article = next(e for e in got['entries'] if e['url'].endswith('/26'))
        self.assertTrue(x_article.needs_article(article))
        self.assertEqual('alice 本人 的动态', article['title'])

    def test_one_old_post_at_the_top_does_not_end_the_scan(self):
        """置顶那种:最上面一条老推不是「读到底」的证据,下面的新推一条不能少。"""
        self.api(alice=[[status('alice', 9, 500, '置顶'), status('alice', 8, 1, '新')],
                        [status('alice', 7, 2, '更早但仍新')]])
        got = self.scan('alice')
        self.assertEqual('', got['incomplete'])
        self.assertEqual({'https://x.com/alice/status/7', 'https://x.com/alice/status/8'},
                         {e['url'] for e in got['entries']})

    def test_a_first_page_404_is_a_failure_not_an_empty_timeline(self):
        """偶发 404 是这个接口的常态。当成「没有推文」就是这条订阅从此安静地空着。"""
        fake = self.api(alice=[HTTP_404])
        got = self.scan('alice')
        self.assertIn('HTTP 404', got['error'])
        self.assertEqual([], got['entries'])
        self.assertEqual(x_guest.RETRIES, len(fake.asked))

    def test_a_legitimately_empty_timeline_is_a_complete_empty_read(self):
        """反过来钉一边:接口好好答了「没有」,那就是读完了、零条,不是故障。"""
        self.api(alice=[[]])
        got = self.scan('alice')
        self.assertEqual(('', '', []), (got['error'], got['incomplete'], got['entries']))

    def test_results_that_cannot_be_read_are_a_failure(self):
        self.api(alice=[[{'id': '1'}, {'text': 'x'}]])
        self.assertIn('换了形状', self.scan('alice')['error'])

    def test_a_failure_on_a_later_page_keeps_what_was_read_and_says_so(self):
        self.api(alice=[[status('alice', 5, 1)], HTTP_404])
        got = self.scan('alice')
        self.assertEqual('', got['error'])
        self.assertIn('第 2 页', got['incomplete'])
        self.assertEqual(['https://x.com/alice/status/5'], [e['url'] for e in got['entries']])

    def test_running_out_of_pages_is_incomplete(self):
        with patch.object(x_guest, 'MAX_PAGES', 1):
            self.api(alice=[[status('alice', 5, 1)], [status('alice', 4, 2)]])
            got = self.scan('alice')
        self.assertIn('翻了 1 页', got['incomplete'])


class CollectTests(GuestBase):
    def collect(self):
        return sync.collect('social', FetchScope())

    def stored(self):
        with reg.db() as c:
            return sorted(r['url'] for r in c.execute('SELECT url FROM feed_items'))

    def test_a_daily_sync_stores_posts_and_proves_coverage_without_any_login(self):
        alice, bob = self.sub('alice'), self.sub('bob')
        self.settings(x_channels.GUEST)
        self.api(alice=[[status('alice', 3, 1, '甲'), status('alice', 2, 2, '乙')]],
                 bob=[[status('bob', 4, 3, '丙')]])
        summary = self.collect()
        self.assertEqual([], summary['errors'])
        self.assertEqual(0, summary['unconfigured'])
        self.assertEqual(['https://x.com/alice/status/2', 'https://x.com/alice/status/3',
                          'https://x.com/bob/status/4'], self.stored())
        with reg.db() as c:
            rows = {r['source_id']: r for r in c.execute('SELECT * FROM x_intake_sources')}
            subs = {r['id']: r for r in c.execute('SELECT * FROM subscriptions')}
        for source in (alice, bob):
            self.assertEqual(x_channels.GUEST, rows[source['id']]['channel'])
            self.assertEqual('complete', rows[source['id']]['discovery'])
            self.assertEqual('', rows[source['id']]['gap'])
            self.assertTrue(subs[source['id']]['last_success'])
            self.assertEqual('', subs[source['id']]['last_error'])

    def test_an_account_the_api_will_not_answer_fails_alone(self):
        alice, bob = self.sub('alice'), self.sub('bob')
        self.settings(x_channels.GUEST)
        self.api(alice=[HTTP_404], bob=[[status('bob', 4, 3, '丙')]])
        self.collect()
        self.assertEqual(['https://x.com/bob/status/4'], self.stored())
        with reg.db() as c:
            subs = {r['id']: r for r in c.execute('SELECT * FROM subscriptions')}
            rows = {r['source_id']: r for r in c.execute('SELECT * FROM x_intake_sources')}
        self.assertIn('HTTP 404', subs[alice['id']]['last_error'])
        self.assertFalse(subs[alice['id']]['last_success'])
        self.assertNotEqual('complete', rows[alice['id']]['discovery'])
        self.assertTrue(subs[bob['id']]['last_success'])

    def test_an_article_post_is_held_until_its_body_arrives(self):
        """长文正文没取到时不许拿空正文落库(落了就 `DO NOTHING` 改不回来);取到之后照常入库。"""
        self.sub('alice')
        self.settings(x_channels.GUEST)
        self.api(alice=[[status('alice', 6, 1, '', article={'id': '900'})]])
        with patch.object(x_article, 'fetch', side_effect=x_article.ArticleUnavailable('接口挂了')):
            summary = self.collect()
        self.assertEqual([], self.stored())
        self.assertEqual(['1 条正文待补'], [p['reason'] for p in summary['partial']])
        with reg.db() as c:                       # 正文待办的退避过去了
            c.execute('UPDATE x_intake_candidates SET retry_at=0')
        with patch.object(x_article, 'fetch', return_value={'title': '长文标题', 'body': '长文正文'}):
            self.collect()
        with reg.db() as c:
            row = c.execute('SELECT title, body FROM feed_items').fetchone()
        self.assertEqual(('长文标题', '长文正文'), (row['title'], row['body']))


if __name__ == '__main__':
    unittest.main()
