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
tests/test_fetch_identity.py,写故障之后的重放协议在 tests/test_fetch_recovery.py;
日报取材口径本身在 tests/test_daily_carried.py 和 tests/test_daily_report.py;
前端那几种说法在 tests/daily_probe.mjs。
"""
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from kb import daily, daily_carry, fetch_rounds, llm, llm_stages
from kb import pipeline_batch, store
from kb import pipeline_state as ps
from tests.fetch_rounds_fixture import BOOK, T, _Base


class RoundBookTests(_Base):
    """账本本体:几次、几条、哪一天、故障时说什么。"""

    def test_a_second_sync_the_same_day_is_a_second_round(self):
        """当天两轮:第二轮是「第 2 次」,报的是它自己抓了多少,不是两轮的和。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', self.caught(
            'batch-a', *(f'source/rss/p{i}.md' for i in range(9))))
        fetch_rounds.note_end('batch-a')
        self.assertEqual(BOOK(1, 9, '09:00'),
                         fetch_rounds.today(day))
        later = T('2026-09-29 15:30')
        fetch_rounds.open_round('batch-b', later)
        fetch_rounds.note_new('batch-b', self.caught(
            'batch-b', 'source/rss/q1.md', 'source/rss/q2.md'))
        fetch_rounds.note_end('batch-b')
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
        fetch_rounds.note_end('batch-a')
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
        fetch_rounds.note_end('batch-a')
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
        fetch_rounds.note_end('batch-a')
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
        fetch_rounds.note_end('batch-a')
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
            '{"key": "batch-old", "paths": ["source/rss/a.md", "source/rss/b.md"]}\n'
            '{"key": "batch-old", "end": 1}\n',
            encoding='utf-8')
        self.assertEqual(BOOK(1, 2, '09:00', fresh=0, unsure=2),
                         fetch_rounds.today(day))

    def _book_line(self, line):
        day = T('2026-09-29 09:00')
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / fetch_rounds.FILE_NAME).write_text(
            '{"key": "batch-a", "day": "2026-09-29", "at": "09:00", "ex": 1}\n'
            + line + '\n{"key": "batch-a", "end": 1}\n', encoding='utf-8')
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
            fetch_rounds.note_end('batch-a')
            self.assertIsNone(fetch_rounds.today(day))         # 不是 0 条
        fetch_rounds.open_round('batch-a', day)                # 磁盘好了
        self.assertEqual(BOOK(1, 3, '09:00'),
                         fetch_rounds.today(day))


class CompletenessIsAnExplicitEndTests(_Base):
    """**这一组是第 1 轮审查 F1 的要害**:盘上那几行可以全部合法、也收到过回执,
    却仍然不是「这一轮交完了」。

    两种形态都是实测的,而且都落在**确定**那一档上(所以刷新和重试都纠不回来):

    - 导出阶段进门先落一张空回执,于是「刚开始」和「全部交完」在盘上长得一模
      一样;随后生产者真写出了文章、进程却在交回执前没了(实测 `os._exit(23)`,
      vault 多了 1 篇,重启读出 `{'n':1,'added':0,'partial':False}`)。
    - 多来源里第一个来源交完回执就足以让整轮看着完整,第二个还没开口时进程没了,
      已知的**下界**被当成了总数。

    判据一律落在面板上那个数是「确定 N 条」还是「至少 N 条」,不验标志位。
    """

    def test_a_receipt_alone_does_not_finish_the_round(self):
        """开轮 + 导出阶段进门那张空回执 = 「刚开始」,不是「确定 0 条」;
        **反过来钉一边**:逐来源循环走完之后记上那一行,它才是确定的 0 条。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', ())
        self.assertEqual(BOOK(1, 0, '09:00', True),
                         fetch_rounds.today(day))
        fetch_rounds.note_end('batch-a')
        self.assertEqual(BOOK(1, 0, '09:00'),
                         fetch_rounds.today(day))

    def test_a_round_that_died_between_two_sources_reports_a_lower_bound(self):
        """勾了两个来源,第一个交回 1 篇、第二个还没开口时进程没了 —— 1 是
        **下界**(面板写「至少 +1 条」),不是确定的总数。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a', ())
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        self.assertEqual(BOOK(1, 1, '09:00', True),
                         fetch_rounds.today(day))

    def test_a_complete_round_stays_complete_across_a_rotation(self):
        """轮换是整份重写,它得把「交完了」那一位抄过去 —— 漏了它,一轮本来完整
        的抓取过一次台面下的维护就变成「至少 N 条」,而用户只会看到数字自己变了。

        顺带钉住轮换本身确实发生了(过期那一轮被清出去了)。"""
        fetch_rounds.open_round('old', T('2026-09-28 09:00'))
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a')
        fetch_rounds.open_round('batch-c', T('2026-09-30 10:00'))   # 这一下触发轮换
        self.assertNotIn('"old"', (self.root / fetch_rounds.FILE_NAME)
                         .read_text(encoding='utf-8'))
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(day))

    def test_a_round_that_lost_the_lock_is_not_rotated_away(self):
        """**第 1 轮审查实测的那一条**:取不到旁挂锁时还继续做覆盖式轮换,会把
        另一个进程**已经确认过的整轮**抹掉 —— 剩下的 JSON 行行合法,`today()`
        发现不了,结论从 `added=1` 变成确定的 0 条。

        失锁时:账本一个字都不许被重写,展示降级成「读不出来」;锁一好,先前
        确认过的那一轮原样还在。"""
        fetch_rounds.open_round('old', T('2026-09-28 09:00'))
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day)
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a')
        before = (self.root / fetch_rounds.FILE_NAME).read_text(encoding='utf-8')
        (self.root / fetch_rounds.LOCK_NAME).unlink()
        (self.root / fetch_rounds.LOCK_NAME).mkdir()        # 旁挂锁取不到了
        fetch_rounds.open_round('batch-c', T('2026-09-30 10:00'))
        now = (self.root / fetch_rounds.FILE_NAME).read_text(encoding='utf-8')
        self.assertTrue(now.startswith(before))             # 没有被整份重写
        self.assertIsNone(fetch_rounds.today(day))           # 这期间只能说读不出来
        (self.root / fetch_rounds.LOCK_NAME).rmdir()
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(day))


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
        fetch_rounds.note_end('batch-a')          # 物化这一段明确走完了
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


