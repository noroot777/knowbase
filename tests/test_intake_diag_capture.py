# -*- coding: utf-8 -*-
"""「从浏览器采集」那颗按钮:**它和「手动补正文」是一道闸上的一对**。

会落进正文那三档的多半是「页面上没有识别出正文,可能需要登录或由脚本渲染」
「这一页要求先完成验证或登录」「HTTP 403」—— 那些页人已经在浏览器里开着、登录态
也在,所以主入口是从那个标签抓,手动全选复制退成兜底。这一份钉三件事:

① **结构红:`capture` 和 `body` 只许成对出现,而且 `capture` 紧排在前面。**
   行为用例证明不了「**所有**档都是这样」,而一颗开一颗关的后果是:开着的那颗点
   下去只在服务端被拒,屏幕上看不出为什么(准入逐字同一份,
   `browser_capture._admit_body`)。顺序本身也是约定 —— 主次读不出来,人就会先点
   那颗兜底的。
② **集合级红:那句「为什么不许」和判据同源。** `link_body_refusal` 空串 ⇔
   `permits_link_body` 为真,**按 `SOURCE_TYPES` 全集**问一遍 —— 只挑三类来问的话,
   新增一类来源(默认不许可)会拿到一句空话,而界面据此把按钮画成可点的。
③ **绕过界面那条路也拒,而且说同一句话。** 人手敲
   `/browser-capture?item=<key>` 时 `browser_capture.target()` 自己拒,
   理由和面板上那行灰字同源。

**执行者和审查者不跑这一份,统一在验收阶段执行**(CLAUDE.md 元规则)。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import browser_capture, source_types
from kb.feeds import registry as reg
from kb.intake_diag import lanes
from kb.web import intake_diag_api
from tests.intake_diag_case import DiagCase, key_of

EXHAUSTED = 'https://news.example.org/exhausted'
QUEUED = 'https://news.example.org/queued'
HELD = 'https://news.example.org/held'
#: 不许可的那几类各摆一条,地址形状按各自的来源写(key 由 kind + url 算)。
BLOCKED = (('youtube', 'https://www.youtube.com/@sample', 'https://yt.example.org/one'),
           ('digest', 'https://digest.example.org/feed.xml', 'https://dg.example.org/one'),
           ('social', 'https://x.com/sample_capture', 'https://x.com/s/status/1'))


class ExitPairingTests(DiagCase):
    """① 结构红:一道闸上的一对,顺序也是约定。"""

    def test_capture_and_paste_always_appear_together_with_capture_first(self):
        seen = 0
        for lane in lanes.LANES:
            with self.subTest(lane=lane.id):
                exits = list(lane.exits)
                self.assertEqual('capture' in exits, 'body' in exits,
                                 '「从浏览器采集」和「手动补正文」过的是同一份准入，'
                                 '不许一档只给一颗')
                if 'capture' not in exits:
                    continue
                seen += 1
                at = exits.index('capture')
                # **紧排在前面**:这两颗是一对,中间插进别的按钮就读不出主次了。
                self.assertEqual('body', exits[at + 1])
        # 一档都没有的话上面整段是空洞的通过(元规则:红 0 条先怀疑用例)。
        self.assertEqual(3, seen, '正文那三档都该给这一对出口')

    def test_only_the_body_lanes_offer_it(self):
        """**反过来钉一边**:翻译 / 入库 / 已完成那几档一颗都没有。

        它们手上没有「这一页就是这篇文章的正文」这个前提:翻译那两档的正文已经定
        下来了,入库那一档等的是导出,已完成那几档后端压根不给出口。
        """
        for lane in lanes.LANES:
            if lane.stage == lanes.BODY:
                continue
            with self.subTest(lane=lane.id):
                self.assertNotIn('capture', lane.exits)


class RefusalWordingTests(DiagCase):
    """② 集合级红:那句理由和判据同源,按全集问。"""

    def test_every_kind_answers_exactly_when_the_gate_says_no(self):
        for kind in source_types.SOURCE_TYPES:
            with self.subTest(kind=kind):
                allowed = source_types.permits_link_body(kind)
                why = source_types.link_body_refusal(kind)
                self.assertEqual(allowed, not why,
                                 '许可的一律空串，不许可的必须答得出一句话')
                if not allowed:
                    self.assertGreater(len(why), 8, '一句看得懂的话，不是一个词')
        # 两边都得真有样本,不然这条是空洞的通过。
        kinds = source_types.SOURCE_TYPES
        self.assertTrue(any(source_types.permits_link_body(k) for k in kinds))
        self.assertTrue(any(not source_types.permits_link_body(k) for k in kinds))

    def test_a_brand_new_kind_is_refused_with_words_not_with_an_empty_string(self):
        """没登记理由的新来源走兜底那一句。默认不许可,**空串等于说「可以点」**。"""
        self.assertFalse(source_types.permits_link_body('brand-new-kind'))
        self.assertTrue(source_types.link_body_refusal('brand-new-kind'))

    def test_youtube_says_the_transcript_would_be_replaced_by_the_watch_page(self):
        """YouTube 那一条的理由不是一句通用话:它的正文是转录稿。"""
        why = source_types.link_body_refusal('youtube')
        self.assertIn('转录稿', why)
        self.assertIn('播放页', why)


class DetailGateTests(DiagCase):
    """`/items/{key}` 交出去的那两样 —— 界面上三颗按钮的灰不灰全靠它。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        app = FastAPI()
        app.include_router(intake_diag_api.router)
        self.client = TestClient(app)

    def detail(self, key: str) -> dict:
        r = self.client.get('/api/intake-care/items/' + key)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def test_the_eligible_lanes_open_both_buttons_and_say_nothing_extra(self):
        """**反过来钉一边**:合格的那几类照旧两颗都开、没有一句理由要显示。"""
        for url in (EXHAUSTED, QUEUED, HELD):
            with self.subTest(url=url):
                found = self.detail(key_of('rss', url))
                self.assertTrue(found['can_paste'])
                self.assertEqual('', found['paste_block'])
                # 这一档真的给了那一对出口(否则上面两句管不着任何按钮)。
                self.assertIn('capture', found['lane']['exits'])
                self.assertIn('body', found['lane']['exits'])

    def test_the_blocked_kinds_shut_the_gate_and_give_the_reason(self):
        for kind, home, url in BLOCKED:
            with self.subTest(kind=kind):
                source = reg.save_subscription(kind, {'name': f'{kind} 样例', 'url': home})
                key = self.put(source, url, body='正文' * 300)
                found = self.detail(key)
                self.assertFalse(found['can_paste'])
                self.assertEqual(source_types.link_body_refusal(kind),
                                 found['paste_block'])
                self.assertTrue(found['paste_block'])

    def test_the_key_it_hands_out_is_the_one_the_capture_page_binds(self):
        """「从浏览器采集」那个地址带的是**真实 `feed_items.key`**。

        补抓页按它绑定条目(`browser_capture.target`),所以这里拿面板列表里那一条
        的 key 直接去问那一侧 —— 展示编号对不上的话这颗按钮会把人送到一个绑不上
        任何条目的页面。
        """
        listed = self.client.get('/api/intake-care/items',
                                 params={'lane': 'body_exhausted'}).json()
        rows = listed['sections'][0]['items']
        self.assertEqual(1, len(rows))
        key = rows[0]['key']
        self.assertEqual(key_of('rss', EXHAUSTED), key)
        bound = browser_capture.target(key)
        self.assertEqual(key, bound['key'])
        self.assertEqual(EXHAUSTED, bound['url'])

    def test_typing_that_address_by_hand_is_refused_with_the_same_reason(self):
        """③ 绕过界面那条路也拒,而且说同一句话。"""
        source = reg.save_subscription('youtube', {
            'name': 'YouTube 样例', 'url': 'https://www.youtube.com/@hand'})
        key = self.put(source, 'https://yt.example.org/hand', body='转录稿' * 300)
        with self.assertRaises(ValueError) as caught:
            browser_capture.target(key)
        self.assertIn(source_types.link_body_refusal('youtube'), str(caught.exception))
