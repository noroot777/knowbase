# -*- coding: utf-8 -*-
"""**「今天第几次抓取」的先后按业务起跑顺序,不按谁最后把行补写进文件** —— 第 1 轮
逻辑审查 F2。

账本是多进程追加的,而一轮的行可能**晚落盘**:锁被占住、写失败时那几行攥在内存里,
下一次操作才补上。原来投影按物理行序排、`today()` 取末位,于是实测形态是:A 轮
11:00 开、交 2 条回执,行因为短暂写失败留在内存;B 轮 12:00 正常开、交 1 条,整轮
落盘;A 的故障过去后补写 —— 两轮全在盘上、没有欠账,而面板写「第 2 次抓取 · +2
条」、时间退回 11:00。重放同一份文件还是这个答案。

所以顺序要是**数据的函数**:开轮行上带一笔微秒级起跑时刻(`t`),投影按
`(day, t, key)` 排好再交出去(`kb.fetch_fold._order_key`),轮换整份重写时把 `t` 一起
抄走。这一组钉的就是「谁是最后一轮」这一句在三条路上都一致:真进程补写、纯投影
重放、轮换之后再读。

**同一句话管到日归属**(第 2 轮审查 F1:同一个不变量没修完整)。「这一轮算哪天的」
也不许由行序决定:同一个业务身份跨零点重复开轮时,原来起跑时刻取了最早那条、`day`
还留着物理首行的,于是昨天 23:59 起跑的那一轮落在今天名下 —— 今天还没同步过,药丸
却说「第 1 次抓取 · +3 条」,今天真的第一次同步之后它又变成「第 2 次」。所以
`day`/`at`/`t` 是**一份事实**,整份取最早那一条(`fetch_fold._start`)。

判据照旧落在**面板上那个数**(`today()` 的 `added` / `at` / `n`),不验排序表达式。
硬约束与事故形态在 docs/decisions/daily.md。
"""
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from kb import fetch_rounds
from tests.fetch_rounds_fixture import REPO, T, _Base


# 另一个进程:它的缓冲是空的,所以它那一轮**整轮先落盘**(这正是本进程那几行
# 「晚落盘」的由来 —— 同一个进程里做不出这个形态,`_flush` 会把攥着的行先写出去)。
_OTHER_PROCESS = '''
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, {repo!r})
from kb import config, fetch_rounds
root = Path(sys.argv[1])
fetch_rounds.DATA_DIR = root
config.VAULT_ROOT = root / 'vault'
config.SOURCES = [config.Source(name='rss', root=config.VAULT_ROOT / 'source/rss')]
key, at = sys.argv[2], datetime.fromisoformat(sys.argv[3])
fetch_rounds.open_round(key, at, seg=key + '-run')
fetch_rounds.note_new(key, {{'source/rss/' + key + '.md'}})
fetch_rounds.note_end(key, key + '-run')
'''


