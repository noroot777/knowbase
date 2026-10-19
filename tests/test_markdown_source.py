import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kb.feeds import fulltext, grab, markdown_source, registry as reg, sync, translate
from kb.feeds.fetching import CollectionBudgetExceeded, FeedError, Response
from tests.test_feed_fulltext import FullTextCase, PAGE, page

README = '''# Demo reader

An English paragraph about a useful project.

```text
/setup          /scrape              /apply <url>
  |                |                     |
  v                v                     v
Fill profile   Search jobs          Evaluate fit
```

| Setting | Default |
| --- | --- |
| `DATA_DIR` | `./data` |
'''
REPOSITORY = 'https://github.com/owner/project'


def github_response(text=README, **changes):
    data = {'type':'file', 'name':'README.md', 'encoding':'base64',
            'content':base64.b64encode(text.encode()).decode()}
    data.update(changes)
    return Response(200, 'https://api.github.com/repos/owner/project/readme',
                    json.dumps(data).encode(), {'content-type':'application/json'})


class MarkdownSourceTests(FullTextCase):
    def test_repository_reads_original_markdown_in_one_request(self):
        request = Mock(return_value=github_response())
        self.assertEqual(README, markdown_source.fetch(REPOSITORY, transport=request, deadline=123))
        self.assertEqual(1, request.call_count)
        self.assertEqual('https://api.github.com/repos/owner/project/readme', request.call_args.args[0])
        self.assertEqual(123, request.call_args.kwargs['deadline'])

    def test_blob_path_and_encoded_branch_are_not_guessed(self):
        candidate = markdown_source.target('https://github.com/owner/project/blob/feature%2Fdocs/docs/Intro.md')
        self.assertEqual(('https://api.github.com/repos/owner/project/contents/docs/Intro.md?ref=feature%2Fdocs', True), candidate)

    def test_direct_markdown_uses_public_transport_and_keeps_indentation(self):
        document = '    preserved code\r\n    with indentation\r\n'
        request = Mock(return_value=Response(200, 'https://example.org/a.md', document.encode(),
                                             {'Content-Type':'text/markdown; charset=utf-8'}))
        self.assertEqual(document.replace('\r\n', '\n'),
                         markdown_source.fetch('https://example.org/a.md?raw=1', transport=request))

    def test_non_markdown_pages_do_not_trigger_extra_requests(self):
        request = Mock()
        for url in ('https://example.org/article', 'https://github.com/owner/project/issues/1',
                    'https://github.com.evil.org/owner/project', 'https://github.com/owner',
                    'https://example.org:bad/readme.md', 'https://user:pass@example.org/a.md'):
            with self.subTest(url=url):
                self.assertIsNone(markdown_source.fetch(url, transport=request))
        request.assert_not_called()

    def test_readme_of_another_format_is_not_mislabeled_markdown(self):
        self.assertIsNone(markdown_source.fetch(REPOSITORY,
            transport=Mock(return_value=github_response(name='README.rst'))))

    def test_rate_limit_or_malformed_base64_keeps_the_html_fallback_available(self):
        for request in (Mock(side_effect=FeedError('HTTP 403')),
                        Mock(return_value=github_response(content='%%invalid%%')),
                        Mock(return_value=github_response(content=''))):
            with self.subTest(request=request):
                self.assertIsNone(markdown_source.fetch(REPOSITORY, transport=request))

    def test_large_api_file_download_uses_the_same_budget(self):
        request = Mock(side_effect=[github_response(encoding='none', content='',
            download_url='https://raw.githubusercontent.com/owner/project/main/README.md'),
            Response(200, '', README.encode(), {'content-type':'text/plain'})])
        self.assertEqual(README, markdown_source.fetch(REPOSITORY, transport=request,
                                                      deadline=123, collect_deadline=456))
        for call in request.call_args_list:
            self.assertEqual({'deadline':123, 'collect_deadline':456}, call.kwargs)

    def test_api_metadata_cannot_redirect_download_to_an_unrelated_host(self):
        request = Mock(return_value=github_response(encoding='none', content='',
                                                    download_url='http://127.0.0.1/a.md'))
        self.assertIsNone(markdown_source.fetch(REPOSITORY, transport=request))
        self.assertEqual(1, request.call_count)

    def test_binary_and_html_responses_do_not_become_markdown(self):
        for body, mime in ((b'%PDF-1.4 document', 'text/plain'),
                           (b'<!doctype html><html>verification</html>', 'text/plain'),
                           (b'actual web page', 'text/html'),
                           (b'\x00\x01binary', 'application/octet-stream')):
            with self.subTest(body=body):
                request = Mock(return_value=Response(200, '', body, {'content-type':mime}))
                self.assertIsNone(markdown_source.fetch('https://example.org/a.md', transport=request))

    def test_fulltext_and_manual_grab_share_markdown_acquisition(self):
        with patch.object(fulltext, 'request', Mock(return_value=github_response())) as request:
            self.assertEqual((README, ''), fulltext.fetch_one(REPOSITORY))
            self.assertEqual(1, request.call_count)
        with patch.object(grab, 'request', Mock(return_value=github_response())) as request:
            entry = grab.grab(REPOSITORY)
            self.assertEqual(README, entry['body'])
            self.assertEqual('Demo reader', entry['title'])
            self.assertEqual('content', entry['content_kind'])
            self.assertEqual(1, request.call_count)

    def test_a_webpage_can_explicitly_negotiate_a_markdown_response(self):
        response = Response(200, 'https://example.org/docs', README.encode(),
                            {'content-type':'text/markdown; charset=utf-8'})
        for module, operation in ((fulltext, fulltext.fetch_one), (grab, grab.grab)):
            with self.subTest(module=module), patch.object(module, 'request', return_value=response) as request:
                result = operation('https://example.org/docs')
                body = result[0] if isinstance(result, tuple) else result['body']
                self.assertEqual(README, body)
                self.assertTrue(request.call_args.args[1]['Accept'].startswith('text/markdown,'))

    def test_html_content_type_cannot_be_mistaken_for_negotiated_markdown(self):
        self.assertIsNone(markdown_source.from_response(page(PAGE)))

    def test_failed_github_request_falls_back_to_web_extraction(self):
        with patch.object(fulltext, 'request', Mock(side_effect=[FeedError('403'), page(PAGE)])) as request:
            body, reason = fulltext.fetch_one(REPOSITORY)
            self.assertIn('runtime schedules tools', body)
            self.assertFalse(reason)
            self.assertEqual(2, request.call_count)
            self.assertEqual(request.call_args_list[0].kwargs['deadline'],
                             request.call_args_list[1].kwargs['deadline'])

    def test_new_feed_entry_is_enriched_before_insertion_and_translation(self):
        entry = {'url':REPOSITORY, 'title':'owner/project', 'body':'RSS summary', 'content_kind':'summary'}
        validators = {'etag':'tag', 'modified':'', 'resolved_url':self.plain['url']}
        with patch.object(sync, 'fetch_source', return_value=([entry], validators)), \
                patch.object(markdown_source, 'fetch', return_value=README) as fetch:
            entries, returned = sync._fetch_with_budget(self.plain, {}, SimpleNamespace(ranged=False),
                                                        time.monotonic() + 60)
        self.assertEqual(README, entries[0]['body'])
        self.assertEqual('content', entries[0]['content_kind'])
        self.assertIs(returned, validators)
        fetch.assert_called_once()
        reg.put_items(self.plain, entries)
        with reg.db() as connection:
            self.assertEqual(README, connection.execute('SELECT body FROM feed_items WHERE url=?',
                                                        (REPOSITORY,)).fetchone()[0])

    def test_existing_articles_and_posts_are_not_silently_refetched(self):
        entry = {'url':REPOSITORY, 'title':'old', 'body':'Existing article', 'content_kind':'summary'}
        reg.put_items(self.plain, [entry])
        post = {**entry, 'url':'https://github.com/owner/other', 'content_kind':'post'}
        with patch.object(markdown_source, 'fetch') as fetch:
            markdown_source.enrich(self.plain, [entry, post], deadline=time.monotonic() + 30,
                                   collect_deadline=time.monotonic() + 60)
        fetch.assert_not_called()
        self.assertEqual('Existing article', entry['body'])

    def test_optional_enrichment_does_not_extend_source_budget(self):
        entry = {'url':REPOSITORY, 'title':'title', 'body':'Original summary'}
        with patch.object(markdown_source, 'fetch') as fetch:
            markdown_source.enrich(self.plain, [entry], deadline=time.monotonic() - 1,
                                   collect_deadline=time.monotonic() + 60)
        fetch.assert_not_called()
        self.assertEqual('Original summary', entry['body'])

    def test_fulltext_sources_acquire_markdown_once_in_the_existing_fulltext_phase(self):
        entry = {'url':REPOSITORY, 'title':'Demo reader', 'body':'RSS summary', 'content_kind':'summary'}
        with patch.object(markdown_source, 'fetch') as fetch:
            markdown_source.enrich(self.source, [entry], deadline=time.monotonic() + 30,
                                   collect_deadline=time.monotonic() + 60)
        fetch.assert_not_called()
        reg.put_items(self.source, [entry])
        with patch.object(fulltext, 'request', return_value=github_response()) as request:
            result = fulltext.fetch_pending('rss')
        self.assertEqual(1, result['done'])
        self.assertEqual(1, request.call_count)
        with reg.db() as connection:
            row = connection.execute('SELECT * FROM feed_items WHERE url=?', (REPOSITORY,)).fetchone()
        self.assertEqual(README, row['body'])
        self.assertTrue(row['fulltext_at'])

    def test_collection_budget_exhaustion_is_not_swallowed_as_an_optional_failure(self):
        with self.assertRaises(CollectionBudgetExceeded):
            markdown_source.fetch(REPOSITORY, transport=Mock(side_effect=CollectionBudgetExceeded))

    def test_translation_with_a_missing_code_marker_is_not_persisted(self):
        reg.put_items(self.plain, [{'url':REPOSITORY, 'title':'Demo reader', 'body':README,
                                   'content_kind':'content'}])
        with patch.object(translate, '_call', return_value='###TITLE###\n阅读器\n###BODY###\n遗漏代码的译文。'):
            result = translate.translate_pending('rss')
        self.assertEqual(1, result['failed'])
        self.assertEqual(0, result['done'])
        with reg.db() as connection:
            row = connection.execute('SELECT * FROM feed_items WHERE url=?', (REPOSITORY,)).fetchone()
        self.assertFalse(row['body_zh'])
        self.assertFalse(row['translated_at'])
        self.assertEqual(README, row['body'])
