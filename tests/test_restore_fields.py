# -*- coding: utf-8 -*-
"""留档里有几格,恢复就要还回几格 —— 钉的是**两侧的字段集合对不对得上**。

一格一条用例是补不完的:`results` 漏过一次、`scopes` 又漏一次,形态逐字相同 ——
`pipeline_persist.run_record` 那一侧新增一格,`pipeline_state._restore_once` 那一侧
忘了跟着加,而既有用例全绿(它们各自只问自己那一格)。后果也相同:重启之后那一格
凭空变空,而面板上一切正常 —— 真失败的那个来源被算成欠着,点一次重试又抓一遍。

所以这里不问某一格,问集合,**两半各管一种漏法**:

1. **后果那一半** —— 真摆一轮「终态只在历史里」的运行、真重启,`last_result()` 逐格
   和留档相等。它看得见「填进来的东西不对」(旁路被拆掉那一档)。
2. **结构那一半** —— `run_record` 产出的键,恢复那一侧必须逐个碰过(AST 读的是真实
   代码,不是靠这段话约束)。它看得见「往留档里加了一格、恢复那边一个字都没写」,
   而那一档后果那一半看不见:新进程的 `_state` 里压根没有那个键,用例也就无从比较。

有意不搬的格子写进 `_NOT_RESTORED`,**只有写进那张名单才算豁免**:下次往留档里加
字段,要么恢复跟着加,要么在名单上写明为什么不用。眼下名单是空的 ——
`export_filter` / `crawl_retry` 这两个重试私有字段压根不进留档(`run_record` 的
docstring),所以它们不在这一侧的射程里。

摆盘共用 `tests/pipeline_case.py`;恢复那一族逐格的判据在 `test_pipeline_restore.py`。

跑:  uv run --offline python -m unittest tests.test_restore_fields -v
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace
from unittest import main

from kb import pipeline_persist as persist, pipeline_results as results
from kb import pipeline_state as state
from tests.pipeline_case import HistoryCase

#: 留档里有、而恢复**有意**不搬的那几格:`{字段: 为什么不用恢复}`。
#: 空着是对的 —— 往这里加一行等于声明「这一格重启之后丢掉也没有后果」,
#: 而那句话要能对着 `run_record` 那段注释说得通。
_NOT_RESTORED: dict[str, str] = {}


def _restore_touches() -> set[str]:
    """恢复那一侧**碰过**哪几个字段名(读的是代码本身,不是注释里的说法)。

    三种形态就是它真实的读写方式:`for key in (...)` 那几个批量搬的、
    `_state[...] = ` / `run[...] = ` 那几个单独写的、`pick.get(...)` 那几个
    单独取的。注释里提一句名字不算 —— 注释进不了 AST,所以这一道骗不过去。
    """
    out: set[str] = set()
    for fn in (state._restore_once, state._with_restored_scopes):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
                out |= {e.value for e in node.iter.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and isinstance(target.slice.value, str)):
                        out.add(target.slice.value)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == 'get' and node.args
                  and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str)):
                out.add(node.args[0].value)
    return out


class EveryRecordedFieldComesBackTests(HistoryCase):
    """留档那份记录和恢复回来的那份,字段集合必须对得上。"""

    def rich_round(self, run_id='run-b'):
        """摆一轮**每一格都有内容、而终态只在历史里**的运行,返回那份留档。

        状态文件写得上的那一档恢复走的是另一条路(它什么都不缺),所以这里要钉的
        只能是「最新那一轮只在历史里」—— 历史那份记录故意比状态文件少几格
        (`run_record`),漏格子的事只会在这一档上发生。

        **每一格都要有内容**:`results` 和 `scopes` 都是空的那一轮里,「没恢复」和
        「恢复了一个空的」长得一模一样,这一局就不敏感了。所以结果账走
        `results.channel_run` 真建一格,范围两侧都写上。
        """
        self.reset(run_id)
        state._state.update(
            planned_stages=['crawl', 'export'], run_kind='sync',
            crawl_filter={'channel': 'rss'}, export_filter={'channel': 'rss'},
            crawl_retry={'rss': {'targets': ['63'], 'at': '2026-10-14 09:30:00'}})
        state.mark_open(state._state)
        with results.channel_run('crawl', 'rss', SimpleNamespace(), []):
            state._stage('crawl', 'ok', '检查 3/3 个来源，新增 7 条')
        state._stage('export', 'error', '3 条翻译失败（服务返回 503）')
        with self.only_state_write_fails():
            state._end_run()
        return state.load_history()[0]

    def test_every_field_the_history_recorded_is_back_after_a_restart(self):
        """**后果那一半**:重启之后,留档里那几格逐个还在、值还一样。"""
        record = self.rich_round()
        for key in ('results', 'scopes', 'log', 'stages'):
            self.assertTrue(record.get(key),
                            f'这一局的前提:留档里 {key} 不是空的({record.get(key)!r})')
        self.restart()

        back = state.last_result()
        gone = sorted(k for k in record if k not in _NOT_RESTORED and k not in back)
        self.assertEqual([], gone, '留档里有这几格,恢复之后手上那一份里压根没有')
        differs = {k: (record[k], back.get(k)) for k in record
                   if k not in _NOT_RESTORED and back.get(k) != record[k]}
        self.assertEqual({}, differs, '恢复回来的值和留档里那一份不一样')

    def test_the_restore_touches_every_key_the_record_carries(self):
        """**结构那一半**:留档产出的每个键,恢复那一侧都得写过一次。

        这一半管的是「加了一格、恢复那边压根没写」那种漏:新进程的 `_state` 里
        没有那个键,后果那一半也就没有可比的东西,它看不见。
        """
        record = self.rich_round()
        touched = _restore_touches()
        self.assertEqual([], sorted(set(record) - touched - set(_NOT_RESTORED)),
                         '留档里有这几格,而恢复那一侧一个字都没碰它')

    def test_the_exemption_list_stays_honest(self):
        """**反过来钉一边**:名单上写着「不用恢复」的,代码里就不该恢复它。

        不然那张名单会变成一张只增不减的免罪符:哪天恢复补上了,名单还留着,
        下一次有人照着它去掉那段代码,而这一族全绿。
        """
        recorded = set(persist.run_record(dict(state._state)))
        self.assertEqual([], sorted(set(_NOT_RESTORED) & recorded & _restore_touches()),
                         '名单里写着不用恢复,代码里却恢复了')


if __name__ == '__main__':
    main()
