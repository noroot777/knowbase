# -*- coding: utf-8 -*-
"""手动收录的命令行入口(`scripts/grab_cli.py`,即 `./kb.sh grab` 和 skill
`kb-grab` 走的那条路)。

**判据一律落在「终端上那行字 + 退出码 + 真发出去的那几个请求」上**,不验中间
标志 —— 这一层唯一的职责就是把服务端的事实说成一句不谎报的话,而它最容易出的
错恰恰是「话说对了档位却挑错」:把「存下了、还搜不到」说成「已写入知识库」,
用户去检索里找不到,再投一次只会得到同一句「已经收过了」,后半条链永远没人补。

**用一个在进程内跑的假 kb-web,而不是打桩那几个函数**:这样顺带钉住线上协议
(打哪个路径、什么方法、payload 里有没有 `refetch`),也让「本机请求不许走代理」
和「正文读到一半断掉」这两条验得到真后果 —— 打桩掉 HTTP 的话,它们就只剩结构
断言。假服务对 `/api/pipeline/run` **一律回 404 并记账**:恢复必须由服务端按条目
归属去做,这边再去打那条路径就是写死来源,用例要当场红。

反过来钉一边(元规则):该说「没收下」的那一档照旧说得出口(4xx),而 5xx 一律
不许说成「没收下」—— 保存排在起管道之前,那时写没写进去我们并不知道。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tests.headless import fixture_server, requires_headless

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
#: **「全角括号只能是行文」是个错的假设**(第 2 轮审查):按它办,这一条就被截成
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


def _grabbed(**over) -> dict:
    """`/api/grab` 成功收下并起了一轮的回包。"""
    base = {'ok': True, 'added': 1, 'duplicate': False, 'done': False,
            'content_ok': True, 'url': URL, 'title': '把链接存进知识库',
            'kind': 'inbox', 'pipeline': 'started', 'run': 'run-1',
            'detail': '已收下，正在写入知识库'}
    return {**base, **over}


class _Handler(BaseHTTPRequestHandler):
    """假 kb-web。**只认我们真的该打的那几条路径**;`/api/pipeline/run` 故意回
    404 并记账(见模块说明),别的一律 404 —— 多打一条路径的改动要当场红。"""

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
        if self.path == '/api/pipeline/run':
            plan['runs'] += 1
            return self._reply(404, {'detail': '恢复不该由命令行自己起一轮'})
        self._reply(404, {'detail': '没有这条路径'})


class GrabCliTests(unittest.TestCase):
    def setUp(self):
        # 轮询间隔压到几乎为零:用例要验的是「收敛到哪一档」,不是真等那几秒。
        for name, value in (('POLL_SECONDS', 0.01), ('MAX_RENEWS', 2)):
            self.addCleanup(setattr, cli, name, getattr(cli, name))
            setattr(cli, name, value)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.plan = {'grabs': [(200, _grabbed())], 'states': {'*': [(200, _state())]},
                            'running': False, 'runs': 0, 'calls': [], 'posted': [],
                            'asked': [], 'cut_body': False, 'state_delay': 0,
                            'status_delay': 0}
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

    # ---------------------------------------------------------------- 各档

    def test_a_finished_link_is_reported_as_in_the_knowledge_base(self):
        self.plan['states'] = {'*': [(200, _done())]}
        code, out, _ = self.run_cli('--json', URL)
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'saved', rec)
        self.assertEqual(rec['label'], '已写入知识库')
        self.assertTrue(rec['done'])

    def test_a_stored_but_unwritten_link_is_never_called_written(self):
        """**这一条是这一层存在的理由**:存下了、还搜不到,就得这么说。

        而且它不是失败(退出码 0):内容已经在库里了,写进知识库那一步由任何一轮
        inbox 的 export 兜住 —— 报成失败会让调用方(skill / 脚本)去重投,而重投
        被按 URL 去重挡住,一个字节都不会动。
        """
        code, out, _ = self.run_cli(URL)
        self.assertEqual(code, 0, out)
        self.assertIn('已存下，还没写进知识库', out)
        self.assertNotIn('已写入知识库', out)
        self.assertIn('还没写成 vault 里那一篇', out)
        self.assertIn('点一次「写入知识库」', out)       # 下一步要指得出来

    def test_content_that_does_not_look_like_an_article_offers_refetch(self):
        self.plan['states'] = {'*': [(200, _state(content_ok=False,
                                                  owing='收下的那一份看不出是正文'))]}
        code, out, _ = self.run_cli(URL)
        self.assertEqual(code, 0, out)
        self.assertIn('看不出是正文', out)
        self.assertIn('--refetch', out)
        self.assertNotIn('已写入知识库', out)

    def test_a_refused_link_says_so_and_asks_nothing_further(self):
        """4xx 是唯一说得出「没收下」的一档:那时一个字节都没写。

        **而且不许再去问状态** —— 库里压根没有这一行,多问一次只是白跑。
        """
        self.plan['grabs'] = [(400, {'detail': '这一页挂着验证，没收下'})]
        code, out, _ = self.run_cli(URL)
        self.assertEqual(code, 1, out)
        self.assertIn('没收下', out)
        self.assertIn('这一页挂着验证', out)
        self.assertEqual(self.paths('GET'), [], '4xx 之后不该再问这一篇的状态')

    def test_a_server_fault_does_not_claim_the_link_was_dropped(self):
        """**5xx ≠ 没收下。** 保存排在起管道之前,所以那一行可能已经在库里了 ——
        去问一次只读的事实,问到了就照事实说。
        """
        self.plan['grabs'] = [(500, 'Internal Server Error')]
        self.plan['states'] = {'*': [(200, _done())]}
        code, out, err = self.run_cli('--json', URL)
        self.assertEqual(code, 0, out + err)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'saved', rec)
        self.assertNotIn('没收下', out)

    def test_a_server_fault_with_no_answer_stays_uncertain(self):
        """问都问不出来时**不许替它下结论**:既不说存下了,也不说没存下。

        **而且那一条仍然要有自己的一行输出** —— 没有它,单条命令的 stdout 一个字
        都没有,而 skill 被告知「只按 status 汇报」时手里压根没有 status。
        """
        self.plan['grabs'] = [(500, 'boom')]
        self.plan['states'] = {'*': [(500, 'boom')]}
        code, out, err = self.run_cli('--json', URL)
        self.assertEqual(code, 1, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'unknown', rec)
        self.assertEqual(rec['url'], URL)
        self.assertIn('打不通 kb-web', err)

    def test_a_link_that_already_finished_costs_no_extra_poll(self):
        """幂等那一档(后端没起管道,因为它早走完了):一次状态都不必问。

        **而且不许把「没查过」编码成「确认未完成」**:后端回包里就有
        `done=true`,输出 `status=duplicate, done=false` 是自相矛盾的一行。
        """
        self.plan['grabs'] = [(200, {'ok': True, 'added': 0, 'duplicate': True,
                                     'done': True, 'content_ok': True, 'url': URL,
                                     'title': '旧文', 'kind': 'inbox',
                                     'detail': '这条已经在知识库里了（手动收录）'})]
        code, out, _ = self.run_cli('--json', URL)
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual((rec['status'], rec['done']), ('duplicate', True), rec)
        self.assertEqual(self.paths('GET'), [])

    # ---------------------------------------------------------------- 重抓

    def test_a_successful_refetch_is_followed_through_not_called_bad(self):
        """**重抓换成了 ≠ 内容不对。** 后端那一档(`_recover` 的成功分支)只更新
        `replaced` / `done` / `title`,回包里的 `content_ok` 仍是**库里旧那一份**的
        判断 —— 照它先判就把一次成功的替换说成「看不出是正文」,而且**不去等它写进
        知识库**,用户那一次重抓于是永远停在「没换成」。
        """
        self.plan['grabs'] = [(200, _grabbed(
            added=0, duplicate=True, replaced=True, content_ok=False,
            detail='你要求重新抓一次，已经按这次重抓到的内容换掉，正在写入知识库'))]
        self.plan['states'] = {'*': [(200, _done())]}
        code, out, _ = self.run_cli('--json', '--refetch', URL)
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'saved', rec)
        self.assertIn('换掉', rec['detail'])
        self.assertTrue(self.paths('GET'), '换成了之后要去核这一篇写进去了没有')

    def test_a_successful_refetch_survives_a_state_hiccup(self):
        """**问不出来的那一轮不许重算档位。** 换成了、而第一次 state 恰好 5xx ——
        拿 `state=None` 去重算,旧那一版的 `content_ok=false` 会把它判成
        「看不出是正文」并当场停止等待,而新正文明明已经换好了。
        """
        self.plan['grabs'] = [(200, _grabbed(added=0, duplicate=True, replaced=True,
                                             content_ok=False, detail='已经换掉'))]
        self.plan['states'] = {'*': [(500, 'boom'), (200, _done())]}
        code, out, _ = self.run_cli('--json', '--refetch', URL, timeout='3')
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'saved', rec)

    def test_a_refetch_whose_answer_was_lost_is_not_called_done(self):
        """**「库里那一篇走完整条链了」证明不了这次替换成功。** 显式重抓的回包丢了
        (5xx / 断线),而库里旧那一版本来就 `done` —— 照 `done` 报「已写入知识库」
        就是把一件不知道的事说成已完成,而用户要的是**新**那一篇。
        """
        self.plan['grabs'] = [(500, 'boom')]
        self.plan['states'] = {'*': [(200, _done())]}
        code, out, _ = self.run_cli('--json', '--refetch', URL)
        self.assertEqual(code, 1, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'unknown', rec)
        self.assertIn('换没换成', rec['detail'])

    def test_a_refetch_that_did_not_land_is_not_called_written(self):
        self.plan['grabs'] = [(200, _grabbed(
            added=0, duplicate=True, replaced=False, pipeline=None,
            detail='这次重抓还是不行:这一页挂着验证'))]
        code, out, _ = self.run_cli('--json', '--refetch', URL)
        self.assertEqual(code, 0, out)
        # **断在档位上,不是断在那句话上**:只验「出现了这次重抓那句原话」时,
        # 把这一档说成「早就在知识库里了」照旧绿 —— 而那正是谎报的形状。
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'bad_content', rec)
        self.assertIn('这次重抓还是不行', rec['detail'])

    # ---------------------------------------------------------------- 只读 / 参数

    def test_state_only_never_posts_anything(self):
        """`--state` 是只读的:**一个 POST 都不许发**(不抓取、不起管道)。"""
        self.plan['states'] = {'*': [(200, _done())]}
        code, out, _ = self.run_cli('--json', '--state', URL)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.paths('POST'), [])
        self.assertEqual(self.records(out)[0]['status'], 'saved')

    def test_state_only_reports_an_unseen_link_as_absent_not_failed(self):
        self.plan['states'] = {'*': [(200, {'ok': True, 'url': URL, 'known': False,
                                            'done': False, 'content_ok': False,
                                            'owing': ''})]}
        code, out, _ = self.run_cli('--json', '--state', URL)
        self.assertEqual(code, 0, out)        # 「没收过」是一个正常答案,不是故障
        self.assertEqual(self.records(out)[0]['status'], 'absent')

    def test_state_only_cannot_say_a_read_was_never_tried(self):
        """**「现在查不到」不是「还没投」。** `--state` 只读地问一句「这篇我存过
        吗」,压根没有投递这个动作 —— 第一条就打不通时把它写成「前面那一条就打不通
        服务了」,是在说一件不存在的事(而且它那句默认文案指的是**别人**)。

        反过来钉一边:**后面那几条确实没试过**,那一档照旧说得出口 —— 两者的区别
        正是调用方要的(哪条已经问过、哪些压根没问)。
        """
        self.plan['states'] = {'*': [(500, 'boom')]}
        code, out, err = self.run_cli('--state', '--json', URL, OTHER)
        self.assertEqual(code, 1, out + err)
        recs = self.records(out)
        self.assertEqual([r['status'] for r in recs], ['unknown', 'not_tried'])
        self.assertIn('不能确定这篇存过没有', recs[0]['detail'])
        self.assertNotIn('还没投', recs[0]['detail'])

    def test_state_only_turns_a_rejected_address_into_a_sentence(self):
        """一批地址里混进一行脏数据是常事:4xx 要变成一句话,不是 traceback。"""
        self.plan['states'] = {'*': [(400, {'detail': '这不是一条链接'})]}
        code, out, err = self.run_cli('--json', '--state', 'not-a-link')
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.records(out)[0]['status'], 'failed')
        self.assertIn('这不是一条链接', out)
        self.assertEqual(err, '', '不该把异常栈吐给用户')

    def test_refetch_is_passed_through_only_when_asked(self):
        self.run_cli('--no-wait', URL)
        self.run_cli('--no-wait', '--refetch', URL)
        asked = [body['refetch'] for m, p, body in self.plan['calls']
                 if m == 'POST' and p == '/api/grab']
        self.assertEqual(asked, [False, True])

    def test_no_wait_does_not_pretend_the_link_is_searchable(self):
        code, out, _ = self.run_cli('--no-wait', URL)
        self.assertEqual(code, 0, out)
        self.assertNotIn('已写入知识库', out)
        self.assertEqual(self.paths('GET'), [], '--no-wait 不该去轮询')

    # ---------------------------------------------------------------- 地址本身

    def test_an_explicit_address_is_handed_over_untouched(self):
        """**显式给出的那一条一个字符都不许动。** 自带一对括号的地址(维基那一类)
        在第一个 `)` 处被截断之后,那个地址**可能照样存在** —— 收进来的于是是另一篇
        文章,比抓不到更坏;`--refetch` / `--state` 走同一条解析,所以还会连带认错
        「要替换哪一篇」「要查哪一篇」。
        """
        self.run_cli('--no-wait', FULLWIDTH)
        self.run_cli('--no-wait', '--refetch', FULLWIDTH)
        self.run_cli('--state', FULLWIDTH)
        self.run_cli('--no-wait', 'https://example.org/post!')
        self.assertEqual(self.plan['posted'],
                         [FULLWIDTH, FULLWIDTH, 'https://example.org/post!'])
        self.assertEqual(self.plan['asked'], [FULLWIDTH])

    def test_whole_lines_on_stdin_are_handed_over_untouched(self):
        """**标准输入里整行就是一条地址的那些,和显式参数一样保真。**

        `-` 既用来粘一段话、也用来喂「一行一条」的清单,而第 2 轮审查实测的四组
        恰恰都是后者:全角括号那一条被截成 `…/wiki/函数`(那一页真的存在),末尾带
        `;` `,` `:` 的三条被剥掉最后一个字符 —— 后端 `normalize` 本来原样收下它们。
        判据落在**真发出去的那几个请求**上,不是 `links_in` 的返回值。
        """
        self.run_cli('--no-wait', '-', stdin='\n'.join(STDIN_WHOLE_LINES))
        self.assertEqual(self.plan['posted'], STDIN_WHOLE_LINES)

    def test_pasted_prose_keeps_paired_brackets_and_cuts_at_the_lone_one(self):
        """`-` 那一行**夹着叙述文字**时才抽取,判据有两条:

        **括号按配对算** —— 配得上对的属于地址(`…/wiki/函数（数学）` 那一对),
        **落单的右括号**标志行文从那里开始(`（见 <地址>）以及别的`)。
        **全角句读直接切断** —— 它在裸地址里没有来源(要用必须先编码),所以
        「<地址>。后面还有话」这种中间没有空白的常见写法接得住。
        """
        self.run_cli('--no-wait', '-',
                     stdin=f'看这篇 {PAREN} 挺好，还有（{OTHER}）。\n'
                           f'另外 {FULLWIDTH} 也值得读。')
        self.assertEqual(self.plan['posted'], [PAREN, OTHER, FULLWIDTH])

    # ---------------------------------------------------------------- 恢复

    def test_recovery_goes_through_the_server_not_a_hardcoded_channel(self):
        """批量投递时后面几条必然撞上前一条起的那一轮(后端如实回 `busy`),它们的
        内容在库里却没人替它们跑 export。**补这一步要让服务端按那一条自己的归属去
        做** —— 再 POST 一次 `/api/grab`(查重命中,不发任何对外抓取,服务端现查
        归属 + `reclaim_export` + 按 `seen['kind']` 起那一轮)。

        **不许自己打 `/api/pipeline/run` 写死 `inbox`**:一条推文被「大V观点」收过
        时,起一轮 `inbox` 的 export 一个字都不会动它(真库 + 真 `sync.export`
        实测过)。所以假服务对那条路径一律 404 并记账,这里断言它一次都没被打过。
        """
        self.plan['grabs'] = [
            (200, _grabbed()),
            (200, _grabbed(url=OTHER, pipeline='busy', run='',
                           detail='已收下；其他同步正在运行')),
            (200, _grabbed(url=OTHER, added=0, duplicate=True,
                           detail='这条之前被「大V观点」收过了，还没写进知识库')),
        ]
        self.plan['states'] = {URL: [(200, _done())],
                               OTHER: [(200, _state(url=OTHER, kind='social')),
                                       (200, _done(url=OTHER, kind='social'))],
                               '*': [(200, _state())]}
        code, out, _ = self.run_cli('--json', URL, OTHER, timeout='3')
        self.assertEqual(code, 0, out)
        self.assertEqual([r['status'] for r in self.records(out)], ['saved', 'saved'])
        self.assertEqual(self.plan['runs'], 0, '恢复不该由命令行自己起一轮同步')
        self.assertEqual(self.plan['posted'].count(OTHER), 2, '那一条该被重投一次恢复')

    def test_the_automatic_renew_asks_only_to_restore(self):
        """**自动恢复那一次重投必须带上 `restore_only`。**

        `/api/grab` 的公开契约是「在就恢复、不在就抓一篇新的」,而「刚才那次 state
        查到它还在」不是写入那一刻它还在的保证。第 2 轮审查实测:等待期间历史清理
        把那一行删掉之后,这一次重投原地**重新抓了一遍** —— 一条清理掉的 `rss`
        文章被重建成 `inbox`、正文换成这次抓到的,而命令行照旧退出 0。

        带上它之后那条路径**在服务端压根不存在**(`known` 为空就 409,
        `tests/test_grab_store.py` 钉着那一半),不是靠「再查一次 state」赌窗口。
        **也不许带 `refetch`**:恢复补的是后半条链,不是把内容换掉。
        """
        self.plan['grabs'] = [(200, _grabbed(pipeline='busy', run='')),
                              (200, _grabbed(added=0, duplicate=True))]
        self.plan['states'] = {'*': [(200, _state()), (200, _done())]}
        code, out, _ = self.run_cli(URL, timeout='3')
        self.assertEqual(code, 0, out)
        posts = [body for m, path, body in self.plan['calls']
                 if m == 'POST' and path == '/api/grab']
        self.assertEqual(len(posts), 2, '那一条该被重投一次恢复')
        self.assertEqual((posts[0]['restore_only'], posts[0]['refetch']), (False, False),
                         '第一次投递是正常收录,不该自我收窄成「只恢复」')
        self.assertEqual((posts[1]['restore_only'], posts[1]['refetch']), (True, False),
                         '恢复那一次必须声明「只恢复、不许创建」,而且不替人重抓')

    def test_an_item_cleaned_up_while_waiting_is_never_called_written(self):
        """恢复撞上 409(那一行已经不在库里了)之后,**不许说成已写入知识库**。

        后果判据:那一条的终态是「库里没有这一行」,退出码非 0 —— 用户据此知道这
        一篇要重投,而不是去检索里找一篇不存在的文章。
        """
        self.plan['grabs'] = [(200, _grabbed(pipeline='busy', run='')),
                              (409, {'detail': '这一条已经不在库里了，没有可恢复的条目'})]
        self.plan['states'] = {'*': [(200, _state()),
                                     (200, {'ok': True, 'url': URL, 'known': False,
                                            'done': False, 'content_ok': False,
                                            'owing': ''})]}
        code, out, _ = self.run_cli('--json', URL, timeout='3')
        self.assertEqual(code, 1, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'failed', rec)
        self.assertIn('库里没有这一行', rec['detail'])
        self.assertNotIn('已写入知识库', out)

    def test_recovery_has_a_ceiling_even_when_answers_are_lost(self):
        """永远导不出去的那一条**不许变成一个不停起管道的循环**(那一轮带 extract、
        要调模型)。**预算在发请求之前就扣** —— 用「收到 200 没有」判断远端那次写
        发生没有是不可靠的:请求已经生效而回包丢了的那次照样起了一轮同步。
        """
        self.plan['grabs'] = [(200, _grabbed()), (500, 'boom')]
        code, out, _ = self.run_cli(URL, timeout='2')
        self.assertEqual(code, 0, out)
        self.assertIn('还没写进知识库', out)
        self.assertLessEqual(self.plan['posted'].count(URL), 1 + cli.MAX_RENEWS,
                             '回包丢了的那次也得消耗预算')

    def test_a_busy_pipeline_is_not_disturbed(self):
        """别人的那一轮正跑着:**不许去抢** —— 问不出来或正在跑都不补。"""
        self.plan['running'] = True
        self.run_cli(URL)
        self.assertEqual(self.plan['posted'], [URL])
        self.assertEqual(self.plan['runs'], 0)

    # ---------------------------------------------------------------- 通道

    def test_a_body_cut_midway_is_not_a_traceback(self):
        """**正文读到一半对方关连接**抛的是 `http.client.IncompleteRead`,不是
        OSError —— 漏掉它的后果实测过:异常一路穿透,终端上一段 traceback、stdout
        一个字都没有,**而且一次用于核实的 GET 都没发**,而服务端那会儿可能已经把
        这一行存下了。
        """
        self.plan['cut_body'] = True
        code, out, err = self.run_cli('--json', URL)
        self.assertEqual(code, 1, out + err)
        self.assertNotIn('Traceback', err)
        self.assertIn('读到一半断了', out, '那句话要人读得懂')
        [rec] = self.records(out)
        self.assertEqual((rec['status'], rec['url']), ('unknown', URL), rec)
        self.assertTrue(self.paths('GET'), '断在半路之后要去核实一次事实')

    def test_a_broken_channel_still_answers_for_every_input(self):
        """批量途中断线:**每一条输入都要有自己能对上的一行** —— 出问题那一条说
        「结果不确定」,后面几条说「还没投」。少了它们,批量调用方既不知道哪条已经
        投出去了、也不知道哪些压根没试过。
        """
        self.plan['grabs'] = [(500, 'boom')]
        self.plan['states'] = {'*': [(500, 'boom')]}
        code, out, err = self.run_cli('--json', URL, OTHER)
        self.assertEqual(code, 1, out + err)
        recs = self.records(out)
        self.assertEqual([r['url'] for r in recs], [URL, OTHER])
        self.assertEqual([r['status'] for r in recs], ['unknown', 'not_tried'])

    def test_local_requests_ignore_any_proxy_in_the_environment(self):
        """这台机器上常设 `*_proxy`,而 urllib 默认会把 `127.0.0.1` 也交给代理 ——
        拿回一个跟知识库毫无关系的 502,于是被说成「这条没收下」(实测过)。
        """
        for var in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
            # **恢复原值,不是一律删掉**:这台机器上这几个变量本来就设着,而测试
            # 之间共用一个进程 —— pop 掉之后后面哪个用例要联网(或要按代理解析某个
            # 地址)就会在**同跑时**红、单跑永远绿,归因能耗掉一整轮。
            self.addCleanup(os.environ.__setitem__ if var in os.environ
                            else os.environ.pop, var,
                            os.environ.get(var, None))
            os.environ[var] = 'http://127.0.0.1:1'
        self.plan['states'] = {'*': [(200, _done())]}
        code, out, err = self.run_cli('--json', URL)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.records(out)[0]['status'], 'saved')

    def test_the_stated_timeout_bounds_the_whole_wait(self):
        """说出去的那个秒数要是**上限**。超时是逐请求的,所以剩余预算必须传进每一
        次 state 查询 —— 不传的话 `--timeout 1` 会在一条慢 state 上等满单次上限
        (15 秒),一批很多条还会累加。

        **判据有两条,一条看表一条不看表**:总耗时要短于那条慢 state(不然「压短」
        只是说法);而压短之后那一次查询**必然超时**,于是档位停在投递回包那句话上,
        不是 state 的 `owing` —— 后者不依赖机器快慢。
        """
        import time
        self.plan['state_delay'] = 3        # 比修好之后的单次预算(约 1 秒)长
        started = time.monotonic()
        code, out, _ = self.run_cli(URL, timeout='1')
        self.assertEqual(code, 0, out)
        self.assertLess(time.monotonic() - started, 2.5, '单次请求没有被剩余预算压短')
        self.assertIn('正在写入知识库', out)
        self.assertNotIn('还没写成 vault 里那一篇', out, '那一次查询本该超时')

    def test_a_batch_of_slow_queries_does_not_each_get_its_own_wait(self):
        """预算是**整个等待阶段**的,不是每条各一份:一批 N 条慢查询不许继续执行
        N 次 —— 那样说出去的秒数会被乘以条数。所以每一次请求**之前**都要看预算,
        耗尽立即结束,而不是整批查完才看。
        """
        import time
        self.plan['state_delay'] = 3
        started = time.monotonic()
        code, out, _ = self.run_cli('--json', URL, OTHER, 'https://example.org/c',
                                    timeout='1')
        self.assertEqual(len(self.records(out)), 3, '三条输入要各有自己的一行')
        self.assertLess(time.monotonic() - started, 2.5, '后面几条又各等了一份预算')
        self.assertEqual(len(self.plan['asked']), 1,
                         '预算耗尽之后还在发查询:' + repr(self.plan['asked']))

    def test_the_pipeline_probe_is_inside_the_same_budget(self):
        """「管道在不在跑」那一问**也吃等待预算**。它不在预算里的话,`--timeout 1`
        之后还能再等满它自己那一份上限(15 秒);返回之后不再确认预算,还能接着发出
        一次恢复写请求 —— 那是**写**,而且带 extract 的那一轮要调模型。
        """
        import time
        self.plan['status_delay'] = 3
        started = time.monotonic()
        code, out, _ = self.run_cli(URL, timeout='1')
        self.assertEqual(code, 0, out)
        self.assertLess(time.monotonic() - started, 2.5, '那一问没被剩余预算压短')
        self.assertEqual(self.plan['posted'], [URL], '预算耗尽之后还发了一次恢复写')

    def test_links_are_picked_out_of_pasted_text_and_deduped(self):
        found = cli.links_in('看这篇 %s 挺好，还有（%s）以及 %s。' % (URL, OTHER, URL))
        self.assertEqual(found, [URL, OTHER])

    def test_prose_marks_never_turn_one_address_into_another(self):
        """抽取这一层是启发式,所以它的不变量是**宁可留下一个抓不到的坏地址,也不
        截短成另一个可能存在的目标**。左边那一列都是真实写法。
        """
        for text, want in (
                # 配对的括号属于地址:剥掉就换了一篇文章(两种括号各一条)
                (f'见 {PAREN} 这篇', [PAREN]),
                (f'见 {FULLWIDTH} 这篇', [FULLWIDTH]),
                # 落单的右括号 / 全角句读:行文从那里开始
                (f'（见 {URL}）以及别的', [URL]),
                (f'见 {URL}。后面还有话', [URL]),
                (f'见 {URL}，还有 {OTHER}。', [URL, OTHER]),
                # ASCII 的 `!` 是 URL 合法的子分隔符,不许当句末剥掉
                ('看 https://example.org/post! 这条', ['https://example.org/post!'])):
            with self.subTest(text=text):
                self.assertEqual(cli.links_in(text), want)


@requires_headless
class GrabCliLiveTests(unittest.TestCase):
    """真服务 + 真 CLI:**命令行这条路真能把一篇文章写进知识库吗**。

    上面那组钉的是「话挑对了档位」,这一条钉的是**盘上的后果** —— 命令行说
    「已写入知识库」而 vault 里什么都没有,是这条链最难看出来的一种谎报。沙箱
    只打掉「从网上取回那一页」这一步,正文提取、入库、导出、索引全是真的。
    """

    def run_cli(self, base, *argv) -> tuple[int, list[dict]]:
        done = subprocess.run([sys.executable, 'scripts/grab_cli.py', '--api', str(base),
                               '--json', *argv], cwd=ROOT, capture_output=True,
                              text=True, timeout=180)
        records = [json.loads(line) for line in done.stdout.strip().splitlines()
                   if line.strip()]
        return done.returncode, records

    def test_grabbing_from_the_command_line_lands_in_the_vault(self):
        url = 'https://example.org/posts/cli-one'
        with fixture_server('--grab-fixture') as base:
            code, [rec] = self.run_cli(base, url)
            self.assertEqual(code, 0, rec)
            self.assertEqual(rec['status'], 'saved', rec)
            self.assertTrue(rec['done'] and rec['indexed'], rec)
            # **盘上的后果**:导出真把它写成了 vault 里那一篇。
            root = Path(base.root) / 'vault' / 'source' / 'inbox'
            texts = [f.read_text() for f in sorted(root.glob('*.md'))]
            mine = [t for t in texts if f'url: {url}' in t]
            self.assertEqual(len(mine), 1, f'该有且只有一篇，实际 {len(mine)}')
            self.assertIn('把链接存进知识库', mine[0])
            # 再投一次:幂等,不多出第二篇(`put_items` 的 DO NOTHING)。
            code, [again] = self.run_cli(base, url)
            self.assertEqual(code, 0, again)
            self.assertIn(again['status'], ('duplicate', 'saved'), again)
            texts = [f.read_text() for f in sorted(root.glob('*.md'))]
            self.assertEqual(sum(f'url: {url}' in t for t in texts), 1, '多出了一篇')
            # 只读那一问答得出同一个事实。
            code, [state] = self.run_cli(base, '--state', url)
            self.assertEqual((code, state['status']), (0, 'saved'), state)


if __name__ == '__main__':
    unittest.main()
