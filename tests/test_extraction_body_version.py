# -*- coding: utf-8 -*-
"""提取结果和它基于的那份正文绑在一起 —— 对不上就作废(kb/extract.py)。

被测的那件事:提取是「读正文 → 等几十秒模型 → 落库」,而补抓把跳转壳换成全文
正是会在这中间整篇换掉正文的一条路。它还**刻意**把这一篇送回待提取集合,于是
「模型读走旧壳 → 期间正文换新 → 旧结论落库」从边角变成了常规时序。

**落库会把 `extracted_at` 设上,而那是永久的**:这一篇从此再也不进
`extract.pending_docs`,摘要、主题、实体永久停在那个壳上,再同步、再索引都不会
自己纠正(2026-10-09 第 2 轮审查两种交错时序各三次稳定复现)。

两种时序各一道判据(`extract._require_current_body`),**各有射程、缺一不可**:
- **导出完成、索引还没跟上**:提取读的是索引里那份正文(`store.doc_body` 拼的是
  chunks),而 md 已经是全文了。那一刻 chunks 压根没变,所以只能靠
  「`documents.content_hash` 对不上当前这份 md」认出来。
- **旧正文的提取请求在途、模型回包晚于全文更新**:中间那次 index 已经把 chunks
  换成新正文、`content_hash` 也跟着对上了文件,所以只能靠「索引里那份正文和我
  读到的不是同一份」认出来。

两种时序都是**落库被拒、这一篇留在队列里**,下一轮按新正文重提一次。

打桩只打在 HTTP 传输和模型回包上;读正文、落库、回写、导出、索引全部真跑 ——
打桩成"成功"正好绕开唯一要验的那件事。
"""
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kb import cli, config, extract, llm_stages, store
from kb.feeds import fulltext
from kb.extract import StaleBody
from tests.test_fulltext_extract_stale import OLD, StaleCase

NEW = {'entities': [{'name': 'Knowbase', 'kind': 'project'}], 'topics': ['ai-coding'],
       'summary': '讲的是一个给编码智能体调度工具的运行时。'}


class BodyVersionCase(StaleCase):
    """脚手架:`StaleCase` 那条已按壳导出+索引+提取的旧条目,外加真跑一轮 CLI 提取。"""

    def extract_cli(self, model):
        """走真的 `cli.cmd_extract(--writeback)`,只把模型那一步换掉。

        `store.connect()` 的默认参数在函数定义时就绑死了生产库,所以这里和被测
        代码一样把它指到 tmp —— 别的一行不打桩。
        """
        real = store.connect
        with patch.object(store, 'connect', lambda *a, **k: real(config.DB_PATH)), \
                patch.object(llm_stages, 'stage_model', Mock(return_value='stub')), \
                patch.object(extract, 'extract_with_llm', model):
            return cli.cmd_extract(SimpleNamespace(limit=100, writeback=True))

    def seen_model(self, data: dict):
        """一个记下"模型读到的正文"的假提取器。"""
        seen: list[str] = []

        def model(title, body, **kw):
            seen.append(body)
            return dict(data)
        return model, seen


