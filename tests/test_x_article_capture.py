# -*- coding: utf-8 -*-
"""人工补 X 长文(`kb.feeds.x_article_capture` 和它那三个端点):人在自己的浏览器里打开长文、点扩展。

自动同步拿不到长文正文的推文作为候选挂在 X 五天队列里,不入库。这里钉的是后果:
- 「问题与待办」列不列得出它、给的是不是那篇长文的页面;
- 扩展交回那一页的快照之后,**下一轮同步库里那一条是什么**(标题、正文);
- 不是那一篇、还没显示出正文、已经不在等的那几种,**一个字都不写**(候选原样、库里没有)。

扩展那一侧不判任何东西,所以这里直接拿读页脚本会交回的快照形状喂端点。

跑:  python -m unittest tests.test_x_article_capture -v
"""
import io
import json
import sys
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import browser_capture
from kb.collectors import FetchScope
from kb.feeds import grab, registry as reg, sync, x_article, x_channels, x_post
from kb.feeds import x_article_capture, x_intake_store as store
from kb.feeds.x_article_js import SOURCE
from kb.web import browser_capture_api, grab_api, x_article_capture_api
from tests.test_x_guest import GuestBase, status

ARTICLE = 'https://x.com/alice/article/6'
STATUS = 'https://x.com/alice/status/6'


def snapshot(path='/alice/article/6', blocks=('第一段', '第二段'), **extra):
    """读页脚本(`x_article_read.js`)在长文页上交回的那一份。"""
    return {'at': '/article/' in path, 'back': '/status/' in path, 'path': path,
            'blocks': list(blocks), 'title': '长文标题'} | extra


class CaptureBase(GuestBase):
    def setUp(self):
        super().setUp()
        self.source = self.sub('alice')
        self.settings(x_channels.GUEST)
        self.api(alice=[[status('alice', 6, 1, '', article={'id': '900'}), status('alice', 5, 2, '普通推文')]])
        # 公开接口那一步一直问不到长文 —— 这正是要人手动补的那种。
        p = patch.object(x_article, 'fetch', side_effect=x_article.ArticleUnavailable('接口挂了'))
        p.start(); self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(x_article_capture_api.router)
        app.include_router(browser_capture_api.router)
        self.web = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 54321),
                              headers={'Sec-Fetch-Site': 'same-origin'})
        self.auth = {'Authorization': 'Bearer ' + browser_capture.pairing_key(),
                     'Origin': 'chrome-extension://fixture'}
        self.collect()

    def collect(self):
        return sync.collect('social', FetchScope())

    def stored(self):
        with reg.db() as c:
            return {r['url']: (r['title'], r['body']) for r in c.execute('SELECT url, title, body FROM feed_items')}

    def candidate(self):
        with reg.db() as c:
            return dict(c.execute("SELECT * FROM x_intake_candidates WHERE post_id='6'").fetchone())

    def here(self, url, auth=True):
        return self.web.post('/api/x-article-capture/here', json={'url': url}, headers=self.auth if auth else {})

    def save(self, page_url=ARTICLE, snap=None, post_id='6'):
        return self.web.post('/api/x-article-capture/save', headers=self.auth,
                             json={'post_id': post_id, 'page_url': page_url, 'snapshot': snap or snapshot()})


