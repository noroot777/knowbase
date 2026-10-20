# -*- coding: utf-8 -*-
"""当日账本的**缺口与降级**:写不上的那一轮不许就此消失,读不出来的要说出来。

折账本是旁路(写不上只记一笔 `persist_error`,不许挡住收尾),可「写不上」的那一轮
照样欠着东西 —— 退回去只看账本就是漏报。所以:

①写不上的那一轮**留在内存里**,下一次折的时候补进去,合成快照时也先盖上去;
②连内存都没留住的(挤掉的、进程退出的)要把「丢了几轮」**说出来** —— 那一档是真的
  补不回来了,和「这一次读不出来」不是一回事;
③**内存欠账那几桶按天分,旧日那些要扫掉**,而且**不许把旧账搬进今天**(「继承只在
  当日内」是用户定的边界);
④账本读不出来、或者可解析但形状不对 ⇒ 退回最近一次运行**并说出来**,不许让重试
  500;合成那一步自己炸了**单独说一句**(两句话指向的下一步不同)。

判据落在**下一轮还补不补得回来**和**那句降级说明**上。记账单位那一族在
`tests/test_retry_daybook.py`,摆盘共用 `tests/daybook_case.py`。

跑:  uv run --offline python -m unittest tests.test_daybook_gaps -v
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest.mock import patch

from kb import (pipeline_daybook as daybook, pipeline_retry, pipeline_stages,
                pipeline_state)
from tests.daybook_case import DaybookCase


class DaybookGapTests(DaybookCase):
    """账本缺了一块的时候,这颗按钮还补不补得到。"""

    # ── 写不上账本的那一轮:缺口要留得住 ──────────────────────────────────

    def test_a_round_the_book_missed_gets_folded_in_next_time(self):
        """账本写不上的那一轮**留在内存里**,下一次折的时候补进账本。

        折账本是旁路(写不上不许挡住收尾),可它写不上的那一轮照样欠着东西 ——
        什么都不留的话,后面任何一轮成功收尾都会在一份不完整的账本上接着写,而
        「当前有一格」被当成了「今天的历史齐全」。
        """
        with patch.object(daybook, '_write_atomic', side_effect=OSError('满盘')):
            self.crawl({'channel': 'rss'}, broken=[self.a])
        self.assertEqual({}, daybook.read(), '这一局的前提:那一轮一格都没写上')
        self.wechat_round()                             # 下一轮正常收尾
        cells = daybook.read()['stages']['crawl']
        self.assertIn('rss', cells, '补不上的那一轮没跟着折进来')
        self.assertIn('crawl', self.plan(), '它的红没进计划')

    def test_two_rounds_the_book_missed_are_both_still_owed(self):
        """连着两轮都没写进账本:**两轮的红都要在**,不是只剩最近那一轮。

        盖「最近一次运行」那一道只够盖一轮(那一格只有一个位子)—— 更早那一轮
        只在内存里,合成之前不把它盖上去就是漏报。
        """
        today = daybook._day()
        with patch.object(daybook, '_write_atomic', side_effect=OSError('满盘')):
            self.crawl({'channel': 'rss'}, broken=[self.a])          # 早些时候
            self.wechat_round(status='error', detail='公众号这一轮红了',
                              at=f'{today} 10:20:00')               # 最近那一轮
        pending, _ = daybook.unfinished(pipeline_state.last_result())
        owed = {s['channel'] for s in pipeline_stages.crawl_channels(
            pending['crawl_filter'])}
        self.assertEqual({'rss', 'wechat_local'}, owed,
                         f'内存里那两轮没一起补上:{pending["crawl_filter"]}')

    def test_a_round_the_book_never_got_is_still_offered_after_a_restart(self):
        """进程在中间退出(内存里那笔也没了):最近那一轮照旧盖得上去。

        两道各自管一段 —— 内存那一道管「后面还会再折一次」,盖最近那一轮那一道管
        「进程已经没了」。**两道各有自己的红**:只剩一道时另一档静默漏报。
        """
        today = daybook._day()
        self.wechat_round(at=f'{today} 09:00:00')
        with patch.object(daybook, '_write_atomic', side_effect=OSError('满盘')):
            self.plain_round(at=f'{today} 10:20:00')
        daybook._pending.clear()                        # 进程退出,内存那笔没了
        plan = self.plan()
        self.assertIn('crawl', plan, '最近那一轮的抓取失败被账本盖住了')
        self.assertIn('export', plan, '它的导出失败也被盖住了')

    def test_rounds_the_memory_could_not_hold_are_said_out_loud(self):
        """连内存都没留住的那几轮**要说出来** —— 那一档是真的补不回来了。

        和「这一次读不出来」不是一回事:后者下一次可能就好了,前者哪儿都没有了。
        """
        today = daybook._day()
        with patch.object(daybook, '_PENDING_MAX', 1), \
             patch.object(daybook, '_write_atomic', side_effect=OSError('满盘')):
            self.wechat_round(status='error', at=f'{today} 09:00:00')
            self.plain_round(at=f'{today} 10:20:00')
        _, note = daybook.unfinished(pipeline_state.last_result())
        self.assertIn('没能记进账本', note, f'丢了一轮却一个字没说:{note!r}')
        # 后面那几个阶段一律打桩:这一局断言的是**那句话**,真跑起来会去索引整个
        # vault(共用的 `KB_DATA_DIR/kb.db`),一条注入就把用例挂住十几分钟。
        with patch.object(pipeline_retry, '_run_crawl'), \
             patch.object(pipeline_retry, '_run_export'), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             self.no_backlog():
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertTrue(ok, msg)
        self._wait_idle()
        self.assertIn('没能记进账本', msg, f'起跑消息里也要带着它:{msg!r}')

    def test_yesterdays_memory_debt_is_dropped_not_carried_over(self):
        """**审查 N1**:内存欠账那几桶**按天分**,旧日那些要扫掉。

        桶的键是 `(账本文件, 日键)`,而只有「今天」那一桶会被折进账本、也只有它会
        被 `unfinished` 读到。磁盘一直写不进去的那种坏法下,旧日那些桶留着没有任何
        用处,只是一天一桶地攒在进程里(各最多 `_PENDING_MAX` 轮完整留档)。

        **扫掉不等于把旧账搬进今天**:这一局同时钉住那一边 —— 昨天那一轮的红不许
        出现在今天的计划里(「继承只在当日内」是用户定的边界)。
        """
        from datetime import timedelta
        day = datetime.now(daybook.TZ)
        old = day - timedelta(days=5)
        book = str(daybook._book_file())
        # 五天前那一轮没能写进账本,留在内存里:桶是它自己那一天的。
        # `fold` 把写盘异常照抛(`_end_run` 那边分别诊断),这里自己接住 ——
        # 要的是它**留在内存里**那一笔。
        with patch.object(daybook, '_write_atomic', side_effect=OSError('满盘')), \
             self.assertRaises(OSError):
            daybook.fold({'run_id': 'run-老', 'planned_stages': ['crawl'],
                          'crawl_filter': {'channel': 'rss'},
                          'finished_at': old.strftime('%Y-%m-%d %H:%M:%S'),
                          'stages': {'crawl': {'status': 'error', 'detail': '老的红'}}},
                         old)
        self.assertIn((book, daybook._day(old)), daybook._pending,
                      '这一局的前提:那一轮留在了它自己那一天的桶里')

        # 今天来一轮(写得上)。**一次正常访问就该把旧桶扫掉。**
        self.wechat_round(status='ok', at=f'{daybook._day()} 09:00:00')
        self.assertNotIn((book, daybook._day(old)), daybook._pending,
                         '五天前那一桶还攒在进程里')

        snap, note = daybook.unfinished(pipeline_state.last_result())
        self.assertNotIn('老的红', json.dumps(snap, ensure_ascii=False),
                         '把昨天的失败搬进了今天 —— 继承只在当日内')
        self.assertNotIn('没能记进账本', note,
                         '旧日那一笔不许算进「今天有几轮补不到」')

    def test_todays_memory_debt_survives_the_sweep(self):
        """**反过来钉一边**:今天那一桶扫不掉。

        只验「旧桶清掉了」的话,一个「一律清空」的实现也全绿 —— 而那正是漏报:
        写不上的那一轮带着的红就此没人认领。
        """
        today = daybook._day()
        with patch.object(daybook, '_write_atomic', side_effect=OSError('满盘')):
            self.wechat_round(status='error', at=f'{today} 09:00:00')
        key = (str(daybook._book_file()), today)
        self.assertIn(key, daybook._pending, '这一局的前提')
        daybook._bucket()            # 再访问一次:扫的那一下就在这里
        self.assertIn(key, daybook._pending, '把今天那一桶也扫掉了')

    def test_a_book_with_a_broken_inner_shape_does_not_break_the_retry(self):
        """账本**可解析、形状不对**那一档:退回最近一次运行并说出来,不许 500。

        正常 writer 写不出这个形状(手改、半截写、换了版本的 writer 能)。
        `today_failed` 把合成异常兜住了,而起跑那一侧原来没有 —— 同一份文件让状态
        接口照常、点一下重试却 500。
        """
        self._begin({'channel': 'rss'}, stages=('crawl', 'export'))
        pipeline_state._stage('crawl', 'ok', '好了')
        pipeline_state._stage('export', 'error', '摆盘:导出红了')
        pipeline_state._end_run()
        daybook._book_file().write_text(
            json.dumps({'day': daybook._day(), 'stages': {'crawl': {'rss': '不是字典'}}},
                       ensure_ascii=False), encoding='utf-8')
        self.assertEqual(['export'], pipeline_retry.today_failed(),
                         '状态接口那一侧该退回最近一次运行')
        with patch.object(pipeline_retry, '_run_export'), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             self.no_backlog():
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertTrue(ok, f'重试被一份形状不对的账本挡住了:{msg}')
        self._wait_idle()
        # **形状不对和读不出来是同一档**(`read` 里判一次就抛),所以说的是同一句话
        # —— 走到合成那一步才炸的话,报的会是另一句(下面那一局)。
        self.assertIn('读不出来', msg, f'降级原因没带到起跑消息里:{msg!r}')
        # **还要落一行运行日志。** 起跑那句话是一次性的(面板上那条 toast 一会儿
        # 就没了),而「今天早些时候那几笔这次补不到」是事后回头查才想得起来的事。
        self.assertTrue(any('读不出来' in line
                            for line in pipeline_state.get_state()['log']),
                        f'运行日志里没有降级那一句:{pipeline_state.get_state()["log"]}')

    def test_a_synthesis_that_blows_up_is_its_own_degradation(self):
        """合成那一步自己炸了:也只退回最近一次运行,**而且说的是另一句话**。

        两道各管一段(形状在 `read` 里判、别的在合成那一圈外面),**各有自己的红**
        —— 只剩一道时另一档要么 500、要么把原因说成不是那件事。
        """
        self._begin({'channel': 'rss'}, stages=('crawl', 'export'))
        pipeline_state._stage('crawl', 'ok', '好了')
        pipeline_state._stage('export', 'error', '摆盘:导出红了')
        pipeline_state._end_run()
        with patch.object(daybook, '_snapshot', side_effect=RuntimeError('合成炸了')), \
             patch.object(pipeline_retry, '_run_export'), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             self.no_backlog():
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertTrue(ok, f'合成炸了把整次重试带走了:{msg}')
        self._wait_idle()
        self.assertIn('合不成', msg, f'降级原因说成了另一件事:{msg!r}')

