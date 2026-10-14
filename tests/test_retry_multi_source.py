# -*- coding: utf-8 -*-
"""「重试失败项」在**多来源**那一轮的范围,外加面板上那行「本次范围」。

单独成文件是因为这里的形状和单来源那组不同(两个来源、两种失败各一个、还要
看 export 和前端),挤进 `tests/test_retry_scope.py` 会越过 700 行上限;摆盘共用
`tests/retry_scope_fixture.py` 那一份。

这一组钉的是**两种失败不是一件事**:
- 一条订阅自己抓不动(地址错了、源站 500)→ 名单里有它,下一轮只抓它;
- 整个来源这一轮压根没开工(登录态读取器崩了、采集器抛异常)→ 一条订阅都没
  被碰到,也就没有名单 —— 这时该重抓**整个来源**。

两者在 `subscriptions` 那边长得一模一样(都是一个 `last_error` 都没有),所以
判据只能是"这个来源留下名单了吗";拿一个全局布尔决定所有来源的范围同样不行,
它会让一个来源的失败类型决定另一个来源抓多少东西(第 1 轮审查的第 2 条)。
"""
from __future__ import annotations

import re
import subprocess
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from kb import pipeline, pipeline_retry, pipeline_stages, pipeline_state
from kb.feeds import registry as reg, sync, translate, x_channels

from tests.retry_scope_fixture import FeedError, RoundBase, _entry

JS = Path(__file__).resolve().parents[1] / 'kb' / 'static' / 'js'


class MultiSourceBase(RoundBase):
    """一轮同时抓两个来源:大V观点(social)+ RSS,各三条订阅。"""

    def setUp(self):
        super().setUp()
        for kind in ('social', 'rss'):
            for s in reg.subscriptions(kind):
                reg.delete_subscription(kind, s['id'])
        # 桥接模板不填的话这几条 X 订阅一律被判「待配置」、压根进不了 pending
        # (见 `registry.configuration_error`),而这一组要验的正是它们抓没抓。
        reg.save_settings('social', {'bridges': ['https://bridge.example/{handle}']})
        self.rss = [reg.save_subscription(
            'rss', {'name': n.upper(), 'url': f'https://{n}.org/feed'})
            for n in ('a', 'b', 'c')]
        self.x = [reg.save_subscription(
            'social', {'name': '@' + h, 'url': f'https://x.com/{h}'})
            for h in ('x1', 'x2', 'x3')]

    def both(self):
        return {'channels': [{'channel': 'social', 'accounts': []},
                             {'channel': 'rss', 'accounts': []}]}

    def _recorder(self, got, broken, tag):
        """记下每个来源各被抓了哪几条订阅;`broken` 里那几条抛错。"""
        bad = {(s['kind'], s['id']) for s in broken}

        def fetch(source, *a, **kw):
            got.setdefault(source['kind'], []).append(source['id'])
            if (source['kind'], source['id']) in bad:
                raise FeedError('地址错了')
            return ([_entry(f"{source['url']}/{tag}")],
                    {'etag': tag, 'modified': '', 'resolved_url': source['url']})
        return fetch

    def round_one(self, got, broken=(), blow_up=(), stages=('crawl',)):
        """跑第一轮。`blow_up` 里那个来源整批失败(一条订阅都没被碰到)。"""
        self._begin(self.both(), stages=stages)
        real = sync._prefetch_x

        def prefetch(kind, sources, settings, scope):
            if kind in blow_up:
                raise RuntimeError('登录态读取器崩了')
            return real(kind, sources, settings, scope)

        with patch.object(sync, 'fetch_source',
                          side_effect=self._recorder(got, broken, 'p1')), \
             patch.object(sync, '_prefetch_x', side_effect=prefetch):
            pipeline_stages._run_crawl(self.both())

    def retry(self, got, export_seen=None):
        """点一次「重试失败项」,真走 `start_retry_failed` 造范围那条路。"""
        # `export_seen or []` 会在传进来一个**空**列表时换成另一个空列表 ——
        # 记录全写进了那个临时对象,断言永远看到 0 次调用(自查时踩到过)。
        seen = export_seen if export_seen is not None else []
        export = patch.object(pipeline_retry, '_run_export',
                              side_effect=lambda f=None: seen.append(deepcopy(f)))
        with patch.object(sync, 'fetch_source',
                          side_effect=self._recorder(got, (), 'p2')), export, \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             patch.object(pipeline_retry, '_run_projects'), \
             patch.object(pipeline_retry, '_run_wiki'):
            ok, msg = pipeline_retry.start_retry_failed()
            self.assertTrue(ok, msg)
            self._wait_idle('重试那一轮没有收尾')

    def notes(self):
        return pipeline_state.retry_notes(pipeline_state.load_last_run() or {})


