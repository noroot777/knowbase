# -*- coding: utf-8 -*-
"""出口配置(`kb/feeds/egress.py`):设置和环境变量谁说话算数、改完要不要重启。

方向是仓库主人定的(demo 里的 B / C / F):**页面是正经入口,环境变量退成兜底**;
**保存即生效**;**代理也做成可配置**。所以这一份钉的全是这三件事的后果 ——
不是「函数返回了什么」,而是「抓取那一跳最终用了哪个端点、哪个代理」。

**注意这和 `kb.llm` 的 key / base_url 方向相反**(那边环境变量压过文件)。两处不同是
知情的,理由写在 `kb/feeds/egress.py` 的模块 docstring 里。所以这里**两个方向各钉一条**:
设置填了就压过环境变量,设置清空了环境变量重新接管 —— 少了后一条,把读取写成
「只读设置」也全绿,而那会让容器和 CI 那条路直接失效。

**配置文件按 `tests/test_llm_stages.py` 的办法换到临时目录**:它和 `secrets.json` 是同一
份文件,不隔离就会读到开发机上真配的东西(本机实测 `proxy` 一项当场来自环境变量),
而写的那几条会把真文件改掉。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from kb import llm
from kb.feeds import egress, fetching, resolve

DOH = 'https://doh.example/dns-query'


class EgressBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        orig = llm._SECRETS_PATH
        llm._SECRETS_PATH = Path(self.tmp.name) / 'secrets.json'
        self.addCleanup(lambda: setattr(llm, '_SECRETS_PATH', orig))
        resolve.forget()
        self.addCleanup(resolve.forget)
        # 开发机上这几个本来就 export 着,得先清干净,不然「设置为空」那一格测不准
        env = patch.dict(os.environ, {name: '' for name in
                                      (egress.ENV_ENDPOINT, egress.ENV_SECONDS,
                                       *egress.ENV_PROXY)})
        env.start()
        self.addCleanup(env.stop)

    def stored(self, **fields):
        llm._SECRETS_PATH.write_text(json.dumps({egress.SECTION: fields}), encoding='utf-8')


class PrecedenceTests(EgressBase):
    """**设置优先,环境变量兜底** —— 两个方向各一条。"""

    def test_settings_win_over_the_environment(self):
        self.stored(endpoint='https://from-settings.example/q', seconds='3',
                    proxy='http://settings:1')
        with patch.dict(os.environ, {egress.ENV_ENDPOINT: 'https://from-env.example/q',
                                     egress.ENV_SECONDS: '42',
                                     'HTTPS_PROXY': 'http://env:1'}):
            self.assertEqual('https://from-settings.example/q', egress.endpoint())
            self.assertEqual(3.0, egress.seconds())
            self.assertEqual('http://settings:1', egress.proxy())
            self.assertEqual({'endpoint': 'settings', 'seconds': 'settings',
                              'proxy': 'settings'}, egress.view()['sources'])

    def test_the_environment_takes_over_once_settings_are_cleared(self):
        """**反过来钉一边。** 少了它,把读取写成「只读设置」也全绿 —— 而容器和 CI
        那条路上没人去点页面,环境变量是那里唯一的入口。"""
        self.stored()
        with patch.dict(os.environ, {egress.ENV_ENDPOINT: 'https://from-env.example/q',
                                     egress.ENV_SECONDS: '42',
                                     'HTTPS_PROXY': 'http://env:1'}):
            self.assertEqual('https://from-env.example/q', egress.endpoint())
            self.assertEqual(42.0, egress.seconds())
            self.assertEqual('http://env:1', egress.proxy())
            self.assertEqual({'endpoint': 'env', 'seconds': 'env', 'proxy': 'env'},
                             egress.view()['sources'])

    def test_nothing_anywhere_means_off_with_the_default_budget(self):
        self.assertEqual('', egress.endpoint())
        self.assertFalse(egress.configured())
        self.assertEqual(egress.DEFAULT_SECONDS, egress.seconds())
        self.assertIsNone(egress.proxy())

    def test_saving_an_empty_endpoint_hands_it_back_to_the_environment(self):
        """清空 ≠ 写一个空串进去把兜底也挡住。"""
        self.stored(endpoint=DOH)
        egress.save('', '', '')
        with patch.dict(os.environ, {egress.ENV_ENDPOINT: 'https://from-env.example/q'}):
            self.assertEqual('https://from-env.example/q', egress.endpoint())
        self.assertNotIn(egress.SECTION,
                         json.loads(llm._SECRETS_PATH.read_text(encoding='utf-8')))


class TakesEffectTests(EgressBase):
    """**保存即生效**(C):不用重启,而且旧的解析答案不许继续粘着。"""

    def test_a_saved_endpoint_is_used_by_the_next_fetch(self):
        asked = []

        def handler(request):
            if request.url.host.endswith('.example') and 'dns-query' in request.url.path:
                asked.append(str(request.url.host))
                return httpx.Response(200, content=json.dumps(
                    {'Status': 0, 'Answer': [{'type': 1, 'TTL': 300,
                                              'data': '93.184.216.34'}]}).encode())
            return httpx.Response(200, content=b'feed')

        client = httpx.Client
        with patch.object(fetching.httpx, 'Client',
                          side_effect=lambda **kw: client(**kw, transport=httpx.MockTransport(handler))):
            self.stored(endpoint='https://first.example/dns-query')
            fetching.request('https://example.org/rss')
            self.assertEqual(['first.example'], sorted(set(asked)))
            # 改设置:不重启、不清任何进程内状态,下一次抓取就该用新端点
            egress.save('https://second.example/dns-query', '', '')
            fetching.request('https://example.org/rss')
        self.assertIn('second.example', asked, '改完端点还在问旧的 —— 等于要重启')

    def test_saving_drops_the_cached_answers(self):
        """不清缓存的后果:旧答案最多还粘 10 分钟(TTL 上限),人会以为没生效。"""
        self.stored(endpoint=DOH)
        resolve._cache[(DOH, 'example.org')] = (float('inf'), ('93.184.216.34',))
        egress.save(DOH, '6', '')
        self.assertEqual({}, resolve._cache, '保存之后旧答案还在缓存里')


class ProxyTests(EgressBase):
    """**代理也做成可配置**(F)。"""

    def test_a_configured_proxy_is_handed_to_httpx_explicitly(self):
        self.stored(proxy='http://127.0.0.1:7897')
        self.assertEqual({'proxy': 'http://127.0.0.1:7897'}, egress.client_kwargs())

    def test_without_one_the_original_trust_env_path_is_kept(self):
        """**反过来钉一边**:没配就逐字维持原样 —— 那条路上 macOS 读系统代理设置、
        Linux 读环境变量,都不该因为这一块的存在而改变。"""
        self.stored()
        self.assertEqual({'trust_env': True}, egress.client_kwargs())
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://env:1'}):
            # 环境里的代理**不**被提成显式 proxy:httpx 自己会读,提成显式反而会
            # 把 NO_PROXY 那套绕过去。
            self.assertEqual({'trust_env': True}, egress.client_kwargs())

    def test_both_hops_go_through_the_same_proxy_setting(self):
        """抓取那一跳和 DoH 那一跳用的是同一份口径 —— 两处各判一遍就会有一处漏掉。"""
        self.stored(proxy='http://127.0.0.1:7897', endpoint=DOH)
        seen = []
        real = httpx.Client  # patch 之前先拿住真的,否则 factory 里那一下是递归

        def factory(**kwargs):
            seen.append(kwargs.get('proxy') or ('trust_env' if kwargs.get('trust_env') else None))
            # 记下之后要摘掉:httpx 里 proxy 挂的 mounts 压过 transport,留着的话
            # MockTransport 形同虚设、这条用例会真连本机代理出去。
            kwargs.pop('proxy', None)
            return real(**kwargs, transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=json.dumps(
                    {'Status': 0, 'Answer': [{'type': 1, 'TTL': 300,
                                              'data': '93.184.216.34'}]}).encode())
                if 'dns-query' in r.url.path else httpx.Response(200, content=b'feed')))

        with patch.object(fetching.httpx, 'Client', side_effect=factory):
            fetching.request('https://example.org/rss')
        self.assertTrue(seen, '一个客户端都没建')
        self.assertEqual({'http://127.0.0.1:7897'}, set(seen),
                         f'有一跳没用配置里的代理:{seen}')


class CredentialTests(EgressBase):
    """代理地址里的密码位。"""

    def test_the_password_is_masked_on_the_way_out(self):
        self.stored(proxy='http://me:s3cret@127.0.0.1:7897')
        shown = egress.view()['proxy']
        self.assertNotIn('s3cret', shown)
        self.assertIn('127.0.0.1:7897', shown, '主机端口也被藏了,人没法改端口')
        self.assertIn('me', shown, '用户名也被藏了')

    def test_sending_the_mask_back_keeps_the_stored_password(self):
        """不这么做的话,改一次端口就得把密码重输一遍。"""
        self.stored(proxy='http://me:s3cret@127.0.0.1:7897')
        egress.save('', '', f'http://me:{egress.MASK}@127.0.0.1:9999')
        self.assertEqual('http://me:s3cret@127.0.0.1:9999', egress.proxy())

    def test_a_real_new_password_replaces_it(self):
        """反过来钉一边:真改密码时不许把新的吃掉。"""
        self.stored(proxy='http://me:s3cret@127.0.0.1:7897')
        egress.save('', '', 'http://me:brand-new@127.0.0.1:7897')
        self.assertEqual('http://me:brand-new@127.0.0.1:7897', egress.proxy())


class ValidationTests(EgressBase):
    """**判不准一律拒**,别把坏值写进去等抓取时才炸。"""

    def test_bad_values_are_refused_before_they_reach_the_disk(self):
        for endpoint_url, seconds, proxy_url, why in (
                ('ftp://doh.example/q', '', '', '协议不对'),
                ('https:///q', '', '', '没有主机名'),
                ('', 'abc', '', '秒数不是数字'),
                ('', '0', '', '秒数不是正数'),
                ('', '600', '', '秒数超出范围'),
                ('', '', 'tcp://127.0.0.1:1', '代理协议不对')):
            with self.subTest(why=why):
                with self.assertRaises(egress.BadValue):
                    egress.save(endpoint_url, seconds, proxy_url)
                self.assertFalse(llm._SECRETS_PATH.exists(), f'{why}:坏值写进盘了')

    def test_socks_proxies_are_allowed(self):
        """反过来钉一边:别把 socks5 一起拒了。"""
        egress.save('', '', 'socks5://127.0.0.1:7897')
        self.assertEqual('socks5://127.0.0.1:7897', egress.proxy())

    def test_a_bad_stored_budget_is_reported_as_a_config_problem(self):
        """存量里已经有坏值时(手改过文件),面板上要说「配置写错了」而不是网络问题。"""
        self.stored(endpoint=DOH, seconds='abc')
        with patch.object(fetching.socket, 'getaddrinfo',
                          return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]):
            with self.assertRaisesRegex(fetching.FeedError, '出口配置'):
                fetching.public_addresses('https://example.org/rss')


if __name__ == '__main__':
    unittest.main()


class SummaryTests(EgressBase):
    """起服务/看状态时打的那两三行(`summary()`)。

    **这几条原先在 shell 里**(`scripts/web_env.sh` 自己判 `[ -n "$KB_FEED_DOH" ]`)。
    搬过来的理由是主次反过来了:端点以设置页为主,shell 再判一遍就会在「设置里填了、
    环境变量没填」时打「关」,而服务其实开着。那边现在只剩一条管道测试。
    """

    def summary(self, proxies: dict | None = None) -> str:
        """把 `urllib.request.getproxies()` 这件外部事实替掉 —— 开发机上它非空,
        「探不到代理」那一格在真机上永远测不到。"""
        with patch('urllib.request.getproxies', return_value=proxies or {}):
            return '\n'.join(egress.summary())

    def test_off_points_at_the_page_not_at_an_environment_variable(self):
        """关着时得说清去哪儿开。**指页面** —— 这是 B 那个方向的全部意思;
        指环境变量的话,人照着做一遍还得重启服务才生效。"""
        out = self.summary()
        self.assertIn('关', out)
        self.assertIn('设置', out)

    def test_a_stored_endpoint_is_reported_as_on(self):
        """**反过来钉一边,也是搬家的理由**:设置里填了、环境变量空着 —— 原先那一版
        在这一格打「关」,而抓取是真走 DoH 的。"""
        self.stored(endpoint=DOH)
        out = self.summary()
        self.assertIn('开', out)
        self.assertIn('来自设置页', out)

    def test_the_endpoint_path_and_query_never_reach_the_log(self):
        """NextDNS / AdGuard 那类端点把 profile id 写在 path 或 query 里,那是凭证。"""
        self.stored(endpoint='https://dns.nextdns.io/abc123secret?x=1')
        out = self.summary()
        self.assertIn('https://dns.nextdns.io', out)
        self.assertNotIn('abc123secret', out, '端点里的 token 进了日志')
        self.assertNotIn('x=1', out)

    def test_an_endpoint_from_the_environment_names_the_variable(self):
        """代理那一族有四个名字,只说「来自环境变量」的话人得四个都 grep 一遍。"""
        with patch.dict(os.environ, {egress.ENV_ENDPOINT: DOH}):
            self.assertIn(f'来自环境变量 {egress.ENV_ENDPOINT}', self.summary())

    def test_a_configured_proxy_shows_up_masked(self):
        self.stored(endpoint=DOH, proxy='http://me:s3cret@127.0.0.1:7897')
        out = self.summary()
        self.assertIn('127.0.0.1:7897', out)
        self.assertNotIn('s3cret', out, '代理密码进了日志')

    def test_no_egress_anywhere_says_so(self):
        self.stored(endpoint=DOH)
        self.assertIn('探不到', self.summary())

    def test_a_system_proxy_is_enough_to_stay_quiet(self):
        """**反过来钉一边**:macOS 上那次误报就是这一格 —— `getproxies()` 读系统代理
        设置,一个 `*_proxy` 都没 export 也照样有出口,那时不该提。"""
        self.stored(endpoint=DOH)
        self.assertNotIn('探不到', self.summary({'https': 'http://127.0.0.1:7897'}))
