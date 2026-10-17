# -*- coding: utf-8 -*-
"""手动收录的命令行入口(`scripts/grab_cli.py`,即 `./kb.sh grab` 和 skill
`kb-grab` 走的那条路)。

**判据一律落在「终端上那行字 + 退出码 + 真发出去的那几个请求」上**,不验中间
标志 —— 这一层唯一的职责就是把服务端的事实说成一句不谎报的话,而它最容易出的
错恰恰是「话说对了档位却挑错」:把「存下了、还搜不到」说成「已写入知识库」,
用户去检索里找不到,再投一次只会得到同一句「已经收过了」,后半条链永远没人补。

**用一个在进程内跑的假 kb-web,而不是打桩那几个函数**:这样顺带钉住线上协议
(打哪个路径、什么方法、payload 里有没有 refetch),也让「本机请求不许走代理」
那一条验得到真后果 —— 打桩掉 HTTP 的话,那条回归就只剩一句结构断言。

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

from tests.headless import fixture_server, requires_headless

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('grab_cli', ROOT / 'scripts' / 'grab_cli.py')
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

URL = 'https://example.org/posts/hello'
OTHER = 'https://example.org/posts/second'


def _state(**over) -> dict:
    """`/api/grab/state` 的一份回包。默认:收下了、内容是正文、还没走完后半条链。"""
    base = {'ok': True, 'url': URL, 'known': True, 'done': False, 'content_ok': True,
            'kind': 'inbox', 'title': '把链接存进知识库', 'exported': False,
            'stored': False, 'indexed': False, 'owing': '还没写成 vault 里那一篇',
            'resume': '，稍后在「手动收录」里点一次「写入知识库」'}
    return {**base, **over}


def _grabbed(**over) -> dict:
    """`/api/grab` 成功收下并起了一轮的回包。"""
    base = {'ok': True, 'added': 1, 'duplicate': False, 'done': False,
            'content_ok': True, 'url': URL, 'title': '把链接存进知识库',
            'kind': 'inbox', 'pipeline': 'started', 'run': 'run-1',
            'detail': '已收下，正在写入知识库'}
    return {**base, **over}


class _Handler(BaseHTTPRequestHandler):
    """假 kb-web。**只认我们真的会打的那几条路径**,别的一律 404 —— 多打一条
    路径的改动要当场红,而不是静静多发一个请求。"""

    def log_message(self, *a):          # 不要往测试输出里刷访问日志
        pass

    def _reply(self, code: int, payload):
        body = (json.dumps(payload) if not isinstance(payload, str) else payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        plan = self.server.plan
        plan['calls'].append(('GET', self.path, None))
        if self.path.startswith('/api/grab/state'):
            queue = plan['states']
            return self._reply(*(queue.pop(0) if len(queue) > 1 else queue[0]))
        if self.path.startswith('/api/pipeline/status'):
            return self._reply(200, {'running': plan['running']})
        self._reply(404, {'detail': '没有这条路径'})

    def do_POST(self):
        plan = self.server.plan
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        plan['calls'].append(('POST', self.path, json.loads(raw or b'{}')))
        if self.path == '/api/grab':
            queue = plan['grabs']
            return self._reply(*(queue.pop(0) if len(queue) > 1 else queue[0]))
        if self.path == '/api/pipeline/run':
            plan['kicks'] += 1
            return self._reply(200, {'ok': True})
        self._reply(404, {'detail': '没有这条路径'})


class GrabCliTests(unittest.TestCase):
    def setUp(self):
        # 轮询间隔压到几乎为零:用例要验的是「收敛到哪一档」,不是真等那几秒。
        for name, value in (('POLL_SECONDS', 0.01), ('MAX_KICKS', 2)):
            self.addCleanup(setattr, cli, name, getattr(cli, name))
            setattr(cli, name, value)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.plan = {'grabs': [(200, _grabbed())], 'states': [(200, _state())],
                            'running': False, 'kicks': 0, 'calls': []}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api = 'http://127.0.0.1:%d' % self.server.server_address[1]

    # ---------------------------------------------------------------- 工具

    @property
    def plan(self):
        return self.server.plan

    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['--api', self.api, '--timeout', '0.3', *argv])
        return code, out.getvalue(), err.getvalue()

    def records(self, text: str) -> list[dict]:
        return [json.loads(line) for line in text.strip().splitlines() if line.strip()]

    def paths(self, method: str) -> list[str]:
        return [p for m, p, _ in self.plan['calls'] if m == method]

    # ---------------------------------------------------------------- 各档

    def test_a_finished_link_is_reported_as_in_the_knowledge_base(self):
        self.plan['states'] = [(200, _state(done=True, exported=True, stored=True,
                                            indexed=True, owing=''))]
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
        self.plan['states'] = [(200, _state(content_ok=False,
                                            owing='收下的那一份看不出是正文'))]
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
        self.plan['states'] = [(200, _state(done=True, owing=''))]
        code, out, err = self.run_cli('--json', URL)
        self.assertEqual(code, 0, out + err)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'saved', rec)
        self.assertNotIn('没收下', out)

    def test_a_server_fault_with_no_answer_stays_uncertain(self):
        """问都问不出来时**不许替它下结论**:既不说存下了,也不说没存下。"""
        self.plan['grabs'] = [(500, 'boom')]
        self.plan['states'] = [(500, 'boom')]
        code, out, err = self.run_cli(URL)
        self.assertEqual(code, 1, out)
        self.assertNotIn('已写入知识库', out)
        self.assertNotIn('没收下', out)
        self.assertIn('打不通 kb-web', err)

    def test_a_link_that_already_finished_costs_no_extra_poll(self):
        """幂等那一档(后端没起管道,因为它早走完了):一次状态都不必问。"""
        self.plan['grabs'] = [(200, {'ok': True, 'added': 0, 'duplicate': True,
                                     'done': True, 'content_ok': True, 'url': URL,
                                     'title': '旧文', 'kind': 'inbox',
                                     'detail': '这条已经在知识库里了（手动收录）'})]
        code, out, _ = self.run_cli(URL)
        self.assertEqual(code, 0, out)
        self.assertIn('早就在知识库里了', out)
        self.assertEqual(self.paths('GET'), [])

    def test_a_successful_refetch_is_followed_through_not_called_bad(self):
        """**重抓换成了 ≠ 内容不对。** 后端那一档(`_recover` 的成功分支)只更新
        `replaced` / `done` / `title`,回包里的 `content_ok` 仍是**库里旧那一份**的
        判断 —— 照它先判就把一次成功的替换说成「看不出是正文」,而且**不去等它写进
        知识库**,用户那一次重抓于是永远停在「没换成」。
        """
        self.plan['grabs'] = [(200, _grabbed(
            added=0, duplicate=True, replaced=True, content_ok=False,
            detail='你要求重新抓一次，已经按这次重抓到的内容换掉，正在写入知识库'))]
        self.plan['states'] = [(200, _state(done=True, exported=True, stored=True,
                                            indexed=True, owing=''))]
        code, out, _ = self.run_cli('--json', '--refetch', URL)
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'saved', rec)
        self.assertIn('换掉', rec['detail'])
        self.assertTrue(self.paths('GET'), '换成了之后要去核这一篇写进去了没有')

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
        self.plan['states'] = [(200, _state(done=True, owing=''))]
        code, out, _ = self.run_cli('--json', '--state', URL)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.paths('POST'), [])
        self.assertEqual(self.records(out)[0]['status'], 'saved')

    def test_state_only_reports_an_unseen_link_as_absent_not_failed(self):
        self.plan['states'] = [(200, {'ok': True, 'url': URL, 'known': False,
                                      'done': False, 'content_ok': False, 'owing': ''})]
        code, out, _ = self.run_cli('--json', '--state', URL)
        self.assertEqual(code, 0, out)        # 「没收过」是一个正常答案,不是故障
        self.assertEqual(self.records(out)[0]['status'], 'absent')

    def test_state_only_turns_a_rejected_address_into_a_sentence(self):
        """一批地址里混进一行脏数据是常事:4xx 要变成一句话,不是 traceback。"""
        self.plan['states'] = [(400, {'detail': '这不是一条链接'})]
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

    # ---------------------------------------------------------------- 批量

    def test_a_batch_kicks_one_more_round_for_the_links_that_missed_it(self):
        """批量投递时后面几条必然撞上前一条起的那一轮(后端如实回 `busy`),
        它们的内容在库里却没人替它们跑 export —— 收尾要补一轮,否则这几条会
        一直停在「存下了、搜不到」。
        """
        self.plan['grabs'] = [(200, _grabbed()),
                              (200, _grabbed(url=OTHER, pipeline='busy', run='',
                                             detail='已收下；其他同步正在运行'))]
        self.plan['states'] = [(200, _state()), (200, _state()),
                               (200, _state(done=True, owing=''))]
        code, out, _ = self.run_cli('--json', URL, OTHER)
        self.assertEqual(code, 0, out)
        self.assertEqual([r['status'] for r in self.records(out)], ['saved', 'saved'])
        kicked = [body for m, p, body in self.plan['calls']
                  if m == 'POST' and p == '/api/pipeline/run']
        self.assertTrue(kicked, '该补的那一轮没起')
        self.assertEqual(kicked[0]['channel'], 'inbox', '补的那一轮不许扫别的来源')
        self.assertEqual(kicked[0]['stages'], ['export', 'index', 'extract'])

    def test_kicking_has_a_ceiling(self):
        """永远导不出去的那一条**不许变成一个不停起管道的循环**(那一轮带
        extract,要调模型)。
        """
        code, out, _ = self.run_cli(URL)
        self.assertEqual(code, 0, out)
        self.assertIn('还没写进知识库', out)
        self.assertLessEqual(self.plan['kicks'], cli.MAX_KICKS)

    def test_a_busy_pipeline_is_not_kicked(self):
        """别人的那一轮正跑着:**不许去抢** —— 问不出来或正在跑都不起。"""
        self.plan['running'] = True
        self.run_cli(URL)
        self.assertEqual(self.plan['kicks'], 0)

    # ---------------------------------------------------------------- 通道

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
        self.plan['states'] = [(200, _state(done=True, owing=''))]
        code, out, err = self.run_cli('--json', URL)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.records(out)[0]['status'], 'saved')

    def test_links_are_picked_out_of_pasted_text_and_deduped(self):
        found = cli.links_in('看这篇 %s 挺好，还有（%s）以及 %s。' % (URL, OTHER, URL))
        self.assertEqual(found, [URL, OTHER])


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