class MixedFailureTests(MultiSourceBase):
    """一个来源整批失败、另一个只坏了一条:各按各的范围重试。

    报告里那一局:「同时同步大V观点和RSS,大V观点整批连接失败、RSS 有一个订阅
    失败;恢复连接再点「重试失败项」,RSS 会补抓,大V观点仍一条也没有抓」。
    """

    def test_a_batch_failure_reruns_in_full_while_the_other_narrows(self):
        first = {}
        self.round_one(first, broken=[self.rss[0]], blow_up=('social',))
        pipeline_state._end_run()
        self.assertNotIn('social', first,
                         '这一局的前提:整批失败,一条 X 订阅都没被碰到')
        self.assertEqual({'rss'}, set(self.notes()),
                         '这一局的前提:整批失败的那个来源留不下名单')

        got = {}
        self.retry(got)
        self.assertEqual({s['id'] for s in self.x}, set(got.get('social', [])),
                         '整轮都没开工的来源该按原范围重跑,不是一条都不抓')
        self.assertEqual([self.rss[0]['id']], got.get('rss'),
                         '另一个来源只坏了一条,不该跟着被重抓一遍')

    def test_both_sources_narrow_when_each_failed_per_subscription(self):
        """反过来钉一边:两个来源都是逐项失败时,两边都收窄。

        和上一条合起来才说明判据是"这个来源留下名单了吗" —— 一律收窄、或者
        一律按原范围重跑,各有一条会红。
        """
        first = {}
        self.round_one(first, broken=[self.rss[0], self.x[1]])
        pipeline_state._end_run()
        self.assertEqual(3, len(first['social']),
                         '这一局的前提:X 这一轮三条都被碰到了')
        self.assertEqual({'social', 'rss'}, set(self.notes()))

        got = {}
        self.retry(got)
        self.assertEqual([self.x[1]['id']], got.get('social'))
        self.assertEqual([self.rss[0]['id']], got.get('rss'))

    def test_a_source_with_nothing_left_drops_out_of_the_retry(self):
        """这个来源已经没有要重抓的了 → 整个来源不进这一轮。

        留着它(accounts 空 = 全部)就是这颗按钮刚被修掉的那个行为:一条订阅
        的失败换来另一个来源 51 个账号的时间线全部重读。
        """
        first = {}
        self.round_one(first, broken=[self.rss[0], self.x[1]])
        pipeline_state._end_run()
        with reg.db() as c:          # X 那条后来在别处成功同步过一次
            c.execute('UPDATE subscriptions SET last_success=? WHERE id=?',
                      ('2999-01-01T00:00:00+00:00', self.x[1]['id']))

        scopes = pipeline_retry.retry_crawl_scopes(pipeline_state.load_last_run() or {})
        self.assertEqual([{'channel': 'rss', 'accounts': [str(self.rss[0]['id'])],
                           'retry': 'targets'}], scopes)
        got = {}
        self.retry(got)
        self.assertNotIn('social', got, '这个来源没有要重抓的,一个请求都不该发')


