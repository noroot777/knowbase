"""Timeline provenance rejection is per item; healthy posts persist without advancing past gaps."""
import contextlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_article, x_browser, x_channels, x_post, x_post_meta
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import Quick, Timeline, tweet


class AccountTimelines(Timeline):
    def __init__(self, accounts, **options):
        self.accounts = accounts
        self.options = options

    def open_page(self, url):
        pass

    def goto(self, url):
        super().__init__(self.accounts[url.rsplit('/', 1)[-1]], **self.options)


class TimelineEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.water = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.good = [tweet('101'), tweet('103')]
        self.bad = tweet('102', context='Someone\nreposted')
        self.items = [self.good[0], self.bad, self.good[1]]

    def collect(self, accounts=None, *, water=None, **options):
        accounts = accounts or {'example': self.items}
        page = AccountTimelines(accounts, page=10, **options)
        jobs = [(handle, self.water if water is None else water) for handle in accounts]
        with Quick(), \
                patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)):
            return x_browser.harvest(jobs, profile='/unused')

    def assert_incomplete(self, result, skipped=1):
        self.assertEqual('', result['error'])
        note = result['incomplete']
        for expected in ('@example', f'{skipped} 条', self.bad['url'], '上下文', '跳过',
                         '其余推文', '位置保持不变', '下次同步重试'):
            self.assertIn(expected, note)

    def test_bad_context_still_fails_the_independent_evidence_check(self):
        with self.assertRaises(x_post.PostUnavailable):
            x_browser._entry(self.bad)

    def test_rejection_at_any_position_keeps_both_healthy_entries(self):
        for position in range(3):
            items = list(self.good)
            items.insert(position, self.bad)
            rejected = []
            with self.subTest(position=position):
                entries = x_browser._select(items, x_browser._utc(self.water), 'example',
                                            rejected=rejected)
                self.assertEqual([item['url'] for item in self.good],
                                 [entry['url'] for entry in entries])
                self.assertEqual([self.bad['url']], rejected)
                self.assertTrue(all(x_post_meta.eligibility(entry) == ('', '') for entry in entries))

    def test_mixed_timeline_keeps_healthy_entries_on_repeated_rounds_and_continues_accounts(self):
        accounts = {'example': self.items, 'other': [tweet('201', handle='other')]}
        for attempt in range(2):
            with self.subTest(attempt=attempt), \
                    patch.object(x_article, 'fetch') as fetch:
                result = self.collect(accounts)
            fetch.assert_not_called()
            self.assertEqual([item['url'] for item in self.good],
                             [entry['url'] for entry in result['example']['entries']])
            self.assert_incomplete(result['example'])
            self.assertEqual(self.water, result['example']['watermark'])
            self.assertEqual('', result['other']['error'])
            self.assertEqual('', result['other']['incomplete'])
            self.assertEqual(1, len(result['other']['entries']))
            self.assertNotEqual(self.water, result['other']['watermark'])

    def test_stamp_rejection_is_isolated_just_like_context_rejection(self):
        stamp = x_post_meta.stamp

        def checked_stamp(entry, kind, prefix, **evidence):
            return stamp(entry, kind, '> Unproven boundary\n\n' if entry['url'] == self.bad['url']
                         else prefix, **evidence)

        items = [self.good[0], dict(self.bad, context='Someone reposted'), self.good[1]]
        with patch.object(x_post_meta, 'stamp', side_effect=checked_stamp):
            result = self.collect({'example': items})['example']
        self.assertEqual([item['url'] for item in self.good],
                         [entry['url'] for entry in result['entries']])
        self.assert_incomplete(result)
        self.assertEqual(self.water, result['watermark'])

    def test_first_round_saves_its_actual_floor_and_reuses_it_on_retry(self):
        with patch.object(x_browser, '_harvest_one', wraps=x_browser._harvest_one) as harvest_one:
            result = self.collect(water='')['example']
        used_floor = harvest_one.call_args.args[2]
        self.assert_incomplete(result)
        self.assertEqual(used_floor.isoformat(), result['watermark'])
        retry = self.collect(water=result['watermark'])['example']
        self.assertEqual(result['watermark'], retry['watermark'])
        self.assertEqual(2, len(retry['entries']))

    def test_no_floor_does_not_hide_an_item_rejection(self):
        with Quick():
            entries, incomplete = x_browser._harvest_one(Timeline(self.items, page=10), 'example', None)
        self.assertEqual(2, len(entries))
        self.assertIn(self.bad['url'], incomplete)
        self.assertIn('下次同步重试', incomplete)

    def test_existing_scroll_and_stall_notes_survive_the_item_note(self):
        for scrolls, bottom_at, reason in ((1, 3, '滚动次数用完'),
                                          (10, 99, '页面不再长出新内容')):
            with self.subTest(reason=reason), patch.object(x_browser, 'MAX_SCROLLS', scrolls):
                result = self.collect(bottom_at=bottom_at)['example']
            self.assert_incomplete(result)
            self.assertIn(reason, result['incomplete'])
            self.assertEqual(2, len(result['entries']))
            self.assertEqual(self.water, result['watermark'])

    def test_rejected_items_outside_the_window_or_timeline_do_not_hold_the_watermark(self):
        before = (x_browser._utc(self.water) - timedelta(hours=1)).isoformat()
        items = [self.good[0], dict(self.bad, datetime=before),
                 dict(self.bad, id='104', datetime=self.water),
                 tweet('105', context='Promoted\nUnknown context'),
                 tweet('106', handle='stranger')]
        with patch.object(x_browser, '_entry', wraps=x_browser._entry) as entry:
            result = self.collect({'example': items})['example']
        entry.assert_called_once_with(self.good[0])
        self.assertEqual('', result['incomplete'])
        self.assertEqual('', result['error'])
        self.assertNotEqual(self.water, result['watermark'])

    def test_all_rejected_items_are_incomplete_not_an_account_error(self):
        other_bad = dict(self.bad, id='104', url='https://x.com/Example/status/104')
        result = self.collect({'example': [self.bad, other_bad]})['example']
        self.assertEqual([], result['entries'])
        self.assert_incomplete(result, skipped=2)
        self.assertEqual(self.water, result['watermark'])

    def test_repaired_context_is_collected_and_allows_progress_again(self):
        self.assertEqual(self.water, self.collect()['example']['watermark'])
        items = [self.good[0], dict(self.bad, context='Someone reposted'), self.good[1]]
        result = self.collect({'example': items})['example']
        self.assertEqual(3, len(result['entries']))
        self.assertEqual('> Someone reposted\n\na thought', result['entries'][1]['body'])
        self.assertEqual('', result['incomplete'])
        self.assertNotEqual(self.water, result['watermark'])

    def test_completion_failure_notes_are_not_overwritten(self):
        folded = dict(self.good[1], truncated=True)
        with patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('暂不可用')):
            result = self.collect({'example': [self.good[0], self.bad, folded]})['example']
        self.assert_incomplete(result)
        self.assertIn('完整正文没取到', result['incomplete'])
        self.assertEqual([self.good[0]['url']], [entry['url'] for entry in result['entries']])
        self.assertEqual(self.water, result['watermark'])

    def test_selection_does_not_swallow_unrelated_errors_or_rate_limits(self):
        for error in (ValueError('bug'), x_browser.XRateLimited('limited')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(x_browser, '_entry', side_effect=error), \
                    self.assertRaises(type(error)):
                x_browser._select(self.good, None, 'example')


class TimelineEvidenceSyncTests(ChannelBase):
    def test_healthy_posts_persist_and_deduplicate_while_the_gap_and_note_remain(self):
        source = self.sub('Example')
        self.settings(x_channels.BROWSER)
        water = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        x_channels.save_watermark(source['id'], x_channels.BROWSER, water)
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True)
        cookies.write_bytes(b'x')
        items = [tweet('101'), tweet('102', context='Someone\nreposted'), tweet('103')]
        page = AccountTimelines({'example': items}, page=10)
        for attempt, added in enumerate((2, 0)):
            with self.subTest(attempt=attempt), Quick(), \
                    patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)):
                summary = sync.collect('social', FetchScope())
            self.assertEqual(added, summary['added'])
            self.assertEqual([], summary['errors'])
            current = reg.subscription('social', source['id'])
            self.assertEqual(water, x_channels.watermark(current, x_channels.BROWSER))
            self.assertEqual('', current['last_error'])
            self.assertEqual(1, len(summary['partial']))
            self.assertIn('@example', summary['partial'][0]['reason'])
            self.assertIn(items[1]['url'], summary['partial'][0]['reason'])
            self.assertEqual(summary['partial'][0]['reason'], current['last_note'])
            with reg.db() as conn:
                rows = conn.execute('SELECT * FROM feed_items ORDER BY url').fetchall()
            self.assertEqual([items[0]['url'], items[2]['url']], [row['url'] for row in rows])
            self.assertTrue(all(x_post_meta.eligibility(dict(row)) == ('', '') for row in rows))
