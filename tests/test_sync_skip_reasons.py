# -*- coding: utf-8 -*-
"""一轮里有几种跳过原因,面板那句话要**全说出来**,不是只剩最后写进去的那个。

`summary['skipped']` 是各种原因之和,那句解释也得是:只留一个全局字符串的话,
「按了停止」和「预算用完」同时发生的那一轮,给出的解释由线程池的完成顺序决定
(审查第 1 轮非阻塞 C)。

**这里直测 `_note_skip`**:端到端要让两种原因在同一轮里同时发生,得靠线程调度
时序去卡预算边界 —— 那样的用例钉不住口径,只会偶发。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import sync

#: `x_browser.harvest` 里那两句原样写在这儿(它们是内联字面量,没有常量可引)。
STOP_NOTE = '已按停止请求中断'


class SkipReasonTests(unittest.TestCase):
    def _summary(self):
        return {'skipped': 0, 'message': '', 'skip_reasons': []}

    def test_both_reasons_survive(self):
        s = self._summary()
        sync._note_skip(s, STOP_NOTE)
        sync._note_skip(s, sync.BUDGET_MESSAGE)
        self.assertIn(STOP_NOTE, s['message'])
        self.assertIn(sync.BUDGET_MESSAGE, s['message'])
        self.assertEqual(2, len(s['skip_reasons']))

    def test_the_same_reason_is_said_once(self):
        """同一个原因跳过几十条,也只说一遍。"""
        s = self._summary()
        for _ in range(30):
            sync._note_skip(s, STOP_NOTE)
        self.assertEqual([STOP_NOTE], s['skip_reasons'])
        self.assertEqual(STOP_NOTE, s['message'])

    def test_the_order_is_the_order_they_happened(self):
        s = self._summary()
        sync._note_skip(s, 'A')
        sync._note_skip(s, 'B')
        sync._note_skip(s, 'A')
        self.assertEqual('A；B', s['message'])


if __name__ == '__main__':
    unittest.main()
