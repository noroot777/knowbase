# -*- coding: utf-8 -*-
"""「这一轮作用在哪几个来源上」= **按来源执行的那几步各自范围的并集**。

抓取和导出早就是两份范围了(`crawl_filter` / `export_filter`:抓取收窄到失败的那
几条、导出照旧走整条来源),而「整轮来源」这个问题原来只读抓取那一份 —— 于是
**「只补导出」那一轮一个来源名都报不出来**(它的抓取范围是 `{"force": true}`):

- 开着的订阅页在重试成功之后**一直显示翻译失败和等待翻译**。它靠「这一轮带不带本
  来源」决定要不要重读条目,而仅翻译/导出既不改订阅行也不改收录总数 —— 另外两个
  判据对它都是瞎的,下一次轮询照旧什么都不重读,只刷新整页才看得到译好的正文;
- 同步中心写「选择来源后开始一次同步」、历史那行写「未记录来源」;
- **订阅的增 / 改 / 删写保护整条失效** —— 导出正跑着它那几篇文章,而订阅可以删。

所以判据立在契约层,而不是在「只补导出」这一条触发路径上打补丁:后端
`pipeline_stages.run_scopes`、前端 `pipe_channels.js` 的 `pipeRunScopes` 各一份(两份
实现各有自己的红,元规则那条),**输出形状也对齐**(channel / accounts / retry)。
给人翻的那份历史记录**故意不存导出范围**(那一份不许喂给重试派生),所以它改为存
**结论**(`run_record` 的 `scopes`)—— 这条分离不许被破坏,这里反过来钉着。

硬约束在 `docs/decisions/retry-scope.md` 第一节与 `pipeline-history.md` 第一节。
"""
from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from kb import pipeline_retry, pipeline_stages, pipeline_state
from kb.pipeline_persist import run_record, valid_round
from kb.web import intake_api
from tests.daybook_case import DaybookCase

JS = Path(__file__).resolve().parents[1] / 'kb' / 'static' / 'js'


class RoundSourcesCase(DaybookCase):
    """摆一天的欠账,然后**真点一次**「重试失败项」,看那一轮报得出哪几个来源。"""

    def retry_now(self):
        """真走 `start_retry_failed`,回那一轮收尾后的终态(两份落盘都已写)。

        只打桩六个阶段入口 —— 范围派生、计划、锁内核对、落盘、折账一行都不打桩。
        """
        with patch.object(pipeline_retry, '_run_crawl'), \
             patch.object(pipeline_retry, '_run_export'), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             patch.object(pipeline_retry, '_run_projects'), \
             patch.object(pipeline_retry, '_run_wiki'):
            ok, msg = pipeline_retry.start_retry_failed()
            self.assertTrue(ok, msg)
            self._wait_idle('重试那一轮没有收尾')
        return pipeline_state.get_state()

    def channels(self, run):
        return [s['channel'] for s in pipeline_stages.run_scopes(run)]

    def wechat_export_round(self, status='error'):
        """公众号那一轮**抓成了、导出红了**(「这个来源只欠导出」那个前提)。"""
        self._begin({'channel': 'wechat_local'}, stages=('crawl', 'export'))
        pipeline_state._stage('crawl', 'ok', '摆盘:公众号抓好了')
        pipeline_state._stage('export', status, '摆盘:公众号导出红了')
        pipeline_state._end_run()


