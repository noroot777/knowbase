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
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import browser_capture
from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_article, x_channels
from kb.feeds.x_article_js import SOURCE
from kb.web import browser_capture_api, x_article_capture_api
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

    def test_a_post_that_no_longer_waits_is_not_written_again(self):
        """已经补上并入库的,再交一份(哪怕是另一份正文)也不许改它。"""
        self.assertEqual(200, self.save().status_code)
        self.collect()
        r = self.save(snap=snapshot(blocks=('另一份正文',)))
        self.assertEqual(409, r.status_code)
        self.assertIn('已经不在等长文', r.json()['detail'])
        self.assertEqual(('长文标题', '第一段\n\n第二段'), self.stored()[STATUS])


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
