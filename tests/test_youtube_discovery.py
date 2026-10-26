from types import SimpleNamespace
from unittest.mock import patch

from kb.feeds.fetching import FeedError
from kb.youtube import discovery
from tests.youtube_fixture import YouTubeFixture


class YouTubeDiscovery(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.source = self.channel()

    def feed(self, entries):
        identity = self.source['url'].rsplit('/', 1)[-1]
        body = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
                'xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
                'xmlns:media="http://search.yahoo.com/mrss/">' + entries.replace('CHANNEL', identity) + '</feed>')
        return SimpleNamespace(body=body.encode())

    def test_official_rss_parses_identity_description_views_and_shanghai_date(self):
        response = self.feed('''<entry><id>yt:video:12345678901</id><yt:videoId>12345678901</yt:videoId>
          <yt:channelId>CHANNEL</yt:channelId><title>New research</title>
          <published>2026-10-23T22:30:00Z</published><media:group>
          <media:description>AI agent benchmark</media:description>
          <media:community><media:statistics views="1234"/></media:community></media:group></entry>''')
        with patch.object(discovery, 'request', return_value=response):
            entries = discovery.fetch(self.source)
        self.assertEqual(1, len(entries))
        self.assertEqual('AI agent benchmark', entries[0]['description'])
        self.assertEqual(1234, entries[0]['views'])
        self.assertTrue(entries[0]['published'].startswith('2026-10-24T06:30:00'))
        self.assertEqual('https://www.youtube.com/watch?v=12345678901', entries[0]['url'])

    def test_valid_empty_feed_is_not_malformed_entries(self):
        with patch.object(discovery, 'request', return_value=self.feed('')):
            self.assertEqual([], discovery.fetch(self.source))
        response = self.feed('<entry><id>invalid</id><title>Invalid video</title></entry>')
        with patch.object(discovery, 'request', return_value=response), self.assertRaises(FeedError):
            discovery.fetch(self.source)

    def test_handles_resolve_without_browser_or_login(self):
        source = dict(self.source)
        identity = source['url'].rsplit('/', 1)[-1]
        source['url'] = 'https://www.youtube.com/@fixture'
        response = SimpleNamespace(body=('"externalId":"' + identity + '"').encode())
        with patch.object(discovery, 'request', return_value=response) as request:
            self.assertEqual(identity, discovery.channel_id(source, 100))
        request.assert_called_once_with(source['url'], deadline=100)