class ExportOnlyRoundTests(RoundSourcesCase):
    """今天只欠导出 → 那一轮照旧要说出它作用在 RSS 上。"""

    def test_an_export_only_retry_still_names_its_source(self):
        self.export_round([str(self.a['id'])], broken=True)
        got = self.retry_now()
        self.assertNotIn('crawl', got['planned_stages'] or [],
                         '前提立不起来:这一轮压根不该带抓取')
        self.assertEqual({}, {k: v for k, v in (got['crawl_filter'] or {}).items()
                              if k in ('channel', 'channels')},
                         '前提立不起来:抓取范围里不该有来源名')
        self.assertEqual(['rss'], self.channels(got),
                         '只补导出那一轮报不出来源,订阅页就一直显示旧的失败')

    def test_the_subscription_write_guard_covers_an_export_only_round(self):
        """那一轮跑着的时候,这个来源的订阅**改不进去** —— 三个端点都拦。

        判据落在**库里那条订阅还在不在、名字变没变**,不落在「guard 抛没抛」:
        拦的目的是别让人在导出正写它那几篇文章的时候把订阅删掉。
        """
        self.export_round([str(self.a['id'])], broken=True)
        running = dict(self.retry_now(), running=True)
        body = intake_api.Subscription(name='新订阅', url='https://d.org/feed')
        patched = intake_api.SubscriptionPatch(name='改过的名字')
        with patch.object(intake_api.pipeline, 'get_state', return_value=running):
            for why, call in (('新增', lambda: intake_api.add_source('rss', body)),
                              ('修改', lambda: intake_api.update_source(
                                  'rss', self.a['id'], patched)),
                              ('删除', lambda: intake_api.delete_source(
                                  'rss', self.a['id']))):
                with self.assertRaises(HTTPException, msg=f'{why}没被拦住') as caught:
                    call()
                self.assertEqual(409, caught.exception.status_code, why)
        from kb.feeds import registry as reg
        names = {s['id']: s['name'] for s in reg.subscriptions('rss')}
        self.assertEqual('A', names.get(self.a['id']), '那条订阅被改掉了')
        self.assertNotIn('新订阅', names.values(), '那条订阅被加进去了')

    def test_a_library_only_round_does_not_lock_anyones_subscriptions(self):
        """**反过来钉一边**:只跑 index/extract 的那一轮不归任何来源,照旧放行。

        `start_pipeline` 一律往 filter 里盖一个默认采集器名,照它拦等于在一轮跟
        公众号毫无关系的运行里锁住公众号的订阅修改。

        判据**走真端点、落在库里那个名字上**:只问 guard 抛没抛的话,上一条那三个
        409 也可能是请求体本身不对 —— 这一条反过来证明那条路是通的。
        """
        running = {'running': True, 'planned_stages': ['index', 'extract'],
                   'crawl_filter': {'channel': 'rss', 'accounts': []},
                   'export_filter': {'channel': 'rss', 'accounts': []}}
        with patch.object(intake_api.pipeline, 'get_state', return_value=running):
            intake_api.update_source('rss', self.a['id'],
                                     intake_api.SubscriptionPatch(name='改过的名字'))
        from kb.feeds import registry as reg
        self.assertEqual('改过的名字',
                         {s['id']: s['name'] for s in reg.subscriptions('rss')}
                         .get(self.a['id']), '全库那一轮把别人的订阅锁住了')
        self.assertEqual([], pipeline_stages.run_scopes(running),
                         '全库那一轮不许凭空附加来源')


class MixedRangeRoundTests(RoundSourcesCase):
    """抓取 A、导出 A+B 那一轮:两个来源都要在,A 那一格还得是收窄过的那一份。"""

    def test_both_the_crawl_and_the_export_source_are_in_the_round(self):
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.wechat_export_round()
        got = self.retry_now()
        self.assertEqual(['rss', 'wechat_local'],
                         sorted(s['channel'] for s in pipeline_stages.run_scopes(got)),
                         '只读抓取范围就会漏掉只欠导出的那个来源')
        mine = [s for s in pipeline_stages.run_scopes(got) if s['channel'] == 'rss'][0]
        # 同一个来源两步都在时取**抓取**那一份:它是收窄过的那一份,而面板那行
        # 「本次范围」念的正是它(写成导出那份整条通道 = 「全部订阅」,那句话承诺
        # 了这一轮并没有发生的放宽)。
        # 收窄发出去的是「明确点名编号」(`id:63` 这种形态,口径
        # `kb/feeds/targets.py`):裸着发的那串数字同时也可能是另一条订阅的名字。
        from kb.feeds import registry as reg
        self.assertEqual([reg.id_target(self.a['id'])], mine['accounts'],
                         '抓取那一份收窄范围被导出那份整条通道盖掉了')
        self.assertEqual('targets', mine['retry'], 'retry 没透传,那行文案会念错')


