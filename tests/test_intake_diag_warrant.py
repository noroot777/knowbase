# -*- coding: utf-8 -*-
"""**人确认过的那份描述,有没有一路跟到真正执行和真正写入的那一下。**

入口再读一次、再比一次关不掉那个窗口:比完之后那份描述若被丢掉、只把**编号**交
给执行器,执行器就会按编号重读一份**新**配置当成旧授权来跑 —— 审查实测:确认文字
点名来源 A、响应说「同步已开始」,传下去的范围只剩 `accounts=['id:117']`,而采集器
选中并访问了改指之后的 B(`checked=1`、`repointed=0`,一条拒绝都没有)。

三组,每一组都**两头钉**(①对不上就拒绝或作废 ②没变的那一下照旧跑):

- `SourceWarrantTests` —— 来源那一侧走**真实**的 `/scope` → `/run` → `start_sync`
  → `_run_crawl` → `sync.collect`:改指插在「校验全过了、`start_pipeline` 还没被
  调」那一瞬(那正是入口那道关不掉的窗口),断言**一个地址都没访问**。
- `ChannelWarrantTests` —— 确认框里那个下界、那张票,认的是这一轮**真正会用**的
  那条通道;两条通道都留过水位时不许报另一条的。
- `ItemLifeTests` —— 四个写入端点 + 占着管道的那份点名意图,各自把「人看着的那
  一世」AND 进自己的写语句:删掉重收之后旧请求一律写不进去。

**判据落在后果上**,不在「带了没带那个字段」:访问过谁、库里那一行是什么、放回了
几条。只问「描述带没带」的话,「带错了哪一份」恒绿。
"""
from __future__ import annotations

import json
from unittest.mock import patch

from kb import pipeline, pipeline_stages
from kb.collectors import FetchScope, feeds as feedcol
from kb.feeds import fulltext, registry as reg, sync, x_channels
from kb.intake_diag import shelf, sources, warrant
from kb.web import intake_api, intake_diag_api as api
from tests.intake_diag_case import DiagCase


class WarrantCase(DiagCase):
    """共用的脚手架:摆一条会出问题的订阅、走真实的两个端点。"""

    def failing(self, kind: str, suffix: str, **extra) -> dict:
        source = reg.save_subscription(kind, dict(
            name=suffix, url=f'https://{suffix}.example.org/feed.xml', **extra))
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET last_error='连接失败：读不到这个来源'"
                      ' WHERE id=?', (source['id'],))
        return source

    def problem_of(self, kind: str, source_id: int) -> dict:
        found = [one for one in sources.problems(kind) if one['source_id'] == source_id]
        self.assertEqual(1, len(found), '这条订阅此刻应当正好有一个来源级问题')
        return found[0]

    def plan_for(self, kind: str, problem: dict) -> dict:
        return api.scope(api.RunRequest(kind=kind, problem_id=problem['id']))

    def start(self, kind: str, problem: dict, plan: dict, meddle=None) -> dict:
        """真实 `/run`;`meddle` 在「校验全过了、还没占管道」那一瞬动手。"""
        captured: dict = {}

        def boundary(stages, **kwargs):
            captured['crawl_filter'] = kwargs['crawl_filter']
            if meddle:
                meddle()
            return True

        with patch.object(pipeline, 'start_pipeline', boundary):
            api.run(api.RunRequest(kind=kind, problem_id=problem['id'], plan=plan['plan']))
        return captured

    def crawl(self, crawl_filter: dict) -> tuple[list[str], dict]:
        """用那份范围驱动**真实**的抓取阶段,返回访问过谁 + 这一轮的账。"""
        accessed: list[str] = []
        summaries: list[dict] = []

        def fetch(source, settings, **kwargs):
            accessed.append(source['url'])
            return [], dict(etag='', modified='', resolved_url=source['url'])

        real = sync.collect

        def collect(kind, scope):
            out = real(kind, scope)
            summaries.append(out)
            return out

        with patch.object(sync, 'fetch_source', fetch), \
             patch.object(sync, 'collect', collect):
            pipeline_stages._run_crawl(crawl_filter)
        self.assertEqual(1, len(summaries))
        return accessed, summaries[0]


