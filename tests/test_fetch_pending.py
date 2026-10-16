# -*- coding: utf-8 -*-
"""**「没有留下参与证据」不等于「没有参与」** —— 这一份钉第 3 轮审查那条拦验收。

当日抓取账本的每一次写都在一把旁挂的文件锁里,锁取不到就一行都不写、攥在内存
里等下次。那份内存状态随进程消失,于是上一版剩着这一刀:**某个子轮的登记行没
落盘、进程又没了**,盘上一个字都不说明它参与过 —— 而它可能已经往 vault 落了
文章。审查实测的形态是:第二个子轮因为锁暂时不可用没登记上、随后 `os._exit`,
盘上第一个子轮的开轮行/回执/结束行行行合法,`today()` 据此报**确定的 1 条**,而
vault 里躺着两篇;刷新、重启、重导都纠不回来(重导时那一篇已经不是新文件)。

同一条拦项的第二个实例在**缓冲自己**身上:追加是 I/O,它不能罩在进程内那把缓冲
锁里,于是「追加成功 → 清空缓冲」会把这期间另一条线程压进来的回执一起抹掉 ——
而面板每 15 秒读一次,`today()` 自己就是那个补写的人。

两刀的判据都落在**面板上那个数确定不确定**(用户照它决定要不要再花一次模型钱),
不验「标志打上没有」:
- 登记没落盘又硬退出 → 只许报下界(或者干脆「读不出来」),不许沿用上一个子轮
  的确定结论;**反过来钉一边**:行赶在进程没之前落了盘,结论要回到确定值,不许
  从此永久写「至少」。
- 另一条线程在追加期间压进来的回执 → 一条都不许少;**反过来钉一边**:写不下去
  的时候一行都不许剔掉。

参与证据的**寿命**(第 4、5 轮审查:凭证自己的失败、分代清理、以及那笔开工标记
算不算证据)在 tests/test_fetch_evidence.py;账本本体和它的恢复协议在
tests/test_fetch_rounds.py,成果归属在 tests/test_fetch_receipt.py,硬约束与事故
形态在 docs/decisions/daily.md。
"""
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from kb import fetch_pending, fetch_rounds
from tests.fetch_rounds_fixture import BOOK, REPO, T, _Pending

# 真实硬退出那一例的子进程。**不能用抛异常代替**:`finally` 还能跑的进程会把攥着
# 的行补写下去,而这一条要证的正是"补写的机会都没有"那一刀(审查原话:异常可执行
# finally 的用例不能代替硬退出)。
_CHILD = '''
import os, sys
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, {repo!r})
root = Path(sys.argv[1])
from kb import config, fetch_rounds, pipeline_stages
from kb import pipeline_state as ps
fetch_rounds.DATA_DIR = root
config.VAULT_ROOT = root / 'vault'
config.SOURCES = [config.Source(name='rss', root=config.VAULT_ROOT / 'source/rss')]
ps._state.update({{'running': True, 'batch': 'batch-a', 'run_id': 'run-2',
                   'planned_stages': ['crawl', 'export'], 'stop_requested': False,
                   'log': [], 'stages': {{s: {{'status': '', 'detail': ''}}
                                          for s in ps.STAGES}}}})
lock = root / fetch_rounds.LOCK_NAME
lock.unlink()                       # 这个子轮开工时账本那把锁坏了
lock.mkdir()


class Killed:
    name = label = 'rss'

    def fetch(self, scope):
        pass

    def materialize(self):
        target = config.VAULT_ROOT / 'source/rss/b.md'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('---\\ntitle: b\\n---\\n第二个子轮真写出来的那篇\\n',
                          encoding='utf-8')
        lock.rmdir()                # 锁好了,但这一刀落在它来得及补写之前
        os._exit(23)


with patch.object(pipeline_stages, 'crawl_channels',
                  Mock(return_value=[{{'channel': 'rss', 'accounts': []}}])), \\
        patch.object(pipeline_stages, 'active_collector', Mock(return_value=Killed())):
    pipeline_stages._run_crawl()
    pipeline_stages._run_export()
os._exit(70)                        # 走到这儿说明这条用例自己坏了
'''