class ExportScopeTests(MultiSourceBase):
    """crawl 收窄了,export 仍按上一轮**原来**那几个来源跑。

    两者的范围不是同一个:一个来源完全可能 crawl 成功、export 失败(翻译扣着
    不导出、vault 写不进去…)。跟着 crawl 一起摘掉的话它那一步就再也没人重试,
    而这颗按钮承诺的是"把上次失败的都重试一遍"。
    """

    def test_export_keeps_every_source_from_the_last_round(self):
        first = {}
        self.round_one(first, broken=[self.rss[0]], stages=('crawl', 'export'))
        pipeline_state._stage('export', 'error', '导出失败')
        pipeline_state._end_run()

        got, seen = {}, []
        self.retry(got, export_seen=seen)
        self.assertEqual([self.rss[0]['id']], got.get('rss'))
        self.assertNotIn('social', got, 'crawl 这一步 X 没有要重抓的')
        self.assertEqual(1, len(seen))
        self.assertEqual(['social', 'rss'],
                         [s['channel'] for s in pipeline_stages.crawl_channels(seen[0])],
                         'export 的范围该是上一轮原来那两个来源')


class RepeatedExportScopeTests(MultiSourceBase):
    """连着点两次「重试失败项」:导出范围不许在落盘那一步被抓取范围替掉。

    第一次重试里 crawl 收窄成 RSS 那一条、export 照旧跑 social+rss —— 可只把
    **收窄后的抓取范围**落盘的话,第二次重试会从它再派生一次导出范围,于是第一次
    重试里**仍然导出失败**的 social 就此被摘掉:面板报「导出完成 · 已保存 0 条」,
    而它那几条一篇都没落进 vault,再点重试也不会回来(2026-10-09 第 2 轮审查实测)。

    上一条 `ExportScopeTests` 只验了**第一次**重试,范围是在落盘之后丢的,所以
    它对这件事压根不敏感 —— 两条都得有。
    """

    def blocker(self):
        """用一个普通文件占住 `source/social` 该在的位置。

        真实 `sync.export` 开头就是 `root.mkdir(parents=True, exist_ok=True)`,
        那个位置是文件时它当场抛 `FileExistsError`。**不打桩 `_run_export` 或
        `materialize`** —— 那样阶段分段、失败落盘和 `retry_failed_plan` 看到的
        东西全是我们摆的,而这一组要验的正是"第二次重试还认不认得出 export
        失败过、它的范围里还有没有这个来源"。
        """
        path = self.root / 'vault' / 'source' / 'social'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('占位', encoding='utf-8')
        return path

    def exported(self, kind):
        """这个来源在 vault 里真的落了哪几篇 md(后果,不是过程)。"""
        folder = self.root / 'vault' / 'source' / kind
        return sorted(f.name for f in folder.glob('*.md')) if folder.is_dir() else []

    def run_export(self, crawl_filter):
        """真跑一遍导出阶段。只把"配了 key 吗"按死成没配 —— 那是真实的
        `skipped` 那一档(`materialize` 据此不扣留未译条目),也顺手保证这组
        用例一次模型请求都不发。"""
        with patch.object(translate, '_model', side_effect=RuntimeError('未配置模型')):
            pipeline_stages._run_export(crawl_filter)

    def retry_round(self, got):
        """点一次「重试失败项」,crawl 和 export 两条链一行不打桩。"""
        with patch.object(sync, 'fetch_source',
                          side_effect=self._recorder(got, (), 'p2')), \
             patch.object(translate, '_model',
                          side_effect=RuntimeError('未配置模型')), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             patch.object(pipeline_retry, '_run_projects'), \
             patch.object(pipeline_retry, '_run_wiki'):
            ok, msg = pipeline_retry.start_retry_failed()
            self.assertTrue(ok, msg)
            self._wait_idle('重试那一轮没有收尾')

    def test_a_source_that_keeps_failing_export_is_still_retried_next_time(self):
        # 第一轮:social 三条都抓到了,RSS 的 A 抓取失败;导出时 social 那边
        # 的位置被占住 → 真实 export 抛 FileExistsError。
        blocker = self.blocker()
        first = {}
        self.round_one(first, broken=[self.rss[0]], stages=('crawl', 'export'))
        self.run_export(self.both())
        self.assertEqual('error', self.stage('export')['status'],
                         '这一局的前提:导出阶段真失败了')
        pipeline_state._end_run()
        self.assertEqual([], self.exported('social'),
                         '这一局的前提:social 一篇都没落盘')

        # 第一次重试:crawl 只重抓 A(它成功了),export 照旧两个来源都跑,
        # 而 social 那个障碍还在 —— 它再次失败。
        self.retry_round({})
        self.assertEqual('error', self.stage('export')['status'],
                         '这一局的前提:第一次重试里 social 的导出仍然失败')
        self.assertEqual([], self.exported('social'))

        # 排除障碍,第二次重试。
        blocker.unlink()
        self.retry_round({})
        self.assertEqual(3, len(self.exported('social')),
                         'social 那几条文章一篇都没落盘,而面板说导出完成了')
        self.assertEqual('ok', self.stage('export')['status'])

    def test_a_plain_sync_resets_the_export_scope(self):
        """反过来钉一边:常规同步要把导出范围重写成**它自己**那一份。

        `_state` 是模块级常驻的,上一轮如果是重试,里面躺着的是那一轮的导出范围
        —— 不覆盖的话这一轮的落盘把它原样带走,下一次重试就按一份跟这一轮毫无
        关系的范围去导出。

        这一条**真走 `pipeline.start_pipeline`**,不用摆盘那个 `_begin`:要验的
        正是生产启动路径有没有重写这个字段,拿摆盘替它写一遍就把它挡在射程外了
        (第一版就是这么写的,注入"生产那一行去掉"全绿)。
        """
        self.round_one({}, broken=[self.rss[0]], stages=('crawl', 'export'))
        pipeline_state._stage('export', 'error', '导出失败')
        pipeline_state._end_run()
        self.retry_round({})                       # 这一轮的导出范围 = 两个来源

        with patch.object(pipeline, '_run_crawl'):
            self.assertTrue(pipeline.start_pipeline(
                ['crawl'], crawl_filter={'channel': 'rss', 'accounts': []}))
            self._wait_idle('常规同步那一轮没有收尾')
        saved = pipeline_state.load_last_run() or {}
        self.assertEqual(['rss'], [s['channel'] for s in
                                   pipeline_stages.crawl_channels(saved.get('export_filter'))],
                         '常规同步落盘的导出范围该是它自己选的那个来源')


