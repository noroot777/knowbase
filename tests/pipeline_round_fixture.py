# -*- coding: utf-8 -*-
"""管道结果那几组回归共用的摆盘:摆一轮运行 + **走真的收尾**。

好几组都要用它(日报两段式编排、结果的发布与保全、历史那几行谁顶谁),各写一份
的话各组的前提会各漂各的。背景见 `tests/test_batch_results.py` 的类注释。
"""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from kb import pipeline_batch as pb
from kb import pipeline_state as ps

# 进程内常驻、会在用例之间互相渗的那几格(理由见 `isolate_pipeline_state`)。
# **按模块分别列**:批次账本在 `pipeline_batch`,留档诊断和恢复那几格在
# `pipeline_state`(那边顶着 700 行上限,批次层和分段汇报都拆了出去)。还原必须
# `setattr` 到**定义它的那个模块**上 —— 落到另一个模块只会在那边挂一个没人读的
# 同名属性,还原看着做了、其实一格都没还回去。
_PROCESS_GLOBALS = (
    (pb, ('_aggregate', '_aggregate_file', '_covered',
          '_last_settled', '_last_settled_file')),
    (ps, ('_persist_error', '_restore_gap', '_restored', '_seq_hwm')),
)


def isolate_pipeline_state(case: unittest.TestCase) -> Path:
    """把这个用例拥有的**全部管道落盘出口**圈进它自己的临时根目录,顺带还原进程内
    常驻那几格。返回那个临时目录。

    **凡是要走真的 `_end_run()` 的用例都得过这里,不许只 mock 某一个保存函数。**
    `_end_run` 的收尾出口不止一个(状态文件、历史、开工标记),而且还会动
    `_persist_error` / `_last_settled` / `_seq_hwm` / `_restore_gap` 这几个进程全局
    (分散在 `pipeline_state` 和 `pipeline_batch` 两边,见 `_PROCESS_GLOBALS`)。
    只 mock 当时唯一的那个写函数,等于把隔离押在"以后不会再加写出口"上 —— 实测
    形态:日报那组里按 id 留档那条用例只 mock 了 `_save_last_run`,新增的
    `_save_history` 于是把一个**从没发生过**的 `run-42` 成功同步写进了当前
    `KB_DATA_DIR` 的历史文件,还把存满 20 条时最早那一条挤掉;用例返回 0 / `OK`,
    面板上多出一次假同步、少掉一条真记录。
    三份落盘都从 `_STATE_FILE` 派生(见那边的注释),所以圈一个就够 —— 这也是那条
    「派生、不另立模块变量」的用处。
    """
    temp = tempfile.TemporaryDirectory()
    case.addCleanup(temp.cleanup)
    pa = patch.object(ps, '_STATE_FILE', Path(temp.name) / 'last_run.json')
    pa.start(); case.addCleanup(pa.stop)
    before_state, before_done = deepcopy(ps._state), deepcopy(ps._finished)
    keep = [(mod, name, getattr(mod, name))
            for mod, names in _PROCESS_GLOBALS for name in names]

    def restore():
        ps._state.clear(); ps._state.update(before_state)
        ps._finished.clear(); ps._finished.update(before_done)
        for mod, name, value in keep:
            setattr(mod, name, value)
        pb.close_batch()

    case.addCleanup(restore)
    return Path(temp.name)


def finish_round(planned, crawl_filter, batch='', results=None, run_id='run-1',
                 started='2026-10-14 09:00:00', crawl_retry=None):
    """照 `start_pipeline` 的样子摆一轮管道运行,再**走真的收尾**(`_end_run`)。

    两组用例共用一份摆盘:批次账本是由 `_end_run` 喂的,各写一份的话两边的前提
    会各漂各的。含 `start_pipeline` 那个"没指定来源就盖默认采集器"的兜底 ——
    全库那一轮的 `crawl_filter` 现实中就带着一个跟它无关的来源名。
    """
    from kb.collectors import active_collector
    cf = deepcopy(crawl_filter or {})
    cf['channel'] = active_collector(cf.get('channel')).name
    full = {st: {'status': 'skipped', 'detail': '未选择'} for st in ps.STAGES}
    full.update({st: {'status': 'ok', 'detail': f'{st} 好了'} for st in planned})
    full.update(results or {})
    ps._state.update({
        'running': True, 'stop_requested': False, 'stopped': False,
        'run_id': run_id, 'log': [], 'current_stage': '',
        'started_at': started, 'finished_at': '',
        'crawl_filter': cf, 'export_filter': deepcopy(cf),
        'run_kind': 'sync', 'batch': batch, 'crawl_retry': crawl_retry or {},
        'planned_stages': [st for st in ps.STAGES if st in planned],
        'stages': full})
    ps._end_run()
    return run_id


class RoundStore(unittest.TestCase):
    """临时状态文件 + 模块级常驻状态的还原,外加"真跑一个批次"那几个口径。"""

    def setUp(self):
        # 临时落盘根目录 + 进程内常驻那几格的还原,**口径只有一份**(上面那个函数):
        # `_state` / `_finished` 要往里摆终态,批次汇总、上一轮收尾身份、留档诊断、
        # 恢复那几格都是进程内的 —— 不还回去的话同跑时会把别的用例带红,而单跑永远
        # 是绿的。
        isolate_pipeline_state(self)

    def fresh_process(self):
        """装成"另一个进程":进程内那几格归零,最新那一轮只能从磁盘恢复。

        `_state` 里的身份和收尾时刻也要清掉 —— 留着的话 `last_result()` 认的是内存
        那一份(新进程里它本来是空的),磁盘那条恢复路径压根走不到(口径见
        `pipeline_state._latest_locked` / `_restore_once`)。
        """
        for mod, name, value in ((pb, '_aggregate', None), (pb, '_aggregate_file', None),
                                 (pb, '_covered', frozenset()),
                                 (pb, '_last_settled', ''), (pb, '_last_settled_file', None),
                                 (ps, '_restored', ''), (ps, '_restore_gap', ''),
                                 (ps, '_persist_error', '')):
            # `patch.object` 认不认这个属性是一道现成的守卫:名字落错模块当场报错
            pa = patch.object(mod, name, value); pa.start(); self.addCleanup(pa.stop)
        was = deepcopy(ps._state)
        self.addCleanup(lambda: ps._state.update(was))
        ps._state.update(running=False, finished_at='', run_id='', started_at='')

    def spec(self, planned, results=None, run_id='run-x', crawl_filter=None,
             crawl_retry=None):
        """一轮的摆盘参数,交给 `run_batch` 真跑。"""
        return dict(planned=list(planned), results=results or {}, run_id=run_id,
                    crawl_filter=crawl_filter, crawl_retry=crawl_retry)

    def run_batch(self, *specs):
        """按顺序真跑完这几轮:**同一个批次,每一轮都穿过 `_end_run`。**

        原来这几条直接调那个私有合并函数,于是
        `_end_run → pipeline_batch._publish_in_batch` 这一段(含"没资格"和"写不进去"两条退路)
        压根没被走到 —— 而那正是两处会把整批失败降成局部成功的地方(第 5 轮
        审查发现:584 条回归全绿,两种丢失败的方向一条都没钉住)。
        """
        batch = pb.open_batch()
        self.addCleanup(pb.close_batch)
        for i, sp in enumerate(specs):
            finish_round(sp['planned'], sp['crawl_filter'], batch, sp['results'],
                         sp['run_id'], f'2026-10-14 09:{i * 10:02d}:00',
                         sp['crawl_retry'])
        return batch
