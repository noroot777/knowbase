"""原文链接能力约束旧补抓、订阅保存和翻译/挂起出口。仅在验收阶段执行。"""
import hashlib
from dataclasses import replace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.feeds import fulltext, registry as reg, translate
from kb.source_types import SOURCE_TYPES, SourceType, catalog, feed_types
from kb.web import intake_api
from tests.test_feed_fulltext import FullTextCase, PAGE, SHELL, page


class CapabilityCase(FullTextCase):
    def legacy_item(self, kind, hold=''):
        self.legacy_serial = getattr(self, 'legacy_serial', 0) + 1
        source = reg.save_subscription(kind, {
            'name': f'{kind} legacy',
            'url': f'https://{kind}.example.org/feed/{self.legacy_serial}',
            'extract': True,
        })
        url = f'https://example.org/{kind}-article/{self.legacy_serial}'
        reg.put_items(source, [{
            'url': url, 'title': 'An English article', 'body': SHELL,
            'content_kind': 'summary', 'published': reg.now(),
        }])
        key = hashlib.sha256(f'{kind}:{url}'.encode()).hexdigest()[:32]
        with reg.db() as conn:
            conn.execute('UPDATE subscriptions SET fulltext=1 WHERE id=?', (source['id'],))
            conn.execute('UPDATE feed_items SET fulltext_hold=?,lang=? WHERE key=?',
                         (hold, 'en', key))
        return source, key

    def matches(self, key, predicate):
        condition, args = predicate()
        with reg.db() as conn:
            return bool(conn.execute(
                f'SELECT 1 FROM feed_items WHERE key=? AND ({condition})',
                (key, *args),
            ).fetchone())


class FulltextCapabilityTests(CapabilityCase):
    def test_legacy_disallowed_sources_never_fetch_or_block_translation(self):
        for kind in ('digest', 'social'):
            for hold in ('', '正文身份待确认'):
                with self.subTest(kind=kind, hold=hold):
                    source, key = self.legacy_item(kind, hold)
                    before = self.row(key)
                    request = Mock(side_effect=AssertionError('不应请求第三方原文'))
                    with patch.object(fulltext, 'request', request):
                        self.assertEqual(0, fulltext.pending(kind))
                        self.assertEqual(0, fulltext.fetch_pending(kind)['done'])
                    request.assert_not_called()
                    self.assertEqual([], fulltext.held(kind))
                    self.assertEqual(0, fulltext.release([key], kind))
                    self.assertFalse(self.matches(key, fulltext.unsettled_sql))
                    self.assertTrue(self.matches(key, translate.queued_sql))
                    with patch.object(translate, 'translate_item',
                                      return_value=('简讯标题', '这还是原简讯的译文')):
                        self.assertEqual(1, translate.translate_pending(kind)['done'])
                    after = self.row(key)
                    for field in ('body', 'content_kind', 'content_hash', 'content_rev',
                                  'fulltext_at', 'fulltext_tries', 'fulltext_hold'):
                        self.assertEqual(before[field], after[field], field)
                    self.assertEqual('这还是原简讯的译文', after['body_zh'])
                    reg.delete_subscription(kind, source['id'])

    def test_supported_sources_still_fetch_then_queue_the_new_body_for_translation(self):
        for kind in ('rss', 'web', 'inbox'):
            with self.subTest(kind=kind):
                _, key = self.legacy_item(kind)
                self.assertTrue(self.matches(key, fulltext.queued_sql))
                self.assertFalse(self.matches(key, translate.queued_sql))
                with patch.object(fulltext, 'request', return_value=page(PAGE)):
                    self.assertEqual(1, fulltext.fetch_pending(kind)['done'])
                stored = self.row(key)
                self.assertIn('The runtime schedules tools', stored['body'])
                self.assertEqual('content', stored['content_kind'])
                self.assertTrue(stored['fulltext_at'])
                self.assertEqual(1, stored['content_rev'])
                self.assertTrue(self.matches(key, translate.queued_sql))

    def test_supported_hold_release_and_last_membership_rules_remain(self):
        for kind in ('rss', 'web', 'inbox'):
            with self.subTest(kind=kind):
                source, key = self.legacy_item(kind, '正文身份待确认')
                self.assertFalse(self.matches(key, fulltext.queued_sql))
                self.assertTrue(self.matches(key, fulltext.unsettled_sql))
                self.assertFalse(self.matches(key, translate.queued_sql))
                self.assertEqual([key], [row['key'] for row in fulltext.held(kind)])
                self.assertEqual(1, fulltext.release([key, key], kind))
                self.assertEqual(0, fulltext.release([key], kind))
                self.assertTrue(self.matches(key, fulltext.queued_sql))
                reg.save_subscription(kind, {'fulltext': False}, source['id'])
                self.assertFalse(self.matches(key, fulltext.unsettled_sql))
                self.assertTrue(self.matches(key, translate.queued_sql))

    def test_disallowed_membership_cannot_grant_permission_to_a_supported_item(self):
        source, disallowed_key = self.legacy_item('digest')
        key = self.put(self.plain)
        with reg.db() as conn:
            conn.execute('INSERT INTO subscription_items VALUES(?,?)', (source['id'], key))
        self.assertFalse(self.matches(key, fulltext.queued_sql))
        with reg.db() as conn:
            conn.execute('INSERT INTO subscription_items VALUES(?,?)', (self.source['id'], key))
            conn.execute('INSERT INTO subscription_items VALUES(?,?)',
                         (self.source['id'], disallowed_key))
        self.assertTrue(self.matches(key, fulltext.queued_sql))
        self.assertFalse(self.matches(disallowed_key, fulltext.queued_sql))
        reg.delete_subscription('rss', self.source['id'])
        self.assertFalse(self.matches(key, fulltext.queued_sql))

    def test_no_capable_types_is_a_closed_queue_and_the_catalog_defaults_closed(self):
        key = self.put()
        disabled = {kind: replace(source, body_from_link=False)
                    for kind, source in SOURCE_TYPES.items()}
        with patch.dict(SOURCE_TYPES, disabled):
            self.assertFalse(self.matches(key, fulltext.queued_sql))
            self.assertFalse(self.matches(key, fulltext.unsettled_sql))
        self.assertFalse(SourceType('future', '新来源', '', ('future',)).body_from_link)
        self.assertEqual({'rss', 'web', 'inbox'},
                         {source['id'] for source in catalog() if source['body_from_link']})

    def test_default_seed_cannot_enable_a_source_without_the_capability(self):
        disabled_rss = replace(SOURCE_TYPES['rss'], body_from_link=False)
        with patch.dict(SOURCE_TYPES, {'rss': disabled_rss}), \
                patch.object(reg, 'DB_PATH', self.root / 'closed-defaults.db'):
            with reg.db() as conn:
                seeded = conn.execute(
                    "SELECT fulltext FROM subscriptions WHERE name='HN Buzzing'",
                ).fetchone()
        self.assertIsNotNone(seeded)
        self.assertEqual(0, seeded['fulltext'])


