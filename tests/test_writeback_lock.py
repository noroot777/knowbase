"""提取的 tag 回写与存量清理之间的互斥(kb/maintenance.py)。

这一组回归的是**写 vault 文件**那种形状,和 `test_index_lock.py` 里写 kb.db 的
那几条互为补集(两边挤在一个文件里会越过 700 行上限;「一条旧文同时落在
feeds.db / vault / kb.db 三处」那份摆盘仍然共用 `feed_trim_fixture.py`)。

`writeback_tags` 是「`read_text` 拿快照 → 过一会儿 `write_text` 整份写回」——
清理在这中间把这一篇从三处删干净的话,那次写回会把**文件重新创建出来**,下一轮
索引合法地把它收回库里,而 feeds.db 里已经没有这一条,重跑清理只会说「无事可做」:
比 index 那条路更糟,那一条下一轮 `remove_missing_documents` 还收得掉。

另一半是**锁序**:回写整段在维护锁里,而 SQLite 的写锁不认那把锁 —— 攥着一个
没提交的写事务去等锁,持锁的清理就卡在自己的 DELETE 上,两边互等到清理报
`database is locked`。
"""
import argparse
import contextlib
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from kb import cli, embedder, extract, maintenance, pipeline_stages, scanner, store
from kb.config import Source
from kb.pipeline_state import _state

from feed_trim_fixture import ROOT, TrimFixture, trim  # noqa: E402