class SourceWarrantTests(WarrantCase):
    """改指插在入口那道之后、执行器读到之前。"""

    def setUp(self):
        super().setUp()
        self.source = self.failing('rss', 'source-a')
        self.problem = self.problem_of('rss', self.source['id'])
        self.plan = self.plan_for('rss', self.problem)

    def repoint(self) -> None:
        intake_api.update_source('rss', self.source['id'], intake_api.SubscriptionPatch(
            name='source-b', url='https://source-b.example.org/feed.xml'))

    def test_a_source_repointed_after_the_confirmation_is_never_visited(self):
        captured = self.start('rss', self.problem, self.plan, self.repoint)
        # 描述**随范围一起**交下去了(不是只有编号)。
        self.assertEqual({reg.id_target(self.source['id']): self.problem['sig']},
                         captured['crawl_filter']['expect'])
        # 改指之后那一行长什么样,由改指那一下自己决定(换链接会把采集进度连同
        # 旧错误一起清掉)—— 这里要钉的是**这一轮作废不许再去碰它**。
        before = dict(reg.subscription('rss', self.source['id']))
        accessed, summary = self.crawl(captured['crawl_filter'])
        self.assertEqual([], accessed)
        self.assertEqual(1, summary['stale'])
        # **作废不是失败,也不是「检查过」**:退避没动、水位没动,下一轮按新设置
        # 照常跑;而说「检查过」等于替改完之后那一条宣布它同步过了。
        self.assertEqual(0, summary['checked'])
        self.assertEqual([], summary['errors'])
        self.assertEqual(0, summary['repointed'])
        # 一条都不许去结清谁的旧失败(契约在 `kb.collectors.Coverage`)。
        self.assertEqual([], summary['succeeded_ids'])
        # **整行逐列对过**:退避、下界、成败时间、提示语一个字都没动。只盯某一列
        # 的话「这一轮顺手替它记了一次成功」那种形态照旧是绿的。
        self.assertEqual(before, dict(reg.subscription('rss', self.source['id'])))

    def test_the_same_round_visits_it_when_nothing_changed(self):
        """**反过来钉一边**:什么都没改,这一轮照旧去读它。"""
        captured = self.start('rss', self.problem, self.plan)
        accessed, summary = self.crawl(captured['crawl_filter'])
        self.assertEqual([self.source['url']], accessed)
        self.assertEqual(0, summary['stale'])
        self.assertEqual(1, summary['checked'])

    def test_an_entry_without_any_confirmation_still_runs(self):
        """日常同步、定时那一轮、CLI 不带描述 —— 它们点的就是「现在这几条」。"""
        accessed, summary = [], None
        scope = FetchScope(targets=(reg.id_target(self.source['id']),), force=True)

        def fetch(source, settings, **kwargs):
            accessed.append(source['url'])
            return [], dict(etag='', modified='', resolved_url=source['url'])

        self.repoint()
        with patch.object(sync, 'fetch_source', fetch):
            summary = sync.collect('rss', scope)
        self.assertEqual(['https://source-b.example.org/feed.xml'], accessed)
        self.assertEqual(0, summary['stale'])

    def test_the_run_is_refused_outright_when_the_description_cannot_be_signed(self):
        """描述算不出来时**一律拒**,不许退化成「按编号跑」。"""
        with patch.object(warrant, 'source_sig', return_value=''):
            problem = self.problem_of('rss', self.source['id'])
            plan = self.plan_for('rss', problem)
            started: list[dict] = []
            with patch.object(pipeline, 'start_pipeline',
                              lambda *a, **k: started.append(k) or True):
                with self.assertRaises(Exception) as caught:
                    api.run(api.RunRequest(kind='rss', problem_id=problem['id'],
                                           plan=plan['plan']))
        self.assertEqual(409, getattr(caught.exception, 'status_code', 0))
        self.assertEqual([], started)