class ListTests(CaptureBase):
    def test_an_article_the_sync_could_not_complete_is_listed_with_its_article_page(self):
        """没入库、也没出现在文章那一侧 —— 只有这张列表把它交到人手上。普通推文照常入库,不在列表里。"""
        self.assertEqual(['https://x.com/alice/status/5'], list(self.stored()))
        items = self.web.get('/api/x-article-capture/owed', params={'source_id': self.source['id']}).json()['items']
        self.assertEqual([('6', ARTICLE, STATUS)], [(i['post_id'], i['article_url'], i['url']) for i in items])
        self.assertEqual([], self.web.get('/api/x-article-capture/owed',
                                          params={'source_id': self.source['id'] + 99}).json()['items'])

    def test_the_page_question_answers_what_this_page_can_do(self):
        self.assertEqual('fill', self.here(ARTICLE).json()['situation'])
        self.assertEqual('6', self.here(ARTICLE + '?s=20').json()['post']['post_id'])
        self.assertEqual('open_article', self.here(STATUS).json()['situation'])
        self.assertEqual('absent', self.here('https://x.com/alice/article/7').json()['situation'])
        self.assertEqual('other', self.here('https://x.com/alice').json()['situation'])
        self.assertEqual('stored', self.here('https://x.com/alice/status/5').json()['situation'])

    def test_a_post_nobody_is_waiting_for_is_handed_to_the_manual_grab(self):
        """库里没有、同步也没在收的那一条:给手动收录的地址(长文页也换成它所属的推文)。

        扩展在这一档摆「采集」,交的就是 `grab_url`。反过来钉一边:在等长文的那一条不给这个地址
        (下一步是读页补正文,不是另收一条)。
        """
        absent = self.here('https://twitter.com/Alice/article/7?s=20').json()
        self.assertEqual(('absent', 'https://x.com/Alice/status/7'), (absent['situation'], absent['grab_url']))
        self.assertNotIn('grab_url', self.here(ARTICLE).json())
        self.assertNotIn('grab_url', self.here(STATUS).json())

    def test_a_manually_grabbed_post_is_stored_only_once_the_rest_of_the_chain_ran(self):
        """收件桶里有这一行,只说明**暂存下来了**;导出、索引走没走完由 `grab_store` 一份口径答。

        **判据落在那一问的档位和出口上**:没走完时答 `unfinished`、带上库里那一行的地址(扩展拿它重投,
        `/api/grab` 的重复那一档补上欠的那一步),话里说欠哪一步;说成「已经在知识库里了」的话,
        人搜不到、扩展又一颗按钮都不给,后半条链永远没人补。反过来钉一边:走完了照旧答 `stored`。
        """
        grab.save({'url': 'https://x.com/Alice/status/7', 'title': '收下的推文', 'body': '推文正文',
                   'author': 'Alice', 'published': '', 'content_kind': 'post'})
        state = self.here('https://x.com/alice/article/7').json()
        self.assertEqual(('unfinished', 'https://x.com/Alice/status/7'), (state['situation'], state['grab_url']))
        self.assertIn('还没导出成笔记文件', state['detail'])
        done = dict(content_ok=True, exported=True, stored=True, indexed=True, done=True)
        with patch.object(x_article_capture.grab_store, 'known', lambda url: done):
            self.assertEqual('stored', self.here('https://x.com/alice/status/7').json()['situation'])

    def test_another_site_with_the_same_status_tail_is_not_this_post(self):
        """收件桶什么站点都收:`https://other.example/status/<同一串数字>` 不是这条推文。

        只比地址尾巴的话,扩展说「已经在知识库里了」、把「采集」藏掉,而这条推文一次都没收过。
        """
        grab.save({'url': 'https://other.example/status/7', 'title': '别处的页面', 'body': '别处的正文',
                   'author': '', 'published': '', 'content_kind': 'content'})
        self.assertEqual('absent', self.here('https://x.com/alice/status/7').json()['situation'])

    def test_a_post_the_sync_still_holds_is_not_collected_a_second_time(self):
        """同步还攥着的推文不另收一份 —— 不只是在等长文的那一撮。

        长推展开那一种(`pending`,等的不是长文)、正文齐了还没投递的(`ready`),下一轮同步都会投进
        「大V观点」;这时手动收录落进收件桶,两类的键不同,同一条推文就成了两行。**判据落在库里**:
        扩展那一问不给 `grab_url`,手动收录那个入口拒掉、收件桶里一行都没有。
        反过来钉一边:候选已经过期(下一轮不会投)时照旧给「采集」。
        """
        with reg.db() as c:
            entry = json.loads(c.execute("SELECT entry FROM x_intake_candidates WHERE post_id='6'").fetchone()[0])
            entry[x_post.HINT] = entry['url']
            c.execute("UPDATE x_intake_candidates SET entry=? WHERE post_id='6'", (json.dumps(entry, ensure_ascii=False),))
        state = self.here(STATUS).json()
        self.assertEqual('syncing', state['situation'], '等的不是长文,也是同步攥着的')
        self.assertNotIn('grab_url', state)
        app = FastAPI()
        app.include_router(grab_api.router)
        manual = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 54321),
                            headers={'Sec-Fetch-Site': 'same-origin'})
        with patch.object(grab, 'grab', side_effect=AssertionError('不该去抓')):
            refused = manual.post('/api/grab', json={'url': STATUS})
        self.assertEqual(409, refused.status_code, refused.text)
        with reg.db() as c:
            self.assertEqual([], c.execute("SELECT url FROM feed_items WHERE kind='inbox'").fetchall())

        with reg.db() as c:
            c.execute("UPDATE x_intake_candidates SET state='ready' WHERE post_id='6'")
        self.assertEqual('syncing', self.here(ARTICLE).json()['situation'], '正文齐了、还没投递')
        with reg.db() as c:
            c.execute("UPDATE x_intake_candidates SET state='expired' WHERE post_id='6'")
        self.assertEqual('absent', self.here(STATUS).json()['situation'], '下一轮不会投了,就该给采集')

    def test_the_extension_endpoints_need_the_pairing_key(self):
        """反过来钉一边:带着密钥照旧放行(否则「一律拒」也能让这条绿)。"""
        self.assertEqual(401, self.here(ARTICLE, auth=False).status_code)
        self.assertEqual(401, self.web.post('/api/x-article-capture/save',
                                            json={'post_id': '6', 'page_url': ARTICLE, 'snapshot': snapshot()}).status_code)
        self.assertEqual(200, self.here(ARTICLE).status_code)