class WritebackLockTests(TrimFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for p in (patch.object(scanner, 'VAULT_ROOT', self.vault),
                  patch.object(scanner, 'SOURCES',
                               [Source(name='rss', root=self.vault / 'source' / 'rss')]),
                  patch.object(embedder, 'embed_pending_chunks', lambda *a: 0)):
            p.start(); self.addCleanup(p.stop)
        self.stages = []
        p = patch.object(pipeline_stages, '_stage',
                         side_effect=lambda *a, **k: self.stages.append(a))
        p.start(); self.addCleanup(p.stop)
        was = _state.get('stop_requested')
        self.addCleanup(lambda: _state.__setitem__('stop_requested', was))
        _state['stop_requested'] = False

    EXTRACTED = {'entities': [{'name': 'Review A', 'kind': 'org'}],
                 'topics': ['AI编程'], 'summary': 's'}
    JUDGED = {'level': 'hard', 'reason': '整篇买下来卖一个产品'}

    def seed_gzh(self, path='source/dr-gzh/promo-me.md'):
        """一篇待补判的公众号文章(软广补判只认 dr-gzh/gzh 这两个来源)。

        它**不在**这一轮清理的待删集合里 —— 复现要的就是「两批文章不同、
        共享同一个 kb.db」:数据库写锁是跨文章的。
        """
        body = '扫码注册领取福利,阅读原文直达。' * 12
        full = self.vault / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(f'---\ntitle: 补判目标\n---\n{body}', encoding='utf-8')
        conn = store.connect()
        # content_hash 照索引那条真路径算(`scanner.file_hash`):留空等于摆出
        # 一个「索引还没扫到当前这份 md」的状态,`save_extraction` 的正文版本
        # 校验会把这次提取如实作废 —— 这条用例测的是锁序,不是那件事。
        conn.execute('INSERT INTO documents(path,source,title,published,indexed_at,'
                     "extracted_at,promo_level,content_hash) VALUES(?,?,?,?,?,?,'',?)",
                     (path, 'dr-gzh', '补判目标', '2026-09-01 08:00',
                      '2026-10-01 09:00:00', '2026-10-01 09:00:00',
                      scanner.file_hash(full.read_text(encoding='utf-8'))))
        conn.execute('INSERT INTO chunks(path,seq,text) VALUES(?,?,?)',
                     (path, 0, body))
        conn.commit(); conn.close()
        return path

    def a_writeback_racing_a_cleanup(self, runner):
        """清理持锁期间起跑一轮带回写的提取,返回(持锁期间的观察, 那条旧文)。

        回写必须整段卡在锁上:读也不许读 —— 读到了就等于手里攥着一份删除**之前**
        的全文快照,等锁拿到手再写回去,文件就回来了。
        """
        source = self.legacy(self.sub())
        item = self.seed(source)
        target = self.vault / item['path']
        real_backup, real_write = trim._backup, Path.write_text
        writing, release = threading.Event(), threading.Event()

        def gated_write(who, data, **kw):
            # 只卡这一篇的回写;清理自己的备份/进度文件照常写。站在「快照已经
            # 读完、正要写回」这一刻 —— 审查那次的复现就卡在这里。
            if Path(who) == target:
                writing.set()
                self.assertTrue(release.wait(10))
            return real_write(who, data, **kw)

        seen = {}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = None

            def backup_then_start_extract(*a):
                nonlocal future
                out = real_backup(*a)                 # 此刻清理持着锁
                future = pool.submit(runner)
                seen['writing'] = writing.wait(1.0)
                return out

            try:
                with patch.object(extract, 'VAULT_ROOT', self.vault), \
                     patch.object(extract, 'extract_with_llm',
                                  lambda *a, **k: dict(self.EXTRACTED)), \
                     patch.object(Path, 'write_text', gated_write), \
                     patch.object(trim, '_backup',
                                  side_effect=backup_then_start_extract):
                    self.assertEqual(0, trim.cmd_run(self.args(source)))
                    # **被 race 的那个线程要在打桩还生效的时候跑完**:它这时候
                    # 才刚拿到锁,放到 with 外面等的话回写读的是**生产**
                    # `VAULT_ROOT`,`_still_indexed` 当然说「这篇不在」——绿是绿了,
                    # 可那和修没修对没关系(第 3 轮自查发现)。
                    release.set()
                    if future is not None:
                        future.result(timeout=20)
            finally:
                release.set()          # cmd_run 抛了的话别把那个线程吊死
        return seen, item

    def assert_the_cleanup_stuck(self, seen, item):
        """末态先断:文件有没有被重新创建出来,才是审查报告里的那份证据。

        过程观察(「回写在持锁期间就动手了」)断在它前面的话,注入旧行为时红的
        是过程、看不见后果 —— 而后果正是「索引合法地把旧文收回库里」。
        """
        after = self.state(item)
        self.assertFalse(after['file'], '回写把清理删掉的 vault 文件重建出来了')
        pipeline_stages._run_index()                  # 下一轮索引:不该有东西可收
        done = self.state(item)
        self.assertEqual((False, 0, 0), (done['file'], done['documents'],
                                         done['searchable']))
        self.assertFalse(self.documents_without_a_file())
        self.assertFalse(seen['writing'], '回写在清理持锁期间就把文件写回去了')

    def test_the_cli_writeback_cannot_recreate_a_file_the_cleanup_deleted(self):
        """`kb extract --writeback`:清理删掉的那一篇不许被回写重建出来。"""
        args = argparse.Namespace(limit=0, writeback=True)
        seen, item = self.a_writeback_racing_a_cleanup(lambda: cli.cmd_extract(args))
        self.assert_the_cleanup_stuck(seen, item)

    def test_the_pipeline_writeback_cannot_recreate_a_file_the_cleanup_deleted(self):
        """同步的并行存量提取走的是另一条路(`_extract_pass`),同样不许重建。"""
        # 用生产那个构造器,别在用例里手抄一份键集合:`_extract_pass` 以后
        # 多记一个维度时,手抄的那份会在它第一次被写到的时候 KeyError。
        stats = pipeline_stages._new_extract_stats()
        seen, item = self.a_writeback_racing_a_cleanup(
            lambda: pipeline_stages._extract_pass('存量', stats))
        self.assert_the_cleanup_stuck(seen, item)
        # 文章是用户主动清理掉的,不是一次故障:回写什么都没做要返回 False,
        # 不能抛异常 —— `_extract_pass` 把回写和 save_extraction 包在同一个
        # try 里,抛出去就是把这一篇记成提取失败、面板标红。
        self.assertEqual((1, set()), (stats['ok'], stats['failed_paths']))

    def script(self, name):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            name, ROOT / 'scripts' / f'{name}.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_every_writeback_caller_commits_before_taking_the_lock(self):
        """**四个**调用方调回写时,共享连接都不许攥着一个没提交的写事务。

        回写整段要取维护锁,而 **SQLite 的写锁不认那把锁**:攥着 save_promo /
        save_extraction 留下的写事务去等锁,持锁的清理就卡在自己的 DELETE 上,
        两边互相等到清理报 `database is locked`。两条提取路径本来就是先提交
        再回写,`extract_parallel.py` 是第 2 轮跟着改的,**软广补判那条是第 3 轮
        审查抓出来的**:它经 `sync_promo_tag` 进来,而上一版这条用例只枚举了
        三条提取路径,于是 16 项定向回归和全量测试全绿照过。
        """
        ep, pb = self.script('extract_parallel'), self.script('promo_backfill')
        args = argparse.Namespace(limit=0, writeback=True)
        stats = pipeline_stages._new_extract_stats()
        # 打桩点分两处:模型在各自那条路自己的模块里,回写则在它**实际被调到**
        # 的那个模块里 —— 四条路现在都经协调入口转发(提取三条走
        # `extract.commit_extraction`、补判走 `extract_promo.commit_promo`),
        # 所以回写一律 patch 在 `extract` 上(各脚本自己压根不再 import 它)。
        for i, (label, runner, mwhere, model, bwhere, back) in enumerate((
                ('kb extract --writeback', lambda: cli.cmd_extract(args),
                 extract, 'extract_with_llm', extract, 'writeback_extraction'),
                ('管道存量提取', lambda: pipeline_stages._extract_pass('存量', stats),
                 extract, 'extract_with_llm', extract, 'writeback_extraction'),
                ('extract_parallel.py', ep.main, ep, 'extract_with_llm',
                 extract, 'writeback_extraction'),
                ('promo_backfill.py',
                 lambda: pb.cmd_run(argparse.Namespace(limit=0, dry_run=False)),
                 pb, 'judge_promo', extract, 'writeback_extraction'))):
            with self.subTest(label):
                self.seed(self.legacy(self.sub(label, f'https://example.org/w{i}.xml')),
                          self.entry(url=f'https://example.org/old-w{i}'))
                self.seed_gzh(f'source/dr-gzh/w{i}.md')
                seen = []
                # VAULT_ROOT 也得指到 tmp:协调入口在锁内会先问一次「这一篇
                # 还在吗」(`_still_indexed`),照生产 vault 问的话它说"不在"、
                # 回写压根不会被调到 —— 摆出来的就是一个生产里不存在的状态
                # (documents 行指着 VAULT_ROOT 下没有的文件)。
                with patch.object(extract, 'VAULT_ROOT', self.vault), \
                     patch.object(mwhere, model, lambda *a, **k: dict(self.JUDGED)
                                  if model == 'judge_promo' else dict(self.EXTRACTED)), \
                     patch.object(bwhere, back,
                                  lambda conn, *a, **kw: seen.append(conn.in_transaction)):
                    runner()
                # 补判那一轮会把前几格摆下的 dr-gzh 文档一起判掉(它按
                # `promo_level=''` 捞全库),所以数量不定、按集合判
                self.assertTrue(seen, f'{label}:压根没调到回写')
                self.assertEqual({False}, set(seen), f'{label}:回写之前没有提交')

    def test_the_shared_writeback_flushes_before_it_queues_for_the_lock(self):
        """就算调用方忘了提交,共用入口自己也得先 flush 再去排队。

        上面那条钉的是四个调用点的时序,这一条钉的是**共用入口自己**:锁序
        正确性不该取决于「每个调用方都记得先提交」—— 第 3 轮漏掉的正是其中
        一个,而回归按名单枚举,名单本身就是那个缺口。
        """
        source = self.legacy(self.sub())
        item = self.seed(source)
        conn = store.connect()
        self.addCleanup(conn.close)
        conn.execute("UPDATE documents SET promo_level='hard' WHERE path=?",
                     (item['path'],))
        self.assertTrue(conn.in_transaction, '摆盘没摆出一个开着的写事务')

        seen, real_lock = {}, maintenance.maintenance_lock

        @contextlib.contextmanager
        def watched(*a, **kw):
            seen['in_txn'] = conn.in_transaction      # 就要去排队了
            with real_lock(*a, **kw):
                yield

        with patch.object(extract, 'VAULT_ROOT', self.vault), \
             patch.object(maintenance, 'maintenance_lock', watched):
            extract.sync_promo_tag(conn, item['path'], 'hard')
        self.assertIn('in_txn', seen, '压根没去取锁')
        self.assertFalse(seen['in_txn'], '带着没提交的写事务去等维护锁了')
        self.assertFalse(conn.in_transaction)

    def test_the_commit_entry_flushes_before_it_queues_for_the_lock(self):
        """协调入口自己也得先 flush 再去排队 —— 它比回写更早取那把锁。

        上面那条钉的是 `writeback_extraction`;提取那三条路现在经
        `commit_extraction` 进来,取锁的那一下**提前到了落库之前**,所以这道
        锁序关口得自己有一条。攥着一个没提交的写事务去等维护锁,持锁的清理
        就卡在它自己的 DELETE 上(SQLite 的写锁不认那把锁)。
        """
        source = self.legacy(self.sub())
        item = self.seed(source)
        conn = store.connect()
        self.addCleanup(conn.close)
        seen_body = extract.body_fingerprint(store.doc_body(conn, item['path']))
        conn.execute("UPDATE documents SET summary='x' WHERE path=?", (item['path'],))
        self.assertTrue(conn.in_transaction, '摆盘没摆出一个开着的写事务')

        seen, real_lock = {}, maintenance.maintenance_lock

        @contextlib.contextmanager
        def watched(*a, **kw):
            seen.setdefault('in_txn', conn.in_transaction)   # 就要去排队了
            with real_lock(*a, **kw):
                yield

        with patch.object(extract, 'VAULT_ROOT', self.vault), \
             patch.object(maintenance, 'maintenance_lock', watched):
            extract.commit_extraction(conn, item['path'], dict(self.EXTRACTED),
                                      seen_body)
        self.assertIn('in_txn', seen, '压根没去取锁')
        self.assertFalse(seen['in_txn'], '带着没提交的写事务去等维护锁了')
        self.assertFalse(conn.in_transaction)

    def test_a_promo_backfill_does_not_make_a_cleanup_fail_halfway(self):
        """清理持锁期间起跑的软广补判,不许把清理卡死在它自己的 DELETE 上。

        审查那次的复现(2026-10-01 第 3 轮,跨进程 32.5 秒):补判在 `save_promo`
        之后、提交之前就去取维护锁,形成「SQLite 写事务 → 维护锁」,而清理是
        「维护锁 → SQLite 写事务」—— 两边互等到 SQLite 的 busy 超时把清理打断,
        清理报 `database is locked` 退出,feeds 行和 md 都删了、索引那一步没做完:
        **搜索里还留着刚删掉的旧文**(实测 `documents=1`、全文命中 1),要再跑
        一次清理才收得干净。

        busy 超时在这里缩到 2 秒只为让坏版本快点红:修好之后补判在取锁之前就
        已经提交,和清理的 DELETE 压根没有重叠,缩短它不会造出假绿。
        """
        source = self.legacy(self.sub())
        item = self.seed(source)                       # 要被清掉的那篇 RSS 旧文
        kept = self.seed_gzh()                         # 补判目标,另一篇、留着
        pb = self.script('promo_backfill')
        real_backup, real_connect = trim._backup, store.connect
        real_lock, main = maintenance.maintenance_lock, threading.current_thread()
        queued = threading.Event()

        def impatient(*a, **kw):
            conn = real_connect(*a, **kw)
            conn.execute('PRAGMA busy_timeout = 2000')
            return conn

        @contextlib.contextmanager
        def watched(*a, **kw):
            if threading.current_thread() is not main:
                queued.set()        # 补判已经站到「要取锁」这个位置上了
            with real_lock(*a, **kw):
                yield

        seen, out = {}, {}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = None

            def backup_then_start_backfill(*a):
                nonlocal future
                res = real_backup(*a)                  # 此刻清理持着锁
                future = pool.submit(
                    pb.cmd_run, argparse.Namespace(limit=0, dry_run=False))
                seen['queued'] = queued.wait(10)
                return res

            with patch.object(store, 'connect', impatient), \
                 patch.object(maintenance, 'maintenance_lock', watched), \
                 patch.object(extract, 'VAULT_ROOT', self.vault), \
                 patch.object(pb, 'judge_promo', lambda *a, **k: dict(self.JUDGED)), \
                 patch.object(trim, '_backup',
                              side_effect=backup_then_start_backfill):
                try:
                    out['trim'] = trim.cmd_run(self.args(source))
                except Exception as e:
                    # 互等到 busy 超时时,清理是**抛**出来的(实测
                    # `OperationalError: database is locked`)。收下来而不是让它
                    # 冒上去:末态那条断言才是这条用例要给的证据,异常先把它顶掉
                    # 的话,注入旧版本看到的只是一个 ERROR。
                    out['trim'] = f'{type(e).__name__}: {e}'
                # 补判这时候才刚拿到锁,它那次回写必须还在打桩里跑(理由同
                # `a_writeback_racing_a_cleanup`)
                if future is not None:
                    out['backfill'] = future.result(timeout=30)

        # 末态先断 —— 「清理半途失败、搜索里还留着旧文」才是审查报告里那份证据
        done = self.state(item)
        self.assertEqual((False, False, 0, 0), (done['feed_row'], done['file'],
                                                done['documents'], done['searchable']),
                         '清理没收干净,旧文还留在库里')
        self.assertEqual(0, out['trim'], '清理没能正常收尾(互等到 busy 超时)')
        self.assertEqual(0, out['backfill'])
        self.assertTrue(seen['queued'], '补判压根没站到要取锁那个位置上')
        # 补判自己那一篇照样判了、标记也写进了 md
        conn = store.connect()
        self.addCleanup(conn.close)
        self.assertEqual('hard', conn.execute(
            'SELECT promo_level FROM documents WHERE path=?', (kept,)).fetchone()[0])
        self.assertIn(extract.PROMO_TAG, (self.vault / kept).read_text(encoding='utf-8'))

    def test_the_callers_of_the_shared_writeback_are_all_accounted_for(self):
        """全仓调用这条共用回写的地方就这几处 —— 多一处就得重新想一次锁序。

        第 3 轮那条缺陷的形态就是「名单之外还有一个调用方」,所以名单本身也要
        钉住:新加一处会在这里红,而不是在某次清理报 `database is locked` 时
        才被发现。

        **两个协调入口一并数进来**:提取那三条路经 `commit_extraction`、补判经
        `commit_promo`,它们自己就是「维护锁 → SQLite 写锁」这道锁序的关口。
        新开一个协调入口而忘了加进这条正则,等于把名单上的一处悄悄换成零处。

        **`align_promo_tag` 也数进来**:它是真正动 md 那一行的那一下(回写
        经它、`promo_backfill sync-tags` 那条修复路也经它),所以它的每个调用方
        都得自己回答一次「锁取了没有、取之前 flush 了没有」。
        """
        call = re.compile(
            r'\b(?:writeback_extraction|sync_promo_tag|align_promo_tag'
            r'|commit_extraction|commit_promo)\s*\(')
        found = {}
        for py in sorted(ROOT.glob('kb/*.py')) + sorted(ROOT.glob('scripts/*.py')):
            hits = [ln for ln in py.read_text(encoding='utf-8').splitlines()
                    if call.search(ln) and not ln.lstrip().startswith('def ')]
            if hits:
                found[py.relative_to(ROOT).as_posix()] = len(hits)
        self.assertEqual({'kb/cli.py': 1, 'kb/extract.py': 2,
                          'kb/extract_promo.py': 2,
                          'kb/pipeline_stages.py': 1,
                          'scripts/extract_parallel.py': 1,
                          'scripts/promo_backfill.py': 2}, found)


if __name__ == '__main__':
    unittest.main()