class ChannelWarrantTests(WarrantCase):
    """X 那条:确认框里的下界和那张票,认的是这一轮真正会用的通道。"""

    def setUp(self):
        super().setUp()
        # X 通道在测试环境里一律「待配置」(没登录过),而那一道和这里要验的事
        # 无关 —— 两侧读的是同一个 `configuration_error`,所以描述照旧一致。
        patched = patch.object(reg, 'configuration_error', lambda *a, **k: '')
        patched.start()
        self.addCleanup(patched.stop)
        self.source = reg.save_subscription('social', {
            'name': 'X 账号（样例）', 'url': 'https://x.com/sample'})
        with reg.db() as c:
            # **两条通道都留过水位**:浏览器到分钟、feedgrab 只到天。
            c.execute('UPDATE subscriptions SET last_note=?,config=? WHERE id=?',
                      ('滚动次数用完，没有读到上次的位置',
                       json.dumps({'x_watermark_browser': '2026-10-25 09:30',
                                   'x_watermark_feedgrab': '2026-10-01'}),
                       self.source['id']))

    def use(self, channel: str) -> None:
        reg.save_settings('social', {'x_channel': channel})

    def test_the_floor_on_screen_is_the_one_this_round_would_use(self):
        self.use('browser')
        on_browser = self.problem_of('social', self.source['id'])
        self.assertEqual('2026-10-25 09:30', on_browser['since'])
        self.assertEqual('browser', on_browser['channel'])
        self.use('feedgrab')
        on_feedgrab = self.problem_of('social', self.source['id'])
        self.assertEqual('2026-10-01', on_feedgrab['since'])
        # **和真跑那一轮取的是同一个值**(`x_channels.prefetch` 按当前通道取)。
        self.assertEqual(
            x_channels.watermark(reg.subscription('social', self.source['id']),
                                 x_channels.channel_of(reg.get_settings('social'))),
            on_feedgrab['since'])
        self.assertNotEqual(on_browser['sig'], on_feedgrab['sig'])

    def test_switching_channel_invalidates_the_open_confirmation(self):
        self.use('browser')
        problem = self.problem_of('social', self.source['id'])
        plan = self.plan_for('social', problem)
        self.use('feedgrab')
        started: list[dict] = []
        with patch.object(pipeline, 'start_pipeline',
                          lambda *a, **k: started.append(k) or True):
            with self.assertRaises(Exception) as caught:
                api.run(api.RunRequest(kind='social', problem_id=problem['id'],
                                       plan=plan['plan']))
        self.assertEqual(409, getattr(caught.exception, 'status_code', 0))
        self.assertEqual([], started)

    def test_the_same_confirmation_runs_when_the_channel_stays_put(self):
        """**反过来钉一边。**"""
        self.use('browser')
        problem = self.problem_of('social', self.source['id'])
        plan = self.plan_for('social', problem)
        captured = self.start('social', problem, plan)
        self.assertEqual({reg.id_target(self.source['id']): problem['sig']},
                         captured['crawl_filter']['expect'])


