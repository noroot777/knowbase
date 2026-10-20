# -*- coding: utf-8 -*-
"""提取结果和它基于的那份正文绑在一起 —— 对不上就作废(kb/extract.py)。

被测的那件事:提取是「读正文 → 等几十秒模型 → 落库」,而补抓把跳转壳换成全文
正是会在这中间整篇换掉正文的一条路。它还**刻意**把这一篇送回待提取集合,于是
「模型读走旧壳 → 期间正文换新 → 旧结论落库」从边角变成了常规时序。

**落库会把 `extracted_at` 设上,而那是永久的**:这一篇从此再也不进
`extract.pending_docs`,摘要、主题、实体永久停在那个壳上,再同步、再索引都不会
自己纠正(2026-10-09 第 2 轮审查两种交错时序各三次稳定复现)。

两种时序各一道判据(`extract._require_current_input`),**各有射程、缺一不可**:
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

from kb import cli, config, extract, llm_stages, maintenance, store
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

    def content_hash(self, rel: str) -> str:
        """索引记着的那份 md 指纹 —— 回写改了文件才该刷新它。"""
        with self.kb() as c:
            row = c.execute('SELECT content_hash FROM documents WHERE path=?',
                            (rel,)).fetchone()
        return row and row['content_hash']

    def account_model(self):
        """一个记下"模型收到的账号"的假提取器。account 走关键字 —— 位置传参的
        实现(把它塞到 timeout 上)会在这儿红,而不是等真实判定跑偏。"""
        seen: list[str] = []

        def model(title, body, timeout=120, account=''):
            seen.append(account)
            return dict(NEW)
        return model, seen

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
            seen = extract.model_input(c, rel)["seen"]
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
            seen = extract.model_input(c, rel)["seen"]
        full = self.root / 'vault' / rel
        full.write_text(full.read_text() + '\n新正文来了。\n', encoding='utf-8')
        with self.kb() as c:
            # 反过来确认索引里那组输入真的没变:②判据此刻是通过的。
            self.assertEqual(seen, extract.model_input(c, rel)["seen"])
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
            seen = extract.model_input(c, rel)["seen"]
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
            seen = extract.model_input(c, rel)["seen"]
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

    def test_the_fingerprint_covers_every_field_the_model_reads(self):
        """指纹盖的是**整组**输入:标题、账号、正文,换哪一样都得换个数。

        只盖正文的话,"这个结论还是针对当前那篇文章的吗"这个问题在标题或账号
        被换掉时答错 —— 而那两样都进 prompt(软广判定按「这个号在卖什么」读
        文末那段)。少盖一样,那一样就成了可以静默替换的输入。
        """
        base = extract.input_fingerprint('标题', '账号', '正文')
        self.assertEqual(base, extract.input_fingerprint('标题', '账号', '正文'))
        for label, args in (('标题', ('别的标题', '账号', '正文')),
                            ('账号', ('标题', '别的账号', '正文')),
                            ('正文', ('标题', '账号', '别的正文'))):
            with self.subTest(changed=label):
                self.assertNotEqual(base, extract.input_fingerprint(*args))
        # 三段不许能拼出歧义:'a|b' + 'c' 不该等于 'a' + 'b|c'
        self.assertNotEqual(extract.input_fingerprint('a', 'b', 'c'),
                            extract.input_fingerprint('ab', '', 'c'))

    def test_a_changed_title_voids_the_result_too(self):
        """② 的新射程:正文一个字没动,**标题**换了 —— 这次结果照样作废。

        模型是按旧标题判的。只比正文的话这一次放行,旧标题下的摘要、主题、
        软广判定就固化到新标题上,而 `extracted_at` 一落这一篇再也不进待提取
        集合:界面上已经是另一篇文章,带着别人的结论,再同步也不纠正。
        """
        _, rel = self.shell_in_the_vault()
        before = self.state(rel)
        with self.kb() as c:
            seen = extract.model_input(c, rel)["seen"]
            # 只换标题:文件没动,chunks 没动,`content_hash` 也没动 —— ① 那道
            # 判据此刻完全是瞎的,所以这一条钉的是 ② 自己。
            c.execute('UPDATE documents SET title=? WHERE path=?',
                      ('换掉的标题', rel))
            c.commit()
            from kb.scanner import file_hash
            full = self.root / 'vault' / rel
            self.assertFalse(store.doc_needs_reindex(
                c, rel, file_hash(full.read_text(encoding='utf-8'))),
                '夹具没摆成「只有标题换了」')
            with self.assertRaises(StaleBody):
                extract.save_extraction(c, rel, NEW, seen)
            c.commit()
        after = self.state(rel)
        self.assertEqual(before['summary'], after['summary'])
        self.assertEqual(before['extracted_at'], after['extracted_at'],
                         'extracted_at 被这次作废的结果刷新了')
        self.assertEqual(before['entities'], after['entities'],
                         '按旧标题算出来的实体落了库')


class ModelInputTests(BodyVersionCase):
    """模型读到的那组输入,和落库校验的那组,必须出自**同一个快照**。

    三条提取路径原先是「标题、账号从待提取名单里带过来,正文稍后另读一次」。
    那两次读之间另一条连接的索引可以把这一篇整篇换掉,于是模型收到旧标题配新
    正文 —— 判出来的结论落在一份没人写过的东西上,而按正文比的那道判据看不出
    任何异常(正文确实是当前那一份)。`extract.model_input` 把四样收进一条语句,
    这一组钉住「名单那一份不会混进模型输入」和「账号真的传到了模型」。
    """

    def test_the_model_does_not_read_the_title_from_the_todo_list(self):
        """名单里的标题是旧快照:模型必须拿当前那一份,不是名单那一份。"""
        _, rel = self.shell_in_the_vault()
        with self.kb() as c:
            c.execute('UPDATE documents SET title=? WHERE path=?',
                      ('索引刚换上的新标题', rel))
            c.commit()
        stale_row = {'path': rel, 'title': '名单里那份旧标题', 'account': ''}
        seen = {}

        def model(title, body, **kw):
            seen.update(title=title, account=kw.get('account'))
            return dict(NEW)

        with patch.object(extract, 'pending_docs',
                          Mock(return_value=[stale_row])):
            self.assertEqual(0, self.extract_cli(model))
        self.assertEqual('索引刚换上的新标题', seen['title'],
                         '名单那一份旧标题被喂给了模型')
        # 后果:这次结果落得下来(校验的和模型读的是同一组输入)
        self.assertEqual(NEW['summary'], self.state(rel)['summary'])

    def test_the_account_reaches_the_model_on_the_cli_path(self):
        """账号也得传下去 —— 它是软广判定的判据之一,不是展示字段。"""
        _, rel = self.shell_in_the_vault()
        fulltext._drop_extraction([rel])              # 回到待提取集合
        with self.kb() as c:
            c.execute('UPDATE documents SET account=? WHERE path=?',
                      ('卖代充值的号', rel))
            c.commit()
        model, seen = self.account_model()
        self.assertEqual(0, self.extract_cli(model))
        self.assertEqual(['卖代充值的号'], seen)

    def test_the_account_reaches_the_model_on_the_pipeline_path(self):
        """管道那条路同一件事 —— 三条路的输入口径必须一样。"""
        from kb import pipeline_stages as ps
        _, rel = self.shell_in_the_vault()
        fulltext._drop_extraction([rel])              # 回到待提取集合
        with self.kb() as c:
            c.execute('UPDATE documents SET account=? WHERE path=?',
                      ('卖代充值的号', rel))
            c.commit()
        model, seen = self.account_model()
        real = store.connect
        with patch.object(store, 'connect', lambda *a, **kw: real(config.DB_PATH)), \
                patch.object(extract, 'extract_with_llm', model), \
                patch.object(llm_stages, 'stage_model', Mock(return_value='stub')), \
                patch.object(ps, '_stage', Mock()), patch.object(ps, '_log', Mock()):
            ps._extract_pass('存量', ps._new_extract_stats())
        self.assertEqual(['卖代充值的号'], seen)


class CallSiteTests(unittest.TestCase):
    """三条提取路径都得把指纹传下去 —— 漏一条就是那条路上静默覆盖旧结论。

    判据钉在调用点上(不是"有没有这个参数"):`input_seen` 默认 None = 不校验,
    所以忘了传的那条路**一条测试都不会红**,只会在某天把一篇文章的摘要永久
    冻在旧正文上。多一条提取路径就在这里红,而不是等用户看见错摘要。
    """

    # 读 chunks 正文 + 调 LLM 的那几条:模型那几十秒里正文可能整篇换掉,必须校验
    # ——而且必须走**协调入口**(`commit_extraction`):裸调 `save_extraction` 的话
    # 校验和写入之间、以及落库和回写之间各留一个窗口(见第 3 轮审查那两份证据)。
    LLM_PATHS = {'kb/cli.py': 'commit_extraction(\n                conn, row["path"], data, seen,',
                 'kb/pipeline_stages.py': 'commit_extraction(conn, row["path"], data, seen)',
                 'scripts/extract_parallel.py': 'commit_extraction(\n                    conn, path, data, seen,'}
    # 裸 `save_extraction(` 剩下的那几处都是**不读 chunks 正文**的:条目化就地
    # 入库走 `parse_pretagged` 解 frontmatter(同步、没有模型往返,三处:索引那
    # 两条入口 + `pretagged_refresh.py`)—— 它们没有"基于哪一份正文"可言。
    # **软广补判不在这张名单上,因为它压根不是 `save_extraction` 的调用方**:
    # 它照样读 chunks 正文、照样等模型回包那几十秒,所以也校验,**只是判据和这里
    # 不是同一组**:`commit_promo` 在锁内、落库**之前**问两件事 —— 磁盘正文进索引
    # 了吗(`disk_input_indexed`,标题/账号按索引的读法读一遍、正文按索引那对
    # 函数切一遍再和库里比)、索引里那组换版了吗(`_input_moved` 关掉第一道);
    # 落库那条 UPDATE 自己的
    # WHERE 再比一次 `content_hash`。**它的回写那一侧不传指纹**(理由见
    # `extract_promo.sync_promo_tag`):判据已经提到交付区间的入口了,拦下来是
    # 一个字都没写、两处仍然一致;在回写那一侧再判一次反而会造出不一致 ——
    # 那时判定已经落库,而第一道比的是整个**文件**的 hash,只改别的 frontmatter
    # 字段的流程是常态(翻译回写、tag 回写、wiki 追加图谱尾区)。这几件事由
    # `tests/test_promo_recheck.py` 和 `tests/test_promo_delivery.py` 钉着。
    # 按**文件**比,所以 `kb/cli.py` / `kb/pipeline_stages.py` 两边都在
    # (它们各有一条 LLM 路径和一条条目化路径);「LLM 那三条真的走了协调入口」
    # 由上面那张字面量锚点的表钉住。
    # `kb/pipeline_wiki_stages.py` 是从 `pipeline_stages` 拆出去的那一半(条目化
    # 那一段),不是新开的调用点 —— 搬家会让按文件名记的这类名单静默变红。
    PLAIN_PATHS = {'kb/cli.py', 'kb/extract.py', 'kb/pipeline_stages.py',
                   'kb/pipeline_wiki_stages.py', 'scripts/pretagged_refresh.py'}

    def grep(self, needle):
        """全仓(kb/ 和 scripts/,递归)含 `needle` 的文件 —— **注释行不算**。

        注释里提到一个函数名不是一处调用点。不跳的话写一句解释性注释就会把
        下面那两张名单弄红,而真要新增一处调用点的人只会顺手把名单改掉 ——
        那就把这条用例从「拦住新调用点」变成了「拦住写注释的人」。
        """
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        hit = set()
        for py in sorted(root.glob('kb/**/*.py')) + sorted(root.glob('scripts/**/*.py')):
            for ln in py.read_text(encoding='utf-8').splitlines():
                if needle in ln and not ln.lstrip().startswith('#'):
                    hit.add(py.relative_to(root).as_posix())
                    break
        return hit

    def test_every_llm_extraction_path_passes_the_fingerprint(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for rel, call in self.LLM_PATHS.items():
            with self.subTest(path=rel):
                text = (root / rel).read_text(encoding='utf-8')
                self.assertIn(call, text)
                self.assertIn('StaleBody', text)

    def test_no_other_path_commits_an_extraction(self):
        """协调入口的调用点名单:新增一处就在这里红,逼它自己决定传不传指纹。"""
        self.assertEqual(set(self.LLM_PATHS) | {'kb/extract.py'},
                         self.grep('commit_extraction('))

    def test_every_hash_refresh_proves_the_version_it_overwrites(self):
        """刷「已索引」那张收据的每一处调用点都得拿出「动手之前」那份 hash。

        判据钉在调用点上:`before` 一旦有默认值,忘了传的那条路**一条测试都不会
        红**,只会在某天把别人那份还没索引的改动认证成「已索引」—— 那份改动从此
        永远进不了 chunks(实测最坏的一种:磁盘上换成了新正文、chunks 还是旧的,
        新正文一个字都搜不到,点开文章却是新的,再同步也不恢复)。
        **`before` 交的是原文、不是它的 hash**:判据要拿它回答「文件自己那几句话
        动没动」,hash 答不了(见 `kb.doc_rewrite.index_projection_matches`)。
        所以这里既钉「只有这几处在调它」,也钉「它压根没有默认值可漏」——
        `written` 那个参数同理:**有默认值就等于又给了「事后重读磁盘」一条路**,
        而那一份可能已经是别人的了。

        **「谁有资格登记」那张更大的名单在 `tests/test_doc_rewrite_roster.py`** ——
        光枚举这个函数的调用点不够:第 5 轮审查抓出来的两条旁路当时**都在**
        这张名单上、`expect` 也都老实传着,而它们整段在维护锁外,「库里记的还是
        动手之前那一份」答不了「磁盘上还是我写的那一份」。那两条现在走
        `kb.doc_rewrite.rewriting()`,所以这里只剩提取回写一条。
        """
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(
            {'kb/extract.py', 'kb/extract_writeback.py'},
            self.grep('refresh_content_hash(conn'),
            '多了一处刷新索引收据的调用点 —— 它拿出「动手之前那一份原文」了吗')
        src = (root / 'kb/extract_writeback.py').read_text(encoding='utf-8')
        self.assertIn(
            'def refresh_content_hash(conn, doc_path: str,'
            ' before: str | None,\n                         written: str)', src,
            '签名变了:`written`(本次写出去的那一份)或 `before`(索引消费的'
            '那一份原文)有了默认值,漏传的那条路会静默认证别人的改动')
        self.assertNotIn('read_text', src.split(
            'def refresh_content_hash')[1],
            '它又自己去读磁盘了 —— 「刚读到」不能充当「刚写出」')
        self.assertIn('WHERE path=? AND content_hash=?',
                      (root / 'kb/doc_rewrite.py').read_text(encoding='utf-8'))

    def test_no_llm_path_calls_the_bare_save(self):
        """裸 `save_extraction` 只剩不读正文的那几条 —— 读正文的一律走协调入口。

        漏一条就是那条路上「校验通过了、写进去的却是另一个版本」:第 3 轮审查
        实测新全文配旧摘要(`entities=["htmx"]`)、`extracted_at` 非空、
        `pending=[]`,再同步一轮也不变。
        """
        self.assertEqual(self.PLAIN_PATHS, self.grep('save_extraction('))


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


class CommitWindowTests(BodyVersionCase):
    """校验之后那两个窗口:落库前、回写前,换正文那条路都不许挤进来。

    上面两组钉的是「检查认不认得出来」,这一组钉的是「检查和写入之间还有没有
    缝」—— 两件事,第 2 轮只修了前者。第 3 轮审查两种交错各三次稳定复现:

    - **校验到第一条 SQL**:校验合法通过,随后另一边走完补抓、翻译、导出、作废
      和索引(`extract_stale` 也清了零),这才放行旧结果落库 —— 末态是新全文配
      旧结论(`summary="只有一条链接和 HN 得分。"`、`entities=["htmx"]`),
      `extracted_at` 非空、`pending=[]`,**再同步一轮也不变**。原来那条兜底论证
      (「待办会再作废一次」)在这个顺序下不成立:待办已经清零了。
    - **落库到回写**:旧结果合法落库并提交,期间新全文进了索引、md 里那批旧 tag
      已经被作废摘掉,这才放行回写 —— `topic/link-only` 重新写进新全文的
      frontmatter,而 `writeback_tags` 只追加不摘除、还顺手刷了 content_hash。

    关掉它们的办法是把两头圈进同一个「维护锁 → kb.db 写锁」区间
    (`extract.commit_extraction`):把正文整篇换掉的那条路全程持着**同一把**
    维护锁(`sync._save_result`、`sync.export`、索引整段都认它),进不来。
    """

    def test_nobody_can_touch_the_body_between_the_check_and_the_commit(self):
        """校验刚通过那一刻,换正文那条路真的被挡在维护锁外面。

        **屏障摆在校验刚返回的位置**,不是摆在取锁之后:摆在后面的话,「校验在
        锁外、只把那串 SQL 圈进锁里」这种写法也能混过去 —— 而那正是被审那一版
        的形状(它压根没有锁)。
        """
        _, rel = self.shell_in_the_vault()
        self.fetch_and_translate()                 # 补抓之后的常规状态:正文已换、
        self.index(rel)                            # 索引已跟上、这一篇排回了队列
        self.assertEqual([rel], self.state(rel)['pending'])
        checked, release, rival = threading.Event(), threading.Event(), {}
        real = extract._require_current_input

        def checked_then_hold(conn, path, seen):
            real(conn, path, seen)
            checked.set()
            release.wait(10)

        model, _ = self.seen_model(NEW)
        with patch.object(extract, '_require_current_input', checked_then_hold):
            worker = threading.Thread(target=lambda: self.extract_cli(model))
            worker.start()
            try:
                self.assertTrue(checked.wait(10), '压根没走到校验那一步')
                try:
                    # 换正文那条路要的就是这把锁。拿不到 = 这个窗口是关着的。
                    with maintenance.maintenance_lock(wait=0):
                        rival['got'] = True
                except maintenance.LockBusy:
                    rival['got'] = False
            finally:
                release.set()
                worker.join(20)
        self.assertFalse(rival.get('got', True),
                         '校验通过了、还没落库,换正文那条路却进得来')

    def test_a_writeback_of_a_body_that_moved_is_thrown_away(self):
        """按旧正文算出来的那批 tag,一个都不许写进换好的新全文。"""
        _, rel = self.shell_in_the_vault()
        with self.kb() as c:
            seen = extract.model_input(c, rel)["seen"]
        self.fetch_and_translate()                 # 正文换成全文
        self.index(rel)                            # 索引跟上
        before = self.content_hash(rel)
        with self.kb() as c:
            changed = extract.writeback_extraction(c, rel, dict(OLD),
                                                   input_seen=seen)
        self.assertFalse(changed, '按旧正文算出来的 tag 被写进了新全文')
        self.assertEqual([], [t for t in self.md_tags(rel) if 'link-only' in t],
                         'md 的 frontmatter 里混进了按旧壳算出来的主题')
        self.assertEqual(before, self.content_hash(rel),
                         '什么都没写却刷了 content_hash,下一轮索引会跳过它')

    def test_a_writeback_of_the_current_body_still_lands(self):
        """反过来钉一边:正文没动的那一次照旧写得进去。

        不钉的话"拦住了"和"这个入口压根不写文件"长得一模一样。
        """
        _, rel = self.shell_in_the_vault()
        self.fetch_and_translate()
        self.index(rel)
        with self.kb() as c:
            seen = extract.model_input(c, rel)["seen"]
            changed = extract.writeback_extraction(c, rel, dict(NEW),
                                                   input_seen=seen)
        self.assertTrue(changed, '正文还是当前那一份,tag 却没写进去')
        self.assertIn('topic/ai-coding', self.md_tags(rel))
