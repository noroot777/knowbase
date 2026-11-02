"""Phase ownership and confirmed coverage are facts, not provisional scan messages."""
from datetime import timedelta
from itertools import permutations
import sqlite3
from unittest.mock import patch
from uuid import uuid4

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_cooldown, x_feedgrab
from kb.feeds import x_intake_store as store
from kb.feeds.x_cdp import XBudgetExceeded
from kb.feeds.x_intake_schema import SCHEMA
from tests.test_x_intake import IntakeBase, Session


class ProgressFixture(IntakeBase):
    def setUp(self):
        super().setUp()
        with reg.db() as conn:
            conn.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                         ((self.now - timedelta(days=10)).isoformat(), self.source['id']))
        self.source = reg.subscription('social', self.source['id'])
        self.prepare()

    def receipt(self, phase, *, entries=None, complete=True, missing='', boundary=None, end=None):
        entries = [self.entry()] if entries is None else entries
        boundary = boundary or store.floor_for(self.source, self.now).isoformat()
        end = end or self.now.isoformat()
        observed = store.observed(entries)
        batch_id = uuid4().hex
        store.attempt(self.source, phase)
        store.stage(self.source, entries, phase=phase, incomplete=missing,
                    observed=observed, boundary=boundary, end=end, batch_id=batch_id)
        return dict(phase=phase, entries=entries, incomplete=missing, observed=observed,
                    boundary=boundary, end=end, coverage_complete=complete, batch_id=batch_id)

    def accept(self, phase, **kwargs):
        self.assertTrue(store.confirm(self.source, **self.receipt(phase, **kwargs)))
        return store.progress(self.source)

    def ledger(self):
        source = reg.subscription('social', self.source['id'])
        return {key: source[key] for key in ('last_success', 'last_error', 'failures', 'retry_at')}


class ProvisionalReceiptTests(ProgressFixture):
    def test_staging_never_publishes_latest_coverage_or_complete_discovery(self):
        for phase in ('discovery', 'history', 'combined', 'recovery'):
            with self.subTest(phase=phase):
                store.prepare([self.source], 'browser', self.now)
                before = store.progress(self.source)
                receipt = self.receipt(phase)
                staged = store.progress(self.source)
                self.assertEqual(tuple(staged[key] for key in ('latest', 'covered', 'coverage_end', 'gap')),
                                 tuple(before[key] for key in ('latest', 'covered', 'coverage_end', 'gap')))
                self.assertNotEqual(staged['discovery'], 'complete')
                self.assertEqual(store.delivery(self.source)[0], [])
                self.assertEqual(staged['unconfirmed'], 1)
                self.assertTrue(store.confirm(self.source, **receipt))
                self.assertEqual(store.progress(self.source)['latest'], receipt['observed'])
                with reg.db() as conn:
                    conn.execute('DELETE FROM x_intake_links')
                    conn.execute("UPDATE x_intake_sources SET latest='',covered='',coverage_end='' WHERE source_id=?",
                                 (self.source['id'],))

    def test_unconfirmed_later_scan_cannot_advance_confirmed_observation(self):
        accepted = self.accept('history', entries=[self.entry('100', age=3)])
        self.receipt('discovery', entries=[self.entry('101', age=1)])
        state = store.progress(self.source)
        self.assertEqual(state['latest'], accepted['latest'])
        self.assertEqual(state['covered'], accepted['covered'])
        self.assertEqual([store.post_id(entry) for entry in store.delivery(self.source)[0]], ['100'])

    def test_unknown_phase_fails_before_creating_batch_or_mutating_progress(self):
        before = store.progress(self.source)
        with self.assertRaises(ValueError):
            self.receipt('unknown')
        self.assertEqual(store.progress(self.source), before)
        with reg.read_db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM x_intake_batches').fetchone()[0], 0)

    def test_invalid_coverage_proof_rolls_back_candidate_confirmation(self):
        receipt = self.receipt('history')
        receipt['end'] = (self.now - timedelta(days=10)).isoformat()
        with self.assertRaises(ValueError):
            store.confirm(self.source, **receipt)
        self.assertEqual(store.progress(self.source)['unconfirmed'], 1)
        self.assertEqual(store.delivery(self.source)[0], [])


