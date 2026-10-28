# -*- coding: utf-8 -*-
"""**一次补抓的工作身份:可复原的输入值不是身份。**

`fulltext.work_version` 五道一起 —— `content_hash`、`content_rev`、`fulltext_url`、
`life`、`fulltext_gen`。前三道都**可以复原**,这一份就是为那件事写的
(审查第 2 轮 R2 实测):

- **地址 A→B→A**:`relink` 只改那个字符串时,改回 A 之后元组逐字复原,于是改地址
  之前发出去的那个请求重新获得写回权 —— 实测旧 `_save` 返回 true、`fulltext_at`
  被置上,面板显示「正文已补齐」,而新地址一次都没去过;
- **删掉重收**:`item_key` 按网址算,同一个链接收回来之后 `content_hash`(同一份
  摘要)、`content_rev`(新行从 0 起)、`fulltext_url`(新行是空串)**三道可以逐字
  相同**,上一世在路上的那个请求回来就盖在新收录的这一行上;
- **人工放回队列**:`release` / `reopen` 把额度和待办清成目标值 = 发了一轮新的工作
  机会,而清完之后那几列和「从没挂起过」逐字相同。

所以后两道:`life` 不可复用(触发器发),`fulltext_gen` 只增(换地址、还额度各加
一代)。**三种写回(成功、异常、证据不足)用同一个判据**,不是只在成功那一支加一次
预检;每一条都**反过来钉一边** —— 没动过的时候那份工作照旧写得回去,不然「写回整个
坏掉」也是全绿。
"""
from __future__ import annotations

from kb.feeds import fulltext, registry as reg
from kb.intake_diag import actions
from tests.intake_diag_case import DiagCase, key_of

HELD = 'https://news.example.org/held'
QUEUED = 'https://news.example.org/queued'


class RelinkGenerationTests(DiagCase):
    """换地址:一代新工作。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', QUEUED)
        actions.relink(self.key, 'rss', 'https://origin.example.org/a')
        self.seen = fulltext.work_version(self.row(self.key))

    def test_the_result_of_the_old_address_is_refused_after_changing_it_back(self):
        actions.relink(self.key, 'rss', 'https://origin.example.org/b')
        # 正向对照:地址是 B 的时候,A 那一份被拒 —— 上一版就做到了这一步。
        self.assertFalse(fulltext._fail(self.key, '旧地址超时', self.seen))
        actions.relink(self.key, 'rss', 'https://origin.example.org/a')
        row = self.row(self.key)
        # 前三道**逐字复原**了,这正是上一版放行的原因。
        self.assertEqual(self.seen[:3],
                         (row['content_hash'], row['content_rev'], row['fulltext_url']))
        self.assertFalse(fulltext._save(self.key, '旧地址那一页的正文。' * 40, self.seen))
        self.assertFalse(fulltext._fail(self.key, '旧地址超时', self.seen))
        self.assertFalse(fulltext._fail(self.key, '看不出是不是正文', self.seen,
                                        '页面上没有识别出正文；已挂起等人看一眼',
                                        count=False))
        self.assertEqual('', self.row(self.key)['fulltext_at'])

    def test_a_fresh_sample_after_the_relink_still_writes_back(self):
        """**反过来钉一边**:换完地址重新取样的那一份照旧写得回去。"""
        actions.relink(self.key, 'rss', 'https://origin.example.org/b')
        seen = fulltext.work_version(self.row(self.key))
        self.assertTrue(fulltext._save(self.key, '新地址那一页的正文。' * 40, seen))
        self.assertNotEqual('', self.row(self.key)['fulltext_at'])

    def test_changing_the_address_does_not_invalidate_other_content_work(self):
        """**换地址不是换内容**:`content_rev` 一个都不许动(别人手上那份内容工作
        资格 —— 比如同一时刻跑着的手动重抓 —— 和这一条去哪儿补抓无关)。"""
        before = self.row(self.key)['content_rev']
        actions.relink(self.key, 'rss', 'https://origin.example.org/c')
        self.assertEqual(before, self.row(self.key)['content_rev'])


class LifeGenerationTests(DiagCase):
    """删掉重收:另一世,另一份待办。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', QUEUED)

    def recollect(self) -> dict:
        """删掉重收:同一个链接、**逐字同一条内容**,于是 key 和内容哈希都没变。

        重收那一下交回去的字段要和样例当初那一条**逐字一样** —— 内容哈希算的是
        整个条目字典(`registry.put_items`),多塞一个键就换一个哈希,而那样这条
        用例验的就不再是「前三道相同、只差这一世」那一档了。
        """
        row = self.row(self.key)
        with reg.db() as c:
            c.execute('DELETE FROM subscription_items WHERE item_key=?', (self.key,))
            c.execute('DELETE FROM feed_items WHERE key=?', (self.key,))
        reg.put_items(reg.subscription('rss', self.ids['agg']),
                      [dict(url=QUEUED, published=row['published'],
                            title=row['title'], body=row['body'])])
        return row

    def test_a_request_from_the_previous_life_cannot_write_to_the_new_row(self):
        old = self.recollect()
        seen = (old['content_hash'], old['content_rev'], old['fulltext_url'] or '',
                old['life'], int(old['fulltext_gen'] or 0))
        fresh = self.row(self.key)
        # 前三道**可以逐字相同**,这正是上一版放行的原因;世代不同。
        self.assertEqual(seen[:3], (fresh['content_hash'], fresh['content_rev'],
                                    fresh['fulltext_url'] or ''))
        self.assertNotEqual(old['life'], fresh['life'])
        self.assertFalse(fulltext._save(self.key, '上一世迟到的正文。' * 40, seen))
        self.assertFalse(fulltext._fail(self.key, '上一世的超时', seen))
        self.assertEqual(fresh, self.row(self.key))

    def test_this_life_can_still_be_fetched_normally(self):
        """**反过来钉一边**:新收回来的这一条照旧补抓得了。"""
        self.recollect()
        seen = fulltext.work_version(self.row(self.key))
        self.assertTrue(fulltext._save(self.key, '这一世补到的正文。' * 40, seen))


