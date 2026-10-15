# -*- coding: utf-8 -*-
"""当日抓取账本:今天第几次抓取、那一次往 vault 新落了多少篇。

这一组钉三件事,三件都曾经错过,而且错法一次比一次隐蔽。

**① 「抓了多少」不是「这一次生成能选进去几条」。** 按钮上一度报的是日报取材
结果,于是:第二次同步真抓回来 9 条、名额只剩 3 个就写「3 条」;抓回来那条发布
得早、抢不到补录席位就写「没有新内容」;一条都没再抓、只是重新生成了一次日报,
那个数就从 9 变成 0。

**② 「抓了多少」也不是「库里头一回出现这个路径的文档数」。** 改成由索引阶段数
之后仍然是拿消费者的结果猜抓取:空抓取碰上 3 篇历史未索引的旧文会报成「+3 条」,
反过来真抓到 2 篇而这一轮不索引则报 0、之后单独索引也补不回来。成果的归属只能
由抓取自己定 —— 数的是**物化**那一步往 vault 新落下的 md。

**③ 「读不出来」不是「没抓到」。** 账本写失败被吞掉的话,25 篇文章已经入库而
按钮永久写 0 条;坏掉的账本被当成空的话,面板会笃定地说「今天还没有抓取」,
下一次开轮次还把它覆盖成「第 1 次」。

所以断言一律落在**后果**上:面板上那个数是什么、它在生成日报前后变没变、
重启之后还在不在、故障之后它说的是「0」还是「不可确认」—— 只验"账本写了没有"
的话,一个把取材数抄进去的实现也能全绿。

两个方向都钉住:该算一次的算一次(多来源、多阶段共用批次身份),不该算的不算
(只勾索引/只勾导出的那种运行、衍生树、停用的树、历史旧文件)。

成果**归属**(回执由谁交)在 tests/test_fetch_receipt.py,那一条属于哪一次抓取在
tests/test_fetch_identity.py;
日报取材口径本身在 tests/test_daily_carried.py 和 tests/test_daily_report.py;
前端那几种说法在 tests/daily_probe.mjs。
"""
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from kb import config, daily, daily_carry, fetch_rounds, llm, llm_stages
from kb import pipeline_batch, store
from kb import pipeline_state as ps
from kb.collectors import Fetched
from fetch_rounds_fixture import BOOK, T, _Base