class ExportIndexGapTests(BodyVersionCase):
    """时序 A:新 md 落了盘、`extracted_at` 已经清掉,索引还没扫到它。"""

    def test_an_extraction_of_the_stale_index_is_refused(self):
        key, rel = self.shell_in_the_vault()
        self.fetch_and_translate()
        # 这一篇已经排回待提取集合,而索引里还是那份壳 —— 旁路的一轮
        # `kb extract` 正好落在这个缝里。
        self.assertEqual([rel], self.state(rel)['pending'])
        model, seen = self.seen_model(OLD)
        self.assertEqual(0, self.extract_cli(model))
        # 后果先断言:旧壳的结论一个字都没写进去 —— 红的就是审查那份证据。
        gap = self.state(rel)
        self.assertIsNone(gap['extracted_at'])
        self.assertEqual('', gap['summary'])
        self.assertEqual(([], []), (gap['tags'], gap['entities']))
        self.assertEqual([], self.md_tags(rel))
        self.assertEqual([rel], gap['pending'])
        # 反过来钉一边:样本确实进来了 —— 模型真被调了一次,读到的正是那份壳
        # (不然"判据生效了"和"这一轮压根没提取"长得一模一样)。
        self.assertEqual(1, len(seen))
        self.assertIn('HN Points', seen[0])

    def test_the_same_round_succeeds_once_the_index_caught_up(self):
        """索引跟上之后,**同一轮**管道的提取阶段就能按新正文重提。

        管道顺序是 export → index → extract,所以正常路径一轮就走完,没有
        「补抓成功的文章要等下一次同步才有摘要」这回事。
        """
        _, rel = self.shell_in_the_vault()
        self.fetch_and_translate()
        self.index(rel)
        model, seen = self.seen_model(NEW)
        self.assertEqual(0, self.extract_cli(model))
        self.assertIn('这个运行时为编码智能体调度工具', seen[0])
        self.assertNotIn('HN Points', seen[0])
        after = self.state(rel)
        self.assertEqual(NEW['summary'], after['summary'])
        self.assertEqual(['Knowbase'], after['entities'])
        self.assertEqual(['entity/knowbase', 'topic/ai-coding'], after['tags'])
        self.assertEqual(['entity/knowbase', 'topic/ai-coding'], self.md_tags(rel))
        self.assertEqual([], after['pending'])


class InFlightExtractionTests(BodyVersionCase):
    """时序 B:旧正文的提取请求已经发出,模型回包晚于全文更新。"""

    def _race(self, data: dict):
        """让一轮真 CLI 提取停在模型调用里,期间走完补抓/翻译/导出/索引。"""
        key, rel = self.shell_in_the_vault()
        # 前提:这一篇正常排在待提取集合里(等价于上一轮作废之后的状态)。
        from kb.feeds import fulltext
        fulltext._drop_extraction([rel])
        self.assertEqual([rel], self.state(rel)['pending'])
        started, release, out, seen = threading.Event(), threading.Event(), {}, []

        def model(title, body, **kw):
            seen.append(body)
            started.set()
            if not release.wait(20):
                raise TimeoutError('模型回包没被放行')
            return dict(data)

        def run():
            try:
                out['code'] = self.extract_cli(model)
            except BaseException as e:          # noqa: BLE001
                out['error'] = repr(e)

        worker = threading.Thread(target=run, name='in-flight-extract')
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(20)))
        self.assertTrue(started.wait(15), out)
        self.fetch_and_translate()
        self.index(rel)
        mid = self.state(rel)
        release.set()
        worker.join(20)
        self.assertFalse(worker.is_alive(), '提取线程没结束')
        return key, rel, out, seen, mid

    def test_the_late_result_of_the_old_body_is_thrown_away(self):
        key, rel, out, seen, mid = self._race(OLD)
        # 后果先断言:旧结论一个字都没写回去 —— 红的就是审查那份证据。
        after = self.state(rel)
        self.assertIsNone(after['extracted_at'])
        self.assertEqual('', after['summary'])
        self.assertEqual(([], []), (after['tags'], after['entities']))
        self.assertEqual([], self.md_tags(rel))
        self.assertEqual([rel], after['pending'])
        # 反过来钉一边:样本确实进来了 —— 模型读到的是旧壳,而放行前新正文已经
        # 进了索引(不然"判据生效了"和"这一轮压根没交错"长得一模一样)。
        self.assertIn('HN Points', seen[0])
        self.assertTrue(mid['extracted_at'] is None and not mid['summary'])
        with self.kb() as c:
            self.assertIn('这个运行时为编码智能体调度工具', store.doc_body(c, rel))
        # CLI 正常收尾:这不是故障,是"这一篇留着下一轮重提"。
        self.assertEqual({'code': 0}, out)
        self.assertEqual(0, self.row(key)['extract_stale'])

    def test_the_next_round_extracts_the_new_body(self):
        """丢掉旧结果之后,这一篇必须仍然能按全文重新提取。"""
        _, rel, _, _, _ = self._race(OLD)
        model, seen = self.seen_model(NEW)
        self.assertEqual(0, self.extract_cli(model))
        self.assertIn('这个运行时为编码智能体调度工具', seen[0])
        after = self.state(rel)
        self.assertEqual(NEW['summary'], after['summary'])
        self.assertEqual(['entity/knowbase', 'topic/ai-coding'], after['tags'])
        self.assertEqual(['entity/knowbase', 'topic/ai-coding'], self.md_tags(rel))
        self.assertIsNotNone(after['extracted_at'])


