# -*- coding: utf-8 -*-
"""**「没有留下参与证据」不等于「没有参与」** —— 这一份钉第 3 轮审查那条拦验收。

当日抓取账本的每一次写都在一把旁挂的文件锁里,锁取不到就一行都不写、攥在内存
里等下次。那份内存状态随进程消失,于是上一版剩着这一刀:**某个子轮的登记行没
落盘、进程又没了**,盘上一个字都不说明它参与过 —— 而它可能已经往 vault 落了
文章。审查实测的形态是:第二个子轮因为锁暂时不可用没登记上、随后 `os._exit`,
盘上第一个子轮的开轮行/回执/结束行行行合法,`today()` 据此报**确定的 1 条**,而
vault 里躺着两篇;刷新、重启、重导都纠不回来(重导时那一篇已经不是新文件)。

同一条拦项的第二个实例在**缓冲自己**身上:追加是 I/O,它不能罩在进程内那把缓冲
锁里,于是「追加成功 → 清空缓冲」会把这期间另一条线程压进来的回执一起抹掉 ——
而面板每 15 秒读一次,`today()` 自己就是那个补写的人。

第 4 轮审查接着钉同一条拦项的另外两刀 —— **凭证自己的失败和清理**:

- **账本和凭证能一起写不进去**(旁挂锁被占成目录 + 凭证目录路径被占成普通文件),
  而数据目录照旧可写:真实起跑闸成功、采集器真写出第二篇、随后硬退出,上一版仍报
  **确定的 1 条**。旧文档拿「数据目录写不进去就不会起跑」当这一档的闸,那条推理不
  成立 —— 早先某一个文件写成功,推不出稍后另外两个文件一定可写。所以参与证据有
  第三处:那笔**起跑前提**的开工标记(`kb.fetch_claim`),而三处一处都留不下时
  `open_round` 直接拦住,不许产生一批永远不会进账的文章。
- **过期清理会按旧内容删掉所有者刚写上去的新一代**:上一版一个进程一个固定路径、
  每次原子替换,清理方「读出旧的 → 判过期 → 删这个路径」整件事不是原子的。现在
  一次写就是一代、代与代各占一个路径、内容从此不改(`fetch_pending._slot`)。

四条的判据都落在**面板上那个数确定不确定**(用户照它决定要不要再花一次模型钱),
不验「标志打上没有」:
- 登记没落盘又硬退出 → 只许报下界(或者干脆「读不出来」),不许沿用上一个子轮
  的确定结论;**反过来钉一边**:行赶在进程没之前落了盘,结论要回到确定值,不许
  从此永久写「至少」。
- 另一条线程在追加期间压进来的回执 → 一条都不许少;**反过来钉一边**:写不下去
  的时候一行都不许剔掉。
- 三处证据一处都留不下 → 生产者一次 `fetch` 都不许跑;**反过来钉一边**:**别人**
  那份记录读不出来(坏到抛 `RecursionError` 也算)只许降级成「读不出来」,不许把
  抓取拦在采集器之前,也不许把一轮只勾索引的运行判成「不完整」。
- 清理只许撤销自己判过期的那一代;**反过来钉一边**:真正的孤儿照旧收掉,不然面板
  从此永久写「至少」。

账本本体和它的恢复协议在 tests/test_fetch_rounds.py,成果归属在
tests/test_fetch_receipt.py,硬约束与事故形态在 docs/decisions/daily.md。
"""
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kb import fetch_pending, fetch_rounds, pipeline_stages
from kb import pipeline_state as ps
from fetch_rounds_fixture import T, _Base


REPO = Path(__file__).resolve().parent.parent

