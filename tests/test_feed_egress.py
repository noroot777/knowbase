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
import re
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

    def test_the_mask_is_never_what_gets_stored(self):
        """**存量里那条解析不动时整段 userinfo 一起被盖**(`***@h`,见 `mask_proxy`),
        于是人交回来的那一份里 `password` 是 `None` —— 只认「密码位是 MASK」的那一版
        在这一格判成「这不是占位」,`http://***@h:7897` 一路过 `check_url` 存进
        `secrets.json`,**密码真的变成了 `***` 三个字**,而面板上看着像刚把端口改好了,
        要等下一次抓取连不上才知道。拼不出来就拒,别猜。
        """
        self.stored(proxy='http://me:s3cret@h:abc')      # 端口是垃圾 → 解析不动
        shown = egress.mask_proxy(egress.proxy() or '')
        self.assertNotIn('s3cret', shown, '面板上就泄漏了')
        with self.assertRaises(egress.BadValue):
            egress.save('', '', shown.replace(':abc', ':7897'))
        self.assertEqual('http://me:s3cret@h:abc', egress.proxy(), '存着的密码被动过了')

    def test_a_real_new_password_replaces_it(self):
        """反过来钉一边:真改密码时不许把新的吃掉。"""
        self.stored(proxy='http://me:s3cret@127.0.0.1:7897')
        egress.save('', '', 'http://me:brand-new@127.0.0.1:7897')
        self.assertEqual('http://me:brand-new@127.0.0.1:7897', egress.proxy())


class SchemelessProxyTests(EgressBase):
    """不带协议头的代理地址(`me:s3cret@h:7897`)。

    **「`urlsplit` 没报错」不等于「这份结果能信」**:它把 `me` 当成 scheme、netloc 空、
    整条 `s3cret@127.0.0.1:7897` 落进 path,于是 `parts.password` 是 `None` ——
    「这里没有密码」这个结论是假的。实测后果:`view()` 和 `./kb.sh status` 两个出口
    都把密码原样打出来,贴一张截图或一段日志就泄漏。这和端点那一轮是**同一类凭证
    问题**,只是入口从端点换成了代理,所以两个出口都要钉。
    """

    def env_proxy(self, value: str):
        patcher = patch.dict(os.environ, {'HTTPS_PROXY': value})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_neither_exit_prints_the_password(self):
        self.env_proxy('me:s3cret@127.0.0.1:7897')
        self.assertNotIn('s3cret', json.dumps(egress.view(), ensure_ascii=False))
        self.assertNotIn('s3cret', '\n'.join(egress.summary()))

    def test_the_host_is_still_legible(self):
        """**反过来钉一边**:盖掉的是凭证那一段,主机端口照旧看得见 —— 否则这一行
        就回答不了「服务在走哪个出口」。"""
        self.env_proxy('me:s3cret@127.0.0.1:7897')
        self.assertIn('127.0.0.1:7897', egress.view()['proxy'])

    def test_changing_only_the_endpoint_still_saves(self):
        """脱敏值拆不开的那几种(无协议头、IPv6)原样交回来 = 这一位没改。
        不先认出这一点的话,人只想改端点,却被一句他根本没动过的代理挡住。"""
        self.env_proxy('me:s3cret@127.0.0.1:7897')
        egress.save(DOH, '', egress.view()['proxy'])
        self.assertEqual(DOH, egress.endpoint())
        self.assertEqual('me:s3cret@127.0.0.1:7897', egress.proxy())
        self.assertEqual('env', egress.source('proxy'), '环境变量那份被复制进设置了')


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

    def test_the_proxy_line_does_not_depend_on_the_endpoint_being_on(self):
        """**代理那一行和端点开没开无关。** 原先它整段写在「端点开着」那个分支里,
        于是「端点关、代理在环境变量里」这种最常见的形态(用户那台机器此刻就是)
        一行代理都不打 —— 而这几行存在的理由正是回答「跑着的那个进程在用什么出口」。
        """
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:7897'}):
            out = self.summary().splitlines()
        self.assertIn('关', out[0])
        self.assertEqual(2, len(out), out)
        self.assertIn('出口代理:http://127.0.0.1:7897', out[1])
        self.assertIn('HTTPS_PROXY', out[1])

    def test_endpoint_off_and_no_proxy_stays_quiet_about_doh(self):
        """**反过来钉一边**:端点关着时「没配代理」是默认状态,不是问题 ——
        那句「DoH 端点在墙外时…」在这儿是无中生有(口径同 code_stale:提示而不代劳)。
        """
        out = self.summary().splitlines()
        self.assertEqual(1, len(out), out)
        self.assertIn('关', out[0])

    def test_no_egress_anywhere_says_so(self):
        self.stored(endpoint=DOH)
        self.assertIn('探不到', self.summary())

    def test_a_system_proxy_is_enough_to_stay_quiet(self):
        """**反过来钉一边**:macOS 上那次误报就是这一格 —— `getproxies()` 读系统代理
        设置,一个 `*_proxy` 都没 export 也照样有出口,那时不该提。"""
        self.stored(endpoint=DOH)
        self.assertNotIn('探不到', self.summary({'https': 'http://127.0.0.1:7897'}))

    def test_only_a_no_proxy_setting_is_not_a_proxy(self):
        """`getproxies()` 把 `NO_PROXY` 也当一项回来,照「非空就是有代理」判的话这句
        提示被这一个键吃掉 —— 而「只设了 `NO_PROXY`、根本没有代理」的机器正是最该
        看见它的那一台。"""
        self.stored(endpoint=DOH)
        self.assertIn('探不到', self.summary({'no': 'example.internal'}))

    def test_a_proxy_we_do_not_read_is_still_said_out_loud(self):
        """**探到了、但不在 `ENV_PROXY` 那份名单里**:`HTTP_PROXY`、或者 macOS 的系统
        代理设置。名单只收 https/all 那几个是知情的(`HTTP_PROXY` 管不着 https 这一跳),
        可一行都不打也不对 —— 这几行要回答的就是「跑着的那个进程在用什么出口」。"""
        self.stored(endpoint=DOH)
        out = self.summary({'http': 'http://127.0.0.1:7897'})
        self.assertIn('http://127.0.0.1:7897', out)
        self.assertNotIn('探不到', out, '探到了却说探不到')

    def test_a_probed_proxy_is_masked_too(self):
        """系统代理设置和 `*_PROXY` 里一样能带凭证,而这一行是要落盘的。"""
        self.stored(endpoint=DOH)
        out = self.summary({'https': 'http://me:s3cret@127.0.0.1:7897'})
        self.assertNotIn('s3cret', out, '探到的那个代理把密码打进了日志')
        self.assertIn('127.0.0.1:7897', out)


