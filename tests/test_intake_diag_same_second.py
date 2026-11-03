# -*- coding: utf-8 -*-
"""「问题与待办」一个账号只列最后发生的那一条 —— **同一秒里先后写下的两件事也要分得出先后**。

`error_at` / `note_at` 一度跟 `reg.now()` 只记到秒:同一频道先连接失败、紧接着又写下
「频道没列完」,两笔独立提交落在同一秒里比出来相等,`sources.latest` 就按拼装顺序留下了
早的那条连接失败。这里走真实的写端(`source_failure.write`、`collector.commit_notes`)和
真实的接口,不手捏时间:先对齐到某一秒的开头,三笔写入都落在这一秒里(断言钉着),
留下的必须是每一步之后最新的那一条 —— 失败之后写下提示,留提示;再失败一次,留失败。
"""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.feeds import registry as reg, source_failure
from kb.web import intake_diag_api
from kb.youtube import collector
from tests.intake_diag_case import DiagCase


class SameSecondTests(DiagCase):
    def setUp(self):
        super().setUp()
        self.seed()
        app = FastAPI()
        app.include_router(intake_diag_api.router)
        self.client = TestClient(app)
        stamp = reg.now()
        with reg.db() as c:
            self.source_id = c.execute(
                'INSERT INTO subscriptions(kind,name,url,created_at,updated_at) VALUES(?,?,?,?,?)',
                ('youtube', '先失败后没列完', 'https://www.youtube.com/@SameSecond', stamp, stamp)).lastrowid

    def source(self) -> dict:
        return dict(reg.subscription('youtube', self.source_id))

    def write_failure(self):
        with reg.db() as c:
            source_failure.write(c, self.source(), RuntimeError('连接超时'), now=time.time())

    def shown(self) -> tuple[list, list]:
        over = self.client.get('/api/intake-care', params={'kind': 'youtube'}).json()
        mine = lambda rows: [p['problem'] for p in rows if p['source_id'] == self.source_id]
        return mine(over['sources']), mine(over['source_rows'])

    def test_two_writes_in_one_second_keep_their_order(self):
        # 对齐到下一秒的开头,三笔写入只要几毫秒,都落在这一秒里。
        time.sleep(1 - time.time() % 1)
        self.write_failure()
        summary = {'added': 0, 'partial': [{'source_id': self.source_id, 'reason': '频道仍未列完'}]}
        self.assertTrue(collector.commit_notes(self.source(), [], summary, record_success=False))
        noted = self.source()
        self.assertEqual(noted['error_at'][:19], noted['note_at'][:19])
        facts, shown = self.shown()
        self.assertEqual(2, len(facts))
        self.assertEqual('connect', facts[0])
        self.assertEqual([facts[1]], shown)
        # 反过来钉一边:同一秒里又失败一次,留的换回失败。
        self.write_failure()
        again = self.source()
        self.assertEqual(noted['note_at'][:19], again['error_at'][:19])
        self.assertEqual(facts, self.shown()[0])
        self.assertEqual(['connect'], self.shown()[1])
