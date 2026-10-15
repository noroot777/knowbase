# -*- coding: utf-8 -*-
"""**参与证据的寿命**:它什么时候算证据、什么时候必须拦住抓取 —— 第 4、5 轮审查。

第 3 轮把「参与过」从账本里搬了出来(一笔不走那把锁的独立凭证,见
tests/test_fetch_pending.py)。第 4 轮审查接着钉了两刀:

- **账本和凭证能一起写不进去**(旁挂锁被占成目录 + 凭证目录路径被占成普通文件),
  而数据目录照旧可写:真实起跑闸成功、采集器真写出第二篇、随后硬退出,那一版仍报
  **确定的 1 条**。旧文档拿「数据目录写不进去就不会起跑」当这一档的闸,那条推理不
  成立 —— 早先某一个文件写成功,推不出稍后另外两个文件一定可写。
- **过期清理会按旧内容删掉所有者刚写上去的新一代**:一个进程一个固定路径、每次原子
  替换的话,清理方「读出旧的 → 判过期 → 删这个路径」整件事不是原子的。现在一次写
  就是一代、代与代各占一个路径、内容从此不改(`fetch_pending._slot`)。

第一刀当时是靠「第三处证据」闭合的 —— 那笔**起跑前提**的开工标记。**第 5 轮审查
证伪了它:标记的寿命是「那一轮运行」的,不是「那笔欠账」的。** 两份终态一写上就把它
清掉(`pipeline_state._end_run`),下一次起跑又原样覆盖;实测这一档**连硬退出都不
需要** —— 两条旁路一起坏掉、采集器真写出第三篇、`_end_run` 正常跑完、进程 0 退出,
全新进程仍报**确定的 2 条**,而 vault 里躺着三篇。

所以闸和读侧现在**只认寿命和这笔欠账绑死的两处证据**:账本里那条登记行(追加即不可
撤,只随保留窗口轮换)、那笔独立凭证(手里还欠行就在,行一落盘就撤)。判据照旧落在
**面板上那个数确定不确定**,不验「标志打上没有」:

- 两处都记不下 → 生产者一次 `fetch` 都不许跑,**盘上那笔开工标记认不认这个子轮
  都一样**;拦住的那句话要出现在阶段卡片上(它的代价全押在「误报看得见」上)。
- **反过来钉三边**:登记行落了盘的子轮照旧往下抓(之后两条旁路再坏也只报下界);
  一轮刚起跑、还没登记子轮的时候不许把上一轮的确定数说成「不完整」;**别人**那份
  记录读不出来(坏到抛 `RecursionError` 也算)只许降级成「读不出来」,不许把抓取
  拦在采集器之前。
- 清理只许撤销自己判过期的那一代;**反过来钉一边**:真正的孤儿照旧收掉,不然面板
  从此永久写「至少」。

硬约束、事故形态与反向验证红数在 docs/decisions/daily.md。
"""
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from kb import fetch_pending, fetch_rounds, pipeline_stages
from kb import pipeline_state as ps
from fetch_rounds_fixture import REPO, T, _Pending


