# -*- coding: utf-8 -*-
"""走真的 `_end_run()` 的用例,必须把管道那几个落盘出口整组圈进自己的临时目录。

两条判据一前一后:一条**真跑一遍那个用例、比落盘文件的字节**(后果),一条
**按结构扫全部用例**(类问题)。只写后者的话它永远证不出"写出去的那一下真的发生
过";只写前者的话下一个新增的写出口照旧从别处漏出去。

硬约束和事故形态在 `tests/pipeline_round_fixture.isolate_pipeline_state`。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS = _ROOT / 'tests'
# 一个用例"把落盘圈起来了"的三种合法形状(见下面那条用例的 docstring)
_FENCE_CALL = 'isolate_pipeline_state'
_FENCE_ATTR = '_STATE_FILE'
_FINISH = '_end_run'


def _calls(tree: ast.AST):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _name_of(func) -> str:
    """被调用者的名字:`_end_run` / `ps._end_run` / `a.b._end_run` 都归成末段。"""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ''


def _finishes_a_round(tree: ast.AST) -> bool:
    return any(_name_of(c.func) == _FINISH for c in _calls(tree))


def _fences_itself(tree: ast.AST) -> bool:
    """自己就地圈了:调 `isolate_pipeline_state(...)`,或者 patch 掉 `_STATE_FILE`。

    patch 那一档按**参数里的字符串**认,不按引号风格 —— 仓库里单双引号混用
    (`patch.object(pipeline_state, "_STATE_FILE", ...)` 和 `'_STATE_FILE'` 都有),
    照文本 grep 会漏掉一半。
    """
    for call in _calls(tree):
        if _name_of(call.func) == _FENCE_CALL:
            return True
        if _name_of(call.func) in ('object', 'patch') and any(
                isinstance(a, ast.Constant) and a.value == _FENCE_ATTR for a in call.args):
            return True
    return False


def _classes(tree: ast.AST) -> set[str]:
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def _base_names(tree: ast.AST) -> set[str]:
    """这份文件的那些类是从哪些名字继承下来的(末段名字)。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                out.add(base.id if isinstance(base, ast.Name) else _name_of(base))
    return out


def _parsed() -> dict[str, ast.AST]:
    out = {}
    for path in sorted(_TESTS.glob('*.py')):
        out[path.stem] = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    return out


class PipelinePersistenceFenceTests(unittest.TestCase):
    """落盘副作用的所有权:谁收尾,谁负责把那几份文件圈在自己的临时目录里。"""

    def test_a_real_finish_writes_nothing_outside_its_own_temp_dir(self):
        """**真跑一遍**那条按 id 留档的用例,数据目录里三份落盘要一个字节都不变。

        判据落在**外部数据上**,不是落在"夹具里有没有那行调用"上:实测形态是用例
        自己返回 0 / `OK`,而当前 `KB_DATA_DIR` 的历史文件里多出一次从没发生过的
        `run-42` 成功同步、存满 20 条时最早那一条被挤掉 —— 面板上多一次假同步、少
        一条真记录,而测试全绿。
        三份都预置成"已经有存量"的样子:状态文件、存满的历史、以及一个开工标记
        (收尾那一步会去清它)。
        """
        with tempfile.TemporaryDirectory() as home:
            data = Path(home) / 'data'
            data.mkdir()
            seeded = {
                'pipeline_state.json': {
                    'run_id': 'sentinel', 'run_seq': 7, 'seq_gen': 'x',
                    'started_at': '2026-10-01 09:00:00',
                    'finished_at': '2026-10-01 09:05:00', 'planned_stages': ['crawl'],
                    'stages': {'crawl': {'status': 'ok', 'detail': '好了'}},
                    'crawl_filter': {}, 'log': []},
                'pipeline_state_runs.json': {'runs': [
                    {'run_id': f'old-{i}', 'run_seq': 20 - i, 'seq_gen': 'x',
                     'started_at': '2026-10-01 09:00:00',
                     'finished_at': '2026-10-01 09:05:00', 'planned_stages': ['crawl'],
                     'stages': {'crawl': {'status': 'ok', 'detail': '好了'}},
                     'crawl_filter': {}, 'log': []} for i in range(20)]},
                'pipeline_state_open.json': {'run_id': 'sentinel-open', 'run_seq': 7},
            }
            for name, body in seeded.items():
                (data / name).write_text(json.dumps(body, ensure_ascii=False),
                                         encoding='utf-8')
            before = {name: (data / name).read_bytes() for name in seeded}
            env = {**os.environ, 'KB_DATA_DIR': str(data), 'TMPDIR': str(Path(home))}
            case = ('tests.test_daily_stop.DailyStopTests'
                    '.test_the_pipeline_keeps_each_finished_run_addressable_by_id')
            r = subprocess.run([sys.executable, '-m', 'unittest', case],
                               cwd=_ROOT, env=env, capture_output=True, text=True,
                               timeout=300)
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            for name, data_before in before.items():
                with self.subTest(name):
                    self.assertEqual(data_before, (data / name).read_bytes(),
                                     f'{name} 被那条用例改写了')

    def test_every_test_that_finishes_a_real_round_fences_its_own_persistence(self):
        """**按结构扫全部用例**:凡是调了真的 `_end_run()`,就得圈住落盘。

        三种合法形状:①就地调 `isolate_pipeline_state(...)`;②就地 patch 掉
        `_STATE_FILE`(三份落盘都从它派生,圈一个就够);③**继承**一个定义在
        「自己满足前两条」的模块里的基类(它的 `setUp` 替它圈)。
        第三条认的是**继承**,不是"import 过那个夹具" —— 只 import 一个函数
        (`finish_round`)并不会让谁替你圈地,而按 import 放行会把那一档静默漏掉。
        按 AST 认而不是照文本 grep:`_end_run` 这个名字在 mock 里也出现
        (`patch.object(pipeline_retry, "_end_run")` —— 那是**打掉**它,不是跑它),
        而引号风格仓库里是混用的。
        """
        trees = _parsed()
        fenced = {name for name, tree in trees.items() if _fences_itself(tree)}
        inheritable = set().union(*(_classes(trees[name]) for name in fenced))
        offenders = [name for name, tree in sorted(trees.items())
                     if _finishes_a_round(tree) and name not in fenced
                     and not (_base_names(tree) & inheritable)]
        self.assertEqual([], offenders,
                         '这些用例走了真的 _end_run() 却没圈住落盘出口')
        # 反过来钉一边:这条判据得真的认出"有人在收尾",不是一律放行
        self.assertTrue(_finishes_a_round(trees['pipeline_round_fixture']))
        self.assertIn('pipeline_round_fixture', fenced)
        self.assertIn('RoundStore', inheritable, '夹具那个圈地基类得被认出来')


if __name__ == '__main__':
    unittest.main()