class EvidenceOutlivesABadReadTests(_Base):
    """**这一组是第 2 轮审查 F1 的第一条**:账本读不出来那一下,已经交到手里的
    回执不许就地丢掉。

    实测形态:采集器真写出一篇 md,只在**随后那一次**读账上注入一次 `OSError`,
    之后读取完全恢复 —— 旧实现在 `_mark` 里直接 return,那篇文章永久不进账,而
    结束行照样写成功,面板笃定地写「第 2 次抓取 · 0 条」;刷新、重导都纠不回来
    (重导时它已经不是新文件,`fresh=False`)。

    判据分两档,都落在面板那个数上:**自己开的那一轮**要真把那一行记下来(数
    对得上);**别人开的那一轮**至少不许在读账恢复之后解除未知(宁可说「至少」)。
    """

    def test_a_receipt_that_arrived_during_a_read_failure_is_still_counted(self):
        """这一轮是本进程开的,所以"它存不存在"不必问账本 —— 那一行照旧追加。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        with self.ledger_read_fails():
            fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(day))

    def test_a_line_that_was_lost_keeps_the_round_a_lower_bound(self):
        """换成**别人**开的那一轮(上一次运行、另一个进程):读账失败时连
        `_is_open` 都判不了,那一行只能丢 —— 但「这一轮缺了一行」要留住,
        由结束行带着落盘,**重启之后重读盘上那几行,结论一个字不变**。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        fetch_rounds._opened.clear()            # 换一个进程:它没开过这一轮
        with self.ledger_read_fails():
            fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual(BOOK(1, 0, '09:00', True),
                         fetch_rounds.today(day))
        fetch_rounds._gaps.clear()              # 再换一个进程:只剩盘上那几行
        self.assertEqual(BOOK(1, 0, '09:00', True),
                         fetch_rounds.today(day))

    def test_a_read_failure_does_not_invent_a_round_nobody_opened(self):
        """**反过来钉一边**:读不出账、这一轮又压根没开过(只勾了索引的那种运行
        在喊成果)—— 那就什么都不记。往一份读不懂的账本里追加一条对不上轮次的
        孤行,会让它从此永久「读不出来」。"""
        day = T('2026-09-29 09:00')
        with self.ledger_read_fails():
            fetch_rounds.note_new('ghost',
                              self.caught('ghost', 'source/rss/a.md'))
        self.assertEqual([], self.lines())
        self.assertEqual(BOOK(0, 0, ''),
                         fetch_rounds.today(day))


