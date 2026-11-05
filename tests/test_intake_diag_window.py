# -*- coding: utf-8 -*-
"""「问题与待办」只看近 7 天(`lanes.WINDOW_DAYS`)。

口径在 `docs/decisions/intake-diagnostics.md` 第一节那条「面板只看近 7 天」:
每一组按它自己那件事的时刻回看(待决定 / 待执行按收进来、已搁置按做决定),
窗口外的从计数和列表上一起消失。**窗口只收窄显示** —— 所以每一条都反过来钉
一边:分档、「从浏览器采集」的准入、搁置本身都还是原样。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kb.feeds import registry as reg
from kb.intake_diag import lanes, shelf
from kb.web import intake_diag_api as api
from tests.intake_diag_case import DiagCase, key_of


def days_ago(days: float) -> str:
    """和 `reg.now()` 同一种形状(UTC ISO 带偏移)。"""
    at = datetime.now(timezone.utc) - timedelta(days=days)
    return at.isoformat(timespec='seconds')


QUEUED = key_of('rss', 'https://news.example.org/queued')
HELD = key_of('rss', 'https://news.example.org/held')
SNOOZED = key_of('rss', 'https://news.example.org/snoozed')
EXCLUDED = key_of('rss', 'https://news.example.org/excluded')


class WindowCase(DiagCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def arrived(self, key: str, days: float) -> None:
        with reg.db() as c:
            c.execute('UPDATE feed_items SET fetched_at=? WHERE key=?', (days_ago(days), key))

    def decided(self, key: str, days: float) -> None:
        with reg.db() as c:
            c.execute('UPDATE feed_item_shelf SET created_at=? WHERE item_key=?',
                      (days_ago(days), key))

    def listed(self, lane_id: str) -> list[str]:
        return [row['key'] for row in lanes.rows(lane_id, 'rss')['items']]


class PendingWindowTests(WindowCase):
    def test_an_item_collected_before_the_window_leaves_the_count_and_the_list(self):
        self.assertIn(QUEUED, self.listed('body_queued'))
        self.arrived(QUEUED, lanes.WINDOW_DAYS + 1)
        self.assertNotIn(QUEUED, self.listed('body_queued'))
        self.assertEqual(0, lanes.rows('body_queued', 'rss')['total'])
        self.assertEqual(0, lanes.counts(['rss'])['rss']['body_queued'])
        # 页头那几个数跟着它走:页签上「待执行」少了这一条。
        before = api.overview('rss')['groups']
        self.assertEqual(before[lanes.QUEUED],
                         sum(lanes.rows(one.id, 'rss')['total'] for one in lanes.LANES
                             if one.group == lanes.QUEUED))

    def test_an_item_collected_inside_the_window_is_still_listed(self):
        """反过来钉一边:窗口边上往里一天的照旧在。"""
        self.arrived(HELD, lanes.WINDOW_DAYS - 1)
        self.assertEqual([HELD], self.listed('body_held'))
        self.assertEqual(1, lanes.counts(['rss'])['rss']['body_held'])

    def test_the_window_does_not_move_the_item_out_of_its_lane(self):
        """窗口不写进 `exclusive_where`:分档和「从浏览器采集」的准入照旧认它。

        写进去的话,一条八天前收进来的内容在同步面板失败明细里当场丢了那颗
        按钮,写请求也会按「已经不在积压里」分流。
        """
        self.arrived(QUEUED, lanes.WINDOW_DAYS + 1)
        self.assertEqual({'body_queued': [QUEUED]}, lanes.split([QUEUED], 'rss'))
        self.assertEqual({('rss', QUEUED)}, lanes.capturable([('rss', QUEUED)]))
        self.assertIsNotNone(lanes.one(QUEUED))
        self.assertEqual('body_queued', api.item(QUEUED)['lane']['id'])

    def test_counts_and_lists_agree_lane_by_lane(self):
        """计数和列表过同一份 `shown_where`:窗口外摆一条之后,每一档两边还相等。"""
        self.arrived(QUEUED, lanes.WINDOW_DAYS + 1)
        self.arrived(HELD, lanes.WINDOW_DAYS + 1)
        tally = lanes.counts(['rss'])['rss']
        self.assertEqual({one.id: lanes.rows(one.id, 'rss')['total'] for one in lanes.LANES},
                         tally)
        self.assertEqual(0, tally['body_held'])


class ShelvedWindowTests(WindowCase):
    def test_a_decision_made_before_the_window_is_no_longer_listed(self):
        found = lanes.shelved('rss')
        self.assertEqual({SNOOZED, EXCLUDED}, {row['key'] for row in found['items']})
        self.decided(EXCLUDED, lanes.WINDOW_DAYS + 1)
        found = lanes.shelved('rss')
        self.assertEqual([SNOOZED], [row['key'] for row in found['items']])
        self.assertEqual(1, found['total'])
        self.assertEqual(1, api.overview('rss')['groups'][lanes.ARCHIVED])

    def test_an_old_decision_still_holds(self):
        """窗口外的那一条**照旧搁置着**:搁置记录一行没删,也没被放回任何队列。"""
        self.decided(EXCLUDED, lanes.WINDOW_DAYS + 1)
        self.assertEqual(1, self.count(shelf.live_sql() + ' AND key=?', [EXCLUDED]))
        self.assertEqual({lanes.ARCHIVED: [EXCLUDED]}, lanes.split([EXCLUDED], 'rss'))
        self.assertTrue(api.item(EXCLUDED)['shelved'])

    def test_the_window_counts_from_the_decision_not_from_arrival(self):
        """收进来很久、刚刚才搁置的照旧列着 —— 已搁置那一组按做决定的时刻回看。"""
        self.arrived(SNOOZED, lanes.WINDOW_DAYS + 30)
        self.assertIn(SNOOZED, {row['key'] for row in lanes.shelved('rss')['items']})

    def test_shelving_again_brings_it_back_into_the_window(self):
        """原地改写刷新 `created_at`,所以重新做一次决定它就回到列表上。"""
        self.decided(EXCLUDED, lanes.WINDOW_DAYS + 1)
        self.shelve([EXCLUDED], 'rss', shelf.EXCLUDE)
        self.assertIn(EXCLUDED, {row['key'] for row in lanes.shelved('rss')['items']})

    def test_search_still_applies_inside_the_window(self):
        """占位符顺序:窗口那一个 `?` 在前、来源与搜索在后。"""
        self.decided(EXCLUDED, lanes.WINDOW_DAYS + 1)
        self.assertEqual(0, lanes.shelved('rss', '不再收录的一条')['total'])
        self.assertEqual(1, lanes.shelved('rss', '被暂缓')['total'])


class SourceWindowTests(WindowCase):
    """来源连接问题同一个窗口:按那条事实**自己**的时刻(这里是 `error_at`)。

    样例里那条连不上的 RSS 订阅只写了 `last_error`、`error_at` 是空串 —— 正是
    「记不出时间」那一种,所以每条用例先给它补上时刻。
    """

    def setUp(self):
        super().setUp()
        self.dead = next(s for s in reg.subscriptions('rss') if s['last_error'])

    def failed(self, days: float | None) -> None:
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET error_at=? WHERE id=?',
                      ('' if days is None else days_ago(days), self.dead['id']))

    def shown(self) -> dict:
        data = api.overview('rss')
        return {'rows': [row['source_id'] for row in data['source_rows']],
                'accounts': data['source_accounts'], 'problems': data['source_problems'],
                'facts': [row['source_id'] for row in data['sources']]}

    def test_a_failure_before_the_window_leaves_the_list_and_the_counts(self):
        self.failed(lanes.WINDOW_DAYS + 1)
        self.assertEqual({'rows': [], 'accounts': 0, 'problems': 0,
                          'facts': [self.dead['id']]}, self.shown())

    def test_a_failure_inside_the_window_is_still_listed(self):
        """反过来钉一边。"""
        self.failed(lanes.WINDOW_DAYS - 1)
        self.assertEqual({'rows': [self.dead['id']], 'accounts': 1, 'problems': 1,
                          'facts': [self.dead['id']]}, self.shown())

    def test_a_problem_without_its_own_time_is_still_listed(self):
        """说不出旧的不当旧的扔掉 —— 也不借订阅共用的 `last_attempt` 去判。"""
        self.failed(None)
        with reg.db() as c:
            c.execute('UPDATE subscriptions SET last_attempt=? WHERE id=?',
                      (days_ago(lanes.WINDOW_DAYS + 30), self.dead['id']))
        self.assertEqual([self.dead['id']], self.shown()['rows'])

    def test_the_action_layer_still_finds_a_problem_outside_the_window(self):
        """窗口只管屏幕:`/run` 现查的事实层照旧认得它。"""
        self.failed(lanes.WINDOW_DAYS + 1)
        problem_id = next(row['id'] for row in api.overview('rss')['sources'])
        self.assertEqual(self.dead['id'], api._problem_of('rss', problem_id)['source_id'])


class NoteTests(WindowCase):
    def test_the_header_says_the_page_only_looks_at_the_window(self):
        data = api.overview()
        self.assertEqual(lanes.WINDOW_DAYS, data['window_days'])
        self.assertIn(f'近 {lanes.WINDOW_DAYS} 天', data['note'])
        self.assertNotIn('全库', data['note'])
