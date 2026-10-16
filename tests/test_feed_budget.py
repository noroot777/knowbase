"""Collection cutoffs must not poison source health or automatic retries."""
import json
import socket
import tempfile
import threading
import unittest
from concurrent.futures import wait
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from kb import maintenance
from kb.collectors import FetchScope
from kb.feeds import fetching, registry as reg, sync


class Clock:
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.value

    def set(self, value):
        with self.lock:
            self.value = value


class FeedBudgetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.clock = Clock()
        self.epoch = 1_000_000
        clock = SimpleNamespace(monotonic=self.clock.monotonic, time=lambda: self.epoch)
        for mock in (
            patch.object(reg, 'DB_PATH', Path(temp.name) / 'feeds.db'),
            # 维护锁的 lockfile 在 DATA_DIR 下,指到 tmp 才不会去 flock 生产那一个
            patch.object(maintenance, 'DATA_DIR', Path(temp.name)),
            patch.object(sync, '_state', {'stop_requested': False}),
            patch.object(sync, '_stage'),
            patch.object(sync, 'time', clock),
            patch.object(fetching, 'time', clock),
            patch.object(fetching.socket, 'getaddrinfo', side_effect=AssertionError('Unexpected network')),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        self.sources = sorted(reg.subscriptions('rss'), key=lambda s: s['id'])

    def healthy(self, source, settings, *, deadline, collect_deadline=None, **kwargs):
        # This healthy source needs two seconds; the old 0.1-second tail fails.
        if fetching.remaining(deadline, collect_deadline) < 2:
            raise fetching.FeedError('订阅连接超时')
        entry = dict(url=f"https://example.org/{source['id']}", title=source['name'],
                     body='A complete article', author='', published='', content_kind='content')
        return [entry], dict(etag='v1', modified='', resolved_url=source['url'])

    def one_batch(self, elapsed=89.9):
        self.clock.set(0)
        save_result = sync._save_result
        saved = []

        def save(source, result, scope):
            added = save_result(source, result, scope)
            saved.append(source['id'])
            if len(saved) == sync.FETCH_WORKERS:
                self.clock.set(elapsed)
            return added

        with patch.object(sync, 'fetch_source', side_effect=self.healthy) as fetch, \
             patch.object(sync, '_save_result', side_effect=save):
            summary = sync.collect('rss', FetchScope())
        return summary, fetch.call_args_list

    def assert_unchanged(self, sources):
        for source in sources:
            persisted = {k: v for k, v in source.items() if k != 'item_count'}
            self.assertEqual(persisted, reg.subscription(source['kind'], source['id']))

    def transport(self, handler):
        client = httpx.Client

        def factory(**kwargs):
            return client(**kwargs, transport=httpx.MockTransport(handler))

        for mock in (
            patch.object(fetching.httpx, 'Client', side_effect=factory),
            patch.object(fetching.socket, 'getaddrinfo',
                         return_value=[(socket.AF_INET, 1, 6, '', ('8.8.8.8', 443))]),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def collect_one(self, source=None, **kwargs):
        source = source or self.sources[0]
        return sync.collect(source['kind'], FetchScope(targets=(str(source['id']),), **kwargs))

    def test_tail_without_full_allowance_never_submits_or_changes_source_state(self):
        summary, calls = self.one_batch()
        self.assertEqual((4, 5, 4), (summary['checked'], summary['skipped'], summary['added']))
        self.assertEqual([], summary['errors'])
        self.assertEqual('ok', sync._stage.call_args.args[1])
        self.assertIn('时间预算', summary['message'])
        self.assertEqual(4, len(calls))
        self.assertTrue(all(call.kwargs['deadline'] == 20 for call in calls))
        self.assert_unchanged(self.sources[4:])
        with reg.db() as c:
            stored = json.loads(c.execute('SELECT summary FROM feed_runs').fetchone()[0])
        self.assertEqual(summary, stored)

    def test_next_automatic_runs_prioritize_every_unfinished_source_without_force(self):
        first, first_calls = self.one_batch()
        second, second_calls = self.one_batch()
        first_ids = {call.args[0]['id'] for call in first_calls}
        second_ids = {call.args[0]['id'] for call in second_calls}
        self.assertEqual((4, 4), (first['checked'], second['checked']))
        self.assertTrue(first_ids.isdisjoint(second_ids))
        self.clock.set(0)
        with patch.object(sync, 'fetch_source', side_effect=self.healthy) as fetch:
            third = sync.collect('rss', FetchScope())
        self.assertEqual(self.sources[-1]['id'], fetch.call_args_list[0].args[0]['id'])
        self.assertEqual((9, 0, 0), (third['checked'], third['skipped'], third['deferred']))
        self.assertEqual([], third['errors'])
        self.assertTrue(all(s['last_success'] for s in reg.subscriptions('rss')))

    def test_no_full_allowance_at_start_skips_all(self):
        with patch.object(sync, 'COLLECT_SECONDS', 19.9), \
             patch.object(sync, 'fetch_source') as fetch:
            summary = sync.collect('rss', FetchScope())
        fetch.assert_not_called()
        self.assertEqual((0, 9, []), (summary['checked'], summary['skipped'], summary['errors']))
        self.assertEqual('ok', sync._stage.call_args.args[1])
        self.assert_unchanged(self.sources)

    def test_last_fully_admitted_batch_succeeds_near_run_deadline(self):
        # Six batches start at 0, 14, 28, 42, 56, 70. The last has the full
        # 20 seconds and returns at 89.9; the unopened 25th source waits.
        for index in range(16):
            reg.save_subscription('rss', {'name': f'Feed {index}',
                'url': f'https://example.org/feed-{index}.xml'})
        sources = sorted(reg.subscriptions('rss'), key=lambda s: s['id'])
        ends = iter((14, 28, 42, 56, 70, 89.9))
        barrier = threading.Barrier(sync.FETCH_WORKERS,
                                    action=lambda: self.clock.set(next(ends)))
        body = b'''<rss version="2.0"><channel><title>Test</title><item>
            <title>Healthy article</title><link>https://example.org/article</link>
            <description>Complete article</description></item></channel></rss>'''
        requests = []

        def response(request):
            requests.append(request)
            barrier.wait(timeout=2)
            return httpx.Response(200, content=body)

        self.transport(response)
        summary = sync.collect('rss', FetchScope())
        self.assertEqual((24, 1, []), (summary['checked'], summary['skipped'], summary['errors']))
        self.assertEqual(24, len(requests))
        saved = reg.subscriptions('rss')
        self.assertEqual(24, sum(bool(s['last_success']) for s in saved))
        self.assertTrue(all(not s['last_error'] and not s['failures'] and not s['retry_at'] for s in saved))
        self.assert_unchanged(sources[-1:])
        self.assertEqual('ok', sync._stage.call_args.args[1])

    def test_submission_delay_cannot_shorten_worker_allowance(self):
        with patch.object(sync, '_stage', side_effect=lambda *a, **k: self.clock.set(89.9)), \
             patch.object(sync, 'fetch_source') as fetch:
            summary = sync.collect('rss', FetchScope())
        fetch.assert_not_called()
        self.assertEqual((0, 9, []), (summary['checked'], summary['skipped'], summary['errors']))
        self.assert_unchanged(self.sources)

    def test_inflight_http_timeout_at_collection_deadline_keeps_existing_health(self):
        source = self.sources[0]
        with reg.db() as c:
            c.execute("""UPDATE subscriptions SET last_attempt='old',last_success='older',
                last_error='previous failure',failures=2,retry_at=?,etag='cached' WHERE id=?""",
                (self.epoch - 1, source['id']))
        source = reg.subscription('rss', source['id'])
        requests = []

        def timeout(request):
            requests.append(request)
            self.clock.set(20)
            raise httpx.ReadTimeout('collection boundary')

        self.transport(timeout)
        with patch.object(sync, 'COLLECT_SECONDS', 20):
            summary = self.collect_one(source)
        self.assertEqual(1, len(requests))
        self.assertEqual((0, 1, []), (summary['checked'], summary['skipped'], summary['errors']))
        self.assertEqual('ok', sync._stage.call_args.args[1])
        self.assert_unchanged([source])
        self.clock.set(0)
        with patch.object(sync, 'fetch_source', side_effect=self.healthy):
            retried = self.collect_one(source)
        self.assertEqual((1, 0, []), (retried['checked'], retried['deferred'], retried['errors']))
        self.assertEqual(0, reg.subscription('rss', source['id'])['failures'])

    def test_inflight_stream_collection_cutoff_is_not_wrapped_as_feed_error(self):
        clock = self.clock

        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'<rss>'
                clock.set(20)
                yield b'</rss>'

        self.transport(lambda r: httpx.Response(200, stream=SlowStream()))
        with patch.object(sync, 'COLLECT_SECONDS', 20):
            summary = self.collect_one()
        self.assertEqual((0, 1, []), (summary['checked'], summary['skipped'], summary['errors']))
        self.assertEqual('ok', sync._stage.call_args.args[1])
        self.assert_unchanged(self.sources)

    def test_source_timeout_still_backs_off_even_if_returning_after_both_deadlines(self):
        def timeout(request):
            self.clock.set(elapsed)
            raise httpx.ReadTimeout('source timeout')

        self.transport(timeout)
        for elapsed, source in zip((6, 20, 95), self.sources):
            with self.subTest(elapsed=elapsed):
                self.clock.set(0)
                summary = self.collect_one(source)
                self.assertEqual((1, 0, 1), (summary['checked'], summary['skipped'], len(summary['errors'])))
                self.assertEqual('error', sync._stage.call_args.args[1])
                saved = reg.subscription('rss', source['id'])
                self.assertEqual(1, saved['failures'])
                self.assertEqual(self.epoch + 1800, saved['retry_at'])
                self.assertIn('超时', saved['last_error'])
                with patch.object(sync, 'fetch_source') as fetch:
                    retried = self.collect_one(source)
                fetch.assert_not_called()
                self.assertEqual(1, retried['deferred'])

    def test_finished_failure_is_not_erased_when_persistence_crosses_run_deadline(self):
        barrier = threading.Barrier(2)
        good, bad = self.sources[:2]
        save_result = sync._save_result

        def fetch(source, *args, **kwargs):
            barrier.wait(timeout=2)
            if source['id'] == bad['id']:
                raise fetching.FeedError('HTTP 503')
            return self.healthy(source, *args, **kwargs)

        def completed(futures):
            wait(futures)
            return iter(sorted(futures, key=lambda f: futures[f]['id']))

        def save(*args):
            added = save_result(*args)
            self.clock.set(95)
            return added

        with patch.object(sync, 'fetch_source', side_effect=fetch), \
             patch.object(sync, 'as_completed', side_effect=completed), \
             patch.object(sync, '_save_result', side_effect=save):
            summary = sync.collect('rss', FetchScope(targets=(str(good['id']), str(bad['id']))))
        self.assertEqual((2, 0, 1), (summary['checked'], summary['skipped'], len(summary['errors'])))
        self.assertEqual('error', sync._stage.call_args.args[1])
        self.assertEqual('HTTP 503', reg.subscription('rss', bad['id'])['last_error'])
        self.assertTrue(reg.subscription('rss', good['id'])['last_success'])

    def test_collection_cutoff_skips_the_rest_without_blaming_them(self):
        """**预算用完是本轮的事,不是哪条订阅的错。** 没轮到的那些既不许记
        `last_error`、也不许动 `last_attempt`(否则下一轮的退避会把它们再推后
        一次),整轮状态照旧是 ok —— 它确实按预算正常收工了。"""
        # 专用订阅链接那条路才走 HTTP(X 的三条通道都在预取里整批抓完,这里
        # 要验的是逐条取的那一段),所以每条都填上。
        for s in reg.subscriptions('social'):
            reg.save_subscription('social', {'feed_url': f"https://feed.test/{s['id']}"}, s['id'])
        sources = reg.subscriptions('social')
        barrier = threading.Barrier(sync.FETCH_WORKERS)

        def timeout(*args, deadline, collect_deadline, **kwargs):
            barrier.wait(timeout=2)
            self.clock.set(20)
            fetching.remaining(deadline, collect_deadline)

        with patch.object(sync, 'COLLECT_SECONDS', 20), \
             patch.object(fetching, 'request', side_effect=timeout) as request:
            summary = sync.collect('social', FetchScope())
        self.assertEqual(4, request.call_count)
        self.assertEqual((0, 51, []), (summary['checked'], summary['skipped'], summary['errors']))
        self.assertEqual('ok', sync._stage.call_args.args[1])
        self.assertIn('时间预算', summary['message'])
        self.assert_unchanged(sources)


if __name__ == '__main__':
    unittest.main()