#: 人在面板上敲得出来、`urlsplit` 却会抛的那几种。**端口那一条最阴**:`urlsplit` 当场
#: 不抛,要等读 `.port`(一个 property)才抛,所以「解析一下看看」是拦不住它的。
JUNK = ('http://127.0.0.1:99999', 'http://[::1', 'http://h:abc', 'abc', 'ftp://h/q')


class UrlShapeTests(EgressBase):
    """坏地址:**保存那一关就拒掉**,而显示路径上**一个字都不许抛**。

    这是一类问题,不是三个例子:判据分散在 `check` / `mask_proxy` / `mask_endpoint` /
    `view` 四处各写一遍时,漏掉的那一处就是事故 —— 实测漏的是端口。`http://h:99999`
    一路存进 `secrets.json`,下一次抓取在 `httpx.Client(proxy=...)` 那一行抛
    `httpx.InvalidURL`,而**它不是 `httpx.HTTPError` 的子类**,`fetching.request` 那两道
    except 一个都接不住,整轮同步 500、面板上一个字的原因都没有。
    所以这一族现在只有一份判据(`egress.check_url`),下面三条分别钉它的三个后果。
    """

    def test_a_malformed_address_never_reaches_the_disk(self):
        for bad in JUNK:
            for field in ('endpoint', 'proxy'):
                with self.subTest(bad=bad, field=field):
                    args = {'endpoint': (bad, '', ''), 'proxy': ('', '', bad)}[field]
                    with self.assertRaises(egress.BadValue) as caught:
                        egress.save(*args)
                    self.assertEqual(field, caught.exception.field,
                                     '报错没说是哪个输入框,面板只能飘个 toast')
                    self.assertFalse(llm._SECRETS_PATH.exists(), '坏值写进盘了')

    def test_the_panel_can_still_be_opened_on_junk_that_is_already_stored(self):
        """**反过来钉一边,而且是最要紧的那一边**:`check_url` 是这一轮才加的,存量里
        (手改过文件、更早版本存进去的)就是会有这种值 —— 而人打开这个面板正是为了
        把它改掉。这儿抛一下,面板就只剩一句「读取失败」,那个值再也改不掉。"""
        for bad in JUNK:
            with self.subTest(bad=bad):
                self.stored(endpoint=bad, proxy=f'http://me:s3cret@{bad}')
                shown = egress.view()          # 不抛就是通过
                self.assertNotIn('s3cret', json.dumps(shown, ensure_ascii=False),
                                 '解析不动的地址把密码原样显示出来了')
                egress.summary()               # 起服务那两行同理不许抛

    def test_junk_already_on_disk_is_reported_as_a_config_problem_not_a_500(self):
        """末态判据:存量坏值撞上抓取时,面板上要是一句说得出下一步的话。"""
        self.stored(proxy='http://h:abc')
        # 解析那一步先打桩:这条要钉的是**建 client 那一行**,不是本机 DNS 能不能用。
        with patch.object(fetching.socket, 'getaddrinfo',
                          return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]):
            with self.assertRaisesRegex(fetching.FeedError, '出口配置写错了'):
                fetching.request('https://example.org/rss')

    def test_a_good_address_with_a_port_is_still_accepted(self):
        """**反过来钉一边**:别把端口这一条写成「有端口就拒」。"""
        egress.save('https://doh.example:8443/q', '', 'http://127.0.0.1:65535')
        self.assertEqual('https://doh.example:8443/q', egress.endpoint())
        self.assertEqual('http://127.0.0.1:65535', egress.proxy())

    def test_the_port_range_is_judged_on_both_ends(self):
        """端口的两头各钉一格。**0 是这一族最容易漏的那个**:它是合法整数,
        `urlsplit` 读 `.port` 时一声不吭,于是只靠「解析会不会抛」判的那一版把它
        放了进去 —— 存进盘、下一次抓取连接失败,而面板上看不出是这条值本身不合法。
        文案和 docstring 一直写着「1-65535」,判据得跟上。"""
        for port, ok in (('0', False), ('65536', False), ('1', True), ('65535', True)):
            with self.subTest(port=port):
                url = f'http://127.0.0.1:{port}'
                if ok:
                    egress.save('', '', url)
                    self.assertEqual(url, egress.proxy())
                else:
                    with self.assertRaises(egress.BadValue) as caught:
                        egress.save('', '', url)
                    self.assertEqual('proxy', caught.exception.field)
                    self.assertIn('1-65535', str(caught.exception))

    def test_a_bad_port_stays_visible_in_what_the_panel_shows(self):
        """存量里的端口 0 要**看得见**。`if parts.port:` 把 0 当成「没有端口」,于是
        脱敏之后那个不合法的端口凭空消失 —— 面板和状态行上显示成一条看着正常的地址,
        而人正是被叫来改它的(`check_url` 是这一轮才拒的 0,存量里有)。"""
        self.stored(endpoint='https://doh.example:0/q', proxy='http://me:s3cret@127.0.0.1:0')
        self.assertIn(':0', egress.view()['proxy'])
        self.assertIn('doh.example:0', egress.summary()[0])

    def test_a_real_ipv6_address_survives_the_round_trip(self):
        """**合法的 IPv6 和坏括号是两件事。** `parts.hostname` 给的是**去过壳**的
        `::1`,三处拼 host 的地方各写各的话就会拼出 `user:***@::1:7897` —— 那不是
        一个合法地址。实测后果不是「显示得丑」:页面把这份回传之后 `check_url` 当场
        报「方括号没闭合」,于是**这个代理底下连端点都改不了**。"""
        egress.save('https://[::1]:8443/q', '', 'http://me:s3cret@[::1]:7897')
        shown = egress.view()
        self.assertEqual('http://me:***@[::1]:7897', shown['proxy'])
        self.assertIn('[::1]:8443', egress.summary()[0])
        # 人只改端点、把脱敏那份原样交回来 —— 这一下必须存得下去。
        egress.save('https://[::1]:9443/q', '', shown['proxy'])
        self.assertEqual('http://me:s3cret@[::1]:7897', egress.proxy())
        self.assertEqual('https://[::1]:9443/q', egress.endpoint())


