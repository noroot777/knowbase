import json
import sqlite3
from collections import Counter
from unittest.mock import patch

from kb.collectors import get_collector
from kb.feeds import registry as reg
from kb.youtube import channels, settings
from tests.youtube_fixture import YouTubeFixture


class YouTubeChannels(YouTubeFixture):
    def test_seed_existing_and_new_install_and_deletion(self):
        sources = reg.subscriptions('youtube')
        self.assertEqual(52, len(sources))
        self.assertEqual(48, sum(source['enabled'] for source in sources))
        self.assertEqual({'creator_core': 38, 'interview_pool': 10, 'creator_watchlist': 4},
                         Counter(self.policy(source)['pool'] for source in sources))
        reg.delete_subscription('youtube', sources[0]['id'])
        self.assertEqual(51, len(reg.subscriptions('youtube')))
        with sqlite3.connect(reg.DB_PATH) as connection:
            connection.execute("DELETE FROM feed_meta WHERE key='youtube-defaults-rev'")
            connection.execute("DELETE FROM subscriptions WHERE kind='youtube'")
        self.assertEqual(52, len(reg.subscriptions('youtube')))

    def test_weights_and_interview_watchlist_remain_eligible(self):
        by_name = {source['name']: self.policy(source) for source in reg.subscriptions('youtube')}
        self.assertEqual(1.25, by_name['AI Search']['score_multiplier'])
        self.assertEqual(.75, by_name['Matthew Berman']['score_multiplier'])
        self.assertEqual(1, by_name['Nate Herk | AI Automation']['score_multiplier'])
        self.assertTrue(by_name['Lex Fridman']['ranking_enabled'])
        self.assertFalse(by_name['Fireship']['ranking_enabled'])

    def test_channel_crud_and_custom_policy(self):
        response = self.client.post('/api/intake/youtube/sources', json={
            'name': '测试频道', 'url': 'https://www.youtube.com/@Example/videos',
            'youtube': {'pool': 'interview_pool', 'score_multiplier': 1.5}})
        self.assertEqual(201, response.status_code, response.text)
        source = response.json()
        self.assertEqual('https://www.youtube.com/@example', source['url'])
        updated = self.client.patch(f"/api/intake/youtube/sources/{source['id']}",
                                    json={'enabled': False}).json()
        self.assertEqual(1.5, self.policy(updated)['score_multiplier'])
        self.assertEqual('interview_pool', self.policy(updated)['pool'])
        changed = reg.save_subscription('youtube', {'url': 'https://www.youtube.com/@another'}, source['id'])
        self.assertEqual('creator_core', self.policy(changed)['pool'])
        self.assertFalse(changed['fulltext'])

    def test_invalid_channel_links_and_weights(self):
        for url in ('https://youtu.be/12345678901', 'https://youtube.com/watch?v=12345678901',
                    'https://youtube.com:4430/@example', 'https://example.com/@example',
                    'http://youtube.com/@example'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                reg.save_subscription('youtube', {'name': '错误频道', 'url': url})
        source = self.channel()
        for value in (-1, 4, True, float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reg.save_subscription('youtube', {'youtube': {'score_multiplier': value}}, source['id'])
        with self.assertRaises(ValueError):
            reg.save_subscription('youtube', {'feed_url': 'https://example.com/feed'}, source['id'])

    def test_settings_validation_and_status(self):
        self.assertEqual(2, reg.get_settings('youtube')['youtube']['asr_threads'])
        response = self.client.put('/api/intake/youtube/settings', json={'youtube': {'max_videos': 1}})
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()['youtube']['max_videos'])
        for values in ({'max_videos': 0}, {'asr_threads': 9}, {'max_videos': True},
                       {'cookies_from_browser': 'chrome'}, {'asr_enabled': 'yes'}):
            self.assertEqual(400, self.client.put('/api/intake/youtube/settings', json={'youtube': values}).status_code)
        with patch('kb.youtube.notes.available', return_value=(False, '请配置模型')):
            response = self.client.get('/api/intake/youtube/status')
        self.assertFalse(response.json()['models_ready'])
        self.assertTrue(hasattr(get_collector('youtube'), 'materialize'))
        self.assertEqual(settings.DEFAULTS.keys(), reg.get_settings('youtube')['youtube'].keys())

    def test_source_catalog_and_rank_endpoint(self):
        catalog = self.client.get('/api/intake/modules').json()['modules']
        self.assertIn('youtube', {item['id'] for item in catalog})
        self.assertEqual([], self.client.get('/api/intake/youtube/ranking').json()['items'])
        self.assertEqual(52, len(channels.defaults()['channels']))
