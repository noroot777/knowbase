# -*- coding: utf-8 -*-
"""**「仅重新排队」「换链接」「处理记录」三个出口**(`kb/intake_diag/actions.py`、
`ledger.py`)。

这三个出口的共同点是**改完之后从列表上看不出来**:「仅重新排队」把重试次数清零,
那一条当场从「重试耗尽」跳回「排队等补齐」,跟从来没失败过长得一样;「换链接」只换
补抓地址,那一行的标题和原地址一个字都不变。所以这一份钉的是:

① **统计按实际写入结果报**(元规则)。给 5 个 key 只放回 3 条就说 3,跳过的如实
   列出 —— 按计划长度报的话,人以为自己把那两条放回去了,而下一轮根本不会碰它们。
② **分流按条目此刻真正所在的那一档**,不按前端传来的说法(页面上那一份可能已经
   过时几秒)。
③ **不顺手打上付费标记**。`translate_manual` 是「立即执行」那一轮的付费边界,而它
   在每一轮重试开始时会被整类清掉 —— 在这里打上既到不了那一轮,又会让下一次常规
   同步把这一批顶到队首。
④ **换链接不动身份那一列**。`item_key` 按原网址算:换掉 `url` 会让
   `exclusion.failure()` 那道 `item_key(channel,url)==key` 不再成立,失败明细里
   「不再抓取」那颗按钮**静默消失**;而把 key 一起重算就不是这条内容了。新地址
   只决定「这一次去哪儿抓」。
⑤ **处理记录不伪造一次没发生过的失败**。只有搁置那一个出口真的可撤销(删一行就
   回到原状),其余几个记录在案、不给撤销按钮。**而且它认「这一世」** —— 同一个链
   接删掉重收之后,上一世那几行不许挂在新那一条下面。
⑥ **「立即执行」要真的碰到人选的那几条**(`NamedBodyRetryTests`)。那两档本来就
   不在补抓队列里,只传一个 `channel` 的后果是人勾的那几条一次也没被重试,而页面
   说已开始。

只有 `fulltext.fetch_one` 和 `pipeline_state.get_state` 打桩(前者那一层只连公网
地址,后者摆的是「这一轮的点名意图」),判据和文案一个字都没打。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.feeds import fulltext, registry as reg
from kb.intake_diag import actions, ledger, lanes, shelf
from tests.intake_diag_case import DiagCase, key_of

SHELL = '标题壳 (\nexample.org\n)\n10-08\n108 HN Points'
QUEUED = 'https://news.example.org/queued'
HELD = 'https://news.example.org/held'
EXHAUSTED = 'https://news.example.org/exhausted'
TR_EXHAUSTED = 'https://solo.example.org/translate-exhausted'
DONE = 'https://news.example.org/recent'


class RequeueTests(DiagCase):
    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def test_it_reports_the_rows_it_actually_wrote(self):
        """三档各一条 + 一条已经不在积压里的,一次交给它。"""
        done = key_of('rss', DONE)
        keys = [key_of('rss', HELD), key_of('rss', EXHAUSTED),
                key_of('rss', TR_EXHAUSTED), done]
        result = actions.requeue(keys, 'rss')
        self.assertEqual(
            {'released': 1, 'body': 1, 'translate': 1, 'requeued': 3,
             'skipped': [done]}, result)
        # 额度真的还回去了,而且三条都回到了「等调度」那一组。
        for url in (HELD, EXHAUSTED):
            with self.subTest(url=url):
                row = self.row(key_of('rss', url))
                self.assertEqual(0, row['fulltext_tries'])
                self.assertEqual('', row['fulltext_hold'])
                self.assertEqual('', row['fulltext_error'])
        self.assertEqual(0, self.row(key_of('rss', TR_EXHAUSTED))['translate_tries'])
        # 桶内顺序由 `IN` 查询给,没有 ORDER BY —— 比集合,不比顺序。
        self.assertEqual(
            {'body_queued': {key_of('rss', HELD), key_of('rss', EXHAUSTED)},
             'translate_queued': {key_of('rss', TR_EXHAUSTED)}},
            {name: set(rows) for name, rows in lanes.split(keys[:3], 'rss').items()})

    def test_it_does_not_mark_the_batch_as_paid_translation(self):
        """③:`translate_manual` 是付费边界,这颗按钮承诺的只是「下一轮会轮到它」。"""
        key = key_of('rss', TR_EXHAUSTED)
        actions.requeue([key], 'rss')
        self.assertEqual(0, self.row(key)['translate_manual'])

    def test_an_item_from_another_source_is_not_touched(self):
        key = key_of('rss', EXHAUSTED)
        before = self.row(key)
        result = actions.requeue([key], 'web')
        self.assertEqual({'released': 0, 'body': 0, 'translate': 0, 'requeued': 0,
                          'skipped': [key]}, result)
        self.assertEqual(before, self.row(key))

    def test_a_shelved_item_stays_shelved(self):
        """**反过来钉一边**:人说过「先别动」的那一条,这颗按钮也不许把它放回去。"""
        key = key_of('rss', EXHAUSTED)
        self.shelve([key], 'rss', shelf.SNOOZE)
        before = self.row(key)
        self.assertEqual({'released': 0, 'body': 0, 'translate': 0, 'requeued': 0,
                          'skipped': [key]}, actions.requeue([key], 'rss'))
        self.assertEqual(before, self.row(key))

    def test_duplicates_in_one_batch_are_written_once(self):
        key = key_of('rss', EXHAUSTED)
        self.assertEqual(1, actions.requeue([key, key, key], 'rss')['requeued'])

    def test_an_empty_batch_is_a_no_op_not_an_error(self):
        self.assertEqual({'released': 0, 'body': 0, 'translate': 0, 'requeued': 0,
                          'skipped': []}, actions.requeue([], 'rss'))


class RelinkTests(DiagCase):
    """④:换一个取正文的地址,身份那一列一个字都不动。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', QUEUED)

    def test_the_identity_column_is_left_alone_and_the_new_address_goes_elsewhere(self):
        before = self.row(self.key)
        result = actions.relink(self.key, 'rss', 'https://blog.example.org/real-post')
        self.assertEqual({'key': self.key,
                          'url': 'https://blog.example.org/real-post'}, result)
        row = self.row(self.key)
        self.assertEqual(before['url'], row['url'])
        self.assertEqual('https://blog.example.org/real-post', row['fulltext_url'])
        self.assertEqual(before['body'], row['body'])          # 这一下不抓
        self.assertEqual('', row['fulltext_at'])
        self.assertEqual(0, row['fulltext_tries'])
        self.assertEqual('', row['fulltext_hold'])
        self.assertEqual('', row['fulltext_error'])

    def test_a_held_item_can_be_relinked_and_comes_back_to_the_queue(self):
        held = key_of('rss', HELD)
        self.assertNotIn(held, [r['key'] for r in
                                lanes.rows('body_queued', 'rss')['items']])
        actions.relink(held, 'rss', 'https://blog.example.org/other')
        self.assertIn(held, [r['key'] for r in
                             lanes.rows('body_queued', 'rss')['items']])

    def test_an_address_that_is_not_a_page_is_refused(self):
        """PDF 的字节解出来是一段 `%PDF-1.4 …` 加几百个替换字符,长度轻松越过正文
        门槛 —— 于是「补抓成功」、条目离开队列,还拿这堆乱码去买一次翻译。
        """
        for bad in ('https://e.org/paper.pdf', 'https://e.org/deck.PPTX',
                    'ftp://e.org/post', 'mailto:someone@e.org', '不是地址'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    actions.relink(self.key, 'rss', bad)
        self.assertEqual('', self.row(self.key)['fulltext_url'])

    def test_an_item_whose_body_is_already_settled_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            actions.relink(key_of('rss', DONE), 'rss', 'https://blog.example.org/x')
        self.assertIn('地址没有改动', str(caught.exception))

    def test_another_source_cannot_relink_this_item(self):
        with self.assertRaises(ValueError):
            actions.relink(self.key, 'web', 'https://blog.example.org/x')
        self.assertEqual('', self.row(self.key)['fulltext_url'])


class RelinkFetchTargetTests(DiagCase):
    """换过链接之后,下一轮**真的**去新地址取 —— 只摆一条,免得排序来猜。"""

    def test_the_next_round_fetches_the_new_address(self):
        source = reg.save_subscription(
            'rss', {'name': '聚合源', 'url': 'https://agg.example.org/feed.xml',
                    'fulltext': True})
        key = self.put(source, 'https://news.example.org/redirect', body=SHELL)
        actions.relink(key, 'rss', 'https://blog.example.org/real-post')
        seen: list[str] = []

        def fake(url: str):
            seen.append(url)
            return '', '页面上没有识别出正文'

        with patch.object(fulltext, 'fetch_one', fake):
            fulltext.fetch_pending('rss', limit=1)
        self.assertEqual(['https://blog.example.org/real-post'], seen)
        # 身份那一列照旧是原网址 —— 失败明细里那颗「不再抓取」还认得它。
        self.assertEqual('https://news.example.org/redirect', self.row(key)['url'])


class NamedBodyRetryTests(DiagCase):
    """**「立即执行」要真的碰到人选的那几条**(审查第 1 轮 F02)。

    上一版的 `run()` 只往 `crawl_filter` 里放了一个 `channel`,于是那一轮的补抓
    按 `fulltext.queued_sql` 取数 —— 而「待人工确认」和「重试已停止」**本来就不在
    那个队列里**(前者等的是人,后者额度已经用完)。结果:人在「需处理」里勾了
    那几条、点了「立即执行」,页面说已开始、同步中心真跑了一轮,而他勾的那几条
    **一次也没被重试**,下一次打开面板它们还在原处。

    链条三节,这里逐节钉住:
    ① `actions.run` 把点名意图放进 `crawl_filter`(**只放那一份原子状态**);
    ② `FeedCollector._body_retry` 只认属于自己那一类来源的意图;
    ③ `fulltext.reopen` 按现态解除限制,`fetch_pending(keys=)` 让它们排在队首。
    """

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.held = key_of('rss', HELD)
        self.exhausted = key_of('rss', EXHAUSTED)

    def test_reopen_releases_the_held_and_refunds_the_exhausted(self):
        done = key_of('rss', DONE)
        before = self.row(done)
        self.assertEqual({'released': 1, 'body': 1},
                         fulltext.reopen([self.held, self.exhausted, done], 'rss'))
        self.assertEqual('', self.row(self.held)['fulltext_hold'])
        self.assertEqual(0, self.row(self.exhausted)['fulltext_tries'])
        self.assertEqual('', self.row(self.exhausted)['fulltext_error'])
        # 已经走完的那一条不在射程里 —— 统计按实际写入结果报(元规则)。
        self.assertEqual(before, self.row(done))

    def test_items_that_were_not_picked_keep_their_hold_and_their_spent_budget(self):
        """**反过来钉一边**:这颗按钮的射程是「人勾的那几条」,不是整档。

        整档放开的后果是人勾一条、几百条一起回到队列 —— 而确认框里写的是前者。
        """
        self.assertEqual({'released': 1, 'body': 0},
                         fulltext.reopen([self.held], 'rss'))
        self.assertEqual(fulltext.MAX_TRIES, self.row(self.exhausted)['fulltext_tries'])
        self.assertNotEqual('', self.row(self.exhausted)['fulltext_error'])

    def test_a_shelved_item_is_not_reopened_by_the_named_round(self):
        """人说过「先别动」的那一条,点名也不许把它放回去。"""
        self.shelve([self.held, self.exhausted], 'rss', shelf.SNOOZE)
        before = (self.row(self.held), self.row(self.exhausted))
        self.assertEqual({'released': 0, 'body': 0},
                         fulltext.reopen([self.held, self.exhausted], 'rss'))
        self.assertEqual(before, (self.row(self.held), self.row(self.exhausted)))

    def test_another_source_cannot_reopen_these_items(self):
        before = self.row(self.exhausted)
        self.assertEqual({'released': 0, 'body': 0},
                         fulltext.reopen([self.held, self.exhausted], 'web'))
        self.assertEqual(before, self.row(self.exhausted))
        self.assertEqual({'released': 0, 'body': 0}, fulltext.reopen([], 'rss'))

    def backlog(self) -> None:
        """摆 4 条「从没试过」的积压,并把点名那一条还回额度。

        **积压比它新,这一点要显式摆出来。** 取数的第三段排序是 `fetched_at DESC`,
        而样例和这几条是同一秒落库的 —— 不拉开的话「不点名时先取谁」由 key 的哈希
        决定,反面那一半就成了抛硬币。拉开之后的形态正是下面说的那一种:人点的那
        一条本来排在后面。
        """
        agg = reg.subscription('rss', self.ids['agg'])
        for i in range(4):
            self.put(agg, f'https://news.example.org/backlog-{i}', body=SHELL,
                     title=f'积压里的第 {i} 条')
        with reg.db() as c:
            c.execute("UPDATE feed_items SET fetched_at='2026-01-01T00:00:00+00:00'"
                      ' WHERE key=?', (self.exhausted,))
        fulltext.reopen([self.exhausted], 'rss')

    def one_round(self, **kwargs) -> list[str]:
        """跑一轮上限 1 条的补抓,返回这一轮去取了哪几个地址。"""
        seen: list[str] = []

        def fake(url: str):
            seen.append(url)
            return '', '页面上没有识别出正文'

        with patch.object(fulltext, 'fetch_one', fake):
            fulltext.fetch_pending('rss', limit=1, **kwargs)
        return seen

    def test_the_named_items_are_fetched_first_even_under_a_backlog(self):
        """③ 的后半:放回队列把额度清成 0,而取数是「失败过的优先」。

        不插队的话它们排在几百条「从没试过」的后面,一轮上限 40 条 ——
        人点下去那一轮照旧一条都轮不到,而页面说已开始。
        """
        self.backlog()
        self.assertEqual([self.row(self.exhausted)['url']],
                         self.one_round(keys=[self.exhausted]))

    def test_the_same_round_takes_someone_else_when_nobody_is_named(self):
        """**反过来钉一边**:没点名的时候这一轮取的是别人 —— 不然上面那条在
        「永远只抓这一条」的实现上一样是绿的。

        **单独一条用例,不接在上面那一轮后面跑。** 上面那一轮会给点名那一条记一次
        失败,而取数是「失败过的优先」—— 于是它在紧接着的第二轮里本来就该排最前面,
        接着跑的话这条断言验的是另一件事(实测:红在这里,而插队那一道是好的)。
        """
        self.backlog()
        seen = self.one_round()
        self.assertEqual(1, len(seen))
        self.assertNotIn('/exhausted', seen[0])

    def test_the_intent_only_reaches_the_collector_that_owns_it(self):
        """②:意图从 `crawl_filter` 里读,而且**只认属于自己那一类来源的那一份**。

        不认 channel 的话,点名重试 RSS 那几条会让网页专栏那一类也拿着同一批 key
        去放回队列 —— 而那几个 key 压根不属于它(写入那一侧的 `kind` 判据会挡住,
        但那时日志里已经报了一句「点名放回 N 条」)。
        """
        from kb import pipeline_state
        from kb.collectors.feeds import FeedCollector

        wanted = {'channel': 'rss', 'fulltext_retry': {'channel': 'rss',
                                                       'keys': [self.exhausted]}}
        for kind, expected in (('rss', wanted['fulltext_retry']), ('web', None)):
            with self.subTest(kind=kind):
                with patch.object(pipeline_state, 'get_state',
                                  lambda: {'crawl_filter': wanted}):
                    self.assertEqual(expected, FeedCollector(kind)._body_retry())
        # 形状不对、或者压根没有这一份时一律当没点名(不许崩在这里:这一下跑在
        # 导出阶段的执行体里,崩掉就是整轮同步红)。
        for state in ({}, {'crawl_filter': None}, {'crawl_filter': {'channel': 'rss'}},
                      {'crawl_filter': {'fulltext_retry': 'rss'}}):
            with self.subTest(state=state):
                with patch.object(pipeline_state, 'get_state', lambda: dict(state)):
                    self.assertIsNone(FeedCollector('rss')._body_retry())


class CareLogTests(DiagCase):
    """⑤:处理记录。它只是旁证,不参与任何判据。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', QUEUED)

    def log_rows(self) -> int:
        """**表里**还有几行(不是列出来几行):窗口那一刀要的是真删掉。"""
        with reg.db() as c:
            return c.execute('SELECT count(*) FROM intake_care_log').fetchone()[0]

    def test_only_the_shelf_offers_a_way_back(self):
        """把 `fulltext_tries` 按快照写回 `MAX_TRIES` 是在伪造一次没发生过的失败,
        而下一轮补抓会照着这个假数字拒绝重试它。所以其余出口不给撤销按钮。
        """
        ledger.record('requeue', 'rss', '仅重新排队 1 条', affected=1, keys=[self.key])
        ledger.record('relink', 'rss', '换链接 1 条', affected=1, keys=[self.key])
        ledger.record('shelf', 'rss', '暂缓 1 条', affected=1, keys=[self.key],
                      undo=ledger.UNDO_RELEASE)
        undoable = {row['action']: row['undo'] for row in ledger.recent('rss')}
        self.assertEqual({'requeue': '', 'relink': '',
                          'shelf': ledger.UNDO_RELEASE}, undoable)

    def test_it_records_the_number_of_rows_that_were_really_written(self):
        result = actions.requeue([key_of('rss', EXHAUSTED), key_of('rss', DONE)], 'rss')
        row_id = ledger.record('requeue', 'rss', f'仅重新排队 {result["requeued"]} 条',
                               affected=result['requeued'],
                               keys=[key_of('rss', EXHAUSTED)])
        self.assertTrue(row_id)
        latest = ledger.recent('rss')[0]
        self.assertEqual(1, latest['affected'])
        self.assertIn('1 条', latest['summary'])

    def test_a_records_own_item_is_found_by_the_whole_quoted_key(self):
        other = key_of('rss', HELD)
        ledger.record('shelf', 'rss', '暂缓 1 条', affected=1, keys=[self.key],
                      undo=ledger.UNDO_RELEASE)
        life = self.row(self.key)['life']
        self.assertEqual(1, len(ledger.for_item(self.key, life)))
        self.assertEqual([], ledger.for_item(other, self.row(other)['life']))

    def test_a_records_from_a_previous_life_are_not_shown_on_the_new_item(self):
        """同一个链接删掉重收之后,**上一世的处理记录不许挂在新那一条下面**。

        那几行说的是「我对**那一份**内容做过什么」—— 挂在一条全新的待办下面,人
        看到的是一份跟眼前内容无关的历史(「你暂缓过它」),而他压根没有。
        """
        ledger.record('shelf', 'rss', '暂缓 1 条', affected=1, keys=[self.key],
                      undo=ledger.UNDO_RELEASE)
        old_life = self.row(self.key)['life']
        self.assertEqual(1, len(ledger.for_item(self.key, old_life)))
        with reg.db() as c:
            c.execute('DELETE FROM subscription_items WHERE item_key=?', (self.key,))
            c.execute('DELETE FROM feed_items WHERE key=?', (self.key,))
        reg.put_items(reg.subscription('rss', self.ids['agg']),
                      [{'url': QUEUED, 'title': '又收回来的同一个链接', 'body': SHELL,
                        'published': '2026-10-08 09:00', 'content_kind': 'summary'}])
        new_life = self.row(self.key)['life']
        self.assertNotEqual(old_life, new_life)
        self.assertEqual([], ledger.for_item(self.key, new_life))
        # 认不出「这是哪一世」时一律当没有,不退化成按 key 查。
        self.assertEqual([], ledger.for_item(self.key, ''))

    def test_records_past_the_window_are_swept_when_a_new_one_lands(self):
        old = (datetime.now(timezone.utc) - timedelta(days=ledger.KEEP_DAYS + 2))
        with reg.db() as c:
            c.execute('INSERT INTO intake_care_log (at,action,kind,summary)'
                      " VALUES (?,'requeue','rss','很久以前那一次')",
                      (old.isoformat(timespec='seconds'),))
        # **读那一侧按同一个下界过滤**,所以窗口外那一行从一开始就不会显示 ——
        # 这里要钉的是它真被**删掉**了,所以数表里的行数,不数列出来几行。只看
        # `recent()` 的话「只滤不清」也是绿的,而那样表会一直长。
        self.assertEqual(1, self.log_rows())
        self.assertEqual([], ledger.recent('rss'))
        ledger.record('shelf', 'rss', '暂缓 1 条', affected=1, keys=[self.key])
        kept = [row['summary'] for row in ledger.recent('rss')]
        self.assertEqual(['暂缓 1 条'], kept)
        self.assertEqual(1, self.log_rows())

    def test_the_cutoff_has_the_same_shape_as_the_stored_timestamps(self):
        """`at` 存的是 `reg.now()` 的 UTC ISO 带偏移。拿 `datetime('now','-30 day')`
        那种没有 `T`、没有偏移的形状去比,会在日界上判反(`'T' > ' '`)——
        和 `lanes.resolved_cutoff` 同一个坑。
        """
        cutoff, stamp = ledger._cutoff(), reg.now()
        self.assertTrue(datetime.fromisoformat(cutoff).tzinfo)
        self.assertIn('T', cutoff)
        self.assertEqual(len(stamp), len(cutoff))
        self.assertLess(cutoff, stamp)

    def test_it_keeps_a_sample_of_keys_not_the_whole_batch(self):
        keys = [f'{i:032x}' for i in range(ledger.MAX_KEYS + 20)]
        ledger.record('shelf', 'rss', '暂缓很多条', affected=len(keys), keys=keys)
        self.assertEqual(ledger.MAX_KEYS, len(ledger.recent('rss')[0]['item_keys']))

    def test_the_log_never_decides_anything(self):
        """面板上的档位和计数一律现查 `feed_items`,不读这张表。"""
        before = lanes.counts()['rss']
        ledger.record('shelf', 'rss', '暂缓 1 条', affected=1, keys=[self.key],
                      undo=ledger.UNDO_RELEASE)
        self.assertEqual(before, lanes.counts()['rss'])