class FallbackWriteBackTests(EgressBase):
    """页面显示的是**生效值**,人原样交回来时不许把兜底值固化进设置。

    形态:代理来自 `HTTPS_PROXY`,人只想改端点 —— 另外两格是页面填好的,他动都没动。
    照单全收的话,`secrets.json` 里从此多出一份代理,**运维再改环境变量就不生效了**,
    而面板上显示的值和之前一模一样,看不出发生过什么。密码那一位更糟:页面回传的是
    脱敏过的 `***`,而「沿用旧值」原先只看设置里那一份(空的),于是存进去的是一条
    **密码被抹成空串**的代理 —— 本来好用的出口当场坏掉,人要到下一次抓取超时才发现。
    """

    def env(self, **names):
        patcher = patch.dict(os.environ, names)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_saving_only_the_endpoint_leaves_the_env_proxy_alone(self):
        self.env(HTTPS_PROXY='http://me:s3cret@127.0.0.1:7897')
        shown = egress.view()
        self.assertEqual('env', shown['sources']['proxy'])
        egress.save(DOH, shown['seconds'], shown['proxy'])   # 页面原样交回来
        self.assertEqual('http://me:s3cret@127.0.0.1:7897', egress.proxy())
        self.assertEqual('env', egress.source('proxy'), '环境变量那份被复制进设置了')
        stored = json.loads(llm._SECRETS_PATH.read_text(encoding='utf-8'))
        self.assertEqual({'endpoint': DOH}, stored[egress.SECTION])

    def test_a_real_edit_of_an_env_proxy_does_land_in_settings(self):
        """**反过来钉一边**:真动了那一格就得存下来,并且从此由设置说了算。"""
        self.env(HTTPS_PROXY='http://me:s3cret@127.0.0.1:7897')
        egress.save('', '', f'http://me:{egress.MASK}@127.0.0.1:9999')
        self.assertEqual('http://me:s3cret@127.0.0.1:9999', egress.proxy())
        self.assertEqual('settings', egress.source('proxy'))

    def test_the_mask_is_refused_when_there_is_no_password_left_to_reuse(self):
        """**判不准一律拒**:没有旧值可沿用时,不许把 `***` 猜成空串存下去 ——
        那是一条连不上的代理,而人要到下一次抓取超时才发现。"""
        with self.assertRaises(egress.BadValue) as caught:
            egress.save('', '', f'http://me:{egress.MASK}@127.0.0.1:7897')
        self.assertEqual('proxy', caught.exception.field)
        self.assertFalse(llm._SECRETS_PATH.exists())

    def test_an_env_proxy_is_left_to_trust_env_so_no_proxy_still_works(self):
        """`client_kwargs` 对两个来源**刻意**不一样,这一条把理由钉住:显式 `proxy=`
        的语义是「所有主机都走它」,而 `trust_env` 会一并读 `NO_PROXY`。把环境变量那份
        改成显式传,等于悄悄作废人家配的 `NO_PROXY`,内网订阅会突然开始走代理。"""
        self.env(HTTPS_PROXY='http://127.0.0.1:7897', NO_PROXY='example.internal')
        self.assertEqual({'trust_env': True}, egress.client_kwargs())
        self.stored(proxy='http://127.0.0.1:7897')
        self.assertEqual({'proxy': 'http://127.0.0.1:7897'}, egress.client_kwargs())