# 第 5 轮审查那一例的子进程:**整条起跑—收尾走真的**,而且**不硬退出** —— 要证的
# 正是「`_end_run` 正常跑完」这一档,拿 `os._exit` 代替就把这一条的前提假掉了。
_NORMAL_END_CHILD = '''
import json, os, sys, threading, time
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, {repo!r})
root = Path(sys.argv[1])
from kb import config, fetch_rounds, pipeline, pipeline_stages
from kb import pipeline_state as ps
fetch_rounds.DATA_DIR = root
ps._STATE_FILE = root / 'pipeline_state.json'
config.VAULT_ROOT = root / 'vault'
config.SOURCES = [config.Source(name='rss', root=config.VAULT_ROOT / 'source/rss')]
# 闸是靠抛来拦住后面那个导出阶段的,所以管道线程那条 traceback 是**预期**的(生产
# 里它落在 kb-web 日志里)。这一条只看末态,别让它把用例输出刷满。
threading.excepthook = lambda args: None
lock = root / fetch_rounds.LOCK_NAME
pending = root / fetch_rounds.PENDING_DIR


class Producer:
    name = label = 'rss'

    def fetch(self, scope):
        (root / 'fetched.flag').write_text('采集器真跑了', encoding='utf-8')

    def materialize(self):
        target = config.VAULT_ROOT / 'source/rss/b.md'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('---\\ntitle: b\\n---\\n第二个子轮写出来的那篇\\n',
                          encoding='utf-8')
        return frozenset({{'source/rss/b.md'}})


real_crawl = pipeline._run_crawl


def crawl_after_both_side_files_break(filters):
    # 起跑闸已经过了(开工标记真落了盘),**这之后**账本那把锁和凭证目录一起坏掉
    assert (root / 'pipeline_state_open.json').exists(), '开工标记没落盘,这一例自己坏了'
    lock.unlink()
    lock.mkdir()
    pending.write_text('占住凭证目录的位置')
    real_crawl(filters)


with patch.object(pipeline, '_run_crawl', crawl_after_both_side_files_break), \\
        patch.object(pipeline_stages, 'crawl_channels',
                     Mock(return_value=[{{'channel': 'rss', 'accounts': []}}])), \\
        patch.object(pipeline_stages, 'active_collector', Mock(return_value=Producer())):
    assert pipeline.start_pipeline(['crawl', 'export'], {{'channel': 'rss'}},
                                   batch='batch-a'), '起跑被拒了,这一例自己坏了'
    deadline = time.monotonic() + 30
    while ps._state['running'] and time.monotonic() < deadline:
        time.sleep(0.02)
assert not ps._state['running'], '这一轮没按时收尾'
lock.rmdir()                         # 故障过去了(审查复现也是这么恢复的)
pending.unlink()
(root / 'child-proof.json').write_text(json.dumps({{
    'marker_after_normal_end': (root / 'pipeline_state_open.json').exists(),
    'persist_error': ps._persist_error,
    'crawl': ps._state['stages']['crawl'],
    'unsaved': list(fetch_rounds._unsaved),
    'fetched': (root / 'fetched.flag').exists(),
}}, ensure_ascii=False), encoding='utf-8')
'''


