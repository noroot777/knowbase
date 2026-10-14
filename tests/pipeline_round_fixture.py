# -*- coding: utf-8 -*-
"""管道结果那几组回归共用的摆盘:摆一轮运行 + **走真的收尾**。

两个文件都要用它(日报两段式编排那一组、结果发布与保全那一组),各写一份的话
两边的前提会各漂各的。背景见 `tests/test_batch_results.py` 的类注释。
"""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from kb import pipeline_state as ps


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
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(ps, '_STATE_FILE', Path(temp.name) / 'last_run.json')
        p.start(); self.addCleanup(p.stop)
        # `_state` / `_finished` 是模块级常驻的:这几条要往里摆终态,**走的时候
        # 一定要还回去**,否则同跑时会把别的用例带红,而单跑永远是绿的。
        before_state, before_done = deepcopy(ps._state), deepcopy(ps._finished)

        def restore():
            ps._state.clear(); ps._state.update(before_state)
            ps._finished.clear(); ps._finished.update(before_done)
            ps.close_batch()
        self.addCleanup(restore)

    def fresh_process(self):
        """装成"另一个进程":内存里那份权威记录归零,只剩磁盘。"""
        for name in ('_last_result', '_last_result_file'):
            pa = patch.object(ps, name, None); pa.start(); self.addCleanup(pa.stop)

    def spec(self, planned, results=None, run_id='run-x', crawl_filter=None,
             crawl_retry=None):
        """一轮的摆盘参数,交给 `run_batch` 真跑。"""
        return dict(planned=list(planned), results=results or {}, run_id=run_id,
                    crawl_filter=crawl_filter, crawl_retry=crawl_retry)

    def run_batch(self, *specs):
        """按顺序真跑完这几轮:**同一个批次,每一轮都穿过 `_end_run`。**

        原来这几条直接调那个私有合并函数,于是
        `_end_run → _publish_in_batch` 这一段(含"没资格"和"写不进去"两条退路)
        压根没被走到 —— 而那正是两处会把整批失败降成局部成功的地方(第 5 轮
        审查发现:584 条回归全绿,两种丢失败的方向一条都没钉住)。
        """
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        for i, sp in enumerate(specs):
            finish_round(sp['planned'], sp['crawl_filter'], batch, sp['results'],
                         sp['run_id'], f'2026-10-14 09:{i * 10:02d}:00',
                         sp['crawl_retry'])
        return batch