class PhaseOwnershipTests(ProgressFixture):
    def test_history_and_recovery_cannot_claim_discovery_ran(self):
        for phase in ('history', 'recovery'):
            with self.subTest(phase=phase):
                state = self.accept(phase)
                self.assertEqual(state['discovery'], 'unattempted')
                self.assertEqual(state['scan_at'], 0)
                self.assertGreater(state['history_at'], 0)
                self.assertEqual(state['gap'], '')

    def test_complete_history_does_not_relabel_partial_discovery(self):
        self.accept('discovery', complete=False, missing='发现切片只读到一部分')
        state = self.accept('history')
        self.assertEqual(state['discovery'], 'partial')
        self.assertEqual(state['gap'], '')

    def test_history_failure_does_not_relabel_complete_discovery(self):
        bound = (self.now - timedelta(days=1)).isoformat()
        self.accept('discovery', boundary=bound)
        state = self.accept('history', complete=False, missing='历史重扫未到底')
        self.assertEqual(state['discovery'], 'complete')
        self.assertEqual(state['gap'], '历史重扫未到底')

    def test_discovery_timeout_is_not_a_history_gap_when_history_is_proven(self):
        self.accept('history')
        state = self.accept('discovery', complete=False, missing='本次2次滚动用完',
                            boundary=(self.now - timedelta(days=1)).isoformat())
        self.assertEqual(state['discovery'], 'partial')
        self.assertEqual(state['gap'], '')
        sources = [dict(self.source)]
        store.statuses(sources)
        self.assertNotIn('x_intake_history', {issue['kind'] for issue in sources[0]['x_intake']['issues']})

    def test_discovery_message_cannot_overwrite_a_real_history_gap(self):
        self.accept('history', complete=False, missing='五天历史只有半段')
        state = self.accept('discovery', complete=False, missing='发现2次滚动用完')
        self.assertNotIn('发现2次', state['gap'])
        self.assertNotEqual(state['gap'], '')

    def test_confirmation_order_does_not_change_independent_phase_facts(self):
        for order in permutations(('discovery', 'history')):
            with self.subTest(order=order):
                receipts = {'discovery': self.receipt('discovery', complete=False, missing='发现没读完'),
                            'history': self.receipt('history')}
                for phase in order:
                    self.assertTrue(store.confirm(self.source, **receipts[phase]))
                state = store.progress(self.source)
                self.assertEqual(state['discovery'], 'partial')
                self.assertEqual(state['gap'], '')

    def test_opaque_combined_scan_explicitly_owns_both_phases(self):
        state = self.accept('combined')
        self.assertEqual(state['discovery'], 'complete')
        self.assertEqual(state['gap'], '')
        self.assertGreater(state['scan_at'], 0)
        self.assertGreater(state['history_at'], 0)


