"""存量清理脚本(scripts/feed_trim_history.py)与它和同步之间的互斥。

这个脚本是**不可逆删除**:除了"清对了哪一批",还得钉住三件事 —— 共享条目
不许连带挖空另一个订阅、中途失败重跑能收敛、清理期间起跑的同步不能把刚删掉
的旧文写回来。它和**索引**之间的互斥在 `test_index_lock.py`。
"""
import sqlite3
import subprocess
import sys
import textwrap
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from kb import maintenance, store
from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync

from feed_trim_fixture import TrimFixture, _real_busy, trim  # noqa: E402


class TrimTests(TrimFixture, unittest.TestCase):
    # ---- 口径:清掉的正是以后会被拦下的那一批 ----
    def test_picks_the_same_set_the_floor_would_have_rejected(self):
        """和往后那道闸(`sync._within_history`)必须同一个口径。

        两处各判各的话,清掉的和以后拦掉的不是同一批,库里会留下一段谁也说不
        清的灰色地带。没有发布时间的照留,理由同 `_within_history`。
        """
        s = self.sub()
        rows = [self.aged(reg.HISTORY_DAYS + 1), self.aged(reg.HISTORY_DAYS), self.aged(800),
                {**self.aged(1), 'url': 'https://example.org/undated', 'published': ''}]
        floor = trim.planned_floor(reg.subscription('rss', s['id']))
        self.assertEqual(reg.history_floor(), floor)
        cut = {r['published'][:10] for r in trim.below_floor(rows, floor)}
        kept = {e['published'][:10] for e in sync._within_history(s, rows)}
        self.assertEqual(cut | kept, {r['published'][:10] for r in rows})
        self.assertFalse(cut & kept)
        self.assertIn('', kept)                      # 无日期的留着
        self.assertNotIn(floor, cut)                 # 边界当天不清

    def test_keeps_an_existing_floor(self):
        """已经钉过下界就用它自己那个,不按创建日重算。"""
        s = self.sub()
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET history_floor='2026-01-01' WHERE id=?", (s['id'],))
        self.assertEqual('2026-01-01', trim.planned_floor(reg.subscription('rss', s['id'])))

    # ---- 共享条目 ----
    def test_a_shared_article_keeps_its_file_and_index(self):
        """共享条目只摘归属:另一个订阅那边照样读得到、搜得到。

        原来按整份待清集合删文件和索引,而 `feed_items` 行(连同
        `exported_hash`)是留着的 —— 另一边仍然列着这篇文章,点进去读不到、
        搜不到,再同步也不恢复(导出按 exported_hash 直接跳过它)。
        """
        a = self.sub()
        shared = self.seed(a)
        lonely = self.seed(a, self.entry(url='https://example.org/only-a'))
        b = self.sub('Review B', 'https://example.org/b.xml')
        reg.put_items(b, [self.entry()])
        self.assertEqual(2, trim.cmd_run(self.args(a)))          # 默认拒绝
        self.assertTrue(self.state(shared)['file'])
        self.assertEqual(0, trim.cmd_run(self.args(a, allow_shared=True)))
        kept = self.state(shared)
        self.assertEqual((True, 1, True, True, 1), (kept['feed_row'], kept['memberships'],
                                                    kept['exported'], kept['file'],
                                                    kept['documents']))
        self.assertTrue(kept['searchable'])
        sync.export('rss')                                        # 下一轮同步照旧
        self.assertTrue(self.state(shared)['file'])
        dropped = self.state(lonely)                              # 不共享的照样清掉
        self.assertEqual((False, 0, False, 0), (dropped['feed_row'], dropped['memberships'],
                                                dropped['file'], dropped['documents']))

    def test_favorites_are_judged_on_what_will_really_be_deleted(self):
        """收藏保护只对真要删的那些判:共享条目压根不删文件,不该把整趟拦下。"""
        a = self.sub()
        shared = self.seed(a)
        b = self.sub('Review B', 'https://example.org/b.xml')
        reg.put_items(b, [self.entry()])
        conn = store.connect()
        conn.execute('INSERT INTO favorites(path,created_at,updated_at) VALUES(?,?,?)',
                     (shared['path'], '2026-10-01 09:00:00', '2026-10-01 09:00:00'))
        conn.commit(); conn.close()
        self.assertEqual(0, trim.cmd_run(self.args(a, allow_shared=True)))
        self.assertTrue(self.state(shared)['file'])

    # ---- 下界写法 ----
    def test_a_malformed_floor_is_refused_before_anything_changes(self):
        """少写一个零的日期不许拿去比较。

        `published[:10] < floor` 是字符串比较:`2026-10-1` 比当天那篇
        `2026-10-01 08:00` 大(于是被删),也比第二天的 `2026-10-02` 大
        (于是新文还被挡在门外)。
        """
        s = self.sub()
        item = self.seed(s, self.entry(published='2026-10-01 08:00'))
        before = self.floor_of(s)
        for bad in ('2026-10-1', '2026-2-30', '2026-02-30', 'yesterday', '20261001'):
            with self.subTest(bad):
                self.assertEqual(2, trim.cmd_run(self.args(s, floor=bad)))
                st = self.state(item)
                self.assertEqual((True, True, 1), (st['feed_row'], st['file'], st['documents']))
                self.assertEqual(before, self.floor_of(s))
        self.assertEqual('2026-10-01', trim.norm_floor(' 2026-10-01 '))

    def test_a_malformed_stored_floor_stops_the_run(self):
        """库里存着一个非规范下界时同样不许动手:比较口径已经不可信了。"""
        s = self.sub()
        item = self.seed(s)
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET history_floor='2026-9-1' WHERE id=?", (s['id'],))
        self.assertEqual(2, trim.cmd_run(self.args(s)))
        self.assertTrue(self.state(item)['file'])

    # ---- 中途失败 ----
    def test_a_failed_unlink_is_finished_by_the_next_run(self):
        """feeds.db 先提交,文件没删掉就抛 —— 重跑必须接着把它删完。

        不记进度的话重跑按 feeds 查出来的待清集合已经空了:命令打印「无事可做」
        并以 0 退出,而 md 文件、documents 行和 FTS 命中全都还在。
        """
        s = self.sub()
        item = self.seed(s)
        with patch.object(Path, 'unlink', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                trim.cmd_run(self.args(s))
        half = self.state(item)
        self.assertEqual((False, True, 1), (half['feed_row'], half['file'], half['documents']))
        self.assertEqual(0, trim.cmd_run(self.args(s)))
        done = self.state(item)
        self.assertEqual((False, False, 0, 0), (done['feed_row'], done['file'],
                                                done['documents'], done['searchable']))
        self.assertFalse(trim._plans())

    def test_a_failed_index_cleanup_is_finished_by_the_next_run(self):
        s = self.sub()
        item = self.seed(s)
        with patch.object(store, 'remove_missing_documents',
                          side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                trim.cmd_run(self.args(s))
        self.assertEqual(1, self.state(item)['documents'])
        self.assertEqual(0, trim.cmd_run(self.args(s)))
        done = self.state(item)
        self.assertEqual((False, 0, 0), (done['file'], done['documents'], done['searchable']))
        self.assertFalse(trim._plans())

    def test_an_unfinished_cleanup_is_reported_and_not_resumed_without_yes(self):
        s = self.sub()
        item = self.seed(s)
        with patch.object(Path, 'unlink', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                trim.cmd_run(self.args(s))
        self.assertEqual(1, len(trim._plans(s['id'])))
        self.assertEqual(1, trim.cmd_run(self.args(s, yes=False)))
        self.assertTrue(self.state(item)['file'])
        self.assertEqual(0, trim.cmd_scan(self.args(s, verbose=True)))

    # ---- 没有旧文可清 ----
    def test_an_empty_cut_still_pins_the_floor(self):
        """老订阅眼下恰好没有旧文 ≠ 不用钉下界。

        提前返回的话这一趟什么都没改变、命令还以 0 退出,而源站下次补发 7 月的
        东西照样整批收。
        """
        s = self.legacy(self.sub())
        self.seed(s, self.entry(published='2026-10-01 08:00'))
        self.assertEqual(0, trim.cmd_run(self.args(s)))
        self.assertEqual(reg.history_floor(), self.floor_of(s))
        s = reg.subscription('rss', s['id'])
        self.assertEqual(0, sync._save_result(
            s, ([self.entry(url='https://example.org/newly-visible')],
                dict(etag='n', modified='', resolved_url=s['url'])), FetchScope()))

    def test_an_empty_cut_changes_nothing_without_yes(self):
        s = self.legacy(self.sub())
        self.seed(s, self.entry(published='2026-10-01 08:00'))
        self.assertEqual(1, trim.cmd_run(self.args(s, yes=False)))
        self.assertEqual('', self.floor_of(s))

    def test_an_already_pinned_subscription_has_nothing_to_do(self):
        s = self.sub()
        self.seed(s, self.entry(published='2026-10-01 08:00'))
        with patch.object(trim, '_backup', side_effect=AssertionError('不该备份')):
            self.assertEqual(0, trim.cmd_run(self.args(s)))

    # ---- 和同步的互斥 ----
    def test_a_sync_holding_an_older_snapshot_cannot_resurrect_the_archive(self):
        """同步手里那份订阅快照是发请求**之前**读的。

        备份期间起跑的一轮同步拿着空下界的旧快照,在清理收尾之后存结果 ——
        刚删掉的旧文连文件带归属一起回来(审查实测重新入库 1 条)。下界现读
        才挡得住。
        """
        s = self.legacy(self.sub())
        stale = reg.subscription('rss', s['id'])          # 同步手里那一份
        item = self.seed(s)
        self.assertEqual(0, trim.cmd_run(self.args(s)))
        added = sync._save_result(stale, ([self.entry()],
                                          dict(etag='n', modified='', resolved_url=stale['url'])),
                                  FetchScope())
        sync.export('rss')
        st = self.state(item)
        self.assertEqual((0, False, False, 0), (added, st['feed_row'], st['file'], st['documents']))

    def test_the_write_paths_take_the_maintenance_lock(self):
        """存盘和导出都要在维护锁里:下界现读只拦"写回旧文",拦不住导出。

        导出是「选出待导出的行 → 逐篇写 vault」:清理删掉行之后,导出按自己
        那份快照照样把文件写回去,而 `feed_items` 里已经没有这一条 —— vault
        里从此躺着一篇谁也管不着的旧文,索引照样收它,下一次清理也找不到它。
        """
        s = self.sub()
        taken = []

        @contextmanager
        def spy():
            taken.append(1)
            yield

        with patch.object(sync, 'maintenance_lock', spy):
            sync._save_result(s, ([self.entry(published='2026-10-01 08:00')],
                                  dict(etag='n', modified='', resolved_url=s['url'])), FetchScope())
            self.assertEqual(1, len(taken))
            sync.export('rss')
            self.assertEqual(2, len(taken))

    def test_the_maintenance_lock_really_excludes_another_process(self):
        """锁必须跨进程:CLI、kb-web 管道、手动重跑各是独立进程。"""
        child = textwrap.dedent('''
            import fcntl, sys
            fh = open(sys.argv[1], 'a+')
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                print('free')
            except OSError:
                print('busy')
        ''')
        lock = self.root / 'feeds-trim.lock'
        with maintenance.maintenance_lock():
            self.assertTrue(lock.exists())
            r = subprocess.run([sys.executable, '-c', child, str(lock)],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual('busy', r.stdout.strip())
        r = subprocess.run([sys.executable, '-c', child, str(lock)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual('free', r.stdout.strip())

    def test_the_maintenance_lock_is_reentrant_in_one_thread(self):
        """flock 认的是 open file description,同进程换个 fd 再锁会把自己锁死。"""
        with maintenance.maintenance_lock():
            with maintenance.maintenance_lock():
                pass

    # ---- 开工前那道状态查询 ----
    def test_an_unreadable_pipeline_status_blocks_the_run(self):
        """问不到管道状态就别猜:这是不可逆删除。

        连接被拒是唯一能证明"服务没起、没人在跑"的那一种,放行;超时、HTTP
        错、回包不是 JSON 都只说明问不到 —— 原来这三种一律当成"空闲"放行。
        """
        cases = {urllib.error.URLError(TimeoutError('timed out')): True,
                 urllib.error.URLError(ConnectionRefusedError()): False,
                 ValueError('not json'): True,
                 OSError('broken pipe'): True}
        for err, blocked in cases.items():
            with self.subTest(type(err).__name__):
                with patch.object(trim.urllib.request, 'urlopen', side_effect=err):
                    self.assertEqual(blocked, bool(_real_busy()))


if __name__ == '__main__':
    unittest.main()
