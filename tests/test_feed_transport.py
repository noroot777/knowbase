"""Exercise the HTTP boundary without contacting public or private servers."""
import socket
import ssl
import unittest
from unittest.mock import patch

import httpx

from kb.feeds import fetching


def addresses(*ips):
    return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, 1, 6, '', (ip, 443)) for ip in ips]


class FeedTransportTests(unittest.TestCase):
    def transport(self, handler):
        client = httpx.Client
        options = []
        def factory(**kwargs):
            options.append(kwargs)
            return client(**kwargs, transport=httpx.MockTransport(handler))
        mock = patch.object(fetching.httpx, 'Client', side_effect=factory)
        mock.start(); self.addCleanup(mock.stop)
        return options

    def test_mixed_dns_pins_public_ip_with_original_host_and_tls_sni(self):
        seen = []
        options = self.transport(lambda r: (seen.append(r), httpx.Response(200, content=b'feed'))[1])
        with patch.object(fetching.socket, 'getaddrinfo', return_value=addresses('2001::6ca0:a689', '108.160.170.44')) as dns:
            response = fetching.request('https://huggingface.co/blog/feed.xml')
        self.assertEqual(1, dns.call_count)
        self.assertEqual('108.160.170.44', seen[0].url.host)
        self.assertEqual('huggingface.co', seen[0].headers['host'])
        self.assertEqual('huggingface.co', seen[0].extensions['sni_hostname'])
        self.assertTrue(options[0]['trust_env'])
        self.assertTrue(options[0]['verify'].check_hostname)
        self.assertEqual('https://huggingface.co/blog/feed.xml', response.url)

    def test_proxy_tls_uses_origin_sni_and_hostname_verification(self):
        with patch.object(ssl.SSLContext, 'wrap_socket') as wrap:
            context = fetching.tls_context('huggingface.co')
            context.wrap_socket('proxy tunnel socket', server_hostname='108.160.170.44')
        wrap.assert_called_once_with('proxy tunnel socket', server_hostname='huggingface.co')
        self.assertTrue(context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)

    def test_all_nonpublic_or_unresolved_dns_reports_distinct_errors(self):
        seen = []
        self.transport(lambda r: seen.append(r))
        for ips in [('127.0.0.1','::1'), ('10.0.0.1','2001::6ca0:a689'), ()]:
            with self.subTest(ips=ips), patch.object(fetching.socket, 'getaddrinfo', return_value=addresses(*ips)):
                with self.assertRaisesRegex(fetching.FeedError, '未解析到公网地址'):
                    fetching.request('https://example.org/rss')
        with patch.object(fetching.socket, 'getaddrinfo', side_effect=socket.gaierror):
            with self.assertRaisesRegex(fetching.FeedError, '无法解析'):
                fetching.request('https://example.org/rss')
        self.assertEqual([], seen)

    def test_redirect_revalidates_dns_and_drops_cross_origin_validators(self):
        seen = []
        def handler(r):
            seen.append(r)
            return (httpx.Response(302, headers={'location':'https://feeds.example.net/data'})
                    if len(seen) == 1 else httpx.Response(200, content=b'feed'))
        self.transport(handler)
        with patch.object(fetching.socket, 'getaddrinfo', side_effect=[addresses('8.8.8.8'), addresses('1.1.1.1')]):
            response = fetching.request('https://example.org/rss', {'If-None-Match':'old'})
        self.assertEqual(['8.8.8.8','1.1.1.1'], [r.url.host for r in seen])
        self.assertEqual('old', seen[0].headers['if-none-match'])
        self.assertNotIn('if-none-match', seen[1].headers)
        self.assertEqual('feeds.example.net', seen[1].extensions['sni_hostname'])
        self.assertEqual('https://feeds.example.net/data', response.url)

    def test_redirect_dns_rebinding_or_literal_private_never_connects(self):
        for target in ('/rebind', 'http://127.0.0.1/internal'):
            with self.subTest(target=target):
                seen = []
                def handler(r):
                    seen.append(r)
                    return httpx.Response(302, headers={'location':target})
                client = httpx.Client(transport=httpx.MockTransport(handler))
                with patch.object(fetching.socket, 'getaddrinfo', side_effect=[addresses('8.8.8.8'), addresses('10.0.0.1')]), \
                     patch.object(fetching.httpx, 'Client', return_value=client):
                    with self.assertRaises(ValueError):
                        fetching.request('https://example.org/rss')
                self.assertEqual(1, len(seen))

    def test_connect_failure_tries_other_validated_public_address(self):
        seen = []
        def handler(r):
            seen.append(r.url.host)
            if r.url.host == '8.8.8.8':
                raise httpx.ConnectError('unreachable')
            return httpx.Response(200, content=b'ok')
        self.transport(handler)
        with patch.object(fetching.socket, 'getaddrinfo', return_value=addresses('8.8.8.8','::1','1.1.1.1')):
            self.assertEqual(b'ok', fetching.request('https://example.org/rss').body)
        self.assertEqual(['8.8.8.8','1.1.1.1'], seen)

    def test_deadline_stops_stream_even_if_each_read_succeeds(self):
        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'first'
                yield b'second'
        self.transport(lambda r: httpx.Response(200, stream=SlowStream()))
        with patch.object(fetching.socket, 'getaddrinfo', return_value=addresses('8.8.8.8')), \
             patch.object(fetching.time, 'monotonic', side_effect=[0,0,0,1,21]):
            with self.assertRaisesRegex(fetching.FeedError, '超时'):
                fetching.request('https://example.org/rss', deadline=20)

    def test_redirects_still_bounded(self):
        seen = []
        self.transport(lambda r: (seen.append(r), httpx.Response(302, headers={'location':'/again'}))[1])
        with patch.object(fetching.socket, 'getaddrinfo', return_value=addresses('8.8.8.8')):
            with self.assertRaisesRegex(fetching.FeedError, '重定向次数过多'):
                fetching.request('https://example.org/rss')
        self.assertEqual(6, len(seen))

    def test_response_size_still_bounded(self):
        self.transport(lambda r: httpx.Response(200, content=b'12345'))
        with patch.object(fetching.socket, 'getaddrinfo', return_value=addresses('8.8.8.8')), \
             patch.object(fetching, 'MAX_BYTES', 4):
            with self.assertRaisesRegex(fetching.FeedError, '超过 8 MB'):
                fetching.request('https://example.org/rss')


if __name__ == '__main__':
    unittest.main()
