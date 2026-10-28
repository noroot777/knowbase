# -*- coding: utf-8 -*-
"""**可撤销的搁置**(`kb/intake_diag/shelf.py`):暂缓 / 不再收录,以及它认谁。

被测的那几件事,每一件都是「点下去之后没发生」或者「发生在别的内容上」:

① **按钮要真的拦住下一轮**。`shelf` 只在一张旁表上记一句话,拦不拦得住全看
   补抓和翻译那两道判据有没有 AND 上它 —— 没有的话,人点完「暂缓」下一轮照旧
   去抓、照旧买一次翻译,而屏幕上那一行确实变成了「已搁置」。
   **交付那一道在 `tests/test_intake_diag_export.py`**:它的方向是反的(队列的
   反集),所以单独一份。
② **搁置那一道加在 `queued_sql` 上,不许加进 `unsettled_sql`**。后者答的是
   「这一条的正文还没定下来」;加进去之后,一条被暂缓、手里还是跳转壳的英文
   内容当场变成「正文已定」、排进翻译队列 —— 按壳买一次模型调用,而人刚刚说的
   是「这条先别动」。所以这里**反向钉住**:被搁置的壳在 `unsettled_sql` 眼里
   照旧是「正文没定」。
③ **身份认「这一世」,而且认的是 `feed_items.life` 那一列**。`item_key` 按网址
   算:存量清理删掉一条、下一轮又从同一个链接收回来,key 一个字都没变,而库里那
   是另一行、另一份正文。继承上一世的「不再收录」等于拿一个针对别的内容做过的决
   定静默挡住它,而面板上看不见。**上一版拿 `fetched_at` + `content_rev` 两道组合
   认这件事,而那两列都是可以重复的本地状态** —— 同一秒删掉重收、上一世代次本来
   就是 0 时,两道同时成立,那条新内容被静默挡住。反过来,**同一世里换过一版正文
   的照旧算搁置着**(不然按钮会在正文更新后失效)。
④ **「这一世的哪一次决定」另算一个编号**。同一条内容先暂缓、恢复、再「不再收录」,
   第一条记录上那颗「恢复」只按 key 删的话,会把**后来**那次决定一起取消。
⑤ **它不是永久排除**。`kb/feeds/exclusion.py` 那一份会删行、删文件、装触发器
   让这条链接再也收不回来;这里一个字节都不碰,恢复之后逐列相同。

没有打桩:临时 registry 真建表、真写条目,判据真跑 SQLite。
"""
from __future__ import annotations

from kb.feeds import fulltext, registry as reg, translate_scope
from kb.intake_diag import lanes, shelf
from tests.intake_diag_case import DiagCase, key_of

SHELL = '标题壳 (\nexample.org\n)\n10-08\n108 HN Points'
QUEUED = 'https://news.example.org/queued'
HELD = 'https://news.example.org/held'
TRANSLATE = 'https://solo.example.org/translate-queued'


