"""Isolated source lifecycle, ingestion, scheduling and search regressions."""
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import maintenance, pipeline, pipeline_state
from kb.collectors import FetchScope, get_collector
from kb.feeds import registry as reg, fetching, sync, schedule
from kb.source_types import feed_types
from kb.web import intake_api

# 固定日期会让这套测试在几天后自己变红:新订阅只收最近 `reg.HISTORY_DAYS` 天
# (见 sync._within_history),写死 2026-09-28 的样本过几天就被那道闸挡住,而红
# 的是一堆跟历史下界毫无关系的用例。所以样本日期跟着今天走,断言也用同一个值。
_FRESH = datetime.now(timezone.utc) - timedelta(days=1)
# published 由 fetching.parse_feed 按 Asia/Shanghai 格式化,断言要用同一个时区
# 换算出来的日历日,否则每天有 8 小时窗口对不上。
DAY = _FRESH.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
RSS = ('''<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>
<item><guid>id-1</guid><title>Agent notebook</title><link>https://example.org/post?utm_source=rss</link>
<description><![CDATA[<p>agent notebook knowledge</p><script>danger()</script>]]></description>
<pubDate>%s</pubDate></item></channel></rss>''' % format_datetime(_FRESH)).encode()
ATOM = ('''<feed xmlns="http://www.w3.org/2005/Atom"><title>Notes</title>
<entry><id>urn:post:1</id><title>Short thought</title><link href="https://nitter.net/Example/status/123#m"/>
<updated>%s</updated><author><name>Example</name></author>
<content type="html">&lt;p&gt;A thought&lt;/p&gt;</content></entry></feed>'''
        % _FRESH.strftime('%Y-%m-%dT%H:%M:%SZ')).encode()


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # 维护锁的 lockfile 在 DATA_DIR 下:不指到 tmp 的话这些用例会去 flock 生产
        # 那一个,被 kb-web 正在跑的一轮索引(整趟持锁)挂住(见 kb.maintenance)。
        self.patches = [patch.object(reg,'DB_PATH',self.root/'feeds.db'),
                        patch.object(maintenance,'DATA_DIR',self.root),
                        patch.object(sync,'VAULT_ROOT',self.root/'vault')]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        self.state = deepcopy(pipeline_state._state)
        pipeline_state._state.update(running=False,stop_requested=False)
        self.addCleanup(lambda: pipeline_state._state.update(self.state))
        app = FastAPI(); app.include_router(intake_api.router)
        self.client = TestClient(app)

    def source(self, url='https://example.org/feed.xml', name='Test', kind='rss'):
        return reg.save_subscription(kind,{'name':name,'url':url})

    def entries(self):
        return fetching.parse_feed(RSS,'https://example.org/feed.xml')

    def test_defaults_once_no_youtube_or_blog_pages(self):
        self.assertEqual(51,len(reg.subscriptions('social')))
        self.assertEqual(9,len(reg.subscriptions('rss')))
        first = reg.subscriptions('social')[0]
        reg.delete_subscription('social',first['id'])
        self.assertEqual(50,len(reg.subscriptions('social')))
        self.assertFalse(reg.get_settings('rss')['enabled'])
        data = self.client.get('/api/intake/modules').json()['modules']
        # 模块清单跟着注册表走,不写死 —— 这条用例钉的是「defaults 只跑一次、
        # 没有 youtube 和博客页」,跟来源总数无关。
        self.assertEqual({'wechat',*feed_types()},{d['id'] for d in data})

    def test_normalized_duplicate_edit_and_disable_survive_reconnect(self):
        s = self.source('https://twitter.com/NewPerson/with_replies',kind='social')
        self.assertEqual('https://x.com/newperson',s['url'])
        with self.assertRaises(ValueError):
            self.source('https://x.com/NEWPERSON',kind='social')
        seed = next(s for s in reg.subscriptions('social') if 'mrlarus' in s['url'])
        with self.assertRaises(ValueError):
            self.source('https://twitter.com/MrLarus',kind='social')
        s = reg.save_subscription('social',{'name':'Edited','enabled':False},s['id'])
        self.assertEqual('Edited',s['name']); self.assertFalse(s['enabled'])
        self.assertTrue(seed['enabled'])

    def test_unsafe_urls_and_tweet_links_are_rejected(self):
        for url in ['javascript:alert(1)','file:///etc/passwd','http://127.0.0.1/rss',
                    'http://[::1]/rss','https://user:password@example.org/feed','http://localhost/rss',
                    'https://x.com/name/status/123','https://x.com/home']:
            with self.subTest(url=url),self.assertRaises(ValueError):
                self.source(url,kind='social')

    def test_dns_private_and_redirect_private_rejected(self):
        with patch.object(fetching.socket,'getaddrinfo',return_value=[(2,1,6,'',('10.0.0.1',443))]):
            with self.assertRaises(fetching.FeedError): fetching.public_url('https://example.org/rss')
        # Public URL validation itself is applied for every redirect in request().
        with patch.object(fetching,'public_addresses',side_effect=[('https://example.org/rss',['8.8.8.8']),fetching.FeedError('private')]), \
             patch.object(fetching.httpx,'Client') as client:
            client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value = Mock(
                status_code=302,headers={'location':'http://127.0.0.1/private'})
            with self.assertRaises(fetching.FeedError): fetching.request('https://example.org/rss')

    def test_rss_atom_sanitization_dates_and_social_permalink(self):
        e = self.entries()[0]
        self.assertEqual('https://example.org/post',e['url'])
        self.assertNotIn('danger',e['body'])
        self.assertIn(DAY,e['published'])
        self.assertEqual('summary',e['content_kind'])
        atom = fetching.parse_feed(ATOM,'https://example.org/rss',social=True)[0]
        self.assertEqual('https://x.com/example/status/123',atom['url'])
        self.assertEqual('Example',atom['author'])
        self.assertEqual('post',atom['content_kind'])
        with self.assertRaises(fetching.FeedError): fetching.parse_feed(b'<html>Login required</html>','https://example.org')
        self.assertEqual([],fetching.parse_feed(b'<rss version="2.0"><channel><title>Empty</title></channel></rss>','https://example.org'))

    def test_dedupe_across_aggregate_and_author_delete_preserves_archive(self):
        a=self.source(); b=self.source('https://other.org/feed')
        self.assertEqual(1,reg.put_items(a,self.entries()))
        self.assertEqual(0,reg.put_items(a,self.entries()))
        self.assertEqual(0,reg.put_items(b,self.entries()))
        self.assertEqual(1,reg.items('rss',b['id'])['total'])
        reg.delete_subscription('rss',a['id'])
        self.assertEqual(1,reg.items('rss')['total'])
        self.assertEqual(1,reg.items('rss',b['id'])['total'])

    def test_url_change_resets_validators_but_keeps_old_archive(self):
        s=self.source(); reg.put_items(s,self.entries())
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET etag='old',last_success='old',last_error='error',retry_at=99999 WHERE id=?",(s['id'],))
        new=reg.save_subscription('rss',{'url':'https://example.org/new.xml'},s['id'])
        for key in ['etag','last_error','last_success']:
            self.assertEqual('',new[key])
        self.assertEqual(0,reg.items('rss',s['id'])['total'])
        self.assertEqual(1,reg.items('rss')['total'])

    def test_etag_304_and_bridge_fallback(self):
        s=self.source(); s.update(etag='v1',modified='date',resolved_url=s['url'])
        with patch.object(fetching,'request',return_value=fetching.Response(304,s['url'],b'',{})) as req:
            entries,_=fetching.fetch_source(s,reg.get_settings('rss'))
            self.assertEqual([],entries)
            self.assertEqual('v1',req.call_args.args[1]['If-None-Match'])
        s=self.source('https://x.com/example',kind='social')
        with patch.object(fetching,'request',side_effect=[fetching.FeedError('blocked'),fetching.Response(200,'https://xcancel.com/example/rss',ATOM,{})]) as req:
            entries,_=fetching.fetch_source(s,{'bridges':['https://nitter.net/{handle}/rss','https://xcancel.com/{handle}/rss']})
            self.assertEqual(2,req.call_count); self.assertEqual(1,len(entries))
            self.assertEqual(req.call_args_list[0].kwargs['deadline'], req.call_args_list[1].kwargs['deadline'])

    def test_homepage_discovers_only_advertised_rss(self):
        s=self.source('https://example.org/')
        html=b'<html><link rel="alternate" type="application/rss+xml" href="/feed.xml"></html>'
        with patch.object(fetching,'request',side_effect=[fetching.Response(200,s['url'],html,{}),fetching.Response(200,'https://example.org/feed.xml',RSS,{'etag':'v2'})]):
            entries,cache=fetching.fetch_source(s,reg.get_settings('rss'))
        self.assertEqual(1,len(entries)); self.assertEqual('https://example.org/feed.xml',cache['resolved_url'])

    def test_failure_isolation_backoff_manual_retry_and_disabled(self):
        a=self.source(); b=self.source('https://other.org/feed'); disabled=self.source('https://paused.org/feed')
        reg.save_subscription('rss',{'enabled':False},disabled['id'])
        scope=FetchScope(targets=tuple(str(s['id']) for s in [a,b,disabled]))
        good=(self.entries(),{'etag':'v1','modified':'','resolved_url':a['url']})
        def response(source, *args, **kwargs):
            if source['id'] == a['id']:
                raise fetching.FeedError('blocked')
            return good
        with patch.object(sync,'fetch_source',side_effect=response) as fetch:
            summary=sync.collect('rss',scope)
            self.assertEqual(2,fetch.call_count); self.assertEqual(1,summary['added']); self.assertEqual(1,len(summary['errors']))
        self.assertEqual('',reg.subscription('rss',a['id'])['last_success'])
        with patch.object(sync,'fetch_source',return_value=good) as fetch:
            summary=sync.collect('rss',FetchScope(targets=(str(a['id']),)))
            fetch.assert_not_called(); self.assertEqual(1,summary['deferred'])
            sync.collect('rss',FetchScope(targets=(str(a['id']),),force=True))
            self.assertEqual('',reg.subscription('rss',a['id'])['last_error'])

    # ---- 新订阅的历史下界(sync._within_history / reg.history_floor) ----
    # 背景:一个刚添加的源常常把整个存档一次交出来(实测 Hugging Face Blog 的
    # feed 一口气给了 500 条、最早到 2024-04-10)。这些旧文随后逐批翻译、逐批
    # 重新导出,每重新导出一次 indexed_at 就刷新一次,于是连着十几天以"补录"
    # 的形态占满当天日报(2026-10-01 那份 46 条素材里补录占了 30 条)。

    def fed(self, source, entries, etag='v1'):
        """把一份 feed 交给 collect,返回这一轮新增几条。"""
        with patch.object(sync,'fetch_source',
                          return_value=(entries,{'etag':etag,'modified':'','resolved_url':source['url']})):
            return sync.collect('rss',FetchScope(targets=(str(source['id']),),force=True))['added']

    def aged(self, days, url=None):
        """一条 days 天前发布的条目。"""
        day = (datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=days)).isoformat()
        return {**self.entries()[0],'url':url or f'https://example.org/p{days}',
                'published':f'{day} 08:00'}

    def floor_of(self, source):
        return reg.subscription('rss',source['id'])['history_floor']

    def test_new_subscription_takes_only_recent_history(self):
        s=self.source()
        self.assertEqual(reg.history_floor(),self.floor_of(s))
        # 下界含当天:正好第 HISTORY_DAYS 天的那条要收,再老一天的不收。
        edge=self.aged(reg.HISTORY_DAYS); stale=self.aged(reg.HISTORY_DAYS+1); ancient=self.aged(800)
        self.assertEqual(2,self.fed(s,[*self.entries(),edge,stale,ancient]))
        kept={i['url'] for i in reg.items('rss',s['id'],limit=50)['items']}
        self.assertEqual({'https://example.org/post',edge['url']},kept)

    def test_history_floor_still_applies_after_the_first_fetch(self):
        """只拦"第一次抓取"挡不住任何事。

        第一次之后 etag 命中 304、什么都不返回;等源站下次更新,整份 feed(连同
        那 500 条存档)又回来了——那时"首次导入"的闸门早就撤了,存档照样整批
        进来。所以判据是一个固定日期,与抓第几次无关。
        """
        s=self.source()
        self.assertEqual(1,self.fed(s,self.entries()))
        s=reg.subscription('rss',s['id'])
        self.assertEqual(0,self.fed(s,[self.aged(400),self.aged(90)],etag='v2'))
        self.assertEqual(1,reg.items('rss',s['id'])['total'])

    def test_history_floor_is_fixed_so_outage_recovery_keeps_its_backlog(self):
        """下界是钉死的日期,不是"距今 N 天"的滑动窗口。

        滑动窗口会在源站停更(或我们这边挂了)几天之后恢复时,把那几天的内容
        静默丢掉——而那正是最该补回来的一批。
        """
        s=self.source()
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET history_floor=? WHERE id=?",
                      ((datetime.now(ZoneInfo('Asia/Shanghai')).date()-timedelta(days=60)).isoformat(),s['id']))
        s=reg.subscription('rss',s['id'])
        self.assertEqual(2,self.fed(s,[self.aged(9),self.aged(30)]))

    def test_subscriptions_without_a_floor_are_unrestricted(self):
        """老库里的订阅补成空串 = 不设下界,行为逐字不变。

        它们的存档早就抓完了,回头给它们钉一个下界只会在源站补发旧文时静默
        丢内容,挡不住任何已经发生的事。
        """
        s=self.source()
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET history_floor='' WHERE id=?",(s['id'],))
        s=reg.subscription('rss',s['id'])
        self.assertEqual(2,self.fed(s,[self.aged(800),self.aged(1)]))

    def test_undated_entries_pass_the_floor(self):
        """没有发布时间的条目照收:它身上没有任何"很旧"的证据。

        丢掉是不可恢复的静默损失,而日报那边本来就按"补录"接住这一档。
        """
        self.assertEqual(1,self.fed(self.source(),
                                    [{**self.entries()[0],'url':'https://example.org/undated','published':''}]))

    def test_ranged_import_bypasses_the_floor(self):
        """区间补抓是用户点名要的那段日期,是这道闸的逃生口。"""
        s=self.source(); old=self.aged(400)
        with patch.object(sync,'fetch_source',
                          return_value=([old],{'etag':'v1','modified':'','resolved_url':s['url']})):
            day=old['published'][:10]
            sync.collect('rss',FetchScope(targets=(str(s['id']),),since=day,until=day,force=True))
        self.assertEqual(1,reg.items('rss',s['id'])['total'])

    def test_url_change_recomputes_the_floor(self):
        """换了链接 = 这个订阅从头开始。

        指向一个全新的源时,旧订阅那个两年前的下界等于没有下界。
        """
        s=self.source()
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET history_floor='2024-01-01' WHERE id=?",(s['id'],))
        reg.save_subscription('rss',{'url':'https://example.org/other.xml'},s['id'])
        self.assertEqual(reg.history_floor(),self.floor_of(s))

    # 存量清理那个脚本(scripts/feed_trim_history.py)的回归都在
    # tests/test_feed_trim.py:它和这道闸互为补集,但自己还有共享条目、中途
    # 失败续跑、和同步互斥那几摊事,挤在这个文件里会越过 700 行上限。

    def test_range_does_not_cache_away_out_of_range_entries(self):
        s=self.source()
        entries=self.entries()+[{**self.entries()[0],'url':'https://example.org/old','published':'2026-08-01 08:00'}]
        with patch.object(sync,'fetch_source',return_value=(entries,{'etag':'new','modified':'','resolved_url':s['url']})):
            sync.collect('rss',FetchScope(targets=(str(s['id']),),since=DAY,until=DAY))
        self.assertEqual(1,reg.items('rss')['total'])
        self.assertEqual('',reg.subscription('rss',s['id'])['etag'])

    def test_export_atomic_repeat_and_index_search(self):
        from kb import scanner, store
        from kb.config import Source
        from kb.chunker import split_chunks
        from kb.search import _fts_leg, Filters
        s=self.source(); reg.put_items(s,self.entries()); sync.export('rss'); sync.export('rss')
        vault=self.root/'vault'
        self.assertEqual(1,len(list(vault.rglob('*.md'))))
        with patch.object(scanner,'VAULT_ROOT',vault),patch.object(scanner,'SOURCES',[Source('rss',vault/'source/rss')]):
            docs=list(scanner.scan_all())
        self.assertEqual(1,len(docs)); self.assertEqual('rss',docs[0].source)
        conn=store.connect(self.root/'index.db')
        try:
            store.upsert_document(conn,docs[0],split_chunks(docs[0].body),reg.now());conn.commit()
            found=_fts_leg(conn,'notebook',Filters(source='rss',until=DAY))
            self.assertEqual(1,len(found))
            self.assertEqual([], _fts_leg(conn,'notebook',Filters(source='social')))
        finally: conn.close()

    def test_api_crud_errors_and_scoped_sync(self):
        response=self.client.post('/api/intake/rss/sources',json={'name':'Custom','url':'https://example.org/feed'})
        self.assertEqual(201,response.status_code); s=response.json()
        self.assertEqual(400,self.client.post('/api/intake/rss/sources',json={'name':'Duplicate','url':s['url']}).status_code)
        response=self.client.patch(f"/api/intake/rss/sources/{s['id']}",json={'name':'New'})
        self.assertEqual('New',response.json()['name'])
        with patch.object(pipeline,'start_pipeline',return_value=True) as start:
            self.assertEqual(200,self.client.post('/api/intake/rss/sync',json={'source_ids':[s['id']]}).status_code)
            self.assertEqual(['crawl','export','index'],start.call_args.args[0])
            self.assertEqual([str(s['id'])],start.call_args.kwargs['crawl_filter']['accounts'])
        with patch.object(pipeline,'start_pipeline',return_value=False):
            self.assertEqual(409,self.client.post('/api/intake/rss/sync',json={}).status_code)
        self.assertEqual(404,self.client.get('/api/intake/missing').status_code)
        self.assertEqual(422,self.client.get('/api/intake/rss/items?offset=-1').status_code)
        self.assertEqual(200,self.client.delete(f"/api/intake/rss/sources/{s['id']}").status_code)
        self.assertEqual(404,self.client.delete(f"/api/intake/rss/sources/{s['id']}").status_code)

    def test_active_run_blocks_mutation(self):
        pipeline_state._state.update(running=True,crawl_filter={'channel':'rss'})
        response=self.client.post('/api/intake/rss/sources',json={'name':'x','url':'https://example.org/feed'})
        self.assertEqual(409,response.status_code)

    def test_schedule_busy_retries_and_modules_keep_independent_settings(self):
        reg.save_settings('rss',{'enabled':True,'interval_minutes':120,'next_run':10})
        reg.save_settings('social',{'enabled':True,'interval_minutes':30,'next_run':20,
                                  'bridges':['https://example.org/{handle}/rss']})
        start=Mock(return_value=False)
        self.assertIsNone(schedule.tick(start,current_time=100))
        self.assertEqual(10,reg.get_settings('rss')['next_run'])
        start.return_value=True
        self.assertEqual('rss',schedule.tick(start,current_time=100))
        self.assertEqual(7300,reg.get_settings('rss')['next_run'])
        self.assertEqual(20,reg.get_settings('social')['next_run'])
        self.assertEqual('social',schedule.tick(start,current_time=100))

    def test_a_stored_legacy_interval_is_migrated_to_the_new_default(self):
        """改 `SourceType.interval_minutes` **只影响没有设置行的新库**。

        已经保存过的那份 JSON 在 `get_settings` 里盖在默认值上面,面板保存通道时
        又把旧间隔原样带回去 —— 于是「social 改成每天一次」对所有老用户都没有
        发生(2026-10-08 审查实测:存量 60 的设置行,真跑一次 `tick` 仍然安排
        一小时后再来)。所以迁移按 `settings_rev` 做,读的时候归一。
        """
        with reg.db() as c:          # 旧版真实存下来的形状,没有 settings_rev
            c.execute('INSERT OR REPLACE INTO feed_settings VALUES(?,?)',
                      ('social', json.dumps({'enabled': True, 'interval_minutes': 60,
                                             'next_run': 0, 'bridges':
                                             ['https://example.org/{handle}/rss']})))
        self.assertEqual(1440, reg.get_settings('social')['interval_minutes'])
        reg.save_subscription('social', {'name': 'Someone',
                                         'url': 'https://x.com/Someone'})
        start = Mock(return_value=True)
        self.assertEqual('social', schedule.tick(start, current_time=100))
        self.assertEqual(100 + 1440 * 60, reg.get_settings('social')['next_run'])

    def test_an_interval_the_user_picked_himself_is_left_alone(self):
        """只迁"仍然等于旧默认值"的那些。升级之后他自己挑的值也不许被再迁一次
        —— `save_settings` 把 `settings_rev` 一起存下去,所以盖过章就不再动。"""
        with reg.db() as c:
            c.execute('INSERT OR REPLACE INTO feed_settings VALUES(?,?)',
                      ('social', json.dumps({'enabled': False, 'interval_minutes': 180,
                                             'next_run': 0})))
        self.assertEqual(180, reg.get_settings('social')['interval_minutes'])
        reg.save_settings('social', {'interval_minutes': 60})
        self.assertEqual(60, reg.get_settings('social')['interval_minutes'])

    def test_other_kinds_keep_their_own_interval(self):
        reg.save_settings('rss', {'enabled': False, 'interval_minutes': 60})
        self.assertEqual(60, reg.get_settings('rss')['interval_minutes'])

    def test_collector_registration_and_manual_scope_force(self):
        from kb import pipeline_stages
        self.assertEqual('rss',get_collector('rss').name)
        self.assertEqual('social',get_collector('social').name)
        with patch.object(sync,'collect') as collect:
            pipeline_stages._run_crawl({'channel':'rss','force':True,'accounts':['1']})
        self.assertTrue(collect.call_args.args[1].force)

    def test_wechat_daily_schedule_is_not_lost_while_feeds_run(self):
        saved = {'enabled':True,'time':'00:00','last_fired':'','channel':'wechat_local'}
        with patch.object(pipeline,'load_schedule',return_value=saved), \
             patch.object(pipeline,'_mark_fired') as mark, \
             patch.object(pipeline,'start_pipeline',return_value=False) as start, \
             patch.object(schedule,'tick') as feed_tick:
            pipeline._scheduler_tick()
            mark.assert_not_called()
            feed_tick.assert_called_once()
            start.return_value=True
            pipeline._scheduler_tick()
            mark.assert_called_once()
            self.assertEqual('wechat_local',start.call_args.kwargs['crawl_filter']['channel'])

    def test_default_social_waits_for_configuration_without_starting_pipeline(self):
        overview = self.client.get('/api/intake/social').json()
        self.assertEqual((51, 0), (overview['unconfigured'], overview['ready']))
        self.assertEqual([], overview['settings']['bridges'])
        self.assertFalse(get_collector('social').status().ready)
        with patch.object(pipeline, 'start_pipeline') as start:
            response = self.client.post('/api/intake/social/sync', json={})
            self.assertEqual(400, response.status_code)
            self.assertIn('待配置', response.json()['detail'])
            start.assert_not_called()
        with patch.object(sync, 'fetch_source') as fetch:
            summary = sync.collect('social', FetchScope(force=True))
            self.assertEqual(51, summary['unconfigured'])
            fetch.assert_not_called()
        reg.save_settings('social', {'enabled':True, 'next_run':0})
        start = Mock(return_value=True)
        self.assertIsNone(schedule.tick(start, current_time=100))
        start.assert_not_called()
        self.assertFalse(any(s['last_error'] for s in reg.subscriptions('social')))

    def test_dedicated_feed_and_saved_bridges_remain_usable(self):
        source = reg.subscriptions('social')[0]
        reg.save_subscription('social', {'feed_url':'https://example.org/account/rss'}, source['id'])
        data = self.client.get('/api/intake/social').json()
        self.assertEqual((50, 1), (data['unconfigured'], data['ready']))
        with patch.object(pipeline, 'start_pipeline', return_value=True):
            self.assertEqual(200, self.client.post('/api/intake/social/sync', json={'source_ids':[source['id']]}).status_code)
        reg.save_settings('social', {'bridges':['https://custom.org/{handle}/rss']})
        self.assertEqual(['https://custom.org/{handle}/rss'], reg.get_settings('social')['bridges'])
        self.assertEqual(51, self.client.get('/api/intake/social').json()['ready'])
        reg.save_settings('social', {'bridges':[]})
        self.assertEqual(1, self.client.get('/api/intake/social').json()['ready'])

    def test_shared_bridge_failure_stops_after_one_concurrent_batch(self):
        reg.save_settings('social', {'bridges':['https://broken.org/{handle}/rss']})
        barrier = threading.Barrier(sync.FETCH_WORKERS)
        def blocked(*args, **kwargs):
            barrier.wait(timeout=2)  # Fails if fetching becomes serial again.
            raise fetching.FeedError('HTTP 451')
        with patch.object(sync, 'fetch_source', side_effect=blocked) as fetch:
            summary = sync.collect('social', FetchScope(force=True))
        self.assertEqual(4, fetch.call_count)
        self.assertEqual((4,47), (summary['checked'],summary['skipped']))
        self.assertIn('X 桥接连续失败', summary['message'])
        sources = reg.subscriptions('social')
        self.assertEqual(4, sum(bool(s['last_error']) for s in sources))
        self.assertTrue(all(not s['last_attempt'] for s in sources[4:]))
        self.assertEqual('error', pipeline_state._state['stages']['crawl']['status'])

    def test_bridge_circuit_does_not_skip_dedicated_or_other_social_feeds(self):
        reg.save_settings('social', {'bridges':['https://broken.org/{handle}/rss']})
        direct = self.source('https://example.org/social-feed', kind='social')
        dedicated = self.source('https://x.com/dedicated', kind='social')
        reg.save_subscription('social', {'feed_url':'https://example.org/dedicated'}, dedicated['id'])
        def response(source, *args, **kwargs):
            if reg.uses_bridge(source):
                raise fetching.FeedError('HTTP 451')
            return self.entries(), {'etag':'', 'modified':'', 'resolved_url':source['url']}
        with patch.object(sync, 'fetch_source', side_effect=response):
            summary = sync.collect('social', FetchScope(force=True))
        self.assertEqual((6,47,1), (summary['checked'],summary['skipped'],summary['added']))
        self.assertTrue(reg.subscription('social', direct['id'])['last_success'])
        self.assertTrue(reg.subscription('social', dedicated['id'])['last_success'])

    def test_export_keeps_plain_symbols_and_fts_has_no_entity_words(self):
        from kb import scanner, store
        from kb.config import Source
        from kb.chunker import split_chunks
        from kb.search import _fts_leg, Filters
        source = self.source()
        entry = {**self.entries()[0], 'title':'A & B <b> 标题',
                 'body':'比较 a < b 且 b > c，成本 R&D 占 30%。\nRust & Go'}
        reg.put_items(source, [entry]); sync.export('rss')
        vault = self.root/'vault'
        text = next(vault.rglob('*.md')).read_text()
        self.assertIn('# ' + entry['title'] + '\n', text)
        self.assertIn(entry['body'], text)
        self.assertNotIn('&amp;', text)
        self.assertNotIn('&lt;', text)
        with patch.object(scanner,'VAULT_ROOT',vault), patch.object(scanner,'SOURCES',[Source('rss',vault/'source/rss')]):
            doc = next(scanner.scan_all())
        self.assertEqual(entry['title'], doc.title)
        conn = store.connect(self.root/'index.db')
        try:
            store.upsert_document(conn,doc,split_chunks(doc.body),reg.now()); conn.commit()
            self.assertEqual([], _fts_leg(conn, 'amp', Filters(source='rss')))
            self.assertTrue(_fts_leg(conn, 'Rust', Filters(source='rss')))
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
