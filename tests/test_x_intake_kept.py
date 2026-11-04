"""「收没收」看链接,「换不换」才看内容:库里那条已是成品,就算已收(`x_intake_store.kept`)。

实页形态:同一批推文先经浏览器通道入库,换成不登录通道后又被看到一次,两份抄法不同。原来
只要不是同一版就落进 `repair` —— 没有哪一轮自动同步会再碰它,「问题与待办」上挂「正文待补」,
滑出五天窗口再以「已超自动窗口」报红一天,而那条推文一直好好地在库里(实测 11 条全是这样)。

判据落在后果上:候选落哪一档、交付的是哪一份、库里那一行有没有被改写、窗口过去之后报不报超窗。
反过来钉住:库里那份证不了自己的、帖子和长文对不上的,照旧留在 `repair`,窗口外照旧报。
只多了链接的那一版照旧交给覆盖(`tests/test_x_intake_supersede.py` 的 `parked` 前提钉着)。

跑:  uv run --offline python -m unittest tests.test_x_intake_kept -v
"""
from datetime import timedelta
from unittest.mock import patch

from kb.feeds import registry as reg, x_post, x_post_meta
from kb.feeds import x_intake_store as store
from kb.feeds import x_intake_supersede as supersede
from tests.test_x_intake_title_drift import TEXT, DriftBase

CHANGED = TEXT.replace('bottleneck', 'constraint')     # 改了一个字,不是只多了链接


class KeptTests(DriftBase):
    def assert_collected_as_stored(self, stored):
        self.assertEqual(self.candidate()['state'], 'ready')
        entries, pending, _, _ = store.delivery(self.source)
        self.assertEqual(pending, 0, '库里已有成品,不该再算「正文待补」')
        self.assertEqual([entry['body'] for entry in entries], [stored['body']], '交付的是库里那一份')
        row = self.feed_row()
        self.assertEqual((row['title'], row['body'], row['content_rev']),
                         (stored['title'], stored['body'], stored['content_rev']), '库里那一行一个字不改')

    def expired_after_window(self):
        store.prepare([self.source], 'browser', self.now + timedelta(days=6))
        return store.progress(self.source)['expired']

    def test_a_copy_that_differs_by_a_word_counts_as_collected(self):
        stored = self.saved_by_detail_page()

        self.stage([self.timeline_full_text(CHANGED)])

        self.assert_collected_as_stored(stored)

    def test_it_never_becomes_an_expiry_alarm(self):
        self.saved_by_detail_page()
        self.stage([self.timeline_full_text(CHANGED)])

        self.assertEqual(self.expired_after_window(), 0)

    def test_an_unproven_stored_copy_still_waits_and_expires_visibly(self):
        stored = self.saved_by_detail_page()
        with reg.db() as conn:                 # 升级前入库的那一批:没有完整证明
            conn.execute("UPDATE feed_items SET x_post_meta=''")

        self.stage([self.timeline_full_text(CHANGED)])

        self.assertEqual(self.candidate()['state'], 'repair')
        self.assertEqual(store.delivery(self.source)[:2], ([], 1))
        self.assertEqual(self.feed_row()['body'], stored['body'], '证不了自己也不许直接覆盖')
        self.assertEqual(self.expired_after_window(), 1)

    def test_body_completion_reaches_the_same_verdict(self):
        """`finish` 那条路:预览进了正文待补,补回来的全文跟库里差一个字。"""
        stored = self.saved_by_detail_page()
        preview = self.raw(CHANGED.splitlines()[0], truncated=True)
        self.stage([preview])
        source, task = store.pending([self.source])[0]

        store.finish(source, task, [x_post.merge(preview, CHANGED)], [], '')

        self.assert_collected_as_stored(stored)

    def test_a_parked_title_drift_row_is_released_without_retitling_the_stored_post(self):
        """升级前停进 `repair` 的那几条:每轮的清扫认库里那条,不拿另一条路的标题去改写它。"""
        stored = self.saved_by_detail_page()
        with patch.object(x_post_meta, 'same_post', return_value=False):   # = 修之前那一轮
            self.stage([self.timeline_full_text()])
        self.assertEqual(self.candidate()['state'], 'repair', '这一局的前提')

        self.assertEqual(1, supersede.run([self.source]))

        self.assert_collected_as_stored(stored)

    def test_a_parked_changed_word_row_is_released_by_the_sweep(self):
        stored = self.saved_by_detail_page()
        with patch.object(store, 'kept', return_value=False):              # = 改之前那一轮
            self.stage([self.timeline_full_text(CHANGED)])
        self.assertEqual(self.candidate()['state'], 'repair', '这一局的前提')

        self.assertEqual(1, supersede.run([self.source]))

        self.assert_collected_as_stored(stored)
        self.assertEqual(self.expired_after_window(), 0)

    def test_a_post_and_an_article_are_never_one_copy(self):
        stored = self.saved_by_detail_page()
        observed = self.timeline_full_text(CHANGED)
        article = x_post_meta.stamp({k: v for k, v in observed.items() if k != x_post_meta.FIELD}, 'article', '')
        self.assertTrue(store.complete(article), '这一局的前提')

        self.assertTrue(store.kept(stored, observed))
        self.assertFalse(store.kept(stored, article))