class TheLatestRoundIsTheLatestStartedTests(_Base):
    """**晚落盘的那一轮不许冒充最后一轮。**"""

    def _other_process_round(self, key, at):
        script = self.root / 'other_process.py'
        script.write_text(_OTHER_PROCESS.format(repo=str(REPO)), encoding='utf-8')
        done = subprocess.run([sys.executable, str(script), str(self.root), key, at],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stderr)

    def test_a_round_that_lands_late_does_not_become_the_latest(self):
        """审查那一例,整条记账走真的:11:00 那一轮的行晚落盘,12:00 那一轮先落。

        末态是两轮都在盘上、谁都不欠行(`partial=False`),所以这一句只能由「谁先
        起跑」回答。拿物理行序回答的后果是用户看到的条数和时间都是**上一次**那一轮
        的 —— 而他正是靠这个数决定要不要再花一次模型钱。
        """
        day = T('2026-09-29 13:00')
        with patch.object(fetch_rounds, '_append', return_value=False):
            fetch_rounds.open_round('earlier-round', T('2026-09-29 11:00'),
                                    seg='earlier-run')
            fetch_rounds.note_new('earlier-round',
                                  {'source/rss/earlier-a.md', 'source/rss/earlier-b.md'})
            fetch_rounds.note_end('earlier-round', 'earlier-run')
        self._other_process_round('later-round', '2026-09-29 12:00')
        got = fetch_rounds.today(day)                    # 这一下顺手把攥着的行补写
        # 摆盘自检:补写之后,12:00 那一轮的行确实排在前面(物理顺序是反的)
        body = (self.root / fetch_rounds.FILE_NAME).read_text(encoding='utf-8')
        self.assertLess(body.index('later-round'), body.index('earlier-round'))
        self.assertEqual([], fetch_rounds._unsaved)      # 两轮都在盘上、谁都不欠行
        self.assertEqual({'n': 2, 'added': 1, 'partial': False, 'at': '12:00'}, got)
        # 重放:同一份文件再读一次(新进程、空缓冲)还是同一个答案
        self.assertEqual(got, fetch_rounds.today(day))

    def test_the_order_survives_a_rotation_and_a_replay(self):
        """**轮换整份重写之后还是同一个答案** —— `t` 要被抄过去。

        轮换是台面下的维护动作(清掉过期那几天),用户只会看到数字自己变了。漏抄
        `t` 的话这一天就退回分钟级精度,同一分钟内的先后又由物理行序决定。
        """
        day = T('2026-09-29 13:00')
        # 两轮**同一分钟**内开,而且三样东西刻意互相矛盾:物理行序、起跑时刻、key 的
        # 字典序。只有按起跑时刻排才会挑中 `aa-late` —— 丢掉微秒、或者拿 key/行序兜,
        # 挑中的都是 `zz-early`(它的条数是 2)。
        lines = [
            # 过期那一天:它存在才会触发整份重写
            {'day': '2026-09-20', 'key': 'old', 'at': '2026-09-20 10:00',
             't': '2026-09-20 10:00:00.000000', 'ex': 0},
            {'day': '2026-09-29', 'key': 'aa-late', 'at': '2026-09-29 11:00',
             't': '2026-09-29 11:00:59.900000', 'ex': 1},
            {'key': 'aa-late', 'seg': 's'}, {'key': 'aa-late', 'paths': ['source/rss/s.md']},
            {'key': 'aa-late', 'seg': 's', 'end': 1},
            {'day': '2026-09-29', 'key': 'zz-early', 'at': '2026-09-29 11:00',
             't': '2026-09-29 11:00:01.100000', 'ex': 1},
            {'key': 'zz-early', 'seg': 'f'},
            {'key': 'zz-early', 'paths': ['source/rss/f1.md', 'source/rss/f2.md']},
            {'key': 'zz-early', 'seg': 'f', 'end': 1},
        ]
        (self.root / fetch_rounds.FILE_NAME).write_text(''.join(
            json.dumps(o, ensure_ascii=False) + '\n' for o in lines), encoding='utf-8')
        expected = {'n': 2, 'added': 1, 'partial': False, 'at': '11:00'}
        self.assertEqual(expected, fetch_rounds.today(day))
        # 开一轮新的 → 触发轮换(过期那天被清掉),随后同一句话还要成立
        fetch_rounds.open_round('third', T('2026-09-29 12:00'),
                                materializes=False, seg='t')
        body = (self.root / fetch_rounds.FILE_NAME).read_text(encoding='utf-8')
        self.assertNotIn('2026-09-20', body)             # 真轮换过
        self.assertIn('11:00:59.900000', body)           # `t` 抄过去了
        self.assertEqual({'n': 3, 'added': 0, 'partial': False, 'at': '12:00'},
                         fetch_rounds.today(day))
        # 把最后那一轮摘掉再读:11:00 那两轮的先后仍然是起跑顺序,不是行序
        kept = [l for l in body.splitlines() if '"third"' not in l]
        (self.root / fetch_rounds.FILE_NAME).write_text('\n'.join(kept) + '\n',
                                                        encoding='utf-8')
        self.assertEqual(expected, fetch_rounds.today(day))

    def test_two_rounds_inside_the_same_minute_still_keep_their_order(self):
        """**同一分钟内开的两轮也要按起跑顺序** —— 这是「分钟级精度不够」那一句的红。

        一天里同一分钟开两轮不难碰上(定时那一轮刚起,用户紧跟着手点一次同步)。
        这一条整条走真的:11:00:01 那一轮的行晚落盘,11:00:59 那一轮先落,而两个 key
        的字典序又和起跑顺序相反 —— 只有开轮行上真带着那笔微秒级时刻、而且投影按它
        排序,挑中的才是真正最后开的那一轮。
        """
        day = T('2026-09-29 13:00')
        with patch.object(fetch_rounds, '_append', return_value=False):
            fetch_rounds.open_round('zz-early', T('2026-09-29 11:00:01'), seg='z')
            fetch_rounds.note_new('zz-early',
                                  {'source/rss/z1.md', 'source/rss/z2.md'})
            fetch_rounds.note_end('zz-early', 'z')
        self._other_process_round('aa-late', '2026-09-29 11:00:59')
        self.assertEqual({'n': 2, 'added': 1, 'partial': False, 'at': '11:00'},
                         fetch_rounds.today(day))

    def test_rounds_without_the_fine_grained_mark_keep_working(self):
        """**反过来钉一边:存量账本行(没有 `t`)照旧按分钟排,不许一个数都读不出来。**

        平局(同一分钟、都没有 `t`)按 `key` 定,**这是刻意的**:同一刻开的两轮没有
        真实先后可恢复,而重放、补写、轮换重写必须给出同一个答案 —— 拿物理位置兜
        平局就又把它押回行序上了。
        """
        day = T('2026-09-29 13:00')
        lines = [
            {'day': '2026-09-29', 'key': 'b-round', 'at': '2026-09-29 11:00', 'ex': 1},
            {'key': 'b-round', 'seg': 'b'}, {'key': 'b-round', 'paths': ['source/rss/b.md']},
            {'key': 'b-round', 'seg': 'b', 'end': 1},
            {'day': '2026-09-29', 'key': 'a-round', 'at': '2026-09-29 11:00', 'ex': 1},
            {'key': 'a-round', 'seg': 'a'},
            {'key': 'a-round', 'paths': ['source/rss/a1.md', 'source/rss/a2.md']},
            {'key': 'a-round', 'seg': 'a', 'end': 1},
            # 更晚的一天内时刻:它在最前面,照样不是「最后一轮」
            {'day': '2026-09-29', 'key': 'early', 'at': '2026-09-29 09:00', 'ex': 1},
            {'key': 'early', 'seg': 'e'}, {'key': 'early', 'seg': 'e', 'end': 1},
        ]
        (self.root / fetch_rounds.FILE_NAME).write_text(''.join(
            json.dumps(o, ensure_ascii=False) + '\n' for o in lines), encoding='utf-8')
        # 11:00 那两轮平局 → 按 key:'b-round' > 'a-round',所以末位是 b 那一轮
        self.assertEqual({'n': 3, 'added': 1, 'partial': False, 'at': '11:00'},
                         fetch_rounds.today(day))

    def test_a_round_reopened_after_a_read_failure_keeps_its_first_start(self):
        """**同一轮重复开的那几条开轮行里,起跑时刻取最早那一条。**

        读失败那一档会给同一个 key 再写一条开轮行(`open_round` 读不出账本时走的是
        「没见过这一轮」那条分支)。按「先见到的那条」算的话,这一轮几点开的又变成
        行序的函数 —— 两条行谁先落盘就听谁的。
        """
        day = T('2026-09-29 13:00')
        with patch.object(fetch_rounds, '_append', return_value=False):
            fetch_rounds.open_round('batch-a', T('2026-09-29 10:00'), seg='run-1')
            fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
            fetch_rounds.note_end('batch-a', 'run-1')
        # 另一个进程把**同一轮**又开了一次(它读不到那几行,所以照旧写一条开轮行),
        # 于是物理上 12:00 那条在前、10:00 那条是补写进来的
        self._other_process_round('batch-a', '2026-09-29 12:00')
        got = fetch_rounds.today(day)
        body = (self.root / fetch_rounds.FILE_NAME).read_text(encoding='utf-8')
        self.assertEqual(2, body.count('"day"'))         # 摆盘自检:真写了两条开轮行
        self.assertLess(body.index('12:00'), body.index('10:00'))
        self.assertEqual('10:00', got['at'])             # 这一轮是 10 点开的
        self.assertEqual({'n': 1, 'added': 2, 'partial': False},
                         {k: got[k] for k in ('n', 'added', 'partial')})


