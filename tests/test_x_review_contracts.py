"""Audit convergence, transient HTTP conflicts and informational sync receipts."""
import contextlib
import json
import sqlite3
import time
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_channels, x_post
from scripts import x_post_audit as audit, x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import Quick, tweet
from tests.test_x_page_policy import Clock, PolicySession
from tests.test_x_page_state import response
from tests.test_x_post import BODY, OLD, PREVIEW
from tests.test_x_post_backfill import backfill_entry
from tests.test_x_post_scope import OpenTimeline


class ReviewContractsTests(ChannelBase):
    def row(self):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())

    def test_http_empty_conflicts_remain_in_backfill_queue_until_a_successful_retry(self):
        source = self.sub()
        reg.put_items(source, [backfill_entry(tweet('123', text=PREVIEW + '…'))])
        original = self.row()
        for status in [429, 403, 404, 503]:
            with self.subTest(status=status):
                session = PolicySession({'Example': ([response(status)], dict(empty=True))},
                                        detail=True)
                clock = Clock()
                with patch.object(time, 'monotonic', clock.monotonic), \
                     patch.object(time, 'sleep', clock.sleep), patch.object(x_post, 'SECONDS', 1):
                    stats = backfill.repair([original], session, apply=True, remember=True)
                self.assertEqual('failed', stats['results'][0]['state'])
                self.assertEqual(original, self.row())
                self.assertEqual([original], audit.select([original], apply=True))
        session = PolicySession({'Example': ([], dict(id='123', text=BODY))}, detail=True)
        with patch.object(x_post, 'STEP', 0):
            stats = backfill.repair([original], session, apply=True, remember=True)
        self.assertEqual(1, stats['updated'])
        self.assertEqual(BODY, self.row()['body'])
        self.assertEqual([], audit.select([self.row()], apply=True))

    def test_matching_blocked_audit_debt_settles_without_changing_the_source_version(self):
        reg.put_items(self.sub(), [backfill_entry(tweet('123', text='Read\n\nhttps://example.org'))])
        original = self.row()
        for state in ['blocked_whitespace', 'blocked_evidence']:
            with self.subTest(state=state):
                audit.record(original, dict(state='normalized', remaining=state))
                for transient in ['failed', 'stale', 'deferred', 'identical']:
                    audit.record(original, dict(state=transient))
                    self.assertEqual([original], audit.select([original]))
                audit.record(original, dict(state=state, reason='unproven'))
                for _ in range(5):
                    self.assertEqual([], audit.select([original], apply=True))
                self.assertEqual([original], audit.select([original], recheck=True))
                self.assertEqual(original, self.row())
                changed = dict(original, content_rev=original['content_rev'] + 1)
                self.assertEqual([changed], audit.select([changed]))

    def test_old_unavailable_receipts_are_rechecked_once_without_reopening_other_conclusions(self):
        reg.put_items(self.sub(), [backfill_entry(tweet('123', text='Retain this body'))])
        original = self.row()
        audit.record(original, dict(state='unavailable'))
        self.assertEqual([], audit.select([original]))
        with contextlib.closing(sqlite3.connect(audit.path())) as conn, conn:
            conn.execute('UPDATE audits SET result=?', (json.dumps(dict(state='unavailable')),))
        self.assertEqual([original], audit.select([original]))
        self.assertEqual(original, self.row())
        audit.record(original, dict(state='unavailable'))
        self.assertEqual([], audit.select([original]))
        audit.record(original, dict(state='blocked_whitespace'))
        self.assertEqual([], audit.select([original]))

    def test_repeated_identical_rereads_also_settle_debt_without_claiming_a_full_repair(self):
        reg.put_items(self.sub(), [backfill_entry(tweet('123', text='Retain this body'))])
        original = self.row()
        audit.record(original, dict(state='normalized', remaining='blocked_evidence'))
        audit.record(original, dict(state='identical'))
        self.assertEqual([original], audit.select([original]))
        audit.record(original, dict(state='identical'))
        self.assertEqual([], audit.select([original]))
        receipt = audit.load()[(original['key'], audit.version(original))][1]
        self.assertEqual('blocked_evidence', receipt['retained'])
        self.assertNotIn('remaining', receipt)
        self.assertEqual(original, self.row())

    def test_notices_survive_collect_without_partial_summary_or_frozen_watermarks(self):
        source = self.sub()
        self.settings(x_channels.BROWSER)
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True)
        cookies.write_bytes(b'fixture')
        x_channels.save_watermark(source['id'], x_channels.BROWSER, OLD)
        items = [dict(tweet('123', text=''), quoted=True), tweet('124', text='Own complete text')]
        with Quick(), patch.object(x_browser, 'browser_session',
                                   return_value=contextlib.nullcontext(OpenTimeline(items))):
            summary = sync.collect('social', FetchScope())
        self.assertEqual([], summary['partial'])
        self.assertEqual([], summary['errors'])
        self.assertEqual(1, summary['added'])
        self.assertEqual('Example', summary['notices'][0]['title'])
        saved = reg.subscription('social', source['id'])
        self.assertEqual('', saved['last_error'])
        self.assertEqual('', saved['last_note'])
        self.assertIn('跳过 1 条本人无正文', saved['last_notice'])
        self.assertNotEqual(OLD, x_channels.watermark(saved, x_channels.BROWSER))
        with reg.db() as conn:
            run = json.loads(conn.execute('SELECT summary FROM feed_runs ORDER BY id DESC').fetchone()[0])
        self.assertEqual(summary['notices'], run['notices'])
        self.assertEqual('Own complete text', self.row()['body'])
        row = self.client.get('/api/intake/social').json()['sources'][0]
        self.assertEqual(saved['last_notice'], row['last_notice'])

    def test_partial_reason_and_notice_are_independent_and_follow_subscription_lifecycle(self):
        source = self.sub()
        settings = self.settings(x_channels.BROWSER)
        self.harvested(example=dict(entries=[], watermark=OLD, incomplete='未读完', note='纯提示'))
        result = x_channels.take(source, settings)
        self.assertEqual('未读完', result[1]['note'])
        self.assertEqual('纯提示', result[1]['notice'])
        sync._save_result(source, result, FetchScope())
        saved = reg.subscription('social', source['id'])
        sync._save_result(saved, ([], dict(etag='', modified='', resolved_url='')),
                          FetchScope(since='2026-10-01', until='2026-10-20'))
        ranged = reg.subscription('social', source['id'])
        self.assertEqual(('未读完', '纯提示'), (ranged['last_note'], ranged['last_notice']))
        sync._save_result(ranged, ([], dict(etag='', modified='', resolved_url='')), FetchScope())
        cleared = reg.subscription('social', source['id'])
        self.assertEqual(('', ''), (cleared['last_note'], cleared['last_notice']))
        sync._save_result(cleared, result, FetchScope())
        reset = reg.save_subscription('social', dict(name='Other', url='https://x.com/Other'),
                                      source_id=source['id'])
        self.assertEqual(source['id'], reset['id'])
        reset = reg.subscription('social', source['id'])
        self.assertEqual('https://x.com/other', reset['url'])
        self.assertEqual(('', ''), (reset['last_note'], reset['last_notice']))

    def test_existing_subscription_schema_gains_an_empty_notice_without_resetting_progress(self):
        source = self.sub()
        with reg.db() as conn:
            conn.execute('ALTER TABLE subscriptions DROP COLUMN last_notice')
            conn.execute("UPDATE subscriptions SET last_note='未读完', last_success='previous'")
        migrated = reg.subscription('social', source['id'])
        self.assertEqual('', migrated['last_notice'])
        self.assertEqual('未读完', migrated['last_note'])
        self.assertEqual('previous', migrated['last_success'])
