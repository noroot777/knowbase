# -*- coding: utf-8 -*-
"""调度账本:定时这一轮的 **attempt / 退避 / 补跑次数 / 今天到此为止** 四格。

**从 `tests/test_schedule_unreadable.py` 拆出来只因为 700 行上限**,摆法原样搬过来
(临时设置文件 + 钉死的时钟 + 接住"留原因"那句话)。那边钉的是「配置读不出来 ≠ 用户
关掉了开关」和写失败不动上一份快照;这边钉的是**盘上那几格写不进去/写晚了的时候,
定时还该不该起**。两组共用的事故形态写在 `docs/decisions/sync-schedule.md`。
"""
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from kb import daily, daily_run
from kb.config import ScheduleUnreadable


class SameDayRerunTests(unittest.TestCase):
    """**不许用"配置里没写上当天标记"证明"当天还没跑过"。**

    定时是"先起跑、再回写当天标记"。回写那一步失败(配置一时读不出、满盘、进程在
    写之前被杀)之后,只认盘上那一格的话:等到下一跳,同一天又会自动跑一整轮 ——
    那是再花一次模型钱。没有重启、也没有用户点重试。所以进程里另记一格,**而且它
    只记"今天到此为止",不记"今天起过"**(起过 ≠ 出报:一次模型 502 之后同一天
    还要补几次)。
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'cfg.json'
        for p in (patch.object(daily_run, 'SETTINGS_FILE', self.path),
                  patch.object(daily_run, '_log'),
                  patch('kb.config._announced', {}),
                  patch.object(daily_run, '_report_done', Mock(return_value=False))):
            p.start(); self.addCleanup(p.stop)
        self.said: list[str] = []
        # 接住"留原因"那句话,顺带别刷屏
        p = patch.object(daily_run, 'announce_config_issue',
                         lambda what, reason: self.said.append(f'{what}:{reason}'))
        p.start(); self.addCleanup(p.stop)
        self.clock = Mock(wraps=datetime)
        # 保存那一刻要**早于**定时时刻:保存只安排下一次、绝不补跑今天已经过去的
        # 时刻,在 07:30 存一条 07:30 的定时会被排到明天,"到点了"压根不成立。
        self.clock.now.return_value = datetime(2026, 10, 15, 7, 0)
        p = patch.object(daily_run, 'datetime', self.clock)
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


class ReleaseBoundaryTests(unittest.TestCase):
    """**一轮的终态没提交之前,不许对调度器发布"可以受理下一轮"。**

    上面那几组停在 `tick` / `_finish_schedule` 这两头,`start` 都是打的桩 —— 它们
    钉不住**生产方**那一侧:工作线程原来是先在锁里发布空闲(`running=False`)、放开
    锁之后才 `_finish_schedule`。调度线程挤进中间那一刻时"空闲"是真的,而"今天到此
    为止 / 必须等到重试时刻"还没提交,于是同一天又受理一轮(第 6 轮审查用真实线程
    两种异常各复现一次,生成各被调用 2 次)。一轮跑过 30 分钟是正常可达的 ——
    抓取预算本来就是 3 小时。

    所以这一组走**真实的 `start` → 真实工作线程 → 真实 `tick`**,只把 `daily.generate`
    换成对应的异常桩,判据落在**生成一共被调用了几次**和**盘上那几格**上。
    摆法:按住产品自己那次 `_finish_schedule`(刚进来、终态还没落下),这期间让另一条
    线程去跑真实 `tick`;收尾那边**不等它跑完**(等的话修好之后它正堵在同一把锁上,
    验证脚本自己就死锁了),只给它一个有界的窗口。
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'cfg.json'
        for p in (patch.object(daily_run, 'SETTINGS_FILE', self.path),
                  patch.object(daily_run, '_log'), patch('kb.config._announced', {}),
                  patch.object(daily_run, '_save_last_run', Mock()),
                  patch.object(daily_run, '_channel_labels',
                               Mock(return_value={'rss': 'RSS 订阅'})),
                  patch.object(daily_run, '_report_done', Mock(return_value=False))):
            p.start(); self.addCleanup(p.stop)
        self.said: list[str] = []
        p = patch.object(daily_run, 'announce_config_issue',
                         lambda what, reason: self.said.append(f'{what}:{reason}'))
        p.start(); self.addCleanup(p.stop)
        self.clock = Mock(wraps=datetime)
        self.clock.now.return_value = datetime(2026, 10, 15, 7, 0)
        p = patch.object(daily_run, 'datetime', self.clock)
        p.start(); self.addCleanup(p.stop)
        self.addCleanup(self._let_the_round_settle)

    def at(self, hour: int, minute: int, day: int = 15) -> None:
        self.clock.now.return_value = datetime(2026, 10, day, hour, minute)

    def _let_the_round_settle(self, timeout: float = 5.0) -> None:
        """真实工作线程跑完(状态回到空闲)。它是 daemon,拿不到 handle,只能等后果。"""
        deadline = time.monotonic() + timeout
        while daily_run.is_running() and time.monotonic() < deadline:
            time.sleep(0.02)

    def _one_round_with(self, failure: BaseException, retry_expected: bool) -> dict:
        """跑完整一轮:真实受理 → 真实工作线程 → 收尾窗口里插一次真实 `tick`。

        返回 `{'calls': 生成被调用几次, 'accepted': 窗口里那次 tick 的返回值}`。
        """
        daily_run.save_settings(True, '07:30', ['rss'], False, 1)   # sync=False:不碰管道
        calls: list[dict] = []
        at_finish = threading.Event()
        probe_started, probe_returned = threading.Event(), threading.Event()
        accepted: list[bool] = []
        real_finish = daily_run._finish_schedule

        def generate(**kwargs):
            calls.append(kwargs)
            raise failure

        def pausing_finish(day, retry):
            self.assertEqual(retry_expected, retry)
            at_finish.set()                 # 刚进收尾:终态一格都还没提交
            probe_started.wait(3.0)
            probe_returned.wait(2.0)        # 有界:修好之后那次 tick 正堵在这把锁上
            real_finish(day, retry)

        def probe():
            at_finish.wait(5.0)
            self.at(8, 5)                   # 这一轮跑了 35 分钟,起跑那个退避早过了
            probe_started.set()
            accepted.append(daily_run.tick())
            probe_returned.set()

        watcher = threading.Thread(target=probe)
        watcher.start()
        self.at(7, 30)
        with patch.object(daily_run, '_finish_schedule', pausing_finish), \
                patch.object(daily, 'generate', generate):
            self.assertTrue(daily_run.tick(), '到点了却没受理')
            self._let_the_round_settle()
        watcher.join(5)
        self.assertFalse(watcher.is_alive(), '那条探针线程没跑完')
        return {'calls': len(calls), 'accepted': accepted}

    def test_no_new_content_does_not_let_the_same_day_start_again(self):
        """"这段时间没有新内容"(`LookupError`):当天到此为止,窗口里不许再受理。

        这一条最要命的地方是**没有补挡**:盘上既没有 last_fired、也没有当天的日报
        文件给 `_report_done` 兜住,所以窗口一开就是真的又跑一轮。
        """
        out = self._one_round_with(LookupError('这段时间没有新内容'), retry_expected=False)
        self.assertEqual(1, out['calls'], '同一天又生成了一次')
        self.assertEqual([False], out['accepted'], '收尾还没提交就受理了新一轮')
        s = daily_run.load_settings()
        self.assertEqual(('2026-10-15', '2026-10-16T07:30'), (s['last_fired'], s['next_run']))
        # 反过来钉一边:**第二天到点照常跑**(别把定时按死)
        self.at(7, 30, day=16)
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.assertTrue(daily_run.tick())
            start.assert_called_once()

    def test_a_transient_failure_backs_off_from_when_it_ended(self):
        """临时失败:退避从**收尾时刻**再算 30 分钟,窗口里不许提前重试。

        提前那一次还会白消耗一格补跑次数 —— `MAX_RETRIES` 于是提前用完,当天真正
        该补的那几次反而补不上了。
        """
        out = self._one_round_with(RuntimeError('模型 502'), retry_expected=True)
        self.assertEqual(1, out['calls'], '退避还没写下来就重试了')
        self.assertEqual([False], out['accepted'], '收尾还没提交就受理了新一轮')
        s = daily_run.load_settings()
        self.assertEqual((1, '2026-10-15T08:35'), (s['fail_count'], s['next_run']))
        with patch.object(daily_run, 'start', Mock(return_value=True)) as start:
            self.at(8, 10)
            self.assertFalse(daily_run.tick(), '退避没到就起了')
            self.at(8, 35)                  # 反过来钉一边:退避到了照常补跑
            self.assertTrue(daily_run.tick())
            start.assert_called_once()

    def test_publishing_idle_has_exactly_one_exit(self):
        """`running` 放回 False 的地方**只许有一处**,而且在 `_release_round` 里。

        这一条是结构守卫:上面两条钉的是那一处的顺序,而顺序对不对只有在"发布空闲
        就这一个出口"的前提下才有意义 —— 哪天又有人在别处写一句 `running=False`,
        那两条照样全绿,窗口却重新开了。
        """
        source = (Path(daily_run.__file__).parent / 'daily_run.py').read_text(encoding='utf-8')
        lines = [i for i, line in enumerate(source.splitlines(), 1)
                 if "_state['running'] = False" in line]
        self.assertEqual(1, len(lines), f'发布空闲的出口不止一处:{lines}')
        state = (Path(daily_run.__file__).parent / 'daily_state.py').read_text(encoding='utf-8')
        self.assertNotIn("_state['running'] = False", state, '状态模块里又多了一个出口')
        head = source[source.index('def _release_round'):]
        self.assertIn("_state['running'] = False", head[:head.index('\ndef ')],
                      '那一处不在 _release_round 里')
        # 顺序也按源码钉一道:终态提交要排在发布空闲之前
        body = head[:head.index('\ndef ')]
        self.assertLess(body.index('_finish_schedule('), body.index("_state['running'] = False"),
                        '定时终态要在发布空闲之前提交')



if __name__ == '__main__':
    unittest.main()
