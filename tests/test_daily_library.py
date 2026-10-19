# -*- coding: utf-8 -*-
"""出报之后那一段(条目化 + wiki)到底跑不跑、跑在哪、算在谁头上。

**判据一律落在后果上**:管道真的被起了几轮、每一轮点名的是哪几个阶段、那一轮
算不算在日报的业务批次里、成绩单上多没多出一项。只验"调没调 `library_round`"
的话,一个把它调了却不起管道的实现也能全绿。

这一份要钉死的四件事,每一件都对应一个具体的坏形态:
①**它真的会跑** —— 原来只有手动一条路,结果 10 天一次都没跑过(图鉴最后一条
  停在 10-09 09:50、三张图谱停在 10-09 17:23,而那 10 天里新入库 4000+ 篇);
②**排在出报之后** —— 排在前面就是把日报推迟一个钟头,而日报不读它的产物;
③**出报失败照样跑** —— 绑在"出报成功"上的话,2026-10-19 那种连失败三次的
  日子里图鉴和 wiki 也跟着三次不跑,而它们消费的是**已经入库**的内容;
④**要进同一个业务批次** —— 批次外的一轮会把自己写成「最近一次运行」,这一批
  的抓取失败和失败名单跟着从主视图消失(`pipeline-history.md` 那个事故)。

跑:  python -m unittest tests.test_daily_library -v
"""
import unittest
from unittest.mock import Mock, patch

from kb import daily_library, daily_run
from kb.daily_state import _state


class LibraryRoundTests(unittest.TestCase):
    """`library_round` 自己那四道判据。`sync_round` 由调用方传进来,所以这里
    用一个记账的替身 —— 它记下"被要求跑哪几个阶段、带的是哪个批次"。"""

    def setUp(self):
        self.rounds: list[dict] = []
        self.channels: list[dict] = []
        _state['channels'] = self.channels
        _state['stop_requested'] = False
        self.addCleanup(lambda: _state.update(channels=[], stop_requested=False))

        def sync_round(stages, crawl_filter, label, deadline, channel='',
                       halted=None, batch=''):
            self.rounds.append({'stages': list(stages), 'label': label,
                                'channel': channel, 'batch': batch})
            return {'channel': channel, 'label': label, 'status': 'ok', 'detail': '完成'}
        self.sync_round = sync_round

    def _settings(self, **over):
        base = {'library': True}
        base.update(over)
        return patch('kb.daily_run.load_settings', lambda: base)

    def test_it_runs_the_two_library_stages_inside_the_batch(self):
        """① + ④:真起了一轮,点名的就是那两段,而且带着日报那一轮的批次身份。"""
        with self._settings():
            daily_library.library_round('batch-7', self.sync_round)
        self.assertEqual(1, len(self.rounds))
        self.assertEqual(['projects', 'wiki'], self.rounds[0]['stages'])
        self.assertEqual('batch-7', self.rounds[0]['batch'],
                         '没带批次身份:它会把自己写成「最近一次运行」,'
                         '这一批的抓取失败就从主视图消失了')

    def test_it_is_a_line_of_its_own_on_the_scorecard(self):
        """它是全库的活,不属于任何来源 —— `channel` 留空,只有 label。
        记到某个来源头上就是在日报文件里留一句假话。"""
        with self._settings():
            daily_library.library_round('b', self.sync_round)
        self.assertEqual(1, len(self.channels))
        self.assertEqual('', self.channels[0]['channel'])
        self.assertEqual(daily_library.LIBRARY_LABEL, self.channels[0]['label'])

    def test_the_switch_turns_it_off(self):
        """它烧额度,得关得掉。"""
        with self._settings(library=False):
            daily_library.library_round('b', self.sync_round)
        self.assertEqual([], self.rounds)
        self.assertEqual([], self.channels, '关掉的那一档不该在成绩单上占一项')

    def test_an_unreadable_schedule_does_not_spend_money(self):
        """读不出设置就不跑:这一段烧额度,而"读不出来"证明不了用户开着它。
        反过来(读不出就跑)是拿一份读不懂的配置替用户花钱。"""
        def boom():
            raise daily_run.ScheduleUnreadable('配置读不出来')
        with patch('kb.daily_run.load_settings', boom):
            daily_library.library_round('b', self.sync_round)
        self.assertEqual([], self.rounds)

    def test_a_stop_request_skips_it_but_says_so(self):
        """停止是对整轮说的(口径同入库那一段)。**但它要在成绩单上留一项** ——
        静默跳过和"跑了但什么都没干"在面板上长得一模一样。"""
        _state['stop_requested'] = True
        with self._settings():
            daily_library.library_round('b', self.sync_round)
        self.assertEqual([], self.rounds)
        self.assertEqual(1, len(self.channels))
        self.assertEqual('skipped', self.channels[0]['status'])
        self.assertIn('已停止', self.channels[0]['detail'])

    def test_a_report_only_run_has_nothing_to_attach_to(self):
        """手点「直接生成」那一档(`sync=False`)没有批次可进,而且用户要的就是
        "只出报"。判据是**批次是不是空的**,不是"设置开没开"。"""
        with self._settings():
            daily_library.library_round('', self.sync_round)
        self.assertEqual([], self.rounds)
        self.assertEqual([], self.channels)

    def test_over_budget_does_not_skip_it(self):
        """反过来钉一边:**超预算不算停止**。超预算的本意是"别再抓了",而这一段
        压根不抓 —— 跟着跳过等于让"今天抓得慢"决定"图鉴更不更新"。"""
        _state['over_budget'] = True
        self.addCleanup(lambda: _state.update(over_budget=False))
        with self._settings():
            daily_library.library_round('b', self.sync_round)
        self.assertEqual(1, len(self.rounds), '超预算把图鉴那一段也跳过了')


