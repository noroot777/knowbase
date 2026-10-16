# -*- coding: utf-8 -*-
"""一次写故障之后的**重放协议**:账本既不许少记、也不许多记。

从 tests/test_fetch_rounds.py 分出来(那一份已经顶着行数上限),职责也确实是另一
件事:那边问「盘上这几行折出来是多少」,这边问「写不下去的那几行后来怎么办」——
开轮行没落盘、`fsync` 报错、攥着的行被挤掉、三元组某个字段不是文本、轮换和追加
撞在一起,各有一次实测形态。

断言一律落在**面板上那个数**(`today()`),不验"标志打上没有":攥着的行有没有
丢、重放会不会翻倍,只有那个数答得出来。闸本身(两处证据都留不下就不许往下抓)
在 tests/test_fetch_evidence.py;账本本体与完整性在 tests/test_fetch_rounds.py。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kb import config, fetch_rounds
from kb.collectors import Fetched
from tests.fetch_rounds_fixture import BOOK, T


class RecoveryIsExactlyOnceTests(unittest.TestCase):
    """**这一组是问题 1 的要害**:一次写故障之后,重放既不许少记、也不许多记。

    两条是第 3 轮审查实测出来的形态,都不靠重启 —— 旧实现在同一个进程里就已经
    把数搞错了:

    - 数据目录短暂不可用让开轮那一行没落下去,随后物化真落了 2 篇 —— 旧
      `note_new` 只查盘上有没有这一轮,查不到就把整份清单丢掉,面板从此笃定地
      写「0 条」,刷新也纠不回来。
    - `write` 成功而 `fsync` 报错(那几行其实已经进了文件),重试把同一行再追加
      一遍 —— 存条数的话 2 篇报成 4 篇。

    还有两条按同一类列进来:攥不住的行不许静默少记;轮换(整份重写)和追加之间
    不许互相覆盖。断言一律落在**面板上那个数**是什么,不验"标志打上没有"。
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.vault = self.root / 'vault'
        fetch_rounds._unsaved = []
        self.addCleanup(setattr, fetch_rounds, '_unsaved', [])
        for p in (patch.object(fetch_rounds, 'DATA_DIR', self.data),
                  patch.object(config, 'VAULT_ROOT', self.vault),
                  patch.object(config, 'SOURCES',
                               [config.Source(name='rss',
                                              root=self.vault / 'source' / 'rss')])):
            p.start(); self.addCleanup(p.stop)
        self.day = T('2026-09-29 09:00')

    def _paths(self, *names, of='batch-a'):
        """这一轮抓到的那几篇,按真实回执的形状(内容身份 + 抓取身份 + 路径)。"""
        return frozenset(Fetched(f'rss:{n}', of, f'source/rss/{n}.md')
                         for n in names)

    def test_a_round_recovered_after_a_failed_open_still_records_its_catch(self):
        """数据目录短暂不可用 → 开轮一个字都没写下去 → 恢复之后同一轮交回 2 篇。

        实测的旧末态:`{'n': 1, 'added': 0}`,而且是**确定**的 0 —— 开轮行后来
        补写成功了,清单却已经被丢掉,重读账本也是 0。

        整个数据目录被占成普通文件时,登记行和那笔凭证**一起**留不下,所以这一轮
        被闸拦住(那一刀自己的红在 tests/test_fetch_evidence.py)。这一条钉的是它
        之后的那一段:**攥着的那几行一个都没丢** —— 故障过去、用户再点一次同步,
        同一轮的回执照旧并进去,而且 `n` 还是 1(两条开轮行折成同一轮,不是第二次
        抓取)。"""
        import shutil
        shutil.rmtree(self.data)
        self.data.write_text('占住目录位置', encoding='utf-8')     # 目录变成普通文件
        with self.assertRaises(fetch_rounds.NoEvidence):
            fetch_rounds.open_round('batch-a', self.day)
        self.assertIsNone(fetch_rounds.today(self.day))            # 此刻只能说不可确认
        self.data.unlink()
        self.data.mkdir()
        fetch_rounds.open_round('batch-a', self.day)                # 故障过去:这次过闸
        fetch_rounds.note_new('batch-a', self._paths('a', 'b'))
        fetch_rounds.note_end('batch-a')
        self.assertEqual(BOOK(1, 2, '09:00'),
                         fetch_rounds.today(self.day))

    def test_a_retry_after_a_failed_fsync_does_not_double_count(self):
        """`write` 成功、`fsync` 报错:那一行其实已经在文件里,重试再追加一遍。

        实测的旧末态:2 篇报成 `added: 4`。现在回执存**内容身份**、折叠按它取
        并集,所以同一行出现两次和出现一次是同一个结论。

        顺带钉住 N4 那一条:故障期间 `today()` 答「不可确认」,**故障一好,下一次
        读自己就把攥着的行补写下去**(上一版要等下一次抓取才补,面板在这期间
        一直写「读不出来」)。"""
        fetch_rounds.open_round('batch-a', self.day)
        with patch.object(os, 'fsync', Mock(side_effect=OSError('fsync 失败'))):
            fetch_rounds.note_new('batch-a', self._paths('a', 'b'))
            fetch_rounds.note_end('batch-a')
            self.assertIsNone(fetch_rounds.today(self.day))   # 故障还在:不可确认
        book = (self.data / fetch_rounds.FILE_NAME).read_text(encoding='utf-8')
        self.assertIn('rss:a', book)                    # 行其实已经写进去了
        got = fetch_rounds.today(self.day)              # 故障好了:这一下自己补写
        lines = (self.data / fetch_rounds.FILE_NAME).read_text(
            encoding='utf-8').splitlines()
        # 同一行确实重复落进了文件(不重复的话这条用例证明不了去重)
        self.assertGreaterEqual(sum(1 for l in lines if 'rss:a' in l), 2)
        self.assertEqual(BOOK(1, 2, '09:00'), got)

    def test_rows_that_had_to_be_dropped_keep_the_round_unconfirmed(self):
        """磁盘一直写不进去,攥在手里的行超过上限被挤掉 —— 挤掉的换成「不可确认」。

        旧实现直接切掉尾部:剩下的补写成功之后 `today()` 会重新给出一个**确定**、
        但已经少记的数(这里是 70 篇只剩 63 篇),而面板上一点异样都看不出来。

        **这一轮的物化确实交完了**(下面补记了结束行),所以 `partial` 只可能来自
        挤掉那几行换上的标记 —— 不补这一行的话,「没交完」会把缺口盖住,摘掉标记
        的注入照样是绿的(第 1 轮审查改完 `end` 之后实测如此)。"""
        fetch_rounds.open_round('batch-a', self.day)
        with patch.object(fetch_rounds, '_append', Mock(return_value=False)):
            for i in range(70):
                fetch_rounds.note_new('batch-a', self._paths(f'x{i}'))
        fetch_rounds.open_round('batch-a', self.day)     # 磁盘好了,补写
        fetch_rounds.note_end('batch-a')                 # 逐来源循环确实走完了
        got = fetch_rounds.today(self.day)
        self.assertEqual(1, got['n'])                    # 开轮行没被挤掉
        self.assertLess(got['added'], 70)                # 确实少记了
        self.assertTrue(got['partial'])                  # 但它如实说了自己不全

    def test_a_field_that_is_not_text_is_a_gap_not_a_quiet_skip(self):
        """三元组里某个字段不是字符串:记成这一轮的缺口,不许 `str()` 一下糊过去。

        `str(None)` 是 `'None'`,而那两个位置上的 `'None'` 各有一种骗法:
        当抓取身份用时它是个**非空**值,会被归进「之前抓到、这一次才补导的」;
        当路径用时它不在任何来源树里,会被范围过滤当成**正常筛掉** —— 一个缺口
        于是伪装成确定的零条。两样都是把「不知道」写成一个确定答案。

        判据落在后果上:那一条不计数,但这一轮如实说自己不全(`partial`)。
        反过来钉一边在 `test_only_the_sources_the_report_reads_are_counted`:
        真的不在取材范围内的那几条是正常筛掉,不该把整轮拖成「至少」。
        """
        for bad in (Fetched('rss:a', None, 'source/rss/a.md'),
                    Fetched('rss:a', 'batch-a', None),
                    Fetched(None, 'batch-a', 'source/rss/a.md')):
            with self.subTest(bad=bad):
                fetch_rounds._unsaved = []
                (self.root / fetch_rounds.FILE_NAME).unlink(missing_ok=True)
                fetch_rounds.open_round('batch-a', self.day)
                fetch_rounds.note_new('batch-a', [
                    Fetched('rss:ok', 'batch-a', 'source/rss/ok.md'), bad])
                self.assertEqual(BOOK(1, 1, '09:00', partial=True),
                                 fetch_rounds.today(self.day))

    def test_a_round_that_still_owes_lines_is_not_rotated_away(self):
        """**审查实测的那一条(F2)**:旧开轮已经落盘、它的回执还攥在手里,
        下一次开轮先走轮换 —— 清掉那一轮的话,随后补写的回执就成了**找不到轮次的
        孤行**,折叠据此报「读不出来」,而「读不出来」又禁止下一次轮换,那条孤行
        从此清不掉。

        实测的旧末态:当天那一轮正常落盘、文章也正常保存,`today()` 却是 `None`;
        全新进程读同一份账本也是 `None`;第二天再正常开一轮、收一轮,仍是 `None`
        —— 面板从此一直写「抓取次数读不出来」,刷新、重启、再同步都不恢复。

        **反过来钉一边**:欠账补写完之后那一轮该退役就退役(连成果带结束行),
        不是靠把保留窗口拉长糊过去。"""
        old = T('2026-10-13 09:00')
        fetch_rounds.open_round('old-round', old, seg='old-child')
        with patch.object(fetch_rounds, '_append', Mock(return_value=False)):
            fetch_rounds.note_new('old-round', self._paths('old', of='old-round'))
            fetch_rounds.note_end('old-round', 'old-child')
        self.assertTrue(fetch_rounds._unsaved)           # 前提:行真攥在手里
        day = T('2026-10-16 09:00')
        fetch_rounds.open_round('today-round', day, seg='today-child')  # 触发轮换
        fetch_rounds.note_new('today-round', self._paths('new', of='today-round'))
        fetch_rounds.note_end('today-round', 'today-child')
        rounds, ok = fetch_rounds._read()
        self.assertTrue(ok)                              # 一条孤行都没留下
        self.assertEqual({'old-round', 'today-round'}, {r['key'] for r in rounds})
        self.assertEqual(BOOK(1, 1, '09:00'), fetch_rounds.today(day))
        for name in ('_unsaved', '_opened', '_gaps'):    # 进程没了,盘上这份照样对
            setattr(fetch_rounds, name, [])
        self.assertEqual(BOOK(1, 1, '09:00'), fetch_rounds.today(day))
        # 欠账清完了:再开一轮时那一轮才真的成组退役
        nxt = T('2026-10-17 09:00')
        fetch_rounds.open_round('next-round', nxt, seg='next-child')
        fetch_rounds.note_new('next-round', ())
        fetch_rounds.note_end('next-round', 'next-child')
        self.assertNotIn('old-round', (self.data / fetch_rounds.FILE_NAME)
                         .read_text(encoding='utf-8'))
        self.assertEqual(BOOK(1, 0, '09:00'), fetch_rounds.today(nxt))

    def test_a_round_another_process_still_owes_is_not_rotated_away(self):
        """同一类的另一半:欠着行的是**另一个进程**,而它那笔凭证的日子停在三天前
        (凭证只在搬动缓冲时才重写,那个进程这期间没动过)。按保留窗口筛凭证的话,
        这一轮照旧被清走,它随后补写的回执就是那条孤行。

        所以轮换这一侧认**盘上还在的每一笔凭证**,不按天筛;凭证自己的过期清理归
        `fetch_pending._prune`(和账本同一个 `_floor`),所以放宽这一刀不会让孤儿
        凭证永久扣着一轮 —— 这里顺带钉住那一头:那笔过期凭证在这一轮里就被收掉了,
        于是下一次轮换那一轮正常退役。"""
        old = T('2026-10-13 09:00')
        fetch_rounds.open_round('old-round', old, seg='old-child')
        folder = self.data / fetch_rounds.PENDING_DIR
        folder.mkdir(parents=True, exist_ok=True)
        # 另一个进程那笔凭证(TAG 不是本进程的,日子停在开轮那天)
        (folder / 'other-7-deadbeef-1.json').write_text(
            json.dumps({'day': '2026-10-13', 'keys': ['old-round']}),
            encoding='utf-8')
        day = T('2026-10-16 09:00')
        fetch_rounds.open_round('today-round', day, seg='today-child')  # 触发轮换
        fetch_rounds.note_new('today-round', self._paths('new', of='today-round'))
        fetch_rounds.note_end('today-round', 'today-child')
        # 那个进程这时候才把它攥着的那几行补写下去(交错是摆出来的,不靠撞运气)
        self.assertTrue(fetch_rounds._append([
            {'key': 'old-round', 'items': [['rss:old', 'old-round']]},
            {'key': 'old-round', 'seg': 'old-child', 'end': 1}]))
        rounds, ok = fetch_rounds._read()
        self.assertTrue(ok)                              # 不是孤行:它那一轮还在
        self.assertEqual(BOOK(1, 1, '09:00'), fetch_rounds.today(day))
        nxt = T('2026-10-17 09:00')
        fetch_rounds.open_round('next-round', nxt, seg='next-child')
        fetch_rounds.note_new('next-round', ())
        fetch_rounds.note_end('next-round', 'next-child')
        self.assertNotIn('old-round', (self.data / fetch_rounds.FILE_NAME)
                         .read_text(encoding='utf-8'))   # 凭证收掉了,这才退役
        self.assertEqual(BOOK(1, 0, '09:00'), fetch_rounds.today(nxt))

    def _elsewhere(self, key, at, seg):
        """**另一个真进程**在 `at` 开一轮并交完 —— 它看不见我们缓冲里攥着的那几行。

        跨进程是这一组里唯一能摆出「凭证被别人收掉、而所有者还活着」的手法:同一个
        进程里 `_owed()` 的缓冲那一半会把那一轮保下来,所以单进程版本永远绿。
        """
        code = ('from datetime import datetime; from pathlib import Path; '
                'from kb import fetch_rounds as r; '
                f'r.DATA_DIR = Path({str(self.data)!r}); '
                f'at = datetime.fromisoformat({at.isoformat()!r}); '
                f'r.open_round({key!r}, at, seg={seg!r}); '
                f'r.note_new({key!r}, ()); r.note_end({key!r}, {seg!r})')
        done = subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(0, done.returncode, done.stderr)

    def test_a_round_retired_by_another_process_is_not_written_back(self):
        """**同一类的第三种形态,这一条是审查实测的那个**:欠着行的进程活着,它那笔
        凭证被**别的进程**按日期收掉(`fetch_pending._prune`),那一轮于是正常退役
        —— 随后它一补写就是一条谁也认不了的孤行,整份账本从此「读不出来」,而
        `ok=False` 又禁掉下一次轮换,那条孤行永久清不掉:面板写「抓取次数读不出来」,
        刷新、重启、第二天再同步都不恢复。

        所以补写那一侧也要闭合:落盘前先问一句「我这几行的父轮还在吗」,不在就连那笔
        欠账一起退役(`fetch_rotate.disown`)。**丢掉的一定是过了保留窗口的轮次的行**
        —— 轮换只清这种,面板只显示今天,所以这里断言的末态是「今天那两轮一个不少、
        而且是确定的数」,不是「那条旧回执还在」。"""
        old = T('2026-10-13 09:00')
        # 时钟也要退回那一天:那笔凭证记的是**它落下来的那一天**,而「这一轮欠着行」
        # 要跨过保留窗口才被别人收掉(凭证只在搬动缓冲时才重写,这个进程这期间没动过)。
        real = fetch_rounds._now
        with patch.object(fetch_rounds, '_now',
                          lambda now=None: real(now or old)):
            fetch_rounds.open_round('old-round', old, seg='old-child')
            with patch.object(fetch_rounds, '_append', return_value=False):
                fetch_rounds.note_new('old-round', self._paths('old', of='old-round'))
                fetch_rounds.note_end('old-round', 'old-child')
        self.assertTrue(fetch_rounds._unsaved)           # 摆盘自检:确实欠着行
        # 别的进程开第一轮:它读到我们那笔凭证,所以这一轮还保着 old-round;
        # 但它落盘时顺手把那笔**过期**凭证收掉了。
        self._elsewhere('new-round-1', T('2026-10-16 09:00'), 'new-1-child')
        self.assertIn('old-round', (self.data / fetch_rounds.FILE_NAME)
                      .read_text(encoding='utf-8'))
        # 它开第二轮:凭证没了,old-round 正常退役
        self._elsewhere('new-round-2', T('2026-10-16 10:00'), 'new-2-child')
        self.assertNotIn('old-round', (self.data / fetch_rounds.FILE_NAME)
                         .read_text(encoding='utf-8'))
        later = T('2026-10-16 10:01')
        self.assertEqual(BOOK(2, 0, '10:00'), fetch_rounds.today(later))
        rounds, ok = fetch_rounds._read()
        self.assertTrue(ok)                              # 没留下孤行
        self.assertEqual([], fetch_rounds._unsaved)      # 那笔欠账成组退役了
        # 全新进程读出来是同一句话(内存里什么都没有,盘上留下什么就是什么)
        code = ('import json; from datetime import datetime; from pathlib import Path; '
                'from kb import fetch_rounds as r; '
                f'r.DATA_DIR = Path({str(self.data)!r}); '
                "print(json.dumps(r.today(datetime.fromisoformat('2026-10-16 10:02'))))")
        done = subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, check=True,
                              cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(BOOK(2, 0, '10:00'),
                         json.loads(done.stdout.strip().splitlines()[-1]))

    def test_an_empty_ledger_does_not_retire_what_is_still_owed(self):
        """再反过来钉一边:账本**整份空着**时,攥着的行一行都不许退役。

        「父轮不在了」有两种来头:过期那一轮被别的进程清走了(清完总还剩今天那几轮),
        以及这份展示账本被整个删掉/还没建起来。后一种里连**今天**那一轮的欠账都会被
        当成孤儿 —— 丢了就是静默少报,而面板会笃定地说「今天还没有抓取」。这一档宁可
        写成孤行:末态是「读不出来」(显式故障),而且下一次开轮的孤行清理会把它收走。"""
        fetch_rounds.open_round('batch-a', self.day, seg='a-child')
        with patch.object(fetch_rounds, '_append', return_value=False):
            fetch_rounds.note_new('batch-a', self._paths('a', 'b'))
        book = self.data / fetch_rounds.FILE_NAME
        book.unlink()                                     # 账本被整个删掉
        self.assertIsNone(fetch_rounds.today(self.day))    # 不是笃定的 0 条
        self.assertIn('rss:a', book.read_text(encoding='utf-8'))  # 那几行写下去了
        # 下一次开轮把这条孤行收走,面板据此回到一个确定的数(那一轮的开轮行跟着
        # 文件一起没了,本来就显示不出来)
        later = T('2026-09-29 10:00')
        fetch_rounds.open_round('batch-b', later, seg='b-child')
        fetch_rounds.note_new('batch-b', ())
        fetch_rounds.note_end('batch-b', 'b-child')
        self.assertNotIn('rss:a', book.read_text(encoding='utf-8'))
        self.assertEqual(BOOK(1, 0, '10:00'), fetch_rounds.today(later))

    def test_an_orphan_row_left_behind_is_cleaned_up_but_a_broken_one_is_not(self):
        """反过来钉一边:孤行**清得掉**,读不出来的行**一个字都不许动**。

        孤行认得出来是谁的,而它那一轮的开轮行已经不在 —— 没有开轮行的轮次本来就不
        显示,所以持锁重写时把它清掉,账本据此从「永久读不出来」里回来(旧版本留下的
        那份坏账本也是这样自己恢复的)。半截行、被改坏的行是另一档:它的内容我们压根
        不知道,拿认得出来的那部分重写回去就是把它悄悄删掉,所以那时连轮换一起停。"""
        book = self.data / fetch_rounds.FILE_NAME
        orphan = json.dumps({'key': 'gone-round', 'items': [['rss:x', 'gone-round']]})
        book.write_text(orphan + '\n', encoding='utf-8')
        self.assertIsNone(fetch_rounds.today(self.day))      # 摆盘自检:读不出来
        fetch_rounds.open_round('batch-a', self.day, seg='a-child')
        fetch_rounds.note_new('batch-a', self._paths('a'))
        fetch_rounds.note_end('batch-a', 'a-child')
        self.assertNotIn('gone-round', book.read_text(encoding='utf-8'))
        self.assertEqual(BOOK(1, 1, '09:00'), fetch_rounds.today(self.day))
        # 另一半:同一个位置换成一行读不出来的垃圾,**而且摆一轮过期的进去** ——
        # 轮换本来就只在确有东西要清时才重写,不摆的话这一半对注入不敏感(实测红 0)。
        stale = json.dumps({'day': '2026-09-20', 'key': 'stale-round',
                            'at': '2026-09-20 09:00', 'ex': 1})
        book.write_text(stale + '\n{"key": "half-written", "items": [[',
                        encoding='utf-8')
        fetch_rounds.open_round('batch-b', self.day, seg='b-child')
        kept = book.read_text(encoding='utf-8')
        self.assertIn('half-written', kept)              # 一个字都没动
        self.assertIn('stale-round', kept)               # 连过期那一轮也没清
        self.assertIsNone(fetch_rounds.today(self.day))

    def test_a_slow_rotation_does_not_overwrite_what_another_run_appended(self):
        """轮换是**整份重写**:它和别人的追加不在同一把锁里的话,就会按自己那份
        旧快照把人家已经确认的行覆盖掉。

        交错是摆出来的,不靠撞运气:账本里先放一条过期的轮次(逼这一次开轮走
        轮换),再把整份重写那一下拖慢,期间另一条运行开自己的轮、记自己的回执。
        末态要求:两轮都在,各自的条数都对。`flock` 认的是打开的文件描述符,
        同进程不同 fd 一样互斥,所以线程够用。

        **不要改回"几条线程各跑几轮"的写法** —— 那种写法注入(把 flock 摘掉)
        之后全绿:轮换只在有过期轮次时触发,第一次重写之后就没有了,后面几十次
        开轮压根不走那条路。"""
        from kb import fetch_rotate
        fetch_rounds.open_round('old', T('2026-09-20 09:00'))
        slow = fetch_rotate._write_atomic

        def lag(*a, **k):
            time.sleep(0.4)             # 把整份重写那一下拖长
            return slow(*a, **k)

        # 整份重写本体在 `kb.fetch_rotate`(`fetch_rounds._rotate` 只管「现在许不许
        # 重写」),所以要拖慢的是那一边那个符号。
        with patch.object(fetch_rotate, '_write_atomic', Mock(side_effect=lag)):
            rot = threading.Thread(target=fetch_rounds.open_round,
                                   args=('batch-rot', self.day))
            rot.start()
            time.sleep(0.15)            # 让它读完旧账、正卡在重写里
            fetch_rounds.open_round('batch-app', self.day)
            fetch_rounds.note_new('batch-app', self._paths('a', 'b', of='batch-app'))
            rot.join()
        rounds, ok = fetch_rounds._read()
        self.assertTrue(ok)
        self.assertEqual({'batch-rot': 0, 'batch-app': 2},
                         {r['key']: len(r['items']) for r in rounds
                          if r['day'] == '2026-09-29'})


if __name__ == '__main__':
    unittest.main()
