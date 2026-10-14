# -*- coding: utf-8 -*-
"""调度账本:定时这一轮的 **attempt / 退避 / 补跑次数 / 今天到此为止** 四格。

**从 `tests/test_schedule_unreadable.py` 拆出来只因为 700 行上限**,摆法原样搬过来
(临时设置文件 + 钉死的时钟 + 接住"留原因"那句话)。那边钉的是「配置读不出来 ≠ 用户
关掉了开关」和写失败不动上一份快照;这边钉的是**盘上那几格写不进去/写晚了的时候,
定时还该不该起**。两组共用的事故形态写在 `docs/decisions/sync-schedule.md`。
"""
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from kb import daily_run, pipeline
from kb.config import ScheduleUnreadable


class SameDayRerunTests(unittest.TestCase):
    """**不许用"配置里没写上当天标记"证明"当天还没跑过"。**

    两条定时都是"先起跑、再回写当天标记"。回写那一步失败(配置一时读不出、满盘、
    进程在写之前被杀)之后,只认盘上那一格的话:等到下一跳,同一天又会自动跑一整
    轮 —— 日报那一轮要再花一次模型钱,页头那一轮要再跑一遍六段(含 extract)。
    没有重启、也没有用户点重试。所以进程里另记一格,**而且它只记"今天到此为止",
    不记"今天起过"**(起过 ≠ 出报:一次模型 502 之后同一天还要补几次)。
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'cfg.json'
        for p in (patch.object(daily_run, 'SETTINGS_FILE', self.path),
                  patch.object(pipeline, '_SCHED_FILE', self.path),
                  patch.object(daily_run, '_log'), patch.object(pipeline, '_log'),
                  patch('kb.config._announced', {}),
                  patch.object(daily_run, '_report_done', Mock(return_value=False))):
            p.start(); self.addCleanup(p.stop)
        self.said: list[str] = []
        for module in (daily_run, pipeline):      # 接住"留原因"那句话,顺带别刷屏
            p = patch.object(module, 'announce_config_issue',
                             lambda what, reason: self.said.append(f'{what}:{reason}'))
            p.start(); self.addCleanup(p.stop)
        self.clock = Mock(wraps=datetime)
        # 保存那一刻要**早于**定时时刻:保存只安排下一次、绝不补跑今天已经过去的
        # 时刻,在 07:30 存一条 07:30 的定时会被排到明天,"到点了"压根不成立。
        self.clock.now.return_value = datetime(2026, 10, 15, 7, 0)
        for p in (patch.object(daily_run, 'datetime', self.clock),
                  patch.object(pipeline, 'datetime', self.clock)):
            p.start(); self.addCleanup(p.stop)

    def at(self, hour: int, minute: int, day: int = 15) -> None:
        self.clock.now.return_value = datetime(2026, 10, day, hour, minute)

    def test_the_daily_round_does_not_run_twice_when_the_mark_is_lost(self):
        daily_run.save_settings(True, '07:30', ['rss'], True, 1)
        self.at(7, 30)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertTrue(daily_run.tick())
            start.assert_called_once()
            # 收尾那一步读不出配置(权限/介质),当天标记没能写上
            with patch.object(daily_run, '_raw_settings',
                              Mock(side_effect=ScheduleUnreadable('一时读不出'))):
                daily_run._finish_schedule('2026-10-15', retry=False)
            self.assertEqual('', daily_run.load_settings()['last_fired'])
            self.assertTrue(any('一时读不出' in line for line in self.said), self.said)
            # 盘上没有标记,但今天已经跑完了 —— 下一跳不许再起一轮
            self.at(8, 5)
            self.assertFalse(daily_run.tick())
            self.assertEqual(1, start.call_count)
            # 反过来钉一边:**第二天照常跑**(别把定时永久按住)
            self.at(7, 30, day=16)
            self.assertTrue(daily_run.tick())

    def test_a_transient_failure_still_gets_its_same_day_retries(self):
        # 钉边:只记"到此为止"而不是"起过一轮" —— 失败重试那条路不许被挡掉
        daily_run.save_settings(True, '07:30', ['rss'], True, 1)
        self.at(7, 30)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertTrue(daily_run.tick())
            daily_run._finish_schedule('2026-10-15', retry=True)
            self.at(8, 5)
            self.assertTrue(daily_run.tick())
            self.assertEqual(2, start.call_count)

    def test_the_head_round_does_not_run_twice_when_the_mark_is_lost(self):
        pipeline.save_schedule(True, '07:30', ['rss'])
        self.at(7, 30)
        with patch.object(pipeline, 'start_pipeline', Mock(return_value=True)) as start, \
                patch('kb.config.os.replace', Mock(side_effect=OSError('配额不够'))):
            self.assertTrue(pipeline._fire_head_schedule())   # 起了,但标记写不进去
            start.assert_called_once()
        self.assertEqual('', pipeline.load_schedule()['last_fired'])
        with patch.object(pipeline, 'start_pipeline', Mock(return_value=True)) as start:
            self.at(8, 5)
            self.assertFalse(pipeline._fire_head_schedule())
            start.assert_not_called()
            self.at(7, 30, day=16)
            self.assertTrue(pipeline._fire_head_schedule())   # 第二天照常

    def test_todays_report_on_disk_also_counts_as_done(self):
        """重启之后进程里那一格空了,**已经落盘的产物**仍然作数 —— 它比配置里那个
        标记更硬(标记可能根本没写上)。查不出来时说"不知道",照常跑。"""
        daily_run.save_settings(True, '07:30', ['rss'], True, 1)
        self.at(7, 30)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start, \
                patch.object(daily_run, '_report_done', Mock(return_value=True)):
            self.assertFalse(daily_run.tick())
            start.assert_not_called()
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start, \
                patch.object(daily_run, '_report_done', Mock(side_effect=OSError)):
            with self.assertRaises(OSError):
                daily_run.tick()      # 这一层不吞:吞了就看不出"产物查不了"
            start.assert_not_called()


class RetryBudgetTests(unittest.TestCase):
    """**不许用「回写没成功」证明「没到次数上限 / 已经可以再试了」。**

    日报失败之后的补跑有两道闸:退避(`RETRY_AFTER`)和次数上限(`MAX_RETRIES`)。
    两道都持久化在盘上那份配置里(`next_run` / `fail_count`),而那一次回写失败
    (满盘、配置一时读不出)之后只认盘上那份的话:退避丢了 —— 每一跳(30 秒)都判
    到期;次数也丢了 —— 每次失败都算第一次。实测形态是**失败的日报每 30 秒重起
    一轮,三分钟七轮**,每轮各花一次模型钱。

    这两条跟上面 `SameDayRerunTests` 钉的是**同一个不变量的另外两格**:那边钉
    done(今天跑完了别再起),这边钉 attempt / 退避 / 次数(没跑完也别每 30 秒起)。
    判据一律落在「下一跳还起不起」和「总共起了几轮」,不看标志位。
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'cfg.json'
        for p in (patch.object(daily_run, 'SETTINGS_FILE', self.path),
                  patch.object(daily_run, '_log'), patch('kb.config._announced', {}),
                  patch.object(daily_run, '_report_done', Mock(return_value=False))):
            p.start(); self.addCleanup(p.stop)
        self.said: list[str] = []
        p = patch.object(daily_run, 'announce_config_issue',
                         lambda what, reason: self.said.append(f'{what}:{reason}'))
        p.start(); self.addCleanup(p.stop)
        self.clock = Mock(wraps=datetime)
        self.clock.now.return_value = datetime(2026, 10, 15, 7, 0)   # 保存早于定时时刻
        p = patch.object(daily_run, 'datetime', self.clock)
        p.start(); self.addCleanup(p.stop)

    def at(self, hour: int, minute: int, day: int = 15) -> None:
        self.clock.now.return_value = datetime(2026, 10, day, hour, minute)

    def test_a_failed_round_keeps_its_backoff_and_budget_when_the_write_fails(self):
        """生成临时失败,而且这一轮的收尾状态**一个字也没能写进盘**。"""
        daily_run.save_settings(True, '07:30', ['rss'], True, 1)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start, \
                patch('kb.config.os.replace', Mock(side_effect=OSError('满盘'))):
            self.at(7, 30)
            self.assertTrue(daily_run.tick())                 # 到点,起了第一轮
            daily_run._finish_schedule('2026-10-15', retry=True)
            self.assertEqual(0, daily_run.load_settings()['fail_count'])   # 盘上没记上
            self.at(7, 31)
            self.assertFalse(daily_run.tick())                # 退避还在:30 秒后不许再起
            self.at(8, 5)
            self.assertTrue(daily_run.tick())                 # 过了退避才补第二次
            daily_run._finish_schedule('2026-10-15', retry=True)
            self.at(8, 40)
            self.assertTrue(daily_run.tick())
            daily_run._finish_schedule('2026-10-15', retry=True)
            self.at(9, 20)
            self.assertFalse(daily_run.tick())                # 补满次数,今天到此为止
            self.at(23, 59)
            self.assertFalse(daily_run.tick())
            self.assertEqual(daily_run.MAX_RETRIES + 1, start.call_count)
            # 盘上那份从头到尾一个字没改(原子替换守住上一份,见 kb.config)
            self.assertEqual((0, '', '2026-10-15T07:30'),
                             tuple(daily_run.load_settings()[k]
                                   for k in ('fail_count', 'last_fired', 'next_run')))
        # 反过来钉一边:**第二天照常跑**(昨天补满的次数不许把今天按住)
        self.at(7, 30, day=16)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertTrue(daily_run.tick())
            start.assert_called_once()

    def test_an_unaccepted_start_does_not_spend_the_backoff(self):
        """**退避是「受理过一轮」的记账,不是「试过一下」的记账。**

        起跑前还要再读一次设置(可能一时读不出),`start` 自己也会拒(别人占着全局
        管道、一个来源都没剩)。在它们之前就把下一次推后 30 分钟的话,一次瞬时失败
        就把今天的日报推迟半小时 —— 而本来下一跳(30 秒后)就能恢复。
        """
        daily_run.save_settings(True, '07:30', ['rss'], True, 1)
        # ① `start` 拒了:这一跳一轮都没起,退避不许被消耗
        self.at(7, 30)
        with patch.object(daily_run, 'start', Mock(return_value=False)) as start:
            self.assertFalse(daily_run.tick())
            start.assert_called_once()
        self.assertEqual('2026-10-15T07:30', daily_run.load_settings()['next_run'])
        # ② 起跑前那次读设置一时读不出:同样不许消耗,而且要留原因
        self.at(7, 31)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start, \
                patch.object(daily_run, 'load_settings',
                             Mock(side_effect=ScheduleUnreadable('一时读不出'))):
            self.assertFalse(daily_run.tick())
            start.assert_not_called()
        self.assertTrue(any('一时读不出' in line for line in self.said), self.said)
        self.assertEqual('2026-10-15T07:30', daily_run.load_settings()['next_run'])
        # ③ 下一跳恢复了就该照常起 —— 前两次都没把今天这一轮推到半小时后
        self.at(7, 32)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertTrue(daily_run.tick())
            start.assert_called_once()
        self.assertEqual('2026-10-15T08:02', daily_run.load_settings()['next_run'])

    def test_an_earlier_attempt_cannot_overwrite_a_newer_finish(self):
        """**更旧的起跑记账不许盖掉更新的收尾计划。**

        第 5 轮用真实工作线程确定性复现的那条:tick 受理 07:30 那一轮,算好「下一次
        = 今天 08:00」;写进去之前工作线程先收尾,在锁里把配置写成「明天 07:30」;
        这一步再拿着那份更旧的参数写一遍 —— 盘上倒退成今天 08:00。当天由账本的 done
        挡住,表面正常,**第二天 00:05 那一跳就判到期**:凌晨跑出一份日报,到了 07:30
        反而因为"今天已经跑过"不再生成。原子替换只保证一份文件不会写一半,**保证不了
        两次更新的先后**。

        判据落在后果上:收尾之后盘上是「明天那一格」,而且第二天凌晨那一跳不许起。
        摆法照产品的形状 —— `start` 起一条**真实线程**去做收尾,那条线程先等到"起跑
        记账已经算好参数、还没写进去"那一刻(按住产品自己那次回写),然后才动手。
        """
        daily_run.save_settings(True, '07:30', ['rss'], True, 1)
        at_attempt, finished = threading.Event(), threading.Event()
        real_write = daily_run._write_settings

        def pausing_write(**changes):
            # 只按住"起跑记账"那一次:参数已经算好,还没写进去
            if changes.get('next_run') == '2026-10-15T08:00' and not at_attempt.is_set():
                at_attempt.set()
                finished.wait(1.0)      # 给收尾一个真实的机会插到这中间
            real_write(**changes)

        worker: list[threading.Thread] = []

        def start_round(**kwargs):
            def run():
                at_attempt.wait(2.0)
                daily_run._finish_schedule(kwargs['sched_day'], retry=False)
                finished.set()
            thread = threading.Thread(target=run)
            worker.append(thread)
            thread.start()
            return True

        self.at(7, 30)
        with patch.object(daily_run, '_write_settings', pausing_write), \
                patch.object(daily_run, 'start', Mock(side_effect=start_round)):
            self.assertTrue(daily_run.tick())
        worker[0].join(5)
        self.assertFalse(worker[0].is_alive(), '收尾那条线程没跑完')
        s = daily_run.load_settings()
        self.assertEqual(('2026-10-15', '2026-10-16T07:30'), (s['last_fired'], s['next_run']))
        # 第二天凌晨那一跳不许起:只有盘上还留着那份过期的旧计划才会判到期
        self.at(0, 5, day=16)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertFalse(daily_run.tick())
            start.assert_not_called()
        # 反过来钉一边:**第二天到点了照常起**(别把定时按死)
        self.at(7, 30, day=16)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertTrue(daily_run.tick())
            start.assert_called_once()


    def test_recording_an_attempt_is_itself_one_critical_section(self):
        """上一条钉的是 tick 那一层(受理和记账同一个临界区);这一条钉**这个函数
        自己** —— 两道保护各自够用,所以只去掉一道时上一条是绿的(如实记着),
        各自的红得各自钉。

        直接调 `_mark_attempt`(不经过 tick),同时让一条真实线程在"参数算好、还没
        写进去"那一刻收尾:判断、账本、算参数、写盘必须是同一个临界区,否则更旧的
        计划会盖掉收尾写的「明天那一格」。
        """
        daily_run.save_settings(True, '07:30', ['rss'], True, 1)
        self.at(7, 30)
        with daily_run._lock:                      # 摆出 tick 判到期那一刻的账本
            daily_run._book.book('2026-10-15')
        at_attempt, finished = threading.Event(), threading.Event()
        real_write = daily_run._write_settings

        def pausing_write(**changes):
            if changes.get('next_run') == '2026-10-15T08:00' and not at_attempt.is_set():
                at_attempt.set()
                finished.wait(1.0)
            real_write(**changes)

        def finish_round():
            at_attempt.wait(2.0)
            daily_run._finish_schedule('2026-10-15', retry=False)
            finished.set()

        worker = threading.Thread(target=finish_round)
        worker.start()
        with patch.object(daily_run, '_write_settings', pausing_write):
            daily_run._mark_attempt(datetime(2026, 10, 15, 7, 30), '2026-10-15')
        worker.join(5)
        self.assertFalse(worker.is_alive(), '收尾那条线程没跑完')
        s = daily_run.load_settings()
        self.assertEqual(('2026-10-15', '2026-10-16T07:30'), (s['last_fired'], s['next_run']))

    def test_writing_schedule_state_outside_the_lock_is_refused(self):
        """那道持锁守卫自己的红:**不在锁里就不许改定时状态**。

        它挡的是以后有人照着老样子在锁外算好参数再写 —— 那正是第 5 轮那条竞态的
        形状,而它在正常回归里一条都不红(两道保护都在时谁也走不到那一步)。
        """
        daily_run.save_settings(True, '07:30', ['rss'], True, 1)
        with self.assertRaises(RuntimeError) as caught:
            daily_run._write_settings(next_run='2026-10-15T08:00')
        self.assertIn('_lock', str(caught.exception))
        # 反过来钉一边:拿着锁调它照旧写得进去
        with daily_run._lock:
            daily_run._write_settings(next_run='2026-10-15T08:00')
        self.assertEqual('2026-10-15T08:00', daily_run.load_settings()['next_run'])


    def test_a_round_that_already_finished_is_not_pushed_back(self):
        """起跑那一轮快到在同一毫秒里就收尾了(不同步、当场报错):**收尾写的那份
        才是最新的**,记 attempt 不许把「明天那一格」盖成今天 + 30 分钟 —— 盖回去
        的话第二天那一跳在 00:00 就判到期,日报提前一整天的量跑出来。"""
        daily_run.save_settings(True, '07:30', ['rss'], True, 1)
        self.at(7, 30)
        # start 里"这一轮当场跑完并收尾"(产品里是另一个线程,这里就地做掉)
        def run_and_finish(**kwargs):
            daily_run._finish_schedule(kwargs['sched_day'], retry=False)
            return True
        with patch.object(daily_run, 'start', Mock(side_effect=run_and_finish)):
            self.assertTrue(daily_run.tick())
        s = daily_run.load_settings()
        self.assertEqual(('2026-10-15', '2026-10-16T07:30'), (s['last_fired'], s['next_run']))
        # 第二天那一跳还在定时时刻才起
        self.at(0, 5, day=16)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertFalse(daily_run.tick())
        self.at(7, 30, day=16)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertTrue(daily_run.tick())


    def test_the_retry_budget_also_advances_while_the_config_cannot_be_read(self):
        """收尾那一刻配置读不出来:同样不许把这回失败当成第一次。"""
        daily_run.save_settings(True, '07:30', ['rss'], True, 1)
        unreadable = Mock(side_effect=ScheduleUnreadable('一时读不出'))
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            for when, expect in (((7, 30), True), ((7, 31), False), ((8, 5), True),
                                 ((8, 40), True), ((9, 20), False), ((23, 59), False)):
                self.at(*when)
                self.assertEqual(expect, daily_run.tick(), when)
                if expect:
                    with patch.object(daily_run, '_raw_settings', unreadable):
                        daily_run._finish_schedule('2026-10-15', retry=True)
            self.assertEqual(daily_run.MAX_RETRIES + 1, start.call_count)
        self.assertTrue(any('一时读不出' in line for line in self.said), self.said)


if __name__ == '__main__':
    unittest.main()