class HistoryProjectionTests(RoundSourcesCase):
    """历史那一份副本自己答得出来源,**而且照旧没有导出范围**。"""

    def record(self):
        return pipeline_state.load_history()[0]

    def test_the_history_copy_answers_the_source_question_by_itself(self):
        self.export_round([str(self.a['id'])], broken=True)
        self.retry_now()
        disk = self.record()
        self.assertEqual(['rss'], self.channels(disk),
                         '历史那行会写「未记录来源」,点开主视图写「选择来源后开始一次同步」')
        self.assertTrue(valid_round(disk), '这一份得照旧算一轮有效运行')

    def test_the_history_copy_still_carries_no_retry_range(self):
        """**反过来钉一边:存的是答案,不是输入。**

        把 `export_filter` 存进历史就等于让重试从一份只够展示的记录里派生范围 ——
        而「这一份没有重试派生字段」正是 `scope` 那档欠账的判据(那条路错一个字
        就是静默不抓)。
        """
        self.export_round([str(self.a['id'])], broken=True)
        self.retry_now()
        disk = self.record()
        self.assertNotIn('export_filter', disk, '历史那一份不许带重试派生范围')
        self.assertNotIn('crawl_retry', disk, '同上:失败名单也不许进历史')

    def test_a_whole_library_round_records_no_source_at_all(self):
        """整库同步那一轮:空通道那一格不往这儿存(它不挂在任何具名来源上)。

        存着它的话历史那行会显示成一个空名字 —— 而那一轮该写「全库」。
        """
        self.plain_round(status='ok')
        self.assertEqual([], self.record()['scopes'])

    def test_a_record_without_the_answer_falls_back_to_the_crawl_range(self):
        """存量记录没有这一格,照老样子按抓取范围念(不许一律判成「不按来源」)。"""
        self.assertEqual(['rss'], self.channels(
            {'planned_stages': ['crawl', 'export'],
             'crawl_filter': {'channel': 'rss', 'accounts': []}}))

    def test_a_broken_answer_is_not_trusted(self):
        """那一格坏掉时**退回自己派生**,不许把坏数据当答案念出去。"""
        self.assertEqual(['rss'], self.channels(
            {'scopes': 'rss', 'planned_stages': ['export'],
             'crawl_filter': {'force': True},
             'export_filter': {'channel': 'rss', 'accounts': []}}))
        self.assertEqual(['rss'], self.channels(
            {'scopes': [None, {'channel': 'rss'}], 'planned_stages': [],
             'crawl_filter': {}}),
            '混进来的那个 null 不许把整格废掉,也不许抛')
        self.assertFalse(valid_round(dict(run_record(dict(
            pipeline_state._state, started_at='2026-10-20 09:00:00',
            finished_at='2026-10-20 09:10:00')), scopes=[None])),
            '元素验到被取用的那一层:历史那行逐条读 channel')
        for bad in ({'channel': 'rss', 'accounts': 7},
                    {'channel': 'rss', 'accounts': [{'id': 1}]},
                    {'channel': ['rss'], 'accounts': []},
                    {'channel': 'rss', 'accounts': [], 'retry': 7}):
            self.assertFalse(valid_round(dict(run_record(dict(
                pipeline_state._state, started_at='2026-10-20 09:00:00',
                finished_at='2026-10-20 09:10:00')), scopes=[bad])),
                f'逐字段验,和来源范围同一道:{bad}')
        self.assertTrue(valid_round(dict(run_record(dict(
            pipeline_state._state, started_at='2026-10-20 09:00:00',
            finished_at='2026-10-20 09:10:00')),
            scopes=[{'channel': 'rss', 'accounts': ['3'], 'retry': 'targets'}])),
            '**反过来钉一边**:正常那一份照旧过门')


