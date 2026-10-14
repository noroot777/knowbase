# -*- coding: utf-8 -*-
"""软广判定的**交付**:判定、`doc_tags` 那行、md 那行,三处必须最终一致。

从 `tests/test_promo.py` 拆出来的一组(那个文件写到 771 行、越过仓库约定的
700 行上限)。判定**怎么下**仍在 `test_promo.py`(口径、截断、查询范围)和
`test_promo_recheck.py`(严格结论、条件写、协调区间);这里只问交付:

  * 交付跨两个存储(kb.db 的事务管不着 vault 文件),第二步失败留没留下
    可恢复的待办、`sync-tags` 修不修得回来;
  * 索引重建 `doc_tags` 时,过时的 md 那行会不会把错的标签带回库里;
  * 「要不要摘那行」的判据是**库里那份判定**,不是「上一次是不是 hard」。

硬约束在 `docs/decisions/promo.md` 第一节。
"""
from __future__ import annotations

import contextlib
import io as _io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from kb import extract, maintenance, scanner, store      # noqa: E402

_REAL_CONNECT = store.connect


class ReclassifyVaultTest(unittest.TestCase):
    """误判 hard 之后改判,md 里那行 flag 也得摘掉。

    writeback_tags 只会追加。DB 侧改判时 doc_tags 清了,但 frontmatter 还留着
    那行——这篇文件之后任何一次改动触发 upsert_document,doc_tags 会按
    frontmatter 重建,误判的标记就又回到库里(第 2 轮审查发现)。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name) / "vault"
        self.vault.mkdir()
        self.md = self.vault / "a.md"
        self.md.write_text(
            '---\ntitle: "广告"\ntags:\n  - source/dr-gzh\n---\n正文\n',
            encoding="utf-8")
        self.conn = store.connect(Path(self._tmp.name) / "kb.db")
        self.conn.execute(
            "INSERT INTO documents (path, source, title, content_hash, url)"
            " VALUES ('a.md','dr-gzh','广告','old','u')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _reindex(self):
        """模拟下一轮 index:doc_tags 按 md 当前的 frontmatter 重建。"""
        tags = re.findall(r"^\s*-\s+(\S+)\s*$", self.md.read_text(encoding="utf-8"),
                          re.M)
        doc = SimpleNamespace(path="a.md", source="dr-gzh", title="广告",
                              account="号", published="", url="u",
                              content_hash="new", tags=tags)
        store.upsert_document(self.conn, doc, [], "t2")
        self.conn.commit()
        return [r["tag"] for r in self.conn.execute(
            "SELECT tag FROM doc_tags WHERE path='a.md' ORDER BY tag")]

    def _judge(self, level: str):
        """落一次判定并把两处标记对齐。**不传任何「上次是不是 hard」的信号** ——
        md 那行按库里的判定幂等对齐(`extract_promo.align_promo_tag`)。"""
        with mock.patch.object(extract, "VAULT_ROOT", self.vault):
            self.assertTrue(extract.save_promo(self.conn, "a.md",
                                               {"level": level, "reason": "r"}))
            extract.sync_promo_tag(self.conn, "a.md", level)
        self.conn.commit()

    def test_flag_removed_from_markdown_on_reclassify(self):
        self._judge("hard")
        self.assertIn(extract.PROMO_TAG, self.md.read_text(encoding="utf-8"))

        self._judge("none")
        text = self.md.read_text(encoding="utf-8")
        self.assertNotIn(extract.PROMO_TAG, text)
        self.assertIn("source/dr-gzh", text)          # 别的 tag 不能被误删
        self.assertIn("正文", text)                   # 正文不碰

        self.assertNotIn(extract.PROMO_TAG, self._reindex())   # 重索引不再带回

    def test_index_does_not_resurrect_a_flag_the_verdict_contradicts(self):
        """md 里那行过时了(改判成 none 之后没摘干净)—— 索引不许把它读回库里。

        `upsert_document` 把 doc_tags 整表删掉再按 frontmatter 重建,于是 md
        那一行就成了这张表的唯一真相来源:判定早已是 none,一次**完全合法**的
        索引又把 `flag/软广` 插回去,按这个标签过滤的查询从此照旧捞得到这篇已
        经纠正为正常内容的文章。md 那行过时的路子不止一条(回写时碰上没权限、
        磁盘满、进程被杀,或者有人手工改了 frontmatter),所以判据只能是库里
        那份判定(`store._align_promo_tag`)。
        """
        self._judge("hard")
        self.conn.execute("UPDATE documents SET promo_level='none'"
                          " WHERE path='a.md'")
        self.conn.commit()
        self.assertIn(extract.PROMO_TAG, self.md.read_text(encoding="utf-8"),
                      "夹具没摆出「md 那行还在」那个前置")
        self.assertNotIn(extract.PROMO_TAG, self._reindex())

    def test_index_restores_the_flag_the_verdict_requires(self):
        """反过来钉一边:库里判 hard 而 md 那行缺着时,索引要把它补回 doc_tags。

        不然「md 不是真相来源」就滑到了另一头 —— 一篇真广告在重建索引之后
        不再带这个标签,面板和 obs 上都看不出来。
        """
        self._judge("hard")
        with mock.patch.object(extract, "VAULT_ROOT", self.vault):
            extract.remove_writeback_tag("a.md", extract.PROMO_TAG)
        self.assertNotIn(extract.PROMO_TAG, self.md.read_text(encoding="utf-8"))
        self.assertIn(extract.PROMO_TAG, self._reindex())

    def test_a_first_judgement_is_not_contradicted_by_an_empty_level(self):
        """`promo_level=''`(还没判过)时两样都不做:md 那行是唯一的记录。

        重建过 kb.db 的库就是这个状态(提取阶段写进 frontmatter 的标记还在,
        判定列是空的)。按「矛盾」摘掉等于凭空丢数据。
        """
        self.conn.execute("UPDATE documents SET promo_level='' WHERE path='a.md'")
        self.conn.commit()
        tags = re.findall(r"^\s*-\s+(\S+)\s*$",
                          self.md.read_text(encoding="utf-8"), re.M)
        self.md.write_text(self.md.read_text(encoding="utf-8").replace(
            "---\n正文", f"  - {extract.PROMO_TAG}\n---\n正文"), encoding="utf-8")
        self.assertNotIn(extract.PROMO_TAG, tags)
        self.assertIn(extract.PROMO_TAG, self._reindex())

    def test_no_file_rewrite_when_never_hard(self):
        """判 soft 时要确认 md 那行不在(读一次),但**不许改文件**。

        幂等对齐的代价就是这一次读:原来靠「上次是不是 hard」那个信号决定要
        不要碰文件,省掉了读,代价是一次失败之后再也没有第二次机会。
        """
        before = self.md.read_text(encoding="utf-8")
        self._judge("soft")
        self.assertEqual(self.md.read_text(encoding="utf-8"), before)

    def test_reclassify_needs_no_stale_signal(self):
        """改判摘 md 那行,**不依赖任何「上次是 hard」的信号**。

        原来 `save_extraction` 会把这个信号透传给回写方,而它只在「这一次恰好
        改判了」时为真:一次回写失败之后库里已经是 none,第二次重判拿不到信号,
        压根不会去碰文件,md 那行永久留着。现在的判据是库里那份判定,所以这里
        连着判两次 none —— 第二次也必须把那行摘掉。
        """
        self._judge("hard")
        for _ in range(2):
            extract.save_extraction(self.conn, "a.md", {
                "entities": [], "topics": [], "summary": "改判",
                "promo": {"level": "none", "reason": "不是广告"}})
            with mock.patch.object(extract, "VAULT_ROOT", self.vault):
                extract.writeback_extraction(self.conn, "a.md", {
                    "entities": [], "topics": [],
                    "promo": {"level": "none", "reason": "不是广告"}})
            self.assertNotIn(extract.PROMO_TAG,
                             self.md.read_text(encoding="utf-8"))
            # 第二轮前把 md 那行塞回去:摆「上一次没摘干净」的那个末态
            self.md.write_text(self.md.read_text(encoding="utf-8").replace(
                "---\n正文", f"  - {extract.PROMO_TAG}\n---\n正文"),
                encoding="utf-8")

    def test_remove_tag_raises_on_missing_file(self):
        """文件读不出来**不许**静默返回 False。

        False 和「本来就没有这个 tag」长得一模一样:没权限、文件刚被挪走的那次
        「摘标签」会报成成功,库里是 none、md 里那行还在,再没有人回来看它。
        抛出去才有人接(`align_promo_tag` 回读确认 → 留待办)。
        """
        with mock.patch.object(extract, "VAULT_ROOT", self.vault / "nope"):
            with self.assertRaises(OSError):
                extract.remove_writeback_tag("a.md", extract.PROMO_TAG)

PATH = "source/dr-gzh/ad.md"
ACCOUNT = "GitHub猿"
OLD_BODY = "这是一篇开源项目资讯。htmx 发布了 2.0 版本,oldreporttoken。"
NEW_BODY = "加微信 reviewsvc 办理代充值,官方一半价格,newpromotoken。"


class _Vault(unittest.TestCase):
    """一篇已经判过、也已经索引过的文档:库里有行、chunks 有正文、vault 有 md。

    和 `test_promo_recheck._Fixture` 的差别只在它要能跑真实 `cmd_run` 的几条
    命令行入口,所以把那几样打桩收在 `env()` 一处(范围覆盖到工作线程跑完 ——
    `store.connect` 的替身提前恢复的话,还在跑的线程会连到真实的
    `$KB_DATA_DIR/kb.db`,把夹具文档写进共享库、带红别的测试文件)。
    """

    def setUp(self):
        import promo_backfill
        self.pb = promo_backfill
        self._tmp = tempfile.TemporaryDirectory(prefix="kb-promo-deliver-")
        self.root = Path(self._tmp.name)
        self.db = self.root / "kb.db"
        self.vault = self.root / "vault"
        (self.vault / "source" / "dr-gzh").mkdir(parents=True)
        self.md = self.vault / PATH
        self.conn = _REAL_CONNECT(self.db)
        enter = mock.patch.object(extract, "VAULT_ROOT", self.vault)
        enter.start()
        self.addCleanup(enter.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.conn.close)

    def md_text(self, body: str, flagged: bool, path: str = PATH) -> str:
        """一份 md。**frontmatter 带 path 自己的 url**:同题的两篇在真实库里
        各有各的链接,`content_hash` 必然不同 —— 写成逐字相同的话,「每篇各按
        自己那一版的号判」就区分不出来了。"""
        flag = f"  - {extract.PROMO_TAG}\n" if flagged else ""
        # **account 要写进 frontmatter**:库里那一列就是索引从这里读出来的
        # (`scanner.frontmatter_input`),夹具里只给库不给 md 等于摆出一个真实
        # 数据里不存在的状态 —— 而交付前那道「磁盘上那组输入进索引了吗」正是
        # 拿这两边比,会一直判「没进」。
        return (f'---\ntitle: "稿子"\naccount: "{ACCOUNT}"\n'
                f'url: "https://example.org/{Path(path).stem}"\n'
                f'tags:\n  - source/dr-gzh\n{flag}---\n{body}\n')

    def seed(self, level: str, body: str, flagged: bool,
             path: str = PATH) -> str:
        text = self.md_text(body, flagged, path)
        full = self.vault / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
        version = scanner.file_hash(text)
        self.conn.execute(
            "INSERT INTO documents (path, source, title, account, url,"
            " content_hash, extracted_at, promo_level, promo_reason)"
            " VALUES (?,'dr-gzh','稿子',?,?,?,"
            "'2026-10-14 10:00:00',?,'原判定')",
            (path, ACCOUNT, f"https://example.org/{Path(path).stem}",
             version, level))
        self.conn.execute("INSERT INTO chunks (path, seq, text) VALUES (?,0,?)",
                          (path, body))
        # **md 上那组 tag 要跟着进 doc_tags** —— 这是手工摆盘,不走
        # `store.upsert_document`,而真实索引会把 frontmatter 的每个 tag 都插
        # 进去。少插的那几行会让「登记已索引」那道判据从一开始就看到两边不一致
        # (它问的是「现在重新索引一次库里会变吗」),于是每条用例拒绝的理由都
        # 变成了「标签还没进库」—— 末态照旧对,各自本来要钉的那个原因却没了。
        for tag in scanner.frontmatter_tags(
                scanner.parse_frontmatter(text)[0]):
            if tag != extract.PROMO_TAG or flagged:
                self.conn.execute(
                    "INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?,?)",
                    (path, tag))
        if flagged:
            self.conn.execute("INSERT OR IGNORE INTO doc_tags (path, tag)"
                              " VALUES (?,?)", (path, extract.PROMO_TAG))
        self.conn.commit()
        return version

    @contextlib.contextmanager
    def env(self, *, reply: str | None = None, judge=None):
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(store, "connect",
                               lambda *a, **k: _REAL_CONNECT(self.db)), \
             mock.patch.object(maintenance, "DATA_DIR", self.root), \
             mock.patch("kb.llm_stages.stage_model", return_value="stub"), \
             mock.patch("kb.llm.chat", return_value=reply or "{}"):
            if judge is None:
                yield out
            else:
                with mock.patch.object(self.pb, "judge_promo",
                                       side_effect=judge):
                    yield out

    def cand(self, path: str = PATH) -> dict:
        """一个**真实形状**的候选 —— 从 `_candidates` 用的那同一条语句里取。

        手写字典的话,交付层哪天多要一样输入夹具就对不上:实测把标题和账号
        圈进指纹之后,手写的那几个 cand 红出来的是 `KeyError`,而真实的
        `_candidates` 一直给齐着 —— 那种红证明不了被测的行为。
        """
        return store.doc_input_snapshot(self.conn, path)

    def level(self, path: str = PATH) -> str:
        return self.conn.execute(
            "SELECT promo_level FROM documents WHERE path=?",
            (path,)).fetchone()["promo_level"]

    def reason(self, path: str = PATH) -> str:
        return self.conn.execute(
            "SELECT promo_reason FROM documents WHERE path=?",
            (path,)).fetchone()["promo_reason"]

    def version(self) -> str:
        return self.conn.execute(
            "SELECT content_hash FROM documents WHERE path=?",
            (PATH,)).fetchone()["content_hash"]

    def pending(self) -> int:
        return self.conn.execute(
            "SELECT promo_tag_pending FROM documents WHERE path=?",
            (PATH,)).fetchone()["promo_tag_pending"]

    def md_flag(self) -> bool:
        return extract.PROMO_TAG in self.md.read_text(encoding="utf-8")

    def db_flag(self, path: str = PATH) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM doc_tags WHERE path=? AND tag=?",
            (path, extract.PROMO_TAG)).fetchone() is not None

    @staticmethod
    def args(**kw):
        base = dict(account=[ACCOUNT], recheck=True, dry_run=False, limit=0)
        base.update(kw)
        return SimpleNamespace(**base)


class StagedBodyTest(_Vault):
    """模型在途时磁盘换了正文、索引还没跟上 —— 旧结论不许固化。

    这一档比「正文换了版」更隐蔽:`documents.content_hash` 和 chunks 都还是
    旧的,落库那道条件写照旧放行。导出和索引各自取锁、按两个阶段依次调用
    (`feeds.sync` → `pipeline_stages`),一次补判完全可以插在两者之间。

    不拦的末态实测过:库里存着对**旧**资讯正文判出的 none,而回写摘标签时
    `refresh_content_hash` 把**新**文件的 hash 写进库 —— 索引从此认为这一篇
    「未变」,新正文一个字都搜不到,点开文章却是新的,再同步也不恢复。
    """

    def _swap_the_body_midflight(self, title, body, account, **kw):
        self.assertEqual(body, OLD_BODY, "worker 读到的该是旧正文")
        # 另一边:导出把这一篇的正文整篇换掉(索引还没跟上,所以 chunks 和
        # content_hash 都不动)。这是两个阶段之间一个完全合法的时刻。
        self.md.write_text(self.md_text(NEW_BODY, flagged=True),
                           encoding="utf-8")
        return {"level": "none", "reason": "旧正文是正常资讯"}

    def test_a_staged_body_is_not_certified_as_indexed(self):
        v1 = self.seed("hard", OLD_BODY, flagged=True)
        with self.env(judge=self._swap_the_body_midflight) as out:
            self.assertEqual(0, self.pb.cmd_run(self.args()))
        text = out.getvalue()

        # 末态:一个字都没写 —— 库里和 md 里仍是原来那份判定,彼此一致
        self.assertEqual("hard", self.level(), "针对旧正文的 none 不许落库")
        self.assertTrue(self.db_flag())
        self.assertTrue(self.md_flag())
        self.assertIn("跳过 1 篇", text)
        self.assertIn("判定 0 篇", text)
        # 而且没把新文件认证成「已索引」:下一轮索引还会重建这一篇
        self.assertEqual(v1, self.version(), "content_hash 被刷成新文件的了")
        self.assertTrue(store.doc_needs_reindex(
            self.conn, PATH, scanner.file_hash(
                self.md.read_text(encoding="utf-8"))),
            "索引认为这一篇「未变」—— 新正文永远进不了 chunks")

    def _swap_the_title_midflight(self, title, body, account, **kw):
        """另一边只换**标题**:正文一个字不动,索引也还没跟上。"""
        self.assertEqual("稿子", title, "worker 读到的该是旧标题")
        self.md.write_text(
            self.md_text(OLD_BODY, flagged=True).replace(
                'title: "稿子"', 'title: "换成另一篇了"', 1),
            encoding="utf-8")
        return {"level": "none", "reason": "旧标题下是正常资讯"}

    def test_a_staged_title_is_not_certified_as_indexed_either(self):
        """只换标题也要拦:它和正文一样进 prompt,换了就不是同一篇文章了。

        只比正文的那一版在这里放行,末态是旧标题下判出的 none 固化到新标题上
        —— 界面上已经是另一篇文章,带着别人的判定;再加上回写那一下把新文件的
        hash 刷进库,新标题连索引都进不去。
        """
        v1 = self.seed("hard", OLD_BODY, flagged=True)
        with self.env(judge=self._swap_the_title_midflight) as out:
            self.assertEqual(0, self.pb.cmd_run(self.args()))
        self.assertEqual("hard", self.level(), "针对旧标题的 none 不许落库")
        self.assertTrue(self.md_flag())
        self.assertIn("跳过 1 篇", out.getvalue())
        self.assertEqual(v1, self.version(), "content_hash 被刷成新文件的了")
        self.assertTrue(store.doc_needs_reindex(
            self.conn, PATH, scanner.file_hash(
                self.md.read_text(encoding="utf-8"))),
            "索引认为这一篇「未变」—— 新标题永远进不了库")

    def _scan_the_fixture(self):
        """索引那一趟的扫描结果 —— 只给夹具这一篇,别的全是真的。

        不能让 `scan_all` 去扫真实 vault(它按 `kb.config` 解析来源目录,
        patch `scanner.VAULT_ROOT` 盖不住)。切块、upsert、FTS、提交全是真实的。
        """
        text = self.md.read_text(encoding="utf-8")
        fm, body = scanner.parse_frontmatter(text)
        yield scanner.Doc(path=PATH, source="dr-gzh", title="稿子",
                          account=ACCOUNT, published="2026-10-14",
                          url="https://example.org/ad",
                          tags=fm.get("tags") or [],
                          body=scanner.normalize_body(body),
                          content_hash=scanner.file_hash(text), meta=fm)

    def test_the_new_body_can_still_be_indexed_and_judged(self):
        """接着上一条跑真实索引 + 再判一次:新正文进得了检索,也判得出 hard。

        「拒绝」只有在下一轮能自己接上才算对 —— 否则就是把一次误判换成了
        一篇永远判不了的文章。
        """
        from kb import cli
        self.seed("hard", OLD_BODY, flagged=True)
        with self.env(judge=self._swap_the_body_midflight):
            self.assertEqual(0, self.pb.cmd_run(self.args()))
        with self.env(), \
             mock.patch.object(cli, "scan_all", self._scan_the_fixture):
            self.assertEqual(0, cli.cmd_index(
                SimpleNamespace(reembed=False, no_vector=True)))
        self.assertIn(NEW_BODY, store.doc_body(self.conn, PATH),
                      "新正文没进 chunks")
        with self.env(reply='{"level":"hard","reason":"文末在卖代充值"}') as out:
            self.assertEqual(0, self.pb.cmd_run(self.args()))
        self.assertEqual("hard", self.level())
        self.assertIn("判定 1 篇", out.getvalue())


class RefreshHashTest(_Vault):
    """回写刷 `content_hash` 时只认「库里记的还是我动手之前那一份」。

    这一句写的是「这个文件已经索引过了」。磁盘上有别人刚改完、索引还没跟上的
    改动时,无条件刷新等于**替那份改动签一张已索引的收据** —— `doc_needs_reindex`
    从此说「未变」,那份改动永远进不了 chunks。
    """

    def test_a_pending_edit_is_not_certified(self):
        self.seed("none", OLD_BODY, flagged=False)
        self.conn.execute("UPDATE documents SET content_hash='别人改之前那一版'"
                          " WHERE path=?", (PATH,))
        self.conn.commit()
        written = self.md.read_text(encoding="utf-8")
        # 没人改过这个文件,所以「动手之前那一份」就是它自己 —— 要拦下来的是
        # 库里记的那个 hash 已经不是它了(第三样证明)。
        extract.refresh_content_hash(self.conn, PATH, written, written)
        self.conn.commit()
        self.assertEqual("别人改之前那一版", self.version(),
                         "0 行才是对的末态:这一篇继续带着「待索引」")

    def test_a_tag_writeback_does_not_certify_someone_elses_edit(self):
        """别人刚改过 frontmatter、索引还没跟上 —— 我们摘自己那行标签的时候,
        不许顺手把整个文件认证成「已索引」。

        这一档是**合法通路**(翻译回写、tag 回写、wiki 追加图谱尾区都只改
        frontmatter),所以改判照旧放行、md 那行照旧摘掉;要钉住的是刷 hash 那
        一下别越界 —— 刷了的话 `doc_needs_reindex` 从此说「未变」,别人那份改动
        永远进不了索引,而这件事从外面一点看不出来。
        """
        v1 = self.seed("hard", OLD_BODY, flagged=True)
        self.md.write_text(self.md.read_text(encoding="utf-8").replace(
            "title:", "wiki_round: 2\ntitle:", 1), encoding="utf-8")
        cand = self.cand()
        self.assertEqual(v1, cand["version"], "夹具没摆成「磁盘领先于索引」")
        with self.env():
            self.assertEqual(("", True), self.pb._deliver(
                self.conn, cand, {"level": "none", "reason": "正常资讯"}))
        self.assertEqual("none", self.level(), "合法的改判被拦掉了")
        self.assertFalse(self.md_flag(), "md 那行没摘掉")
        self.assertEqual(v1, self.version(),
                         "把别人那份还没索引的改动认证成「已索引」了")
        self.assertTrue(store.doc_needs_reindex(
            self.conn, PATH, scanner.file_hash(
                self.md.read_text(encoding="utf-8"))),
            "下一轮索引不会再看这一篇 —— 别人那份改动就此丢了")

    def test_the_normal_path_still_refreshes(self):
        """反过来钉一边:正常那条路(我动手之前库里记的就是这个文件)照旧刷新,
        不然每次回写都会让下一轮索引白重建一遍。

        **tag 要同时落库**:真实链路是「落库 → 提交 → 回写 md」
        (`save_extraction` 把 topic/entity `INSERT OR IGNORE` 进 `doc_tags`,
        随后 `writeback_extraction` 才去改 frontmatter),所以登记那一步看到的
        两边本来就是一致的。只写 md 不落库的那一档是**磁盘领先于索引**,新判据
        拒绝它是对的 —— 那一档在 `tests/test_doc_rewrite_tags.py` 里单独钉着。
        """
        v1 = self.seed("none", OLD_BODY, flagged=False)
        self.conn.execute("INSERT OR IGNORE INTO doc_tags (path, tag)"
                          " VALUES (?, 'topic/x')", (PATH,))
        self.conn.commit()
        before = self.md.read_text(encoding="utf-8")
        self.assertEqual(v1, scanner.file_hash(before), "摆盘:库里记的就是它")
        written = before.replace("---\n这是", "  - topic/x\n---\n这是")
        self.md.write_text(written, encoding="utf-8")
        extract.refresh_content_hash(self.conn, PATH, before, written)
        self.conn.commit()
        self.assertEqual(scanner.file_hash(written), self.version())


class TagRepairTest(_Vault):
    """回写失败留下的待办,`sync-tags` 要能不花钱地修回来。

    交付跨两个存储:kb.db 的事务管不着 vault 文件。权限错误、磁盘满、进程被杀
    都能让判定落了库而 md 那行还在,而这一篇按 `promo_level <> ''` 从此掉出所有
    默认候选 —— 没有持久化待办,谁也不会再回来看它一眼。
    """

    def _deliver_with_readonly_md(self):
        """真实注入一次写失败:把 md 文件本身设成只读,交付完再恢复。"""
        self.md.chmod(0o444)
        try:
            cand = self.cand()
            with self.env():
                return self.pb._deliver(self.conn, cand,
                                        {"level": "none", "reason": "正常资讯"})
        finally:
            self.md.chmod(0o644)

    def test_a_failed_write_leaves_a_repairable_todo(self):
        self.seed("hard", OLD_BODY, flagged=True)
        self.assertEqual(("", False), self._deliver_with_readonly_md())
        # 判定落了库,md 那行还在 —— 这**不是**一次成功的交付
        self.assertEqual("none", self.level())
        self.assertFalse(self.db_flag())
        self.assertTrue(self.md_flag())
        self.assertTrue(self.pending(), "没留下可修的待办")

    def test_sync_tags_repairs_it_after_the_failure_clears(self):
        self.seed("hard", OLD_BODY, flagged=True)
        self._deliver_with_readonly_md()
        with self.env() as out:
            self.assertEqual(0, self.pb.cmd_sync_tags(
                SimpleNamespace(account=[ACCOUNT])))
        self.assertFalse(self.md_flag(), "md 那行还挂着")
        self.assertFalse(self.pending(), "待办没清")
        self.assertIn("对齐 1 篇", out.getvalue())
        # 末态钉在后果上:下一轮索引不会再把那行读回 doc_tags
        self.assertFalse(self.db_flag())

    def test_a_second_recheck_also_takes_it_off(self):
        """报告里那条路:撤去故障之后**重判一次**也该修好。

        原来的判据是「上一次是不是 hard」,失败之后库里已经是 none,第二次重判
        拿不到那个信号、压根不会碰文件,于是两处永久不一致、两次都报成功。
        """
        self.seed("hard", OLD_BODY, flagged=True)
        self._deliver_with_readonly_md()
        with self.env(reply='{"level":"none","reason":"核对过,是正常资讯"}'):
            self.assertEqual(0, self.pb.cmd_run(self.args()))
        self.assertFalse(self.md_flag())
        self.assertFalse(self.pending())

    def test_an_extraction_writeback_failure_also_leaves_a_todo(self):
        """**提取**那三条路上回写失败,同样要留下待办。

        待办不是补判脚本的私事:`writeback_extraction` 是四条路的共用入口,
        管道、`kb extract --writeback`、并行脚本都从它过。标在那个入口里,多一个
        调用方就少一处可漏的地方 —— 补判这一侧另有一道兜底(`commit_promo` 的
        except 收得更宽),所以只在补判上钉住的话,提取那三条路是裸的。
        """
        self.seed("", OLD_BODY, flagged=False)
        data = {"entities": [], "topics": [], "summary": "s",
                "promo": {"level": "hard", "reason": "文末在卖代充值"}}
        seen = extract.model_input(self.conn, PATH)["seen"]
        self.md.chmod(0o444)
        try:
            with self.env(), self.assertRaises(OSError):
                extract.commit_extraction(self.conn, PATH, data, seen)
        finally:
            self.md.chmod(0o644)
        self.assertEqual("hard", self.level(), "判定该落库的照样要落库")
        self.assertFalse(self.md_flag(), "夹具没摆成「md 那行没写进去」")
        self.assertTrue(self.pending(), "提取那条路上的回写失败没留下待办")

    def test_nothing_to_repair_is_a_noop(self):
        """反过来钉一边:没有待办时 `sync-tags` 一个文件都不许碰。"""
        self.seed("hard", OLD_BODY, flagged=True)
        before = self.md.read_text(encoding="utf-8")
        with self.env() as out:
            self.assertEqual(0, self.pb.cmd_sync_tags(
                SimpleNamespace(account=None)))
        self.assertIn("待对齐 0 篇", out.getvalue())
        self.assertEqual(before, self.md.read_text(encoding="utf-8"))


class QuotedTagTest(_Vault):
    """合法 YAML 的另外几种写法:摘得掉的要摘掉,摘不掉的不许报成功。

    索引读 tags 走 `yaml.safe_load`(`scanner.frontmatter_tags`),所以
    `- "flag/软广"` 和 `- flag/软广` 对它是同一个标签 —— 按行删的那条正则
    认不出引号的话,这一行就留在 md 里被索引读回 `doc_tags`。
    """

    def _write(self, flag_line: str) -> None:
        self.seed("none", OLD_BODY, flagged=False)
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(text.replace("  - source/dr-gzh\n",
                                        f"  - source/dr-gzh\n{flag_line}"),
                           encoding="utf-8")

    def test_a_quoted_tag_is_removed(self):
        self._write(f'  - "{extract.PROMO_TAG}"\n')
        self.assertTrue(extract.remove_writeback_tag(PATH, extract.PROMO_TAG))
        self.assertFalse(extract.promo_tag_in_md(PATH))

    def test_a_flow_style_tag_is_reported_not_swallowed(self):
        """flow-style(`tags: [flag/软广]`)按行删不掉 —— 必须抛,不许返回「没有」。

        这条才是判据的归宿:摘除那边**不自己判**摘干净了没有,由
        `align_promo_tag` 用索引的读法回读确认,对不上就抛 → 留待办 → 如实记
        一次没完成的交付。静默返回 False 的话,库里是 none、md 里那行还在,
        而交付方报成功。
        """
        self.seed("none", OLD_BODY, flagged=False)
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(
            text.replace("tags:\n  - source/dr-gzh\n",
                         f'tags: [source/dr-gzh, "{extract.PROMO_TAG}"]\n'),
            encoding="utf-8")
        self.assertTrue(extract.promo_tag_in_md(PATH), "夹具没摆对")
        with self.assertRaises(extract.PromoTagNotAligned):
            extract.align_promo_tag(self.conn, PATH)


TUTORIAL = "htmx 2.0 上手教程:先装依赖,再写 hx-get。originaltutorial。"
SHELL_AD = "htmx 2.0 上手教程:先装依赖,再写 hx-get。加微信办理代充值。"
PATH2 = "source/dr-gzh/tut.md"


class CandidateSnapshotTest(_Vault):
    """候选的标题/账号/正文/版本来自**一条语句**,不是两次查询拼起来的。

    分两次读的话,两次之间另一条连接的索引可以把这一篇换版:候选里就成了旧标题
    配新正文,而后面那道按 `content_hash` 的条件写拿到的是新版本号、照旧放行 ——
    判的是一份没人写过的东西。把换版插进两条 SQL 之间那一瞬间,夹具控不住,所以
    这里钉的是「四样同属一个快照」这件结构事实:名单查询只负责给出 path。
    """

    def test_the_candidate_comes_from_one_snapshot(self):
        self.seed("", OLD_BODY, flagged=False)
        snap = {"path": PATH, "title": "换版之后的标题", "account": ACCOUNT,
                "version": "换版之后的号", "body": "换版之后的正文"}
        with mock.patch.object(store, "doc_input_snapshot",
                               return_value=dict(snap)) as spy:
            cands = self.pb._candidates(self.conn, 0, [ACCOUNT], False)
        spy.assert_called_once()
        self.assertEqual([snap], cands,
                         "候选把名单查询那一次的标题/账号混了进来")


class GroupEndStateTest(_Vault):
    """同号、同题、长度落在同一个 500 字桶里、**正文不同** —— 各自判。

    旧归组键是「账号 + 标题 + 正文长度桶」,那是个猜:一个号把同一个标题反复
    用是合法输入,广告段有没有也可能只差几十个字。报告的确定性复现里,广告那篇
    稍长当了代表,正常教程跟着被判 hard —— 连实体带条目一起从检索和图鉴里摘掉,
    理由写着「同稿判定:…」,而脚本报的是「软广 ×2」。

    判定替身只实现一条无歧义的规则(读到代充值段就 hard),不依赖真实模型会不会
    误判;断言落在两篇各自的末态和调用次数上。
    """

    def _judge_by_content(self, title, body, account, **kw):
        self.seen.append(body)
        if "代充值" in body:
            return {"level": "hard", "reason": "文末在卖代充值"}
        return {"level": "none", "reason": "正常教程"}

    def setUp(self):
        super().setUp()
        self.seen = []

    def test_a_different_body_is_judged_on_its_own(self):
        self.seed("", SHELL_AD, flagged=False)
        self.seed("", TUTORIAL, flagged=False, path=PATH2)
        with self.env(judge=self._judge_by_content) as out:
            self.assertEqual(0, self.pb.cmd_run(self.args(recheck=False,
                                                          account=None)))
        text = out.getvalue()
        self.assertEqual(2, len(self.seen), "两篇正文不同,必须各判一次")
        self.assertEqual("hard", self.level(), "广告那篇")
        self.assertEqual("none", self.level(PATH2), "正常教程被广播成广告了")
        self.assertFalse(self.db_flag(PATH2))
        self.assertNotIn("同稿判定", self.reason(PATH2))
        self.assertIn("软广 ×1", text)
        self.assertNotIn("软广 ×2", text)

    def test_an_identical_body_is_still_judged_once(self):
        """反过来钉一边:正文逐字相同的两篇照旧只判一次 —— 那是这套归组省钱的
        全部依据,丢掉它等于把补判的调用量按副本数翻上去。"""
        self.seed("", SHELL_AD, flagged=False)
        self.seed("", SHELL_AD, flagged=False, path=PATH2)
        with self.env(judge=self._judge_by_content) as out:
            self.assertEqual(0, self.pb.cmd_run(self.args(recheck=False,
                                                          account=None)))
        self.assertEqual(1, len(self.seen), "同一份稿子判了两次")
        self.assertEqual("hard", self.level())
        self.assertEqual("hard", self.level(PATH2))
        self.assertIn("同稿判定", self.reason(PATH2))
        self.assertIn("软广 ×2", out.getvalue())


if __name__ == "__main__":
    unittest.main()