class FingerprintTests(StaleCase):
    """指纹校验本身。"""

    def test_an_unchanged_body_is_written_as_usual(self):
        """反过来钉一边:正文没变时照常落库 —— 一律丢弃的实现会在这里红。"""
        _, rel = self.shell_in_the_vault()
        with self.kb() as c:
            seen = extract.body_fingerprint(store.doc_body(c, rel))
            extract.save_extraction(c, rel, NEW, seen)
            c.commit()
        self.assertEqual(NEW['summary'], self.state(rel)['summary'])

    def test_a_changed_body_raises_and_writes_nothing(self):
        _, rel = self.shell_in_the_vault()
        before = self.state(rel)
        with self.kb() as c:
            seen = extract.body_fingerprint('这不是库里那份正文')
            with self.assertRaises(StaleBody):
                extract.save_extraction(c, rel, NEW, seen)
            c.commit()
        self.assertEqual(before, self.state(rel))

    def test_a_stale_index_is_refused_even_though_the_chunks_never_moved(self):
        """①那一道的独占射程:md 比索引新,而 chunks 一个字没动。

        这就是时序 A 的形状 —— 指纹那一道(②)对它完全是瞎的,所以它必须有
        自己这一条用例。
        """
        _, rel = self.shell_in_the_vault()
        before = self.state(rel)
        with self.kb() as c:
            seen = extract.body_fingerprint(store.doc_body(c, rel))
        full = self.root / 'vault' / rel
        full.write_text(full.read_text() + '\n新正文来了。\n', encoding='utf-8')
        with self.kb() as c:
            # 反过来确认 chunks 真的没变:②判据此刻是通过的。
            self.assertEqual(seen, extract.body_fingerprint(store.doc_body(c, rel)))
            with self.assertRaises(StaleBody):
                extract.save_extraction(c, rel, NEW, seen)
            c.commit()
        self.assertEqual(before, self.state(rel))

    def test_a_replaced_body_is_refused_even_though_the_index_caught_up(self):
        """②那一道的独占射程:索引已经扫过当前这份 md,而 chunks 换过了。

        这就是时序 B 的形状 —— 索引滞后那一道(①)对它完全是瞎的。
        """
        _, rel = self.shell_in_the_vault()
        before = self.state(rel)
        with self.kb() as c:
            seen = extract.body_fingerprint(store.doc_body(c, rel))
        full = self.root / 'vault' / rel
        full.write_text(full.read_text().replace('HN Points', '整篇文章的正文在这里'),
                        encoding='utf-8')
        self.index(rel)                        # chunks 和 content_hash 一起更新
        with self.kb() as c:
            from kb.scanner import file_hash
            # 反过来确认①判据此刻是通过的。
            self.assertFalse(store.doc_needs_reindex(c, rel, file_hash(full.read_text())))
            with self.assertRaises(StaleBody):
                extract.save_extraction(c, rel, NEW, seen)
            c.commit()
        self.assertEqual(before['extracted_at'], self.state(rel)['extracted_at'])
        self.assertEqual(before['summary'], self.state(rel)['summary'])

    def test_a_vanished_markdown_file_is_left_to_the_writeback_guard(self):
        """md 已经不在了(清理正把这一篇删掉)**不算**"版本不对",照旧放行。

        这一档本来就有人管:`writeback_extraction` 锁内复查「这一篇还在吗」,
        落进库的判定随后被外键级联一并带走,末态干净。在版本校验这里改判成作废
        的代价是实打实的 —— 回写压根不会被调到,`test_writeback_lock` 那组
        「回写在锁里、读也不许读」对它就不再敏感了(把那把锁去掉也照样全绿)。
        这道判据只回答「我读到的正文还是当前那一份吗」。
        """
        _, rel = self.shell_in_the_vault()
        with self.kb() as c:
            seen = extract.body_fingerprint(store.doc_body(c, rel))
        (self.root / 'vault' / rel).unlink()
        with self.kb() as c:
            extract.save_extraction(c, rel, NEW, seen)
            c.commit()
        self.assertEqual(NEW['summary'], self.state(rel)['summary'])

    def test_no_fingerprint_means_no_check(self):
        """不传就不校验 —— 软广补判那条路压根不读正文,它的结论不随正文作废。"""
        _, rel = self.shell_in_the_vault()
        with self.kb() as c:
            extract.save_extraction(c, rel, NEW)
            c.commit()
        self.assertEqual(NEW['summary'], self.state(rel)['summary'])

    def test_the_fingerprint_is_one_single_rule(self):
        """口径只有一份:调用方按手里那份 body 算的,和落库前按索引算的同一个数。"""
        _, rel = self.shell_in_the_vault()
        with self.kb() as c:
            body = store.doc_body(c, rel)
        self.assertEqual(extract.body_fingerprint(body),
                         extract.body_fingerprint(body))
        self.assertNotEqual(extract.body_fingerprint(body),
                            extract.body_fingerprint(body + ' '))