class ItemLifeTests(DiagCase):
    """删掉重收之后,旧表单 / 旧意图那一下写不进新一世。"""

    def setUp(self):
        super().setUp()
        self.source = reg.save_subscription('rss', {
            'name': '一条订阅（样例）', 'url': 'https://life.example.org/feed.xml',
            'fulltext': True})
        self.url = 'https://article.example.org/life'
        self.key = self.put(self.source, self.url, body='几十个字的跳转壳。')

    def reborn(self) -> str:
        """删掉重收:同一个链接、同一个 key,**新的一世**。"""
        with reg.db() as c:
            c.execute('DELETE FROM subscription_items WHERE item_key=?', (self.key,))
            c.execute('DELETE FROM feed_items WHERE key=?', (self.key,))
        reg.put_items(self.source, [{'url': self.url, 'title': '一条样例',
                                     'body': '几十个字的跳转壳。',
                                     'published': '2026-10-08 09:00',
                                     'content_kind': 'summary'}])
        return self.row(self.key)['life']

    def exhaust(self) -> None:
        with reg.db() as c:
            c.execute('UPDATE feed_items SET fulltext_tries=3 WHERE key=?', (self.key,))

    def test_a_named_retry_intent_does_not_reopen_the_next_life(self):
        """「立即执行」那份意图要等占住管道、跑到导出阶段才被消费。"""
        self.exhaust()
        before = self.row(self.key)['life']
        captured: dict = {}
        with patch.object(pipeline, 'start_pipeline',
                          lambda *a, **k: captured.update(k) or True):
            from kb.intake_diag import actions
            actions.run('rss', [self.key])
        self.assertEqual({self.key: before}, captured['crawl_filter']['fulltext_retry']['lives'])
        after = self.reborn()
        self.exhaust()
        self.assertNotEqual(before, after)
        with patch('kb.pipeline_state.get_state',
                   return_value={'crawl_filter': captured['crawl_filter']}):
            retry = feedcol.FeedCollector('rss')._body_retry()
            opened = fulltext.reopen([str(one) for one in retry['keys']], 'rss',
                                     lives=retry.get('lives'))
        self.assertEqual({'released': 0, 'body': 0}, opened)
        self.assertEqual(3, self.row(self.key)['fulltext_tries'])
        # **反过来钉一边**:同一份意图按新那一世就放得回去。
        self.assertEqual(1, fulltext.reopen([self.key], 'rss',
                                            lives={self.key: after})['body'])

    def test_requeue_with_the_life_the_page_showed_does_not_touch_the_next_life(self):
        self.exhaust()
        stale = self.row(self.key)['life']
        fresh = self.reborn()
        self.exhaust()
        asked = api.requeue(api.Batch(kind='rss', keys=[self.key], lives={self.key: stale}))
        self.assertEqual(0, asked['requeued'])
        self.assertEqual(3, self.row(self.key)['fulltext_tries'])
        self.assertEqual(1, api.requeue(api.Batch(kind='rss', keys=[self.key],
                                                  lives={self.key: fresh}))['requeued'])

    def test_a_shelving_decision_does_not_land_on_the_next_life(self):
        stale = self.row(self.key)['life']
        fresh = self.reborn()
        asked = api.shelve(api.ShelfRequest(kind='rss', keys=[self.key],
                                            decision='exclude', lives={self.key: stale}))
        self.assertEqual(0, asked['shelved'])
        self.assertTrue(shelf.admitted(self.key))
        self.assertEqual(1, api.shelve(api.ShelfRequest(
            kind='rss', keys=[self.key], decision='exclude',
            lives={self.key: fresh}))['shelved'])

    def test_a_relink_does_not_land_on_the_next_life(self):
        stale = self.row(self.key)['life']
        fresh = self.reborn()
        with self.assertRaises(Exception) as caught:
            api.relink(api.LinkRequest(kind='rss', key=self.key, life=stale,
                                       url='https://other.example.org/real'))
        self.assertEqual(400, getattr(caught.exception, 'status_code', 0))
        self.assertEqual('', self.row(self.key)['fulltext_url'])
        self.assertEqual('https://other.example.org/real', api.relink(api.LinkRequest(
            kind='rss', key=self.key, life=fresh,
            url='https://other.example.org/real'))['url'])

    def test_a_pasted_body_does_not_land_on_the_next_life(self):
        text = '这是人工粘进来的一整段正文，它要过得了那道最小长度门槛。' * 40
        stale = self.row(self.key)['life']
        fresh = self.reborn()
        with self.assertRaises(Exception) as caught:
            api.paste_body(api.BodyRequest(key=self.key, body=text, life=stale))
        self.assertEqual(400, getattr(caught.exception, 'status_code', 0))
        self.assertEqual('', self.row(self.key)['fulltext_at'])
        self.assertGreater(api.paste_body(api.BodyRequest(
            key=self.key, body=text, life=fresh))['characters'], 0)

    def test_a_request_without_any_life_still_goes_through(self):
        """**反过来钉一边**:存量页面、脚本直调不带那一世 —— 一律拒会让它们跑不起来。"""
        self.exhaust()
        self.assertEqual(1, api.requeue(api.Batch(kind='rss', keys=[self.key]))['requeued'])
