"""索引与存量清理之间的互斥(kb/maintenance.py)。

清理是「删 vault 文件 + 删 documents 行」,索引是「读磁盘上有什么 → 按它写
kb.db」。两边不互斥的话,一轮在清理之前开扫的索引会拿删除**之前**读到的那个
Doc 完成 upsert —— 库里留下一行指向不存在文件的 documents:搜得到、点开没有
原文,而清理那一侧已经收尾、重跑也找不到它(2026-10-01 审查实测 `documents=1`、
全文检索命中 1)。写 kb.db 的入口有三条(管道 index 阶段、`kb index`、条目化的
就地入库)各是一条路,跨进程那一半只能用真的子进程验。

写 vault 文件那一种形状(提取的 tag 回写、软广补判)在 `test_writeback_lock.py`
—— 两边挤在一个文件里会越过 700 行上限。
"""
import argparse
import struct
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from kb import cli, embedder, maintenance, pipeline_stages, scanner, store
from kb.config import EMBEDDING_DIM, Source
from kb.feeds import sync
from kb.pipeline_state import _state

# setUp 会把批量编码换成桩(真跑要加载 bge-m3),验「孤儿向量」的那条用例得把
# 真的那一份要回来 —— 它要的正是 embed_pending_chunks 自己那个「先读快照、再
# 分批写回」的形状,只替掉最底层的模型调用。
_real_embed = embedder.embed_pending_chunks

from feed_trim_fixture import TrimFixture, trim  # noqa: E402

# 另一个进程持着维护锁。flock 认的是 open file description,所以只有真子进程
# 才证明得了跨进程互斥(同进程换个 fd 的话重入保护会直接放行)。
HOLDER = textwrap.dedent('''
    import fcntl, sys, time
    fh = open(sys.argv[1], 'a+')
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    print('locked', flush=True)
    time.sleep(120)
''')


