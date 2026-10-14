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
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from kb import extract, maintenance, store                     # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
