# -*- coding: utf-8 -*-
"""软广交付那几份测试共用的摆盘:一篇**已经判过、也已经索引过**的文档。

`tests/test_promo_delivery.py` 两边各追加了几条之后越过仓库约定的 700 行上限,
所以把共用的那部分搬到这里(手法同 `tests/doc_rewrite_fixture.py`)。消费方:
`test_promo_delivery`(交付本体)、`test_promo_todo`(待办与 `sync-tags`)、
`test_atomic_md_write`(写盘被 SIGKILL 打断)、`test_input_unreadable`
(模型输入读不出来那一档)。

**摆盘要摆成真实索引的那个形状,不是「够这条用例用」的形状。** 这里每一样都有
它的理由(各自写在方法上):frontmatter 里带 path 自己的 url、account 也要写进
frontmatter、md 上那组 tag 要跟着进 `doc_tags`、候选从 `store.doc_input_snapshot`
真取 —— 少摆一样,用例红出来的就不是它本来要钉的那个原因。
"""
from __future__ import annotations

import contextlib
import io as _io
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
