# -*- coding: utf-8 -*-
"""写入响应契约里那一问:**这一次到底有没有把正文写进去。**

重复投递怎么答在 `tests/test_grab_endpoint.py`,入库那一半在
`tests/test_grab_store.py`,夹具共用 `tests/grab_fixtures.py`。这一组单开一个文件
是因为它问的不是某一条路的行为,而是**所有出口放在一起之后**那张值表 —— 两条真
写了的为真、其余一律为假。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from kb.feeds import grab
from tests.grab_fixtures import (BODY, WALL, EndpointCase, html_resp, page_html)


class WriteContractTests(EndpointCase):
    """**HTTP 200 不代表这一次写了正文** —— 那一问单独算一份,两个入口共用。"""

    def test_only_a_real_write_says_the_body_went_in(self):
        """**`stored_body` 答的是「这一次把正文写进去了没有」,不是「HTTP 200」。**

        这一层**每条出口都是 200**,而扩展那颗「关掉这一页」据它决定给不给许可:
        给错就是关掉人家**还没采的**那一页(消费侧口径与实测形态见
        `docs/decisions/browser-capture.md`)。另外三个键一个都答不了这一问 ——
        `added` 在重复那一档恒 0;`content_ok` 说的是**库里那一份**的质量(订阅
        同步先落下的一段 RSS 摘要也为真);`done` 说的是后半条链走完没有(正文刚
        写进去时它恒假)。所以服务端单独算一份,两个新条目入口共用。

        **集合级地比值,不是比有无**:每条出口各给一组期望值,两条真写了的为真、
        其余一律为假。只问「带没带这个键」的用例,在「带错了哪一道」上恒绿;而这
        一问的两个方向不对称 —— 漏给只是少一个出口,多给不可撤销。
        """
        def collected_fresh():
            with patch.object(grab, 'request', return_value=html_resp(page_html())), \
                 self.pipeline_patch():
                return self.post(url='https://example.org/posts/fresh').json()

        def from_the_browser():
            with patch.object(grab, 'request', side_effect=AssertionError('不许发请求')), \
                 self.pipeline_patch():
                return self.from_page(url='https://example.org/posts/browser').json()

        def refetch_replaced():
            url = 'https://example.org/posts/walled'
            grab.save({'url': url, 'title': 'Sign in to read', 'body': WALL,
                       'author': 'x', 'published': '', 'content_kind': 'content'})
            with patch.object(grab, 'request', return_value=html_resp(page_html())), \
                 self.pipeline_patch():
                return self.post(url=url).json()

        def refetch_still_walled():
            url = 'https://example.org/posts/still-walled'
            grab.save({'url': url, 'title': 'Sign in to read', 'body': WALL,
                       'author': 'x', 'published': '', 'content_kind': 'content'})
            wall = page_html(title='Sign in', body=[
                'Sign in to continue reading. Subscribe to unlock this story. ' * 3] * 7)
            with patch.object(grab, 'request', return_value=html_resp(wall)), \
                 self.pipeline_patch():
                return self.post(url=url).json()

        def good_duplicate():
            url = 'https://example.org/posts/already-fine'
            grab.save({'url': url, 'title': 'A', 'body': BODY, 'author': 'x',
                       'published': '', 'content_kind': 'content'})
            with patch.object(grab, 'request', side_effect=AssertionError('不许发请求')), \
                 self.pipeline_patch():
                return self.post(url=url).json()

        def finished_duplicate():
            url, path = 'https://example.org/posts/all-done', 'source/inbox/done.md'
            grab.save({'url': url, 'title': 'A', 'body': BODY, 'author': 'x',
                       'published': '', 'content_kind': 'content'})
            self.mark_exported(url, path)
            self.index_doc(path)
            with self.pipeline_patch():
                return self.post(url=url).json()

        def race_fallback():
            with patch.object(grab, 'request', return_value=html_resp(page_html())), \
                 patch.object(grab, 'save', return_value={'added': 0}), \
                 self.pipeline_patch():
                return self.post(url='https://example.org/posts/race').json()

        def refetch_refused():
            url = 'https://example.org/digest-linked/openai-cli'
            self.digest_item(url=url)
            with patch.object(grab, 'grab', side_effect=AssertionError('不许发请求')), \
                 self.pipeline_patch():
                return self.post(url=url, refetch=True).json()

        cases = [
            ('抓了一篇新的', collected_fresh,
             {'duplicate': False, 'stored_body': True}),
            ('浏览器里那一页', from_the_browser,
             {'duplicate': False, 'stored_body': True}),
            # 这一档 `added` 恒 0:拿 `added` 推的实现在这里把真写了的说成没写。
            ('重抓把那张墙换掉了', refetch_replaced,
             {'duplicate': True, 'added': 0, 'replaced': True, 'stored_body': True}),
            ('重抓到的还是那张墙', refetch_still_walled,
             {'duplicate': True, 'replaced': False, 'stored_body': False}),
            # 反过来的那一边:`content_ok` 为真也不代表**这一次**写了什么。
            ('库里那一份质量没毛病', good_duplicate,
             {'duplicate': True, 'content_ok': True, 'stored_body': False}),
            ('后半条链也走完了的幂等答复', finished_duplicate,
             {'duplicate': True, 'done': True, 'stored_body': False}),
            ('写入时撞上别人那一份(`DO NOTHING`)', race_fallback,
             {'duplicate': True, 'added': 0, 'stored_body': False}),
            ('那一行的 url 指向别人家那篇,拒绝重抓', refetch_refused,
             {'duplicate': True, 'replaced': False, 'stored_body': False}),
        ]
        for name, act, expected in cases:
            with self.subTest(name):
                data = act()
                self.assertEqual({key: data.get(key) for key in expected}, expected, name)


if __name__ == '__main__':
    unittest.main()
