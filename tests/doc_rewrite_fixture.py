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

        **注入点必须在写回之后、核对与登记之前。** 挂在 `register_written` 上是
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

    # ── 跑主回写那条路 ────────────────────────────────────────────────────────

    def run_main_writeback(self, rel: str, topic: str = "审查复现") -> bool:
        """真跑 `extract.writeback_extraction`(提取 tag 回写那条主路)。

        **tag 先落库再回写**,顺序和真实链路一致(`save_extraction` 把
        topic/entity `INSERT OR IGNORE` 进 `doc_tags`、提交,随后才回写 md)——
        登记那道判据问的是「这一份现在重新索引一次库里会变吗」,所以这一步不做
        的话,合法回写也会被正确地拒绝,而那不是这几条用例要摆的局。
        """
        from kb import extract
        tag = f"topic/{topic}"
        self.conn.execute("INSERT OR IGNORE INTO doc_tags (path, tag)"
                          " VALUES (?, ?)", (rel, tag))
        self.conn.commit()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            changed = extract.writeback_extraction(
                self.conn, rel, {"topics": [topic], "entities": []})
        self.said = out.getvalue()
        return changed

    @contextlib.contextmanager
    def rival_slips_in_after_the_main_writeback(self, rel: str, *, lock: bool,
                                                text: str | None = None):
        """把独立进程安排在**主回写**刚写完文件、还没登记那一瞬。

        和旁路那个注入点对称(那边挂 `Rewriting.write`),这边挂写文件的那一步
        `extract_writeback.writeback_tags` 的返回处。原缺陷的窗口就在这里:
        它之后那句登记原来会**再读一次磁盘**来当凭据。
        """
        from kb import extract_writeback
        seen = {}
        real = extract_writeback.writeback_tags

        def write_then_rival(doc_path, new_tags):
            written = real(doc_path, new_tags)
            if doc_path == rel and "child" not in seen:
                seen["child"] = self.rival(rel, lock=lock, text=text)
            return written

        with mock.patch.object(extract_writeback, "writeback_tags",
                               write_then_rival), \
             mock.patch("kb.extract.writeback_tags", write_then_rival):
            yield seen

    def md_with(self, kind: str, *, body: str | None = None,
                tags: list[str] | None = None, url: str | None = None,
                published: str | None = None, source: str | None = None,
                heading: str | None = None) -> str:
        """照 `md_text` 的模板摆一份,但按需换掉某一样索引看得见的东西。

        五个字段各摆一次:审查那条发现就是「外来编辑只动其中任意一样,模型输入
        那三样分毫不差」—— 判据只比那三样时全部放行。
        """
        source = source or self.SOURCES[kind]
        tag_lines = "".join(f"  - {t}\n" for t in
                            (tags if tags is not None
                             else [f"source/{self.SOURCES[kind]}"]))
        text = (f'---\ntitle: "并发登记审查"\nsource: {source}\n'
                f'account: "审查隔离号"\npublished: "{published or "2026-10-14"}"\n'
                f'url: "{url or f"https://example.invalid/{kind}-race"}"\n'
                f"tags:\n{tag_lines}---\n")
        if heading is not None:
            text += f"## {heading}\n"
        return text + (body or f"{OLD_TOKEN} 旧版导语与旧版正文。") + "\n"

    def col(self, rel: str, name: str) -> str:
        """`documents` 里某一列此刻的值(投影判据比的就是这几列)。"""
        return self.conn.execute(
            f"SELECT {name} FROM documents WHERE path=?", (rel,)
        ).fetchone()[name] or ""

    # ── 「提取只落库、不回写文件」那个合法状态 ────────────────────────────────

    def extract_without_writeback(self, rel: str, topic: str = "只落库的主题",
                                  entity: str = "ReviewTool") -> None:
        """真跑一次 `commit_extraction(writeback=False)` —— `kb extract` 不带
        `--writeback` 就是这条路(`kb/cli.py` 那个开关),是明确支持的操作。

        末态是**库里有 `topic/*`、`entity/*`,md 文件上没有**,`extracted_at`
        非空。这是合法状态,不是「磁盘领先于索引」—— 它恰好是反方向。
        """
        from kb import extract, store
        snap = store.doc_input_snapshot(self.conn, rel)
        # `input_seen` 是**模型输入那一组的指纹**,不是 content_hash
        # (口径同 `extract._candidates` 给 snap 塞的那个 "seen")
        seen = extract.input_fingerprint(snap["title"], snap["account"],
                                         snap["body"])
        extract.commit_extraction(
            self.conn, rel,
            {"summary": "审查复现", "topics": [topic],
             "entities": [{"name": entity, "kind": "tool"}]},
            seen, writeback=False)
        self.conn.commit()

    def promo_level(self, rel: str) -> str:
        return self.conn.execute(
            "SELECT promo_level FROM documents WHERE path=?",
            (rel,)).fetchone()["promo_level"] or ""

    def set_promo_level(self, rel: str, level: str) -> None:
        self.conn.execute("UPDATE documents SET promo_level=? WHERE path=?",
                          (level, rel))
        self.conn.commit()

    def flag_in_db(self, rel: str) -> bool:
        from kb.extract_promo import PROMO_TAG
        return PROMO_TAG in self.tags(rel)

    def flag_in_md(self, rel: str) -> bool:
        from kb.extract_promo import PROMO_TAG
        return PROMO_TAG in scanner.frontmatter_tags(
            scanner.parse_frontmatter(self.disk(rel))[0])

    @contextlib.contextmanager
    def rival_slips_in_before_the_main_writeback_reads(
            self, rel: str, *, lock: bool, text: str | None = None):
        """把独立进程安排在「捕获 before」之后、写入函数**读文件之前**。

        和 `rival_slips_in_after_the_main_writeback` 的区别要紧:那个注入点在
        写回**之后**,所以外来改动不在 `written` 里 —— ①「磁盘上还是本次写出
        去的那一份」当场就拦住了,② 压根轮不到说话。这个注入点在读之前,
        `writeback_tags` 读到的就是外来那一份、把那个改动**带进自己写出去的
        全文**,于是 ① 合法通过,这一档**只有 ② 拦得住**。

        两道判据对大多数外来编辑互为备份,所以要分辨「某一道自己还在不在」,
        注入时点必须选得让另一道闭嘴 —— 否则一道坏掉也全绿。
        """
        from kb import extract_writeback
        seen = {}
        real = extract_writeback.writeback_tags

        def rival_then_write(doc_path, new_tags):
            if doc_path == rel and "child" not in seen:
                seen["child"] = self.rival(rel, lock=lock, text=text)
            return real(doc_path, new_tags)

        with mock.patch.object(extract_writeback, "writeback_tags",
                               rival_then_write), \
             mock.patch("kb.extract.writeback_tags", rival_then_write):
            yield seen