class ParticipationOutlivesTheProcessTests(_Pending):
    """**第 3 轮审查 F1 的第一个实例**:登记行没落盘的子轮也得留下参与证据。"""

    def _first_sub_round(self, day):
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(day))        # 第一个子轮:确定 1 条

    def test_a_sub_round_whose_registration_never_landed_is_not_a_certain_count(self):
        """第二个子轮开工时锁坏了(登记行只攥在手里),它写出第二篇之后进程没了。

        盘上只有第一个子轮那一套合法的行,`segs == ends` 照旧成立 —— 上一版据此
        报**确定的 1 条**,而 vault 里躺着两篇。现在那一笔不走锁的凭证留下了
        「这一轮的证据还在我手里」,结论压到下界。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.break_lock()
        fetch_rounds.open_round('batch-a', day, seg='run-2')
        self.write('source/rss/b.md')                    # 它真写出了第二篇
        self.assertEqual([], [l for l in self.lines() if 'run-2' in l])
        self.process_gone()
        self.fix_lock()
        self.assertEqual(BOOK(1, 1, '09:00', True),
                         fetch_rounds.today(day))
        self.assertEqual(BOOK(1, 1, '09:00', True),
                         fetch_rounds.today(day))        # 重读一次:结论不变

    def test_lines_that_landed_before_the_process_died_take_the_doubt_away(self):
        """**反过来钉一边**:锁在进程没之前就好了、那几行补写成功 —— 结论要回到
        确定值。永久写「至少」的凭证等于把这颗 tooltip 作废,而它要回答的是"要不要再
        花一次模型钱"。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.break_lock()
        fetch_rounds.open_round('batch-a', day, seg='run-2')
        self.fix_lock()
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/b.md'))
        fetch_rounds.note_end('batch-a', 'run-2')
        self.assertEqual([], self.certs())               # 凭证撤掉了
        self.process_gone()
        self.assertEqual(BOOK(1, 2, '09:00'),
                         fetch_rounds.today(day))

    def test_a_round_whose_open_line_was_lost_is_unknown_not_an_older_count(self):
        """同一刀的**单轮形态**:第二次抓取连开轮行都没落下去,盘上最新的轮次是
        第一次那次。拿它的数当今天的答案,就是把上一次的成果说成这一次的 ——
        所以这一档只能答「读不出来」。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.break_lock()
        fetch_rounds.open_round('batch-b', T('2026-09-29 14:00'), seg='run-2')
        self.write('source/rss/b.md')
        self.process_gone()
        self.fix_lock()
        self.assertIsNone(fetch_rounds.today(day))

    def test_a_certificate_from_outside_the_window_does_not_haunt_today(self):
        """**反过来钉一边 + 挡无界增长**:凭证和账本共用同一个保留窗口。窗口外
        那几份忽略(账本自己也只留到那天 —— 它指的那一轮早被轮换清掉了,再认它
        就会从此永久答「读不出来」),而且下一次写成功时顺手删掉。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        folder = self.root / fetch_rounds.PENDING_DIR
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'dead-proc.json').write_text(
            json.dumps({'day': '2026-09-20', 'keys': ['ghost']}), encoding='utf-8')
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(day))
        fetch_rounds.note_new('batch-a', ())             # 下一次写:顺手清场
        self.assertEqual([], self.certs())

    def test_an_unreadable_certificate_is_unknown_not_nothing(self):
        """凭证读不出来(手改过、坏块、别的版本写的)是**说不清**,不是「没有」。

        合成一个空集的话,一次读故障就把「有一轮的证据还扣着」说成「一切都落
        盘了」—— 这一整层存在的理由就是不许出现那句话。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        folder = self.root / fetch_rounds.PENDING_DIR
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'broken.json').write_text('{"day": "2026-09', encoding='utf-8')
        self.assertIsNone(fetch_rounds.today(day))
        (folder / 'broken.json').unlink()
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(day))        # 反过来:好的凭证不拦

    def test_a_hard_exit_in_the_real_stages_still_leaves_the_participation(self):
        """整条管道走真的、进程**硬退出**(`os._exit(23)`,`finally` 一句都跑不到)。

        子进程里 `_run_crawl` / `_run_export` 都是真的,采集器真往 vault 写第二篇;
        账本那把锁在它开工时坏着,所以盘上没有它的任何一行 —— 断言就钉在这里:
        `run-2` 在账本里查不到,而面板那个数不许是确定的 1 条。"""
        fetch_rounds.open_round('batch-a', seg='run-1')
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a', 'run-1')
        before = fetch_rounds.today()
        self.assertEqual({'added': 1, 'partial': False},
                         {k: before[k] for k in ('added', 'partial')})
        script = self.root / 'child.py'
        script.write_text(_CHILD.format(repo=str(REPO)), encoding='utf-8')
        done = subprocess.run([sys.executable, str(script), str(self.root)],
                              capture_output=True, text=True)
        self.assertEqual(23, done.returncode, done.stderr)
        self.assertTrue((self.vault / 'source/rss/b.md').exists())
        self.assertEqual([], [l for l in self.lines() if 'run-2' in l])
        after = fetch_rounds.today()
        self.assertEqual({'added': 1, 'partial': True},
                         {k: after[k] for k in ('added', 'partial')})


class ConfirmRemovesOnlyWhatItWroteTests(_Pending):
    """**第 3 轮审查 F1 的第二个实例**:缓冲是两条线程共用的。

    追加是 I/O,罩进进程内那把缓冲锁就会把面板那一跳堵在磁盘故障后面 —— 所以它
    在锁外跑,而这期间另一条线程完全可以压进新的回执。上一版追加成功之后一句
    `_unsaved = []` 把它一起抹掉了:那一篇再也补不回账(重导时它已经不是新文件),
    而 `today()` 自称确定。
    """

    def test_a_receipt_queued_while_another_thread_flushes_is_not_erased(self):
        """面板那一跳(`today()` 替攥着的行补写)正卡在追加里,管道线程这时记下
        第二篇的回执 —— 追加回来之后它必须还在,随后照旧落盘、照旧算进那个数。

        交错是摆出来的,不靠撞运气:在 `_append` 里把面板那条线程按住,主线程这
        期间把锁占成目录(于是它走的是"攥着不写"那条路,正是上一版会被抹掉的那
        一批),再放开面板那条线程。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        self.break_lock()
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))   # 第一篇:攥着
        self.fix_lock()
        entered, release = threading.Event(), threading.Event()
        real_append = fetch_rounds._append
        reader = []

        def paused_append(batch):
            if threading.current_thread().name == 'panel':
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('放行等超时了')
            return real_append(batch)

        with patch.object(fetch_rounds, '_append', paused_append):
            panel = threading.Thread(name='panel',
                                     target=lambda: reader.append(
                                         fetch_rounds.today(day)))
            panel.start()
            self.assertTrue(entered.wait(10))
            self.break_lock()                 # 这会儿锁又坏了
            fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/b.md'))   # 第二篇:压进缓冲
            release.set()
            panel.join(10)
            self.assertFalse(panel.is_alive())
        self.fix_lock()
        self.assertIsNone(reader[0])          # 失锁期间面板如实说「读不出来」
        fetch_rounds.note_end('batch-a', 'run-1')
        book = '\n'.join(self.lines())
        self.assertIn('rss:source/rss/b.md', book)    # 那一行真落了盘
        self.assertEqual(BOOK(1, 2, '09:00'),
                         fetch_rounds.today(day))

    def test_a_flush_that_could_not_write_keeps_owing_every_line(self):
        """**反过来钉一边**:确认只剔自己写下去的那几行,**写不下去就一行都不剔**
        —— 连那笔凭证一起留着。磁盘一好,两篇都补得回来。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        with patch.object(fetch_rounds, '_append', Mock(return_value=False)):
            fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
            fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/b.md'))
            self.assertEqual(2, len([o for o in fetch_rounds._unsaved
                                     if 'items' in o]))
            # 读回来确认那一笔真落下去了(名字是分代的,别钉具体代号)
            self.assertEqual({'batch-a'}, fetch_pending.mine(self.pending()))
            self.assertEqual(1, len(self.certs()))       # 一代,不是一代一代堆着
            self.assertIsNone(fetch_rounds.today(day))
        fetch_rounds.note_end('batch-a', 'run-1')         # 磁盘好了
        self.assertEqual(BOOK(1, 2, '09:00'),
                         fetch_rounds.today(day))
        self.assertEqual([], self.certs())                # 落了盘就撤掉



if __name__ == '__main__':
    unittest.main()