class BrowserPrefetchScopeTests(MultiSourceBase):
    """登录态通道:用户真正付的代价在**预取**那一步,所以判据是它收到的名单。

    一个账号要滚时间线、必要时等懒加载,51 个账号一轮十几分钟;而这条通道在
    HTTP 那一侧压根不发请求(`fetch_source` 直接走 `take()` 取本轮缓存)——
    数 `fetch_source` 答不了"这一轮读了几个账号的时间线"。
    """

    def setUp(self):
        super().setUp()
        reg.save_settings('social', {'x_channel': x_channels.BROWSER})
        # 没真登录过的话这几条订阅会被判「待配置」、压根进不了 pending,预取
        # 收到的永远是空名单,这一局就什么都验不出来。
        p = patch.object(x_channels, 'readiness', return_value=(True, '', ''))
        p.start()
        self.addCleanup(p.stop)

    def harvest(self, seen, broken=()):
        """把预取和取用都换成记录型的,别的一行不打桩。"""
        bad = {s['id'] for s in broken}

        def prefetch(sources, settings, **kw):
            seen.append([reg.x_handle(s['url']) for s in sources])
            return {}

        def take(source, settings):
            if source['id'] in bad:
                raise ValueError('这个号的时间线打不开')
            return ([_entry(source['url'] + '/post')],
                    {'etag': '', 'modified': '', 'resolved_url': source['url']})
        return (patch.object(x_channels, 'prefetch', side_effect=prefetch),
                patch.object(x_channels, 'take', side_effect=take))

    def social_round(self, seen, broken=(), force=False):
        crawl_filter = {'channel': 'social', 'force': force}
        self._begin(crawl_filter)
        pre, take = self.harvest(seen, broken)
        with pre, take:
            pipeline_stages._run_crawl(crawl_filter)
        pipeline_state._end_run()

    def test_only_the_failed_handle_is_read_again(self):
        seen = []
        self.social_round(seen, broken=[self.x[1]])
        self.assertEqual([['x1', 'x2', 'x3']], seen, '这一局的前提:整批预取过')

        seen.clear()
        pre, take = self.harvest(seen)
        with pre, take, patch.object(pipeline_retry, '_run_export'), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             patch.object(pipeline_retry, '_run_projects'), \
             patch.object(pipeline_retry, '_run_wiki'):
            ok, msg = pipeline_retry.start_retry_failed()
            self.assertTrue(ok, msg)
            self._wait_idle('重试那一轮没有收尾')
        self.assertEqual([['x2']], seen,
                         '一个账号地址错了,不该把别人的时间线也重读一遍')

    def test_a_plain_sync_still_prefetches_everyone(self):
        """反过来钉一边:常规同步照旧整批预取。"""
        seen = []
        self.social_round(seen, broken=[self.x[1]])
        seen.clear()
        # `force`:失败给那条订阅记了退避(最短 1800 秒),常规同步会如实把它
        # 记成 deferred 跳过 —— 那是另一件事,不是"没收窄"。
        self.social_round(seen, force=True)
        self.assertEqual([['x1', 'x2', 'x3']], seen)


