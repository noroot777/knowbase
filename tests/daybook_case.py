# -*- coding: utf-8 -*-
"""当日账本那两族共用的摆盘 —— **判据一条都不在这里**。

分出来只为行数(单文件 700 行上限,CLAUDE.md 那条):那两族各装自己的判据,而
「怎么摆出一轮、怎么摆一轮导出、怎么把账本里的时刻改掉」是它们共用的一套。
摆盘动了会同时改变两边的前提,所以改它之前先想清楚两边。
"""
from __future__ import annotations

import json
from unittest.mock import patch

from kb import pipeline_daybook as daybook, pipeline_retry, pipeline_stages, pipeline_state
from tests.retry_scope_fixture import RetryScopeBase


class DaybookCase(RetryScopeBase):
    """今天那本账的摆盘。"""

    def wechat_round(self, status='ok', detail='公众号这一轮好了', at=''):
        """真走收尾摆一轮**另一个来源**的运行(日报那一轮就是这个形状)。"""
        with patch.object(pipeline_state, '_now',
                          return_value=at or pipeline_state._now()):
            self._begin({'channel': 'wechat_local'}, stages=('crawl', 'export'))
            pipeline_state._stage('crawl', status, detail)
            pipeline_state._stage('export', status, detail)
            pipeline_state._end_run()

    def plain_round(self, status='error', detail='摆盘:这一轮红了', at=''):
        """**整库同步**那一轮(面板上一个来源都没点、`crawl_filter` 是空的)。

        这是最常见的那一轮,所以它必须有自己的一局:带来源那几局全绿时,「没指定
        来源的那一轮一格都不记」这个坏法一条都红不出来。
        """
        with patch.object(pipeline_state, '_now',
                          return_value=at or pipeline_state._now()):
            self._begin({}, stages=('crawl', 'export'))
            pipeline_state._stage('crawl', status, detail)
            pipeline_state._stage('export', status, detail)
            pipeline_state._end_run()

    def plan(self):
        with self.no_backlog():
            return pipeline_retry.retry_failed_plan()

    def bump_book(self, stage, at):
        """把账本里这一步**每一格**的时刻改成 `at`(「账本比它新」那几局的摆盘)。

        账本是 (步骤, 来源, 目标) 三层的,所以要走到最里面那一格 —— 改在中间那一层
        上只会多出一个形状不对的键,而那一档现在会被 `read` 判成「账本坏了」,于是
        这一局验的变成了降级那条路。
        """
        raw = json.loads(daybook._book_file().read_text(encoding='utf-8'))
        for targets in raw['stages'][stage].values():
            for cell in targets.values():
                cell['at'] = at
        daybook._book_file().write_text(json.dumps(raw, ensure_ascii=False),
                                        encoding='utf-8')

    def rename_book_key(self, stage, channel, old, new):
        """把账本里那一格的键从 `old` 改成 `new`。

        用来摆**升级前那份账**:那一版按订阅的显示名记,这一版按稳定 id 记。账本是
        (步骤, 来源, 目标) 三层的,改的是最里面那一层的键 —— 改在中间那一层上只会
        多出一个形状不对的键,`read` 会把它判成「账本坏了」,那一局就跑到降级那条
        路上去了。
        """
        raw = json.loads(daybook._book_file().read_text(encoding='utf-8'))
        cells = raw['stages'][stage][channel]
        cells[new] = cells.pop(old)
        daybook._book_file().write_text(json.dumps(raw, ensure_ascii=False),
                                        encoding='utf-8')

    def book_cells(self, stage, channel):
        """账本里这一步这个来源那几格:`{目标: 状态}`(判据留给用例自己写)。

        **读的是落盘那一份**,不是进程内的 —— 折账走的就是写盘那条路。
        """
        return {target: cell.get('status')
                for target, cell in ((daybook.read().get('stages') or {})
                                     .get(stage, {}).get(channel, {})).items()}

    def export_round(self, accounts, broken=False):
        """真走 `_run_export` 摆一轮导出(`materialize` 打桩,不真落盘)。

        **必须走 `_per_channel`**:导出的覆盖范围是在那儿记下来的
        (`pipeline_results.WHOLE_SOURCE_STAGES`)。把 `_run_export` 整个打桩掉的
        那几局验不到它 —— 那时压根没有结果账,折账走的是退回请求范围那条保守路。

        `broken=True` 时让 `materialize` 抛出去,**不是让它自己报一个 error** ——
        导出进门先落一张空回执、顺手把卡片写成 `ok/已保存 0 条`(`fetch_rounds`),
        自己报的那个 error 会被它盖掉(实测),于是这一局的前提压根立不起来。
        抛出去那条路和生产里一样,**连编排层那一下也照着做**(`_run_pipeline` 的
        export 分支把异常接住、记一笔 error)—— 少了它,这一局摆出来的是「异常穿
        出去、谁都没记」,而生产里永远有人记。
        """
        # **打桩打在类上。** `active_collector('rss')` 每次新建一个 `FeedCollector`
        # (`_REGISTRY` 里存的是类,不是实例),打在自己手里那个实例上拦不住
        # `_per_channel` 现取的那一个 —— 而用例会红在「前提立不起来」上,指不到真
        # 原因。公众号那条是共享实例,所以那几局打实例是对的。
        from kb.collectors.feeds import FeedCollector
        scope = {'channel': 'rss', 'accounts': accounts}
        self._begin(scope, stages=('export',))
        def materialize(_self):
            if broken:
                raise OSError('摆盘:磁盘暂时只读')
            pipeline_state._stage('export', 'ok', '已保存 1 条')
            return frozenset()
        with patch.object(FeedCollector, 'materialize', materialize):
            try:
                pipeline_stages._run_export(scope)
            except OSError as e:
                pipeline_state._stage('export', 'error', f'导出失败:{e}')
        pipeline_state._end_run()

