# -*- coding: utf-8 -*-
"""可信解析器(`kb/feeds/resolve.py`)接进传输层之后的判据。

**判据落在后果上**:`request` 到底连了哪个地址、面板上是哪一句话、发了几次 DoH 请求 ——
不验「DoH 被调用了」。污染注入用的是真实形态(2026-10-22 实测:`mistral.ai` 被解析成
Twitter / Dropbox / Meta 段的**公网**地址,`buy-european.net` 被解析成 127.0.0.1),所以
「绕过了本机那份假答案」和「假答案指向内网时照旧拒连」是分开的两条红。

**反过来钉的那一边在 `OffByDefaultTests`**:没配 `KB_FEED_DOH` 时一次 DoH 请求都不许发、
解析逐字走 `socket.getaddrinfo` —— 这一层是可选的,关着的时候不许改动任何既有行为
(既有的那些不变量钉在 `tests/test_feed_transport.py`,那一份一个字都没改)。

**两边都挂在同一个 `httpx.Client` 桩上**,按主机名分流:DoH 这一跳怎么发、订阅那一跳
连了谁,它们之间的接缝正是这一层要钉的东西。桩**只在 setUp 里装一次** —— 装第二遍时
`httpx.Client` 已经是上一层的 mock,`client(**kwargs)` 会返回一个 MagicMock,整组用例
就在「解析服务不可用」上假红一片(写这一份时实测到过)。所以 subTest 换料走 `arm()`。
"""
import json
import os
import socket
import time
import unittest
from unittest.mock import patch

import httpx

from kb.feeds import fetching, resolve

DOH = 'https://doh.example/dns-query'
#: 真实污染形态:本机把域名指到别人家的公网地址上,连过去卡在 TLS 握手。
POISONED = [(socket.AF_INET, 1, 6, '', ('108.160.173.207', 443))]


def answer(*records, status=0):
    return {'Status': status, 'Answer': [dict(r) for r in records]}


def rec(data, rtype=1, ttl=300):
    return {'name': 'example.org', 'type': rtype, 'TTL': ttl, 'data': data}


class ResolveBase(unittest.TestCase):
    def setUp(self):
        resolve.forget()
        self.addCleanup(resolve.forget)
        self.asked: list = []
        self.connected: list = []
        self.options: list = []
        self.dns: list = [answer(rec('1.1.1.1'))]
        self.feed = httpx.Response(200, content=b'feed')
        client = httpx.Client

        def handler(request):
            if request.url.host == 'doh.example':
                self.asked.append(request)
                payload = self.dns.pop(0) if len(self.dns) > 1 else self.dns[0]
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, httpx.Response):
                    return payload
                return httpx.Response(200, content=json.dumps(payload).encode(),
                                      headers={'content-type': 'application/dns-json'})
            self.connected.append(request)
            return self.feed(request) if callable(self.feed) else self.feed

        def factory(**kwargs):
            self.options.append(kwargs)
            return client(**kwargs, transport=httpx.MockTransport(handler))

        mock = patch.object(fetching.httpx, 'Client', side_effect=factory)
        mock.start()
        self.addCleanup(mock.stop)
        env = patch.dict(os.environ, {resolve.ENV: DOH})
        env.start()
        self.addCleanup(env.stop)

    def arm(self, dns, feed=None):
        """换一份 DoH 答案(subTest 用),顺带把缓存和两本流水账清空。"""
        resolve.forget()
        del self.asked[:], self.connected[:], self.options[:]
        self.dns = list(dns) if isinstance(dns, list) else [dns]
        if feed is not None:
            self.feed = feed

    def types(self):
        return [int(httpx.QueryParams(r.url.query)['type']) for r in self.asked]

    def names(self):
        return [httpx.QueryParams(r.url.query)['name'] for r in self.asked]

    def hosts(self):
        return [r.url.host for r in self.connected]


class OffByDefaultTests(ResolveBase):
    def test_unset_endpoint_never_asks_doh_and_keeps_system_resolution(self):
        with patch.dict(os.environ, {resolve.ENV: ''}), \
             patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED) as dns:
            response = fetching.request('https://example.org/rss')
        self.assertEqual(b'feed', response.body)
        self.assertEqual([], self.asked)                      # 一次 DoH 都没发
        self.assertEqual(1, dns.call_count)                   # 照旧走系统解析
        self.assertEqual(['108.160.173.207'], self.hosts())

    def test_blank_endpoint_counts_as_unset(self):
        with patch.dict(os.environ, {resolve.ENV: '   '}), \
             patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            fetching.request('https://example.org/rss')
        self.assertEqual([], self.asked)


