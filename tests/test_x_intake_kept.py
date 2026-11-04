"""「收没收」看链接,「换不换」才看内容:库里那条已是成品,就算已收(`x_intake_store.kept`)。

实页形态:同一批推文先经浏览器通道入库,换成不登录通道后又被看到一次,两份抄法不同。原来
只要不是同一版就落进 `repair` —— 没有哪一轮自动同步会再碰它,「问题与待办」上挂「正文待补」,
滑出五天窗口再以「已超自动窗口」报红一天,而那条推文一直好好地在库里(实测 11 条全是这样)。

判据落在后果上:候选落哪一档、交付的是哪一份、库里那一行有没有被改写、窗口过去之后报不报超窗。
**超窗那一半要按真实顺序走一遍下一轮的 `prepare`**:报警是从候选状态派生的,光删报警、或者只在
收尾清 `repair`,下一轮准备时都会重建(`ExpiryTests`)。同一条推文挂在两个账号底下时逐账号判,
一个账号先收敛了共享候选,另一个账号那条关联照样要认下(`SharedPostTests`)。
反过来钉住:库里那份证不了自己的、帖子和长文对不上的、库里压根没有的,窗口外照旧报。
只多了链接的那一版照旧交给覆盖(`tests/test_x_intake_supersede.py` 的 `parked` 前提钉着)。

跑:  uv run --offline python -m unittest tests.test_x_intake_kept -v
"""
import json
from datetime import timedelta
from unittest.mock import patch

from kb.feeds import registry as reg, x_article, x_post, x_post_meta
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

    def test_a_parked_row_this_account_already_delivered_is_still_released(self):
        """「正文待补」跟这个账号早先交付过没有关系:交付标记不许把它挡在收敛之外。"""
        stored = self.saved_by_detail_page()
        with patch.object(store, 'kept', return_value=False):              # = 改之前那一轮
            self.stage([self.timeline_full_text(CHANGED)])
        with reg.db() as conn:
            conn.execute('UPDATE x_intake_links SET confirmed=1,delivered=1')
        self.assertEqual((self.candidate()['state'], store.delivery(self.source)[1]), ('repair', 1), '这一局的前提')

        self.assertEqual(1, supersede.run([self.source]))

        self.assertEqual((self.candidate()['state'], store.delivery(self.source)[1]), ('ready', 0))
        self.assertEqual(self.feed_row()['body'], stored['body'])

    def test_a_post_and_an_article_are_never_one_copy(self):
        stored = self.saved_by_detail_page()
        observed = self.timeline_full_text(CHANGED)
        article = x_post_meta.stamp({k: v for k, v in observed.items() if k != x_post_meta.FIELD}, 'article', '')
        self.assertTrue(store.complete(article), '这一局的前提')

        self.assertTrue(store.kept(stored, observed))
        self.assertFalse(store.kept(stored, article))


class ExpiryTests(DriftBase):
    """改之前那几轮留下的状态,下一轮按真实顺序准备时收敛;库里没有成品的照旧报。"""

    def later(self, hours=0):
        return self.now + timedelta(days=6, hours=hours)

    def expired_under_old_rules(self):
        """改之前:差一个字停进 `repair`,跨过窗口时被转成 `expired`、记下一条超窗。"""
        stored = self.saved_by_detail_page()
        with patch.object(store, 'kept', return_value=False), patch.object(store, 'settle', return_value=0):
            self.stage([self.timeline_full_text(CHANGED)])
            store.prepare([self.source], 'browser', self.later())
        self.assertEqual((self.candidate()['state'], store.progress(self.source)['expired']), ('expired', 1),
                         '这一局的前提')
        return stored

    def alarms(self, hours):
        store.prepare([self.source], 'browser', self.later(hours))
        with reg.db() as conn:
            rows = conn.execute('SELECT count(*) FROM x_intake_expirations').fetchone()[0]
        return store.progress(self.source)['expired'], rows

    def test_an_expired_post_the_library_holds_stops_alarming_next_round(self):
        stored = self.expired_under_old_rules()

        self.assertEqual(self.alarms(1), (0, 0))

        self.assertEqual(self.candidate()['state'], 'ready')
        self.assertEqual(store.delivery(self.source)[:2], ([], 0), '已收:没有要交付的,也不欠')
        self.assertEqual(self.feed_row()['body'], stored['body'])

    def test_a_cleared_alarm_is_not_recreated(self):
        self.expired_under_old_rules()
        with reg.db() as conn:                 # 只删报警、候选照旧 expired
            conn.execute('DELETE FROM x_intake_expirations')

        self.assertEqual(self.alarms(1), (0, 0))

    def test_a_repair_crossing_the_window_is_settled_before_expiry_accounting(self):
        self.saved_by_detail_page()
        with patch.object(store, 'kept', return_value=False):               # = 改之前那一轮
            self.stage([self.timeline_full_text(CHANGED)])
        self.assertEqual(self.candidate()['state'], 'repair', '这一局的前提')

        self.assertEqual(self.alarms(0), (0, 0))
        self.assertEqual(self.candidate()['state'], 'ready')

    def test_an_unfinished_preview_of_a_collected_post_does_not_alarm(self):
        self.saved_by_detail_page()
        self.stage([self.raw(TEXT.splitlines()[0], truncated=True)])
        self.assertEqual(self.candidate()['state'], 'pending', '这一局的前提')

        self.assertEqual(self.alarms(0), (0, 0))

    def test_an_unfinished_preview_of_an_uncollected_post_still_alarms(self):
        self.prepare()
        self.stage([self.raw(TEXT.splitlines()[0], truncated=True)])

        self.assertEqual(self.alarms(0), (1, 1))
        self.assertEqual(self.candidate()['state'], 'expired')

    def test_a_missing_article_still_alarms_when_the_library_holds_only_the_post(self):
        self.saved_by_detail_page()
        self.stage([dict(self.timeline_full_text(), **{x_article.HINT: '1'})])
        self.assertEqual(self.candidate()['state'], 'pending', '这一局的前提')

        self.assertEqual(self.alarms(0), (1, 1))

    def test_an_unproven_stored_copy_still_alarms(self):
        self.expired_under_old_rules()
        with reg.db() as conn:                 # 升级前入库的那一批:没有完整证明
            conn.execute("UPDATE feed_items SET x_post_meta=''")

        self.assertEqual(self.alarms(1), (1, 1))