class RoundBookTests(_Base):
    """账本本体:几次、几条、哪一天、故障时说什么。"""

    def test_a_second_sync_the_same_day_is_a_second_round(self):
        """当天两轮:第二轮是「第 2 次」,报的是它自己抓了多少,不是两轮的和。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', self.caught(
            'batch-a', *(f'source/rss/p{i}.md' for i in range(9))))
        self.assertEqual(BOOK(1, 9, '09:00'),
                         fetch_rounds.today(day))
        later = T('2026-09-29 15:30')
        fetch_rounds.open_round('batch-b', later)
        fetch_rounds.note_new('batch-b', self.caught(
            'batch-b', 'source/rss/q1.md', 'source/rss/q2.md'))
        self.assertEqual(BOOK(2, 2, '15:30'),
                         fetch_rounds.today(later))

    def test_one_business_fetch_across_several_sources_is_one_round(self):
        """勾了三个来源同步 = 一次抓取、三次管道运行。按运行记的话面板会写成
        「今天第 3 次抓取」而用户只点过一次。"""
        day = T('2026-09-29 09:00')
        for _ in range(3):
            fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', self.caught(
            'batch-a', *(f'source/rss/{c}.md' for c in 'abcd')))
        self.assertEqual(BOOK(1, 4, '09:00'),
                         fetch_rounds.today(day))

    def test_a_run_that_did_not_fetch_is_not_a_round(self):
        """没开过轮次的运行(只勾导出/索引)落下来的东西不该凭空变出一次抓取。"""
        fetch_rounds.note_new('run-x', self.caught(
            'run-x', 'source/rss/a.md', 'source/rss/b.md'))
        self.assertEqual(BOOK(0, 0, ''),
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_an_empty_fetch_still_counts_as_a_fetch(self):
        """什么都没抓到也是抓过一次 —— 用户要能看出"同步跑了,确实没有新的"。

        走生产路径:物化交回来的是一张**空回执**(`note_new(key, ())`),这才是
        「确定 0 条」那句话的依据。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', ())
        self.assertEqual(BOOK(1, 0, '09:00'),
                         fetch_rounds.today(day))

    def test_a_run_that_will_not_materialize_is_a_certain_zero(self):
        """只勾了抓取的那种运行:一篇都落不进 vault,所以 0 条是**确定**的,
        不是「读不出来」—— 它压根没有物化阶段来交回执。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, materializes=False)
        self.assertEqual(BOOK(1, 0, '09:00'),
                         fetch_rounds.today(day))

    def test_a_round_that_should_have_reported_but_did_not_is_unknown(self):
        """反过来钉一边:会物化、却一条回执都没等到(进程在写账本之前就没了)
        —— 那是「不可确认」。

        这两条在账本上长得一模一样(一条开轮行、没有回执),差别只在开轮时记下的
        那个 `ex`。混成一句的后果是:物化真落了 9 篇、进程随后被 kill,面板笃定地
        写「第 1 次抓取 · 0 条」,用户据此不再重新生成日报,那 9 篇就这么漏掉。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        self.assertEqual(BOOK(1, 0, '09:00', True),
                         fetch_rounds.today(day))

    def test_yesterdays_rounds_do_not_count_as_today(self):
        """跨自然日自己归零:昨天抓过 30 条,今天面板上是「今天还没有抓取」。"""
        fetch_rounds.open_round('batch-a', T('2026-09-28 22:00'))
        fetch_rounds.note_new('batch-a', self.caught(
            'batch-a', *(f'source/rss/p{i}.md' for i in range(30))))
        self.assertEqual(BOOK(0, 0, ''),
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_a_round_that_finishes_after_midnight_still_gets_its_count(self):
        """23:59 开的轮次,00:01 才物化完 —— 它那一笔得记得上(按**开工那天**
        归属)。按"现在是今天"去找轮次的话,这一轮永远停在 0 条。"""
        fetch_rounds.open_round('batch-a', T('2026-09-29 23:59'))
        fetch_rounds.note_new('batch-a', self.caught(
            'batch-a', 'source/rss/a.md', 'source/rss/b.md', 'source/rss/c.md'))
        self.assertEqual(BOOK(1, 3, '23:59'),
                         fetch_rounds.today(T('2026-09-29 23:59')))
        # 过了零点:那一轮算在 29 日,30 日这边还没有抓取
        self.assertEqual(BOOK(0, 0, ''),
                         fetch_rounds.today(T('2026-09-30 00:01')))

    def test_the_day_boundary_follows_the_project_timezone(self):
        """「今天」按项目时区(Asia/Shanghai),不按宿主机。UTC 机器上按宿主机
        算的话,北京时间 00:00~07:59 这八小时会被记进前一天。"""
        at = datetime(2026, 9, 29, 16, 30, tzinfo=timezone.utc)   # 沪 09-30 00:30
        fetch_rounds.open_round('batch-a', at)
        fetch_rounds.note_new('batch-a', self.caught('batch-a', 'source/rss/a.md'))
        self.assertEqual(BOOK(1, 1, '00:30'),
                         fetch_rounds.today(at))
        self.assertEqual(BOOK(0, 0, ''),
                         fetch_rounds.today(T('2026-09-29 23:00')))
        # 反过来钉一边:不带时区的照旧当项目时区,不再按 UTC 解释一次
        fetch_rounds.open_round('batch-b', T('2026-09-30 09:00'))
        self.assertEqual(2, fetch_rounds.today(T('2026-09-30 09:00'))['n'])

    def test_the_round_number_does_not_stop_at_a_cap(self):
        """同一天抓很多次时次数照旧往上走 —— 旧实现只留最后 200 条明细而
        `n` 取明细条数,第 201 次之后次数就永久停在 200。"""
        day = T('2026-09-29 09:00')
        for i in range(205):
            fetch_rounds.open_round(f'batch-{i}', day)
        self.assertEqual(205, fetch_rounds.today(day)['n'])

    def test_the_count_survives_a_restart(self):
        """账本在数据目录里,重启 kb-web 之后那个数还在。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', self.caught(
            'batch-a', *(f'source/rss/{c}.md' for c in 'abcde')))
        self.assertEqual(5, fetch_rounds.today(day)['added'])   # 每次都重新读文件

    def test_a_restarted_process_does_not_reuse_a_batch_identity(self):
        """重启之后批次号不许撞上今天已经记过的那一轮 —— 撞上的话第二次同步
        会被当成同一轮(面板停在「第 1 次」,新抓的直接累加进旧那一行)。"""
        day = T('2026-09-29 09:00')
        with patch.object(ps, '_RUN_EPOCH', 'aaaa'):
            first = pipeline_batch.open_batch()
            pipeline_batch.close_batch()
        with patch.object(ps, '_RUN_EPOCH', 'bbbb'), \
                patch.object(pipeline_batch, '_batch_seq', 0):
            second = pipeline_batch.open_batch()
            pipeline_batch.close_batch()
        self.assertNotEqual(first, second)
        for key, n in ((first, 2), (second, 3)):
            fetch_rounds.open_round(key, day)
            fetch_rounds.note_new(key, self.caught(
                key, *(f'source/rss/{key}-{i}.md' for i in range(n))))
        self.assertEqual({'n': 2, 'added': 3}, {k: fetch_rounds.today(day)[k]
                                                for k in ('n', 'added')})

    def test_a_missing_book_is_a_certain_no_not_unknown(self):
        """反过来钉一边:账本还没建立 = 确定今天没抓过,不是「读不出来」。"""
        self.assertEqual(BOOK(0, 0, ''),
                         fetch_rounds.today(T('2026-09-29 09:00')))

    def test_an_unreadable_book_is_unknown_not_no_record(self):
        """账本坏了要说「不可确认」。当成空的后果有两重:面板笃定地说「今天还
        没有抓取」(而今天确实抓过 9 条),下一次开轮次还把它覆盖成「第 1 次」。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', self.caught(
            'batch-a', *(f'source/rss/p{i}.md' for i in range(9))))
        (self.root / fetch_rounds.FILE_NAME).write_text('{坏了', encoding='utf-8')
        self.assertIsNone(fetch_rounds.today(day))
        before = (self.root / fetch_rounds.FILE_NAME).read_bytes()
        fetch_rounds.open_round('batch-b', day)
        # 读不全的文件一个字都不动(认得出来的那部分重写回去 = 悄悄删掉读不懂的)
        self.assertIn(before, (self.root / fetch_rounds.FILE_NAME).read_bytes())
        self.assertIsNone(fetch_rounds.today(day))

    def test_a_half_written_line_does_not_lose_the_confirmed_rounds(self):
        """追加而不是整份重写:断电留下一条半截行,**已经确认的那几轮还在文件里**
        (整份重写的话一次写坏就把当天全带走)。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', self.caught(
            'batch-a', 'source/rss/a.md', 'source/rss/b.md'))
        good = self.lines()
        with open(self.root / fetch_rounds.FILE_NAME, 'a', encoding='utf-8') as fh:
            fh.write('{"key": "batch-b", "da')        # 写到一半断电
        self.assertIsNone(fetch_rounds.today(day))     # 不可确认
        self.assertEqual(good, self.lines()[:len(good)])

    def test_an_old_shape_receipt_line_is_still_a_receipt(self):
        """升级那一下盘上正躺着旧形状的回执行(只有 `paths`、没有身份)。

        **不认它的后果不是少记条数,是凭空多出一次抓取** —— 那一行没有 `day`
        键,于是会被当成一条开轮行,面板上「第 1 次」当场变成「第 2 次」。
        条数照旧折得出来,而每一条的抓取身份是空串 = 说不清(那时真没记过)。
        """
        day = T('2026-09-29 09:00')
        raw = self.root / fetch_rounds.FILE_NAME
        self.root.mkdir(parents=True, exist_ok=True)
        raw.write_text(
            '{"key": "batch-old", "day": "2026-09-29", "at": "09:00", "ex": 1}\n'
            '{"key": "batch-old", "paths": ["source/rss/a.md", "source/rss/b.md"]}\n',
            encoding='utf-8')
        self.assertEqual(BOOK(1, 2, '09:00', fresh=0, unsure=2),
                         fetch_rounds.today(day))

    def _book_line(self, line):
        day = T('2026-09-29 09:00')
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / fetch_rounds.FILE_NAME).write_text(
            '{"key": "batch-a", "day": "2026-09-29", "at": "09:00", "ex": 1}\n'
            + line + '\n', encoding='utf-8')
        return fetch_rounds.today(day)

    def test_a_persisted_field_of_the_wrong_type_is_unknown_not_an_identity(self):
        """盘上那份账本里某个字段**给错了类型**:报「不可确认」,不许读成一个身份。

        `str({"not": "an id"})` 是 `"{'not': 'an id'}"` —— **非空**,于是那一条被
        归进「之前抓到、这一次才补导的」(审查第 2 轮实测到的就是这个);内容身份
        位置上给错类型则连去重都没法做。一行 `paths` 里的坏值同档:路径没有「说不
        清」这个合法值,读不出来就筛不了取材范围,而静默筛掉等于少记一条确定值。

        反过来钉一边有两处:形状旧但字段都读得懂的那种行照旧折得出条数
        (`test_an_old_shape_receipt_line_is_still_a_receipt`),抓取身份位置上的
        `null` 读成「说不清」而不是整份作废(下一条)。
        """
        for bad in ('{"key": "batch-a", "items": [[null, "batch-a"]]}',
                    '{"key": "batch-a", "items": [[7, "batch-a"]]}',
                    '{"key": "batch-a", "items": [["rss:test", {"not": "an id"}]]}',
                    '{"key": "batch-a", "items": [["rss:test", 7]]}',
                    '{"key": "batch-a", "paths": [null]}',
                    '{"key": "batch-a", "paths": [{"p": 1}]}'):
            with self.subTest(line=bad):
                self.assertIsNone(self._book_line(bad))

    def test_a_missing_identity_on_disk_is_unsure_not_a_void_book(self):
        """反过来钉一边:**抓取身份那一格没给值** = 「说不清」,不是整份作废。

        空串本来就是这一格的合法答案(存量内容、手工导入的链接、那一轮真没记过),
        而只有一个元素的旧行读出来也正是空串 —— 把 `null` 判成坏数据会把一天的
        正确条数整份丢掉,那是用另一种静默丢数据换掉这一种。
        """
        self.assertEqual(BOOK(1, 1, '09:00', fresh=0, unsure=1),
                         self._book_line(
                             '{"key": "batch-a", "items": [["rss:test", null]]}'))
        self.assertEqual(BOOK(1, 1, '09:00', fresh=0, unsure=1),
                         self._book_line(
                             '{"key": "batch-a", "items": [["rss:test"]]}'))

    def test_a_write_failure_says_unknown_and_is_retried_later(self):
        """写失败不许假装成功(旧实现吞掉 `OSError`:文章已入库而按钮永久写
        0 条)。这一刻说「不可确认」,下一次账本操作把漏掉的那几行补上去。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        with patch.object(fetch_rounds, '_append', Mock(return_value=False)):
            fetch_rounds.note_new('batch-a', self.caught(
                'batch-a', 'source/rss/a.md', 'source/rss/b.md',
                'source/rss/c.md'))
            self.assertIsNone(fetch_rounds.today(day))         # 不是 0 条
        fetch_rounds.open_round('batch-a', day)                # 磁盘好了
        self.assertEqual(BOOK(1, 3, '09:00'),
                         fetch_rounds.today(day))


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
        """数据目录短暂不可用 → 开轮没写下去 → 恢复后物化交回 2 篇的回执。

        实测的旧末态:`{'n': 1, 'added': 0}`,而且是**确定**的 0 —— 开轮行后来
        补写成功了,清单却已经被丢掉,重读账本也是 0。"""
        import shutil
        shutil.rmtree(self.data)
        self.data.write_text('占住目录位置', encoding='utf-8')     # 目录变成普通文件
        fetch_rounds.open_round('batch-a', self.day)
        self.assertIsNone(fetch_rounds.today(self.day))            # 此刻只能说不可确认
        self.data.unlink()
        self.data.mkdir()
        fetch_rounds.note_new('batch-a', self._paths('a', 'b'))
        self.assertEqual(BOOK(1, 2, '09:00'),
                         fetch_rounds.today(self.day))

    def test_a_retry_after_a_failed_fsync_does_not_double_count(self):
        """`write` 成功、`fsync` 报错:那一行其实已经在文件里,重试再追加一遍。

        实测的旧末态:2 篇报成 `added: 4`。现在回执存**内容身份**、折叠按它取
        并集,所以同一行出现两次和出现一次是同一个结论。"""
        fetch_rounds.open_round('batch-a', self.day)
        with patch.object(os, 'fsync', Mock(side_effect=OSError('fsync 失败'))):
            fetch_rounds.note_new('batch-a', self._paths('a', 'b'))
        book = (self.data / fetch_rounds.FILE_NAME).read_text(encoding='utf-8')
        self.assertIn('rss:a', book)                    # 行其实已经写进去了
        self.assertIsNone(fetch_rounds.today(self.day))  # 但我们不知道,先说不可确认
        fetch_rounds.open_round('batch-a', self.day)     # 顺手重试那一行
        lines = (self.data / fetch_rounds.FILE_NAME).read_text(
            encoding='utf-8').splitlines()
        self.assertEqual(2, sum(1 for l in lines if 'rss:a' in l))
        self.assertEqual(BOOK(1, 2, '09:00'),
                         fetch_rounds.today(self.day))

    def test_rows_that_had_to_be_dropped_keep_the_round_unconfirmed(self):
        """磁盘一直写不进去,攥在手里的行超过上限被挤掉 —— 挤掉的换成「不可确认」。

        旧实现直接切掉尾部:剩下的补写成功之后 `today()` 会重新给出一个**确定**、
        但已经少记的数(这里是 70 篇只剩 63 篇),而面板上一点异样都看不出来。"""
        fetch_rounds.open_round('batch-a', self.day)
        with patch.object(fetch_rounds, '_append', Mock(return_value=False)):
            for i in range(70):
                fetch_rounds.note_new('batch-a', self._paths(f'x{i}'))
        fetch_rounds.open_round('batch-a', self.day)     # 磁盘好了,补写
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
        import kb.pipeline_persist as pp
        fetch_rounds.open_round('old', T('2026-09-20 09:00'))
        slow = pp._write_atomic

        def lag(*a, **k):
            time.sleep(0.4)             # 把整份重写那一下拖长
            return slow(*a, **k)

        with patch.object(fetch_rounds, '_write_atomic', Mock(side_effect=lag)):
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


