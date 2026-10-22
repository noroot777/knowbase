# -*- coding: utf-8 -*-
"""「这一轮是**人按的**还是**定时自己跑的**」—— 判据只有一份。

这个事实只有一个消费方(抓取要不要跳过来源退避),却曾经在**四个入口各判一次**:
「重试失败项」、「我的订阅」页那颗同步、起跑弹窗走管道那条、起跑弹窗勾了「跑完
出日报」那条。前三个各自写死了一句「这一轮带 force」,第四个压根没人写 —— 于是
同一颗「开始同步」按钮,勾不勾「跑完出日报」是**两种行为**:不勾抓得到刚失败的
那条订阅,勾上就被静默跳过(只记一笔 `deferred`,而页面说「同步已开始」)。这条
缺口此前是一个入口一个入口补的,每次都只补了自己看得见的那一处。

所以这里钉两头:
1. **方向**:忘了声明的那个调用方落在「多抓一次」那一侧,不是「静默不同步」;
2. **结构**:整个 `kb/` 下「盖 force」只许出现在 `run_trigger.apply_force` 里 ——
   第五个入口再自己判一次就是红。**按写法搜而不是按名字搜**(注释和文档里要能
   照常讨论它),而**读** `force` 的那一侧不在射程内(消费方只有
   `pipeline_stages._fetch` 一处,它照此开 `FetchScope.force` 那条红在
   `tests/test_feed_sources.py`)。

每个入口自己「真跳过了退避」那条红各在各处:管道那条在 `tests/test_sync_channels.py`、
日报那条在 `tests/test_daily_two_stage.py`、重试那颗在 `tests/test_retry_scope.py`、
「我的订阅」那颗在 `tests/test_feed_sources.py`。硬约束写在
`docs/decisions/sync-entry.md`。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from kb import run_trigger

KB = Path(__file__).resolve().parent.parent / 'kb'

#: 「盖 force」唯一的落点。
FORCE_HOME = ('run_trigger.py', 'apply_force')


class RunTriggerTests(unittest.TestCase):
    def test_an_entry_that_forgets_to_say_lands_on_the_fetch_again_side(self):
        """**没说就算人按的。**

        方向是不对称的:误判成手动顶多多抓一次(退避只是省一次无用请求),误判成
        定时是**静默不同步** —— 用户点了同步、退避中的那几条被跳过,而面板上一个
        字都不说。所以空值、空串、没传,全落在手动那一侧。
        """
        for trigger in (None, '', run_trigger.MANUAL, 'whatever'):
            self.assertTrue(run_trigger.hand_pressed(trigger), repr(trigger))
        self.assertTrue(run_trigger.hand_pressed(), '连参数都没给')

    def test_only_the_timer_itself_is_not_a_hand_press(self):
        """**反过来钉一边**:明说了是定时的那一轮不许带 —— 退避本来就是给它用的。"""
        self.assertFalse(run_trigger.hand_pressed(run_trigger.SCHEDULE))
        self.assertIsNone(run_trigger.apply_force({'channel': 'rss'},
                                                  run_trigger.SCHEDULE).get('force'))

    def test_a_whole_library_round_stays_unscoped(self):
        """`crawl_filter` 是 `None` 时照旧是 `None`。

        那种形态(整库增量、不限来源)下面那几层压根不读 `filters`;把它变成
        `{'force': True}` 等于凭空给这一轮加了来源范围这一维。
        """
        self.assertIsNone(run_trigger.apply_force(None, run_trigger.MANUAL))

    def test_the_callers_dict_is_not_edited_in_place(self):
        """调用方传进来的那只字典不许被改。

        `pipeline_retry` 那一侧把范围攒好之后还要接着读它;就地改会让「这一轮作用
        在哪几个来源上」那笔账跟着多出一个键。
        """
        scope = {'channel': 'rss'}
        out = run_trigger.apply_force(scope, run_trigger.MANUAL)
        self.assertEqual({'channel': 'rss'}, scope, '原字典被就地改了')
        self.assertIs(True, out['force'])

    def test_stamping_force_only_happens_in_that_one_place(self):
        """**结构性那一半:整个 `kb/` 下只有一处盖 force。**

        运行时那道闸拦不住第五个入口自己写一句 `crawl_filter['force'] = True` ——
        那样写出来的东西**当场是对的**,坏的是下一次有人改方向时只改得到一处
        (这正是前三次各补一个入口的那个形态,元规则「口径只有一份」)。
        """
        offenders = []
        for path in sorted(KB.rglob('*.py')):
            for i, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                code = line.split('#', 1)[0]
                if not any(w in code for w in ("'force': True", '"force": True',
                                               "['force'] = True", '["force"] = True',
                                               "'force':True", '"force":True')):
                    continue
                where = f'{path.relative_to(KB.parent)}:{i}'
                if path.name != FORCE_HOME[0]:
                    offenders.append(where)
        self.assertEqual([], offenders,
                         f'盖 force 只许在 kb/{FORCE_HOME[0]} 的 {FORCE_HOME[1]} 里')

    def test_that_one_place_really_is_where_it_is_stamped(self):
        """**红 0 条先怀疑用例**:上一条靠「别处没有」证明,它在 `apply_force`
        自己改名、搬走、或者改成别的写法之后会变成一条永远绿的空规则。所以这里
        反过来钉住:那一句确实就在那个函数体里。
        """
        home = KB / FORCE_HOME[0]
        text = home.read_text(encoding='utf-8')
        fn = next((n for n in ast.walk(ast.parse(text))
                   if isinstance(n, ast.FunctionDef) and n.name == FORCE_HOME[1]), None)
        self.assertIsNotNone(fn, f'{FORCE_HOME[1]} 改名了,上一条规则要跟着改')
        inside = [i for i, line in enumerate(text.splitlines(), 1)
                  if "'force': True" in line.split('#', 1)[0]
                  and fn.lineno <= i <= (fn.end_lineno or fn.lineno)]
        self.assertTrue(inside, f'{FORCE_HOME[1]} 里已经没有那一句了')


if __name__ == '__main__':
    unittest.main()