class CompletenessIsTheWholeBatchTests(_Base):
    """**这一组是第 2 轮审查 F1 的第二条**:「交完了」是**参与的子轮全交齐**,
    不是哪一个子轮说了算。

    一轮日报里每个来源各跑一次导出阶段、共用一个业务批次身份(`daily_run` 那个
    逐来源循环)。实测形态:第一个子轮正常收尾,第二个子轮在**另一个进程**里写出
    第二篇之后 `os._exit(23)` —— 盘上第一个子轮的结束行让整轮报成**确定的 1 条**,
    而 vault 里躺着两篇。上一组(`CompletenessIsAnExplicitEndTests`)只证明了
    **一次调用**收尾前没有结束行,证明不了**多次调用共用一个 key** 这一档。
    """

    def test_a_later_sub_round_that_died_turns_the_round_into_a_lower_bound(self):
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(day))         # 第一个子轮:确定 1 条
        fetch_rounds.open_round('batch-a', day, seg='run-2')    # 第二个来源开工
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/b.md'))
        self.assertEqual(BOOK(1, 2, '09:00', True),
                         fetch_rounds.today(day))         # 它没交完 → 至少 2 条
        fetch_rounds.note_end('batch-a', 'run-2')
        self.assertEqual(BOOK(1, 2, '09:00'),
                         fetch_rounds.today(day))         # **反过来钉一边**:交齐了才确定

    def test_an_export_only_retry_does_not_reopen_the_question(self):
        """**反过来钉一边**:一轮交齐之后,只勾了导出的那种重试又交了一条结束行
        —— 它压根没开过轮、没有自己的登记行,结论不许因此退回「至少」。结束行
        **同时**算"这个子轮登记过",正是为了这一档;少了它,那一轮永久「至少」。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a', 'run-1')
        fetch_rounds.note_new('batch-a', ())                # 重试那一轮的空回执
        fetch_rounds.note_end('batch-a', 'run-7')
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(day))

    def test_a_ledger_written_by_the_old_code_keeps_its_verdict(self):
        """部署那一刻盘上躺着的是**老代码写的账本**:一条子轮行都没有。那几轮的
        结论一个字都不许变 —— 有结束行的照旧算确定(结束行自己就算一个子轮),
        没有的算下界(新代码确实证明不了它们完整,这比笃定地报一个数安全)。

        这一条钉的是 `today()` 里 `segs or not ex` 那半句:去掉它,老账本里一轮
        没交完的抓取会当场变成「确定 N 条」。"""
        day = T('2026-09-29 09:00')
        book = self.root / fetch_rounds.FILE_NAME
        opened = '{"day": "2026-09-29", "key": "old-open", "at": "2026-09-29 09:00", "ex": 1}\n'
        caught = '{"key": "old-open", "paths": ["source/rss/b.md"]}\n'
        book.write_text(opened + caught, encoding='utf-8')
        # 老账本的回执行没有抓取身份那一格 —— 条数照旧折得出来,分解是「说不清」
        self.assertEqual(BOOK(1, 1, '09:00', True, fresh=0, unsure=1),
                         fetch_rounds.today(day))
        book.write_text(opened + caught + '{"key": "old-open", "end": 1}\n',
                        encoding='utf-8')
        self.assertEqual(BOOK(1, 1, '09:00', fresh=0, unsure=1),
                         fetch_rounds.today(day))

    def test_an_unfinished_sub_round_survives_a_rotation(self):
        """轮换是整份重写,**还没交完的那几个子轮也要抄过去**。漏了它们,一轮
        不完整的抓取过一次台面下的维护就变成「确定 N 条」—— 那比反过来漏掉结束行
        更坏:把不知道的说成知道的。顺带钉住轮换确实发生了。"""
        fetch_rounds.open_round('old', T('2026-09-28 09:00'))
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a', 'run-1')
        fetch_rounds.open_round('batch-a', day, seg='run-2')    # 第二个来源开工
        fetch_rounds.open_round('batch-c', T('2026-09-30 10:00'))   # 触发轮换
        self.assertNotIn('"old"', (self.root / fetch_rounds.FILE_NAME)
                         .read_text(encoding='utf-8'))
        self.assertEqual(BOOK(1, 1, '09:00', True),
                         fetch_rounds.today(day))


class OneWriteProtocolForEveryWriterTests(_Base):
    """**这一组是第 2 轮审查 F1 的第三条**:账本的每一次写都在那把文件锁里。

    上一版的降级是「失锁只许追加、不许轮换」—— 它只挡住了"失锁者自己轮换",挡不住
    反过来那一半:**另一个持锁的进程**按它读到的旧快照整份重写,把失锁者已经追加
    成功的整轮抹掉(实测:失锁进程把开轮、回执、结束行全部追加成功、`_unsaved`
    空,随后那一整轮消失,`today()` 自称确定、报 0 条,而它写出的
    `source/rss/ack.md` 还在 vault 里)。

    所以失锁时一行都不写,攥在手里、展示降级成「读不出来」;抓取那一侧照旧不拦
    (这一点由 `tests/test_fetch_receipt.py` 的
    `ABrokenLedgerMustNotBlockTheCaptureTests` 钉住)。
    """

    def test_an_unlocked_writer_appends_nothing(self):
        """失锁期间:盘上一行都不多,`today()` 答「读不出来」。**反过来钉一边**:
        锁一回来,攥着的那几行照旧落盘,数一条不少 —— 这是降级,不是丢数据。"""
        day = T('2026-09-29 09:00')
        (self.root / fetch_rounds.LOCK_NAME).mkdir(parents=True)    # 旁挂锁取不到
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        fetch_rounds.note_new('batch-a',
                              self.caught('batch-a', 'source/rss/a.md'))
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual([], self.lines())          # 没有任何没人保护的追加
        self.assertIsNone(fetch_rounds.today(day))  # 也不许给出确定结论
        (self.root / fetch_rounds.LOCK_NAME).rmdir()
        self.assertEqual(BOOK(1, 1, '09:00'),
                         fetch_rounds.today(day))


if __name__ == '__main__':
    unittest.main()
