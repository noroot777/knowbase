"""Isolated source lifecycle, ingestion, scheduling and search regressions."""
import json
import shutil
import sqlite3
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
from kb.feeds import registry as reg, fetching, sync, x_channels
from kb.source_types import SOURCE_TYPES, feed_types
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

    def test_etag_304_is_sent_and_honoured(self):
        s=self.source(); s.update(etag='v1',modified='date',resolved_url=s['url'])
        with patch.object(fetching,'request',return_value=fetching.Response(304,s['url'],b'',{})) as req:
            entries,_=fetching.fetch_source(s,reg.get_settings('rss'))
            self.assertEqual([],entries)
            self.assertEqual('v1',req.call_args.args[1]['If-None-Match'])

    def test_homepage_discovers_only_advertised_rss(self):
        s=self.source('https://example.org/')
        html=b'<html><link rel="alternate" type="application/rss+xml" href="/feed.xml"></html>'
        with patch.object(fetching,'request',side_effect=[fetching.Response(200,s['url'],html,{}),fetching.Response(200,'https://example.org/feed.xml',RSS,{'etag':'v2'})]) as req:
            entries,cache=fetching.fetch_source(s,reg.get_settings('rss'))
        self.assertEqual(1,len(entries)); self.assertEqual('https://example.org/feed.xml',cache['resolved_url'])
        # 第二跳不许重新计时:一条订阅两跳也只有一份预算,各自重新计时的话一条源
        # 最久能占掉两倍 SOURCE_SECONDS,把同一轮后面的源挤掉。
        self.assertEqual(req.call_args_list[0].kwargs['deadline'],
                         req.call_args_list[1].kwargs['deadline'])

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
            # extract 在列里:这颗按钮同步完就该把该提取的提取掉。「哪些条目
            # 值得提取」由订阅级开关回答(默认关,见 `extract.pending_docs`),
            # 不是靠把这一步排除在计划外 —— 那样用户给某条订阅开了提取,它
            # 同步完也不会提取,得等绿泡泡那轮全流程定时同步顺手捎上。
            self.assertEqual(['crawl','export','index','extract'],start.call_args.args[0])
            self.assertEqual([str(s['id'])],start.call_args.kwargs['crawl_filter']['accounts'])
        with patch.object(pipeline,'start_pipeline',return_value=False):
            self.assertEqual(409,self.client.post('/api/intake/rss/sync',json={}).status_code)
        self.assertEqual(404,self.client.get('/api/intake/missing').status_code)
        self.assertEqual(422,self.client.get('/api/intake/rss/items?offset=-1').status_code)
        self.assertEqual(200,self.client.delete(f"/api/intake/rss/sources/{s['id']}").status_code)
        self.assertEqual(404,self.client.delete(f"/api/intake/rss/sources/{s['id']}").status_code)

    def test_per_kind_interval_scheduling_is_gone_but_the_rest_is_untouched(self):
        """「按来源类型每 N 分钟自动同步」整条拆掉了(见 docs/decisions/sync-schedule.md)。

        拆的是**定时**。三样东西一动没动,而且都很容易被这次改动误伤:
        ①手动同步;②**逐条订阅**的启用开关(它跟 kind 级的 `enabled` 同名,
        但从来是两回事);③订阅级的提取开关那道闸。

        页头那颗「定时同步」后来也删了,所以这里不再给它打桩;「起跑没被受理就不
        记账」那条判据跟着搬去了日报那一侧
        (`test_schedule_ledger.test_an_unaccepted_start_does_not_spend_the_backoff`)。
        """
        from kb import daily_run
        # ① kind 级设置写成"开着、间隔 15 分钟、早就到点了",调度线程也不该起管道
        reg.save_settings('rss', {'enabled': True, 'interval_minutes': 15, 'next_run': 1})
        with patch.object(daily_run, 'tick', return_value=False), \
             patch.object(pipeline, 'start_pipeline') as start:
            pipeline._scheduler_tick()
            start.assert_not_called()
        # ② 手动同步照旧:这是删掉定时之后唯一的"我现在就要"入口
        with patch.object(pipeline, 'start_pipeline', return_value='run-1') as start:
            self.assertEqual(200, self.client.post('/api/intake/rss/sync', json={}).status_code)
            start.assert_called_once()
        # ③ 逐条订阅的启用开关没被误伤 —— `collectors.feeds.status` 和 `sync.collect`
        #    读的始终是 `source['enabled']`,不是上面那个 kind 级的同名键
        source = reg.subscriptions('rss')[0]
        self.assertIn(source['name'], get_collector('rss').status().targets)
        self.assertEqual(200, self.client.patch(f"/api/intake/rss/sources/{source['id']}",
                                                json={'enabled': False}).status_code)
        self.assertFalse(reg.subscriptions('rss')[0]['enabled'])
        self.assertNotIn(source['name'], get_collector('rss').status().targets)

    def test_settings_without_the_retired_keys_neither_fails_nor_drops_them(self):
        """新页面不再发 `enabled` / `interval_minutes`(那两个控件删了)。

        **仍旧接受、仍旧原样存**:老页面和脚本还会发,拒掉等于让它们保存失败;
        照收不存则是静默丢字段。键按元规则「只增不减」留在盘上。
        """
        reg.save_settings('rss', {'enabled': True, 'interval_minutes': 120})
        response = self.client.put('/api/intake/rss/settings', json={})
        self.assertEqual(200, response.status_code, response.text)
        kept = reg.get_settings('rss')
        self.assertEqual((True, 120), (kept['enabled'], kept['interval_minutes']))
        # 老调用方照旧发得上去,存下来的就是它发的那一份
        self.assertEqual(200, self.client.put(
            '/api/intake/rss/settings',
            json={'enabled': False, 'interval_minutes': 240}).status_code)
        self.assertEqual(240, reg.get_settings('rss')['interval_minutes'])

    def test_active_run_blocks_mutation(self):
        pipeline_state._state.update(running=True,crawl_filter={'channel':'rss'})
        response=self.client.post('/api/intake/rss/sources',json={'name':'x','url':'https://example.org/feed'})
        self.assertEqual(409,response.status_code)

    def test_a_stored_legacy_interval_is_migrated_to_the_new_default(self):
        """存量设置里没人碰过的旧默认值,读的时候按 `settings_rev` 归一。

        **`interval_minutes` 现在没有消费方了**(按来源类型的间隔定时整条拆掉,
        见 `kb.pipeline._scheduler_tick`),所以这一条钉的只剩 `_upgrade_settings`
        本身:读出来的形状一致、盖过章不再迁第二次。键按元规则「只增不减」留着。
        """
        with reg.db() as c:          # 旧版真实存下来的形状,没有 settings_rev
            c.execute('INSERT OR REPLACE INTO feed_settings VALUES(?,?)',
                      ('social', json.dumps({'enabled': True, 'interval_minutes': 60,
                                             'next_run': 0, 'bridges':
                                             ['https://example.org/{handle}/rss']})))
        self.assertEqual(1440, reg.get_settings('social')['interval_minutes'])
        # 盖过章之后不再迁第二次(这才是 settings_rev 要钉的东西)
        reg.save_settings('social', {'bridges': []})
        self.assertEqual(1440, reg.get_settings('social')['interval_minutes'])

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

    def test_default_social_waits_for_configuration_without_starting_pipeline(self):
        overview = self.client.get('/api/intake/social').json()
        self.assertEqual((51, 0), (overview['unconfigured'], overview['ready']))
        self.assertEqual(x_channels.DEFAULT, overview['settings']['x_channel'])
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
        # 一个都没抓成,但也不许给订阅记上"连接失败"——待配置不是失败
        self.assertFalse(any(s['last_error'] for s in reg.subscriptions('social')))

    def test_a_dedicated_feed_url_is_ready_on_its_own(self):
        """**填了专用订阅链接的那条不看通道脸色。** 它走的是普通 HTTP,通道登
        没登录都跟它无关;反过来,通道就绪也不许把"就绪"算到别人头上之外的事
        上——两边各自独立。"""
        source = reg.subscriptions('social')[0]
        reg.save_subscription('social', {'feed_url':'https://example.org/account/rss'}, source['id'])
        data = self.client.get('/api/intake/social').json()
        self.assertEqual((50, 1), (data['unconfigured'], data['ready']))
        with patch.object(pipeline, 'start_pipeline', return_value=True):
            self.assertEqual(200, self.client.post('/api/intake/social/sync', json={'source_ids':[source['id']]}).status_code)
        # 通道自己就绪之后,剩下 50 条才跟着就绪;那一条照旧就绪。
        profile = self.root / 'xprof'
        (profile / 'Default').mkdir(parents=True)
        (profile / 'Default' / 'Cookies').write_bytes(b'x')
        with patch.object(x_channels, 'BROWSER_PROFILE', profile):
            self.assertEqual(51, self.client.get('/api/intake/social').json()['ready'])
        self.assertEqual(1, self.client.get('/api/intake/social').json()['ready'])

    def test_one_batch_of_failures_never_skips_the_rest_of_the_round(self):
        """**同一轮里别人失败不许顺手把后面的跳过。** 曾经有一道"连续失败就
        熔断"的闸,它防的是"公共实例挂了别再拿 50 个账号去撞";桥接删掉之后
        没有公共实例可撞了,留着只会让一批超时把同一轮里好好的源一起扔掉。
        顺带钉住并发:`barrier` 等不到人就是又退回串行了。"""
        direct = self.source('https://example.org/social-feed', kind='social')
        dedicated = self.source('https://x.com/dedicated', kind='social')
        reg.save_subscription('social', {'feed_url':'https://example.org/dedicated'}, dedicated['id'])
        profile = self.root / 'xprof'
        (profile / 'Default').mkdir(parents=True)
        (profile / 'Default' / 'Cookies').write_bytes(b'x')
        barrier = threading.Barrier(sync.FETCH_WORKERS)
        def response(source, *args, **kwargs):
            if reg.uses_channel(source):
                barrier.wait(timeout=5)
                raise fetching.FeedError('HTTP 451')
            return self.entries(), {'etag':'', 'modified':'', 'resolved_url':source['url']}
        with patch.object(sync, 'fetch_source', side_effect=response), \
             patch.object(x_channels, 'BROWSER_PROFILE', profile), \
             patch.object(x_channels, 'prefetch'):
            summary = sync.collect('social', FetchScope(force=True))
        self.assertEqual((53, 0, 1), (summary['checked'], summary['skipped'], summary['added']))
        self.assertEqual(51, sum(bool(s['last_error']) for s in reg.subscriptions('social')))
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