# 真实硬退出那一例的子进程。**不能用抛异常代替**:`finally` 还能跑的进程会把攥着
# 的行补写下去,而这一条要证的正是"补写的机会都没有"那一刀(审查原话:异常可执行
# finally 的用例不能代替硬退出)。
_CHILD = '''
import os, sys
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, {repo!r})
root = Path(sys.argv[1])
from kb import config, fetch_rounds, pipeline_stages
from kb import pipeline_state as ps
fetch_rounds.DATA_DIR = root
config.VAULT_ROOT = root / 'vault'
config.SOURCES = [config.Source(name='rss', root=config.VAULT_ROOT / 'source/rss')]
ps._state.update({{'batch': 'batch-a', 'run_id': 'run-2',
                   'planned_stages': ['crawl', 'export'], 'stop_requested': False,
                   'log': [], 'stages': {{s: {{'status': '', 'detail': ''}}
                                          for s in ps.STAGES}}}})
lock = root / fetch_rounds.LOCK_NAME
lock.unlink()                       # 这个子轮开工时账本那把锁坏了
lock.mkdir()


class Killed:
    name = label = 'rss'

    def fetch(self, scope):
        pass

    def materialize(self):
        target = config.VAULT_ROOT / 'source/rss/b.md'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('---\\ntitle: b\\n---\\n第二个子轮真写出来的那篇\\n',
                          encoding='utf-8')
        lock.rmdir()                # 锁好了,但这一刀落在它来得及补写之前
        os._exit(23)


with patch.object(pipeline_stages, 'crawl_channels',
                  Mock(return_value=[{{'channel': 'rss', 'accounts': []}}])), \\
        patch.object(pipeline_stages, 'active_collector', Mock(return_value=Killed())):
    pipeline_stages._run_crawl()
    pipeline_stages._run_export()
os._exit(70)                        # 走到这儿说明这条用例自己坏了
'''


# 「两条旁路一起失效」那一例的子进程。**起跑那一段走真的 `start_pipeline`**:要证的
# 正是「真实起跑闸已经成功、随后另外两个文件一起写不进去」,拿手搓的标记代替就把
# 这一条最关键的前提假掉了。
_JOINT_CHILD = '''
import os, sys, threading
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
lock = root / fetch_rounds.LOCK_NAME
pending = root / fetch_rounds.PENDING_DIR


class Killed:
    name = label = 'rss'

    def fetch(self, scope):
        pass

    def materialize(self):
        target = config.VAULT_ROOT / 'source/rss/b.md'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('---\\ntitle: b\\n---\\n第二个子轮真写出来的那篇\\n',
                          encoding='utf-8')
        lock.rmdir()                 # 两条旁路都好了,可这一刀落在补写之前
        pending.unlink()
        os._exit(23)


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
        patch.object(pipeline_stages, 'active_collector', Mock(return_value=Killed())):
    assert pipeline.start_pipeline(['crawl', 'export'], {{'channel': 'rss'}},
                                   batch='batch-a'), '起跑被拒了,这一例自己坏了'
    threading.Event().wait(20)
os._exit(70)                         # 走到这儿说明这条用例自己坏了
'''


class _Pending(_Base):
    """账本那把锁、那几笔凭证的小工具。"""

    def lock(self):
        return self.root / fetch_rounds.LOCK_NAME

    def break_lock(self):
        """把旁挂锁占成一个目录 —— 取锁从此失败(审查复现用的就是这一手)。"""
        self.lock().unlink(missing_ok=True)
        self.lock().mkdir()

    def fix_lock(self):
        self.lock().rmdir()

    def pending(self):
        return self.root / fetch_rounds.PENDING_DIR

    def certs(self):
        folder = self.pending()
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    def block_pending(self):
        """把凭证目录的位置占成一个普通文件 —— 那一笔从此写不下去。"""
        self.pending().write_text('占住凭证目录的位置', encoding='utf-8')

    def mark_open(self, batch, run_id, stages=('crawl', 'export')):
        """落一笔**真的**开工标记(走生产那条 `pipeline_open.mark`,不手搓 JSON)。

        标记的位置按账本自己那个数据目录解析(`fetch_claim._marker`),所以这里把
        `pipeline_state._STATE_FILE` 也指到临时目录 —— 两边指到同一个库,才是生产里
        那个形状。
        """
        with patch.object(ps, '_STATE_FILE', self.root / 'pipeline_state.json'):
            self.assertEqual('', ps.mark_open(
                {'run_id': run_id, 'seq_gen': 1, 'started_at': '2026-09-29 09:00',
                 'run_kind': 'sync', 'batch': batch,
                 'planned_stages': list(stages)}))

    def restart_today(self):
        """**另一个进程**读出来的那个数(内存里什么都没有,盘上留下什么就是什么)。

        只把账本那一侧指到临时目录 —— 审查复现脚本也是这么打桩的,而参与证据必须
        在这种打桩下也找得到:开工标记按**账本自己那个数据目录**解析
        (`fetch_claim._marker`),不认另一个库那份标记。
        """
        code = ('import json; from pathlib import Path; from kb import fetch_rounds; '
                f'fetch_rounds.DATA_DIR = Path({str(self.root)!r}); '
                'print(json.dumps(fetch_rounds.today()))')
        done = subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, check=True, cwd=str(REPO))
        return json.loads(done.stdout.strip().splitlines()[-1])

    def process_gone(self):
        """模拟进程没了:内存那三份状态随它消失,盘上留下什么就是什么。"""
        for name in ('_unsaved', '_opened', '_gaps'):
            setattr(fetch_rounds, name, [])