class CallSiteTests(unittest.TestCase):
    """三条提取路径都得把指纹传下去 —— 漏一条就是那条路上静默覆盖旧结论。

    判据钉在调用点上(不是"有没有这个参数"):`body_seen` 默认 None = 不校验,
    所以忘了传的那条路**一条测试都不会红**,只会在某天把一篇文章的摘要永久
    冻在旧正文上。多一条提取路径就在这里红,而不是等用户看见错摘要。
    """

    # 读 chunks 正文 + 调 LLM 的那几条:模型那几十秒里正文可能整篇换掉,必须校验
    LLM_PATHS = {'kb/cli.py': 'save_extraction(conn, row["path"], data, seen)',
                 'kb/pipeline_stages.py': 'save_extraction(conn, row["path"], data, seen)',
                 'scripts/extract_parallel.py': 'save_extraction(conn, path, data, seen)'}
    # 不读 chunks 正文的那几条:`parse_pretagged` 直接解析 frontmatter(同步、没有
    # 模型往返),软广补判经 `sync_promo_tag` 进来、只改 promo —— 它们没有"基于哪
    # 一份正文"可言,传指纹反而会把一次合法的刷新判成作废。
    PLAIN_PATHS = {'kb/extract.py', 'scripts/pretagged_refresh.py'}

    def test_every_llm_extraction_path_passes_the_fingerprint(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for rel, call in self.LLM_PATHS.items():
            with self.subTest(path=rel):
                text = (root / rel).read_text(encoding='utf-8')
                self.assertIn(call, text)
                self.assertIn('StaleBody', text)

    def test_no_other_path_calls_save_extraction(self):
        """全仓调用点名单:新增一处就在这里红,逼它自己决定传不传指纹。"""
        import subprocess
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        out = subprocess.run(['grep', '-rln', 'save_extraction(', 'kb', 'scripts'],
                             cwd=root, capture_output=True, text=True).stdout.split()
        self.assertEqual(self.PLAIN_PATHS | set(self.LLM_PATHS), set(out))


class PipelineStageTests(StaleCase):
    """管道那条路:作废不算失败,面板不许因此标红。"""

    @contextmanager
    def a_pipeline_round(self):
        """真跑一轮管道提取:只打桩模型和展示,连接指到 tmp 那个 kb.db。

        `store.connect` 的默认参数在函数定义时就绑死了生产路径,所以和
        `reproduce.py` 一样显式把 `config.DB_PATH` 传进去 —— 不这么做
        `_extract_pass` / `_extract_one` 会去连真库。
        """
        from kb import pipeline_stages as ps
        real = store.connect
        with patch.object(store, 'connect', lambda *a, **kw: real(config.DB_PATH)), \
                patch.object(extract, 'extract_with_llm', lambda *a, **kw: dict(NEW)), \
                patch.object(llm_stages, 'stage_model', Mock(return_value='stub')), \
                patch.object(ps, '_stage', Mock()), patch.object(ps, '_log', Mock()):
            yield

    def test_the_pipeline_pass_books_a_discarded_result_as_a_skip(self):
        """真走一轮 `_extract_pass`:作废落 `stale_paths`,不落 `failed_paths`。

        **这一条非有不可**:下面那条只喂一份现成的 stats 给收尾那句话,对
        `_extract_pass` 里「`except StaleBody` 排在 `except Exception` 之前」
        完全是瞎的 —— 把那个分支摘掉(作废当成一次提取失败、面板标红)它照旧
        全绿,反向验证实测红 0 条。
        """
        from kb import pipeline_stages as ps
        _, rel = self.shell_in_the_vault()
        fulltext._drop_extraction([rel])              # 回到待提取集合
        # 摆「导出写了新正文、索引还没跟上」:md 换掉,chunks 和 content_hash 没动
        full = self.root / 'vault' / rel
        full.write_text(full.read_text().replace('HN Points', '整篇正文在这里'),
                        encoding='utf-8')
        stats = ps._new_extract_stats()
        with self.a_pipeline_round():
            ps._extract_pass('存量', stats)
        # 后果先断言:这一篇没被静默标记成"已提取",还在队列里。
        after = self.state(rel)
        self.assertEqual(([rel], None), (after['pending'], after['extracted_at']))
        self.assertEqual({rel}, stats['stale_paths'])
        self.assertEqual((0, set(), {}), (stats['ok'], stats['failed_paths'],
                                         stats['failures']))
        # 收尾那句话:阶段仍是 ok,面板不标红
        seen = {}
        with patch.object(ps, '_stage', lambda *a, **kw: seen.update(args=a, kw=kw)):
            ps._finish_extract_stage(stats)
        self.assertEqual('ok', seen['args'][1])
        self.assertIn('正文已更新', seen['args'][2])
        # 反过来钉一边:这一篇还在队列里,索引跟上之后下一轮提得出来。
        self.index(rel)
        stats2 = ps._new_extract_stats()
        with self.a_pipeline_round():
            ps._extract_pass('存量', stats2)
        self.assertEqual((1, set()), (stats2['ok'], stats2['stale_paths']))
        self.assertEqual(NEW['summary'], self.state(rel)['summary'])

    def test_a_discarded_result_is_not_a_failure(self):
        from kb import pipeline_stages as ps
        stats = ps._new_extract_stats()
        stats['ok'] = 2
        stats['stale_paths'].add('source/rss/x.md')
        seen = {}
        with patch.object(ps, '_stage', lambda *a, **kw: seen.update(args=a, kw=kw)):
            ps._finish_extract_stage(stats)
        self.assertEqual('ok', seen['args'][1])
        self.assertIn('正文已更新', seen['args'][2])
        self.assertEqual([], seen['kw']['failures'])


if __name__ == '__main__':
    unittest.main()
