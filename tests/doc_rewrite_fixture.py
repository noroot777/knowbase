# -*- coding: utf-8 -*-
"""「登记 content_hash 即已索引」那组用例的摆盘,两个测试文件共用。

`test_doc_rewrite.py` 验那三层各自的红(窗口、两道证明、等锁收场),
`test_doc_rewrite_roster.py` 按 AST 枚举「谁有资格写那一句」—— 后者压根不用这份
摆盘。摆盘里最花功夫的两样跟「判哪一道」无关:**照索引那条真路径摆一篇真的索引
过的文档**,和**真起独立子进程**去跟它抢(认锁的、不认锁的各一种)。各写一份的话
两边对「什么叫已索引」的前提会各漂各的,而这组用例判的后果不可恢复。
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "wiki"))

from kb import cli, doc_rewrite, extract, maintenance, scanner, search, store  # noqa: E402
from kb.chunker import split_chunks                                            # noqa: E402
from kb.config import Source                                                   # noqa: E402

import backfill_youtube_summaries as summaries   # noqa: E402
import wiki_daily                                # noqa: E402
import wikilib                                   # noqa: E402

_REAL_CONNECT = store.connect

OLD_TOKEN = "oldindexedtoken"
NEW_TOKEN = "rivalchangedtoken"

# 子进程:真起一个进程去改那篇 md。`lock=True` 时它先取**真实**维护锁 ——
# 等待上限压到 0 秒,所以「父进程正持着锁」会立刻以 LockBusy 收场,而不是挂住。
_RIVAL = '''
import sys
from pathlib import Path
target, text, lock = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "lock"
if lock:
    from kb.maintenance import maintenance_lock
    with maintenance_lock():
        target.write_text(text, encoding="utf-8")
else:
    target.write_text(text, encoding="utf-8")
print("rival-wrote-the-file")
'''


# 占位进程:取到真实维护锁就报一声,然后攥着它等 stdin 关闭(父进程说「放手」)。
_HOLDER = """
import sys
from kb.maintenance import maintenance_lock
with maintenance_lock():
    sys.stdout.write("holding\\n")
    sys.stdout.flush()
    sys.stdin.read()