class FetchFactIsNotReportFactTests(_Base):
    """抓取事实不随日报变 —— 这是整件事的起点。"""

    def setUp(self):
        super().setUp()
        self.db = self.root / 'kb.db'
        connect = store.connect
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        for p in (patch.object(daily, 'VAULT_ROOT', self.vault),
                  patch.object(store, 'connect', lambda *a, **k: connect(self.db)),
                  patch.object(llm_stages, 'stage_model',
                               Mock(return_value=llm.Model('openai', 'm'))),
                  patch.object(daily_carry, 'DATA_DIR', self.root),
                  patch.object(llm, 'chat',
                               Mock(return_value='## 今日要点\n\n- **有事**:说明 [1]'))):
            p.start(); self.addCleanup(p.stop)

    def _doc(self, path, published):
        self.conn.execute(
            'INSERT INTO documents(path,source,title,account,published,url,summary,'
            'indexed_at,extracted_at,promo_level) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (path, 'rss', path, '某号', published, '', '摘要',
             '2026-09-28 09:30', '2026-09-28 09:30', ''))
        self.conn.commit()

    def _two_fetched(self, day):
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', self.caught(
            'batch-a', 'source/rss/a.md', 'source/rss/b.md'))
        self._doc('source/rss/a.md', '2026-09-28 09:00')
        self._doc('source/rss/b.md', '2026-09-28 10:00')
        return daily.pending(first_days=1, now=day)

    def test_generating_a_report_does_not_change_what_was_fetched(self):
        """生成日报不会把「今天抓了 2 条」抹成 0 —— 旧实现正是这么丢的:
        用户同步完看到数字,生成一次之后那个数就没了,第二次同步抓回来的东西
        再也没有地方显示。"""
        day = T('2026-09-29 08:00')
        info = self._two_fetched(day)
        before = fetch_rounds.today(day)
        daily.generate(since=info['since'], until=info['until'])
        self.assertEqual(before, fetch_rounds.today(day))
        self.assertEqual(2, fetch_rounds.today(day)['added'])

    def test_a_failed_ledger_write_cannot_invent_new_content(self):
        """日报文件写好了、取材账本没写上 —— 这是**受支持**的降级终态(那一档
        故意倒向"多报一遍"而不是永久写死)。按钮上那个数来自抓取账本,所以它一个
        字都不受影响;拿 `daily_carried` 去反推"今天这份里有什么"的实现会在这里
        凭空报出新增(审查实测 `next_button_gain=2` 而真实新增 0)。"""
        day = T('2026-09-29 08:00')
        info = self._two_fetched(day)
        # `daily` 是 `from ... import mark_carried` 进来的,所以要打它那一份名字
        with patch.object(daily, 'mark_carried',
                          Mock(side_effect=sqlite3.OperationalError('disk I/O error'))):
            daily.generate(since=info['since'], until=info['until'])
        self.assertEqual(set(), {r[0] for r in self.conn.execute(
            "SELECT path FROM daily_carried WHERE day<>'#seed'")})  # 账本确实空着
        self.assertEqual(BOOK(1, 2, '08:00'),
                         fetch_rounds.today(day))


if __name__ == '__main__':
    unittest.main()
