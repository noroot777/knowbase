"""索引与存量清理之间的互斥(kb/maintenance.py)。

清理是「删 vault 文件 + 删 documents 行」,索引是「读磁盘上有什么 → 按它写
kb.db」。两边不互斥的话,一轮在清理之前开扫的索引会拿删除**之前**读到的那个
Doc 完成 upsert —— 库里留下一行指向不存在文件的 documents:搜得到、点开没有
原文,而清理那一侧已经收尾、重跑也找不到它(2026-10-01 审查实测 `documents=1`、
全文检索命中 1)。两条索引入口(管道 index 阶段、`kb index`)各是一条路,
跨进程那一半只能用真的子进程验。
"""
import argparse
import subprocess
import sys
import textwrap
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from kb import cli, embedder, maintenance, pipeline_stages, scanner, store
from kb.config import Source
from kb.feeds import sync
from kb.pipeline_state import _state

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

    def documents_without_a_file(self):
        conn = store.connect()
        try:
            return [r['path'] for r in conn.execute('SELECT path FROM documents')
                    if not (self.vault / r['path']).exists()]
        finally:
            conn.close()

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

    def test_a_stop_request_ends_the_wait(self):
        """等锁期间按下的「停止」不该等到锁拿到手才生效。"""
        self.hold_the_lock_in_another_process()
        _state['stop_requested'] = True
        with patch.object(maintenance, 'WAIT_SECONDS', 30):
            pipeline_stages._run_index()
        self.assertTrue(self.stage('stopped'))
        self.assertFalse(self.stage('error'))

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
        item = self.seed(source)
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