class SaveTests(CaptureBase):
    def test_the_article_read_from_the_page_is_stored_on_the_next_sync(self):
        """判据落在库里那一条:标题和正文是那一页的,而公开接口那一步这时仍然问不到。"""
        r = self.save()
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual('长文标题', r.json()['title'])
        self.assertEqual('ready', self.candidate()['state'])       # 不等自动重试的冷却,下一轮扫描也不会重写它
        self.collect()
        self.assertEqual(('长文标题', '第一段\n\n第二段'), self.stored()[STATUS])
        self.assertEqual('stored', self.here(ARTICLE).json()['situation'])
        self.assertEqual([], self.web.get('/api/x-article-capture/owed').json()['items'])

    def test_a_page_that_does_not_prove_it_is_that_article_writes_nothing(self):
        """每一种拒绝都是 409、候选原样、下一轮库里照旧没有它。"""
        before = self.candidate()['entry']
        cases = {
            '另一篇长文的页面': dict(page_url='https://x.com/alice/article/7'),
            '推文页': dict(page_url=STATUS, snap=snapshot('/alice/status/6')),
            '地址对、页面其实停在另一篇上': dict(snap=snapshot('/alice/article/7')),
            '还没显示出正文': dict(snap=snapshot(blocks=())),
            '正文块不是文字': dict(snap=snapshot(blocks=('第一段', {'x': 1}))),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self.assertEqual(409, self.save(**kw).status_code)
                self.assertEqual(before, self.candidate()['entry'])
        self.collect()
        self.assertNotIn(STATUS, self.stored())

    def test_a_stale_link_of_a_repointed_subscription_does_not_block_the_live_one(self):
        """订阅 A 改指了别的账号、还没同步(它那条旧关联和旧进度都还在);同一个账号又加成了订阅 B。
        人补的正文要经 B 写进去 —— 挑中 A 的话 `finish` 找不到它现在的进度,一个字都不写。"""
        reg.save_subscription('social', {'url': 'https://x.com/carol'}, self.source['id'])
        again = self.sub('alice', name='alice 又一次')
        identity = store.identity(reg.subscription('social', again['id']))
        with reg.db() as c:
            row = c.execute('SELECT channel, floor FROM x_intake_sources WHERE source_id=?',
                            (self.source['id'],)).fetchone()
            c.execute('INSERT INTO x_intake_sources(source_id, identity, channel, floor) VALUES (?,?,?,?)',
                      (again['id'], identity, row['channel'], row['floor']))
            c.execute("INSERT INTO x_intake_links(source_id, identity, post_id) VALUES (?,?,'6')",
                      (again['id'], identity))
        r = self.save()
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual('ready', self.candidate()['state'])

    def test_an_edited_subscription_stops_offering_the_page_until_it_syncs_again(self):
        """列表、「这一页能不能补」和交回过同一份判据:订阅编辑过(身份带着 rev)、还没同步时,扩展不该
        说「可以补」、交回时才拒。反过来钉一边:下一轮同步之后它照旧列得出来、交得回去。"""
        reg.save_subscription('social', {'name': 'alice 改了名'}, self.source['id'])
        self.assertEqual([], self.web.get('/api/x-article-capture/owed').json()['items'])
        self.assertNotEqual('fill', self.here(ARTICLE).json()['situation'])
        self.assertEqual(409, self.save().status_code)
        self.collect()
        self.assertEqual(['6'], [i['post_id'] for i in self.web.get('/api/x-article-capture/owed').json()['items']])
        self.assertEqual('fill', self.here(ARTICLE).json()['situation'])
        self.assertEqual(200, self.save().status_code)

    def test_a_post_that_no_longer_waits_is_not_written_again(self):
        """已经补上并入库的,再交一份(哪怕是另一份正文)也不许改它。"""
        self.assertEqual(200, self.save().status_code)
        self.collect()
        r = self.save(snap=snapshot(blocks=('另一份正文',)))
        self.assertEqual(409, r.status_code)
        self.assertIn('已经不在等长文', r.json()['detail'])
        self.assertEqual(('长文标题', '第一段\n\n第二段'), self.stored()[STATUS])


class LockWaitTests(CaptureBase):
    """人点了按钮在等:维护锁只等一小会儿(`x_article_capture._LOCK_WAIT`),短暂攥着的照常写进去,
    等不到就 409、如实说一句、候选原样。两边各钉一次。"""

    def hold(self, seconds):
        from tests.test_x_intake_lock_wait import hold_maintenance_lock
        worker = hold_maintenance_lock(seconds)
        self.addCleanup(worker.join, 5)

    def test_a_briefly_held_lock_is_waited_for(self):
        self.hold(0.3)
        with patch.object(x_article_capture, '_LOCK_WAIT', 3.0):
            r = self.save()
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual('ready', self.candidate()['state'])

    def test_a_briefly_held_feeds_db_is_waited_for(self):
        """反过来钉一边:写锁只是短暂被占、时限还够,照常交得回去。"""
        from tests.test_x_intake_lock_wait import hold_sqlite_writer
        worker = hold_sqlite_writer(0.3)
        self.addCleanup(worker.join, 5)
        with patch.object(x_article_capture, '_LOCK_WAIT', 3.0):
            r = self.save()
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual('ready', self.candidate()['state'])

    def test_a_lock_held_past_the_short_wait_is_refused_and_writes_nothing(self):
        before = self.candidate()['entry']
        self.hold(2.0)
        started = time.monotonic()
        with patch.object(x_article_capture, '_LOCK_WAIT', 0.3):
            r = self.save()
        self.assertEqual(409, r.status_code)
        self.assertIn('同步正在写库', r.json()['detail'])
        self.assertLess(time.monotonic() - started, 1.5)          # 不陪着那一轮维护挂下去
        self.assertEqual(('pending', before), (self.candidate()['state'], self.candidate()['entry']))


class FeedsDbHeldTests(CaptureBase):
    """另一个连接一直攥着 feeds.db 的写锁(同步正在写库):三个端点都在那一份短时限里答复,不是陪它等满
    30 秒再报 500。认身份、列候选只读,所以列表和「这一页能做什么」照常答;交回那一下写不进去就 409、候选原样。"""

    def setUp(self):
        super().setUp()
        from tests.test_x_intake_lock_wait import hold_sqlite_until
        self.before = self.candidate()['entry']
        limit = patch.object(x_article_capture, '_LOCK_WAIT', 0.5)
        limit.start(); self.addCleanup(limit.stop)
        hold_sqlite_until(self)

    def timed(self, call):
        started = time.monotonic()
        r = call()
        self.assertLess(time.monotonic() - started, 0.5 + 0.4, '没守住这一个请求的那一份时限')
        return r

    def test_the_list_and_the_page_question_still_answer(self):
        r = self.timed(lambda: self.web.get('/api/x-article-capture/owed'))
        self.assertEqual((200, ['6']), (r.status_code, [i['post_id'] for i in r.json()['items']]))
        r = self.timed(lambda: self.here(ARTICLE))
        self.assertEqual((200, 'fill'), (r.status_code, r.json()['situation']))
        r = self.timed(lambda: self.here('https://x.com/alice/status/5'))
        self.assertEqual((200, 'stored'), (r.status_code, r.json()['situation']))

    def test_handing_in_is_refused_within_the_budget_and_writes_nothing(self):
        r = self.timed(self.save)
        self.assertEqual(409, r.status_code, r.text)
        self.assertIn('同步正在写库', r.json()['detail'])
        with reg.read_db() as c:              # 锁还被攥着:只读地看一眼
            row = c.execute("SELECT state, entry FROM x_intake_candidates WHERE post_id='6'").fetchone()
        self.assertEqual(('pending', self.before), tuple(row))


class ShippingTests(unittest.TestCase):
    def test_the_extension_carries_the_same_reader_the_browser_channel_runs(self):
        """读页脚本只有一份:登录通道读进来的就是扩展目录里那个文件;ZIP 装的扩展也带着它和 `x_capture.js`
        (少带一个,ZIP 装的扩展在长文页上点下去就是「找不到文件」)。"""
        self.assertEqual(SOURCE.read_text(encoding='utf-8'), x_article._READ_JS)
        manifest = json.loads((SOURCE.parent / 'manifest.json').read_text())
        self.assertTrue(manifest['version'])
        app = FastAPI(); app.include_router(browser_capture_api.router)
        web = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 54321),
                         headers={'Sec-Fetch-Site': 'same-origin'})
        names = zipfile.ZipFile(io.BytesIO(web.get('/api/browser-capture/extension.zip').content)).namelist()
        html = (SOURCE.parent / 'popup.html').read_text()
        for name in ('x_capture.js', 'x_article_read.js'):
            self.assertIn('knowbase-capture/' + name, names)
        self.assertIn('src="x_capture.js"', html)
        self.assertLess(html.index('x_capture.js'), html.index('popup.js'))   # popup.js 加载时要已经有它


if __name__ == '__main__':
    unittest.main()