class ShelfReachTests(DiagCase):
    """点下去之后,下一轮真的不碰它了 —— 以及不该拦的照旧放行。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.queued = key_of('rss', QUEUED)

    def queue(self) -> list[str]:
        cond, args = fulltext.queued_sql()
        with reg.db() as c:
            return [r[0] for r in c.execute(
                f'SELECT key FROM feed_items WHERE kind=? AND {cond}',
                ('rss', *args)).fetchall()]

    def test_a_snoozed_item_leaves_the_fetch_queue_and_the_others_stay(self):
        before = self.queue()
        self.assertIn(self.queued, before)
        self.shelve([self.queued], 'rss', shelf.SNOOZE, '等作者补完')
        after = self.queue()
        self.assertNotIn(self.queued, after)
        self.assertEqual(fulltext.pending('rss'), len(after))
        # **反过来钉一边**:同一批里没被搁置的那几条照旧在队列里。
        # 两次取数都没有 ORDER BY,所以比集合而不是比顺序。
        self.assertEqual({k for k in before if k != self.queued}, set(after))
        self.assertEqual({lanes.ARCHIVED: [self.queued]},
                         lanes.split([self.queued], 'rss'))

    def test_a_snoozed_item_is_not_bought_a_translation(self):
        key = key_of('rss', TRANSLATE)
        self.assertEqual(1, translate_scope.pending('rss'))
        ids = self.shelve([key], 'rss', shelf.SNOOZE)
        self.assertEqual(0, translate_scope.pending('rss'))
        # 恢复之后它回到翻译队列 —— 搁置不是放弃。
        self.assertEqual(1, shelf.release([key], 'rss', ids))
        self.assertEqual(1, translate_scope.pending('rss'))

    def test_a_snoozed_shell_is_still_unsettled_so_it_cannot_sneak_in_as_settled(self):
        """②:`unsettled_sql` **刻意不带搁置那一道**,这里反向钉住。

        把搁置加进去的话,这一条壳当场被判成「正文已定」,于是
        `translate_scope.queued_sql` 的 `NOT (unsettled)` 放它过去 —— 一条刚被
        人叫停的内容按壳买走一次翻译。
        """
        agg = reg.subscription('rss', self.ids['agg'])
        key = self.put(agg, 'https://news.example.org/en-shell', body=SHELL,
                       title='An English shell', lang='en')
        self.shelve([key], 'rss', shelf.SNOOZE)
        cond, args = fulltext.unsettled_sql()
        self.assertEqual(1, self.count(f'key=? AND ({cond})', [key, *args]))
        # 两道队列都不要它。
        self.assertNotIn(key, self.queue())
        tcond, targs = translate_scope.queued_sql()
        self.assertEqual(0, self.count(f'key=? AND ({tcond})', [key, *targs]))

    def test_releasing_a_held_item_is_refused_while_it_is_shelved(self):
        """挂起那条既有通道(`fulltext.release`)也要认搁置,否则两颗按钮互相打架:
        人点过「暂缓」,再点一次「仅重新排队」就把它放回去了。
        """
        held = key_of('rss', HELD)
        ids = self.shelve([held], 'rss', shelf.SNOOZE)
        self.assertEqual(0, fulltext.release([held], kind='rss'))
        self.assertEqual(1, shelf.release([held], 'rss', ids))
        # **反过来钉一边**:没搁置的那一条照旧恢复得了。
        self.assertEqual(1, fulltext.release([held], kind='rss'))


class ShelfLifeTests(DiagCase):
    """③:搁置认的是 `feed_items.life` —— 一个不可复用的生命周期标识。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.agg = reg.subscription('rss', self.ids['agg'])
        self.key = key_of('rss', QUEUED)

    def recollect(self, *, keep_fetched_at: str = '') -> None:
        """模拟「存量清理删掉一条、下一轮又从同一个链接收回来」。

        `put_items` 的 `DO NOTHING` 对已存在的 key 不生效,所以先真删行 ——
        **归属行要先删**(外键挡着),顺序照 `scripts/feed_trim_history.py` 和
        `exclusion.drop_item` 那两条真路径。
        """
        with reg.db() as c:
            c.execute('DELETE FROM subscription_items WHERE item_key=?', (self.key,))
            c.execute('DELETE FROM feed_items WHERE key=?', (self.key,))
        reg.put_items(self.agg, [{'url': QUEUED, 'title': '又收回来的同一个链接',
                                  'body': SHELL, 'published': '2026-10-08 09:00',
                                  'content_kind': 'summary'}])
        if keep_fetched_at:
            with reg.db() as c:
                c.execute('UPDATE feed_items SET fetched_at=? WHERE key=?',
                          (keep_fetched_at, self.key))

    def test_every_row_gets_a_life_and_a_recollection_gets_a_different_one(self):
        """那一列由 INSERT 触发器发(`kb/feeds/schema.py`),**五条插入路径共用**。

        漏发的后果是静默的:`life` 留空时 `_match` 的 `item_life<>''` 认不出任何
        东西,于是这一条**永远搁置不上**(按钮点下去库里有记录、面板上不显示、
        队列照旧抓它)。
        """
        first = self.row(self.key)['life']
        self.assertEqual(32, len(first))
        self.recollect()
        self.assertEqual(32, len(self.row(self.key)['life']))
        self.assertNotEqual(first, self.row(self.key)['life'])

    def test_a_later_recollection_does_not_inherit_the_decision(self):
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fetched_at='2026-09-01T00:00:00+00:00'"
                      ' WHERE key=?', (self.key,))
        self.shelve([self.key], 'rss', shelf.EXCLUDE, '这个站点无关')
        self.assertEqual(1, self.count(shelf.live_sql() + ' AND key=?', [self.key]))
        self.recollect()
        self.assertEqual(0, self.count(shelf.live_sql() + ' AND key=?', [self.key]))
        self.assertEqual({'body_queued': [self.key]}, lanes.split([self.key], 'rss'))

    def test_a_same_second_recollection_at_revision_zero_is_still_a_new_life(self):
        """**上一版在这一档上漏判**(审查第 1 轮 F07)。

        上一版的判据是「`item_fetched_at` 相等」加「`content_rev` 没倒退」。
        `fetched_at` 只记到秒,存量清理删掉又收回来撞在同一秒里是常事;而新行的
        `content_rev` 从 0 起 —— 上一世本来就没补过正文、代次也是 0 时,两道
        **同时成立**,这条全新的内容继承了上一世那次「不再收录」:它一次都不会被
        补抓、不会出现在待办里,而面板上什么也看不见。

        所以这里把那一下**逐字摆出来**:同一秒、代次都是 0,两行都不动。判据换成
        `life` 相等之后它被认出来 —— 新收回来的那一篇是一条全新的待办。
        """
        before = self.row(self.key)
        self.assertEqual(0, before['content_rev'])
        self.shelve([self.key], 'rss', shelf.EXCLUDE)
        self.recollect(keep_fetched_at=before['fetched_at'])
        after = self.row(self.key)
        # 上一版那两道**在这里全都成立**,所以它单独挡不住这一条。
        self.assertEqual(before['fetched_at'], after['fetched_at'])
        self.assertEqual(before['content_rev'], after['content_rev'])
        self.assertNotEqual(before['life'], after['life'])
        self.assertEqual(0, self.count(shelf.live_sql() + ' AND key=?', [self.key]))
        self.assertEqual({'body_queued': [self.key]}, lanes.split([self.key], 'rss'))

    def test_an_old_decision_is_neither_live_nor_releasable_after_a_recollection(self):
        """**旧那一世的决定:按钮要失效,`release` 也不许报「已恢复 1 条」。**

        形态(审查第 2 轮 B7):`live_decisions` 原来只查那张旁表,而上一世留下的
        那一行还躺在表上 —— 于是一条删掉重收、**从来没有被搁置过**的内容,处理记录
        里那颗旧「恢复」显示为可点,点下去 `release` 还按 key + 决定编号删掉了那
        一行、报「已恢复 1 条」。两头是同一句话,所以一起钉:**按钮的可点性**和
        **那一下真能删掉的行**问的必须是同一个问题。
        """
        ids = self.shelve([self.key], 'rss', shelf.EXCLUDE, '这个站点无关')
        self.recollect()
        self.assertEqual(0, self.count(shelf.live_sql() + ' AND key=?', [self.key]))
        self.assertEqual([], shelf.live_decisions(ids))
        self.assertEqual(0, shelf.release([self.key], 'rss', ids))

    def test_a_decision_made_in_this_life_is_live_and_releasable(self):
        """**反过来钉一边**:这一世真搁置了的,照旧可点、照旧恢复得了 —— 不然
        「两头一律报没有」也能让上面那条变绿。"""
        self.recollect()
        ids = self.shelve([self.key], 'rss', shelf.EXCLUDE, '这个站点无关')
        self.assertEqual(ids, shelf.live_decisions(ids))
        self.assertEqual(1, shelf.release([self.key], 'rss', ids))
        self.assertEqual(0, self.count(shelf.live_sql() + ' AND key=?', [self.key]))

    def test_a_new_body_version_in_the_same_life_stays_shelved(self):
        """**反过来钉一边**:同一行里换过一版正文的照旧算搁置着 —— 不然按钮会在
        正文更新之后静默失效,而人以为自己已经把它叫停了。
        """
        self.shelve([self.key], 'rss', shelf.SNOOZE)
        with reg.db() as c:
            c.execute('UPDATE feed_items SET content_rev=content_rev+1,'
                      " fetched_at='2026-10-20T00:00:00+00:00' WHERE key=?", (self.key,))
        self.assertEqual(1, self.count(shelf.live_sql() + ' AND key=?', [self.key]))
        self.assertNotIn(self.key, [r['key'] for r in
                                    lanes.rows('body_queued', 'rss')['items']])

    def test_rows_that_predate_the_column_are_backfilled_in_the_same_transaction(self):
        """存量库:那一列是 ALTER 补出来的,**回填必须在同一笔事务里**。

        不回填的话存量那 8850 篇的 `life` 全是空字符串,`_match` 的 `item_life<>''`
        一条都认不出 —— 面板上每一颗「暂缓 / 不再收录」对老内容**静默无效**,
        而新收进来的内容一切正常(所以单看新库测不出来)。
        """
        with reg.db() as c:
            c.execute('DROP TRIGGER IF EXISTS feed_items_life')
            c.execute('ALTER TABLE feed_items DROP COLUMN life')
            self.assertNotIn('life', {r['name'] for r in
                                      c.execute('PRAGMA table_info(feed_items)')})
        # 下一次 `reg.db()` 就是迁移那一笔:补列 + 回填 + 装触发器。
        with reg.db() as c:
            lives = [r[0] for r in c.execute('SELECT life FROM feed_items')]
        self.assertTrue(lives)
        self.assertEqual([], [one for one in lives if len(one) != 32])
        self.assertEqual(len(lives), len(set(lives)))     # 一条一个,没有共用
        # 补回来之后老内容照旧搁置得上。
        self.shelve([self.key], 'rss', shelf.SNOOZE)
        self.assertEqual(1, self.count(shelf.live_sql() + ' AND key=?', [self.key]))

    def test_shelving_again_rewrites_the_row_for_this_life(self):
        """上一世留下的那一行不碍事,下一次搁置时原地改写成这一世。"""
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fetched_at='2026-09-01T00:00:00+00:00'"
                      ' WHERE key=?', (self.key,))
        self.shelve([self.key], 'rss', shelf.EXCLUDE)
        self.recollect()
        self.shelve([self.key], 'rss', shelf.SNOOZE, '这回只是暂缓')
        with reg.db() as c:
            rows = c.execute('SELECT decision,reason FROM feed_item_shelf'
                             ' WHERE item_key=?', (self.key,)).fetchall()
        self.assertEqual([(shelf.SNOOZE, '这回只是暂缓')], [tuple(r) for r in rows])
        self.assertEqual(1, self.count(shelf.live_sql() + ' AND key=?', [self.key]))


