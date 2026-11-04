# -*- coding: utf-8 -*-
"""「问题与待办」那几处纯读**不许排队抢写锁**(`registry.read_db`)。

`registry.db()` 每开一次都在 `BEGIN IMMEDIATE` 里把建表补列种默认值跑一遍。纯读
走它,等于每一问都先排队抢写锁:别的连接(管道写回、另一个进程同步订阅)握着一笔
写事务多久,概览和列表就卡多久 —— 页面上的形态是「处理中…」或者整屏停着不动,
而服务端日志里每一问都是 200。

① 写锁被别人握着时,概览、各档列表、已搁置、单条详情照旧当场读得出来,而且读到的
   是**已经提交的那一份**(和没人握锁时逐字相等)。**进程里第一次读也一样**:库已经
   是当前的样子时,首访那一下的就绪检查不许去抢写锁(`kb.feeds.read_ready`)。每一问
   都先清掉进程内的就绪登记再握锁去读 —— 预热过的那一问证明不了冷的那一问。
② 反过来钉一边:只读连接不建表也不补列。缺列的存量库第一问就走只读路径时,要先
   补列再读,不能报 `no such column`(只判「库文件在不在」不够,存量库一直都在)。
③ 真要补列、而写锁被别人握着时,只等调用方给的那点时限(X 长文那几问给的是整笔
   10 秒,不是迁移另算 30 秒);几问同时首访只有一问去迁。

没有打桩:临时 registry 真建表,写锁是另一条真连接真握着的。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from unittest.mock import patch

from kb.feeds import read_ready, registry as reg
from kb.intake_diag import lanes
from kb.web import intake_diag_api as api
from tests.intake_diag_case import DiagCase

#: 握锁那一侧不放手的话,走 `db()` 的读会在 `BEGIN IMMEDIATE` 里等满 30 秒。
#: 这里给 10 秒:比正常那一问(零点几秒)宽两个数量级,机器再忙也不会冤枉它。
PATIENCE = 10


def within(fn, seconds=PATIENCE):
    """在另一个线程里跑 `fn`,等它 `seconds` 秒。返回 `(还没跑完, 结果或异常)`。"""
    out: dict = {}

    def go():
        try:
            out['value'] = fn()
        except Exception as error:      # 原样交回给断言,别让线程把它吞掉
            out['error'] = error

    worker = threading.Thread(target=go, daemon=True)
    worker.start()
    worker.join(seconds)
    return worker.is_alive(), out


class ReadsDoNotQueueForTheWriterLockTests(DiagCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def hold_writer(self):
        writer = sqlite3.connect(reg.DB_PATH, timeout=0, isolation_level=None)
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("UPDATE feed_meta SET value=value WHERE key='defaults-rev'")
        self.addCleanup(writer.close)       # 关连接即回滚,读那一侧随之放行
        return writer

    def assert_reads_through(self, label, fn):
        baseline = fn()
        writer = self.hold_writer()
        read_ready._READY.clear()           # 等于「这个进程第一次碰这个库」
        stuck, out = within(fn)
        writer.close()
        self.assertFalse(stuck, f'{label}:进程里第一次读时被别人的写锁卡住了')
        self.assertNotIn('error', out, f'{label}:{out.get("error")!r}')
        self.assertEqual(baseline, out['value'], f'{label}:读到的不是已提交的那一份')

    def test_overview_reads_while_another_connection_holds_the_write_lock(self):
        def overview():
            data = api.overview('')
            return data['counts'], data['groups'], data['sources']
        self.assertTrue(any(api.overview('')['groups'].values()))   # 样例真的摆上了
        self.assert_reads_through('概览', overview)

    def test_lane_lists_and_detail_read_while_the_write_lock_is_held(self):
        for lane in lanes.LANES:
            self.assert_reads_through(f'列表 {lane.id}', lambda lid=lane.id: lanes.rows(lid))
        self.assert_reads_through('已搁置', lanes.shelved)
        some = next(row['key'] for lane in lanes.LANES for row in lanes.rows(lane.id)['items'])
        self.assert_reads_through('单条详情', lambda: lanes.one(some))


class ReadOnlyPathMigratesFirstTests(DiagCase):
    def old_store(self):
        """一个缺列缺表的存量库,换到新 inode 上:等于「升级之后这个进程第一次碰它」。"""
        self.seed()
        with reg.db() as c:
            c.execute('DROP TABLE feed_item_shelf')
            c.execute('ALTER TABLE feed_items DROP COLUMN fulltext_hold')
        old = reg.DB_PATH.read_bytes()
        for path in (reg.DB_PATH, *reg.DB_PATH.parent.glob('feeds.db-*')):
            path.unlink()
        reg.DB_PATH.write_bytes(old)

    def hold_writer(self):
        writer = sqlite3.connect(reg.DB_PATH, timeout=0, isolation_level=None)
        writer.execute('BEGIN IMMEDIATE')
        self.addCleanup(writer.close)
        return writer

    def test_a_store_missing_a_column_is_migrated_before_the_first_read(self):
        self.old_store()
        self.assertIsInstance(lanes.rows('body_held')['total'], int)
        with reg.read_db() as c:
            columns = {row[1] for row in c.execute('PRAGMA table_info(feed_items)')}
        self.assertIn('fulltext_hold', columns)
        self.assertIsInstance(lanes.shelved()['total'], int)

    def test_a_needed_migration_waits_no_longer_than_the_callers_timeout(self):
        self.old_store()
        writer = self.hold_writer()

        def first_read():
            with reg.read_db(timeout=0.3):
                pass
        stuck, out = within(first_read)
        writer.close()
        self.assertFalse(stuck, '要补列时没守调用方的时限,照默认的 30 秒去等写锁')
        self.assertIsInstance(out.get('error'), sqlite3.OperationalError, repr(out))
        # 反过来钉一边:锁放开之后同一问照旧补列、读得出来。
        with reg.read_db(timeout=0.3) as c:
            columns = {row[1] for row in c.execute('PRAGMA table_info(feed_items)')}
        self.assertIn('fulltext_hold', columns)

    def test_simultaneous_first_reads_migrate_once(self):
        self.old_store()
        opened = []
        real_db = reg.db

        def counted(*args, **kwargs):
            opened.append(1)
            return real_db(*args, **kwargs)
        writer = self.hold_writer()            # 让第一问卡在补列上,其余几问此刻也都到了
        with patch.object(reg, 'db', counted):
            readers = [threading.Thread(target=lambda: lanes.rows('body_held'), daemon=True)
                       for _ in range(6)]
            for reader in readers:
                reader.start()
            time.sleep(0.5)
            writer.close()
            for reader in readers:
                reader.join(PATIENCE)
        self.assertFalse(any(reader.is_alive() for reader in readers))
        self.assertEqual(1, len(opened), '同时首访的几问各自去补了一遍列')


if __name__ == '__main__':
    import unittest
    unittest.main()
