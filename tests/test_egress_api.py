# -*- coding: utf-8 -*-
"""「出口与解析」那块面板的接口(`/api/egress` 读、写、测)。

**钉的是后果,不是转发对不对**:页面上看到的值是不是生效的那一份、坏值有没有被挡在
盘外、以及**点一次「测一下」不许把线上配置改掉**(那颗按钮按设计是用还没保存的值去
试,实现上就是一层临时覆盖 —— 覆盖漏出去的话,人试一把就把正在跑的那一轮采集换了,
而面板上看不出来)。

配置文件和环境变量的隔离办法跟 `tests/test_feed_egress.py` 一样,理由见那一份。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import llm
from kb.feeds import egress, fetching, resolve
from kb.web import settings_api

DOH = 'https://doh.example/dns-query'


def doh_answer(request):
    """固定答案的 DoH 端点 + 任意正文,全程不出网。"""
    if 'dns-query' in request.url.path or 'resolve' in request.url.path:
        return httpx.Response(200, content=json.dumps(
            {'Status': 0, 'Answer': [{'type': 1, 'TTL': 300, 'data': '93.184.216.34'}]}).encode())
    return httpx.Response(200, content=b'feed')


class EgressApiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        orig = llm._SECRETS_PATH
        llm._SECRETS_PATH = Path(self.tmp.name) / 'secrets.json'
        self.addCleanup(lambda: setattr(llm, '_SECRETS_PATH', orig))
        resolve.forget()
        self.addCleanup(resolve.forget)
        env = patch.dict(os.environ, {name: '' for name in
                                      (egress.ENV_ENDPOINT, egress.ENV_SECONDS,
                                       *egress.ENV_PROXY)})
        env.start()
        self.addCleanup(env.stop)
        app = FastAPI()
        app.include_router(settings_api.router)
        self.client = TestClient(app)

    def stored(self, **fields):
        llm._SECRETS_PATH.write_text(json.dumps({egress.SECTION: fields}), encoding='utf-8')

    def armed(self):
        """把 httpx 换成 MockTransport —— 探测那条路会真解析,这里不许出网。"""
        real = httpx.Client
        return patch.object(fetching.httpx, 'Client',
                            side_effect=lambda **kw: real(**kw, transport=httpx.MockTransport(doh_answer)))


class ReadWriteTests(EgressApiBase):
    def test_the_page_sees_the_value_that_is_in_effect_and_where_it_came_from(self):
        with patch.dict(os.environ, {egress.ENV_ENDPOINT: 'https://from-env.example/q'}):
            body = self.client.get('/api/egress').json()
        self.assertEqual('https://from-env.example/q', body['endpoint'])
        self.assertEqual('env', body['sources']['endpoint'])

    def test_the_page_is_told_which_variable_is_doing_the_falling_back(self):
        """**出处要带上到底是哪个变量名。** 代理那一族有四个候选(`HTTPS_PROXY` /
        `https_proxy` / `ALL_PROXY` / `all_proxy`),只说「来自环境变量」的话人得四个都
        grep 一遍才知道去改哪个;而前端自己写一份名字就是第二份口径。
        **页面那边的探针碰不到这一条**(它用的是固定夹具),所以钉在这儿。
        """
        with patch.dict(os.environ, {'ALL_PROXY': 'socks5://127.0.0.1:7897'}):
            body = self.client.get('/api/egress').json()
        self.assertEqual('来自环境变量 ALL_PROXY', body['where']['proxy'])
        self.assertEqual('', body['where']['endpoint'], '没配的项不该编一个出处出来')

    def test_the_proxy_password_never_leaves_the_machine(self):
        self.stored(proxy='http://me:s3cret@127.0.0.1:7897')
        body = self.client.get('/api/egress').json()
        self.assertNotIn('s3cret', json.dumps(body))
        self.assertIn('127.0.0.1:7897', body['proxy'], '主机端口也藏了,人没法改端口')

    def test_saving_lands_in_settings_and_is_reported_as_such(self):
        r = self.client.post('/api/egress', json={'endpoint': DOH, 'seconds': '5', 'proxy': ''})
        self.assertEqual(200, r.status_code)
        self.assertEqual('settings', r.json()['sources']['endpoint'])
        self.assertEqual(DOH, egress.endpoint(), '写盘了但下一次取值没看见')

    def test_a_bad_value_is_refused_with_the_box_it_belongs_to(self):
        """**`detail` 必须还是一句话**:ui.js 的 `apiFail` 只认字符串,给它一个对象的话
        那句话会被静默换成「保存失败」—— 而人这时唯一想知道的就是哪儿填错了。"""
        for payload, field in ((('ftp://x/y', '', ''), 'endpoint'),
                               (('', 'abc', ''), 'seconds'),
                               (('', '', 'tcp://127.0.0.1:1'), 'proxy')):
            with self.subTest(field=field):
                r = self.client.post('/api/egress', json=dict(
                    zip(('endpoint', 'seconds', 'proxy'), payload)))
                self.assertEqual(400, r.status_code)
                self.assertIsInstance(r.json()['detail'], str)
                self.assertEqual(field, r.json()['field'])
                self.assertFalse(llm._SECRETS_PATH.exists(), '坏值写进盘了')


class ProbeTests(EgressApiBase):
    """「测一下」:用还没保存的那几项真解析一次。"""

    def test_it_answers_with_the_addresses_a_fetch_would_see(self):
        with self.armed():
            body = self.client.post('/api/egress/probe',
                                    json={'endpoint': DOH, 'host': 'example.org'}).json()
        self.assertTrue(body['ok'], body)
        self.assertEqual(['93.184.216.34'], body['addresses'])
        self.assertEqual('doh', body['via'])

    def test_it_does_not_write_the_values_to_disk(self):
        """**这颗按钮不是「保存并测试」。** 写盘了的话,人拿一个端点试一把就把线上
        配置换了 —— 而他以为自己只是看了一眼。"""
        with self.armed():
            self.client.post('/api/egress/probe', json={'endpoint': DOH, 'host': 'example.org'})
        self.assertFalse(llm._SECRETS_PATH.exists(), '测一下把配置写进盘了')
        self.assertEqual('', egress.endpoint(), '测完端点留在了进程里')

    def test_a_probe_does_not_change_what_other_threads_resolve_with(self):
        """**覆盖的作用域是这一次请求。** 漏出去的话,正在跑的那一轮采集会在人点按钮的
        那一刻换掉解析器 —— 面板上看不出来,而那一轮的失败明细会写成别的原因。
        """
        seen: list[str] = []
        gate, done = threading.Event(), threading.Event()

        def watcher():
            gate.wait(5)
            seen.append(egress.endpoint())
            done.set()

        threading.Thread(target=watcher, daemon=True).start()
        with egress.using(endpoint=DOH):
            gate.set()
            done.wait(5)
            self.assertEqual(DOH, egress.endpoint(), '覆盖在本线程里没生效')
        self.assertEqual([''], seen, '覆盖漏到了另一个线程')

    def test_an_empty_endpoint_probes_the_system_resolver(self):
        """端点留空也值得测:那一下走系统解析,**正好把本机被污染这件事摆出来**。"""
        with patch.object(fetching.socket, 'getaddrinfo',
                          return_value=[(2, 1, 6, '', ('31.13.75.12', 0))]):
            body = self.client.post('/api/egress/probe', json={'host': 'mistral.ai'}).json()
        self.assertEqual('system', body['via'])
        self.assertEqual(['31.13.75.12'], body['addresses'])

    def test_a_failure_is_reported_as_a_result_not_as_a_broken_api(self):
        """解析失败是**这颗按钮的正常输出**(它就是来看哪一步给错了的),不是 500 ——
        500 在面板上只会变成一句没有信息的「测试失败」。"""
        with patch.object(fetching.socket, 'getaddrinfo',
                          return_value=[(2, 1, 6, '', ('127.0.0.1', 0))]):
            r = self.client.post('/api/egress/probe', json={'host': 'buy-european.net'})
        self.assertEqual(200, r.status_code)
        self.assertFalse(r.json()['ok'])
        self.assertIn('公网', r.json()['error'])

    def test_a_pasted_link_is_accepted_as_a_hostname(self):
        """人往域名框里粘整条链接是常事。"""
        with patch.object(fetching.socket, 'getaddrinfo',
                          return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
            body = self.client.post('/api/egress/probe',
                                    json={'host': 'https://example.org/news/x?a=1'}).json()
        self.assertEqual('example.org', body['host'])

    def test_the_masked_password_is_not_what_gets_probed(self):
        """页面回传的是脱敏过的那一份;拿 `***` 去连代理的话,「测一下」会报一个
        跟真实配置无关的失败。"""
        self.stored(proxy='http://me:s3cret@127.0.0.1:7897')
        seen: list[str] = []
        with patch.object(egress, 'client_kwargs', side_effect=lambda: seen.append(egress.proxy()) or {}):
            with patch.object(fetching.socket, 'getaddrinfo',
                              return_value=[(2, 1, 6, '', ('93.184.216.34', 0))]):
                self.client.post('/api/egress/probe', json={
                    'endpoint': DOH, 'proxy': f'http://me:{egress.MASK}@127.0.0.1:7897',
                    'host': 'example.org'})
        # A 和 AAAA 各建一次客户端,所以这里是集合不是单条
        self.assertEqual({'http://me:s3cret@127.0.0.1:7897'}, set(seen))
        self.assertTrue(seen, '一次都没问过代理')


if __name__ == '__main__':
    unittest.main()