class ParticipationOutlivesTheProcessTests(_Pending):
    """**第 3 轮审查 F1 的第一个实例**:登记行没落盘的子轮也得留下参与证据。"""

    def _first_sub_round(self, day):
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual({'n': 1, 'added': 1, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))        # 第一个子轮:确定 1 条

    def test_a_sub_round_whose_registration_never_landed_is_not_a_certain_count(self):
        """第二个子轮开工时锁坏了(登记行只攥在手里),它写出第二篇之后进程没了。

        盘上只有第一个子轮那一套合法的行,`segs == ends` 照旧成立 —— 上一版据此
        报**确定的 1 条**,而 vault 里躺着两篇。现在那一笔不走锁的凭证留下了
        「这一轮的证据还在我手里」,结论压到下界。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.break_lock()
        fetch_rounds.open_round('batch-a', day, seg='run-2')
        self.write('source/rss/b.md')                    # 它真写出了第二篇
        self.assertEqual([], [l for l in self.lines() if 'run-2' in l])
        self.process_gone()
        self.fix_lock()
        self.assertEqual({'n': 1, 'added': 1, 'partial': True, 'at': '09:00'},
                         fetch_rounds.today(day))
        self.assertEqual({'n': 1, 'added': 1, 'partial': True, 'at': '09:00'},
                         fetch_rounds.today(day))        # 重读一次:结论不变

    def test_lines_that_landed_before_the_process_died_take_the_doubt_away(self):
        """**反过来钉一边**:锁在进程没之前就好了、那几行补写成功 —— 结论要回到
        确定值。永久写「至少」的凭证等于把这颗药丸作废,而它要回答的是"要不要再
        花一次模型钱"。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.break_lock()
        fetch_rounds.open_round('batch-a', day, seg='run-2')
        self.fix_lock()
        fetch_rounds.note_new('batch-a', {'source/rss/b.md'})
        fetch_rounds.note_end('batch-a', 'run-2')
        self.assertEqual([], self.certs())               # 凭证撤掉了
        self.process_gone()
        self.assertEqual({'n': 1, 'added': 2, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_a_round_whose_open_line_was_lost_is_unknown_not_an_older_count(self):
        """同一刀的**单轮形态**:第二次抓取连开轮行都没落下去,盘上最新的轮次是
        第一次那次。拿它的数当今天的答案,就是把上一次的成果说成这一次的 ——
        所以这一档只能答「读不出来」。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.break_lock()
        fetch_rounds.open_round('batch-b', T('2026-09-29 14:00'), seg='run-2')
        self.write('source/rss/b.md')
        self.process_gone()
        self.fix_lock()
        self.assertIsNone(fetch_rounds.today(day))

    def test_a_certificate_from_outside_the_window_does_not_haunt_today(self):
        """**反过来钉一边 + 挡无界增长**:凭证和账本共用同一个保留窗口。窗口外
        那几份忽略(账本自己也只留到那天 —— 它指的那一轮早被轮换清掉了,再认它
        就会从此永久答「读不出来」),而且下一次写成功时顺手删掉。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        folder = self.root / fetch_rounds.PENDING_DIR
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'dead-proc.json').write_text(
            json.dumps({'day': '2026-09-20', 'keys': ['ghost']}), encoding='utf-8')
        self.assertEqual({'n': 1, 'added': 1, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))
        fetch_rounds.note_new('batch-a', ())             # 下一次写:顺手清场
        self.assertEqual([], self.certs())

    def test_an_unreadable_certificate_is_unknown_not_nothing(self):
        """凭证读不出来(手改过、坏块、别的版本写的)是**说不清**,不是「没有」。

        合成一个空集的话,一次读故障就把「有一轮的证据还扣着」说成「一切都落
        盘了」—— 这一整层存在的理由就是不许出现那句话。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        folder = self.root / fetch_rounds.PENDING_DIR
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'broken.json').write_text('{"day": "2026-09', encoding='utf-8')
        self.assertIsNone(fetch_rounds.today(day))
        (folder / 'broken.json').unlink()
        self.assertEqual({'n': 1, 'added': 1, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))        # 反过来:好的凭证不拦

    def test_a_hard_exit_in_the_real_stages_still_leaves_the_participation(self):
        """整条管道走真的、进程**硬退出**(`os._exit(23)`,`finally` 一句都跑不到)。

        子进程里 `_run_crawl` / `_run_export` 都是真的,采集器真往 vault 写第二篇;
        账本那把锁在它开工时坏着,所以盘上没有它的任何一行 —— 断言就钉在这里:
        `run-2` 在账本里查不到,而面板那个数不许是确定的 1 条。"""
        fetch_rounds.open_round('batch-a', seg='run-1')
        fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
        fetch_rounds.note_end('batch-a', 'run-1')
        before = fetch_rounds.today()
        self.assertEqual({'added': 1, 'partial': False},
                         {k: before[k] for k in ('added', 'partial')})
        script = self.root / 'child.py'
        script.write_text(_CHILD.format(repo=str(REPO)), encoding='utf-8')
        done = subprocess.run([sys.executable, str(script), str(self.root)],
                              capture_output=True, text=True)
        self.assertEqual(23, done.returncode, done.stderr)
        self.assertTrue((self.vault / 'source/rss/b.md').exists())
        self.assertEqual([], [l for l in self.lines() if 'run-2' in l])
        after = fetch_rounds.today()
        self.assertEqual({'added': 1, 'partial': True},
                         {k: after[k] for k in ('added', 'partial')})


class ConfirmRemovesOnlyWhatItWroteTests(_Pending):
    """**第 3 轮审查 F1 的第二个实例**:缓冲是两条线程共用的。

    追加是 I/O,罩进进程内那把缓冲锁就会把面板那一跳堵在磁盘故障后面 —— 所以它
    在锁外跑,而这期间另一条线程完全可以压进新的回执。上一版追加成功之后一句
    `_unsaved = []` 把它一起抹掉了:那一篇再也补不回账(重导时它已经不是新文件),
    而 `today()` 自称确定。
    """

    def test_a_receipt_queued_while_another_thread_flushes_is_not_erased(self):
        """面板那一跳(`today()` 替攥着的行补写)正卡在追加里,管道线程这时记下
        第二篇的回执 —— 追加回来之后它必须还在,随后照旧落盘、照旧算进那个数。

        交错是摆出来的,不靠撞运气:在 `_append` 里把面板那条线程按住,主线程这
        期间把锁占成目录(于是它走的是"攥着不写"那条路,正是上一版会被抹掉的那
        一批),再放开面板那条线程。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        self.break_lock()
        fetch_rounds.note_new('batch-a', {'source/rss/a.md'})   # 第一篇:攥着
        self.fix_lock()
        entered, release = threading.Event(), threading.Event()
        real_append = fetch_rounds._append
        reader = []

        def paused_append(batch):
            if threading.current_thread().name == 'panel':
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('放行等超时了')
            return real_append(batch)

        with patch.object(fetch_rounds, '_append', paused_append):
            panel = threading.Thread(name='panel',
                                     target=lambda: reader.append(
                                         fetch_rounds.today(day)))
            panel.start()
            self.assertTrue(entered.wait(10))
            self.break_lock()                 # 这会儿锁又坏了
            fetch_rounds.note_new('batch-a', {'source/rss/b.md'})   # 第二篇:压进缓冲
            release.set()
            panel.join(10)
            self.assertFalse(panel.is_alive())
        self.fix_lock()
        self.assertIsNone(reader[0])          # 失锁期间面板如实说「读不出来」
        fetch_rounds.note_end('batch-a', 'run-1')
        book = '\n'.join(self.lines())
        self.assertIn('source/rss/b.md', book)        # 那一行真落了盘
        self.assertEqual({'n': 1, 'added': 2, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_a_flush_that_could_not_write_keeps_owing_every_line(self):
        """**反过来钉一边**:确认只剔自己写下去的那几行,**写不下去就一行都不剔**
        —— 连那笔凭证一起留着。磁盘一好,两篇都补得回来。"""
        day = T('2026-09-29 09:00')
        fetch_rounds.open_round('batch-a', day, seg='run-1')
        with patch.object(fetch_rounds, '_append', Mock(return_value=False)):
            fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
            fetch_rounds.note_new('batch-a', {'source/rss/b.md'})
            self.assertEqual(2, len([o for o in fetch_rounds._unsaved
                                     if 'paths' in o]))
            # 读回来确认那一笔真落下去了(名字是分代的,别钉具体代号)
            self.assertEqual({'batch-a'}, fetch_pending.mine(self.pending()))
            self.assertEqual(1, len(self.certs()))       # 一代,不是一代一代堆着
            self.assertIsNone(fetch_rounds.today(day))
        fetch_rounds.note_end('batch-a', 'run-1')         # 磁盘好了
        self.assertEqual({'n': 1, 'added': 2, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))
        self.assertEqual([], self.certs())                # 落了盘就撤掉



class EvidenceMustOutliveBothSideFilesTests(_Pending):
    """**第 4 轮审查 F1 的第一个实例**:账本和凭证能一起写不进去。

    那时数据目录本身照旧可写 —— 真实起跑闸成功、采集器真写出文章、随后硬退出。
    旧版拿「数据目录写不进去就不会起跑」当这一档的闸,那条推理不成立,所以参与证据
    有第三处(起跑前提那笔开工标记),而三处一处都留不下时不许往下抓。
    """

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

    def test_the_start_marker_alone_still_lower_bounds_the_count(self):
        """账本那把锁和凭证目录**一起**坏掉:盘上只剩那笔开工标记。

        它不是尽力而为的旁文件,是起跑的前提 —— 读侧据它把这一轮压到下界,不许
        沿用第一个子轮那个确定的 1 条(审查实测的末态就是 `n=1, added=1,
        partial=false`,而 vault 里躺着两篇)。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.mark_open('batch-a', 'run-2')               # 起跑闸:这一轮真开过工
        self.break_lock()
        self.block_pending()
        fetch_rounds.open_round('batch-a', day, seg='run-2')   # 不许拦:标记在
        self.write('source/rss/b.md')                    # 它真写出了第二篇
        self.assertEqual(set(), fetch_pending.mine(self.pending()))  # 凭证一笔都没有
        self.assertEqual([], [l for l in self.lines() if 'run-2' in l])
        self.process_gone()
        self.pending().unlink()
        self.fix_lock()
        self.assertEqual({'n': 1, 'added': 1, 'partial': True, 'at': '09:00'},
                         fetch_rounds.today(day))
        self.assertEqual({'n': 1, 'added': 1, 'partial': True, 'at': '09:00'},
                         fetch_rounds.today(day))        # 重读一次:结论不变

    def test_a_sub_round_whose_registration_landed_takes_the_doubt_away(self):
        """**反过来钉一边**:开工标记指的那个子轮登记在册(正常跑完的样子)——
        结论要回到确定值。标记还在就一律报「不完整」的话,每一轮同步都永久「至少」。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.mark_open('batch-a', 'run-2')
        fetch_rounds.open_round('batch-a', day, seg='run-2')
        fetch_rounds.note_new('batch-a', {'source/rss/b.md'})
        fetch_rounds.note_end('batch-a', 'run-2')
        self.process_gone()
        self.assertEqual({'n': 1, 'added': 2, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))

    def test_a_run_that_never_fetches_is_not_a_missing_round(self):
        """**反过来钉一边**:判据是「身份对得上」,不是「标记还在」。

        只勾了索引的那种运行压根不在当日账本上开轮 —— 只看「标记在不在」的话,
        它会让面板从此永久答「读不出来」。而老格式的标记(没有这两个字段)按
        **说不清**处理:那一档下一次起跑就覆盖掉了,宁可多拒。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.mark_open('', 'run-index', stages=('index',))
        self.assertEqual({'n': 1, 'added': 1, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))
        (self.root / 'pipeline_state_open.json').write_text(
            json.dumps({'run_id': 'run-old', 'started_at': '2026-09-29 09:00'}),
            encoding='utf-8')
        self.assertIsNone(fetch_rounds.today(day))       # 老格式:说不清它抓不抓

    def test_a_sub_round_that_cannot_record_itself_anywhere_does_not_fetch(self):
        """**三处一处都留不下 → 生产者一次 `fetch` 都不许跑。**

        继续抓就是产生一批永远不会进账的文章(重导时它们已经不是新文件,补不回来),
        而面板会笃定地报一个少记的数。拦住的代价看得见:阶段红一条,数字一个字没变。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        self.break_lock()
        self.block_pending()                             # 这一轮没有开工标记
        producer = Mock(name='rss')
        with self.assertRaises(RuntimeError) as got:
            self._crawl('batch-a', 'run-2', producer)
        self.assertIn('没有往下抓', str(got.exception))
        self.assertEqual(0, producer.fetch.call_count)
        self.assertEqual(0, producer.materialize.call_count)
        self.process_gone()
        self.pending().unlink()
        self.fix_lock()
        self.assertEqual({'n': 1, 'added': 1, 'partial': False, 'at': '09:00'},
                         fetch_rounds.today(day))        # 那个确定数一个字没变

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
        """**反过来钉一边(本轮 N5,对称三处各一次)**:别人那份记录坏到抛
        `RecursionError`,只许降级成「读不出来」。

        纯展示层不许阻断生产者 —— 原来那几处 `except` 各列了一张异常单子,深嵌套
        JSON 抛的 `RecursionError`(`RuntimeError` 的子类)一个都不在单子上,于是
        一份手改坏的计数记录能把整次同步拦在采集器之前(审查实测生产者 `fetch`
        次数为 0)。三处:独立凭证、账本自己那一行、那笔开工标记。"""
        day = T('2026-09-29 09:00')
        self._first_sub_round(day)
        deep = '[' * 1100 + ']' * 1100
        self.pending().mkdir(parents=True, exist_ok=True)
        cert = self.pending() / 'other-7.json'
        cert.write_text(deep, encoding='utf-8')
        self._unknown_but_still_fetches('run-2', '坏掉的凭证')
        cert.unlink()
        marker = self.root / 'pipeline_state_open.json'
        marker.write_text(deep, encoding='utf-8')
        self._unknown_but_still_fetches('run-3', '坏掉的开工标记')
        marker.unlink()
        with open(self.root / fetch_rounds.FILE_NAME, 'a', encoding='utf-8') as fh:
            fh.write(deep + '\n')                        # 对称的第三处:账本那一行
        self._unknown_but_still_fetches('run-4', '坏掉的账本行')

    def test_a_hard_exit_after_both_side_files_broke_still_lower_bounds(self):
        """整条管道走真的:真实 `start_pipeline`(开工标记真落盘)→ 两条旁路一起
        坏掉 → 采集器真往 vault 写第二篇 → `os._exit(23)`。

        断言钉在**另一个进程**读出来的那个数上(盘上留下什么就是什么),而且那个
        子进程只把账本那一侧指到临时目录 —— 参与证据必须在这种打桩下也找得到。"""
        fetch_rounds.open_round('batch-a', seg='run-1')
        fetch_rounds.note_new('batch-a', {'source/rss/a.md'})
        fetch_rounds.note_end('batch-a', 'run-1')
        self.assertEqual({'added': 1, 'partial': False},
                         {k: fetch_rounds.today()[k] for k in ('added', 'partial')})
        script = self.root / 'joint_child.py'
        script.write_text(_JOINT_CHILD.format(repo=str(REPO)), encoding='utf-8')
        done = subprocess.run([sys.executable, str(script), str(self.root)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(23, done.returncode, done.stderr)
        self.assertTrue((self.vault / 'source/rss/b.md').exists())
        self.assertEqual([], self.certs())               # 凭证一笔都没落下去
        rounds, ok = fetch_rounds._read()
        self.assertTrue(ok)                              # 账本行行合法 ——
        self.assertEqual([({'run-1'}, {'run-1'})],       # 子进程那个子轮一行都没留下
                         [(r['segs'], r['ends']) for r in rounds])
        self.assertEqual({'added': 1, 'partial': True},
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
