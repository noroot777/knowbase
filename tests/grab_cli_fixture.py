# -*- coding: utf-8 -*-
"""命令行那条入口两组用例共用的夹具:**一个在进程内跑的假 kb-web**。

**不打桩那几个函数,起一个真服务**:这样顺带钉住线上协议(打哪个路径、什么方法、
payload 里有哪几个键),也让「本机请求不许走代理」和「正文读到一半断掉」这两条验
得到真后果 —— 打桩掉 HTTP 的话,它们就只剩结构断言。

假服务只认我们真该打的那几条路径,别的一律 404 并记账:`/api/pipeline/run` 那条
(恢复必须由服务端按条目归属去做,这边去打它就是写死来源)、以及**旧服务没有的**
`/api/grab/restore`(跨版本那一刻正是靠它回 404 才失败关闭的)。

判据的方法论写在 `tests/test_grab_cli.py` 的模块说明里。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('grab_cli', ROOT / 'scripts' / 'grab_cli.py')
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

URL = 'https://example.org/posts/hello'
OTHER = 'https://example.org/posts/second'
#: 自带一对括号的真实形态(维基那一类)。截断之后那个地址**可能照样存在** ——
#: 于是收进来的是另一篇文章,比抓不到更坏。
PAREN = 'https://en.wikipedia.org/wiki/Function_(mathematics)'
#: 中文维基的条目名用的是**全角**括号,浏览器地址栏里就是裸的这一串(复制出来也是)。
#: **「全角括号只能是行文」是个错的假设**(实测):按它办,这一条就被截成
#: `…/wiki/函数` —— **而那一页真的存在**,收进来的是另一篇文章。所以括号(半角全角
#: 都算)按**配对**算:配得上对的属于地址,落单的那个才标志行文从那里开始。
#: 它和 `PAREN` 钉的是同一件事的两半:半角那一对、全角那一对都不许被剥光。
FULLWIDTH = 'https://zh.wikipedia.org/wiki/函数（数学）'
#: 行内抽取要用到的几条真实形态。末尾那几个 ASCII 标点**在地址里合法**
#: (`grab.normalize` 收下并保留),所以只在「这一行夹着叙述」时才当外围标点剥。
STDIN_WHOLE_LINES = [FULLWIDTH, 'https://example.org/post;',
                     'https://example.org/post,', 'https://example.org/post:']


def _state(**over) -> dict:
    """`/api/grab/state` 的一份回包。默认:收下了、内容是正文、还没走完后半条链。"""
    base = {'ok': True, 'url': URL, 'known': True, 'done': False, 'content_ok': True,
            'kind': 'inbox', 'title': '把链接存进知识库', 'exported': False,
            'stored': False, 'indexed': False, 'owing': '还没写成 vault 里那一篇',
            'resume': '，稍后在「手动收录」里点一次「写入知识库」'}
    return {**base, **over}


def _done(**over) -> dict:
    return _state(done=True, exported=True, stored=True, indexed=True, owing='', **over)


def _absent(**over) -> dict:
    """`/api/grab/state` 的「库里压根没有这一行」那一档。

    **它只说明这一刻没有。** 一次没拿到明确回答的投递还在跑的时候,拿它当「那次
    动作不会写入」的证据就是谎报(见 `verdict` 的 `acked`)。
    """
    return {'ok': True, 'url': URL, 'known': False, 'done': False,
            'content_ok': False, 'owing': '', **over}


def _grabbed(**over) -> dict:
    """`/api/grab` 成功收下并起了一轮的回包。"""
    base = {'ok': True, 'added': 1, 'duplicate': False, 'done': False,
            'content_ok': True, 'url': URL, 'title': '把链接存进知识库',
            'kind': 'inbox', 'pipeline': 'started', 'run': 'run-1',
            'detail': '已收下，正在写入知识库'}
    return {**base, **over}


class _Handler(BaseHTTPRequestHandler):
    """假 kb-web。**只认我们真的该打的那几条路径**;`/api/pipeline/run` 故意回
    404 并记账(见模块说明),别的一律 404 —— 多打一条路径的改动要当场红。

    `/api/grab/restore` 单独记账(`plan['restored']`)、回包按 `plan['restores']`
    排 —— 把它排成 404 就是**更新了文件却没重启的那个旧 kb-web**:它压根没有这条
    路。那一档不许被降级成 `/api/grab`(会抓一篇新的建进去)。
    """

    def log_message(self, *a):          # 不要往测试输出里刷访问日志
        pass

    def _reply(self, code: int, payload):
        body = (json.dumps(payload) if not isinstance(payload, str) else payload).encode()
        if self.server.plan['cut_body']:
            # **正文读到一半对方关连接**:声明一个更长的 Content-Length,只写几个
            # 字节就把连接撕掉 —— `http.client.IncompleteRead`,**不是** OSError。
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body) + 200))
            self.end_headers()
            self.wfile.write(body[:12])
            self.wfile.flush()
            self.close_connection = True
            return
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _next(self, queue: list):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def do_GET(self):
        plan = self.server.plan
        plan['calls'].append(('GET', self.path, None))
        if self.path.startswith('/api/grab/state'):
            asked = (parse_qs(urlsplit(self.path).query).get('url') or [''])[0]
            plan['asked'].append(asked)
            if plan['state_delay']:
                import time as _t
                _t.sleep(plan['state_delay'])
            queue = plan['states'].get(asked) or plan['states']['*']
            return self._reply(*self._next(queue))
        if self.path.startswith('/api/pipeline/status'):
            if plan['status_delay']:
                import time as _t
                _t.sleep(plan['status_delay'])
            return self._reply(200, {'running': plan['running']})
        self._reply(404, {'detail': '没有这条路径'})

    def do_POST(self):
        plan = self.server.plan
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        body = json.loads(raw or b'{}')
        plan['calls'].append(('POST', self.path, body))
        if self.path == '/api/grab':
            plan['posted'].append(body.get('url'))
            return self._reply(*self._next(plan['grabs']))
        if self.path == '/api/grab/restore':
            plan['restored'].append(body.get('url'))
            return self._reply(*self._next(plan['restores']))
        if self.path == '/api/pipeline/run':
            plan['runs'] += 1
            return self._reply(404, {'detail': '恢复不该由命令行自己起一轮'})
        self._reply(404, {'detail': '没有这条路径'})


class GrabCliCase(unittest.TestCase):
    def setUp(self):
        # 轮询间隔压到几乎为零:用例要验的是「收敛到哪一档」,不是真等那几秒。
        for name, value in (('POLL_SECONDS', 0.01), ('MAX_RENEWS', 2)):
            self.addCleanup(setattr, cli, name, getattr(cli, name))
            setattr(cli, name, value)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.plan = {'grabs': [(200, _grabbed())], 'states': {'*': [(200, _state())]},
                            # 恢复那条路**默认是新服务的样子**:认得 `/restore`、
                            # 查重命中、起后半条链。排成 404 就是没重启的那个旧进程。
                            'restores': [(200, _grabbed(added=0, duplicate=True))],
                            'running': False, 'runs': 0, 'calls': [], 'posted': [],
                            'restored': [], 'asked': [], 'cut_body': False,
                            'state_delay': 0, 'status_delay': 0}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api = 'http://127.0.0.1:%d' % self.server.server_address[1]

    # ---------------------------------------------------------------- 工具

    @property
    def plan(self):
        return self.server.plan

    def run_cli(self, *argv, timeout='0.3', stdin=None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        if stdin is not None:
            self.addCleanup(setattr, sys, 'stdin', sys.stdin)
            sys.stdin = io.StringIO(stdin)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['--api', self.api, '--timeout', timeout, *argv])
        return code, out.getvalue(), err.getvalue()

    def records(self, text: str) -> list[dict]:
        return [json.loads(line) for line in text.strip().splitlines() if line.strip()]

    def paths(self, method: str) -> list[str]:
        return [p for m, p, _ in self.plan['calls'] if m == method]