class SharedPostTests(DriftBase):
    """同一条推文挂在两个账号底下:候选全库一行,确认、交付、超窗却逐账号。一个账号先把共享候选放回
    `ready`,另一个账号那条没确认的关联也得被认下 —— 不能拿共享候选还在不在 `expired` 判它收没收。
    认旧正文、覆盖新版两条路各钉一次;删掉报警再准备一轮也不重建。"""

    def shared_and_expired(self, stored_text, seen_text):
        """改之前那几轮留下的:候选跨窗进了 `expired`,第一个账号确认过、第二个没有,两边各一条超窗。"""
        stored = self.saved_by_detail_page(stored_text)
        self.other = self.sub('Other')
        published = (self.now - timedelta(days=6)).isoformat()
        observed = dict(self.timeline_full_text(seen_text), published=published, x_observed_at=published)
        self.assertTrue(store.complete(observed), '这一局的前提')
        with reg.db() as conn:
            conn.execute('DELETE FROM x_intake_links')
            conn.execute("INSERT INTO x_intake_candidates(post_id,entry,published,state) VALUES('100',?,?,'expired')",
                         (json.dumps(observed), published))
            for source, confirmed in ((self.source, 1), (self.other, 0)):
                conn.execute('''INSERT INTO x_intake_links(source_id,identity,post_id,confirmed,delivered)
                    VALUES(?,?,'100',?,0)''', (source['id'], store.identity(source), confirmed))
                conn.execute('''INSERT INTO x_intake_expirations(source_id,identity,post_id,expired_at)
                    VALUES(?,?,'100',?)''', (source['id'], store.identity(source), self.now.isoformat()))
        return stored

    def a_round(self, hours):
        """按真实顺序:准备 → 清扫 → 各账号交付并回执。返回每个账号的(超窗数, 超窗记录数)。"""
        sources = [self.source, self.other]
        store.prepare(sources, 'browser', self.now + timedelta(hours=hours))
        supersede.run(sources)
        for source in sources:
            delivered = store.delivery(source)[0]
            reg.put_items(source, delivered)
            with reg.db() as conn:
                store.acknowledge(conn, source, delivered)
        with reg.db() as conn:
            return [(store.progress(source)['expired'], conn.execute(
                'SELECT count(*) FROM x_intake_expirations WHERE source_id=?', (source['id'],)).fetchone()[0])
                for source in sources]

    def assert_settled_for_both(self):
        self.assertEqual(self.a_round(1), [(0, 0), (0, 0)])
        self.assertEqual(self.a_round(2), [(0, 0), (0, 0)])
        with reg.db() as conn:
            conn.execute('DELETE FROM x_intake_expirations')
            links = conn.execute('SELECT confirmed,delivered FROM x_intake_links ORDER BY source_id').fetchall()
        self.assertEqual([tuple(row) for row in links], [(1, 1), (1, 1)], '两个账号各自那条关联都认下了')
        self.assertEqual(self.a_round(3), [(0, 0), (0, 0)], '删掉的报警不重建')

    def test_the_library_copy_settles_every_account(self):
        stored = self.shared_and_expired(TEXT, CHANGED)

        self.assert_settled_for_both()
        self.assertEqual(self.feed_row()['body'], stored['body'], '认的是库里那份,一个字不改')

    def test_a_replaced_copy_settles_every_account(self):
        stored = self.shared_and_expired(TEXT, TEXT + '\n\nhttps://example.com/extra')
        self.assertTrue(x_post_meta.supersedes(stored, self.candidate_entry()), '这一局的前提:走覆盖')

        self.assert_settled_for_both()
        self.assertIn('https://example.com/extra', self.feed_row()['body'], '覆盖成了新版')

    def test_an_account_whose_copy_is_not_in_the_library_still_alarms(self):
        """反过来:库里那份证不了自己(升级前入库、没有完整证明)时,两个账号照旧各报一条。"""
        self.shared_and_expired(TEXT, CHANGED)
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET x_post_meta=''")

        self.assertEqual(self.a_round(1), [(1, 1), (1, 1)])

    def candidate_entry(self):
        return json.loads(self.candidate()['entry'])