class ShelfDecisionTests(DiagCase):
    """④:恢复绑的是「那一次决定」(`decision_id`),不是「这个 key」。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', QUEUED)

    def live(self) -> int:
        return self.count(shelf.live_sql() + ' AND key=?', [self.key])

    def row_shelf(self) -> dict:
        with reg.db() as c:
            return dict(c.execute('SELECT * FROM feed_item_shelf WHERE item_key=?',
                                  (self.key,)).fetchone())

    def test_an_old_undo_cannot_cancel_a_later_decision(self):
        """审查第 1 轮 F08 的那一串,逐步摆出来。

        暂缓 → 恢复 → 不再收录。处理记录里第一条那颗「恢复」此刻**已经失效**:
        只按 key 删的话,它会把后来那次「不再收录」一起取消 —— 人翻了一眼旧记录、
        点了一下和这条内容当前状态毫无关系的按钮,内容静默回到待办里。
        """
        first = self.shelve([self.key], 'rss', shelf.SNOOZE, '等作者补完')
        self.assertEqual(1, shelf.release([self.key], 'rss', first))
        second = self.shelve([self.key], 'rss', shelf.EXCLUDE, '这个站点无关')
        self.assertNotEqual(first, second)
        # 旧那一下:`rowcount==0` 就是拒绝信号,表上那一行一个字都没动。
        self.assertEqual(0, shelf.release([self.key], 'rss', first))
        self.assertEqual(1, self.live())
        self.assertEqual(shelf.EXCLUDE,
                         self.row_shelf()['decision'])
        # **反过来钉一边**:当前那一次的编号照旧恢复得了。
        self.assertEqual(1, shelf.release([self.key], 'rss', second))
        self.assertEqual(0, self.live())

    def test_rewriting_the_decision_in_place_also_invalidates_the_old_undo(self):
        """没有中间那次恢复也一样:原地改写(暂缓 → 不再收录)换了编号。"""
        first = self.shelve([self.key], 'rss', shelf.SNOOZE)
        second = self.shelve([self.key], 'rss', shelf.EXCLUDE)
        self.assertNotEqual(first, second)
        self.assertEqual(0, shelf.release([self.key], 'rss', first))
        self.assertEqual(1, self.live())
        self.assertEqual(1, shelf.release([self.key], 'rss', second))

    def test_a_release_without_the_decision_id_is_refused_outright(self):
        """**缺编号不许退化成按 key 删** —— 退化那一下正是上面那次事故。"""
        self.shelve([self.key], 'rss', shelf.SNOOZE)
        with self.assertRaises(ValueError):
            shelf.release([self.key], 'rss')
        with self.assertRaises(ValueError):
            shelf.release([self.key, '0' * 32], 'rss', ['only-one'])
        self.assertEqual(1, self.live())

    def test_live_decisions_says_which_undo_buttons_are_still_good(self):
        """处理记录那颗「恢复」要**明确失效**,而不是点下去才拿到一句拒绝。"""
        first = self.shelve([self.key], 'rss', shelf.SNOOZE)
        self.assertEqual(first, shelf.live_decisions(first))
        second = self.shelve([self.key], 'rss', shelf.EXCLUDE)
        self.assertEqual([], shelf.live_decisions(first))
        self.assertEqual(second, shelf.live_decisions(second))
        self.assertEqual([], shelf.live_decisions([]))
        self.assertEqual([], shelf.live_decisions(['', None]))


class ShelfGuardTests(DiagCase):
    """写语句自己的 WHERE 说了算,`rowcount==0` 就是拒绝信号。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', QUEUED)

    def test_another_source_cannot_shelve_or_release_this_item(self):
        self.assertEqual([], self.shelve([self.key], 'web', shelf.SNOOZE))
        self.assertEqual(0, self.count(shelf.live_sql() + ' AND key=?', [self.key]))
        ids = self.shelve([self.key], 'rss', shelf.SNOOZE)
        self.assertEqual(0, shelf.release([self.key], 'web', ids))
        self.assertEqual(1, self.count(shelf.live_sql() + ' AND key=?', [self.key]))
        self.assertEqual(1, shelf.release([self.key], 'rss', ids))

    def test_an_unknown_key_writes_nothing_and_does_not_raise(self):
        self.assertEqual([], self.shelve(['0' * 32], 'rss', shelf.SNOOZE))
        self.assertEqual(0, shelf.release(['0' * 32], 'rss', ['whatever']))
        self.assertEqual([], self.shelve([], 'rss', shelf.SNOOZE))

    def test_an_unknown_decision_is_refused_before_anything_is_written(self):
        with self.assertRaises(ValueError):
            shelf.shelve([self.key], 'rss', 'delete')
        self.assertEqual(0, self.count(shelf.live_sql() + ' AND key=?', [self.key]))

    def test_the_two_decisions_stay_apart_and_neither_counts_as_resolved(self):
        """状态契约:已搁置里「暂缓」和「不再收录」分别显示,**都不计为解决**。"""
        listed = {row['title']: row['shelf_decision'] for row in lanes.shelved()['items']}
        self.assertEqual({'被暂缓的一条': shelf.SNOOZE,
                          '被标成不再收录的一条': shelf.EXCLUDE}, listed)
        both = [key_of('rss', 'https://news.example.org/snoozed'),
                key_of('rss', 'https://news.example.org/excluded')]
        self.assertEqual({lanes.ARCHIVED: set(both)},
                         {name: set(rows)
                          for name, rows in lanes.split(both, 'rss').items()})
        resolved = [lane.id for lane in lanes.LANES if lane.group == lanes.RESOLVED]
        for lane_id in resolved:
            with self.subTest(lane=lane_id):
                listed_keys = [r['key'] for r in lanes.rows(lane_id, 'rss')['items']]
                self.assertEqual([], [k for k in both if k in listed_keys])


class ShelfIsNotExclusionTests(DiagCase):
    """⑤:它不是 `kb/feeds/exclusion.py` 那一份永久移除。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', QUEUED)

    def test_marking_it_excluded_keeps_every_byte_and_can_be_taken_back(self):
        before = self.row(self.key)
        ids = self.shelve([self.key], 'rss', shelf.EXCLUDE, '站点无关')
        self.assertEqual(before, self.row(self.key))      # 一列都没动
        with reg.db() as c:
            left = c.execute('SELECT count(*) FROM feed_item_exclusions').fetchone()[0]
        self.assertEqual(0, left)                          # 永久排除那张表没碰
        self.assertEqual(1, shelf.release([self.key], 'rss', ids))
        self.assertEqual(before, self.row(self.key))
        self.assertIn(self.key, [r['key'] for r in
                                 lanes.rows('body_queued', 'rss')['items']])
