"""存量清理脚本(scripts/feed_trim_history.py)与它和同步之间的互斥。

这个脚本是**不可逆删除**:除了"清对了哪一批",还得钉住三件事 —— 共享条目
不许连带挖空另一个订阅、中途失败重跑能收敛、清理期间起跑的同步不能把刚删掉
的旧文写回来。它和**索引**之间的互斥在 `test_index_lock.py`。
"""
import json
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import unittest
import urllib.error
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from kb import maintenance, store
from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync

from feed_trim_fixture import TrimFixture, _real_busy, trim  # noqa: E402


class _FixedNow(datetime):
    """把备份目录的时间戳钉死,复现「两次清理落在同一秒」。

    用子类而不是 Mock:`planned_floor` 还要 `datetime.fromisoformat`。
    """

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 1, 11, 0, 0)


class TrimTests(TrimFixture, unittest.TestCase):
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

    # ---- 点名哪个订阅 ----
    def test_a_numeric_source_is_resolved_by_id_only(self):
        """纯数字只按 id 查:名字恰好叫「62」的订阅不许抢走 id=62。

        原来是 `id=? OR name=?` 再 `fetchone()`,名字命中排在前面 —— 实测
        `--source <B 的 id>` 清掉的是「名字叫那个数字」的 A,真正点名的 B
        一个字节没动,而且是确定性的。
        """
        a = self.sub('A', 'https://example.org/a.xml')
        b = self.sub('B', 'https://example.org/b.xml')
        reg.save_subscription('rss', {'name': str(b['id'])}, a['id'])
        keep = self.seed(a, self.entry(url='https://example.org/in-a'))
        gone = self.seed(b, self.entry(url='https://example.org/in-b'))
        self.assertEqual(0, trim.cmd_run(self.args(b)))
        self.assertTrue(self.state(keep)['file'])            # 名字叫数字的那个没被动
        self.assertEqual(1, self.state(keep)['documents'])
        self.assertFalse(self.state(gone)['file'])           # 点名的那个清掉了
        self.assertEqual(0, self.state(gone)['documents'])

    def test_a_duplicate_name_refuses_instead_of_guessing(self):
        """重名时拒绝执行:注册表允许同名不同链接,挑哪一条都可能是错的那条。"""
        x = self.sub('Same', 'https://example.org/x.xml')
        self.sub('Same', 'https://example.org/y.xml')
        item = self.seed(x)
        with patch.object(trim, '_backup', side_effect=AssertionError('不该备份')):
            self.assertEqual(2, trim.cmd_run(self.args(x, source='Same')))
        self.assertTrue(self.state(item)['file'])
        self.assertEqual(2, trim.cmd_run(self.args(x, source='9999')))
        self.assertEqual(2, trim.cmd_run(self.args(x, source='Nope')))
        self.assertTrue(self.state(item)['feed_row'])

    def test_a_unique_name_still_works(self):
        s = self.sub('Only One', 'https://example.org/one.xml')
        item = self.seed(s)
        self.assertEqual(0, trim.cmd_run(self.args(s, source='Only One')))
        self.assertFalse(self.state(item)['file'])

    # ---- 续跑不许绕过护栏 ----
    def test_a_favorite_added_after_the_failure_blocks_the_resume(self):
        """失败之后新增的收藏,续跑不许当成"上次已经批准过"。

        收藏那道护栏排在新计划后面,续跑从旁边绕过去了:实测没加
        --allow-favorites 的重跑把文件、documents 和收藏一起删了,退出码 0。
        """
        s = self.sub()
        item = self.seed(s)
        with patch.object(Path, 'unlink', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                trim.cmd_run(self.args(s))
        conn = store.connect()
        conn.execute('INSERT INTO favorites(path,created_at,updated_at) VALUES(?,?,?)',
                     (item['path'], '2026-10-01 11:00:00', '2026-10-01 11:00:00'))
        conn.commit(); conn.close()
        self.assertEqual(2, trim.cmd_run(self.args(s)))
        st = self.state(item)
        self.assertEqual((True, 1, 1), (st['file'], st['documents'], self.favorites()))
        self.assertEqual(1, len(trim._plans(s['id'])))       # 计划还留着,能再续
        self.assertEqual(0, trim.cmd_run(self.args(s, allow_favorites=True)))
        self.assertEqual((False, 0, 0), (self.state(item)['file'],
                                         self.state(item)['documents'], self.favorites()))

    def _star(self, path, note='race-note'):
        """另开一条连接提交一条带备注的片段收藏(模拟收藏 API)。"""
        conn = store.connect()
        try:
            conn.execute('INSERT INTO favorites(path,quote,note,created_at,updated_at)'
                         ' VALUES(?,?,?,?,?)',
                         (path, 'quoted text', note, '2026-10-01 12:00:00',
                          '2026-10-01 12:00:00'))
            conn.commit()
        finally:
            conn.close()

    def test_a_favorite_committed_after_approval_is_not_deleted(self):
        """确认之后、删除之前收藏成功的那篇,不许连备注一起被删掉。

        护栏原来是「查完就关连接」,从那一刻到真正删除之间还有确认、备份打包、
        进度落盘、逐个 unlink 一整段:收藏 API 在这段时间里返回 200、库里真有
        那一行,随后 documents 行一删、`ON DELETE CASCADE` 把收藏连用户刚写的
        备注一起带走,命令还以 0 退出(2026-10-01 第 3 轮审查实测)。索引能从
        vault 重建,收藏和备注不能。
        """
        s = self.sub()
        item = self.seed(s)
        real = trim._backup

        def backup_then_star(*a, **kw):
            out = real(*a, **kw)
            self._star(item['path'])          # 备份之后、_apply 之前
            return out

        with patch.object(trim, '_backup', backup_then_star):
            self.assertEqual(2, trim.cmd_run(self.args(s)))
        st = self.state(item)
        self.assertEqual((True, 1, 1, 1),
                         (st['file'], st['documents'], st['searchable'], self.favorites()))
        self.assertEqual(1, len(trim._plans(s['id'])))   # 计划留着,能续

        # 显式放行时照删,但要毁掉的那几行连备注一起进备份(md 压缩包里没有它们)
        self.assertEqual(0, trim.cmd_run(self.args(s, allow_favorites=True)))
        st = self.state(item)
        self.assertEqual((False, 0, 0, 0),
                         (st['file'], st['documents'], st['searchable'], self.favorites()))
        saved = [json.loads((d / 'favorites.json').read_text(encoding='utf-8'))
                 for d in (self.root / 'backups').glob('feedtrim-*')
                 if (d / 'favorites.json').exists()]
        self.assertEqual(1, len(saved))
        self.assertEqual(['race-note'], [r['note'] for r in saved[0]])

    def test_the_deletion_window_locks_out_favorite_writes(self):
        """删除整段持 kb.db 写锁:这期间的收藏写入只能等/报错,不会"成功了又被删"。

        只靠"动手前再查一遍"挡不住查完那一瞬之后提交的那条;真正关掉这条缝的
        是 `BEGIN IMMEDIATE`。
        """
        s = self.sub()
        item = self.seed(s)
        side = store.connect()
        side.execute('PRAGMA busy_timeout=200')
        seen = {}
        real = trim._drop_index

        def drop_with_a_racing_star(conn, paths):
            try:
                side.execute('INSERT INTO favorites(path,created_at,updated_at)'
                             ' VALUES(?,?,?)',
                             (item['path'], '2026-10-01 12:00:00', '2026-10-01 12:00:00'))
                side.commit()
                seen['err'] = None
            except sqlite3.OperationalError as e:
                side.rollback()
                seen['err'] = str(e)
            return real(conn, paths)

        try:
            with patch.object(trim, '_drop_index', drop_with_a_racing_star):
                self.assertEqual(0, trim.cmd_run(self.args(s)))
        finally:
            side.close()
        self.assertIn('locked', (seen.get('err') or ''))   # 写锁真的挡住了
        self.assertEqual(0, self.favorites())
        self.assertFalse(self.state(item)['file'])

    # ---- 备份目录 ----
    def test_two_cleanups_in_the_same_second_get_their_own_backup(self):
        """同名订阅同一秒清理,不许复用同一个备份目录。

        原来「slug + 秒」+ exist_ok:manifest / md 压缩包 / progress.json 全被
        后一个覆盖 —— 前一个清掉的内容既不在库里也不在备份里,它那份没做完的
        计划也一起没了,重跑只说「无事可做」。
        """
        a = self.sub('Same name', 'https://example.org/a.xml')
        b = self.sub('Same name', 'https://example.org/b.xml')
        first = self.seed(a, self.entry(url='https://example.org/in-a'))
        second = self.seed(b, self.entry(url='https://example.org/in-b'))
        with patch.object(trim, 'datetime', _FixedNow):
            with patch.object(Path, 'unlink', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    trim.cmd_run(self.args(a))
            self.assertEqual(0, trim.cmd_run(self.args(b)))
        dirs = sorted((self.root / 'backups').glob('feedtrim-*'))
        self.assertEqual(2, len(dirs))                       # 各占一个目录
        self.assertEqual(1, len(trim._plans(a['id'])))       # a 那份计划还在
        tars = [p for d in dirs for p in d.glob('vault-md.tar.gz')]
        kept = {n for t in tars for n in tarfile.open(t).getnames()}
        self.assertEqual({first['path'], second['path']}, kept)
        self.assertEqual(0, trim.cmd_run(self.args(a)))      # a 续得完
        self.assertEqual((False, 0), (self.state(first)['file'], self.state(first)['documents']))

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