class PoisonedResolutionTests(ResolveBase):
    def test_configured_endpoint_replaces_the_local_answer_entirely(self):
        self.arm(answer(rec('162.159.142.207'), rec('2606:4700::1111', rtype=28)))
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED) as dns:
            response = fetching.request('https://example.org/rss')
        self.assertEqual(b'feed', response.body)
        self.assertEqual(0, dns.call_count)                   # 本机那份假答案一次都没问
        self.assertEqual(['162.159.142.207'], self.hosts())   # 钉的是 DoH 给的那个
        self.assertEqual([1, 28], self.types())               # A 在前
        self.assertEqual('example.org', self.connected[0].headers['host'])
        self.assertEqual('example.org', self.connected[0].extensions['sni_hostname'])

    def test_doh_hop_goes_by_hostname_through_env_proxy_and_is_not_pinned(self):
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            fetching.request('https://example.org/rss')
        self.assertEqual(['doh.example'], sorted({r.url.host for r in self.asked}))
        self.assertEqual('application/dns-json', self.asked[0].headers['accept'])
        self.assertEqual(['example.org', 'example.org'], self.names())
        self.assertTrue(all(o['trust_env'] for o in self.options))

    def test_doh_answer_pointing_inside_still_refuses_to_connect(self):
        for data in ('127.0.0.1', '10.0.0.1', '::1'):
            with self.subTest(data=data):
                self.arm(answer(rec(data, rtype=28 if ':' in data else 1)))
                with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
                    with self.assertRaisesRegex(fetching.FeedError, '未解析到公网地址'):
                        fetching.request('https://example.org/rss')
                self.assertEqual([], self.connected)


class MalformedAnswerTests(ResolveBase):
    def test_cname_and_unparsable_records_are_skipped_not_treated_as_addresses(self):
        self.arm(answer({'type': 5, 'data': 'example.org.cdn.net'},
                        {'type': 1, 'data': 'not-an-address'},
                        {'type': 1, 'data': ''},
                        rec('1.1.1.1')))
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            fetching.request('https://example.org/rss')
        self.assertEqual(['1.1.1.1'], self.hosts())

    def test_only_a_and_aaaa_count_even_when_another_record_holds_an_address(self):
        # **这一道要独立于「data 解析不动」成立。** 反向验证时去掉记录类型过滤一条红都
        # 没有:CNAME 的 `example.org.cdn.net` 被下一道 `ip_address` 的 ValueError 兜住了,
        # 于是「只认 A/AAAA」在那份料上压根不可见。DoH 答案是外部输入 —— 答案栏目里
        # 任何非 A/AAAA 的记录都不许当成地址,哪怕它的 data 恰好是个合法 IP 串。
        self.arm(answer({'type': 65, 'TTL': 300, 'data': '104.16.0.1'},
                        {'type': 2, 'TTL': 300, 'data': '198.51.100.7'},
                        rec('1.1.1.1')))
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            fetching.request('https://example.org/rss')
        self.assertEqual(['1.1.1.1'], self.hosts())

    def test_servfail_and_transport_failure_blame_our_own_egress(self):
        for payload in ({'Status': 2}, {'Status': 5},
                        httpx.Response(502, content=b'bad gateway'),
                        httpx.ConnectError('proxy refused'),
                        httpx.ConnectTimeout('no route'),
                        httpx.Response(200, content=b'<html>not json</html>'),
                        {'Status': 0, 'Answer': 'nonsense'},
                        ['not', 'a', 'dict']):
            with self.subTest(payload=payload):
                self.arm(payload)
                with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
                    with self.assertRaisesRegex(fetching.FeedError, 'KB_FEED_DOH'):
                        fetching.request('https://example.org/rss')
                self.assertEqual([], self.connected)

    def test_nxdomain_and_address_less_name_say_unresolved_not_egress_broken(self):
        for payload in (answer(status=3), answer(), answer({'type': 5, 'data': 'c.net'})):
            with self.subTest(payload=payload):
                self.arm(payload)
                with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
                    with self.assertRaisesRegex(fetching.FeedError, '无法解析订阅服务器地址'):
                        fetching.request('https://example.org/rss')
                self.assertEqual([], self.connected)