class AmendDoneTests(unittest.TestCase):
    """批次要罩住出报之后那一段,所以「发布成没成」比收尾文案晚一步才知道。"""

    def setUp(self):
        _state['phase'] = 'done'
        _state['detail'] = '日报已生成:12 条素材'
        _state['log'] = []
        self.addCleanup(lambda: _state.update(phase='idle', detail='', log=[]))

    def test_the_publish_problem_reaches_the_closing_line(self):
        """不补的话那句话就是一句干净的「日报已生成」,而同步中心那边看不到这一轮
        的失败、也点不了重试 —— 两边都显示正常,正是不许出现的那一档。"""
        daily_library.amend_done('；同步结果未发布:磁盘满')
        self.assertIn('日报已生成', _state['detail'])
        self.assertIn('磁盘满', _state['detail'])

    def test_a_failed_run_keeps_its_own_reason_first(self):
        """反过来钉一边:失败/停止那两档自己那句话已经说明问题了,往后面接一句
        批次发布的事只会把真正的原因挤到后面去。"""
        _state.update(phase='failed', detail='日报生成失败: Claude 503')
        daily_library.amend_done('；同步结果未发布:磁盘满')
        self.assertEqual('日报生成失败: Claude 503', _state['detail'])


class OrchestrationTests(unittest.TestCase):
    """② + ③:在日报那一轮的真实编排里,它排在出报之后,而且出报失败照样跑。

    判据是**调用顺序**和**出报抛异常那一局里它跑没跑**,不是"函数存在"。
    """

    def _run_once(self, generate):
        order: list[str] = []
        with patch('kb.daily_run.library_round',
                   lambda batch, sr: order.append('library')), \
             patch('kb.daily.generate', lambda **kw: (order.append('generate'),
                                                      generate())[1]), \
             patch('kb.daily_run._sync_round', Mock(return_value={
                 'channel': '', 'label': 'x', 'status': 'ok', 'detail': ''})), \
             patch('kb.daily_run._channel_labels', lambda: {'rss': 'RSS'}), \
             patch('kb.pipeline.open_batch', lambda kind='': 'b-1'), \
             patch('kb.pipeline.close_batch', lambda: ''), \
             patch('kb.pipeline.batch_note', lambda note: True), \
             patch('kb.daily_run._release_round', lambda *a: None):
            _state.update(stop_requested=False, over_budget=False, channels=[],
                          sched_day='', phase='idle', detail='', log=[])
            daily_run._run(None, None, True, ['rss'], 1)
        return order

    def test_the_library_round_comes_after_the_report(self):
        """排在前面就是把日报推迟一个钟头,而日报压根不读它的产物。"""
        order = self._run_once(lambda: {'date': '2026-10-19', 'since': '', 'until': '',
                                        'docs': 3, 'late': 0})
        self.assertEqual(['generate', 'library'], order)

    def test_a_failed_report_still_gets_its_library_round(self):
        """2026-10-19 那种连失败三次的日子:图鉴和 wiki 跟着三次不跑是错的 ——
        它们消费的是已经入库的内容,那一步早就成了。"""
        def boom():
            raise RuntimeError('Claude 调用失败: 503')
        order = self._run_once(boom)
        self.assertIn('library', order, '出报失败就不整理图鉴了')
        self.assertEqual(['generate', 'library'], order)


if __name__ == '__main__':
    unittest.main()
