# -*- coding: utf-8 -*-
"""**「还有多少在等、卡在哪一环」那份计数口径**(`kb/intake_diag/lanes.py`)。

被测的那件事:排查那天只能靠手写 SQL 才问得出「RSS 待补 212、重试耗尽 226、
网页专栏耗尽 1」,面板上一个痕迹都没有。做出来之后,**面板给的数必须和手写
SQL 一样** —— 它是人决定「要不要花这笔钱重跑」的全部依据。

这一份钉四件事:
① **各档相加等于总数**。原始判据彼此重叠(一条正文试满额度的英文内容按设计会
   带着壳走完翻译),各档不减掉前面几档就是同一条内容数两遍 —— 屏幕上「顶上
   403 条、五档加起来 405 条」,而人看不出多出来的两条是什么。
② **计数、列表、分桶三处同一个答案**。各用各的 WHERE,点进去的那一屏和顶上
   那个数就会分叉。
③ **三档正文状态分开**(还没轮到 / 等人看一眼 / 预算用完),不统称「抓取中」:
   三者的下一步完全不同。
④ **反过来钉一边**:还没补抓的那几百条壳不许算进「等着写入知识库」,合法的
   空集照旧是空集,别的来源的 key 提进来数不进去。
⑤ **真处理成功之后那一条要进得了「已完成」**。判据不许挂在失败计数上:放回队列
   和翻译成功都会把它清零,于是「重试 → 成功 → 入库」这条最常走的路反而一条都
   找不到(`RealSuccessClosesTheLaneTests`)。

没有打桩:临时 registry 真建表、真写条目,判据真跑 SQLite。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kb.feeds import fulltext, registry as reg, translate_scope
from kb.intake_diag import lanes
from tests.intake_diag_case import DiagCase, key_of

SHELL = '标题壳 (\nexample.org\n)\n10-08\n108 HN Points'
FULL = '\n\n'.join(
    '这是样例正文第 %d 段，它和别的段说的不是同一件事，整页加起来要过得了正文'
    '提取器那道最小长度的门槛，而且不能是同一段在复读。' % i for i in range(1, 10))


def union_where() -> tuple[str, list]:
    """所有档位的**原始**判据取并集,再减掉已搁置 —— 这就是「总共有多少条内容
    还没走完收录」那个数的另一种写法(独立于 `exclusive_where` 的那一份)。"""
    parts, args, archived = [], [], ''
    for name, cond, cargs in lanes.probes():
        if name == lanes.ARCHIVED:
            archived = cond
            continue
        parts.append(f'({cond})')
        args.extend(cargs)
    return '(' + ' OR '.join(parts) + f') AND NOT ({archived})', args


class LaneCountTests(DiagCase):
    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def total(self) -> int:
        cond, args = union_where()
        return self.count(cond, args)

    def test_each_lane_adds_up_to_the_total_with_nothing_counted_twice(self):
        counts = lanes.counts()
        summed = sum(n for per_kind in counts.values() for n in per_kind.values())
        self.assertEqual(self.total(), summed)
        # 这一份样例每一档都占上了,所以「全 0 也相等」那种空洞的通过不成立。
        self.assertEqual(
            {'body_held': 1, 'body_exhausted': 1, 'translate_exhausted': 1,
             'body_queued': 1, 'translate_queued': 1, 'ingest_queued': 1,
             'body_recent': 1},
            {lane: n for lane, n in counts['rss'].items() if n})
        self.assertEqual(7, summed)

    def test_an_item_matching_two_raw_predicates_is_counted_once_at_its_root_cause(self):
        """正文试满额度的英文内容**同时**满足「正文耗尽」和「待翻译」。

        这不是 bug:生产管道两步都会碰它(`fulltext.unsettled_sql` 刻意放它过去,
        按壳走完翻译和导出)。可屏幕上它只能算一条,而且只能算在**根因**那一档 ——
        人要做的是把正文弄对,不是再买一次翻译。
        """
        agg = reg.subscription('rss', self.ids['agg'])
        key = self.put(agg, 'https://news.example.org/en-exhausted', body=SHELL,
                       title='An English shell that ran out of retries',
                       lang='en', fulltext_tries=fulltext.MAX_TRIES)
        body_cond, body_args = fulltext.exhausted_sql()
        tr_cond, tr_args = translate_scope.queued_sql()
        self.assertEqual(1, self.count(f'key=? AND ({body_cond})', [key, *body_args]))
        self.assertEqual(1, self.count(f'key=? AND ({tr_cond})', [key, *tr_args]))
        # 可它只出现在一档里,而且是根因那一档。
        self.assertEqual({'body_exhausted': [key]}, lanes.split([key], 'rss'))
        counts = lanes.counts()
        summed = sum(n for per_kind in counts.values() for n in per_kind.values())
        self.assertEqual(self.total(), summed)
        self.assertEqual(8, summed)

    def test_counts_list_and_bucketing_give_the_same_answer(self):
        counts = lanes.counts()['rss']
        every = [row['key'] for row in lanes.shelved()['items']]
        for lane in lanes.LANES:
            every += [row['key'] for row in lanes.rows(lane.id, 'rss')['items']]
        buckets = lanes.split(every, 'rss')
        for lane in lanes.LANES:
            with self.subTest(lane=lane.id):
                listed = lanes.rows(lane.id, 'rss')
                self.assertEqual(counts[lane.id], listed['total'])
                self.assertEqual(counts[lane.id], len(listed['items']))
                self.assertEqual(counts[lane.id], len(buckets.get(lane.id, [])))
        # 每个 key 只落进一个桶 —— 桶与桶之间没有交集。
        seen = [key for rows in buckets.values() for key in rows]
        self.assertEqual(len(seen), len(set(seen)))

    def test_the_count_matches_a_second_independently_written_query(self):
        """**另写一遍同样的 SQL 做比对**(就是排查那天手写的那两条)。

        故意不调 `fulltext.*`:面板给的数和一个独立的拼写必须一致,否则「面板
        和手写 SQL 对不上」这件事根本验不到。
        """
        queued = self.count(
            "fulltext_at='' AND fulltext_hold='' AND fulltext_tries<?"
            ' AND EXISTS(SELECT 1 FROM subscription_items si JOIN subscriptions s'
            ' ON s.id=si.subscription_id WHERE si.item_key=feed_items.key AND s.fulltext=1)'
            ' AND NOT EXISTS(SELECT 1 FROM feed_item_shelf sh'
            " WHERE sh.item_key=feed_items.key AND sh.item_life<>''"
            ' AND sh.item_life=feed_items.life)', [fulltext.MAX_TRIES])
        exhausted = self.count(
            "fulltext_at='' AND fulltext_hold='' AND fulltext_tries>=?"
            ' AND EXISTS(SELECT 1 FROM subscription_items si JOIN subscriptions s'
            ' ON s.id=si.subscription_id WHERE si.item_key=feed_items.key AND s.fulltext=1)'
            ' AND NOT EXISTS(SELECT 1 FROM feed_item_shelf sh'
            " WHERE sh.item_key=feed_items.key AND sh.item_life<>''"
            ' AND sh.item_life=feed_items.life)', [fulltext.MAX_TRIES])
        counts = lanes.counts(['rss'])['rss']
        self.assertEqual(queued, counts['body_queued'])
        self.assertEqual(exhausted, counts['body_exhausted'])
        self.assertEqual((1, 1), (queued, exhausted))

    def test_the_three_fulltext_states_never_collapse_into_one(self):
        """还没轮到 / 等人看一眼 / 预算用完 —— 三者的下一步完全不同。"""
        three = [lanes.BY_ID[name] for name in
                 ('body_queued', 'body_held', 'body_exhausted')]
        self.assertEqual(3, len({lane.state for lane in three}))
        self.assertEqual(3, len({lane.summary for lane in three}))
        for lane in lanes.LANES:
            with self.subTest(lane=lane.id):
                self.assertNotIn('抓取中', lane.state + lane.summary)
        # 三条样例真的分别落在三档里。
        self.assertEqual(
            {'body_queued': [key_of('rss', 'https://news.example.org/queued')],
             'body_held': [key_of('rss', 'https://news.example.org/held')],
             'body_exhausted': [key_of('rss', 'https://news.example.org/exhausted')]},
            lanes.split([key_of('rss', 'https://news.example.org/queued'),
                         key_of('rss', 'https://news.example.org/held'),
                         key_of('rss', 'https://news.example.org/exhausted')], 'rss'))

    def test_a_shell_that_was_never_fetched_is_not_pending_ingest(self):
        """**反过来钉一边**:那 212 条还没补抓的壳,导出闸门正扣着它们,所以
        `exported_hash<>content_hash` 同样为真 —— 它们不许在「等着写入知识库」里
        再数一遍,顶上那个总数会当场翻倍。

        **拦住它的是档位顺序,不是 `fulltext_at<>''`**(审查第 1 轮 F06):补抓
        三档排在这一档前面,`exclusive_where` 把它们整个减掉。原先那一道写在
        `ingest_queued` 自己的判据里,顺带把**压根不需要补抓**的内容一起挡在了
        面板外面 —— 见下面那条。
        """
        shell = key_of('rss', 'https://news.example.org/queued')
        self.assertEqual('', self.row(shell)['fulltext_at'])
        self.assertNotEqual(self.row(shell)['exported_hash'],
                            self.row(shell)['content_hash'])
        self.assertNotIn(shell, [row['key'] for row in
                                 lanes.rows('ingest_queued', 'rss')['items']])
        # 而正文真定下来、还没导出的那一条照旧在这一档里(别把闸门钉死)。
        settled = key_of('rss', 'https://news.example.org/ingest')
        self.assertEqual([settled], [row['key'] for row in
                                     lanes.rows('ingest_queued', 'rss')['items']])

    def test_a_plain_item_that_needs_no_fetching_is_still_waiting_to_be_ingested(self):
        """**上一版的盲区**(审查第 1 轮 F06):自带中文全文、来源没开补抓的内容,
        `fulltext_at` 永远是空的 —— 而它确实在等着写进知识库。

        原先 `ingest_queued` 要求 `fulltext_at<>''`,于是这一条**一个档位都不属于**:
        顶上那个「还有多少在等」漏掉它,点进去也找不到,而它就是真实积压里最常见
        的一类(RSS 自带全文的博客、手动收录、网页专栏)。这里把它摆出来 ——
        既不需要补抓(来源没开)、也不需要翻译(中文),只差一轮导出。
        """
        plain = reg.subscription('rss', self.ids['plain'])
        before = lanes.counts(['rss'])['rss']
        key = self.put(plain, 'https://solo.example.org/zh-full', body=FULL,
                       title='自带中文全文、不用补抓的一条', content_kind='content')
        row = self.row(key)
        self.assertEqual('', row['fulltext_at'])
        self.assertEqual(0, row['fulltext_tries'])
        self.assertEqual('', row['exported_hash'])
        body_cond, body_args = fulltext.queued_sql()
        self.assertEqual(0, self.count(f'key=? AND ({body_cond})', [key, *body_args]))
        tr_cond, tr_args = translate_scope.queued_sql()
        self.assertEqual(0, self.count(f'key=? AND ({tr_cond})', [key, *tr_args]))
        # 它就在「等着写入知识库」那一档里,而且总数跟着 +1。
        self.assertEqual({'ingest_queued': [key]}, lanes.split([key], 'rss'))
        after = lanes.counts(['rss'])['rss']
        self.assertEqual(before['ingest_queued'] + 1, after['ingest_queued'])
        self.assertEqual(sum(before.values()) + 1, sum(after.values()))
        self.assertEqual(self.total(), sum(after.values()))

    def test_a_body_that_is_fetched_but_not_exported_is_not_called_finished(self):
        """「已完成」那两档的判据里**必须自己带一句「知识库里就是这一份」**
        (审查第 1 轮 F06)。

        补抓成功 ≠ 已交付:正文刚补齐、还没轮到导出的那一条,此刻库里躺的还是
        那个几十字的跳转壳(或者什么都没有)。把它算进「已完成」等于告诉人
        「这件事办完了」,而检索里压根搜不到 —— 而且它恰好是这条链上最常见的中间
        态(补抓和导出是同一轮里先后两步)。

        **不许靠「排在后面」推**:档位顺序只保证它不会被数两遍,答不出「它算不算
        完成」。所以这里直接问「已完成」那一档。
        """
        agg = reg.subscription('rss', self.ids['agg'])
        key = self.put(agg, 'https://news.example.org/fetched-not-shipped', body=FULL,
                       title='正文刚补齐、还没导出的一条', content_kind='content',
                       fulltext_at=reg.now(), fulltext_tries=1)
        self.assertEqual('', self.row(key)['exported_hash'])
        self.assertNotIn(key, [r['key'] for r in lanes.rows('body_recent', 'rss')['items']])
        self.assertEqual({'ingest_queued': [key]}, lanes.split([key], 'rss'))
        # **反过来钉一边**:导出回执一落,它才进「已完成」。
        with reg.db() as c:
            c.execute('UPDATE feed_items SET exported_hash=content_hash WHERE key=?', (key,))
        self.assertEqual({'body_recent': [key]}, lanes.split([key], 'rss'))

    def test_keys_from_another_source_are_not_counted_in(self):
        """路由上那个 kind 过了合法性校验,不等于请求体里每个 key 都属于它。"""
        every = [row['key'] for row in lanes.rows('body_queued', 'rss')['items']]
        self.assertTrue(every)
        self.assertEqual({'': every}, lanes.split(every, 'web'))

    def test_an_empty_library_reports_empty_and_does_not_raise(self):
        """合法的空集照旧是空集(元规则:反过来钉一边)。"""
        with reg.db() as c:
            # 先解绑再删条目:`subscription_items` 有外键指着 `feed_items`,反过来
            # 删直接 `FOREIGN KEY constraint failed`。
            c.execute('DELETE FROM subscription_items')
            c.execute('DELETE FROM feed_items')
            c.execute('DELETE FROM feed_item_shelf')
        self.assertEqual(0, sum(n for per in lanes.counts().values() for n in per.values()))
        self.assertEqual({'total': 0, 'items': []}, lanes.rows('body_queued', 'rss'))
        self.assertEqual({}, lanes.split([], 'rss'))


class ResolvedWindowTests(DiagCase):
    """「已完成」那一组的回看窗口 —— 下界的**形状**是这里真正被测的东西。"""

    def test_the_cutoff_has_the_same_shape_as_the_stored_timestamps(self):
        """`fulltext_at` 存的是 `reg.now()`(UTC ISO 带偏移)。拿
        `datetime('now','-7 day')` 那种没有 `T`、没有偏移的形状去比,会在日界上
        判反(`'T' > ' '`)—— 而这一列上没有可用的日期函数索引可言。
        """
        cutoff, stamp = lanes.resolved_cutoff(), reg.now()
        self.assertTrue(datetime.fromisoformat(cutoff).tzinfo)
        self.assertTrue(datetime.fromisoformat(stamp).tzinfo)
        self.assertIn('T', cutoff)
        self.assertEqual(len(stamp), len(cutoff))
        self.assertLess(cutoff, stamp)          # 字符串比较也要排对

    def test_only_what_was_finished_inside_the_window_is_listed(self):
        ids = self.seed()
        agg = reg.subscription('rss', ids['agg'])
        old = (datetime.now(timezone.utc) - timedelta(days=lanes.RESOLVED_DAYS + 3))
        stale = self.put(agg, 'https://news.example.org/long-done', body=FULL,
                         title='七天以前就补好的一条',
                         fulltext_at=old.isoformat(timespec='seconds'),
                         fulltext_tries=1, content_kind='content')
        with reg.db() as c:
            c.execute('UPDATE feed_items SET exported_hash=content_hash WHERE key=?',
                      (stale,))
        listed = [row['key'] for row in lanes.rows('body_recent', 'rss')['items']]
        self.assertNotIn(stale, listed)
        self.assertEqual([key_of('rss', 'https://news.example.org/recent')], listed)


class RealSuccessClosesTheLaneTests(DiagCase):
    """**真处理成功之后,人在「已完成」里找得到它** —— 从重试一路跑到分档。

    上一版这两档要求 `fulltext_tries>0` / `translate_tries>0`,也就是拿「失败过」
    当成功历史的前提。可两条路都会把那个计数清零:`release`/`reopen`/`relink` 放回
    队列时清零,翻译成功写入时**必定**清零,人工粘贴压根不制造失败次数 —— 于是
    「重试 → 成功 → 入库」这条最常走的路上,那一条谁也找不到(审查实测:两篇的
    文件在、`exported_hash` 对上、成功时间非空,而 `lanes.split` 返回的桶名是空串)。

    这一组**不种 `*_tries`**,走真实的 `requeue` → 补抓/翻译 → `_export`,只把外部
    页面和模型响应换成固定成功响应;判据落在「桶名是什么」上。反向那一半(没导出
    就不许算完成)在 `LaneBoundaryTests`,另一半(七天以前的不列)在
    `ResolvedWindowTests`。
    """

    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def test_a_body_refetched_after_a_requeue_lands_in_the_finished_lane(self):
        from unittest.mock import patch

        from kb.intake_diag import actions

        agg = reg.subscription('rss', self.ids['agg'])
        key = self.put(agg, 'https://news.example.org/fresh-retry', body=SHELL,
                       title='放回队列之后真补到了正文的一条')
        # **一次失败都没发生过**:这正是上一版判据漏掉的那一档。
        self.assertEqual(0, self.row(key)['fulltext_tries'])
        actions.requeue([key], 'rss')
        with patch.object(fulltext, 'fetch_one',
                          return_value=('补抓回来的完整中文正文。' * 50, '')):
            # **`keys` 只插队、不是筛子**(`fulltext.fetch_pending` 的说明):不配
            # `limit` 的话样例里另外那几条待补抓的也会在这一轮被取,`done` 就不是
            # 这一条的账了。插队 + 上限 1 条 = 这一轮只有它。
            fetched = fulltext.fetch_pending('rss', limit=1, keys=[key])
        self.assertEqual(1, fetched['done'])
        from kb.feeds import sync

        sync._export('rss')
        current = self.row(key)
        self.assertNotEqual('', current['fulltext_at'])
        self.assertEqual(0, current['fulltext_tries'])
        self.assertEqual(current['content_hash'], current['exported_hash'])
        self.assertEqual({'body_recent': [key]}, lanes.split([key], 'rss'))

    def test_a_translation_that_succeeded_lands_in_the_finished_lane(self):
        from unittest.mock import patch

        from kb.feeds import sync, translate

        plain = reg.subscription('rss', self.ids['plain'])
        key = self.put(plain, 'https://solo.example.org/fresh-english',
                       body='An English article with substantive content. ' * 30,
                       title='English title', lang='en', content_kind='content')
        self.assertEqual(0, self.row(key)['translate_tries'])
        with patch.object(translate, '_model', return_value=object()), \
             patch.object(translate, '_call',
                          return_value='###TITLE###\n中文标题\n###BODY###\n真实中文译文。'):
            summary = translate.translate_pending('rss', source_id=plain['id'])
        self.assertGreaterEqual(summary['done'], 1)
        sync._export('rss')
        current = self.row(key)
        self.assertNotEqual('', current['translated_at'])
        # **成功写入必定把失败计数清零** —— 拿它当完成的前提就是这里错的那一步。
        self.assertEqual(0, current['translate_tries'])
        self.assertEqual(current['content_hash'], current['exported_hash'])
        self.assertEqual({'translate_recent': [key]}, lanes.split([key], 'rss'))

    def test_a_manually_pasted_body_lands_in_the_finished_lane(self):
        """人工粘贴同样不制造失败次数(第三条到达同一档的路)。"""
        from kb import browser_capture
        from kb.feeds import sync

        agg = reg.subscription('rss', self.ids['agg'])
        key = self.put(agg, 'https://news.example.org/pasted', body=SHELL,
                       title='人工粘进来正文的一条')
        browser_capture.save_pasted(key, '这是人工粘进来的一整段正文。' * 40)
        sync._export('rss')
        current = self.row(key)
        self.assertEqual(0, current['fulltext_tries'])
        self.assertEqual({'body_recent': [key]}, lanes.split([key], 'rss'))

    def test_an_item_that_needed_nothing_but_ingesting_lands_in_the_finished_lane(self):
        """**第三条到达成功的路**:不需要补抓、也不需要翻译,只等写进知识库。

        上面两档认的是「补抓成功过」「翻译成功过」,而这一类内容那两列**永远**是空
        串 —— 人在面板上对它点过「立即执行」、它也真写进了知识库,而他在「已完成」
        里找不到结果。判据落在交付本身(`exported_at`,由交付确认那一句和回执同一笔
        写下)。
        """
        from kb.feeds import sync

        plain = reg.subscription('rss', self.ids['plain'])
        key = self.put(plain, 'https://solo.example.org/plain-chinese', body=FULL,
                       title='一条自带中文全文的内容', content_kind='content')
        self.assertEqual({'ingest_queued': [key]}, lanes.split([key], 'rss'))
        self.assertEqual('', self.row(key)['exported_at'])
        sync._export('rss')
        current = self.row(key)
        # 上游两环**一次都没发生过** —— 这正是上面两档认不出它的原因。
        self.assertEqual('', current['fulltext_at'])
        self.assertEqual('', current['translated_at'])
        self.assertEqual(current['content_hash'], current['exported_hash'])
        self.assertNotEqual('', current['exported_at'])
        self.assertEqual({'ingest_recent': [key]}, lanes.split([key], 'rss'))

    def test_content_updated_after_delivery_goes_back_to_the_queue(self):
        """**反过来钉一边**:`exported_at` 记的是最近一次交付的时刻,而内容在那之后
        又更新过时这一条该回到待办 —— 只看那个时刻的实现在这里是绿的。
        """
        from kb.feeds import sync

        plain = reg.subscription('rss', self.ids['plain'])
        key = self.put(plain, 'https://solo.example.org/plain-updated', body=FULL,
                       title='交付之后又更新过的一条', content_kind='content')
        sync._export('rss')
        self.assertEqual({'ingest_recent': [key]}, lanes.split([key], 'rss'))
        with reg.db() as c:
            c.execute("UPDATE feed_items SET content_hash='changed-after-delivery'"
                      ' WHERE key=?', (key,))
        self.assertNotEqual('', self.row(key)['exported_at'])
        self.assertEqual({'ingest_queued': [key]}, lanes.split([key], 'rss'))
