"""存量复查与自动补抓共用资格,扫描到写回之间撤销资格不得销毁正文。仅在验收阶段执行。"""
from dataclasses import replace
from unittest.mock import patch

from kb.feeds import fulltext, registry as reg
from kb.source_types import SOURCE_TYPES, feed_types
from scripts import feed_fulltext_recheck as recheck
from tests.test_fulltext_capability import CapabilityCase
from tests.test_fulltext_failures import LEGAL_TEXT


class RecheckCapabilityTests(CapabilityCase):
    def wrong_article(self, kind):
        source, key = self.legacy_item(kind)
        with reg.db() as conn:
            conn.execute(
                "UPDATE feed_items SET body=?,content_kind='content',content_hash=?,"
                "fulltext_at='2026-10-26T09:00:00+08:00',title_zh='原标题译文',"
                "body_zh='原正文译文',translated_at='2026-10-26T09:01:00+08:00',"
                'exported_hash=content_hash WHERE key=?',
                (LEGAL_TEXT, fulltext.body_hash(LEGAL_TEXT), key),
            )
        return source, key

    def plan(self, **scope):
        return recheck._plan(recheck._suspect(**scope), [])

    def test_legacy_disallowed_sources_keep_their_body_and_translation(self):
        for kind, spec in feed_types().items():
            if spec.body_from_link:
                continue
            with self.subTest(kind=kind):
                source, key = self.wrong_article(kind)
                before = self.row(key)
                self.assertEqual([], self.plan())
                self.assertEqual([], self.plan(kind=kind, source=source['id']))
                self.assertEqual(([], []), recheck._downgrade(self.plan()))
                self.assertEqual(before, self.row(key))
                self.assertFalse(self.matches(key, fulltext.queued_sql))

    def test_capable_sources_return_to_a_real_refill_queue(self):
        for kind, spec in feed_types().items():
            if not spec.body_from_link:
                continue
            with self.subTest(kind=kind):
                source, key = self.wrong_article(kind)
                planned = self.plan(kind=kind, source=source['id'])
                self.assertEqual([key], [row['key'] for row in planned])
                cut, moved = recheck._downgrade(planned)
                self.assertEqual(planned, cut)
                self.assertEqual([], moved)
                stored = self.row(key)
                self.assertEqual(planned[0]['shell'], stored['body'])
                self.assertEqual(fulltext.body_hash(stored['body']), stored['content_hash'])
                self.assertEqual('summary', stored['content_kind'])
                for field in ('fulltext_at', 'body_zh', 'title_zh', 'translated_at'):
                    self.assertEqual('', stored[field], field)
                self.assertTrue(self.matches(key, fulltext.queued_sql))

    def test_revoking_the_last_membership_after_scan_preserves_the_whole_item(self):
        for revoke in ('disable', 'delete'):
            with self.subTest(revoke=revoke):
                source, key = self.wrong_article('rss')
                planned = self.plan(kind='rss', source=source['id'])
                self.assertEqual([key], [row['key'] for row in planned])
                if revoke == 'disable':
                    reg.save_subscription('rss', {'fulltext': False}, source['id'])
                else:
                    reg.delete_subscription('rss', source['id'])
                before = self.row(key)
                cut, moved = recheck._downgrade(planned)
                self.assertEqual([], cut)
                self.assertEqual(planned, moved)
                self.assertEqual(before, self.row(key))
                self.assertEqual([], self.plan(kind='rss', source=source['id']))

    def test_revoking_the_source_capability_after_scan_preserves_the_whole_item(self):
        source, key = self.wrong_article('rss')
        planned = self.plan(kind='rss', source=source['id'])
        self.assertEqual([key], [row['key'] for row in planned])
        before = self.row(key)
        disabled = replace(SOURCE_TYPES['rss'], body_from_link=False)
        with patch.dict(SOURCE_TYPES, {'rss': disabled}):
            cut, moved = recheck._downgrade(planned)
            self.assertEqual([], cut)
            self.assertEqual(planned, moved)
            self.assertEqual([], self.plan(kind='rss'))
        self.assertEqual(before, self.row(key))

    def test_another_enabled_membership_keeps_the_recheck_eligible(self):
        source, key = self.wrong_article('rss')
        other = reg.save_subscription('rss', {
            'name': '仍要补抓', 'url': 'https://other.example.org/feed', 'fulltext': True,
        })
        with reg.db() as conn:
            conn.execute('INSERT INTO subscription_items VALUES(?,?)', (other['id'], key))
        planned = self.plan(kind='rss', source=source['id'])
        self.assertEqual([key], [row['key'] for row in planned])
        reg.save_subscription('rss', {'fulltext': False}, source['id'])
        cut, moved = recheck._downgrade(planned)
        self.assertEqual(planned, cut)
        self.assertEqual([], moved)
        self.assertTrue(self.matches(key, fulltext.queued_sql))