class PresetTests(EgressBase):
    """面板上那几颗一键填入的端点:**名单归协议那一侧**。

    判据不是「这家有没有 DoH」,是「它认不认 `resolve._read` 发的那一种请求」——
    `?name=&type=` 加 `accept: application/dns-json`。另一种 DoH 协议(RFC8484
    wireformat,`?dns=<base64>`)走同一个 `/dns-query` 路径、长得一模一样。实测:
    前端照着「这家有 DoH」列进去的 OpenDNS 只认 wireformat,点一下保存、下一次抓取
    当场 HTTP 400,面板上只剩一句「域名解析服务不可用」,人会以为是自己网络的事。
    """

    def test_the_page_gets_the_list_from_the_protocol_side(self):
        self.assertEqual([dict(item) for item in resolve.JSON_API_PRESETS],
                         egress.view()['presets'])

    def test_the_front_end_has_no_endpoint_of_its_own(self):
        """**第二份名单会静默过期**:哪天这里换了请求形式,前端那份还按老样子推荐。"""
        js = (Path(__file__).resolve().parents[1] / 'kb' / 'static' / 'js' / 'egress.js')
        code = '\n'.join(line for line in js.read_text(encoding='utf-8').splitlines()
                          if not line.lstrip().startswith('//'))
        self.assertEqual([], re.findall(r'https?://[^\s\'"`]+', code),
                         'egress.js 里又写死了端点地址')

    def test_every_preset_is_shaped_like_something_read_can_ask(self):
        """**只许放实测能用的**。这条拦不住「协议不对」(那要真发一次请求),但拦得住
        它的前提:端点必须是这一族自己的 `check_url` 认的 https 地址。"""
        for item in resolve.JSON_API_PRESETS:
            with self.subTest(name=item['name']):
                self.assertTrue(item['name'])
                if item['url']:                       # 「关掉」那一颗是空的
                    egress.check_url('endpoint', item['url'])
                    self.assertTrue(item['url'].startswith('https://'))