class DefaultsUpgradeTests(unittest.TestCase):
    """**存量库升级也要拿到后来新增的默认订阅。**

    原来只有 `defaults-v1` 一个"种过没有"的标记,于是往 defaults.json 里加来源
    这件事对所有存量库**永久不生效** —— 升级重启之后那一类来源是 0 个订阅、
    「同步全部」还点不了,而新装的机器上它好好的(2026-10-10 审查第 1 条)。

    下面是用**构造出来的旧库状态**跑的(删掉 `defaults-rev`、删掉标了 `since`
    的那几条 = 旧代码留下的样子);另外拿主检出那份真旧代码建库、再用本分支打开
    交叉验证过一次:60 → 61 → 61 条。
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.old, reg.DB_PATH = reg.DB_PATH, self.root / 'feeds.db'
        self.addCleanup(lambda: setattr(reg, 'DB_PATH', self.old))
        self.fresh = {s['name'] for s in self._defaults() if int(s.get('since', 1)) > 1}
        self.assertTrue(self.fresh, 'defaults.json 里没有标 since 的条目,这组用例就空转了')

    def _defaults(self):
        return json.loads(Path(reg.__file__).with_name('defaults.json').read_text())

    def _names(self):
        with reg.db() as c:
            return {r[0] for r in c.execute('SELECT name FROM subscriptions')}

    def _raw_names(self):
        """**不走 `reg.db()`** —— 它一打开就补种,拿它断言"旧库现在是空的"
        等于自己把前提毁掉(第一版就是这么写的,当场红)。"""
        conn = sqlite3.connect(reg.DB_PATH)
        try:
            return {r[0] for r in conn.execute('SELECT name FROM subscriptions')}
        finally:
            conn.close()

    def _make_old_library(self):
        """跑一次新代码建库,再把它改回旧代码留下的那个状态。"""
        self._names()
        with reg.db() as c:
            c.execute("DELETE FROM feed_meta WHERE key='defaults-rev'")
            c.executemany('DELETE FROM subscriptions WHERE name=?',
                          [(n,) for n in self.fresh])

    def test_an_old_library_gets_the_newly_added_defaults(self):
        self._make_old_library()
        self.assertFalse(self.fresh & self._raw_names())   # 旧库里确实没有
        self.assertTrue(self.fresh <= self._names())       # 打开一次就补上了

    def test_the_backfilled_rows_carry_their_own_settings(self):
        """补种的那几条带着自己的开关和历史下界,不是插一行空壳。"""
        self._make_old_library()
        self._names()
        want = {s['name']: s for s in self._defaults() if s['name'] in self.fresh}
        with reg.db() as c:
            rows = {r['name']: r for r in c.execute(
                'SELECT name,kind,url,fulltext,history_floor,enabled FROM subscriptions')}
        for name, spec in want.items():
            with self.subTest(name):
                got = rows[name]
                self.assertEqual(got['kind'], spec['kind'])
                self.assertEqual(got['fulltext'], int(bool(spec.get('fulltext'))))
                self.assertEqual(got['history_floor'], reg.history_floor())
                self.assertEqual(got['enabled'], 1)

    def test_opening_it_again_changes_nothing(self):
        self._make_old_library()
        before = self._names()
        for _ in range(3):
            self.assertEqual(self._names(), before)

    def test_a_default_the_user_deleted_does_not_come_back(self):
        """**反过来钉一边。** 判据是"这个库种到第几号了",不是"这条订阅还在
        不在" —— 补种要是按名字查一遍,删掉的默认订阅每次重启都会回来。"""
        self._make_old_library()
        victim = sorted(self.fresh)[0]
        self._names()
        with reg.db() as c:
            c.execute('DELETE FROM subscriptions WHERE name=?', (victim,))
        self.assertNotIn(victim, self._names())
        self.assertNotIn(victim, self._names())

    def test_a_url_the_user_already_added_is_not_duplicated(self):
        self._make_old_library()
        spec = next(s for s in self._defaults() if s['name'] in self.fresh)
        url = reg.source_url(spec['kind'], spec['url'])
        with reg.db() as c:
            c.execute("DELETE FROM feed_meta WHERE key='defaults-rev'")
            c.executemany('DELETE FROM subscriptions WHERE name=?',
                          [(n,) for n in self.fresh])
            cols = ('kind,name,url,mode,group_name,config,created_at,'
                    'updated_at,history_floor,fulltext')
            c.execute(f'INSERT INTO subscriptions({cols}) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (spec['kind'], '我自己加的', url, 'feed', '', '{}',
                       reg.now(), reg.now(), '', 0))
        with reg.db() as c:
            n = c.execute('SELECT count(*) FROM subscriptions WHERE url=?',
                          (url,)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertIn('我自己加的', self._names())

    def test_a_fresh_library_still_gets_everything(self):
        """反过来钉一边:补种那条路不许把全新安装弄少。"""
        names = self._names()
        self.assertTrue(self.fresh <= names)
        self.assertEqual(len(names), len({s['name'] for s in self._defaults()}))


class SourceCopyTests(unittest.TestCase):
    """来源类型那几个人读字段**全部经 `textContent` 落地**,写 markdown 就是把符号显示给人看。

    面板上的标签和那段说明走 `sources.js` 的 `textContent = spec.label` /
    `spec.note`,「添加订阅」弹窗里「链接」下面那一行走 `source_forms.js` 的
    `textContent = ...url_hint` —— 一处都不过 markdown。简报那条 note 里曾经写着
    `**不要再开「补抓原文」**`,于是面板上就显示着两对星号。

    所以这里不验文案长什么样(那是文案,改一个字就红一条没有意义),只验
    「没有只在 markdown 里才有意义的符号」;`test_the_check_is_not_vacuous`
    钉住判据本身还抓得到东西。
    """

    MARKUP = ('**', '__', '`')
    FIELDS = ('label', 'description', 'url_hint', 'note')

    def _markup_in(self, value: str) -> list[str]:
        return [m for m in self.MARKUP if m in value]

    def test_no_markdown_in_anything_a_person_reads(self):
        for kind, spec in SOURCE_TYPES.items():
            for field in self.FIELDS:
                value = getattr(spec, field)
                with self.subTest(kind=kind, field=field):
                    self.assertEqual([], self._markup_in(value),
                                     f'{kind}.{field} 带着 markdown 标记,它会原样显示给人看')

    def test_the_check_is_not_vacuous(self):
        """反面:判据真抓得到东西 —— 否则上面那条永远绿,而绿的原因是用例不敏感。"""
        for bad in ('**粗体**', '__也是粗体__', '`等宽`'):
            with self.subTest(bad=bad):
                self.assertNotEqual([], self._markup_in(bad))
        # 正常的中文标点不许被误判:这几个符号在文案里天天用
        self.assertEqual([], self._markup_in('正文就是简报自己写的摘要，不要再开「补抓原文」——那个外链指向别人家的文章。'))


if __name__ == '__main__':
    unittest.main()
