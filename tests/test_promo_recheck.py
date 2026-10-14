# -*- coding: utf-8 -*-
"""存量重判(`promo_backfill --recheck`)这条路上的两道闸。

重判和首次补判的方向正好相反。首次是「这一篇还没有结论」,`normalize_promo`
的「认不出一律落 none」在那儿是对的:宁可漏判留着一篇广告,也不要错杀一篇正常
文章。重判是**去覆盖一个已经有的结论**,同一个兜底在这儿变成「把判出来的 hard
擦成 none」——广告重新进检索、`flag/软广` 连 DB 带 md 一起摘掉,而 `extracted_at`
还在、`pending_docs` 是空的,再同步多少轮都不会自己纠正。

所以这条路上两件事都不许发生:

1. **没拿到合法判定,不许当成「这篇不是广告」。** 重判开 `require_verdict`,
   拿不到就抛,整组记失败、旧结论一个字不动,下次重跑。
2. **旧正文的判定不许盖掉新正文的结论。** 判据钉在 `save_promo` 那条 UPDATE
   自己的 WHERE 里(`content_hash`),0 行 = 这期间换版了 = 这一轮跳过。
3. **判定、`doc_tags`、md 那行必须一起成立。** 三件事在 `commit_promo` 的同一个
   协调区间(维护锁)里对齐,中间不许插进一次索引 —— 插进来就会按**还没摘**的
   frontmatter 把 `flag/软广` 重新插回库里,而回写只摘 md、不再回头清它,还顺手
   把 `content_hash` 刷成当前文件的,于是下一轮索引直接跳过,错的那行永久留下。

判据一律落在「库里那行最后是什么」「md 里那行还在不在」「脚本报了几篇」,
不落在「传了哪个参数」「抛没抛」。每条正断言都配一条反向的:默认那条路
(首次补判)的行为一个字都不许变。

跑:  PYTHONPATH=<worktree 根> python -m unittest tests.test_promo_recheck
"""

from __future__ import annotations

import contextlib
import io as _io
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from kb import extract, maintenance, scanner, store            # noqa: E402
import promo_backfill                                          # noqa: E402

_REAL_CONNECT = store.connect          # cmd_run 的 finally 会 close 它拿到的
                                       # 那条连接,所以每次交给它一条新的,
                                       # 夹具自己那条留着 seed 和断言用
ACCOUNT = "搬运号"
PATH = "source/dr-gzh/ad.md"
PATH2 = "source/dr-gzh/ad-copy.md"       # 同稿副本:同账号 + 同标题 + 同长度桶
OLD_BODY = "这是一篇正常行业资讯,讲谷歌发布了新模型。"
NEW_BODY = "这是会员推广。加微信 reviewsvc 办理代充值,官方一半价格。"


def _md(body: str, flagged: bool, path: str = PATH) -> str:
    """一份 md。**frontmatter 带 path 自己的 url** —— 同稿的两个副本在真实库里
    各有各的链接和落库时间,`content_hash` 必然不同;夹具里写成逐字相同的话,
    「每篇各按自己那一版的号判」和「整组共用代表篇那一版的号」就区分不出来。
    """
    flag = f"  - {extract.PROMO_TAG}\n" if flagged else ""
    return (f'---\ntitle: "稿子"\nurl: "https://example.org/{Path(path).stem}"\n'
            f'tags:\n  - source/dr-gzh\n{flag}---\n{body}\n')