"""


class Vault(unittest.TestCase):
    """一篇**真的索引过**的文档:documents 行、chunks、md 三处一致。

    摆盘不手写 chunks,走 `scanner._doc_from_file` + `split_chunks` +
    `store.upsert_document` 那条真路径 —— 手抄的话「磁盘那组索引输入和索引里
    那一组一致」这道判据会在一个生产里不存在的状态上判,红绿都证明不了什么。
    """

    SOURCES = {"youtube": "dr-youtube-dig", "gzh": "dr-gzh"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="kb-doc-rewrite-")
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.vault = self.root / "vault"
        self.db = self.data / "kb.db"
        self.data.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        for p in (mock.patch.object(store, "connect",
                                    lambda *a, **k: _REAL_CONNECT(self.db)),
                  mock.patch.object(extract, "VAULT_ROOT", self.vault),
                  mock.patch.object(scanner, "VAULT_ROOT", self.vault),
                  mock.patch.object(wikilib, "VAULT", self.vault),
                  mock.patch.object(maintenance, "DATA_DIR", self.data)):
            p.start()
            self.addCleanup(p.stop)
        self.conn = _REAL_CONNECT(self.db)
        self.addCleanup(self.conn.close)

    # ── 摆盘 ──────────────────────────────────────────────────────────────────

    def md_text(self, kind: str, body: str) -> str:
        source = self.SOURCES[kind]
        return (f'---\ntitle: "并发登记审查"\nsource: {source}\n'
                f'account: "审查隔离号"\npublished: "2026-10-14"\n'
                f'url: "https://example.invalid/{kind}-race"\n'
                f"tags:\n  - source/{source}\n---\n{body}\n")

    def doc_of(self, rel: str):
        """照索引那条真路径读一份 Doc(标题/账号/正文/hash 全由它算)。"""
        return scanner._doc_from_file(self.vault / rel,
                                      Source(name="x", root=self.vault))

    def seed(self, kind: str) -> str:
        rel = f"source/{self.SOURCES[kind]}/{kind}-race.md"
        full = self.vault / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(self.md_text(kind, f"{OLD_TOKEN} 旧版导语与旧版正文。"),
                        encoding="utf-8")
        doc = self.doc_of(rel)
        store.upsert_document(self.conn, doc, split_chunks(doc.body), "review")
        self.conn.execute("UPDATE documents SET extracted_at='review'"
                          " WHERE path=?", (rel,))
        self.conn.commit()
        return rel

    # ── 读末态 ────────────────────────────────────────────────────────────────

    def version(self, rel: str) -> str:
        return self.conn.execute("SELECT content_hash FROM documents WHERE path=?",
                                 (rel,)).fetchone()["content_hash"]

    def summary(self, rel: str) -> str:
        return self.conn.execute("SELECT summary FROM documents WHERE path=?",
                                 (rel,)).fetchone()["summary"]

    def disk(self, rel: str) -> str:
        return (self.vault / rel).read_text(encoding="utf-8")

    def needs_index(self, rel: str) -> bool:
        return store.doc_needs_reindex(self.conn, rel,
                                       scanner.file_hash(self.disk(rel)))

    def hits(self, token: str) -> list[str]:
        return [h.path for h in search.search(self.conn, token, use_vector=False,
                                              top_k=20)]

    def reindex(self) -> str:
        """真跑一趟 `kb index`(不编码向量),返回它打印的那几行。"""
        def scan():
            for full in sorted(self.vault.rglob("*.md")):
                yield self.doc_of(str(full.relative_to(self.vault)))

        out = io.StringIO()
        with mock.patch.object(cli, "scan_all", scan), \
             contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.cmd_index(SimpleNamespace(reembed=False,
                                                              no_vector=True)))
        return out.getvalue()

    # ── 跑那两条旁路 ──────────────────────────────────────────────────────────

    def run_bypass(self, kind: str, universe=None):
        """真跑那条旁路入口,吞掉它的输出。返回它打印的那几行。

        `universe` 给了就用那一份名单(摆「名单取好之后清理才动手」那个竞态)。
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            if kind == "youtube":
                with mock.patch.object(sys, "argv",
                                       ["backfill_youtube_summaries.py"]):
                    self.assertEqual(0, summaries.main())
            else:
                wiki_daily.step_linkify(universe or self.universe())
        return out.getvalue()

    def universe(self):
        """`wikilib.Universe` —— dr-gzh 已提取文档的那份快照(linkify 的名单)。"""
        conn = _REAL_CONNECT(self.db)
        try:
            return wikilib.Universe(conn)
        finally:
            conn.close()

    def rival(self, rel: str, *, lock: bool,
              text: str | None = None) -> subprocess.CompletedProcess:
        """真起一个独立进程去改这篇 md(`lock` 决定它认不认维护锁)。

        `text` 不给就换掉整篇正文;给了就写那一份(用来摆「索引输入没动、但
        索引照旧会改库」的那一档)。
        """
        env = dict(os.environ, PYTHONPATH=str(ROOT),
                   KB_DATA_DIR=str(self.data), KB_VAULT_ROOT=str(self.vault),
                   KB_MAINT_LOCK_WAIT="0")
        if text is None:
            kind = "youtube" if "youtube" in rel else "gzh"
            text = self.md_text(kind, f"{NEW_TOKEN} 这是另一个进程刚换上去的新正文。")
        return subprocess.run(
            [sys.executable, "-c", _RIVAL, str(self.vault / rel), text,
             "lock" if lock else "nolock"],
            env=env, capture_output=True, text=True, timeout=120)

    def tags(self, rel: str) -> list[str]:
        return [r["tag"] for r in self.conn.execute(
            "SELECT tag FROM doc_tags WHERE path=? ORDER BY tag", (rel,))]

    @contextlib.contextmanager
    def rival_slips_in_right_after_the_write(self, rel: str, *, lock: bool,
                                             text: str | None = None):
        """把那个独立进程安排在「文件刚写回、还没登记」那一瞬 —— 原缺陷的窗口。

        **注入点必须在写回之后、核对与登记之前。** 挂在 `register_indexed` 上是
        不够的:核对那一步(再读一次磁盘)在它**之前**就跑完了,子进程插在后面
        只会被下一轮索引正常收掉 —— 那种绿证明不了这个窗口关上了(自测时先踩
        的就是这一脚)。所以挂在 `Rewriting.write` 的返回处,登记与核对两步
        原样真跑。
        """
        seen = {}
        real = doc_rewrite.Rewriting.write

        def write_then_rival(doc, new_text):
            real(doc, new_text)
            if doc.path == rel and "child" not in seen:
                seen["child"] = self.rival(rel, lock=lock, text=text)

        with mock.patch.object(doc_rewrite.Rewriting, "write", write_then_rival):
            yield seen


    @contextlib.contextmanager
    def someone_else_holds_the_lock(self):
        """真起一个进程攥着维护锁,`yield` 期间它一直不放。

        必须是**另一个进程**:`maintenance_lock` 对同线程重入直接放行,在本进程里
        先取一次压根拦不住被测代码。
        """
        env = dict(os.environ, PYTHONPATH=str(ROOT), KB_DATA_DIR=str(self.data),
                   KB_VAULT_ROOT=str(self.vault))
        child = subprocess.Popen(
            [sys.executable, "-c", _HOLDER], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual("holding\n", child.stdout.readline(),
                             "占位进程没拿到锁")
            yield child
        finally:
            child.stdin.close()               # 它读到 EOF 就退出,锁随之释放
            child.wait(timeout=30)
