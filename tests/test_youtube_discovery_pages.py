import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.youtube import discovery_pages, media, sync_window, throttle, windowed_discovery
from kb.collectors import FetchScope
from tests.youtube_fixture import YouTubeFixture


class YouTubeDiscoveryPages(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = sync_window.prepare(self.channel(), FetchScope(), settings=self.settings())
        self.now = datetime.now(timezone.utc)
        self.floor = datetime.fromisoformat(self.source['discovery_after'])
        self.limits = []

    def payload(self, number, published):
        payload = {'_type': 'url', 'id': f'{number:011d}', 'title': 'AI coding tutorial',
                   'playlist_index': number, 'channel_id': self.source['url'].rsplit('/', 1)[-1]}
        if published is not None:
            payload['timestamp'] = published.timestamp()
        return payload

    def fetch(self, payloads, notes=None, finished=True, failure=None, deadline=None):
        def transport(arguments, path, *args, **kwargs):
            limit = int(arguments[arguments.index('--playlist-end') + 1])
            self.limits.append(limit)
            if failure and len(self.limits) > 1:
                raise failure
            self.assertIn('--lazy-playlist', arguments)
            self.assertNotIn('--download-archive', arguments)
            output = media.unique(path)
            text = '\n'.join(json.dumps(payload) for payload in payloads[:limit])
            if finished:
                text += '\n[download] Finished downloading playlist: fixture\n'
            output.write_text(text)
            return output
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=transport):
            return windowed_discovery.fetch(self.source, self.settings(),
                                            deadline or time.monotonic() + 30, notes)

    def test_large_history_stops_after_one_bounded_batch(self):
        payloads = [self.payload(number, self.now if number <= 3 else self.floor - timedelta(days=4))
                    for number in range(1, 10001)]
        evidence = []
        entries = self.fetch(payloads, evidence)
        self.assertEqual([10], self.limits)
        self.assertEqual(3, len(entries))
        self.assertTrue(all(entry['approximate'] for entry in entries))
        self.assertEqual('window', self.source['discovery_stop'])
        self.assertEqual([{'limit': 10, 'observed': 10}], self.source['discovery_pages'])
        self.assertEqual([], evidence)

    def test_isolated_old_item_and_old_run_do_not_hide_later_recent_entries(self):
        payloads = [self.payload(number, self.now if number in (2, 9) else self.floor - timedelta(days=4))
                    for number in range(1, 1001)]
        entries = self.fetch(payloads)
        self.assertEqual([10, 20], self.limits)
        self.assertEqual(['00000000002', '00000000009'], [entry['video_id'] for entry in entries])
        self.assertEqual('window', self.source['discovery_stop'])

    def test_five_day_window_is_not_truncated_to_ten_items(self):
        payloads = [self.payload(number, self.now if number <= 160 else self.floor - timedelta(days=4))
                    for number in range(1, 5001)]
        entries = self.fetch(payloads)
        self.assertEqual([10, 20, 40, 80, 160, 320], self.limits)
        self.assertEqual(160, len(entries))
        self.assertEqual(160, len({entry['video_id'] for entry in entries}))
        self.assertEqual('window', self.source['discovery_stop'])

    def test_known_ids_remain_visible_to_boundary_guard_but_not_new_candidates(self):
        payloads = [self.payload(number, self.floor - timedelta(days=4)) for number in range(1, 1001)]
        self.source['known_video_ids'] = tuple(payload['id'] for payload in payloads)
        self.assertEqual([], self.fetch(payloads))
        self.assertEqual([10], self.limits)
        self.assertEqual('window', self.source['discovery_stop'])

    def test_no_dates_does_not_expand_into_all_history_or_confirm_success(self):
        evidence = []
        entries = self.fetch([self.payload(number, None) for number in range(1, 1001)], evidence)
        self.assertEqual([10], self.limits)
        self.assertEqual(10, len(entries))
        self.assertEqual('partial', self.source['discovery_stop'])
        self.assertIn('缺少可靠的粗日期', evidence[0])

    def test_previously_checked_old_ids_are_filtered_without_hiding_boundary_evidence(self):
        payloads = [self.payload(number, self.floor - timedelta(days=1)) for number in range(1, 4)]
        payloads.extend(self.payload(number, self.floor - timedelta(days=4)) for number in range(4, 1001))
        self.source['checked_old_ids'] = tuple(payload['id'] for payload in payloads)
        self.assertEqual([], self.fetch(payloads))
        self.assertEqual([10], self.limits)
        self.assertEqual('window', self.source['discovery_stop'])

    def test_unknown_tail_prevents_false_old_page_confirmation(self):
        payloads = [self.payload(number, None if number == 10 else self.floor - timedelta(days=4))
                    for number in range(1, 1001)]
        evidence = []
        self.fetch(payloads, evidence)
        self.assertEqual([10], self.limits)
        self.assertEqual('partial', self.source['discovery_stop'])
        self.assertTrue(evidence)

    def test_two_day_boundary_margin_is_retained_and_does_not_end_paging(self):
        payloads = [self.payload(number, self.floor - timedelta(days=1 if number <= 10 else 4))
                    for number in range(1, 1001)]
        entries = self.fetch(payloads)
        self.assertEqual([10, 20], self.limits)
        self.assertEqual(10, len(entries))

    def test_small_channel_exhaustion_is_success_even_without_dates(self):
        evidence = []
        entries = self.fetch([self.payload(1, None)], evidence)
        self.assertEqual(1, len(entries))
        self.assertEqual('exhausted', self.source['discovery_stop'])
        self.assertEqual([], evidence)

    def test_empty_finished_channel_is_success(self):
        evidence = []
        self.assertEqual([], self.fetch([], evidence))
        self.assertEqual('exhausted', self.source['discovery_stop'])
        self.assertEqual([], evidence)

    def test_short_output_without_finish_is_not_exhaustion(self):
        evidence = []
        entries = self.fetch([self.payload(1, self.now)], evidence, finished=False)
        self.assertEqual(1, len(entries))
        self.assertEqual('partial', self.source['discovery_stop'])
        self.assertIn('完成凭据', evidence[0])

    def test_later_page_failure_keeps_previous_candidates_without_success(self):
        evidence = []
        payloads = [self.payload(number, self.now) for number in range(1, 1001)]
        entries = self.fetch(payloads, evidence, failure=RuntimeError('upstream timeout'))
        self.assertEqual([10, 20], self.limits)
        self.assertEqual(10, len(entries))
        self.assertEqual('partial', self.source['discovery_stop'])
        self.assertIn('upstream timeout', evidence[0])

    def test_deadline_does_not_confirm_a_full_recent_batch(self):
        evidence = []
        payloads = [self.payload(number, self.now) for number in range(1, 1001)]
        with patch.object(windowed_discovery.time, 'monotonic', side_effect=[0, 2]):
            entries = self.fetch(payloads, evidence, deadline=1)
        self.assertEqual([10], self.limits)
        self.assertEqual(10, len(entries))
        self.assertEqual('partial', self.source['discovery_stop'])
        self.assertIn('时间预算', evidence[0])

    def test_playlist_positions_prevent_false_exhaustion_when_entries_are_missing(self):
        payloads = [self.payload(number, self.now) for number in range(1, 10)]
        payloads[-1]['playlist_index'] = 10
        observation = discovery_pages.inspect('\n'.join(json.dumps(item) for item in payloads) +
            '\n[download] Finished downloading playlist: fixture', self.floor, 10)
        self.assertEqual(10, observation['observed'])
        self.assertFalse(observation['exhausted'])