class PanelScopeLineTests(unittest.TestCase):
    """面板上那行「本次范围」:抠真代码丢给 node 求值。

    收窄发生在采集器里面,继承来的 `accounts` 仍然是空的 —— 照它判这一行会
    写上「全部订阅」,而那正是刚被修掉的那个行为;反过来给所有来源一律写
    「仅重试失败项」也是谎话,它承诺了报不出名单的那个来源并没有发生的收窄。
    """

    def scopes(self, filters):
        src = (JS / 'pipe_channels.js').read_text(encoding='utf-8')
        view = (JS / 'pipe_view.js').read_text(encoding='utf-8')
        # 参数名不写死:它从 `filter` 改成了整份运行结果(`result`),因为「这一轮
        # 作用在哪些来源上」还要看 planned_stages —— 只跑 index/extract 的那一轮
        # 不归任何来源。抠的仍是同一个函数。
        func = re.search(r'function pipeRunScopes\(\w+\) \{.*?\n\}', src, re.S)
        text = re.search(r'  const scopeText = item =>.*?\n  const scope = .*?\n',
                         view, re.S)
        self.assertTrue(func and text, '面板那两行代码的形状变了,这条用例得跟着改')
        script = f'''
{func.group(0)}
function channelLabel(id) {{ return {{social: '大V观点', rss: 'RSS'}}[id] || id; }}
const out = [];
for (const filter of {filters}) {{
  const scopes = pipeRunScopes(filter);
{text.group(0)}
  out.push(scope);
}}
console.log(JSON.stringify(out));
'''
        done = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)
        import json
        return json.loads(done.stdout)

    def test_a_library_round_belongs_to_no_source(self):
        """全库那一轮(只跑 index/extract)不归任何来源。

        `start_pipeline` **一律**会往 `crawl_filter` 里盖一个默认采集器名,照它念
        的话公众号页会显示「同步中…」、而真正参与这一轮的 RSS 反倒被说成别人的
        任务。判据在 `pipeRunScopes` 里,同步中心、公众号页、订阅页共用那一份。
        后端那一份同口径是 `pipeline_stages.run_scopes`。
        """
        self.assertEqual([''], self.scopes(
            """[{planned_stages: ['index', 'extract'],
                 crawl_filter: {channel: 'wechat_local', accounts: []}}]"""))
        # 反过来钉一边:抓取轮照旧报它自己的来源
        self.assertEqual(['全部订阅'], self.scopes(
            """[{planned_stages: ['crawl', 'export'],
                 crawl_filter: {channel: 'rss', accounts: []}}]"""))
        # 存量记录没有 planned_stages,照老样子摊平 —— 一律判成「不按来源」会让
        # 订阅页对所有旧运行记录都说不出是谁在跑
        self.assertEqual(['全部订阅'], self.scopes("[{channel: 'rss', accounts: []}]"))

    def test_the_line_says_what_each_source_actually_did(self):
        got = self.scopes('''[
          {channel: 'rss', accounts: ['7'], retry: 'targets'},
          {channel: 'wechat_local', accounts: ['号A', '号B'], retry: 'all'},
          {channel: 'social', accounts: [], retry: 'all'},
          {channel: 'social', accounts: []},
          {channel: 'rss', accounts: ['7', '9']},
          {channels: [{channel: 'social', accounts: [], retry: 'all'},
                      {channel: 'rss', accounts: ['7'], retry: 'targets'}]}
        ]''')
        self.assertEqual([
            '仅重试失败的 1 个订阅',
            # 收窄不了的来源跑的就是原范围,照原范围说
            '2 个指定订阅',
            '全部订阅',
            '全部订阅',
            '2 个指定订阅',
            '大V观点 全部订阅 · RSS 仅重试失败的 1 个订阅',
        ], got)
        self.assertNotIn('仅重试失败项', ''.join(got),
                         '那句话会把没发生的收窄也说成发生了')


