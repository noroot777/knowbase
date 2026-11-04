import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.intake_diag import sources as source_diagnostics
from kb.youtube import collector, diagnosis, discovery, media, notes, source_status, storage
from tests.youtube_fixture import YouTubeFixture


class YouTubeSourceStatus(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.sources = [self.channel(number) for number in range(1, 6)]
        self.settings(cookies_browser='firefox')

    def collect(self, fetch):
        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'firefox'}), \
                patch.object(collector, 'DISCOVERY_SPACING', 0), \
                patch.object(discovery, 'fetch', side_effect=fetch), \
                patch.object(notes, 'available', return_value=(False, '本测试不调用模型')):
            return collector.collect(FetchScope())

    @contextmanager
    def budget_runs_out_after_each_note(self):
        """每出一条成品,处理时间预算就当场用尽(`collector.process` 的 deadline 那一道)。

        这一轮剩下的候选因此落成 `deferred`、留待下轮 —— 条数预算拆掉之后,「本轮做不完的
        队尾」只剩这一种来路。只换 `collector` 自己看到的钟,别的模块照旧用真钟。
        """
        clock = {'offset': 0.0}
        shim = SimpleNamespace(time=time.time, sleep=time.sleep,
                               monotonic=lambda: time.monotonic() + clock['offset'])

        def note(row, *arguments):
            clock['offset'] += 10 ** 6
            return self.note(row, *arguments)

        with patch.object(collector, 'time', shim):
            yield note

    def summary(self, **values):
        return {'added': 0, 'stopped': False, 'message': '全局汇总中其他频道的错误不能落到本订阅',
                'errors': [], 'failed_ids': [], 'succeeded_ids': [], 'notices': [], 'partial': [],
                'walls': {}, 'cautions': {}, 'browser': 'firefox', **values}

    def test_notices_and_partial_state_are_owned_by_source_id_even_when_names_collide(self):
        for source in self.sources[:2]:
            reg.save_subscription('youtube', {'name': '同名频道'}, source['id'])
        identities = [source['id'] for source in self.sources]

        def fetch(source, settings, timeout, estimates, evidence):
            if source['id'] in identities[:2]:
                count = identities.index(source['id']) + 1
                source['discovery_unavailable'] = [{'video_id': str(index).zfill(11), 'reason': '条目无法读取'}
                                                   for index in range(count)]
            elif source['id'] == identities[3]:
                evidence.append('只列到 7 条就中断了：频道预算用尽')
            elif source['id'] == identities[4]:
                diagnosis.record('WARNING: failed to decrypt cookies')
            return []

        summary = self.collect(fetch)
        first, second, clean, partial, warned = [reg.subscription('youtube', identity) for identity in identities]
        self.assertIn('1 条视频无法读取', first['last_notice'])
        self.assertNotIn('2 条视频', first['last_notice'])
        self.assertIn('2 条视频无法读取', second['last_notice'])
        self.assertNotIn('1 条视频', second['last_notice'])
        self.assertEqual(('', ''), (clean['last_note'], clean['last_notice']))
        self.assertIn('频道没列完', partial['last_note'])
        self.assertIn('7 条', partial['last_note'])
        self.assertEqual(('', '', 0), (partial['last_success'], partial['last_error'], partial['failures']))
        self.assertEqual('', partial['last_notice'])
        self.assertNotIn(str(partial['id']), summary['succeeded_ids'])
        self.assertEqual('频道 4', summary['partial'][0]['title'])
        self.assertEqual(partial['id'], summary['partial'][0]['source_id'])
        self.assertIn('Firefox', warned['last_notice'])
        self.assertNotIn('Firefox', first['last_notice'])
        self.assertIn('同名频道：1 条视频无法读取', summary['message'])
        self.assertEqual({identities[0], identities[1]}, {row['source_id'] for row in summary['notices']})
        recovered = self.collect(lambda *arguments: [])
        self.assertEqual({str(identity) for identity in identities}, set(recovered['succeeded_ids']))
        for identity in identities:
            stored = reg.subscription('youtube', identity)
            self.assertEqual(('', ''), (stored['last_note'], stored['last_notice']))
            self.assertTrue(stored['last_success'])

    def test_successful_empty_partial_listing_clears_connection_error_but_not_watermark(self):
        source = self.sources[0]
        previous = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        with reg.db() as connection:
            connection.execute('''UPDATE subscriptions SET last_success=?,last_error='原有失败',
                failures=3,retry_at=123 WHERE id=?''', (previous, source['id']))

        def fetch(selected, settings, timeout, estimates, evidence):
            if selected['id'] == source['id']:
                evidence.append('只列到 9 条就中断了：等待下轮续扫')
            return []

        result = self.collect(fetch)
        stored = reg.subscription('youtube', source['id'])
        self.assertEqual((previous, '', 0, 0), tuple(
            stored[key] for key in ('last_success', 'last_error', 'failures', 'retry_at')))
        self.assertIn('9 条', stored['last_note'])
        self.assertNotIn(str(source['id']), result['succeeded_ids'])
        other = reg.subscription('youtube', self.sources[1]['id'])
        self.assertEqual(('', ''), (other['last_note'], other['last_notice']))

    def test_both_commit_boundaries_ignore_global_messages_and_other_source_facts(self):
        first, second = self.sources[:2]
        summary = self.summary(notices=[{'source_id': first['id'], 'reason': '仅第一个频道跳过了 2 条'}],
                               partial=[{'source_id': first['id'], 'reason': '仅第一个频道未完成'}])
        self.assertTrue(collector.commit_notes(second, [], summary, pending_failure=True))
        stored = reg.subscription('youtube', second['id'])
        self.assertEqual('', stored['last_note'])
        self.assertNotIn('第一个', stored['last_notice'])
        self.assertIn('未完成处理', stored['last_notice'])
        collector.commit_successes([second], summary)
        stored = reg.subscription('youtube', second['id'])
        self.assertEqual(('', ''), (stored['last_note'], stored['last_notice']))
        self.assertTrue(stored['last_success'])

    def test_partial_listing_clears_old_connection_failure_without_discarding_content_or_auth_evidence(self):
        previous = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        old_entry = self.entry(990, hours=24 * 40)
        for position, (content_error, auth_error) in enumerate(((False, False), (True, False), (False, True))):
            source = self.sources[position]
            with reg.db() as connection:
                connection.execute("UPDATE subscriptions SET last_success=?,last_error='上轮连接失败',"
                                   'failures=3,retry_at=1 WHERE id=?', (previous, source['id']))
            if content_error:
                entry = self.entry(980)
                storage.discover(source, [entry])
                storage.update(entry['video_id'], state='failed', last_error='仍未修复的视频处理错误', retry_at=10**12)

            def fetch(selected, settings, timeout, estimates, evidence):
                if selected['id'] == source['id']:
                    if auth_error:
                        diagnosis.record('WARNING: failed to decrypt cookies')
                    evidence.append('只列到 9 条就中断了：等待下轮续扫')
                    return [old_entry]
                return []

            with self.subTest(content_error=content_error, auth_error=auth_error), patch.object(
                    media, 'probe', return_value={'duration': 600}):
                result = self.collect(fetch)
                stored = reg.subscription('youtube', source['id'])
                self.assertEqual(previous, stored['last_success'])
                self.assertIn('9 条', stored['last_note'])
                self.assertNotIn(str(source['id']), result['succeeded_ids'])
                self.assertEqual(('', 0, 0),
                                 tuple(stored[key] for key in ('last_error', 'failures', 'retry_at')))
                if content_error:
                    row = next(row for row in storage.rows([source['id']]) if row['video_id'] == entry['video_id'])
                    self.assertEqual(('failed', '仍未修复的视频处理错误'), (row['state'], row['last_error']))
                if auth_error:
                    self.assertIn('Firefox', stored['last_notice'])
                problems = [item['problem'] for item in source_diagnostics.problems('youtube')
                            if item['source_id'] == source['id']]
                self.assertIn('partial', problems)
                if not content_error and not auth_error:
                    self.assertEqual(['partial'], problems)

    def test_zero_new_entries_with_deferred_or_failed_content_clears_only_connection_ledger(self):
        previous = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        for number, state in enumerate(('deferred', 'failed'), 1):
            source = self.sources[number - 1]
            entry = self.entry(990 + number)
            storage.discover(source, [entry])
            storage.update(entry['video_id'], state=state, last_error='内容待重试' if state == 'failed' else '',
                           reason='内容预算留尾', retry_at=10**12)
            with reg.db() as connection:
                connection.execute("UPDATE subscriptions SET last_success=?,last_error='旧连接失败',"
                                   "failures=3,retry_at=1 WHERE id=?", (previous, source['id']))
            with patch.object(media, 'probe', return_value={'duration': 600}):
                result = self.collect(lambda *arguments: [])
            stored = reg.subscription('youtube', source['id'])
            self.assertEqual(('', 0, 0, previous), tuple(stored[key] for key in
                             ('last_error', 'failures', 'retry_at', 'last_success')))
            self.assertIn('未完成处理', stored['last_notice'])
            self.assertNotIn(str(source['id']), result['succeeded_ids'])
            row = next(row for row in storage.rows([source['id']]) if row['video_id'] == entry['video_id'])
            self.assertEqual(state, row['state'])
            self.assertEqual(10**12, row['retry_at'])

    def test_current_content_failure_uses_video_backoff_and_notice_not_connection_error(self):
        source = self.sources[0]
        with patch.object(discovery, 'fetch', side_effect=lambda selected, *arguments:
                          [self.entry(970)] if selected['id'] == source['id'] else []), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'firefox'}), \
                patch.object(collector, 'DISCOVERY_SPACING', 0), patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', side_effect=RuntimeError('本轮字幕下载失败')):
            result = collector.collect(FetchScope(force=True))
        stored = reg.subscription('youtube', source['id'])
        self.assertEqual(('', 0, 0, ''), tuple(stored[key] for key in
                         ('last_error', 'failures', 'retry_at', 'last_success')))
        self.assertIn('本轮字幕下载失败', stored['last_notice'])
        self.assertEqual([str(source['id'])], result['failed_ids'])
        row, = storage.rows([source['id']])
        self.assertEqual('failed', row['state'])
        self.assertGreater(row['retry_at'], 0)

    def test_video_auth_diagnostics_survive_backoff_rounds_and_clear_after_recovery(self):
        source = self.sources[0]
        message = 'Sign in to confirm you are not a bot'

        def fetch(selected, *arguments):
            if selected['id'] == source['id']:
                selected['discovery_unavailable'] = [{'video_id': '00000000999', 'reason': '单片不可读'}]
                return [self.entry(970)]
            return []

        with patch.object(discovery, 'fetch', side_effect=fetch), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'firefox'}), \
                patch.object(collector, 'DISCOVERY_SPACING', 0), patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', side_effect=media.ToolFailure(message, message, 'bot_wall')) as obtain, \
                patch.object(notes, 'build', side_effect=self.note):
            first = collector.collect(FetchScope())
            self.assertIn(str(source['id']), first['walls']['bot_wall'])
            stored = reg.subscription('youtube', source['id'])
            self.assertIn(message, stored['last_notice'])
            row, = storage.rows([source['id']])
            self.assertEqual(('failed', 1), (row['state'], row['attempts']))
            retry_at = row['retry_at']
            for round_number in range(2):
                with self.subTest(backoff_round=round_number):
                    result = collector.collect(FetchScope())
                    self.assertEqual({}, result['walls'])
                    stored = reg.subscription('youtube', source['id'])
                    self.assertIn(message, stored['last_notice'])
                    self.assertIn('1 条视频无法读取', stored['last_notice'])
                    self.assertEqual(('', 0, 0, ''), tuple(stored[key] for key in
                                     ('last_error', 'failures', 'retry_at', 'last_success')))
                    self.assertNotIn(message, reg.subscription('youtube', self.sources[1]['id'])['last_notice'])
                    row, = storage.rows([source['id']])
                    self.assertEqual((1, retry_at), (row['attempts'], row['retry_at']))
                    self.assertEqual(1, obtain.call_count)
            obtain.side_effect = None
            obtain.return_value = self.complete_transcript()
            collector.collect(FetchScope(force=True))
            stored = reg.subscription('youtube', source['id'])
            self.assertTrue(stored['last_success'])
            self.assertNotIn(message, stored['last_notice'])
            self.assertIn('1 条视频无法读取', stored['last_notice'])
            row, = storage.rows([source['id']])
            self.assertEqual(('ready', '', 0), (row['state'], row['last_error'], row['retry_at']))

    def test_pending_notice_uses_current_unresolved_owned_rows_not_previous_notice(self):
        source, other = self.sources[:2]
        entries = [self.entry(number) for number in (960, 961, 962, 963)]
        storage.discover(source, entries[:3])
        storage.discover(other, entries[3:])
        for entry, state, message in zip(entries, ('failed', 'filtered', 'failed', 'failed'),
                                         ('当前视频登录受阻', '已过滤旧错误', '已排除旧错误', '另一个频道的错误')):
            storage.update(entry['video_id'], state=state, last_error=message, retry_at=10**12)
        with reg.db() as connection:
            connection.execute('INSERT INTO feed_item_exclusions VALUES(?,?,?,?,?)',
                               (reg.item_key('youtube', entries[2]['url']), 'youtube', entries[2]['url'], '', reg.now()))
            connection.execute("UPDATE subscriptions SET last_notice='旧连接警告与过时跳过数量' WHERE id=?",
                               (source['id'],))
        self.assertTrue(next(row for row in storage.rows([source['id']])
                             if row['video_id'] == entries[2]['video_id'])['excluded'])
        summary = self.summary()
        self.assertTrue(collector.commit_notes(source, [], summary, pending_failure=True))
        stored = reg.subscription('youtube', source['id'])
        self.assertIn('当前视频登录受阻', stored['last_notice'])
        for stale in ('旧连接警告', '已过滤旧错误', '已排除旧错误', '另一个频道的错误'):
            self.assertNotIn(stale, stored['last_notice'])
        storage.update(entries[0]['video_id'], state='ready', last_error='')
        self.assertTrue(collector.commit_notes(source, [], self.summary(), pending_failure=True))
        self.assertNotIn('当前视频登录受阻', reg.subscription('youtube', source['id'])['last_notice'])

    def test_only_current_failed_state_can_supply_a_replayed_failure_notice(self):
        source = self.sources[0]
        message = 'Sign in to confirm you are not a bot'
        for state in ('failed', 'candidate', 'deferred', 'processing', 'ready', 'filtered', 'excluded'):
            row = {'state': state, 'last_error': message, 'excluded': False, 'reason': '等待本轮处理预算'}
            with self.subTest(state=state):
                note, notice = source_status.fields(source, self.summary(), pending=True, videos=[row])
                self.assertEqual(state == 'failed', message in notice)
                self.assertEqual('', note)
                self.assertEqual(message, row['last_error'])
        for pending, excluded, error in ((False, False, message), (True, True, message), (True, False, '')):
            row = {'state': 'failed', 'last_error': error, 'excluded': excluded}
            with self.subTest(pending=pending, excluded=excluded, error=error):
                self.assertNotIn(message, source_status.fields(source, self.summary(), pending, [row])[1])

    def test_commit_preserves_history_without_reporting_queue_states_as_current_failures(self):
        source = self.sources[0]
        entries = [self.entry(number) for number in (950, 951, 952, 953)]
        storage.discover(source, entries)
        states = ('failed', 'candidate', 'deferred', 'processing')
        for entry, state in zip(entries, states):
            storage.update(entry['video_id'], state=state, last_error=state + ' 的旧登录错误',
                           reason='等待本轮处理预算', retry_at=10**12)
        self.assertTrue(collector.commit_notes(source, [], self.summary(), pending_failure=True))
        notice = reg.subscription('youtube', source['id'])['last_notice']
        self.assertIn('failed 的旧登录错误', notice)
        for state in states[1:]:
            self.assertNotIn(state + ' 的旧登录错误', notice)
        storage.update(entries[0]['video_id'], state='deferred', reason='视频处理已停止或时间预算用尽，未完成的内容留待下轮')
        self.assertTrue(collector.commit_notes(source, [], self.summary(), pending_failure=True))
        notice = reg.subscription('youtube', source['id'])['last_notice']
        self.assertNotIn('旧登录错误', notice)
        self.assertIn('未完成处理', notice)
        self.assertEqual({entry['video_id']: state + ' 的旧登录错误' for entry, state in zip(entries, states)},
                         {row['video_id']: row['last_error'] for row in storage.rows([source['id']])})

    def test_recovered_channel_with_budget_queue_does_not_replay_old_auth_failures(self):
        source = self.sources[0]
        entries = [self.entry(number) for number in (940, 941, 942)]
        message = 'Sign in to confirm you are not a bot'
        with patch.object(discovery, 'fetch', side_effect=lambda selected, *arguments:
                          entries if selected['id'] == source['id'] else []), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'firefox'}), \
                patch.object(collector, 'DISCOVERY_SPACING', 0), patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', side_effect=media.ToolFailure(message, message, 'bot_wall')) as obtain, \
                self.budget_runs_out_after_each_note() as note, \
                patch.object(notes, 'build', side_effect=note):
            collector.collect(FetchScope(force=True))
            rows = storage.rows([source['id']])
            self.assertEqual(['failed'] * 3, [row['state'] for row in rows])
            self.assertIn(message, reg.subscription('youtube', source['id'])['last_notice'])
            obtain.side_effect = None
            obtain.return_value = self.complete_transcript()
            for round_number in range(2):
                with self.subTest(recovered_round=round_number):
                    result = collector.collect(FetchScope(force=True))
                    self.assertEqual((1, {}), (result['processed'], result['walls']))
                    rows = storage.rows([source['id']])
                    ready = [row for row in rows if row['state'] == 'ready']
                    waiting = [row for row in rows if row['state'] != 'ready']
                    self.assertEqual(round_number + 1, len(ready))
                    self.assertEqual(2 - round_number, len(waiting))
                    self.assertTrue(all(row['state'] == 'deferred' for row in waiting))
                    self.assertTrue(all(message in row['last_error'] for row in waiting))
                    self.assertTrue(all('时间预算用尽' in row['reason'] for row in waiting))
                    stored = reg.subscription('youtube', source['id'])
                    self.assertNotIn(message, stored['last_notice'])
                    self.assertIn('未完成处理', stored['last_notice'])
                    self.assertEqual(('', 0, 0), tuple(stored[key] for key in ('last_error', 'failures', 'retry_at')))
            self.assertEqual(5, obtain.call_count)
        self.assertEqual(2, len(collector.YouTubeCollector('youtube').materialize()))

    def test_actual_listing_failure_keeps_error_and_backoff_instead_of_becoming_empty_success(self):
        source = self.sources[0]
        with reg.db() as connection:
            connection.execute("UPDATE subscriptions SET last_error='旧连接失败',failures=3,retry_at=1 WHERE id=?",
                               (source['id'],))
        def fetch(selected, *arguments):
            if selected['id'] == source['id']:
                raise RuntimeError('本轮频道列表无法读取')
            return []
        result = self.collect(fetch)
        stored = reg.subscription('youtube', source['id'])
        self.assertIn('本轮频道列表无法读取', stored['last_error'])
        self.assertEqual(4, stored['failures'])
        self.assertGreater(stored['retry_at'], 1)
        self.assertEqual('', stored['last_success'])
        self.assertIn(str(source['id']), result['failed_ids'])
        self.assertNotIn(str(source['id']), result['succeeded_ids'])

    def test_complete_listing_with_budget_tail_clears_old_connection_failure(self):
        source = self.sources[0]
        previous = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        with reg.db() as connection:
            connection.execute("UPDATE subscriptions SET last_success=?,last_error='上一轮连接失败：HTTP Error 429',"
                               'failures=3,retry_at=1 WHERE id=?', (previous, source['id']))
        entries = [self.entry(number) for number in (971, 972)]

        def fetch(selected, *arguments):
            if selected['id'] != source['id']:
                return []
            selected['discovery_unavailable'] = [
                {'video_id': f'{number:011d}', 'reason': '离线夹具：单片无法读取'} for number in (981, 982)]
            return entries

        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'firefox'}), \
                patch.object(collector, 'DISCOVERY_SPACING', 0), patch.object(discovery, 'fetch', side_effect=fetch), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                self.budget_runs_out_after_each_note() as note, \
                patch.object(notes, 'build', side_effect=note):
            result = collector.collect(FetchScope(force=True))
        stored = reg.subscription('youtube', source['id'])
        self.assertEqual(['deferred', 'ready'], sorted(row['state'] for row in storage.rows([source['id']])))
        self.assertEqual(('', 0, 0), tuple(stored[key] for key in ('last_error', 'failures', 'retry_at')))
        self.assertEqual(previous, stored['last_success'])
        self.assertEqual('', stored['last_note'])
        self.assertIn('2 条视频无法读取', stored['last_notice'])
        self.assertIn('未完成处理', stored['last_notice'])
        self.assertFalse(result['partial'])
        self.assertNotIn(str(source['id']), result['succeeded_ids'])
        self.assertFalse([item for item in source_diagnostics.problems('youtube')
                          if item['source_id'] == source['id']])

    def test_content_backlog_is_not_a_source_coverage_problem(self):
        source = self.sources[0]
        entries = [self.entry(number) for number in range(1, 6)]
        with patch.object(media, 'probe', return_value={'duration': 600}):
            result = self.collect(lambda selected, *arguments: entries if selected['id'] == source['id'] else [])
        stored = reg.subscription('youtube', source['id'])
        self.assertEqual(5, len(storage.rows([source['id']])))
        self.assertFalse(result['partial'])
        self.assertEqual('', stored['last_note'])
        self.assertIn('未完成处理', stored['last_notice'])
        self.assertFalse(stored['last_error'])
        self.assertFalse([item for item in source_diagnostics.problems('youtube')
                          if item['source_id'] == source['id']])

    def test_only_actual_listing_partial_creates_a_source_coverage_problem(self):
        source = self.sources[0]

        def fetch(selected, settings, timeout, estimates, evidence):
            if selected['id'] == source['id']:
                evidence.append('只列到 7 条就中断了：频道预算用尽')
            return []

        self.collect(fetch)
        problems = [item for item in source_diagnostics.problems('youtube')
                    if item['source_id'] == source['id']]
        self.assertEqual(1, len(problems))
        self.assertEqual('partial', problems[0]['problem'])
        self.assertIn('7 条', problems[0]['evidence'])

    def test_same_source_warning_is_preserved_but_not_repeated_as_caution(self):
        first, second = self.sources[:2]
        summary = self.summary(walls={'bot_wall': [str(first['id'])]},
                               cautions={'bot_wall': [str(first['id']), str(second['id'])]})
        self.assertEqual(('', collector.login.describe('bot_wall', 'firefox')),
                         source_status.fields(first, summary))
        note, notice = source_status.fields(second, summary)
        self.assertEqual('', note)
        self.assertEqual(collector.login.cautioned('bot_wall', 'firefox'), notice)

    def test_failure_boundary_assigns_identity_to_new_skip_notices(self):
        error = RuntimeError('本频道请求失败')
        error.unavailable = [{'video_id': '00000000999', 'reason': '本频道不可读视频'}]
        error.notice = '本频道有 1 条无法读取'
        summary = self.summary()
        self.assertTrue(collector.save_failure(self.sources[0], error, summary))
        self.assertEqual(self.sources[0]['id'], summary['notices'][0]['source_id'])
        self.assertEqual(('', ''), source_status.fields(self.sources[1], summary))
