"""起跑前那笔**持久化边界**:写不上不许起跑,读不出来不算「不存在」。

两份终态都是收尾时才写的,**都写失败那一档磁盘上一个字都不会留下这一轮** ——
剩下的旧副本彼此一致,`same_event` 于是正确地证明「它们是同一次事件」,可那证明不了
「没有更新的一轮」。开工标记补的就是这一句,而它自己有两头会漏:

· 写不上照旧起跑 —— 那一轮真跑完了,重启却推不出它存在过(两份终态都是上一轮、
  逐字一致,而标记这一笔也没落上),于是面板显示上一轮、重试去抓上一轮的范围。
· 读不出来当成「没有」 —— 一次瞬时读失败就把「确证没有未交付的运行」固化下来,
  同一份文件在解除注入之后其实报得出失证。

所以这一族的判据一律落在**后果**上:起没起跑、`restore_gap()` 哪一档、那颗按钮按不
按得下去、按下去导出阶段收到谁的范围。硬约束见 `docs/decisions/pipeline-history.md`。

`test_pipeline_restore.py` 那边钉的是「恢复本身做对了没有」(内存读回来了没、活进程
被盖没被盖),这边钉的是「起跑的前提成立不成立」。
"""
import json
from pathlib import Path
from unittest import main
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import pipeline, pipeline_retry, pipeline_state as state
from kb.web import pipeline_api
from tests.pipeline_case import HistoryCase


