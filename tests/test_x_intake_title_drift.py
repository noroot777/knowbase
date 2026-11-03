"""推文标题的派生法不同,不许让一条正文一字未改的推文停在 `repair`。

实页形态:同一条多行推文先经浏览器通道「时间线预览 → 详情页展开」入库,标题是**第一行**
(`x_post.merge`);后来不登录通道从时间线直接拿到全文,标题是**整段前 500 字**
(`x_guest._build` / `x_browser._entry`)。两份标题都盖进了指纹,逐字比就把它判成新版,
落进 `repair` —— 没有哪一轮自动同步会再碰它,「问题与待办」上它以「正文待补」挂着,
直到滑出五天窗口变成「已超自动窗口」。判据落在后果上:候选落哪一档、还欠不欠、
库里那一条有没有被改写。反过来钉两边:正文真不一样的、库里那份证不了自己的,照旧扣留。
"""
from datetime import timedelta
from unittest.mock import patch

from kb.feeds import registry as reg, x_browser, x_post, x_post_meta
from kb.feeds import x_intake_store as store
from tests.test_x_channels import tweet
from tests.test_x_intake import IntakeBase

TEXT = 'The AI compute situation\n\n- build out delays will continue\n- power is the bottleneck'


class DriftBase(IntakeBase):
    """摆盘:同一条推文先经详情页展开入库,再从时间线全文看到一次。**这里一条用例都没有**
    (`tests/test_x_intake_supersede.py` 继承它,带用例的话会被一起收走)。"""

    def raw(self, text, *, truncated=False):
        raw = tweet('100', when=(self.now - timedelta(hours=1)).isoformat(), text=text)
        raw['truncated'] = truncated
        return dict(x_browser._entry(raw), x_observed_at=raw['datetime'])

    def saved_by_detail_page(self, text=TEXT):
        """浏览器那条路:预览 → 详情页展开 → 入库。返回库里那一条。"""
        self.prepare()
        preview = self.raw(text.splitlines()[0], truncated=True)
        self.stage([preview])
        source, task = store.pending([self.source])[0]
        full = x_post.merge(preview, text)
        store.finish(source, task, [full], [], '')
        reg.put_items(self.source, [full])
        # 这一轮那条通道看到它时**没有**现成的 ready 候选(实页那 40 条都是 attempted=0 的新
        # 候选:入库走的是另一条通道)。留着它的话 `stage` 直接复用、一个判据都不过 ——
        # 这几条用例就在旧代码上也全绿。
        with reg.db() as conn:
            conn.execute('DELETE FROM x_intake_candidates')
        return self.feed_row()

    def timeline_full_text(self, text=TEXT):
        """不登录那条路:时间线上直接就是全文,标题是整段。"""
        return self.raw(text)

    def feed_row(self):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())


class TitleDriftTests(DriftBase):
    def test_same_body_with_other_title_is_not_parked_in_repair(self):
        stored = self.saved_by_detail_page()
        self.assertEqual(stored['title'], TEXT.splitlines()[0])
        observed = self.timeline_full_text()
        self.assertEqual(observed['body'], stored['body'])
        self.assertNotEqual(observed['title'], stored['title'])

        self.stage([observed])

        self.assertEqual(self.candidate()['state'], 'ready')
        entries, pending, _, _ = store.delivery(self.source)
        self.assertEqual(pending, 0, '正文一字未改的推文不该算「正文待补」')
        self.assertEqual([e['title'] for e in entries], [stored['title']],
                         '交付的是库里那一份,不拿另一条路的标题去改写它')
        self.assertEqual(self.feed_row()['title'], stored['title'])

    def test_parked_candidate_heals_on_next_observation(self):
        """升级前已经停在 `repair` 的那几条,下一轮再看到时自己回到 ready。"""
        self.saved_by_detail_page()
        observed = self.timeline_full_text()
        with patch.object(x_post_meta, 'same_post', return_value=False):   # = 修之前那一轮
            self.stage([observed])
        self.assertEqual(self.candidate()['state'], 'repair', '这一局的前提')
        self.assertEqual(store.delivery(self.source)[1], 1)

        self.stage([observed])

        self.assertEqual(self.candidate()['state'], 'ready')
        self.assertEqual(store.delivery(self.source)[1], 0)

    def test_different_body_still_waits_for_versioned_backfill(self):
        stored = self.saved_by_detail_page()
        changed = TEXT.replace('bottleneck', 'constraint')   # 改了字,不是只多了链接
        self.stage([self.timeline_full_text(changed)])

        self.assertEqual(self.candidate()['state'], 'repair')
        self.assertEqual(store.delivery(self.source)[:2], ([], 1))
        self.assertEqual(self.feed_row()['body'], stored['body'], '正文不同的那一版不许直接覆盖')

    def test_stored_row_that_cannot_prove_itself_is_not_reused(self):
        stored = self.saved_by_detail_page()
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET title=?', (stored['title'] + ' (edited)',))
        self.stage([self.timeline_full_text()])

        self.assertEqual(self.candidate()['state'], 'repair')
        self.assertEqual(store.delivery(self.source)[1], 1)

    def test_articles_keep_the_strict_title_check(self):
        post = self.timeline_full_text()
        article = x_post_meta.stamp({k: v for k, v in post.items() if k != x_post_meta.FIELD},
                                    'article', '')
        retitled = x_post_meta.stamp(dict(article, title='Another headline'), 'article', '')
        self.assertFalse(x_post_meta.same_post(article, retitled))
        self.assertTrue(x_post_meta.same_post(post, post))