class SourceScheduleNoteTests(unittest.TestCase):
    """订阅页那行「这一类来源什么时候自动同步」:抠真代码丢给 node 求值。

    **喂的是两个接口的真实返回包**,不是手写的扁平设置对象 —— 上一版就是把
    `/api/daily/settings` 的外层(`{settings, channels, default_channels}`)当设置
    读,`enabled` 永远取不到,于是开着的定时被一律说成「未启用」;而那一版的用例
    自己造了一个扁平对象喂进去,563 条回归一条都没红。

    **要问遍所有还在生效的定时**:页头那颗「定时同步」(pipeline_schedule)按要求
    暂留着,只看日报的话 B 开着时这里会说「不会自动同步」,跟页头那行当场打架。
    """

    def line(self, daily=None, pipe=None, kind='rss'):
        """按**两个接口各自的真实返回包**喂进去,从 `pipeJson` 这一层开始跑。

        解包那一下必须由产品代码做:上一版在测试脚本里自己写死
        `d.settings`,于是把「读错包形」注入进去照样全绿(红 0 条)—— 而那正是
        这条用例要挡的那个 bug。None = 那个接口读不到(走 catch 那条路)。
        """
        src = (JS / 'source_forms.js').read_text(encoding='utf-8')
        funcs = [re.search(rf'(?:async )?function {name}\([^)]*\) \{{.*?\n\}}', src, re.S)
                 for name in ('loadSourceScheduleNote', 'sourceScheduleHits',
                              'renderSourceScheduleNote')]
        self.assertTrue(all(funcs), '那几行代码的形状变了,这条用例得跟着改')
        script = f"""
let box = {{ textContent: '' }};
function $(id) {{ return id === 'source-settings-status' ? box : null; }}
const sourceKind = {kind!r};
const RESPONSES = {{ '/api/daily/settings': {daily or 'null'},
                     '/api/pipeline/schedule': {pipe or 'null'} }};
// 接口读不到时 `pipeJson` 是会抛的,这里照样抛 —— 那条 catch 分支也要被走到
function pipeJson(url) {{
  const body = RESPONSES[url];
  return body === null ? Promise.reject(new Error('读不到')) : Promise.resolve(body);
}}
let sourceSchedules = null;
{funcs[0].group(0)}
{funcs[1].group(0)}
{funcs[2].group(0)}
loadSourceScheduleNote().then(() => console.log(box.textContent));
"""
        done = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout.strip()

    # `/api/daily/settings` 的真实包形:设置在 settings 里,外层那个 channels 是
    # 可选来源目录(一堆 {id,label}),不是待同步名单 —— 拿它当名单也会判错。
    DAILY_ON = ("{settings: {enabled: true, sync: true, time: '09:00', channels: ['rss']},"
                " channels: [{id: 'rss', label: 'RSS'}, {id: 'social', label: '大V观点'}],"
                " default_channels: ['wechat_local', 'rss']}")
    # 页头那条:读得到、而且确实关着。**跟"读不到"是两件事**,所以要分开摆。
    PIPE_OFF = "{enabled: false, time: '07:30', channel: 'rss', channels: ['rss']}"

    def test_an_enabled_daily_schedule_is_not_reported_as_off(self):
        self.assertIn('每天 09:00 跟着日报那一轮同步', self.line(daily=self.DAILY_ON))
        self.assertNotIn('未启用', self.line(daily=self.DAILY_ON))

    def test_the_retained_head_schedule_counts_too(self):
        # A 关着、B 开着且范围含 RSS:页头写着「定时同步 · 每天 07:30」,这里不许
        # 同时说「目前不会自动同步」。
        off = "{settings: {enabled: false, sync: true, time: '09:00', channels: ['rss']}}"
        b_on = "{enabled: true, time: '07:30', channel: 'rss', channels: ['rss']}"
        got = self.line(daily=off, pipe=b_on)
        self.assertIn('每天 07:30 跟着页头的定时同步那一轮同步', got)
        self.assertNotIn('不会自动同步', got)
        # 反过来钉一边:B 开着但范围里没有这一类,照旧如实说不会
        b_other = "{enabled: true, time: '07:30', channel: 'wechat_local', channels: ['wechat_local']}"
        self.assertIn('不会自动同步', self.line(daily=off, pipe=b_other))

    def test_each_reason_for_no_automatic_sync_is_named(self):
        for daily, expect in (
                ("{settings: {enabled: false, sync: true, time: '09:00', channels: ['rss']}}",
                 '日报定时未启用'),
                ("{settings: {enabled: true, sync: false, time: '09:00', channels: ['rss']}}",
                 "没开「生成前先同步」"),
                ("{settings: {enabled: true, sync: true, time: '09:00', channels: ['social']}}",
                 '不在日报的同步范围里')):
            # **两条定时都读得到**才轮得到念原因(见下一条用例):少一条就是不确定
            got = self.line(daily=daily, pipe=self.PIPE_OFF)
            self.assertIn(expect, got)
            self.assertNotIn('每天 09:00 跟着日报', got)

    def test_one_unreadable_schedule_is_not_read_as_off(self):
        """读不到 ≠ 没开。**少一条就不许断言「不会自动同步」。**

        两态读法(读到一个就按已知的那部分说话)的后果:只中断页头那条的 GET,
        这行字就说「日报定时未启用,这一类来源目前不会自动同步」,而页头上明明
        写着「定时同步 · 每天 07:30」—— 用户看到的是两句互相打脸的话,而真相是
        这一类来源每天 07:30 真的会被同步。反过来(日报那条读不到)同形。
        """
        a_off = "{settings: {enabled: false, sync: true, time: '09:00', channels: ['rss']}}"
        for args, who in ((dict(daily=a_off), '页头的定时同步'),
                          (dict(pipe=self.PIPE_OFF), '日报')):
            got = self.line(**args)
            self.assertIn(f'读不到{who}的设置', got)
            self.assertIn('没法确定', got)
            # 不许替没读到的那条下结论(断言用确定句的字样:不确定那句本身就带着
            # 「会不会自动同步」,拿它当否定词永远红)
            self.assertNotIn('目前不会自动同步', got)
            self.assertNotIn('未启用', got)
        # 两条都读不到时同理,而且两条都点名
        both = self.line()
        self.assertIn('读不到日报、页头的定时同步的设置', both)
        self.assertNotIn('目前不会自动同步', both)
        # 反过来钉一边:两条都读到了、都没带上它,照旧给出确定的那句话和原因
        settled = self.line(daily=a_off, pipe=self.PIPE_OFF)
        self.assertIn('日报定时未启用，这一类来源目前不会自动同步', settled)
        self.assertNotIn('没法确定', settled)

    def test_a_hit_is_stated_even_when_the_other_schedule_is_unreadable(self):
        # 已知有一条带上它就够了:另一条读不到只可能再多一个同步时刻,不会把这个
        # 时刻取消掉。这里退回"不知道"等于把一句真话换成一句含糊话。
        got = self.line(daily=self.DAILY_ON)
        self.assertIn('每天 09:00 跟着日报那一轮同步', got)
        self.assertNotIn('没法确定', got)

    def test_manual_sync_is_always_true(self):
        for args in ({'daily': self.DAILY_ON}, {'pipe': self.PIPE_OFF}, {}):
            self.assertIn('手动同步随时可用', self.line(**args))


if __name__ == '__main__':
    unittest.main()