class TheGateOnlyTrustsEvidenceBoundToTheDebtTests(_Pending):
    """**闸只认寿命和欠账绑死的那两处证据**,第 4、5 轮审查 F1 的第一个实例。"""

    def _first_sub_round(self, day):
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual({'n': 1, 'added': 1, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))        # 第一个子轮:确定 1 条

    def _crawl(self, batch, run_id, producer):
        """真走 `pipeline_stages._run_crawl`(含那道闸),返回那个采集器。"""
        with patch.dict(ps._state, {'batch': batch, 'run_id': run_id,
                                    'planned_stages': ['crawl', 'export']}), \
                patch.object(pipeline_stages, 'crawl_channels',
                             Mock(return_value=[{'channel': 'rss', 'accounts': []}])), \
                patch.object(pipeline_stages, 'active_collector',
                             Mock(return_value=producer)):
            pipeline_stages._run_crawl({'channel': 'rss'})
        return producer

    def test_a_sub_round_that_cannot_record_itself_anywhere_does_not_fetch(self):
        """**两处证据都留不下 → 生产者一次 `fetch` 都不许跑。**

        继续抓就是产生一批永远不会进账的文章(重导时它们已经不是新文件,补不回来),
        而面板会笃定地报一个少记的数。那一档**单独一个异常类型**(`NoEvidence`):
        编排那一侧只把这一类的原因写进阶段卡片(真实路径上那张卡片由
        `test_a_normal_end_cannot_erase_the_only_participation_evidence` 钉),拿
        `RuntimeError` 当判据会把采集器自己抛的运行时错误也说成「抓取没有起跑」。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.break_lock()
        self.block_pending()
        producer = Mock(name='rss')
        with self.assertRaises(fetch_rounds.NoEvidence) as got:
            self._crawl('batch-a', 'run-2', producer)
        self.assertIn('没有往下抓', str(got.exception))
        self.assertEqual(0, producer.fetch.call_count)
        self.assertEqual(0, producer.materialize.call_count)
        # **反过来钉一边**:两处照旧坏着,而这一轮不物化 —— 不许拦。它一篇都落不进
        # vault,没有会被少记的成果,拦它只是把一次只勾索引的运行也做不成。
        fetch_rounds.open_round('batch-c', day, materializes=False, seg='run-3')
        self.process_gone()
        self.pending().unlink()
        self.fix_lock()
        self.assertEqual({'n': 1, 'added': 1, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))        # 那个确定数一个字没变

    def test_a_start_marker_does_not_buy_passage_through_the_gate(self):
        """**盘上那笔开工标记正指着这个子轮,闸照样不许它往下抓。**

        第 4 轮在这里认过它(它是起跑的前提,凡真抓过的运行盘上一定有过它),第 5 轮
        审查证伪了那条:**它的寿命是那一轮运行的,不是那笔欠账的** —— 两份终态一写上
        `_end_run` 就把它清掉,下一次起跑又原样覆盖。拿它放行等于把「这一轮会少报」
        押在「没有下一轮」上,而少报是静默丢数据:那几篇文章重导时已经不是新文件。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.mark_open('run-2')                          # 这一轮真开过工
        self.assertTrue(self.marker().exists())
        self.break_lock()
        self.block_pending()
        producer = Mock(name='rss')
        with self.assertRaises(fetch_rounds.NoEvidence) as got:
            self._crawl('batch-a', 'run-2', producer)
        self.assertIn('没有往下抓', str(got.exception))
        self.assertEqual(0, producer.fetch.call_count)

    def test_a_runtime_error_from_the_collector_is_not_called_a_refusal(self):
        """**反过来钉一边(编排那一侧)**:只有闸那一档才算「抓取没有起跑」。

        卡片和日志上那句话是闸的代价凭据(误报看得见),所以它只许写给
        `fetch_rounds.NoEvidence`。拿 `RuntimeError` 当判据的话,采集器自己抛的运行时
        错误(连接炸了、远端返回畸形)也会被说成「没有起跑」—— 而它明明跑了一半,
        那句话会把人引到磁盘和权限上去。"""
        from kb import pipeline
        with patch.object(ps, '_STATE_FILE', self.root / 'pipeline_state.json'), \
                patch.dict(ps._state, {
                    'run_id': 'run-boom', 'batch': '', 'running': True, 'log': [],
                    'planned_stages': ['crawl'], 'stop_requested': False,
                    'current_stage': '', 'finished_at': '', 'seq_gen': 1,
                    'stages': {name: {'status': 'pending', 'detail': ''}
                               for name in ps.STAGES}}), \
                patch.object(pipeline, '_run_crawl',
                             Mock(side_effect=RuntimeError('采集通道炸了'))):
            with self.assertRaises(RuntimeError):
                pipeline._run_pipeline(['crawl'])
            card = ps._state['stages']['crawl']
            log = '\n'.join(ps._state['log'])
        self.assertNotIn('没有起跑', card['detail'])
        self.assertNotIn('没有起跑', log)
        self.assertIn('流程中断', card['detail'])        # 收尾补的那句通用说法

    def test_a_registration_that_landed_lets_the_round_proceed_and_lower_bounds(self):
        """**反过来钉一边**:登记行落了盘的子轮照旧往下抓 —— 那一行就是寿命绑死的
        证据(追加即不可撤)。之后两条旁路再一起坏掉、进程没了,也只是报下界。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        producer = Mock(name='rss')
        self._crawl('batch-a', 'run-2', producer)        # 锁还好着:不许拦
        self.assertEqual(1, producer.fetch.call_count)
        self.break_lock()
        self.block_pending()
        self.write('source/rss/b.md')                    # 它真写出了第二篇
        self.process_gone()
        self.pending().unlink()
        self.fix_lock()
        self.assertEqual({'n': 1, 'added': 1, 'partial': True, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_a_sub_round_whose_registration_landed_takes_the_doubt_away(self):
        """**反过来钉一边**:那个子轮正常交完 —— 结论要回到确定值。只要有一处证据
        还在就一律报「不完整」的话,每一轮同步都永久「至少」。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        fetch_rounds.open_round('batch-a', day, seg='run-2')
        fetch_rounds.note_new('batch-a', {'source/rss/b.md'})
        fetch_rounds.note_end('batch-a', 'run-2')
        self.process_gone()
        self.assertEqual({'n': 1, 'added': 2, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_a_marker_from_a_run_that_has_not_registered_yet_does_not_cloud_it(self):
        """**反过来钉一边(读侧)**:盘上有一笔开工标记,而账本里还没有它那一行 ——
        读侧不许据此把上一轮的确定数说成「不完整」。

        这正是「标记不是抓取证据」的另一头:一轮刚起跑(标记已落、子轮还没登记)、
        或者一轮只勾了索引,都长成这个样子。读它的话,面板从此在每一轮同步的头几十
        毫秒里写「至少」,一轮只勾索引的运行更会让它永久这么写。少报那一档由
        `open_round` 那道闸在**抓之前**挡掉,不靠读侧事后猜。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.mark_open('run-2')
        self.assertEqual({'n': 1, 'added': 1, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))
        self.marker().write_text(                        # 存量/老格式的那一笔同理
            json.dumps({'run_id': 'run-old', 'started_at': '2026-09-29 09:00'}),
            encoding='utf-8')
        self.assertEqual({'n': 1, 'added': 1, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))

    def _unknown_but_still_fetches(self, run_id, why):
        """那份记录坏掉之后:读侧答「说不清」,而生产者照旧抓。两句都不许抛。"""
        for label, call in (('读侧', lambda: fetch_rounds.today(T('2026-09-29 09:00'))),
                            ('抓取', lambda: self._crawl('batch-a', run_id,
                                                         Mock(name='rss')))):
            try:
                got = call()
            except Exception as e:                       # noqa: BLE001 - 就是在钉它
                self.fail(f'{why}把{label}带红了:{type(e).__name__}')
            if label == '读侧':
                self.assertIsNone(got, f'{why}该算说不清,不是一个确定的数')
            else:
                self.assertEqual(1, got.fetch.call_count,
                                 f'{why}把抓取拦在采集器之前了')

    def test_a_record_too_broken_to_parse_never_blocks_the_capture(self):
        """**反过来钉一边(第 4 轮 N5,对称三处各一次)**:别人那份记录坏到抛
        `RecursionError`,只许降级成「读不出来」。

        纯展示层不许阻断生产者 —— 原来那几处 `except` 各列了一张异常单子,深嵌套
        JSON 抛的 `RecursionError`(`RuntimeError` 的子类)一个都不在单子上,于是
        一份手改坏的计数记录能把整次同步拦在采集器之前(审查实测生产者 `fetch`
        次数为 0)。两处证据各坏一次,读侧都只许答「说不清」;而那笔开工标记现在抓取
        压根不读,所以它坏掉的时候**连结论都不许变** —— 这一段留着当冗余带:谁把它
        读回来,这里会红。账本那一行放最后,它一追加就再也去不掉了。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        deep = '[' * 1100 + ']' * 1100
        self.pending().mkdir(parents=True, exist_ok=True)
        cert = self.pending() / 'other-7.json'
        cert.write_text(deep, encoding='utf-8')
        self._unknown_but_still_fetches('run-2', '坏掉的凭证')
        cert.unlink()
        before = fetch_rounds.today(day)
        self.assertIsNotNone(before)
        self.marker().write_text(deep, encoding='utf-8')
        self.assertEqual(before, fetch_rounds.today(day))  # 读侧压根不读这一笔
        self.assertEqual(1, self._crawl('batch-a', 'run-3',
                                        Mock(name='rss')).fetch.call_count)
        self.marker().unlink()
        with open(self.root / fetch_rounds.FILE_NAME, 'a', encoding='utf-8') as fh:
            fh.write(deep + '\n')                        # 对称的第二处:账本那一行
        self._unknown_but_still_fetches('run-4', '坏掉的账本行')

    def test_a_normal_end_cannot_erase_the_only_participation_evidence(self):
        """**第 5 轮审查那一例:整条管道走真的,而且不硬退出。**

        真实 `start_pipeline` → 开工标记真落盘 → **这之后**账本那把锁和凭证目录一起
        坏掉 → 真实 `_end_run` 正常跑完、两份终态都写上、进程 0 退出。审查那一版在
        这里把标记当第三处证据,而收尾正好把它清掉:vault 里多一篇、全新进程却笃定
        地报原来那个数。

        现在闸在**抓之前**就拦住了,所以末态是:采集器一次都没跑、vault 一篇都没多、
        那个确定数照旧对得上盘上的文章。断言钉在**另一个进程**读出来的那个数上,
        而且那个子进程只把账本那一侧指到临时目录 —— 两处证据必须在这种打桩下也找
        得到。"""
        fetch_rounds.open_round('batch-a', seg='run-1')
        fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
        fetch_rounds.note_end('batch-a', 'run-1')
        self.write('source/rss/a.md')
        self.assertEqual({'added': 1, 'partial': False},
                         {k: fetch_rounds.today()[k] for k in ('added', 'partial')})
        script = self.root / 'normal_end_child.py'
        script.write_text(_NORMAL_END_CHILD.format(repo=str(REPO)), encoding='utf-8')
        done = subprocess.run([sys.executable, str(script), str(self.root)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stderr)
        proof = json.loads((self.root / 'child-proof.json').read_text())
        # 收尾**正常**完成,而它顺手把那笔标记清掉了 —— 这正是它当不了证据的原因
        self.assertEqual('', proof['persist_error'])
        self.assertFalse(proof['marker_after_normal_end'])
        self.assertEqual('error', proof['crawl']['status'])
        self.assertIn('没有往下抓', proof['crawl']['detail'])
        # 闸落在采集器之前:一次 fetch 都没跑、vault 一篇都没多
        self.assertFalse(proof['fetched'])
        self.assertFalse((self.vault / 'source/rss/b.md').exists())
        # 它攥着的就是那一行登记(子轮身份是真 `start_pipeline` 发的,别钉字面量)
        self.assertEqual(1, len(proof['unsaved']))
        self.assertEqual('batch-a', proof['unsaved'][0]['key'])
        self.assertNotIn(proof['unsaved'][0]['seg'], ('', 'run-1'))
        self.assertEqual([], self.certs())               # 凭证一笔都没落下去
        rounds, ok = fetch_rounds._read()
        self.assertTrue(ok)                              # 账本行行合法 ——
        self.assertEqual([({'run-1'}, {'run-1'})],       # 子进程那个子轮一行都没留下
                         [(r['segs'], r['ends']) for r in rounds])
        self.assertEqual(['source/rss/a.md'],
                         sorted(str(p.relative_to(self.vault))
                                for p in self.vault.rglob('*.md')))
        self.assertEqual({'added': 1, 'partial': False},  # 和盘上那一篇对得上
                         {k: self.restart_today()[k] for k in ('added', 'partial')})


class CleanupOnlyRevokesTheGenerationItJudgedTests(_Pending):
    """**第 4 轮审查 F1 的第二个实例**:过期清理和所有者的更新撞在同一个路径上。

    上一版一个进程一个固定路径、每次原子替换,于是清理方「读出旧内容 → 判过期 →
    按同一个路径删」整件事不是原子的:所有者刚换上去的当天那一代被按五天前的内容
    删掉,vault 里的文章还在,而面板回到那个确定的少报值。可达性不靠撞运气 ——
    **常驻进程一直用同一个 TAG**,上次撤销失败之后它跨过保留窗口再工作,旧内容和新
    内容就是同一个路径。
    """

    def _round(self, day):
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
        fetch_rounds.note_end('batch-a', 'run-1')

    def test_expiry_cannot_delete_a_generation_written_during_it(self):
        """清理方读出五天前那一笔、判了过期;**这期间**所有者把自己那笔更新成当天的。

        摆的就是「常驻进程跨过保留窗口再工作」那个形态:上一笔撤销失败(删不掉)留在
        盘上,所以它和新写的那一笔出自同一个进程。交错不靠撞运气 —— 在清理方 `_load`
        读到那一笔的那一刻让所有者更新,再放它往下走。

        **写进去的那一笔必须活下来**:上一版两边撞在同一个路径上,清理方按五天前的
        内容把当天那笔删掉,`held` 变空、面板回到那个确定的少报值。**反过来钉一边**:
        过期那一笔照旧收掉(真孤儿不收的话面板从此永久写「至少」)。

        这里直接调清理那一侧(`_prune`,生产入口 `put` 的 `floor` 参数原样透传给
        它):同一个进程里既当清理方又当所有者的话,走 `put(keys=())` 会把「我什么
        都不欠了」这件事也一起表达掉,两个角色就分不开了。
        """
        day = T('2026-09-29 09:00')
        self._round(day)
        folder = self.pending()
        # 五天前它自己写的那一笔(**走生产那条路写**,再把日子改成窗口外的 ——
        # 名字不许在用例里硬编码:那是协议的内部细节,钉了它这条用例就只认旧形状)
        fetch_pending.put(folder, ['batch-a'], '2026-09-24')
        stale = next(iter(folder.glob('*' + fetch_pending.SUFFIX)))
        real_load = fetch_pending._load
        updated = []

        def load_then_let_the_owner_update(path):
            got = real_load(path)
            if path == stale and not updated:
                updated.append(path)
                fetch_pending.put(folder, ['batch-a'], fetch_rounds._day(day))
            return got

        floor = fetch_rounds._floor(fetch_rounds._now(day))
        with patch.object(fetch_pending, '_load', load_then_let_the_owner_update):
            fetch_pending._prune(folder, floor)
        self.assertEqual([stale], updated)               # 交错真的发生了
        self.assertFalse(stale.exists())                 # 过期那一笔该收掉
        self.assertEqual({'batch-a'}, fetch_pending.mine(folder))  # 当天那笔还在
        self.assertEqual(({'batch-a'}, True), fetch_pending.held(folder, floor))
        self.process_gone()
        self.assertEqual({'n': 1, 'added': 1, 'partial': True, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_a_generation_that_could_not_be_written_keeps_the_older_one(self):
        """**新一代没落下来的时候,旧的那几代一个都不许删** —— 那是「我还欠着行」
        唯一的说法,删了就回到「盘上一个字都不说明它参与过」。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        self.break_lock()
        fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
        first = self.certs()
        self.assertEqual(1, len(first))
        with patch.object(fetch_pending, '_write_atomic',
                          Mock(side_effect=OSError('写不下去'))):
            fetch_rounds.note_new('batch-a', {'source/rss/b.md'})
        self.assertEqual(first, self.certs())            # 旧一代还在
        self.assertEqual({'batch-a'}, fetch_pending.mine(self.pending()))
        self.process_gone()
        self.fix_lock()
        self.assertTrue(fetch_rounds.today(day)['partial'])

if __name__ == '__main__':
    unittest.main()
