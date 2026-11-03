# -*- coding: utf-8 -*-
"""X 五天队列遇到锁要等,跟别的来源一个口径(`x_intake_store.db`、`sync._save_result`)。

实测的那一次:切完通道立刻点同步,页面同一刻发来的请求开库时在写锁里核了一遍表结构,X 那几处
写库一秒都不等,排在最前面的 7 个号全报 `database is locked`、这一轮白跑,第 8 个起全部正常。
RSS、YouTube 那几条路同样碰到这一下,只是等了几毫秒照常写进去。

判据落在后果上:**别人短暂攥着锁的那一轮,每个号照常读完、入库、宣布覆盖**,不记本机失败。
另一边(锁一直不放、等满了)照旧记成本机失败、待办保留 —— 那一侧
`test_x_intake_isolation`、`test_x_intake_phase_delivery` 钉着。

**等也有边界**(`BoundedSqliteWaitTests`):SQLite 那一侧的等待跟维护锁(`test_x_lock_bound`)一样
受这一轮的停止和截止约束,锁被长期占着时不许逐账号各等满 30 秒。

人点了按钮在等的那一下(`x_article_capture.save`)只等一小会儿,等不到如实说一句:
那一侧在 `test_x_article_capture` 的 `LockWaitTests`。

跑:  python -m unittest tests.test_x_intake_lock_wait -v
"""
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import maintenance
from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_channels, x_intake_commit
from kb.feeds import x_intake_store as store
from kb.feeds.x_intake import Intake
from tests.test_x_guest import GuestBase, status

HOLD = 0.6
QUICK = 2.0          # 「当场放弃」的宽限,远小于 `store.SQLITE_WAIT`


def hold_sqlite_writer(seconds):
    """另一个连接拿着 feeds.db 的写锁 `seconds` 秒。返回时锁已经拿到了。"""
    taken = threading.Event()

    def run():
        conn = sqlite3.connect(reg.DB_PATH, timeout=0)
        try:
            conn.execute('BEGIN IMMEDIATE')
            taken.set()
            time.sleep(seconds)
            conn.commit()
        finally:
            conn.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert taken.wait(5), '写锁没拿到'
    return worker


def hold_sqlite_until(case):
    """另一个连接一直攥着 feeds.db 的写锁,直到用例结束。"""
    taken, release = threading.Event(), threading.Event()

    def run():
        conn = sqlite3.connect(reg.DB_PATH, timeout=0)
        try:
            conn.execute('BEGIN IMMEDIATE')
            taken.set()
            release.wait(60)
            conn.commit()
        finally:
            conn.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert taken.wait(5), '写锁没拿到'
    case.addCleanup(worker.join, 5)
    case.addCleanup(release.set)


def hold_maintenance_lock(seconds):
    """另一个线程攥着维护锁 `seconds` 秒(同进程不同线程,`_held` 是线程本地的,照样互斥)。"""
    taken = threading.Event()

    def run():
        with maintenance.maintenance_lock():
            taken.set()
            time.sleep(seconds)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert taken.wait(5), '维护锁没拿到'
    return worker


def contend(target, name, holder):
    """`target.name` 第一次被调用时,先让别人攥住锁(`holder`)再放它往下走 —— 正是实测那一下:
    X 要写库的那一刻,恰好另一个连接或另一趟维护拿着锁。返回 patcher 和那个攥锁的线程(的容器)。"""
    real, workers = getattr(target, name), []

    def wrapped(*args, **kwargs):
        if not workers:
            workers.append(holder(HOLD))
        return real(*args, **kwargs)
    return patch.object(target, name, wrapped), workers


