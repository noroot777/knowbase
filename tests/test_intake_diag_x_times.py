# -*- coding: utf-8 -*-
"""「问题与待办」一个账号只列最后发生的那一条 —— **X 那几条的时间得是它们自己的**。

走真实的记账函数(`x_intake_store.prepare` / `stage` / `confirm` / `set_block` /
`attempt` / `statuses`),不手捏 `issues` 的输入:上一版就是在投影那一层把正文待补、
候选待确认、历史缺口统一借了 `scan_at`,而 `attempt()`(扫描开始、失败记账)会刷新
它 —— 撞上启动限流之后照旧写一次,于是没动过的旧正文待补反而比限流新。

序列:旧待办(正文待补 / 候选待确认 / 历史缺口)→ 启动限流 → `attempt()` 收尾 →
再起一轮 `prepare()`。限流必须是留下的那一条;反过来钉一边:那几条正文之后真的又
补过一次,留下的就换成正文待补。
"""
from __future__ import annotations

import time

from kb.feeds import registry as reg, x_intake_blocks as blocks, x_page_state as xstate
from kb.feeds import x_intake_store as store
from kb.intake_diag import sources
from tests.test_x_intake import IntakeBase


class XFactTimeTests(IntakeBase):
    def setUp(self):
        super().setUp()
        self.prepare()
        # 一条进了正文待补并已确认,一条还在等收尾确认。
        self.stage([self.entry('100', truncated=True)], missing='还缺一段')
        self.stage([self.entry('200', age=2)], confirm=False)
        # 把这几件事统一挪到一小时前:它们都是「早就存在的旧待办」。
        with reg.db() as conn:
            conn.execute('UPDATE x_intake_candidates SET staged_at=staged_at-3600,attempted=0')
            conn.execute('UPDATE x_intake_links SET linked_at=linked_at-3600')
            conn.execute('UPDATE x_intake_sources SET gap_at=gap_at-3600')

    def x_state(self):
        source = dict(reg.subscription('social', self.source['id']))
        store.statuses([source], 'browser')
        return source['x_intake']

    def shown(self, state):
        rows = [{'kind': 'social', 'source_id': self.source['id'], 'problem': one['kind'],
                 'label': one['kind'], 'evidence': one['evidence'], 'at': one.get('at')}
                for one in state['issues']]
        return [row['problem'] for row in sources.latest(rows)]

    def test_a_fresh_throttle_is_not_covered_by_old_queues(self):
        before = self.x_state()
        kinds = {one['kind']: one['at'] for one in before['issues']}
        self.assertIn('x_intake_body', kinds)
        self.assertIn('x_intake_unconfirmed', kinds)
        self.assertIn('x_intake_history', kinds)
        old = max(kinds.values())
        throttle = blocks.describe(xstate.session_throttled(), 'discovery')
        store.set_block(self.source, throttle)
        store.attempt(self.source, 'discovery', str(xstate.session_throttled()))
        self.prepare()
        after = self.x_state()
        self.assertGreater(after['scan_at'], throttle['observed_at'])
        times = {one['kind']: one['at'] for one in after['issues']}
        # 扫描开始、失败记账、再起一轮都不是那几条旧待办的新事实。
        for kind in ('x_intake_body', 'x_intake_unconfirmed', 'x_intake_history'):
            self.assertEqual(kinds[kind], times[kind], kind)
        self.assertLess(old, throttle['observed_at'])
        self.assertEqual(['x_intake_throttled'], self.shown(after))

    def test_a_later_body_attempt_takes_the_row_back(self):
        throttle = blocks.describe(xstate.session_throttled(), 'discovery')
        store.set_block(self.source, throttle)
        store.attempt(self.source, 'discovery')
        with reg.db() as conn:
            conn.execute("UPDATE x_intake_candidates SET attempted=? WHERE post_id='100'",
                         (time.time() + 60,))
        self.assertEqual(['x_intake_body'], self.shown(self.x_state()))

    def test_a_retweet_new_to_this_account_is_not_dated_by_another_account(self):
        """A 一小时前收过、正文还待补的那一条,B 刚从转推里第一次收到:B 的正文待补认
        B 收到它的那一刻,压得过 B 早先的限流;A 那边的时间一个字都不动。"""
        other = self.sub('Other')
        store.prepare([other], 'browser', self.now)
        throttle = blocks.describe(xstate.session_throttled(), 'history')
        store.set_block(other, throttle)
        mine = next(one['at'] for one in self.x_state()['issues'] if one['kind'] == 'x_intake_body')
        self.stage([self.entry('100', truncated=True)], source=other, missing='还缺一段')
        theirs = dict(reg.subscription('social', other['id']))
        store.statuses([theirs], 'browser')
        body = next(one['at'] for one in theirs['x_intake']['issues'] if one['kind'] == 'x_intake_body')
        self.assertGreater(body, throttle['observed_at'])
        rows = [{'kind': 'social', 'source_id': other['id'], 'problem': one['kind'], 'label': one['kind'],
                 'evidence': one['evidence'], 'at': one.get('at')} for one in theirs['x_intake']['issues']]
        self.assertEqual(['x_intake_body'], [row['problem'] for row in sources.latest(rows)])
        self.assertEqual(mine, next(one['at'] for one in self.x_state()['issues']
                                    if one['kind'] == 'x_intake_body'))
