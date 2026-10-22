# -*- coding: utf-8 -*-
"""「看不出是不是正文」那几条的**人工出口** —— 网页端点和命令行各钉一遍。

被测的那件事:**落了盘的状态不算出口。** 挂起这一档(`grab_page.Admission.unsure`
→ `fulltext.Unconfirmed` → `feed_items.fulltext_hold`)上一版只有两个 Python 函数
`held()` / `release()`,全仓 `rg` 找不到任何生产调用者 —— 状态写进了库,而人既看不到
也恢复不了,面板上只剩一句「订阅摘要」(2026-10-22 审查第 1 轮的第 2 条)。

**所以这一组一条都不直接调那两个函数**(`tests/test_admit_three_ways.py` 那边钉的是
它们自己的行为),这里走的全是人真点得到的那两条路:HTTP 端点和 `./kb.sh held`。

硬约束在 `docs/decisions/fulltext.md` 第一节。
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.feeds import fulltext
from kb.web import intake_api
from tests.test_fulltext_admit import DENIED_SPLIT, resp
from tests.test_feed_fulltext import FullTextCase, PAGE

ROOT = Path(__file__).resolve().parent.parent


class HeldExitCase(FullTextCase):
    """脚手架:摆一条壳条目、抓一轮把它挂起,再把那两条出口接上来。"""

    def setUp(self):
        super().setUp()
        app = FastAPI(); app.include_router(intake_api.router)
        self.client = TestClient(app)

    def hold_one(self):
        key = self.put()
        with patch.object(fulltext, 'request', Mock(return_value=resp(DENIED_SPLIT))):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual(1, summary['held'])          # 前提:它真挂起了
        return key


class HeldEndpointTests(HeldExitCase):
    """网页那两个端点。"""

    def test_the_list_says_which_item_and_why_it_was_not_taken(self):
        """**查看入口要答得出「哪一条、上一次为什么没收下」。** 只给一个计数的话
        人还是不知道该去看什么 —— 那半句话正是他要读的东西。
        """
        key = self.hold_one()
        got = self.client.get('/api/intake/rss/held').json()
        self.assertEqual(1, got['total'])
        row = got['items'][0]
        self.assertEqual(key, row['key'])
        self.assertEqual('https://e.org/post', row['url'])
        self.assertIn('访问被拒绝', row['fulltext_hold'])

    def test_releasing_through_the_endpoint_really_puts_it_back_in_the_queue(self):
        """**恢复那一下要真的改变下一轮会不会抓它。** 断言落在「下一轮抓到了正文」
        上,不是落在「接口返回 200」上。
        """
        key = self.hold_one()
        self.assertEqual(0, fulltext.pending('rss'))        # 挂起时不在队列里
        got = self.client.post('/api/intake/rss/held/release',
                               json={'keys': [key]}).json()
        self.assertEqual({'released': 1, 'asked': 1, 'left': 0, 'pending': 1}, got)
        with patch.object(fulltext, 'request', Mock(return_value=resp(PAGE))) as again:
            self.assertEqual(1, fulltext.fetch_pending('rss')['done'])
        self.assertEqual(1, again.call_count)
        self.assertIn('The runtime schedules tools', self.row(key)['body'])
        self.assertEqual([], self.client.get('/api/intake/rss/held').json()['items'])

    def test_an_empty_key_list_means_everything_in_this_kind(self):
        """面板那颗「全部恢复」:空 `keys` = 这个来源里挂起的全放回去。**射程按
        来源收窄**,不是整库 —— 它承诺的就是人眼前这一页。
        """
        self.hold_one()
        got = self.client.post('/api/intake/rss/held/release', json={'keys': []}).json()
        self.assertEqual((1, 1), (got['released'], got['asked']))
        self.assertEqual(0, got['left'])

    def test_it_reports_what_it_actually_wrote_not_what_was_asked(self):
        """**报实际写入结果,不报计划长度**(元规则)。给三个 key 只有一条放得回去
        时就说 1 —— 另外两个一个不存在、一个已经补到全文。
        """
        key = self.hold_one()
        done = self.put(url='https://e.org/other')
        with patch.object(fulltext, 'request', Mock(return_value=resp(PAGE))):
            fulltext.fetch_pending('rss')
        self.assertNotEqual('', self.row(done)['fulltext_at'])
        got = self.client.post('/api/intake/rss/held/release',
                               json={'keys': [key, done, 'no-such-key']}).json()
        self.assertEqual((1, 3), (got['released'], got['asked']))
        # 反过来钉一边:那条已经补到全文的,正文一个字节都没被碰
        self.assertIn('The runtime schedules tools', self.row(done)['body'])

    def test_an_unknown_kind_is_a_404_not_an_empty_list(self):
        """**空集合和「没有这个来源」是两件事。** 拼错来源名时返回空列表,人会以为
        「没有待办」—— 那正是元规则说的「静默得到空集」。
        """
        self.assertEqual(404, self.client.get('/api/intake/nope/held').status_code)
        self.assertEqual(404, self.client.post('/api/intake/nope/held/release',
                                               json={'keys': []}).status_code)


class HeldCommandTests(HeldExitCase):
    """`./kb.sh held` 那一条(脚本自己,不起服务)。"""

    def run_cli(self, *args):
        """按子进程真跑一次 —— 它读的是 `KB_DATA_DIR` 下那份库。

        **不 import 进来调 `main()`**:那样测不到「数据目录解析对不对」,而这个脚本
        唯一的副作用就是写那一份库(口径同 `tests/test_grab_cli.py`)。
        """
        return subprocess.run([sys.executable, str(ROOT / 'scripts/feed_held_cli.py'),
                               *args], capture_output=True, text=True, cwd=ROOT,
                              env={'KB_DATA_DIR': str(self.root), 'PATH': '/usr/bin:/bin',
                                   'PYTHONPATH': str(ROOT), 'TZ': 'Asia/Shanghai'})

    def test_it_lists_the_item_with_the_reason_it_was_not_taken(self):
        key = self.hold_one()
        got = self.run_cli()
        self.assertEqual(0, got.returncode, got.stderr)
        self.assertIn(key, got.stdout)
        self.assertIn('访问被拒绝', got.stdout)

    def test_releasing_by_key_really_puts_it_back_in_the_queue(self):
        """**和端点同一个判据**(`fulltext.release`),所以断言也落在同一个地方:
        下一轮真抓到了正文。两处各写一遍 SQL 的话,其中一头迟早漏掉一条。
        """
        key = self.hold_one()
        got = self.run_cli('--release', key)
        self.assertEqual(0, got.returncode, got.stderr)
        self.assertIn('已放回队列 1 条', got.stdout)
        self.assertEqual(1, fulltext.pending('rss'))
        with patch.object(fulltext, 'request', Mock(return_value=resp(PAGE))):
            self.assertEqual(1, fulltext.fetch_pending('rss')['done'])

    def test_nothing_held_says_so_instead_of_printing_nothing(self):
        """**空的时候要说一句话。** 什么都不打印和「脚本坏了」长得一模一样。"""
        got = self.run_cli()
        self.assertEqual(0, got.returncode, got.stderr)
        self.assertIn('没有挂起的条目', got.stdout)

    def test_a_bad_kind_exits_nonzero_instead_of_reporting_nothing_held(self):
        """拼错来源名要以非 0 退出 —— 报「没有待办」就是元规则说的静默空集。"""
        got = self.run_cli('nope')
        self.assertEqual(2, got.returncode)
        self.assertNotIn('没有挂起的条目', got.stdout)

    def test_release_all_leaves_nothing_behind(self):
        key = self.hold_one()
        got = self.run_cli('rss', '--release-all')
        self.assertEqual(0, got.returncode, got.stderr)
        self.assertIn('已放回队列 1 条', got.stdout)
        self.assertEqual('', self.row(key)['fulltext_hold'])


if __name__ == '__main__':
    unittest.main()
