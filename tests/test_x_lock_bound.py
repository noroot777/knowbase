"""大V观点那一路等维护锁:停止和预算要管得住那一段等待(`kb/feeds/x_intake_store._lock`)。

各来源同时跑之后,X 那一路会撞上另一路正在写库。等锁本身没错,错在**等的那一段谁都叫不停**:
按了停止、或者整轮预算早就过了,它照样一个账号一个账号地各等满 `LOCK_WAIT`。判据落在
「多久之内放弃、抛的是不是 `LockBusy`」上 —— 那是 `Intake` 按本机故障记「待续」的那条路。

锁是真的(`kb.maintenance.maintenance_lock`),由另一根线程持着:同一线程再取会被重入保护
直接放行,那样什么都没验。
"""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from kb import maintenance
from kb.feeds import x_intake_store as store
from kb.maintenance import LockBusy, maintenance_lock

QUICK = 2.0          # 「当场放弃」的宽限:几轮 `_POLL`,远小于 `LOCK_WAIT`


class BoundedLockTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = patch.object(maintenance, 'DATA_DIR', Path(temp.name))
        data.start()
        self.addCleanup(data.stop)

    def hold(self):
        """另一根线程持着维护锁,直到用例结束。"""
        taken, release = threading.Event(), threading.Event()

        def holder():
            with maintenance_lock(wait=0):
                taken.set()
                release.wait(30)
        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(taken.wait(5), '持锁那一根线程没拿到锁')
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)

    def gives_up(self, take):
        started = time.monotonic()
        with self.assertRaises(LockBusy):
            with take():
                self.fail('锁被别人持着,这里却进来了')
        spent = time.monotonic() - started
        self.assertLess(spent, QUICK, f'等了 {spent:.1f} 秒才放弃')

    def test_a_stop_ends_the_wait_at_once(self):
        self.hold()
        with store.bounded(lambda: True, None):
            self.gives_up(store._lock)

    def test_a_spent_budget_ends_the_wait_at_once(self):
        self.hold()
        with store.bounded(None, time.monotonic() - 1):
            self.gives_up(store._lock)

    def test_prepare_waits_only_until_the_network_deadline(self):
        """整轮的界限还宽,抓取那一段的截止已经过了:开工那一笔不再等。"""
        self.hold()
        with store.bounded(None, time.monotonic() + 3600):
            self.gives_up(lambda: store._lock(time.monotonic() - 1))

    def test_the_final_save_gives_up_once_its_grace_is_spent(self):
        self.hold()
        spent = {'x_deadline': time.monotonic() - store.LOCK_WAIT - 1}
        self.gives_up(lambda: store.save_lock(spent, None))
        self.gives_up(lambda: store.save_lock({}, lambda: True))

    def test_the_bound_is_scoped_to_its_block_and_thread(self):
        """出了那一段、或者换一根线程,就不再带着那一份停止/截止。"""
        with store.bounded(lambda: True, 0.0):
            seen = []
            other = threading.Thread(target=lambda: seen.append(getattr(store._bound, 'at', None)))
            other.start()
            other.join()
            self.assertEqual([None], seen)
        self.assertIsNone(getattr(store._bound, 'at', None))

    def test_a_free_lock_is_taken_without_waiting(self):
        """反过来钉一边:没人持锁、没按停止、预算还够,照常拿到。"""
        with store.bounded(lambda: False, time.monotonic() + 3600):
            with store._lock():
                pass
        with store.save_lock({'x_deadline': time.monotonic() + 60}, lambda: False):
            pass

    def test_a_held_lock_is_still_waited_for_while_the_budget_lasts(self):
        """预算还在、没按停止:让开之后照样拿到,不因为「有界」就一碰到占着就放弃。"""
        taken, release = threading.Event(), threading.Event()

        def holder():
            with maintenance_lock(wait=0):
                taken.set()
                release.wait(5)
        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(taken.wait(5))
        threading.Timer(0.6, release.set).start()
        with store.bounded(lambda: False, time.monotonic() + 3600):
            with store._lock():
                pass
        thread.join(5)


if __name__ == '__main__':
    unittest.main()