class LockWaitTests(GuestBase):
    """X 那条路上每一处写库各挡一次:五天队列的准备、记开始读、暂存,和最后那一步入库。"""

    def setUp(self):
        super().setUp()
        self.alice, self.bob = self.sub('alice'), self.sub('bob')
        self.settings(x_channels.GUEST)
        self.api(alice=[[status('alice', 9, 1, '甲'), *[status('alice', n, 130 + n) for n in (3, 2, 1)]]],
                 bob=[[status('bob', 19, 1, '乙'), *[status('bob', n, 130 + n) for n in (13, 12, 11)]]])

    def round_with(self, target, name, holder):
        patcher, workers = contend(target, name, holder)
        with patcher:
            summary = sync.collect('social', FetchScope())
        self.assertTrue(workers, f'{name} 没被调到,这条没挡在它前面')
        workers[0].join(5)
        self.assertEqual('', summary.get('local_error', ''))
        with reg.db() as c:
            stored = sorted(r['url'] for r in c.execute('SELECT url FROM feed_items'))
            rows = {r['source_id']: r['discovery'] for r in c.execute('SELECT * FROM x_intake_sources')}
        self.assertEqual(['https://x.com/alice/status/9', 'https://x.com/bob/status/19'], stored)
        self.assertEqual({self.alice['id']: 'complete', self.bob['id']: 'complete'}, rows)

    def test_a_writer_holding_feeds_db_as_the_account_starts_is_waited_for(self):
        self.round_with(store, 'attempt', hold_sqlite_writer)

    def test_a_writer_holding_feeds_db_as_the_timeline_is_staged_is_waited_for(self):
        self.round_with(store, 'stage', hold_sqlite_writer)

    def test_a_writer_holding_feeds_db_as_posts_are_stored_is_waited_for(self):
        self.round_with(x_intake_commit, 'check_budget', hold_sqlite_writer)

    def test_a_maintenance_task_as_the_queue_is_prepared_is_waited_for(self):
        self.round_with(store, 'prepare', hold_maintenance_lock)

    def test_a_maintenance_task_as_the_timeline_is_staged_is_waited_for(self):
        self.round_with(store, 'stage', hold_maintenance_lock)

    def test_a_maintenance_task_as_posts_are_stored_is_waited_for(self):
        self.round_with(x_intake_commit, 'check_budget', hold_maintenance_lock)


class BoundedSqliteWaitTests(GuestBase):
    """feeds.db 的写锁被长期攥着:按了停止、过了截止,每一笔都当场放弃,不逐账号各等满 `SQLITE_WAIT`。"""

    def setUp(self):
        super().setUp()
        self.sources = [self.sub(name) for name in ('alice', 'bob', 'carol')]
        self.settings(x_channels.GUEST)
        self.channel = reg.get_settings('social')      # 攥锁之前读好:开库本身就要过一下写锁
        hold_sqlite_until(self)

    def gives_up(self, take):
        started = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            with take() as conn:
                conn.execute('BEGIN IMMEDIATE')
        spent = time.monotonic() - started
        self.assertLess(spent, QUICK, f'等了 {spent:.1f} 秒才放弃')

    def test_a_stopped_round_gives_up_every_account_at_once(self):
        """后果:整轮很快收工,三个号都记成本机失败(待续),而不是 3 × 30 秒之后才看见停止。"""
        started = time.monotonic()
        intake = Intake(self.sources, self.channel, budget=600, should_stop=lambda: True)
        intake.run()
        spent = time.monotonic() - started
        self.assertLess(spent, QUICK * 2, f'按了停止还等了 {spent:.1f} 秒')
        self.assertEqual({s['id'] for s in self.sources},
                         {e['source_id'] for e in intake.metrics.data['local_errors']})

    def test_a_stop_or_a_spent_deadline_ends_the_wait(self):
        with store.bounded(lambda: True, None):
            self.gives_up(store.db)
        with store.bounded(None, time.monotonic() - 1):
            self.gives_up(store.db)
        with store.bounded(None, time.monotonic() + 3600):
            self.gives_up(lambda: store.db(time.monotonic() - 1))     # 准备那一笔只等到联网截止

    def test_the_final_save_gives_up_once_its_grace_is_spent(self):
        self.gives_up(lambda: store.save_db({'x_deadline': time.monotonic() - store.LOCK_WAIT - 1}, None))
        self.gives_up(lambda: store.save_db({}, lambda: True))

    def test_the_final_save_in_sync_listens_to_stop(self):
        """`sync._save_result` 那一笔真走的是 `save_db`:按了停止,写锁被占着就当场放弃。"""
        validators = {'x_intake': True, 'x_deadline': time.monotonic() + 600}
        started = time.monotonic()
        with patch.object(sync, 'halted', lambda: True), self.assertRaises(sqlite3.OperationalError):
            sync._save_result(self.sources[0], ([], validators), FetchScope())
        self.assertLess(time.monotonic() - started, QUICK)

    def test_the_wait_is_still_capped_by_what_is_left(self):
        """截止还没到但剩得不多:只等剩下那一点,不是整整 30 秒。"""
        with store.bounded(lambda: False, time.monotonic() + 0.3):
            self.gives_up(store.db)
        self.gives_up(lambda: store.db(time.monotonic() + 0.3))


if __name__ == '__main__':
    unittest.main()