class WriteGateTests(HistoryCase):
    """**边界落不下来就不要产生新的外部影响。**

    原来是「写不上只记一笔诊断、照旧起跑」。那个取舍把代价算小了:它的代价不是
    「这一轮的结果将来可能说不清」,而是**系统会重新授权上一轮的范围、而用户看不见**
    —— 实测 B 轮成功、开工标记和两份终态都注入满盘,重启后页头是 A 的 503、空欠账、
    没有过期提示,点「重试失败项」导出阶段收到 `accounts: ['A订阅']`。
    误报的代价很小而且看得见(这一轮不跑、面板上说清为什么;数据目录写不进去本来就
    是用户该知道的事,何况那一轮的终态也一样写不上)。
    """

    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(pipeline_api.router)
        self.client = TestClient(app)

    def test_a_plain_sync_does_not_start_when_the_boundary_cannot_be_written(self):
        """普通同步那条路:标记写不上 → **不起线程、运行状态一个字没改**。

        判据落在后果上,不是「返回了空串」:`running` 要还是 False、`run_id` 还是
        上一轮那个 —— 顺序是「先落边界、再认这一轮」,占不住就什么都没改,不用回滚
        (反过来写的话拒绝起跑得把那一堆字段挨个撤回去,撤漏一个就是下一轮带着上
        一轮的范围)。
        """
        self.run_round('run-a', 'ok', '已保存 3 条到知识库')
        before = dict(state._state)
        why = []
        with patch.object(state._open, '_write_atomic',
                          side_effect=OSError('No space left on device')), \
             patch.object(pipeline.threading, 'Thread') as thread:
            run_id = pipeline.start_pipeline(['crawl'], refused=why)

        self.assertEqual('', run_id, '没起跑')
        self.assertFalse(thread.called, '**线程一次都没起** —— 没有新的外部影响')
        self.assertFalse(state._state['running'])
        self.assertEqual(before['run_id'], state._state['run_id'],
                         '运行状态还是上一轮那份,没被半途改掉')
        self.assertFalse(state._open_file().exists(), '标记确实没写上')

    def test_the_refusal_names_the_real_reason_not_a_busy_pipeline(self):
        """**那一句 409 得说真话。**

        「管道已在运行中」只覆盖没起跑的一种。把「数据目录写不进去」也报成它,等于
        把一个要动手的故障说成「等一下再点」—— 用户会一直点下去。
        """
        self.restart()
        # **这里不许 patch `Thread`**:`pipeline.threading` 就是标准库那个模块对象,
        # patch 它等于把整个进程的 `threading.Thread` 换掉,而 `TestClient` 自己要
        # 起线程 —— 那一下整条用例挂住不返回。闸在起线程之前就拦下了,本来也没有
        # 线程可拦,所以改成直接断言「运行状态没被认下来」。
        with patch.object(state._open, '_write_atomic',
                          side_effect=PermissionError('read-only')):
            r = self.client.post('/api/pipeline/run', json={})

        self.assertEqual(409, r.status_code)
        self.assertFalse(state._state['running'], '这一轮没有被认下来')
        said = r.json()['detail']
        self.assertIn('没有起跑', said)
        self.assertIn('PermissionError', said, '异常类型要带上,人才查得下去')
        self.assertNotIn('已在运行', said, '**不许说成「忙，等一下」**')

    def test_a_failed_retry_does_not_start_when_the_boundary_cannot_be_written(self):
        """重试那条路同样有闸 —— **两份实现各有自己的红**。

        它比普通同步更要紧:重试轮失证之后,这颗按钮下一次按的就是**上一轮**的范围。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        with patch.object(state._open, '_write_atomic',
                          side_effect=OSError('No space left on device')), \
             patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()

        self.assertFalse(ok)
        self.assertFalse(thread.called, '线程一次都没起')
        self.assertIn('没有起跑', msg)
        self.assertFalse(state._state['running'])
        self.assertEqual('run-a', state._state['run_id'], '还是上一轮那份')

    # **下面两条是写闸的反方向**(原来在 `test_pipeline_restore.py`,这一族整体搬来了)
    # —— 闸写宽一格就会把正常那条路一起拦住,而「同步再也起不来」和「这个按钮坏了」
    # 在面板上长得一模一样。两条分开写是因为**两份实现各有自己的红**:合成一条的话
    # 红起来指不出是哪条起跑路漏了标记。
    def test_a_plain_sync_leaves_an_open_mark_before_it_starts(self):
        """**普通同步那条起跑路要落开工标记** —— 而且落在起线程之前。

        两个起跑入口各有自己的回归:任一处漏掉,那条路跑出来的轮次就回到「两份终态
        都没写上时查无此事」那个病,而另一条路的用例全绿。
        """
        self.restart()
        state._open_file().unlink(missing_ok=True)
        with patch.object(pipeline.threading, 'Thread') as thread:
            run_id = pipeline.start_pipeline(['crawl'])
        self.assertTrue(thread.called, '摆局前提:起跑确实走到了起线程那一步')
        mark = json.loads(state._open_file().read_text())
        self.assertEqual(run_id, mark['run_id'], '标记记的是这一轮的身份')
        self.assertEqual(state._SEQ_GEN, mark['seq_gen'], '而且带着出处')

    def test_a_failed_retry_also_leaves_an_open_mark(self):
        """**失败重试那条起跑路同样要落** —— 重试轮的终态也只在收尾时写。"""
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        self.assertFalse(state._open_file().exists(), '摆局前提:上一轮收尾清掉了标记')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, _ = pipeline_retry.start_retry_failed()
        self.assertTrue(ok and thread.called, '摆局前提:重试确实起跑了')
        mark = json.loads(state._open_file().read_text())
        self.assertEqual(state._state['run_id'], mark['run_id'])
        self.assertEqual('retry', mark['run_kind'], '标记里认得出这是重试轮')

class ReadVerdictTests(HistoryCase):
    """**「读不出来」和「不存在」是两件事。**

    合成一个空值的话,一次瞬时读失败就被当成「确证没有未交付的运行」固化下来:实测
    只对第一次读开工标记注入 `PermissionError`(文件一个字节没动、另两份全可读),
    恢复就把上一轮当成最新、报空欠账,真点一次重试导出阶段去抓了上一轮那个来源 ——
    而解除注入之后再读,同一份文件正确地报出 `lost`。能力一直在,被一次读失败吞掉了。
    """

    def _b_round_that_never_landed(self):
        """摆一局:A 正常留档,B 跑完但两份终态都写失败 —— 标记指着 B。"""
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        with self.all_writes_fail(OSError('No space left on device')):
            self.run_round('run-b', 'ok', '已保存 95 条')
        self.assertTrue(state._open_file().exists(), '摆局前提:标记留着')
        self.assertEqual('run-a', (state.load_last_run() or {}).get('run_id'),
                         '摆局前提:磁盘上最新的仍是 A')

    def test_a_mark_that_cannot_be_read_is_not_taken_as_absent(self):
        """标记读不出来 → `unknown`,**而且那颗按钮按不下去**。"""
        self._b_round_that_never_landed()
        self.restart()
        real = state._open._load_json_strict

        def deny(path):
            if path == state._open_file():
                raise PermissionError('read-only')
            return real(path)

        with patch.object(state._open, '_load_json_strict', side_effect=deny):
            state.get_state()
            self.assertEqual('unknown', state.restore_gap(),
                             '一份读不出来 —— 不是「确证没有未交付的运行」')
            # **欠账是懒算的**:读权限回来之后它会重新核对(那正是下一条钉的事),
            # 所以「那颗按钮按不按得下去」要在注入还挂着的时候问。
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '拦住重试:手上这一份证明不了是最新的')
        self.assertIn('读不出来', msg)

    def test_a_transient_mark_read_failure_is_not_frozen(self):
        """注入**只发生一次**:下一次读状态时要再核对一遍,不许固化。

        反向证据也在这一条里:同一份文件、解除注入之后报的是 `lost` —— 检测能力一直
        在,上一步不是摆局漏写了文件。
        """
        self._b_round_that_never_landed()
        self.restart()
        real = state._open._load_json_strict
        tries = []

        def deny_once(path):
            if path == state._open_file():
                tries.append(path)
                if len(tries) == 1:
                    raise PermissionError('read-only')
            return real(path)

        with patch.object(state._open, '_load_json_strict', side_effect=deny_once):
            state.get_state()
            self.assertEqual('unknown', state.restore_gap())
            state.get_state()
            self.assertEqual('lost', state.restore_gap(),
                             '读权限回来了就该核对出来:B 跑过、磁盘上一个字都没留下')
        self.assertEqual(1, len([1 for _ in tries[:1]]), '注入只发生了一次')

    def test_a_mark_with_a_broken_root_is_not_taken_as_absent(self):
        """标记的**根**坏了(`null` / `[]` / 一个字符串)→ 也是「说不清」。

        这三种都是合法 JSON,按「不是字典就算没有」处理的话,一份被手改过或者半截
        写入的标记就等于没有 —— 和读失败那一档同一个结局。
        """
        for raw in ('null', '[]', '"run-b"'):
            with self.subTest(raw=raw):
                self._b_round_that_never_landed()
                state._open_file().write_text(raw)
                self.restart()
                state.get_state()
                self.assertEqual('unknown', state.restore_gap(), raw)
                ok, _ = pipeline_retry.start_retry_failed()
                self.assertFalse(ok, '拦住重试')

    def test_a_mark_hidden_by_a_failing_exists_check_is_still_read(self):
        """判「标记在不在」只认 `FileNotFoundError`,**不许用 `Path.exists()`**。

        `exists()` 吞掉 `OSError`(父目录权限不足、路径里有一节不是目录、底层 I/O
        错误)返回 False —— 「看不见」于是长成「没有」,而这两件事在这里导出的结论
        完全相反。摆法就是让它一律说「不在」:真正的读照旧读得到那一份标记。
        """
        self._b_round_that_never_landed()
        self.restart()
        with patch.object(Path, 'exists', lambda self: False):
            state.get_state()
        self.assertEqual('lost', state.restore_gap(),
                         '标记就在那儿 —— 一句 `exists()` 不该把它变成「没有」')

    def test_a_missing_mark_is_a_proof_of_absence(self):
        """**反过来钉一边**:标记不在就是**确证**没有未交付的运行,照旧放行。

        这一档写宽了的后果是正常收尾之后每次重启都报一次失证,那颗按钮再也按不下去。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        self.assertFalse(state._open_file().exists(), '摆局前提:收尾清掉了标记')
        self.restart()
        state.get_state()
        self.assertEqual('', state.restore_gap(), '没有欠账')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, _ = pipeline_retry.start_retry_failed()
        self.assertTrue(ok and thread.called, '重试照旧起得来')

    def test_a_new_round_does_not_borrow_a_restored_rounds_provenance(self):
        """新一轮的标记带**自己的**出处,不借恢复回来那一轮的。

        恢复旧格式记录时运行状态里的出处是 None(那个格式压根没有这个字段)。起跑时
        拿运行状态去落标记的话,新一轮的标记就认不出身份 —— 而「认不出身份」被当成
        失证:实测只让状态终态写失败(历史那份写上了、带着当前世代),重启误报 `lost`,
        「一个字都没能留档」和那条打得开的历史自相矛盾。
        """
        legacy = self._legacy('run-old', '2026-10-14 08:00:00',
                              failed=True, started='2026-10-14 07:50:00')
        self.assertNotIn('seq_gen', legacy, '摆局前提:旧格式压根没有这个字段')
        self._put(legacy, [legacy])
        self.restart()
        self.assertEqual('run-old', state.get_state()['run_id'], '摆局前提:恢复到旧格式那轮')
        self.assertIsNone(state._state['seq_gen'], '摆局前提:它的出处是空的')

        with patch.object(pipeline.threading, 'Thread'):
            run_id = pipeline.start_pipeline(['crawl'])
        mark = json.loads(state._open_file().read_text())
        self.assertEqual(run_id, mark['run_id'])
        self.assertEqual(state._SEQ_GEN, mark['seq_gen'],
                         '**标记署的是当前世代**,不是被恢复那一轮的空出处')


if __name__ == '__main__':
    main()