class TheDayARoundBelongsToTests(_Base):
    """**一轮算哪一天的,按它开工那天 —— 不按哪条开轮行先落盘。**

    契约本身是存量的(`test_fetch_rounds` 里 23:59 开、00:01 才物化完那一条),这一组
    钉的是它在**同一轮被重复开**时还成立:跨零点时那两条开轮行不同天。
    """

    def _other_process_round(self, key, at):
        script = self.root / 'other_process.py'
        script.write_text(_OTHER_PROCESS.format(repo=str(REPO)), encoding='utf-8')
        done = subprocess.run([sys.executable, str(script), str(self.root), key, at],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stderr)

    def _layout(self, lines):
        (self.root / fetch_rounds.FILE_NAME).write_text(''.join(
            json.dumps(o, ensure_ascii=False) + '\n' for o in lines), encoding='utf-8')

    def test_a_round_that_started_yesterday_is_not_todays_first(self):
        """审查那一例,整条记账走真的:23:59 开的那一轮行晚落盘,00:01 那个子轮先落。

        末态是两条开轮行都在盘上、谁都不欠行,所以「这一轮算哪天的」只能由起跑事实
        回答。错的后果不止 tooltip:用户今天还没同步,药丸就说「第 1 次抓取 · +3
        条」(那 3 条是昨天的),他据此以为今天已经抓过;今天真的第一次同步之后它又
        跳成「第 2 次」。
        """
        with patch.object(fetch_rounds, '_append', return_value=False):
            fetch_rounds.open_round('batch-a', T('2026-09-29 23:59'), seg='early-run')
            fetch_rounds.note_new('batch-a', {'source/rss/a.md', 'source/rss/b.md'})
            fetch_rounds.note_end('batch-a', 'early-run')
        # 另一个进程为**同一个业务身份**登记第二个子轮,已经过了零点
        self._other_process_round('batch-a', '2026-09-30 00:01')
        today = fetch_rounds.today(T('2026-09-30 00:01'))   # 这一下顺手补写攥着的行
        # 摆盘自检:两条开轮行都在盘上,而 00:01 那条物理上在前
        body = (self.root / fetch_rounds.FILE_NAME).read_text(encoding='utf-8')
        self.assertEqual(2, body.count('"day"'))
        self.assertLess(body.index('00:01'), body.index('23:59'))
        self.assertEqual([], fetch_rounds._unsaved)
        self.assertEqual({'n': 0, 'added': 0, 'partial': False, 'at': ''}, today)
        # 它整轮算在 29 日:3 条都在那一轮名下(两个子轮都交完了,所以不是下界)
        self.assertEqual({'n': 1, 'added': 3, 'partial': False, 'at': '23:59'},
                         fetch_rounds.today(T('2026-09-29 23:59')))
        # 重放同一份文件(空缓冲)还是同一个答案
        self.assertEqual({'n': 0, 'added': 0, 'partial': False, 'at': ''},
                         fetch_rounds.today(T('2026-09-30 00:01')))

    def test_the_same_events_in_either_layout_answer_the_same(self):
        """**同一组合法事件,两种物理布局必须给同一个答案** —— 这是「投影是数据的
        函数」最直接的那一刀:两份文件逐行等价、只是两个子轮整组换了位置。"""
        early = [
            {'day': '2026-09-29', 'key': 'batch-a', 'at': '2026-09-29 23:59',
             't': '2026-09-29 23:59:00.000000', 'ex': 1},
            {'key': 'batch-a', 'seg': 'early'},
            {'key': 'batch-a', 'paths': ['source/rss/a.md', 'source/rss/b.md']},
            {'key': 'batch-a', 'seg': 'early', 'end': 1},
        ]
        later = [
            {'day': '2026-09-30', 'key': 'batch-a', 'at': '2026-09-30 00:01',
             't': '2026-09-30 00:01:00.000000', 'ex': 1},
            {'key': 'batch-a', 'seg': 'later'},
            {'key': 'batch-a', 'paths': ['source/rss/c.md']},
            {'key': 'batch-a', 'seg': 'later', 'end': 1},
        ]
        want_today = {'n': 0, 'added': 0, 'partial': False, 'at': ''}
        want_yesterday = {'n': 1, 'added': 3, 'partial': False, 'at': '23:59'}
        for name, lines in (('晚的在前', later + early), ('早的在前', early + later)):
            with self.subTest(name):
                self._layout(lines)
                self.assertEqual(want_today, fetch_rounds.today(T('2026-09-30 00:01')))
                self.assertEqual(want_yesterday,
                                 fetch_rounds.today(T('2026-09-29 23:59')))

    def test_an_open_line_without_a_start_time_cannot_erase_the_day(self):
        """**反过来钉一边:说不出起跑时刻的那条开轮行,不许顶掉说得出的那条。**

        存量账本、被手改过的行可能连 `at` 都没有。让「空」赢的话,这一轮的 `day` 也
        跟着被抹空,它从此落不进任何一天 —— 面板永久说「今天还没有抓取」,而那一轮
        真抓了。两种布局都要成立。
        """
        good = [
            {'day': '2026-09-29', 'key': 'batch-a', 'at': '2026-09-29 11:00',
             't': '2026-09-29 11:00:00.000000', 'ex': 1},
            {'key': 'batch-a', 'seg': 'g'},
            {'key': 'batch-a', 'paths': ['source/rss/a.md', 'source/rss/b.md']},
            {'key': 'batch-a', 'seg': 'g', 'end': 1},
        ]
        blank = [{'key': 'batch-a', 'ex': 1}]            # 连 day 和 at 都没有
        want = {'n': 1, 'added': 2, 'partial': False, 'at': '11:00'}
        for name, lines in (('空的在后', good + blank), ('空的在前', blank + good)):
            with self.subTest(name):
                self._layout(lines)
                self.assertEqual(want, fetch_rounds.today(T('2026-09-29 13:00')))

    def test_two_open_lines_with_no_start_time_still_pick_the_earlier_day(self):
        """两条都说不出起跑时刻时,按**更早那一天**算,而且两种布局同一个答案。

        这一档没有真实先后可恢复,但答案仍必须是数据的函数;拿物理位置兜就又押回
        行序上了。往早的那天算也是安全那一侧:记在今天会让药丸多报一次抓取。
        """
        newer = [{'day': '2026-09-29', 'key': 'batch-a', 'ex': 1}]
        older = [{'day': '2026-09-28', 'key': 'batch-a', 'ex': 1},
                 {'key': 'batch-a', 'paths': ['source/rss/a.md']}]
        for name, lines in (('新的在前', newer + older), ('旧的在前', older + newer)):
            with self.subTest(name):
                self._layout(lines)
                self.assertEqual({'n': 0, 'added': 0, 'partial': False, 'at': ''},
                                 fetch_rounds.today(T('2026-09-29 13:00')))
                # 28 日那边有这一轮:没有子轮行,所以条数只是下界
                self.assertEqual({'n': 1, 'added': 1, 'partial': True, 'at': ''},
                                 fetch_rounds.today(T('2026-09-28 13:00')))

    def test_a_rotation_writes_the_round_back_onto_its_own_day(self):
        """**轮换是归一化者,不是把矛盾固化下来的那一下。**

        轮换整份重写时,一轮只剩一条开轮行 —— 它得带着那份规范化的起跑事实。写回
        后开那一条的日期,这一轮就被永久搬到了第二天,而原始的两条行已经没了。
        """
        lines = [
            # 过期那一天:它存在才会触发整份重写
            {'day': '2026-09-20', 'key': 'old', 'at': '2026-09-20 10:00', 'ex': 0},
            {'day': '2026-09-30', 'key': 'batch-a', 'at': '2026-09-30 00:01',
             't': '2026-09-30 00:01:00.000000', 'ex': 1},
            {'key': 'batch-a', 'seg': 'later'},
            {'key': 'batch-a', 'paths': ['source/rss/c.md']},
            {'key': 'batch-a', 'seg': 'later', 'end': 1},
            {'day': '2026-09-29', 'key': 'batch-a', 'at': '2026-09-29 23:59',
             't': '2026-09-29 23:59:00.000000', 'ex': 1},
            {'key': 'batch-a', 'seg': 'early'},
            {'key': 'batch-a', 'paths': ['source/rss/a.md', 'source/rss/b.md']},
            {'key': 'batch-a', 'seg': 'early', 'end': 1},
        ]
        self._layout(lines)
        self.assertEqual({'n': 0, 'added': 0, 'partial': False, 'at': ''},
                         fetch_rounds.today(T('2026-09-30 00:01')))
        # 轮换只在写路径上发生:真开一轮新的(不物化,所以它自己是确定的 0 条)
        fetch_rounds.open_round('third', T('2026-09-30 00:02'),
                                materializes=False, seg='t')
        opens = [json.loads(l) for l in
                 (self.root / fetch_rounds.FILE_NAME).read_text(encoding='utf-8').splitlines()
                 if 'day' in json.loads(l)]
        self.assertEqual(['batch-a', 'third'], sorted(o['key'] for o in opens))
        batch = next(o for o in opens if o['key'] == 'batch-a')
        self.assertEqual({'day': '2026-09-29', 'at': '2026-09-29 23:59',
                          't': '2026-09-29 23:59:00.000000'},
                         {k: batch[k] for k in ('day', 'at', 't')})
        self.assertEqual({'n': 1, 'added': 0, 'partial': False, 'at': '00:02'},
                         fetch_rounds.today(T('2026-09-30 00:02')))
        self.assertEqual({'n': 1, 'added': 3, 'partial': False, 'at': '23:59'},
                         fetch_rounds.today(T('2026-09-29 23:59')))

if __name__ == '__main__':
    unittest.main()