class MaskEndpointTests(EgressBase):
    """端点往日志里写之前的脱敏(`mask_endpoint`)。

    它存在的全部理由是「别把 token 写进落盘的日志」—— NextDNS、AdGuard 那类端点把
    profile id 放在 path 或 query 里。所以凡是它漏掉的形态,都等于这个函数在那一格
    没干活:实测漏了两种,**`user:pass@` 原样打出去**(它从 `netloc` 拼,而 netloc
    里就带着凭证),以及**没写协议头的地址整条原样返回**(`urlsplit` 认不出主机名)。

    第三种是补这两种时留下的:从原串剪到**第一个 `/`** 为止的那一版,对
    `doh.example?token=s3cret` 一个字都没剪 —— 它没有 `/`,而带 token 的端点恰恰常写成
    这个形状。所以现在**一个字都不从原串上剪,只从解析结果重拼**(拼不出就只打 MASK)。
    """

    def test_credentials_and_tokens_never_reach_the_line(self):
        for url, expected in (
                ('https://doh.example/dns-query?token=s3cret', 'https://doh.example'),
                ('https://user:s3cret@doh.example/q', 'https://doh.example'),
                ('https://doh.example:8443/abc/s3cret', 'https://doh.example:8443'),
                ('doh.example/dns-query?token=s3cret', 'doh.example'),
                ('user:s3cret@doh.example/q', 'doh.example'),
                ('doh.example?token=s3cret', 'doh.example'),
                ('doh.example:8443?token=s3cret', 'doh.example:8443'),
                ('user:s3cret@doh.example?token=x', 'doh.example'),
                ('u:s3cret@h://x', ''),
                ('  https://doh.example/q?token=s3cret  ', 'https://doh.example'),
                ('HTTPS://doh.example/q?token=s3cret', 'https://doh.example'),
                ('http://[::1', '')):
            with self.subTest(url=url):
                got = egress.mask_endpoint(url)
                self.assertNotIn('s3cret', got)
                if expected:
                    self.assertEqual(expected, got)

    def test_the_host_is_still_legible(self):
        """**反过来钉一边**:整条藏掉的话这一行就没用了 —— 它正是让人一眼看出
        「服务在问哪一台解析器」的那一行。"""
        self.stored(endpoint='https://doh.example:8443/q?token=s3cret')
        line = egress.summary()[0]
        self.assertIn('doh.example:8443', line)
        self.assertNotIn('s3cret', line)


if __name__ == '__main__':
    unittest.main()