class BudgetTests(ResolveBase):
    def test_doh_timeout_is_capped_by_what_is_left_of_the_request_budget(self):
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED), \
             patch.object(resolve, 'BUDGET', 99.0):
            fetching.request('https://example.org/rss', deadline=time.monotonic() + 2)
        spent = [o['timeout'] for o in self.options if isinstance(o.get('timeout'), float)]
        self.assertEqual(2, len(spent))                       # 两次查询各一个 client
        self.assertTrue(all(0 < t <= 2 for t in spent), spent)

    def test_exhausted_budget_asks_nothing_and_reports_the_read_timeout(self):
        self.arm(httpx.ConnectTimeout('doh is slow'))
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED), \
             patch.object(resolve, 'BUDGET', 99.0):
            with self.assertRaisesRegex(fetching.FeedError, '超时'):
                fetching.request('https://example.org/rss', deadline=time.monotonic() - 1)
        self.assertEqual([], self.asked)
        self.assertEqual([], self.connected)

    def test_no_budget_left_is_our_own_failure_not_a_missing_domain(self):
        # 第一次查询把预算花光,第二次就没时间问了 —— 末态必须是「我们这一侧没出口」。
        # 报成「这个域名没有地址」就是在替对方断言一件我们没问到的事。
        # 这一条不经 `request`:那条路上 `remaining()` 会先抛,于是 resolve 自己这个
        # 分支一条红都没有(反向验证实测:把它改成「照样去问」整组照旧全绿)。
        self.arm(answer())
        with self.assertRaises(resolve.Unavailable):
            resolve.addresses('example.org', seconds=0)
        self.assertEqual([], self.asked)

    def test_collection_cutoff_still_outranks_the_resolver(self):
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            with self.assertRaises(fetching.CollectionBudgetExceeded):
                fetching.request('https://example.org/rss',
                                 deadline=time.monotonic() + 30,
                                 collect_deadline=time.monotonic() - 1)
        self.assertEqual([], self.asked)


class CacheTests(ResolveBase):
    def test_second_resolve_of_the_same_host_reuses_the_answer(self):
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            fetching.request('https://example.org/rss')
            fetching.request('https://example.org/other')
        self.assertEqual([1, 28], self.types())               # 第二次一个请求都没发
        self.assertEqual(2, len(self.connected))

    def test_expired_entry_is_asked_again(self):
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            fetching.request('https://example.org/rss')
            resolve._cache[(DOH, 'example.org')] = (time.monotonic() - 1, ('1.1.1.1',))
            fetching.request('https://example.org/rss')
        self.assertEqual([1, 28, 1, 28], self.types())

    def test_answer_ttl_decides_the_entry_and_is_clamped(self):
        for ttl, expected in ((1, resolve.TTL_MIN), (10 ** 6, resolve.TTL_MAX), (120, 120)):
            with self.subTest(ttl=ttl):
                self.arm(answer(rec('1.1.1.1', ttl=ttl)))
                with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
                    fetching.request('https://example.org/rss')
                until = resolve._cache[(DOH, 'example.org')][0]
                self.assertAlmostEqual(expected, until - time.monotonic(), delta=1.0)

    def test_failures_are_never_cached(self):
        # 两族都得没答上这一轮才算失败 —— 只喂一条坏答案的话 AAAA 那次会成功,
        # 而「一族查不动、另一族答上了就按答上的算」是有意的(下面那条钉着它)。
        self.arm([{'Status': 2}, {'Status': 2},
                  answer(rec('1.1.1.1')), answer(rec('1.1.1.1'))])
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            with self.assertRaisesRegex(fetching.FeedError, 'KB_FEED_DOH'):
                fetching.request('https://example.org/rss')
            fetching.request('https://example.org/rss')
        self.assertEqual(['1.1.1.1'], self.hosts())

    def test_one_family_failing_does_not_discard_what_the_other_answered(self):
        self.arm([httpx.ConnectError('v4 lookup died'), answer(rec('2606:4700::1111', rtype=28))])
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            fetching.request('https://example.org/rss')
        self.assertEqual(['2606:4700::1111'], self.hosts())

    def test_cache_is_keyed_by_host_so_each_redirect_hop_resolves_again(self):
        hops = []

        def feed(request):
            hops.append(request)
            return (httpx.Response(302, headers={'location': 'https://other.example/data'})
                    if len(hops) == 1 else httpx.Response(200, content=b'feed'))

        self.arm([answer(rec('1.1.1.1')), answer(rec('1.1.1.1')),
                  answer(rec('8.8.8.8')), answer(rec('8.8.8.8'))], feed=feed)
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            response = fetching.request('https://example.org/rss')
        self.assertEqual('https://other.example/data', response.url)
        self.assertEqual(['1.1.1.1', '8.8.8.8'], self.hosts())
        self.assertEqual(['example.org', 'example.org', 'other.example', 'other.example'],
                         self.names())


class BothCallersTests(ResolveBase):
    def test_public_url_also_goes_through_the_trusted_resolver(self):
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED) as dns:
            self.assertEqual('https://example.org/rss',
                             fetching.public_url('https://example.org/rss'))
        self.assertEqual(0, dns.call_count)
        self.assertEqual([1, 28], self.types())

    def test_public_url_reports_the_egress_failure_too(self):
        self.arm({'Status': 2})
        with patch.object(fetching.socket, 'getaddrinfo', return_value=POISONED):
            with self.assertRaisesRegex(fetching.FeedError, 'KB_FEED_DOH'):
                fetching.public_url('https://example.org/rss')


if __name__ == '__main__':
    unittest.main()