class ManualRequeueGenerationTests(DiagCase):
    """人工放回队列:在路上那一轮的结果不许落在刚还回去的额度上。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', HELD)
        self.seen = fulltext.work_version(self.row(self.key))

    def test_a_request_sent_before_the_release_cannot_re_hold_it(self):
        """`release` 之前发出去的那个请求回来:挂起不许被重新写上、额度不许被扣。

        上一版这里是**知情的取舍**(那时还没有代次这一列),代价是「那一条又挂回去
        了,人再点一次」。现在挡住了,所以把它钉成不变量。
        """
        self.assertEqual(1, fulltext.release([self.key], kind='rss'))
        after = self.row(self.key)
        self.assertEqual('', after['fulltext_hold'])
        self.assertEqual(0, after['fulltext_tries'])
        self.assertFalse(fulltext._fail(self.key, '旧请求看不出是不是正文', self.seen,
                                        '页面上没有识别出正文；已挂起等人看一眼',
                                        count=False))
        self.assertFalse(fulltext._fail(self.key, '旧请求超时', self.seen))
        self.assertEqual(after, self.row(self.key))

    def test_the_round_after_the_release_writes_back_as_usual(self):
        """**反过来钉一边**:放回队列之后那一轮照旧写得回去。"""
        fulltext.release([self.key], kind='rss')
        seen = fulltext.work_version(self.row(self.key))
        self.assertTrue(fulltext._save(self.key, '放回队列之后补到的正文。' * 40, seen))

    def test_returning_an_exhausted_budget_also_opens_a_new_generation(self):
        """「重试已停止」那一档走的是 `reopen` 的另一支,它同样要发新代次。"""
        key = key_of('rss', 'https://news.example.org/exhausted')
        seen = fulltext.work_version(self.row(key))
        opened = fulltext.reopen([key], 'rss')
        self.assertEqual(1, opened['body'])
        self.assertFalse(fulltext._fail(key, '还额度之前那一次的超时', seen))
        fresh = fulltext.work_version(self.row(key))
        self.assertTrue(fulltext._fail(key, '还额度之后那一次的超时', fresh))
