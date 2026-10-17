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

import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.grab_cli_fixture import (OTHER, ROOT, URL, GrabCliCase, _absent, _done,
                                    _grabbed, _state, cli)
from tests.headless import fixture_server, requires_headless


class GrabCliTests(GrabCliCase):
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

    def test_an_empty_snapshot_after_a_lost_answer_is_not_a_dropped_link(self):
        """**「这一刻库里没有」证明不了「那次投递不会写入」。**

        实测的时序:后端已经进入取页,投递那条连接断掉(动作还在跑),
        此刻只读那一问答 `known=false` —— 原先据此输出 `failed`「没收下，再投一次」
        并退出 1,而那次动作随后**真的保存成功**、真实导出在 vault 里生成了 1 篇。
        skill 对 `failed` 的承诺是「一个字节都没写」,所以那是谎报;照它重投还会
        多抓一次(重抓那一档更会动库里那一份)。

        所以这一档**不是终态**:在预算内接着核实,事实出来了就按事实说。
        """
        self.plan['grabs'] = [(500, 'boom')]            # 没拿到明确回答
        self.plan['states'] = {'*': [(200, _absent()), (200, _done())]}
        code, out, _ = self.run_cli('--json', URL, timeout='3')
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'saved', rec)
        self.assertNotIn('没收下', out)
        self.assertNotIn('这条没存下来', out)
        # **后面那几轮查到的标题也要补上**(N2):回包丢了的那一条,标题只有只读
        # 这一问答得出来 —— 第一轮答的是「库里还没有」,所以这一条钉的是等待循环
        # 里那一处,不是初次核实那一处。
        self.assertEqual(rec['title'], '把链接存进知识库')

    def test_an_unconfirmed_write_does_not_trust_the_old_bad_snapshot(self):
        """**没拿到回包的那一次,库里那一份旧坏内容不许终结它。**

        实测的时序:后端已经进入取页,投递那条连接断掉(动作还在跑),
        此刻只读那一问答的是**动作之前**那一版(`content_ok=false`)—— 原先据此
        当场落 `bad_content`「收下的那一份看不出是正文」、劝一句「加 --refetch 会
        重抓一次」并以 0 退出(实测 0.047 秒,而预算给的是 2 秒);CLI 返回之后原
        动作才走完,真后端 `replaced=true`、新正文 `content_ok` 为真、真实导出
        生成了 1 篇。

        **这一档于是不是终态**:预算内接着核实,换好了就按换好了说。它跟
        `--refetch` 无关 —— 普通重复提交撞上坏内容时后端也会进受控重抓。
        """
        self.plan['grabs'] = [(500, 'boom')]            # 没拿到明确回答
        self.plan['states'] = {'*': [(200, _state(content_ok=False)),
                                     (200, _done(content_ok=True))]}
        code, out, _ = self.run_cli('--json', URL, timeout='3')
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'saved', rec)
        self.assertNotIn('看不出是正文', out)
        self.assertNotIn('--refetch', out, '不许劝人去重抓一份正在被换掉的内容')

    def test_an_unconfirmed_refetch_that_stays_bad_ends_uncertain_not_bad(self):
        """**反过来钉一边:预算内一直没变好,也只能停在「不确定」。**

        等不到不许改口说「确定是坏内容」—— 那句话的下一步是再重抓一次,而原动作
        可能正在换。少了它,把这一档直接按旧快照落 `bad_content` 照旧绿。
        """
        self.plan['grabs'] = [(500, 'boom')]
        self.plan['states'] = {'*': [(200, _state(content_ok=False))]}
        code, out, _ = self.run_cli('--json', '--refetch', URL, timeout='1')
        self.assertEqual(code, 1, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'unknown', rec)
        self.assertEqual(rec['label'], '结果不确定')
        self.assertIn('别急着重投', rec['detail'])
        # 那句话**可以**如实说「库里那一份看着还不是正文」(它就是现状),但不许把
        # 结论下成「这条是坏内容、加 --refetch 再来一次」—— 后者是 `bad_content`
        # 那一档的话,而这一刻原动作可能正在把它换掉。
        self.assertNotIn('加 --refetch', rec['detail'])

    def test_an_unconfirmed_write_that_never_shows_up_stays_uncertain(self):
        """**预算到期也不许改口说「确定没存下」。**

        等满之后库里还是没有这一行:那依然只说明我们没看见它 —— 停在「不确定」、
        退出码非 0,那句话里要说清「可能还在抓,别急着重投」。
        """
        self.plan['grabs'] = [(500, 'boom')]
        self.plan['states'] = {'*': [(200, _absent())]}
        code, out, _ = self.run_cli('--json', URL, timeout='0.6')
        self.assertEqual(code, 1, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'unknown', rec)
        self.assertIn('可能还在抓', rec['detail'])
        self.assertNotIn('没存下来', rec['detail'])
        # 没拿到回答的那一次**不许借恢复那条路去补**:那条路要的是「库里有这一行」。
        self.assertEqual(self.plan['restored'], [])

    def test_a_confirmed_write_keeps_its_facts_even_if_the_answer_was_lost(self):
        """回包丢了而事实查得到时,**附带的那几个字段也要如实**(N2):

        标题只有只读那一问答得出来(别让一条确认存下的结果带着空标题交给调用方);
        而「这次新建了几条」**没有证据**,那时给 `null` —— 编码成 `0` 是在断言一件
        我们不知道的事(那次投递可能真的新建了一条)。
        """
        self.plan['grabs'] = [(500, 'boom')]
        self.plan['states'] = {'*': [(200, _done())]}
        code, out, _ = self.run_cli('--json', URL)
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual((rec['status'], rec['title']), ('saved', '把链接存进知识库'))
        self.assertIsNone(rec['added'], '缺证据不是 0')

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
        """没换成那一档**按库里那一份自己的质量分两种说法**。

        这一条库里那一份是正文(`content_ok=true`),只是这次重抓撞上了关卡 ——
        说成「收下的那一份看不出是正文」是把一次无害的空转报成质量问题(实测 N1);库里那一份一个字节没坏,所以也不是失败。

        **这一档还要库里那一篇已经走完整条链**(`_done()`):`unchanged` 说的是
        「内容没动、而且那一篇搜得到」—— 还欠导出/索引的那一种是下一条用例。
        """
        self.plan['grabs'] = [(200, _grabbed(
            added=0, duplicate=True, replaced=False, pipeline=None,
            detail='这次重抓还是不行:这一页挂着验证'))]
        self.plan['states'] = {'*': [(200, _done())]}
        code, out, _ = self.run_cli('--json', '--refetch', URL)
        self.assertEqual(code, 0, out)
        # **断在档位上,不是断在那句话上**:只验「那句话里出现了某几个字」时,
        # 把这一档说成「早就在知识库里了」照旧绿 —— 而那正是谎报的形状。
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'unchanged', rec)
        self.assertEqual(rec['label'], '库里那一份没动')
        self.assertIn('没换成', rec['detail'])
        # **后端那句 detail 不转述**:它是按**请求开始那一刻**的质量写的
        # (「这一页挂着验证」),而这一档的结论要按只读那一问核到的当前事实说。
        self.assertNotIn('已写入知识库', out)
        self.assertTrue(rec['content_ok'], rec)

    def test_a_refetch_that_did_not_land_still_owes_the_second_half(self):
        """**「这次有没有换正文」和「这一篇搜不搜得到」是两个维度。**

        库里那一份是好正文、但还没写成 vault 里那一篇,这时要求重抓而原文没变:
        后端如实回 `replaced=false`(那是**动作**那一问的回答),而那一篇照旧搜
        不到。把它当成交付终态的后果实测过(实测,真端点 + 临时库:终端说
        一句「库里那一份没动」就收口,盘上一个 Markdown 文件都没有、恢复请求 0 次
        —— 不是「等完还没索引」,是压根没人去起那一步)。所以这一条要**继续报欠
        账**,并且走只恢复、不创建那条路把后半条链补上。
        """
        self.plan['grabs'] = [(200, _grabbed(
            added=0, duplicate=True, replaced=False, pipeline=None,
            detail='这次重抓到的内容和它一样'))]
        code, out, _ = self.run_cli('--json', '--refetch', URL, timeout='3')
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        # **断在档位和那条补写请求上**:只验那句话的话,把这一档说成「库里那一份
        # 没动」照旧绿 —— 而那正是「动作回答冒充交付结果」的形状。
        self.assertEqual(rec['status'], 'pending', rec)
        self.assertIn('还没写成 vault 里那一篇', rec['detail'])
        self.assertIn('没换成', rec['detail'])   # 动作那个事实一样要留着
        # 打的是 `/api/grab/restore` 这条**不创建内容**的独立路径,不是再投一次。
        # 恰好两次 = `MAX_RENEWS`:这一条在夹具里永远补不上(state 恒为欠账),
        # 所以这里顺带钉住「不许一直补下去」那道上限。
        self.assertEqual(self.plan['restored'], [URL, URL], self.plan)
        self.assertEqual(self.plan['posted'], [URL], self.plan)

    def test_a_refetch_that_found_bad_content_still_says_so(self):
        """**反过来钉一边:库里那一份真的不是正文时,那一档照旧说得出口。**

        少了它,把「没换成」一律说成「库里那一份没动」也全绿 —— 而那会把一篇
        错存的坏内容说得像没事,用户不会再 `--refetch` 一次。
        """
        self.plan['grabs'] = [(200, _grabbed(
            added=0, duplicate=True, replaced=False, content_ok=False, pipeline=None,
            detail='库里那一份看不出是正文；这次重抓到的还是那张墙'))]
        # **库里那一份此刻确实不是正文** —— 这一档现在要 `state` 作证(回包里的
        # `content_ok` 说的是请求开始那一刻),所以只读那一问也得这么说。
        self.plan['states'] = {'*': [(200, _state(content_ok=False))]}
        code, out, _ = self.run_cli('--json', '--refetch', URL)
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'bad_content', rec)
        self.assertIn('看不出是正文', rec['detail'])

    def test_a_failed_swap_is_not_bad_content_when_the_file_is_already_good(self):
        """**CAS 没换成的典型成因是别人在这中间刚换好** —— 那时回包里的
        `content_ok` 带的是人家换**之前**的旧判断。

        后果判据:库里那一份已经是正文了,这一档不许报 `bad_content`、不许再劝一次
        `--refetch`(那会让人去重抓一份已经好了的正文,而重抓要动库里那一份)。
        少了它,拿回包那个旧字段先判也全绿。
        """
        self.plan['grabs'] = [(200, _grabbed(
            added=0, duplicate=True, replaced=False, content_ok=False, pipeline=None,
            detail='库里那一份看不出是正文；这次重抓到的还是那张墙'))]
        # 只读那一问是**动作之后**发的:它看到的是别人换好之后的样子。
        self.plan['states'] = {'*': [(200, _done(content_ok=True))]}
        code, out, _ = self.run_cli('--json', '--refetch', URL)
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'unchanged', rec)
        self.assertNotIn('看不出是正文', rec['detail'])
        self.assertNotIn('--refetch', rec['detail'])
        self.assertTrue(rec['content_ok'], rec)

    def test_a_file_cleaned_up_mid_refetch_is_not_called_unchanged(self):
        """**「这一行还在不在」要排在所有质量/替换的结论之前。**

        实测过的形态:重抓等待期间同一篇被清理掉,终端照旧说「库里那一份
        没动」—— 那是在描述一篇**已经不存在**的文章。没换成是真的,但库里那一份
        此刻压根不在了,这两句话里只有后一句是用户要知道的。
        """
        self.plan['grabs'] = [(200, _grabbed(
            added=0, duplicate=True, replaced=False, content_ok=False, pipeline=None,
            detail='这次重抓还是不行:这一页挂着验证'))]
        self.plan['states'] = {'*': [(200, _absent())]}
        code, out, _ = self.run_cli('--json', '--refetch', URL, timeout='3')
        self.assertEqual(code, 1, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'gone', rec)
        self.assertNotIn('没动', rec['detail'])
        self.assertNotIn('看不出是正文', rec['detail'])
        # 后端确认过它当时在库里(`replaced` 这个键在),所以不是「一个字节都没写」。
        self.assertNotIn('没存下来', rec['detail'])

    def test_an_early_return_publishes_no_stale_quality_verdict(self):
        """**提前收口的那条路也不许发布旧的质量结论。**

        `--no-wait` 不等、也就没核过库里此刻的样子,而回包里那个 `content_ok` 是
        **请求开始那一刻**的 —— CAS 没换成的典型成因正是别人刚换好。这一档于是
        停在「还没核到」,而不是说「看不出是正文、加 --refetch 再来一次」。

        等待那条路上的同一判据在 `test_a_failed_swap_is_not_bad_content_when_the_file_is_already_good`
        —— 两条路各有自己的红(元规则「两份实现各有自己的回归」)。
        """
        self.plan['grabs'] = [(200, _grabbed(
            added=0, duplicate=True, replaced=False, content_ok=False, pipeline=None,
            detail='库里那一份看不出是正文；这次重抓到的还是那张墙'))]
        code, out, _ = self.run_cli('--json', '--no-wait', '--refetch', URL)
        self.assertEqual(code, 1, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'unknown', rec)
        self.assertNotIn('看不出是正文', rec['detail'])
        self.assertNotIn('加 --refetch', rec['detail'])
        self.assertEqual(self.plan['asked'], [], '--no-wait 不许去查状态')

    def test_a_budget_that_runs_out_without_evidence_stays_uncertain(self):
        """**预算耗尽、一次状态都没查成,也不许回头用回包里那个旧结论收口。**

        实测的第四组:另一个写入者已经把正文换好了,而这一次 CAS 没换成;
        状态查询一路 500、0.2 秒预算耗尽 —— 当时仍输出 `bad_content` 并退出 0,
        等于把一份**已经好了**的正文说成坏的,还劝人再抓一次。

        等不到就留在不确定,这是这一类的最后一个出口(别的三个是提前收口、无需
        等待、查询失败)。
        """
        self.plan['grabs'] = [(200, _grabbed(
            added=0, duplicate=True, replaced=False, content_ok=False, pipeline=None,
            detail='库里那一份看不出是正文；这次重抓到的还是那张墙'))]
        self.plan['states'] = {'*': [(500, 'boom')]}
        code, out, _ = self.run_cli('--json', '--refetch', URL, timeout='0.2')
        self.assertEqual(code, 1, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'unknown', rec)
        self.assertNotIn('看不出是正文', rec['detail'])
        self.assertNotIn('加 --refetch', rec['detail'])
        # **「这次没换成」这个动作事实可以留着**(它由回包确认过),不许留的是
        # 「当前正文仍坏」那半句 —— 后者没有任何当前证据。
        self.assertIn('没换成', rec['detail'])

    # ---------------------------------------------------------------- 只读 / 参数

    def test_a_malformed_api_address_is_an_argument_error(self):
        """**`--api` 给错了是参数错,不是「投出去了而结果未知」。**

        `urllib.request.Request('not-a-url/api/grab')` 抛的 `ValueError` 发生在
        通道层那套归一化**之前**,会一路穿透到终端:实测 stdout 一个字都没有、
        stderr 是一段 traceback —— 而「一条输入一行结果」是这条入口的契约。

        后果判据:退出 2(参数错那一档)、stderr 有一句人读得懂的话、**一个请求
        都没发**。
        """
        # `http://[::1` 这种不完整的 IPv6 是 `urlsplit` 自己抛 ValueError 的那一档
        # (实测),和上面几种走同一个出口:一句话,不是 traceback。
        for bad in ('not-a-url', 'ftp://h/x', '127.0.0.1:8787', 'http://[::1'):
            with self.subTest(api=bad):
                code, out, err = self.run_cli('--json', '--api', bad, URL)
                self.assertEqual(code, 2, (out, err))
                self.assertEqual(out.strip(), '', out)
                self.assertIn('--api', err)
                self.assertNotIn('Traceback', err)
        self.assertEqual(self.plan['posted'], [], '参数错那一档不许发请求')
        self.assertEqual(self.plan['asked'], [])

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
        self.assertEqual(self.plan['restored'], [OTHER], '那一条该被求一次恢复')
        self.assertEqual(self.plan['posted'].count(OTHER), 1, '恢复不走可创建那条路')

    def test_the_automatic_renew_goes_down_a_restore_only_path(self):
        """**自动恢复走的是 `/api/grab/restore` 这条独立路径,不是 `/api/grab`。**

        `/api/grab` 的公开契约是「在就恢复、**不在就抓一篇新的**」,而「刚才那次
        state 查到它还在」不是写入那一刻它还在的保证:等待期间历史清理把那一行删掉
        之后,那次重投会原地重新抓一遍,把一条清理掉的 `rss` 文章重建成 `inbox`
        (实测)。

        **收窄这件事必须在协议边界上成立。** 布尔字段那一版只在同版服务上对:
        `kb.sh grab` 每次跑磁盘上的新 CLI,而撞见已经跑着的服务就直接用它 ——
        那个没重启的进程用旧 `GrabRequest`,Pydantic **忽略**不认识的键,于是约束
        静默消失(实测拿 `git show 5b70036:kb/web/grab_api.py` 的真模块实测:
        实发 1 次取页、把清理掉的条目重建成 `inbox`,而命令行退出 0)。独立路径在那种
        服务上回 404,这边据此停下来。

        这里断三件事:打的是那条独立路径、**payload 里只有 url**(不带 `refetch`
        —— 恢复补的是后半条链,不是把内容换掉)、以及可创建那条路没被用来恢复。
        """
        self.plan['grabs'] = [(200, _grabbed(pipeline='busy', run=''))]
        self.plan['states'] = {'*': [(200, _state()), (200, _done())]}
        code, out, _ = self.run_cli(URL, timeout='3')
        self.assertEqual(code, 0, out)
        posts = [(path, body) for m, path, body in self.plan['calls'] if m == 'POST']
        self.assertEqual([path for path, _ in posts],
                         ['/api/grab', '/api/grab/restore'])
        self.assertEqual(posts[1][1], {'url': URL}, '恢复那一次只许给出地址')
        self.assertEqual(self.plan['posted'], [URL], '恢复不许走可创建那条路')

    def test_a_server_too_old_to_restore_is_not_downgraded(self):
        """**跑着的服务没有这条路(404)时到此为止 —— 不换回那条会创建内容的路。**

        这是「更新了文件却没重启」那一刻的形态:库里那一份一个字节没动,所以档位
        照旧是「已存下、还没写进知识库」、退出码 0;但那句话里要说清下一步是重启,
        不然这几条只是「还没写进知识库」,看不出原因。**可创建那条路一次都不许再打**
        (那正是会把清理掉的条目重建出来的动作)。
        """
        self.plan['grabs'] = [(200, _grabbed(pipeline='busy', run=''))]
        self.plan['restores'] = [(404, {'detail': 'Not Found'})]
        self.plan['states'] = {'*': [(200, _state())]}
        code, out, _ = self.run_cli('--json', URL, timeout='1.5')
        self.assertEqual(code, 0, out)
        [rec] = self.records(out)
        self.assertEqual(rec['status'], 'pending', rec)
        self.assertIn('比代码旧', rec['detail'])
        self.assertIn('restart', rec['detail'])
        self.assertEqual(self.plan['posted'], [URL], '不许降级去打可创建那条路')
        # **只试一次**:那条路不存在,再求几次只是白占预算。
        self.assertEqual(self.plan['restored'], [URL])

    def test_an_item_cleaned_up_while_waiting_is_never_called_written(self):
        """恢复撞上 409(那一行已经不在库里了)之后,**不许说成已写入知识库**。

        后果判据:那一条的终态是「库里没有这一行」,退出码非 0 —— 用户据此知道这
        一篇要重投,而不是去检索里找一篇不存在的文章。
        """
        self.plan['grabs'] = [(200, _grabbed(pipeline='busy', run=''))]
        self.plan['restores'] = [
            (409, {'detail': '这一条已经不在库里了，没有可恢复的条目'})]
        self.plan['states'] = {'*': [(200, _state()),
                                     (200, {'ok': True, 'url': URL, 'known': False,
                                            'done': False, 'content_ok': False,
                                            'owing': ''})]}
        code, out, _ = self.run_cli('--json', URL, timeout='3')
        self.assertEqual(code, 1, out)
        [rec] = self.records(out)
        # **这一条后端确认收下了**(`added=1`),之后才被清理掉 —— 所以是「收下了、
        # 现在不在库里」,不是 `failed` 那句「一个字节都没写、再投一次」。两者的
        # 下一步不一样:后者会让人原地再抓一篇,而删掉它的可能正是用户自己。
        self.assertEqual(rec['status'], 'gone', rec)
        self.assertEqual(rec['label'], '收下了，现在不在库里')
        self.assertEqual(rec['added'], 1, rec)
        self.assertNotIn('没存下来', rec['detail'])
        self.assertNotIn('已写入知识库', out)
        # 409 那一档**不许被当成「那就抓一篇新的」**:可创建那条路只许被用过一次
        # (最初那次投递)。
        self.assertEqual(self.plan['posted'], [URL])

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
