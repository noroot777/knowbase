# -*- coding: utf-8 -*-
"""`.\\kb.ps1 status` 读接口时不许被控制台代码页改写掉响应体。

**事故形态(实测,这一条是真进去过的)。** PowerShell 解码原生程序 stdout 用的是
`[Console]::OutputEncoding`,默认是系统 OEM 代码页(中文机器上是 936)。而
kb-web 返回的是 UTF-8,于是每个中文字的多字节序列被按 GBK 拆错,**连带把 JSON 的
引号吃掉**:`"message":"采集器还没装好"` 变成 `"message":"采集器还没装�?,`,
`ConvertFrom-Json` 当场抛 `ArgumentException`。

**后果不止是 status 少打一行。** `Get-PipelineState` 读的是同一份 JSON:解析不了
就落在「问不出来」那一档,于是 `stop` / `restart` **一律拒绝停**(怕砍断正在写
kb.db 的那一步),而浏览器里打开面板一切正常 —— 人只会觉得这个入口坏了。
所以判据落在**命令打出来的那行字**上:说「空闲」才算读懂了,说「(读不到)」就是
这个 bug 又回来了。

命令级测试,口径同 `tests/test_kb_web_start.py`:**跑真脚本**,只把 kb-web 换成一个
只回一条带中文的 `/api/pipeline/status` 的假服务,`KB_WEB_PORT` 指到一个空闲端口。
判定逻辑一行都不打桩 —— 这件事的全部风险就在「那份字节怎么变成字符串」上。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 响应体里这几个字是关键:非 ASCII,而且紧跟着就是 JSON 的结构字符(引号、逗号)。
#: 按 GBK 拆错时被吃掉的正是那个引号 —— 换成纯 ASCII 的话这条用例就永远绿。
CN_MESSAGE = '采集器还没装好'
BODY = (
    '{"running":false,"stop_requested":false,"current_stage":"",'
    '"stages":{"crawl":{"status":"","detail":""}},'
    f'"source":{{"online":false,"message":"{CN_MESSAGE}",'
    '"hint":"缺 .venv、data——跑一次 setup","label":"公众号"},'
    '"log":[],"stop_supported":true}'
)


class _Handler(BaseHTTPRequestHandler):
    #: 收到过几次请求。**用来把两种「读不到」分开**:请求压根没到(代理把回环吃了、
    #: 或者这台机器此刻太忙)和请求到了却解析不出来(本文件要钉的那个 bug)。
    hits = 0

    def do_GET(self):                                      # noqa: N802
        type(self).hits += 1
        raw = BODY.encode('utf-8')                         # 接口就是 UTF-8
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):                             # 别往测试输出里刷访问日志
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _powershell() -> str | None:
    for exe in ('pwsh', 'powershell'):
        found = shutil.which(exe)
        if found:
            return found
    return None


#: kb.ps1 顶层 dot-source 的那几份,跑真脚本时必须一起拷过去。
#: 口径同 `tests/kb_sh_fixture.py`:**少拷一个,隔离环境里脚本一加载就炸,而红出来
#: 的话指不到「夹具没拷全」这件事**。这里现算,不写死清单。
def _sourced() -> list[str]:
    import re
    src = (REPO / 'kb.ps1').read_text(encoding='utf-8-sig')
    rels = re.findall(r"^\.\s+\(Join-Path\s+\$script:Root\s+'([^']+)'\)", src, re.M)
    assert rels, '一条 dot-source 都没认出来 —— 写法变了,先修这条正则'
    return [r.replace('\\', '/') for r in rels]


class StatusEncodingTest(unittest.TestCase):
    """假服务回一份带中文的 JSON,`status` 必须读懂它。"""

    def setUp(self) -> None:
        self.ps = _powershell()
        if not self.ps:
            self.skipTest('没有 powershell / pwsh —— 这是 Windows 入口的命令级测试')
        self.port = _free_port()
        _Handler.hits = 0
        # **ThreadingHTTPServer**:单线程那版在机器忙的时候会让请求排队,而
        # Invoke-Api 带着 `--max-time 5`,于是这条用例在并发跑全量回归时会假红。
        self.srv = ThreadingHTTPServer(('127.0.0.1', self.port), _Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        # 先自己确认夹具真的通了:夹具自己坏掉要在 setUp 就红,
        # 不该伪装成下面那条断言。
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/api/pipeline/status',
                                    timeout=20) as r:
            self.assertEqual(json.loads(r.read().decode('utf-8'))['running'], False)
        _Handler.hits = 0

        self._tmp = tempfile.mkdtemp()
        self.root = Path(self._tmp) / 'repo'
        (self.root / 'scripts').mkdir(parents=True)
        shutil.copy2(REPO / 'kb.ps1', self.root / 'kb.ps1')
        for rel in _sourced():
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dest)
        # Show-GraphStale 别去读用户真 vault;kb/ 不存在时 Show-CodeStale 自己安静
        self.vault = Path(self._tmp) / 'vault'
        self.vault.mkdir()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _status(self) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith('KB_')}
        env['KB_WEB_PORT'] = str(self.port)
        env['KB_VAULT_ROOT'] = str(self.vault)
        out = subprocess.run([self.ps, '-NoProfile', '-NonInteractive',
                              '-ExecutionPolicy', 'Bypass',
                              '-File', str(self.root / 'kb.ps1'), 'status'],
                             capture_output=True, env=env, timeout=240)
        # **按 UTF-8 读回来,别信本地代码页** —— 这条用例自己要是踩同一个坑就毫无意义。
        # kb.ps1 把中文按控制台代码页写出去,所以两种都试:先 UTF-8,退到 GBK。
        raw = out.stdout + out.stderr
        for enc in ('utf-8', 'gbk'):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode('utf-8', 'replace')

    def test_status_reads_a_utf8_body(self) -> None:
        text = self._status()
        # **先分清是哪一种「读不到」。** 请求压根没到夹具的话,病根不是编码 ——
        # 可能是代理把回环吃了(`--noproxy *` 被人删掉就是这个形态),也可能是这台
        # 机器此刻太忙、`--max-time 5` 到点了。两者都该说清楚,不许混成一句。
        self.assertGreater(_Handler.hits, 0,
                           '夹具一次请求都没收到 —— 要么 curl 的 `--noproxy *` 被删了'
                           '(全局代理会把 127.0.0.1 吃掉),要么这台机器太忙超了'
                           '`--max-time 5`。先确认是哪一种,别当成编码问题。')
        self.assertNotIn('(读不到)', text,
                         '请求到了夹具、响应体却解析不出来:被控制台代码页改写了,'
                         'ConvertFrom-Json 抛了 —— Get-PipelineState 会因此永远落在'
                         '「问不出来」,stop/restart 一律拒绝停')
        self.assertIn('空闲', text, f'没读出「空闲」这一档;实际输出:\n{text}')

    def test_the_fixture_really_serves_non_ascii(self) -> None:
        """反过来钉一边:夹具真的回了非 ASCII,上面那条才有意义。

        响应体要是悄悄退化成纯 ASCII(比如有人把中文换成拼音),上面那条会永远绿,
        而真 bug 照旧。
        """
        self.assertFalse(BODY.isascii(), '夹具的响应体已经没有非 ASCII 了')
        raw = BODY.encode('utf-8')
        self.assertNotEqual(raw, BODY.encode('gbk', 'replace'),
                            'UTF-8 和 GBK 编出来一样,这份料测不出编码问题')


if __name__ == '__main__':
    unittest.main()
