# -*- coding: utf-8 -*-
"""命令行那条入口的**通道层**:打接口这件事本身出岔子时,那一行结果还对不对。

对应实现 `scripts/grab_http.py`(两类失败怎么分、五个端点怎么打)。从
`tests/test_grab_cli.py` 拆出来的 —— 那一份加完第 5 轮的用例过了 700 行的上限,
而这一组的判据自成一类:**它验的不是「这一篇是什么状态」,是「连接坏掉的时候
我们说了什么」**。

两条不变量贯穿这一组:
- **回包没送达 ≠ 后端没存下来**(保存排在起管道之前)。所以 5xx、断线、读到一半
  断掉都不许说成「这条没收下」—— 去问一次只读的事实,问不到才停在「结果不确定」。
- **说出去的那个秒数是**轮询**的上限**。等待阶段的每一次请求(含「管道在不在跑」
  那一问)都吃同一份预算,否则一批慢查询会各等满自己那一份。(它管得到的是「还发不发
  下一个请求」;一个已经发出的请求读多久由 socket 超时管,那是两次收包之间的空闲上限
  —— 对外的措辞一律按前者。)

夹具(进程内那个假 kb-web)在 `tests/grab_cli_fixture.py`;收敛那一组在
`tests/test_grab_cli.py`、输入解析在 `tests/test_grab_cli_input.py`。
"""
from __future__ import annotations

import os
import unittest

from tests.grab_cli_fixture import (OTHER, URL, GrabCliCase, _done, _grabbed,
                                    _state)


class GrabCliChannelTests(GrabCliCase):
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

    def test_a_reply_that_is_not_this_services_answer_is_not_a_result(self):
        """**语法有效 ≠ 接口有效。** `--api` 指错地方(反代插了一页、另一个应用、
        异常网关)时回来的可能是一段合法 JSON,而它没有我们要读的那几个键 ——
        `.get()` 一路拿到 `None`,每一档都读得下去。实测形态:回 `[]` / `null` 时
        CLI 当场抛 `AttributeError`,同一批**第二条压根没被处理**;回 `{"ok": false}`
        时两条都被说成「早就在知识库里了」而 `done=false`,一次核实都没发生。

        所以判不准一律归到通道那一档(如实停在「结果不确定」),而不是在各个调用点
        补 `.get` 兜底 —— 那是同一个问题在四处各判一遍。正常回包照旧放行由这一组
        和收敛那一组的其余用例钉着。
        """
        for payload in ([], None, {'ok': False, 'detail': '我不是 kb-web'}, '"hi"'):
            with self.subTest(payload=payload):
                self.plan['grabs'] = [(200, payload)]
                self.plan['states'] = {'*': [(200, payload)]}
                code, out, err = self.run_cli('--json', URL, OTHER)
                self.assertEqual(code, 1, out + err)
                self.assertNotIn('Traceback', err)
                recs = self.records(out)
                # **一条输入一行,失败路径上也成立**:第一条崩在半路会让第二条连
                # 一行都没有(实测过)。
                self.assertEqual([r['url'] for r in recs], [URL, OTHER], out)
                self.assertEqual([r['status'] for r in recs],
                                 ['unknown', 'not_tried'], out)

    def test_a_reply_missing_the_load_bearing_fields_is_not_a_result(self):
        """**「是个对象、而且 ok=true」还不够:下结论要的那几个键必须在,类型还得对。**

        实测形态:`{"ok": true}` 和 `{"ok": true, "duplicate": false, "done": false}`
        让两条输入都被说成「早就在知识库里了」而 `done=false`,一次核实都没发生;
        state 回 `{"ok": true, "known": true}` 被说成「看不出是正文」,
        `{"ok": true, "known": null}` 被说成「还没收进来」—— 后者是**「确定没有」**
        这个结论,照它重投会原地再抓一遍。`added: true` 这种类型顶替也算(布尔不是
        一个计数),`isinstance` 判不出来。
        """
        for payload in ({'ok': True},
                        {'ok': True, 'duplicate': False, 'done': False},
                        {'ok': True, 'added': True, 'duplicate': True, 'done': True,
                         'content_ok': True, 'url': URL},
                        {'ok': True, 'added': 0, 'duplicate': True, 'done': True,
                         'content_ok': True, 'url': URL, 'replaced': 'false'}):
            with self.subTest(grab=payload):
                self.plan['grabs'] = [(200, payload)]
                self.plan['states'] = {'*': [(200, payload)]}
                code, out, err = self.run_cli('--json', URL, OTHER)
                self.assertEqual(code, 1, out + err)
                self.assertNotIn('Traceback', err)
                recs = self.records(out)
                self.assertEqual([r['url'] for r in recs], [URL, OTHER], out)
                self.assertEqual([r['status'] for r in recs],
                                 ['unknown', 'not_tried'], out)
        for payload in ({'ok': True, 'known': True},
                        {'ok': True, 'known': None},
                        {'ok': True, 'url': URL, 'known': True, 'done': 1,
                         'content_ok': True}):
            with self.subTest(state=payload):
                self.plan['states'] = {'*': [(200, payload)]}
                code, out, err = self.run_cli('--json', '--state', URL)
                self.assertEqual(code, 1, out + err)
                self.assertNotIn('Traceback', err)
                [rec] = self.records(out)
                # **不许落在「还没收进来」或者「看不出是正文」上** —— 那两句都是
                # 替一个答不出这件事的回包发布结论。
                self.assertEqual(rec['status'], 'unknown', rec)

    def test_a_grab_reply_without_positive_evidence_is_not_called_duplicate(self):
        """**「早就在知识库里了」要正面证据**:`duplicate` 和 `done` 都为真。

        这一条字段齐、类型也对,但既没起管道、也没说自己走完整条链。按「少了
        pipeline 这个字段就是幂等」推的话它会被说成已完成而 `done=false`,用户去
        检索里找不到,再投一次还是同一句话。现在它不下结论,由只读那一问定档 ——
        反过来那一边(真的幂等回包仍报 `duplicate`)在收敛那一组钉着。
        """
        self.plan['grabs'] = [(200, _grabbed(added=0, duplicate=False, done=False,
                                             pipeline=None, detail='说不清'))]
        self.plan['states'] = {'*': [(200, _state())]}
        code, out, err = self.run_cli('--json', URL, timeout='3')
        self.assertEqual(code, 0, out + err)
        [rec] = self.records(out)
        self.assertNotEqual(rec['status'], 'duplicate', rec)
        self.assertEqual(rec['status'], 'pending', rec)

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


if __name__ == '__main__':
    unittest.main()