class PanelSourceContractTests(unittest.TestCase):
    """前端那两份判据:抠真代码丢给 node 求值(不起浏览器、不连网)。

    一份是「这一轮作用在哪几个来源上」(`pipeRunScopes`),另一份是订阅页那条
    「出现了一轮新的已结束运行、而且它带着本来源」(`sourceRunFinished` ——
    它决定要不要重读条目,R1 里用户看到的「重试成功了还一直显示失败」就是它)。
    """

    SCOPES = r'function pipeRunScopes\(\w+\) \{.*?\n\}'
    FINISHED = (r"let sourceSeenRun = '';\nfunction sourceRunFinished"
                r'\(\w+\) \{.*?\n\}')
    #: 「此刻该念哪一份终态」只有这一份口径,订阅页那条判据也从它取 —— 所以这里
    #: 要把它一起抠进来(漏了它的后果是 `ReferenceError`,而不是一条假绿)。
    RESULT = r'function pipeResult\(\w+\) \{[^\n]*\}'

    def node(self, script):
        done = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)
        return json.loads(done.stdout)

    def pick(self, name, pattern):
        src = (JS / name).read_text(encoding='utf-8')
        got = re.search(pattern, src, re.S)
        self.assertTrue(got, f'{name} 里那段代码的形状变了,这条用例得跟着改')
        return got.group(0)

    def scopes_of(self, runs):
        script = '\n'.join([
            self.pick('pipe_channels.js', self.SCOPES),
            'console.log(JSON.stringify(' + runs + '.map(r => pipeRunScopes(r)',
            "  .map(s => [s.channel, s.accounts.length, s.retry].join('/')))));",
        ])
        return self.node(script)

    def test_the_panel_unions_the_ranges_of_the_stages_it_planned(self):
        self.assertEqual([
            ['rss/0/'],                              # 只补导出:照旧说得出 RSS
            ['rss/1/targets', 'wechat_local/0/'],    # 抓 A / 导 A+B:两个都在
            [],                                      # 纯全库:一个都不附加
            ['rss/0/'],                              # 老记录没有导出范围
            ['rss/2/'],                              # 裸 filter 那条老调用方
            ['social/0/all'],                        # 落盘写着答案就念它
        ], self.scopes_of("""[
          {planned_stages: ['export', 'index'], crawl_filter: {force: true},
           export_filter: {channel: 'rss', accounts: []}},
          {planned_stages: ['crawl', 'export'],
           crawl_filter: {channel: 'rss', accounts: ['7'], retry: 'targets'},
           export_filter: {channels: [{channel: 'rss', accounts: []},
                                      {channel: 'wechat_local', accounts: []}]}},
          {planned_stages: ['index', 'extract'],
           crawl_filter: {channel: 'wechat_local', accounts: []}},
          {planned_stages: ['crawl', 'export'], crawl_filter: {channel: 'rss'}},
          {channel: 'rss', accounts: ['7', '9']},
          {scopes: [{channel: 'social', accounts: [], retry: 'all'}],
           planned_stages: [], crawl_filter: {}}
        ]"""))

    def test_the_subscription_page_rereads_after_an_export_only_round(self):
        """重试那一轮一结束,**开着的那一页就得重读条目**。

        `reset` 那一下吸收屏幕上本来那一轮(它是历史记录,不是「刚刚跑完」),
        随后换成重试那一轮的终态 —— 判据必须为真,否则页面停在旧的翻译失败上。
        **不认 running→idle 那个跳变**:这条路一次模型都不调,整轮可能几百毫秒,
        面板一次都没见过 `running=true`(所以这里压根不摆那个中间态)。
        **反过来钉一边**:同一轮再问一次不许重复报,别人那一轮也不许认成自己的。

        后半截摆的是**重启之后**那一形:身份和来源都只在 `last_run` 里(顶层那几格
        是进程内状态,新进程里是空的),而恢复回来的那一轮**派生不出来源** —— 历史
        故意不存导出范围,来源只写在那一格结论里。这一页原来读的是顶层,于是永远
        认不出「刚跑完那一轮跟我有关」:提示不消失、新抓的条目也不重读,只有整页
        刷新才回来。口径是**身份和来源都从同一份终态上读**(`pipeResult`)。
        """
        retried = {'running': False, 'run_id': 'run-retry-1',
                   'planned_stages': ['export', 'index', 'extract'],
                   'crawl_filter': {'force': True},
                   'export_filter': {'channel': 'rss', 'accounts': []}}
        morning = dict(retried, run_id='run-morning')
        other = dict(retried, run_id='run-other',
                     export_filter={'channel': 'social', 'accounts': []})
        # 重启之后:顶层没有身份,终态和来源结论都只在 `last_run` 里。
        def restored(run_id, channel):
            return {'running': False, 'crawl_filter': {'force': True},
                    'last_run': {'run_id': run_id, 'crawl_filter': {'force': True},
                                 'planned_stages': ['export', 'index', 'extract'],
                                 'scopes': [{'channel': channel, 'accounts': [],
                                             'retry': ''}]}}
        script = '\n'.join([
            self.pick('pipe_channels.js', self.SCOPES),
            self.pick('pipe_view.js', self.RESULT),
            self.pick('sources.js', self.FINISHED),
            "let sourceKind = 'rss';",
            'let pipeSnapshot = ' + json.dumps(morning) + ';',
            'const out = [sourceRunFinished(true)];',
            'pipeSnapshot = ' + json.dumps(retried) + ';',
            'out.push(sourceRunFinished(false), sourceRunFinished(false));',
            'pipeSnapshot = ' + json.dumps(other) + ';',
            'out.push(sourceRunFinished(false));',
            'pipeSnapshot = ' + json.dumps(restored('run-restored', 'rss')) + ';',
            'out.push(sourceRunFinished(false), sourceRunFinished(false));',
            'pipeSnapshot = ' + json.dumps(restored('run-restored-2', 'social')) + ';',
            'out.push(sourceRunFinished(false));',
            'console.log(JSON.stringify(out));',
        ])
        self.assertEqual([False, True, False, False, True, False, False],
                         self.node(script),
                         '只补导出那一轮认不出本来源,这一页就一直显示旧的失败')


if __name__ == '__main__':
    unittest.main()