class SubscriptionCapabilityTests(CapabilityCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(intake_api.router)
        self.client = TestClient(app)
        guard = patch.object(intake_api, 'guard', lambda kind: reg.check_kind(kind))
        guard.start()
        self.addCleanup(guard.stop)

    def test_create_and_explicit_enable_are_rejected_without_partial_writes(self):
        for kind, source_type in feed_types().items():
            if source_type.body_from_link:
                continue
            with self.subTest(kind=kind):
                before = len(reg.subscriptions(kind))
                response = self.client.post(f'/api/intake/{kind}/sources', json={
                    'name': '不允许补抓', 'url': f'https://{kind}.example.org/invalid',
                    'fulltext': True,
                })
                self.assertEqual(400, response.status_code)
                self.assertIn('不支持', response.json()['detail'])
                self.assertEqual(before, len(reg.subscriptions(kind)))
                source, _ = self.legacy_item(kind)
                response = self.client.patch(f"/api/intake/{kind}/sources/{source['id']}",
                                             json={'name': '不应保存', 'fulltext': True})
                self.assertEqual(400, response.status_code)
                self.assertEqual(source['name'], reg.subscription(kind, source['id'])['name'])

    def test_unrelated_patch_repairs_legacy_flag_without_resetting_other_options(self):
        for kind in ('digest', 'social'):
            with self.subTest(kind=kind):
                source, key = self.legacy_item(kind, '正文身份待确认')
                response = self.client.patch(f"/api/intake/{kind}/sources/{source['id']}",
                                             json={'name': '改名后的订阅'})
                self.assertEqual(200, response.status_code)
                self.assertEqual(0, response.json()['fulltext'])
                self.assertEqual(1, response.json()['extract'])
                self.assertEqual(1, response.json()['enabled'])
                self.assertEqual('正文身份待确认', self.row(key)['fulltext_hold'])

    def test_supported_create_and_partial_patch_keep_the_fulltext_choice(self):
        for kind in ('rss', 'web', 'inbox'):
            with self.subTest(kind=kind):
                response = self.client.post(f'/api/intake/{kind}/sources', json={
                    'name': '允许补抓', 'url': f'https://{kind}.example.org/valid',
                    'fulltext': True, 'extract': True,
                })
                self.assertEqual(201, response.status_code)
                source_id = response.json()['id']
                response = self.client.patch(f'/api/intake/{kind}/sources/{source_id}',
                                             json={'name': '保留补抓选择'})
                self.assertEqual(200, response.status_code)
                self.assertEqual(1, response.json()['fulltext'])
                self.assertEqual(1, response.json()['extract'])