class CoverageIntervalTests(ProgressFixture):
    def test_existing_database_adds_frozen_window_end_without_replacing_old_receipts(self):
        path = self.root / 'legacy.db'
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA.replace("window_end TEXT NOT NULL DEFAULT '', ", ''))
            conn.execute('''INSERT INTO x_intake_sources(source_id,identity,channel,floor,latest,covered,coverage_end)
                VALUES(999,'legacy','browser',?,?,?,?)''',
                (self.now.isoformat(), self.now.isoformat(), self.now.isoformat(), self.now.isoformat()))
        with patch.object(reg, 'DB_PATH', path), reg.db() as conn:
            state = dict(conn.execute('SELECT * FROM x_intake_sources WHERE source_id=999').fetchone())
            self.assertEqual(state['window_end'], '')
            self.assertEqual(state['latest'], self.now.isoformat())
            self.assertEqual(state['covered'], self.now.isoformat())

    def test_prepare_reopens_gap_after_window_advances_not_from_a_note(self):
        self.accept('history')
        with reg.db() as conn:
            conn.execute("UPDATE x_intake_sources SET gap='发现阶段旧超时文案' WHERE source_id=?", (self.source['id'],))
        store.prepare([self.source], 'browser', self.now)
        self.assertEqual(store.progress(self.source)['gap'], '')
        store.prepare([self.source], 'browser', self.now + timedelta(days=2))
        state = store.progress(self.source)
        self.assertNotEqual(state['gap'], '')
        self.assertNotIn('旧超时', state['gap'])

    def test_gap_requires_both_interval_ends_not_only_the_old_lower_bound(self):
        state = self.accept('history', end=(self.now - timedelta(days=2)).isoformat())
        self.assertNotEqual(state['gap'], '')
        store.prepare([self.source], 'browser', self.now)
        self.assertEqual(state['covered'], store.progress(self.source)['floor'])
        self.assertNotEqual(store.progress(self.source)['gap'], '')

    def test_disjoint_old_proof_cannot_bridge_or_destroy_newer_coverage(self):
        boundary = (self.now - timedelta(days=1)).isoformat()
        recent = self.accept('discovery', boundary=boundary)
        state = self.accept('history', boundary=(self.now - timedelta(days=5)).isoformat(),
                            end=(self.now - timedelta(days=3)).isoformat())
        self.assertEqual((state['covered'], state['coverage_end']), (recent['covered'], recent['coverage_end']))
        self.assertNotEqual(state['gap'], '')

    def test_touching_proofs_merge_but_a_middle_hole_is_never_guessed_away(self):
        self.accept('history', boundary=(self.now - timedelta(days=5)).isoformat(),
                    end=(self.now - timedelta(days=3)).isoformat())
        state = self.accept('discovery', boundary=(self.now - timedelta(days=2)).isoformat())
        self.assertEqual(state['covered'], (self.now - timedelta(days=2)).isoformat())
        self.assertNotEqual(state['gap'], '')
        state = self.accept('history', boundary=(self.now - timedelta(days=5)).isoformat(),
                            end=(self.now - timedelta(days=2)).isoformat())
        self.assertEqual(state['covered'], store.floor_for(self.source, self.now).isoformat())
        self.assertEqual(state['gap'], '')

    def test_missing_or_non_boolean_proof_does_not_advance_coverage(self):
        for proof, missing in [(False, ''), (None, ''), (1, ''), (True, None), (True, '没到底')]:
            with self.subTest(proof=proof, missing=missing):
                state = self.accept('history', complete=proof, missing=missing)
                self.assertEqual(state['covered'], '')
                self.assertNotEqual(state['gap'], '')


class CollectorProjectionTests(ProgressFixture):
    def collect(self, scanner):
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''), \
             patch.object(x_browser, '_harvest_one', side_effect=scanner), \
             patch.object(x_cooldown, 'of', side_effect=lambda *args: x_cooldown.SessionCooldown(first=0, maximum=0)):
            return sync.collect('social', FetchScope(force=True))

    def test_history_can_save_and_clear_connection_failure_without_faking_discovery(self):
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET last_error='旧连接失败',failures=2,retry_at=42 WHERE id=?", (self.source['id'],))

        def scanner(*args, **kwargs):
            if kwargs['max_scrolls'] == 2:
                raise XBudgetExceeded('发现预算耗尽')
            return [self.entry()], ''

        result = self.collect(scanner)
        self.assertEqual(result['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(result['added'], 1)
        self.assertEqual(self.ledger()['last_error'], '')
        self.assertEqual(store.progress(self.source)['discovery'], 'partial')
        self.assertEqual(store.progress(self.source)['gap'], '')

    def test_feedgrab_combined_scan_confirms_both_phases_and_recovers_old_candidate(self):
        self.settings('feedgrab')
        store.prepare([self.source], 'feedgrab', self.now)
        self.receipt('history', entries=[self.entry('100', age=72)])
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_feedgrab, 'harvest', return_value={'example': {'entries': [self.entry('101')], 'incomplete': '', 'error': ''}}):
            result = sync.collect('social', FetchScope(force=True))
        self.assertEqual(result['added'], 2)
        self.assertEqual(result['succeeded_ids'], [str(self.source['id'])])
        state = store.progress(self.source)
        self.assertEqual(state['discovery'], 'complete')
        self.assertEqual(state['unconfirmed'], 0)
        self.assertEqual(state['gap'], '')
        self.assertFalse(result['x_intake']['timeline_phases_separable'])

    def test_feedgrab_partial_does_not_claim_either_phase_complete(self):
        self.settings('feedgrab')
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_feedgrab, 'harvest', return_value={'example': {'entries': [self.entry()], 'incomplete': '上游仍有缺页', 'error': ''}}):
            result = sync.collect('social', FetchScope(force=True))
        self.assertEqual(result['added'], 1)
        state = store.progress(self.source)
        self.assertEqual(state['discovery'], 'partial')
        self.assertEqual(state['gap'], '上游仍有缺页')
        self.assertGreater(state['history_at'], 0)