class IndexLockTests(TrimFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for p in (patch.object(scanner, 'VAULT_ROOT', self.vault),
                  patch.object(scanner, 'SOURCES',
                               [Source(name='rss', root=self.vault / 'source' / 'rss')]),
                  # 向量编码不在这几条用例的射程里(它在锁内的理由见 kb.maintenance),
                  # 真跑会去加载 bge-m3。
                  patch.object(embedder, 'embed_pending_chunks', lambda *a: 0)):
            p.start(); self.addCleanup(p.stop)
        self.stages = []
        p = patch.object(pipeline_stages, '_stage',
                         side_effect=lambda *a, **k: self.stages.append(a))
        p.start(); self.addCleanup(p.stop)
        was = _state.get('stop_requested')
        self.addCleanup(lambda: _state.__setitem__('stop_requested', was))
        _state['stop_requested'] = False

    def stage(self, status):
        return [s for s in self.stages if s[0] == 'index' and s[1] == status]

    def hold_the_lock_in_another_process(self):
        child = subprocess.Popen([sys.executable, '-c', HOLDER,
                                  str(self.root / maintenance.LOCK_NAME)],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.terminate)
        self.addCleanup(child.stdout.close)
        self.assertEqual('locked', child.stdout.readline().strip())
        return child

    def orphan_vectors(self):
        """指向已经不存在的 chunk 的向量行。

        `chunk_vec` 是 sqlite_vec 的虚表、没有外键级联,`remove_missing_documents`
        只按**当前** chunks 的 id 删它 —— 所以这种行再跑多少轮索引也收不掉,
        还照样占着语义检索的候选名额(「孤儿向量 0」是生产审计在看的不变量)。
        """
        conn = store.connect()
        try:
            return conn.execute('SELECT count(*) FROM chunk_vec WHERE chunk_id '
                                'NOT IN (SELECT id FROM chunks)').fetchone()[0]
        finally:
            conn.close()

    def wait_until_waiting(self, stage='index', timeout=5.0):
        """等到面板上出现这个阶段的第一条「等维护锁」为止。

        那条提示是真的等上了才出的(第一次立刻出),所以它是「已经在排队」这件
        事唯一的外部信号 —— 拿 sleep 猜一个时长的话,机器一忙就变成在测运气。
        """
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if any('等维护锁' in (s[2] or '')
                   for s in self.stages if s[0] == stage and s[1] == 'running'):
                return True
            time.sleep(0.02)
        return False

    # ---- 管道的 index 阶段 ----
    def test_an_index_pass_cannot_resurrect_what_the_cleanup_deleted(self):
        """清理持锁期间起跑的索引,一个字节都不许读、更不许写。

        审查那次的复现:索引在清理动手之前读到了那个 Doc,清理把三处删干净、
        标记计划完成之后,索引才拿着那份旧快照 upsert 回去 —— `documents=1`、
        FTS 命中 1,而 vault 里的文件已经不存在;重跑清理也只会说「无事可做」
        (feeds 行早没了)。所以 scan 必须和写库在同一把锁里:只圈写的那几句
        的话,旧快照照样进得来。
        """
        source = self.legacy(self.sub())
        item = self.seed(source)
        real_scan, real_backup = scanner.scan_all, trim._backup
        scanned, release = threading.Event(), threading.Event()

        def paused_scan():
            docs = list(real_scan())      # 读:这一刻磁盘上的事实
            scanned.set()
            self.assertTrue(release.wait(10))
            yield from docs               # 写:可能已经是删除之后了

        # 持锁期间的观察先记下来、不当场断言:断在这里会把清理从中间掐断,
        # 复活之后库里长什么样就看不到了 —— 那恰恰是审查报告里的那份证据。
        seen = {}

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = None

            def backup_then_start_index(*a):
                nonlocal future
                out = real_backup(*a)                 # 此刻清理持着锁
                future = pool.submit(pipeline_stages._run_index)
                # 它必须卡在锁上:这一秒里既不该开扫,面板上也该出声说在等
                seen['scanned'] = scanned.wait(1.0)
                seen['told'] = any('等维护锁' in (s[2] or '')
                                   for s in self.stage('running'))
                return out

            try:
                with patch.object(scanner, 'scan_all', paused_scan), \
                     patch.object(trim, '_backup', side_effect=backup_then_start_index):
                    self.assertEqual(0, trim.cmd_run(self.args(source)))
                    self.assertEqual(0, self.state(item)['documents'])
            finally:
                release.set()
            if future is not None:
                future.result(timeout=20)

        self.assertFalse(seen['scanned'], '索引在清理持锁期间就开扫了')
        self.assertTrue(seen['told'], '等锁期间面板上一声不响')
        done = self.state(item)
        self.assertEqual((False, False, 0, 0), (done['feed_row'], done['file'],
                                                done['documents'], done['searchable']))
        self.assertTrue(self.stage('ok'), f'索引阶段没有正常收尾: {self.stages}')
        self.assertFalse(self.documents_without_a_file(), '留下了指向不存在文件的 documents 行')

    def test_an_index_pass_after_a_cleanup_leaves_no_document_for_a_missing_file(self):
        """清理之后那一轮索引:把已删文档收掉,不许留下指向不存在文件的行。"""
        source = self.legacy(self.sub())
        item = self.seed(source)
        self.assertEqual(0, trim.cmd_run(self.args(source)))
        pipeline_stages._run_index()
        self.assertTrue(self.stage('ok'))
        self.assertFalse(self.documents_without_a_file())
        self.assertEqual(0, self.state(item)['searchable'])

    def test_the_index_stage_gives_up_instead_of_waiting_forever(self):
        """等不到锁就不做这一轮,并且什么都没写。

        无限等更坏:管道是单例,一个卡住的持锁者会把面板上的同步、定时同步和
        日报一起冻死;而索引漏跑一轮下一轮自己就补上了。
        """
        source = self.legacy(self.sub())
        item = self.seed(source)
        (self.vault / item['path']).unlink()          # 清理删了文件,行还在
        self.hold_the_lock_in_another_process()
        with patch.object(maintenance, 'WAIT_SECONDS', 0.5):
            pipeline_stages._run_index()
        self.assertTrue(self.stage('error'))
        self.assertIn('索引没做', self.stage('error')[0][2])
        self.assertEqual(1, self.state(item)['documents'])   # 一个字节都没动

    def test_a_stop_request_already_in_place_skips_the_wait(self):
        """停止早就按下了的话,这一轮压根不该去排队等锁。"""
        self.hold_the_lock_in_another_process()
        _state['stop_requested'] = True
        with patch.object(maintenance, 'WAIT_SECONDS', 30):
            pipeline_stages._run_index()
        self.assertTrue(self.stage('stopped'))
        self.assertFalse(self.stage('error'))

    def test_a_stop_pressed_after_the_wait_began_takes_effect_at_once(self):
        """等上了之后才按的那一下,也不该等到下一条「还在等」才被看见。

        取消检查原来和那条低频提示挂在同一个回调上(最多 `NOTIFY_EVERY`=60 秒
        一条),于是等锁期间按下的停止最坏要一分钟后才生效 —— 审查实测把等待
        上限缩成 2 秒之后,那一轮等满超时、落的是 `error / 索引没做`,而不是
        `stopped`(2026-10-01 第 1 轮)。所以这里**不动** `NOTIFY_EVERY`:
        第一条提示是立刻出的,第二条要 60 秒后,而等待上限只有 3 秒。
        """
        self.hold_the_lock_in_another_process()
        with patch.object(maintenance, 'WAIT_SECONDS', 3):
            worker = threading.Thread(target=pipeline_stages._run_index)
            worker.start()
            self.assertTrue(self.wait_until_waiting(), '它压根没等上')
            _state['stop_requested'] = True
            began = time.monotonic()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        took = time.monotonic() - began
        self.assertTrue(self.stage('stopped'), f'没有当场停下: {self.stages}')
        self.assertFalse(self.stage('error'))
        self.assertLess(took, 2.0, '停止是等到超时才收场的,不是当场生效')

    def test_a_stop_landing_right_as_the_lock_frees_up_still_counts(self):
        """停止正好落在两次轮询之间、锁紧接着就松了 —— 这一下也不许被吃掉。

        `_acquire` 的循环是「试 flock → 没拿到才问一次要不要放弃」,所以锁在这
        两步之间松开的话,下一轮直接 return、再也不问。拿到锁之后不补一次检查
        的话,这一轮会照常索引到底(按钮上写的是「本阶段结束后停止」,而这一轮
        的本阶段还没开始做事)。
        """
        source = self.legacy(self.sub())
        item = self.seed(source)
        (self.vault / item['path']).unlink()          # 真做了索引就会收掉这行
        holder = self.hold_the_lock_in_another_process()
        with patch.object(maintenance, 'WAIT_SECONDS', 30):
            worker = threading.Thread(target=pipeline_stages._run_index)
            worker.start()
            self.assertTrue(self.wait_until_waiting(), '它压根没等上')
            _state['stop_requested'] = True        # 先按停止
            holder.terminate(); holder.wait()      # 紧接着锁就松了
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertTrue(self.stage('stopped'), f'没有停下: {self.stages}')
        self.assertEqual(1, self.state(item)['documents'])   # 一个字节都没动

    def test_an_idle_lock_does_not_turn_a_stop_request_into_a_skipped_index(self):
        """反过来:锁空着的时候,一个早已按下的停止不该把这一轮索引顶掉。

        「等过锁」才补那次检查,就是为了这条 —— 按钮承诺的是「当前阶段结束后
        停止」,不是取消刚开跑的这个阶段;无条件检查会让管道编排刚放进 index
        的那一轮当场变成 stopped。
        """
        source = self.legacy(self.sub())
        item = self.seed(source)
        (self.vault / item['path']).unlink()
        _state['stop_requested'] = True
        pipeline_stages._run_index()
        self.assertTrue(self.stage('ok'), f'索引被停止请求顶掉了: {self.stages}')
        self.assertEqual(0, self.state(item)['documents'])

    def test_the_vector_pass_is_inside_the_lock(self):
        """补向量也在锁里 —— 这不是顺手圈进来的,挪出去会留下孤儿向量行。

        `embed_pending_chunks` 先把「还没有向量的 chunk」整份读出来,再分批写回
        `chunk_vec`;而 `chunk_vec` 是 sqlite_vec 的虚表,没有外键级联,清理在
        这中间删掉那些 chunk 的话后面几批会按已经不存在的 chunk id 插回向量,
        谁也不会再来收(「孤儿向量 0」是生产审计在看的一条不变量)。
        """
        self.seed(self.legacy(self.sub()))
        held = []
        with patch.object(embedder, 'embed_pending_chunks',
                          lambda *a: held.append(getattr(maintenance._held, 'on', False)) or 0):
            pipeline_stages._run_index()
        self.assertEqual([True], held)

    # ---- 条目化的就地入库 ----
    def test_project_ingestion_cannot_write_vectors_for_deleted_chunks(self):
        """条目化入库那条路也得认这把锁 —— 它补的是**全库**待编码的 chunk。

        审查那次的复现(2026-10-01 第 1 轮):清理持着锁跑,「整理工具」阶段的
        就地入库并行开跑、读到了清理即将删掉的那批 chunk;清理把文件、
        documents、chunks 全删干净之后编码才返回,151 行向量照样插进
        `chunk_vec` —— 那张虚表没有外键级联,下一轮索引也收不掉它们(没有待
        编码 chunk 就直接返回,`remove_missing_documents` 只按当前 chunks 的 id
        删),于是这批更近的孤儿向量占满语义检索的候选名额,清理之后**仍然留着**
        的文章从此召不回来。

        这条路一个新条目都没有时也会调 `embed_pending_chunks`,所以不必摆条目
        文件:要复现的就是「本轮没有新条目,却照样给全库补向量」。
        """
        source = self.legacy(self.sub())
        self.seed(source)                     # 这条 chunk 还没有向量
        real_backup = trim._backup
        encoding, release = threading.Event(), threading.Event()

        def fake_encode(texts, _batch):
            """站在「快照已经读完、正在编码」这一刻。只替掉模型本身。"""
            encoding.set()
            self.assertTrue(release.wait(10))
            one = struct.pack(f'{EMBEDDING_DIM}f', *([0.1] * EMBEDDING_DIM))
            return EMBEDDING_DIM, one * len(texts)

        seen, out = {}, {}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = None

            def backup_then_start_ingest(*a):
                nonlocal future
                res = real_backup(*a)                 # 此刻清理持着锁
                future = pool.submit(pipeline_stages._ingest_pretagged_sources)
                seen['encoding'] = encoding.wait(1.0)
                seen['told'] = any('等维护锁' in (s[2] or '') for s in self.stages
                                   if s[0] == 'projects' and s[1] == 'running')
                return res

            try:
                with patch.object(embedder, 'embed_pending_chunks', _real_embed), \
                     patch.object(embedder, '_encode', fake_encode), \
                     patch.object(embedder, 'shutdown', lambda: None), \
                     patch.object(trim, '_backup', side_effect=backup_then_start_ingest):
                    self.assertEqual(0, trim.cmd_run(self.args(source)))
            finally:
                release.set()
            if future is not None:
                out['result'] = future.result(timeout=20)

        # 末态先断:孤儿向量那一行就是审查报告里的那份证据,而持锁期间的两条
        # 观察断在它前面的话,注入旧行为时红的是过程、看不见后果。
        self.assertEqual(0, self.orphan_vectors(), '留下了指向已删 chunk 的向量行')
        self.assertFalse(self.documents_without_a_file())
        self.assertEqual((0, 0), out['result'])   # 没有新条目,也没有待编码 chunk
        self.assertFalse(seen['encoding'], '入库在清理持锁期间就读走快照开编了')
        self.assertTrue(seen['told'], '等锁期间面板上一声不响')

    def test_project_ingestion_gives_up_instead_of_waiting_forever(self):
        """等不到锁同样是「什么都没做」,面板上写明原因而不是一句裸异常。"""
        from kb.maintenance import LockBusy
        self.hold_the_lock_in_another_process()
        with patch.object(maintenance, 'WAIT_SECONDS', 0.5):
            with self.assertRaises(LockBusy):
                pipeline_stages._ingest_pretagged_sources()

    def test_the_projects_stage_says_why_instead_of_leaking_the_exception(self):
        """等不到锁 / 等锁期间被停止,面板上各有一句说法。

        不分开接的话两种都落进那句笼统的「入库失败」,而它们的含义正相反:
        一个是「别人占着锁,这一轮没做」,一个是「你自己按了停止」。
        """
        from kb.maintenance import LockBusy
        done = subprocess.CompletedProcess([], 0, stdout='', stderr='')
        for boom, status, text in (
                (LockBusy('占着呢'), 'error', '新条目没入库'),
                (pipeline_stages._StopWhileWaiting(), 'stopped', '已请求停止')):
            self.stages.clear()
            with patch.object(pipeline_stages, '_run_export_script',
                              return_value=done), \
                 patch.object(pipeline_stages, '_ingest_pretagged_sources',
                              side_effect=boom):
                pipeline_stages._run_projects()
            said = [s for s in self.stages if s[0] == 'projects' and s[1] == status]
            self.assertTrue(said, f'{status} 没落下: {self.stages}')
            self.assertIn(text, said[-1][2])

    # ---- 命令行那条入口 ----
    def test_the_cli_index_waits_for_a_cleanup_in_another_process(self):
        """`kb index` 是独立进程,也得认这把锁。

        认不出来的话,运维脚本正在删的那批文档会被一条手敲的 `kb index` 按
        删除之前的内容写回去 —— 和管道那条路一模一样的后果。
        """
        source = self.legacy(self.sub())
        item = self.seed(source)
        (self.vault / item['path']).unlink()
        args = argparse.Namespace(reembed=False, no_vector=True)
        holder = self.hold_the_lock_in_another_process()
        with patch.object(maintenance, 'WAIT_SECONDS', 0.5):
            self.assertEqual(2, cli.cmd_index(args))
        self.assertEqual(1, self.state(item)['documents'])   # 没写库
        holder.terminate(); holder.wait()
        self.assertEqual(0, cli.cmd_index(args))             # 锁松了照样做完
        self.assertEqual(0, self.state(item)['documents'])
        self.assertFalse(self.documents_without_a_file())

    # ---- 清理之后同步照旧,而且不抱死 ----
    def test_a_full_sync_still_runs_after_a_cleanup(self):
        """清理收尾之后导出和索引都该照常走;同线程重入也不许锁死自己。

        `_run_index` 可能被一段已经持着锁的代码调起(它自己就在 `cmd_run` 的
        锁里跑过一次续跑),flock 认 open file description —— 同进程换个 fd
        再锁会把自己锁死。
        """
        source = self.legacy(self.sub())
        self.seed(source)
        self.assertEqual(0, trim.cmd_run(self.args(source)))
        sync.export('rss')                                   # 导出也取这把锁
        pipeline_stages._run_index()
        self.assertTrue(self.stage('ok'))
        fresh = self.seed(self.legacy(self.sub('Review B', 'https://example.org/b.xml')),
                          self.entry(url='https://example.org/brand-new',
                                     published='2026-10-01 08:00'))
        with maintenance.maintenance_lock():                 # 嵌套:不该抱死
            pipeline_stages._run_index()
        self.assertEqual(1, self.state(fresh)['documents'])
        self.assertFalse(self.documents_without_a_file())


if __name__ == '__main__':
    unittest.main()
