"""Isolated source lifecycle, ingestion, scheduling and search regressions."""
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import pipeline, pipeline_state
from kb.collectors import FetchScope, get_collector
from kb.feeds import registry as reg, fetching, sync, schedule
from kb.web import intake_api

RSS = b'''<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>
<item><guid>id-1</guid><title>Agent notebook</title><link>https://example.org/post?utm_source=rss</link>
<description><![CDATA[<p>agent notebook knowledge</p><script>danger()</script>]]></description>
<pubDate>Mon, 28 Sep 2026 01:00:00 GMT</pubDate></item></channel></rss>'''
ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Notes</title>
<entry><id>urn:post:1</id><title>Short thought</title><link href="https://nitter.net/Example/status/123#m"/>
<updated>2026-09-28T02:00:00Z</updated><author><name>Example</name></author>
<content type="html">&lt;p&gt;A thought&lt;/p&gt;</content></entry></feed>'''


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patches = [patch.object(reg,'DB_PATH',self.root/'feeds.db'),
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
        self.assertEqual({'wechat','social','rss'},{d['id'] for d in data})

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
        self.assertIn('2026-09-28',e['published'])
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

    def test_range_does_not_cache_away_out_of_range_entries(self):
        s=self.source()
        entries=self.entries()+[{**self.entries()[0],'url':'https://example.org/old','published':'2026-08-01 08:00'}]
        with patch.object(sync,'fetch_source',return_value=(entries,{'etag':'new','modified':'','resolved_url':s['url']})):
            sync.collect('rss',FetchScope(targets=(str(s['id']),),since='2026-09-28',until='2026-09-28'))
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
            found=_fts_leg(conn,'notebook',Filters(source='rss',until='2026-09-28'))
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

    def test_collect_budget_leaves_unattempted_sources_for_next_run(self):
        good = (self.entries(), {'etag':'','modified':'','resolved_url':'https://example.org'})
        with patch.object(sync, 'fetch_source', return_value=good) as fetch, \
             patch.object(sync.time, 'monotonic', side_effect=[0,0,0,100]):
            summary = sync.collect('rss', FetchScope(force=True))
        self.assertEqual((4,5), (summary['checked'],summary['skipped']))
        self.assertEqual([], summary['errors'])
        self.assertEqual('ok', pipeline_state._state['stages']['crawl']['status'])
        self.assertIn('时间预算', summary['message'])
        self.assertEqual(4, fetch.call_count)
        self.assertEqual(20, fetch.call_args.kwargs['deadline'])
        first = {c.args[0]['id'] for c in fetch.call_args_list}
        untouched = [s for s in reg.subscriptions('rss') if s['id'] not in first]
        self.assertTrue(all(not s['last_attempt'] and not s['last_error']
                            and not s['failures'] and not s['retry_at'] for s in untouched))
        with patch.object(sync, 'fetch_source', return_value=good) as fetch, \
             patch.object(sync.time, 'monotonic', side_effect=[0,0,0,100]):
            sync.collect('rss', FetchScope(force=True))
        self.assertTrue(first.isdisjoint(c.args[0]['id'] for c in fetch.call_args_list))

    def test_inflight_batch_crosses_run_budget_without_failure_backoff(self):
        # Twenty-five feeds: five healthy batches finish at t=89.9; the next
        # batch returns HTTP 200 at t=91.9. Only the final unopened source waits.
        for i in range(16):
            self.source(f'https://example.org/feed-{i}.xml', name=f'Feed {i}')
        clock = [0.0]
        durations = iter([18,18,18,18,17.9,2])
        def finish_batch():
            clock[0] += next(durations)
        barrier = threading.Barrier(4, action=finish_batch)
        def response(url, headers=None, *, deadline=None):
            barrier.wait(timeout=2)
            fetching.remaining(deadline)
            return fetching.Response(200, url, RSS, {})
        with patch.object(fetching, 'request', side_effect=response) as request, \
             patch.object(sync.time, 'monotonic', side_effect=lambda:clock[0]):
            summary = sync.collect('rss', FetchScope(force=True))
        self.assertEqual([], summary['errors'])
        self.assertEqual((24,1), (summary['checked'],summary['skipped']))
        self.assertEqual(24, request.call_count)
        sources = reg.subscriptions('rss')
        self.assertEqual(24, sum(bool(s['last_success']) for s in sources))
        self.assertTrue(all(not s['last_error'] and not s['failures'] and not s['retry_at'] for s in sources))
        self.assertEqual('', sources[-1]['last_attempt'])
        self.assertEqual('ok', pipeline_state._state['stages']['crawl']['status'])

    def test_source_timeout_still_records_failure_and_backoff(self):
        source = self.source()
        clock = [0.0]
        def timeout(url, headers=None, *, deadline=None):
            clock[0] = 21  # This source itself has exceeded its full 20s budget.
            fetching.remaining(deadline)
        with patch.object(fetching, 'request', side_effect=timeout), \
             patch.object(sync.time, 'monotonic', side_effect=lambda:clock[0]):
            summary = sync.collect('rss', FetchScope(targets=(str(source['id']),), force=True))
        saved = reg.subscription('rss', source['id'])
        self.assertEqual(1, len(summary['errors']))
        self.assertEqual(0, summary['skipped'])
        self.assertEqual(1, saved['failures'])
        self.assertGreater(saved['retry_at'], sync.time.time())
        self.assertIn('超时', saved['last_error'])
        self.assertEqual('error', pipeline_state._state['stages']['crawl']['status'])

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