class _Fixture(unittest.TestCase):
    """一篇已经判过、也已经索引过的文档:库里有行、chunks 有正文、vault 有 md。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="kb-promo-recheck-")
        root = Path(self._tmp.name)
        self.root = root
        self.db = root / "kb.db"
        self.vault = root / "vault"
        (self.vault / "source" / "dr-gzh").mkdir(parents=True)
        self.md = self.vault / PATH
        self.conn = _REAL_CONNECT(self.db)
        self._enter = mock.patch.object(extract, "VAULT_ROOT", self.vault)
        self._enter.start()
        self.addCleanup(self._enter.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.conn.close)

    def seed(self, level: str, body: str, flagged: bool,
             path: str = PATH) -> str:
        """落一篇文档,返回它此刻的 content_hash(= 正文那一版的号)。

        `path` 给第二篇用:同账号、同标题、同长度桶 = 补判眼里的「同一份稿子」,
        一组里只判一次、结论广播给其余篇(见 `promo_backfill._group`)。
        """
        from kb.scanner import file_hash
        text = _md(body, flagged, path)
        full = self.vault / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
        version = file_hash(text)
        self.conn.execute("DELETE FROM documents WHERE path=?", (path,))
        self.conn.execute(
            "INSERT INTO documents (path, source, title, account, url,"
            " content_hash, extracted_at, promo_level, promo_reason, promo_at)"
            " VALUES (?,'dr-gzh','稿子',?,?,?,"
            "'2026-10-14 10:00:00',?,'原判定','2026-10-14 10:00:00')",
            (path, ACCOUNT, f"https://example.org/{Path(path).stem}",
             version, level))
        self.conn.execute("DELETE FROM chunks WHERE path=?", (path,))
        self.conn.execute(
            "INSERT INTO chunks (path, seq, text) VALUES (?,0,?)", (path, body))
        if flagged:
            self.conn.execute(
                "INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?,?)",
                (path, extract.PROMO_TAG))
        self.conn.commit()
        return version

    def level(self, path: str = PATH) -> str:
        return self.conn.execute(
            "SELECT promo_level FROM documents WHERE path=?", (path,)
        ).fetchone()["promo_level"]

    def reason(self) -> str:
        return self.conn.execute(
            "SELECT promo_reason FROM documents WHERE path=?", (PATH,)
        ).fetchone()["promo_reason"]

    def db_flag(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM doc_tags WHERE path=? AND tag=?",
            (PATH, extract.PROMO_TAG)).fetchone() is not None

    def md_flag(self) -> bool:
        return extract.PROMO_TAG in self.md.read_text(encoding="utf-8")


class StrictVerdictTest(unittest.TestCase):
    """`normalize_promo` 的 strict:认不出就抛,不替模型编一个 none 出来。"""

    def test_missing_level_raises(self):
        with self.assertRaises(ValueError):
            extract.normalize_promo({}, strict=True)

    def test_unknown_level_raises(self):
        with self.assertRaises(ValueError):
            extract.normalize_promo({"level": "ads"}, strict=True)

    def test_explicit_none_is_a_verdict(self):
        """明确说「不是广告」要放行 —— 这条路存在的意义就是能改判回来。"""
        got = extract.normalize_promo({"level": "none", "reason": "正常内容"},
                                      strict=True)
        self.assertEqual(got["level"], "none")

    def test_default_still_falls_back(self):
        """反过来钉一边:首次判定那条路的兜底一个字不许变。"""
        for raw in ({}, {"level": "ads"}, None, "???"):
            self.assertEqual(extract.normalize_promo(raw)["level"], "none")


class JudgeRetryTest(unittest.TestCase):
    """`judge_promo` 的 require_verdict:走完重试仍拿不到判定就抛。"""

    def _run(self, reply: str, **kw):
        calls = []

        def chat(prompt, model, **kwargs):
            calls.append(prompt)
            return reply

        with mock.patch.dict(sys.modules), \
             mock.patch("kb.llm.chat", chat), \
             mock.patch("kb.llm_stages.stage_model", return_value="stub"):
            try:
                return extract.judge_promo("标题", "正文", ACCOUNT, **kw), calls
            except Exception as e:                      # noqa: BLE001
                return e, calls

    def test_require_verdict_raises_after_retrying(self):
        got, calls = self._run("{}", require_verdict=True)
        self.assertIsInstance(got, ValueError)
        self.assertEqual(len(calls), 2, "坏回包要和坏 JSON 一样原地重试一次")

    def test_default_path_returns_none(self):
        """反过来钉一边:不开 require_verdict 时行为一个字不许变。"""
        got, _ = self._run("{}")
        self.assertEqual(got["level"], "none")


class ConditionalWriteTest(_Fixture):
    """`save_promo` 的条件写:判据在 UPDATE 自己的 WHERE 里。"""

    def test_stale_version_writes_nothing(self):
        seen = self.seed("hard", NEW_BODY, flagged=True)
        self.seed("hard", NEW_BODY, flagged=True)   # 重落一遍,换掉 content_hash
        self.conn.execute("UPDATE documents SET content_hash='换过版了'"
                          " WHERE path=?", (PATH,))
        written, stale = extract.save_promo(
            self.conn, PATH, {"level": "none", "reason": "旧正文的结论"},
            version_seen=seen)
        self.assertFalse(written)
        self.assertFalse(stale, "没写进去就不许让调用方去摘 md 里那行")
        self.assertEqual(self.level(), "hard", "库里那行一个字都不许动")
        self.assertEqual(self.reason(), "原判定")
        self.assertTrue(self.db_flag(), "tag 也一个都不许动")

    def test_current_version_writes(self):
        seen = self.seed("hard", NEW_BODY, flagged=True)
        written, stale = extract.save_promo(
            self.conn, PATH, {"level": "none", "reason": "确实判错了"},
            version_seen=seen)
        self.assertTrue(written)
        self.assertTrue(stale, "hard 改判成 none 要让调用方去摘 md 里那行")
        self.conn.commit()
        self.assertEqual(self.level(), "none")
        self.assertFalse(self.db_flag())

    def test_without_version_seen_writes_unconditionally(self):
        """反过来钉一边:提取流水线那条路不传版本号,行为一个字不许变

        (它的正文版本由 `commit_extraction` 在维护锁 + 写事务里校验,比单条
        语句的条件写更严,所以这里不该再拦一道)。
        """
        self.seed("hard", NEW_BODY, flagged=True)
        self.conn.execute("UPDATE documents SET content_hash='换过版了'"
                          " WHERE path=?", (PATH,))
        written, stale = extract.save_promo(
            self.conn, PATH, {"level": "none", "reason": "r"})
        self.assertTrue(written)
        self.conn.commit()
        self.assertEqual(self.level(), "none")


class RecheckEndStateTest(_Fixture):
    """跑真实的 `cmd_run --recheck`,判据落在末态和它报出来的数上。"""

    def _recheck(self, *, reply: str | None = None, judge=None) -> int:
        """跑一轮重判,顺带把它打印的那几行收下来 —— 统计是交付物的一部分:
        元规则要求「按实际写入结果报、跳过的如实列出」,而一次把 hard 擦成
        none 又报「全部成功」的运行,在面板和终端上跟正常的一模一样。"""
        self.out = _io.StringIO()
        with contextlib.redirect_stdout(self.out):
            return self._run(reply, judge)

    def _run(self, reply, judge) -> int:
        args = SimpleNamespace(account=[ACCOUNT], recheck=True,
                               dry_run=False, limit=0)
        with mock.patch.object(store, "connect",
                               lambda *a, **k: _REAL_CONNECT(self.db)), \
             mock.patch.object(maintenance, "DATA_DIR",
                               Path(self._tmp.name)), \
             mock.patch("kb.llm_stages.stage_model", return_value="stub"):
            if judge is not None:
                with mock.patch.object(promo_backfill, "judge_promo",
                                       side_effect=judge):
                    return promo_backfill.cmd_run(args)
            with mock.patch("kb.llm.chat", return_value=reply):
                return promo_backfill.cmd_run(args)

    def test_missing_verdict_keeps_the_existing_hard(self):
        """模型回了一个合法 JSON 但里面没有 level —— 旧的 hard 必须留着。

        这是最要命的那一种:兜底的 none 会让这篇广告重新进检索、两处 flag
        一起消失,而脚本报「全部成功」,没有任何一处会再提醒。
        """
        self.seed("hard", NEW_BODY, flagged=True)
        rc = self._recheck(reply="{}")
        self.assertEqual(rc, 0)
        self.assertIn("判定 0 篇", self.out.getvalue())
        self.assertIn("失败组 1", self.out.getvalue())
        self.assertEqual(self.level(), "hard")
        self.assertEqual(self.reason(), "原判定")
        self.assertTrue(self.db_flag())
        self.assertTrue(self.md_flag())

    def test_explicit_reclassification_still_goes_through(self):
        """反过来钉一边:模型**明确**说不是广告时,照旧改判、照旧摘 flag。

        不然这道闸就成了「hard 再也改不回来」,比原来的问题更糟。
        """
        self.seed("hard", NEW_BODY, flagged=True)
        rc = self._recheck(reply=json.dumps(
            {"level": "none", "reason": "核对过,是正常资讯"},
            ensure_ascii=False))
        self.assertEqual(rc, 0)
        self.assertEqual(self.level(), "none")
        self.assertFalse(self.db_flag())
        self.assertFalse(self.md_flag(), "md 里那行也得摘掉")

    def test_body_changed_midflight_keeps_the_newer_verdict(self):
        """模型在途时正文整篇换了、新那版已经被判成 hard —— 旧结论不许盖上去。

        换版那一步走的是真实的 index 侧写法(改 chunks + content_hash),
        新结论走真实的 `save_promo`;这里只控制「模型什么时候回包」。
        """
        self.seed("none", OLD_BODY, flagged=False)
        fresh = {}

        def judge_then_swap(title, body, account, **kwargs):
            self.assertEqual(body, OLD_BODY, "worker 读到的该是旧正文")
            # 另一边(另一条连接):补抓 → 导出 → 索引把这一篇换成新正文,
            # 随后正常提取把新那版判成 hard。锁序照生产:维护锁 → 写事务。
            from kb.scanner import file_hash
            text = _md(NEW_BODY, flagged=False, path=PATH)
            # 这段跑在 worker 线程里:SQLite 连接不许跨线程,所以在这儿开、
            # 也在这儿关,不挂 addCleanup(那是主线程执行的)
            other = _REAL_CONNECT(self.db)
            try:
                with maintenance.maintenance_lock():
                    other.execute("BEGIN IMMEDIATE")
                    self.md.write_text(text, encoding="utf-8")
                    other.execute("UPDATE chunks SET text=? WHERE path=?",
                                  (NEW_BODY, PATH))
                    other.execute("UPDATE documents SET content_hash=?"
                                  " WHERE path=?", (file_hash(text), PATH))
                    extract.save_promo(other, PATH,
                                       {"level": "hard",
                                        "reason": "新正文在卖代充值"})
                    other.commit()
                fresh["level"] = other.execute(
                    "SELECT promo_level FROM documents WHERE path=?",
                    (PATH,)).fetchone()["promo_level"]
            finally:
                other.close()
            return {"level": "none", "reason": "旧正文是正常资讯"}

        rc = self._recheck(judge=judge_then_swap)
        self.assertEqual(rc, 0)
        self.assertIn("判定 0 篇", self.out.getvalue())
        self.assertIn("跳过 1 篇", self.out.getvalue())
        self.assertEqual(fresh["level"], "hard", "换版那一步自己得先成立")
        self.assertEqual(self.level(), "hard",
                         "旧正文的 none 不许盖掉新正文判出来的 hard")
        self.assertEqual(self.reason(), "新正文在卖代充值")
        self.assertTrue(self.db_flag())


class BroadcastCountTest(_Fixture):
    """同稿组里一篇换了版:广播只落到没换版的那几篇,报的数也得是那个数。

    元规则:统计按**实际写入结果**报、跳过的如实列出,不按计划长度报。
    一组 2 篇、只写进去 1 篇却打印「软广 ×2」,末态和报告就对不上了 ——
    而这正是人决定「要不要接着跑几千篇」时唯一看得到的东西。
    """

    def _run_group(self, judge):
        out = _io.StringIO()
        args = SimpleNamespace(account=[ACCOUNT], recheck=True,
                               dry_run=False, limit=0)
        with contextlib.redirect_stdout(out), \
             mock.patch.object(store, "connect",
                               lambda *a, **k: _REAL_CONNECT(self.db)), \
             mock.patch.object(maintenance, "DATA_DIR", Path(self._tmp.name)), \
             mock.patch("kb.llm_stages.stage_model", return_value="stub"), \
             mock.patch.object(promo_backfill, "judge_promo",
                               side_effect=judge):
            self.assertEqual(promo_backfill.cmd_run(args), 0)
        return out.getvalue()

    def test_nothing_moved_broadcasts_to_every_copy(self):
        """反过来钉一边:没有谁换版时,一次判定要落到组里**每一篇**。

        这条管的是那道闸别关得太死。每篇各按**自己**那一版的号判
        —— 整组共用代表篇那一版的话,非代表篇的 `content_hash` 天生就和它
        不同(各自的 url、落库时间都写在 frontmatter 里),于是广播一篇都写
        不进去,而终端上只会多出几行「跳过」。
        """
        self.seed("none", OLD_BODY, flagged=False)
        self.seed("none", OLD_BODY, flagged=False, path=PATH2)
        text = self._run_group(
            lambda *a, **k: {"level": "hard", "reason": "借壳卖代充值"})
        self.assertEqual(self.level(), "hard")
        self.assertEqual(self.level(PATH2), "hard", "同稿副本也要落上")
        self.assertIn("判定 2 篇,hard 2,", text)
        self.assertIn("跳过 0 篇", text)

    def test_counts_only_what_was_written(self):
        self.seed("none", OLD_BODY, flagged=False)
        self.seed("none", OLD_BODY, flagged=False, path=PATH2)

        def judge_then_swap_the_sibling(title, body, account, **kwargs):
            from kb.scanner import file_hash
            text = _md(NEW_BODY, flagged=False, path=PATH2)
            (self.vault / PATH2).write_text(text, encoding="utf-8")
            other = _REAL_CONNECT(self.db)
            try:
                with maintenance.maintenance_lock():
                    other.execute("BEGIN IMMEDIATE")
                    other.execute("UPDATE documents SET content_hash=?"
                                  " WHERE path=?", (file_hash(text), PATH2))
                    other.commit()
            finally:
                other.close()
            return {"level": "hard", "reason": "借壳卖代充值"}

        out = _io.StringIO()
        args = SimpleNamespace(account=[ACCOUNT], recheck=True,
                               dry_run=False, limit=0)
        with contextlib.redirect_stdout(out), \
             mock.patch.object(store, "connect",
                               lambda *a, **k: _REAL_CONNECT(self.db)), \
             mock.patch.object(maintenance, "DATA_DIR", Path(self._tmp.name)), \
             mock.patch("kb.llm_stages.stage_model", return_value="stub"), \
             mock.patch.object(promo_backfill, "judge_promo",
                               side_effect=judge_then_swap_the_sibling):
            self.assertEqual(promo_backfill.cmd_run(args), 0)

        text = out.getvalue()
        self.assertEqual(self.level(), "hard", "没换版那篇照常落 hard")
        self.assertEqual(self.level(PATH2), "none", "换了版那篇不许被广播")
        self.assertIn("软广 ×1", text, "明细那行报的是实际写进去的那个数")
        self.assertNotIn("软广 ×2", text)
        self.assertIn("判定 1 篇,hard 1,", text, "汇总行同理")
        self.assertIn("跳过 1 篇", text)


class AtomicDeliveryTest(_Fixture):
    """判定、`doc_tags`、md 那行在同一个协调区间里对齐 —— 中间插不进一次索引。

    和上面两组的区别:那两组管的是「这一轮该不该写」,这一组管的是「写的那三处
    会不会只成了一半」。半成品的末态**不可恢复**:`promo_level` 已经是 none、
    `content_hash` 已经刷成当前文件的,所以普通检索正常、下一轮索引报「未变」,
    唯独按 `flag/软广` 过滤仍然捞得到这篇已经纠正为正常内容的文章,再同步、
    再索引多少轮都不会自己纠正。
    """

    def _touched_by_someone_else(self) -> None:
        """摆一个合法的前置:别的流程刚改过这一篇的 frontmatter(正文没动)。

        **少了这一步,交错那条用例对任何版本都是绿的**:索引拿到锁时会先问
        `doc_needs_reindex`,文件和库里对得上就记一次「未变」直接跳过 ——
        `upsert_document` 压根没进过执行路径,也就没有机会按旧 frontmatter 把
        `flag/软广` 重建回来(实测注入旧结构红 0 条)。真实库里这一步天天发生:
        翻译回写、tag 回写、wiki 追加图谱尾区都只改 frontmatter。
        """
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(text.replace('title:', 'wiki_round: 2\ntitle:', 1),
                           encoding="utf-8")

    def _scan_at_index_time(self):
        """索引那一趟的扫描结果 —— **被迭代时**才读磁盘。

        真实 `cli.cmd_index` 是「取锁 → 扫描」,扫描读到的是**拿到锁那一刻**
        磁盘上的内容(见它自己的 docstring)。预先构造好 Doc 再交给它,等于摆出
        一个锁外读盘的时序:那样无论协调区间圈不圈得住,索引都会拿着带旧 flag
        的 frontmatter 去重建 `doc_tags`,红绿都证明不了互斥。
        """
        text = self.md.read_text(encoding="utf-8")
        fm, body = scanner.parse_frontmatter(text)
        yield scanner.Doc(path=PATH, source="dr-gzh", title="稿子",
                          account=ACCOUNT, published="2026-10-14",
                          url=f"https://example.org/{Path(PATH).stem}",
                          tags=fm.get("tags") or [], body=body,
                          content_hash=scanner.file_hash(text), meta=fm)

    def test_an_interleaved_index_cannot_restore_the_cleared_flag(self):
        """改判落库之后、摘 md 那行之前跑一趟索引:摘掉的 flag 不许被带回来。

        索引这一趟全程是真的(真实 `cmd_index`、真实维护锁、真实
        `upsert_document` 按 frontmatter 重建 `doc_tags`、真实提交),只有
        「它什么时候起跑」由夹具控制 —— 那是一个完全合法的交错,不是抢锁。
        """
        from kb import cli

        self.seed("hard", OLD_BODY, flagged=True)
        self._touched_by_someone_else()
        main = threading.current_thread()
        holding = threading.Event()
        real_lock = maintenance.maintenance_lock
        seen = {}

        @contextlib.contextmanager
        def watched(*a, **kw):
            mine = threading.current_thread() is not main
            with real_lock(*a, **kw):
                if mine:
                    # **索引真的拿到了那把锁** —— 判据钉在这里,不是钉在「它
                    # 站到了要取锁的位置上」:后者在取锁**之前**就置位了,主线程
                    # 收到之后照样可能先抢到锁,于是注入旧结构也有一半的运行是
                    # 绿的(实测红 0 条)。
                    holding.set()
                yield

        def index_now():
            with mock.patch.object(cli, "scan_all", self._scan_at_index_time):
                return cli.cmd_index(SimpleNamespace(reembed=False,
                                                     no_vector=True))

        real_save = promo_backfill.commit_promo.__globals__["save_promo"]

        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = {}

            def save_then_let_an_index_butt_in(conn, path, promo, **kw):
                """钩子挂在**落库**上,不是挂在回写上。

                这是两个版本都存在的那个点,所以它对「协调区间圈到哪儿」是敏感
                的:修好之后这一下在锁内,索引只能排在后面;圈不住的话这一下在
                锁外,索引整趟跑完还来得及按旧 frontmatter 把 flag 插回去。
                挂在 `sync_promo_tag` 上**测不出**这件事 —— 回写那一下在两个
                版本里都在锁内,索引一律被挡住,注入旧结构照样全绿(实测红 0 条)。
                """
                written, stale = real_save(conn, path, promo, **kw)
                conn.commit()       # 旧结构里落库紧跟着就整组提交了
                fut["f"] = pool.submit(index_now)
                # 圈住了的话索引拿不到锁,这里必然等满 —— 那个超时就是互斥本身
                seen["index_got_in"] = holding.wait(2)
                return written, stale

            # **索引线程要全程待在打桩范围里**(见 `_recheck_env` 的 docstring):
            # 它此刻还在等锁,`store.connect` 的替身一旦提前恢复,它醒来后会连到
            # 真实的 `$KB_DATA_DIR/kb.db`。
            with self._recheck_env() as out, \
                 mock.patch.object(maintenance, "maintenance_lock", watched), \
                 mock.patch.dict(promo_backfill.commit_promo.__globals__,
                                 {"save_promo": save_then_let_an_index_butt_in}):
                self.assertEqual(0, promo_backfill.cmd_run(self._args()))
                self.assertEqual(0, fut["f"].result(timeout=30),
                                 "索引那一趟没跑成")
            self.out = out

        # 末态先断:三处必须一致,而且下一轮索引没有再纠正的机会
        self.assertEqual("none", self.level(), "明确改判没落下来")
        self.assertFalse(self.md_flag(), "md 里那行没摘掉")
        self.assertFalse(self.db_flag(),
                         "交错的那趟索引把摘掉的 flag 又插回库里了")
        text = self.md.read_text(encoding="utf-8")
        self.assertFalse(
            store.doc_needs_reindex(self.conn, PATH, scanner.file_hash(text)),
            "content_hash 已经刷成当前文件的了 —— 下一轮索引不会再看这一篇")
        # 再断过程:那趟索引压根没能在协调区间里拿到锁
        self.assertFalse(seen["index_got_in"],
                         "索引在判定和两处标记对齐的中途拿到了维护锁")

    def test_a_frontmatter_only_edit_still_lets_the_flag_come_off(self):
        """别的流程只改过 frontmatter(正文一个字没动)—— 改判照样摘得掉那行。

        反过来钉住上一条的代价:回写那一侧**不许**比正文指纹。`_body_moved` 的
        第一道比的是整个**文件**的 hash,只改 frontmatter 也会让它说"版本不对";
        传下去的话这一次完全合法的改判就被判成作废,末态是库里 none、
        frontmatter 还挂着 flag —— 下一次索引按 frontmatter 重建 `doc_tags`,
        错的那行又回来了,而 `promo_level` 已经非空、默认补判再也不会选它。
        """
        self.seed("hard", OLD_BODY, flagged=True)
        self._touched_by_someone_else()
        self.assertTrue(store.doc_needs_reindex(
            self.conn, PATH, scanner.file_hash(
                self.md.read_text(encoding="utf-8"))),
            "夹具没摆出「md 比索引新」那个状态")
        self.assertEqual(0, self._recheck_quietly())
        self.assertEqual("none", self.level())
        self.assertFalse(self.db_flag())
        self.assertFalse(self.md_flag(), "正文没动,md 里那行 flag 必须摘掉")

    @staticmethod
    def _args():
        return SimpleNamespace(account=[ACCOUNT], recheck=True,
                               dry_run=False, limit=0)

    @contextlib.contextmanager
    def _recheck_env(self):
        """重判这一轮要用到的全部打桩,**口径只有这一份**。

        `maintenance.DATA_DIR` 也要指到 tmp —— 不指的话这一轮取的是真实数据
        目录下那把维护锁,和别的测试抢。

        **范围必须覆盖到索引线程跑完**:`store.connect` 的替身提前恢复的话,
        还在等锁的那条索引线程醒来后会连到真实的 `$KB_DATA_DIR/kb.db`,把夹具
        这篇文档写进共享库里 —— 之后任何一条「库里此刻有没有积压」的用例
        (`test_retry_scope` 那两条)就被我们带红了,而且**只在同跑时红、单跑
        这个文件永远是绿的**,查起来极费劲。实测过:基线上同一组合 192 条全绿,
        带着泄漏的替身跑 196 条红 2 条。
        """
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(store, "connect",
                               lambda *a, **k: _REAL_CONNECT(self.db)), \
             mock.patch.object(maintenance, "DATA_DIR", Path(self._tmp.name)), \
             mock.patch("kb.llm_stages.stage_model", return_value="stub"), \
             mock.patch("kb.llm.chat", return_value=json.dumps(
                 {"level": "none", "reason": "核对过,是正常资讯"},
                 ensure_ascii=False)):
            yield out

    def _recheck_quietly(self) -> int:
        with self._recheck_env() as out:
            rc = promo_backfill.cmd_run(self._args())
        self.out = out
        return rc


if __name__ == "__main__":
    unittest.main()
