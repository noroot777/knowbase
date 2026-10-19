"""与已提交文档版本绑定的目录连续性凭据。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


DDL = """
CREATE TABLE IF NOT EXISTS document_scan_evidence (
  path TEXT PRIMARY KEY REFERENCES documents(path) ON DELETE CASCADE,
  source TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  root TEXT NOT NULL,
  directories TEXT NOT NULL
);
"""


def directory_paths(path: str, root: str) -> tuple[str, ...]:
    relative, base = Path(path), Path(root)
    if (relative.is_absolute() or base.is_absolute() or '..' in relative.parts
            or '..' in base.parts or relative == base):
        raise ValueError('目录凭据路径无效')
    parts = relative.parent.relative_to(base).parts
    return tuple(str(base.joinpath(*parts[:depth])) for depth in range(len(parts) + 1))


def capture(full: Path, root: Path, vault: Path) -> tuple[str, tuple[tuple[str, int, int], ...]]:
    vault = Path(os.path.abspath(vault))
    root = str(Path(os.path.abspath(root)).relative_to(vault))
    path = str(Path(os.path.abspath(full)).relative_to(vault))
    directories = []
    for directory in directory_paths(path, root):
        info = (vault / directory).stat()
        directories.append((directory, info.st_dev, info.st_ino))
    return root, tuple(directories)


@dataclass(frozen=True)
class DocumentEvidence:
    path: str
    source: str
    content_hash: str
    root: str
    directories: tuple[tuple[str, int, int], ...]

    def matches(self, document) -> bool:
        return (self.path == document['path'] and self.source == document['source']
                and bool(self.content_hash) and self.content_hash == document['content_hash'])

    def valid(self) -> bool:
        try:
            return (tuple(item[0] for item in self.directories)
                    == directory_paths(self.path, self.root)
                    and all(len(item) == 3 and type(item[1]) is int and type(item[2]) is int
                            for item in self.directories))
        except (ValueError, TypeError, IndexError):
            return False

    @classmethod
    def load(cls, row) -> DocumentEvidence | None:
        if row is None:
            return None
        try:
            evidence = cls(row['path'], row['source'], row['content_hash'], row['root'],
                           tuple(tuple(item) for item in json.loads(row['directories'])))
            return evidence if evidence.valid() else None
        except (ValueError, TypeError, KeyError):
            return None


def remember(conn: sqlite3.Connection, doc, *, replace: bool = False) -> None:
    evidence = getattr(doc, 'scan_evidence', None)
    if replace:
        conn.execute('DELETE FROM document_scan_evidence WHERE path=?', (doc.path,))
    document = {'path': doc.path, 'source': doc.source, 'content_hash': doc.content_hash}
    if (not isinstance(evidence, DocumentEvidence) or not evidence.valid()
            or not evidence.matches(document)):
        return
    written = conn.execute(
        'INSERT INTO document_scan_evidence(path, source, content_hash, root, directories) '
        'SELECT path, source, content_hash, ?, ? FROM documents '
        'WHERE path=? AND source=? AND content_hash=? '
        'ON CONFLICT(path) DO UPDATE SET source=excluded.source, '
        'content_hash=excluded.content_hash, root=excluded.root, directories=excluded.directories',
        (evidence.root, json.dumps(evidence.directories),
         evidence.path, evidence.source, evidence.content_hash))
    if written.rowcount:
        conn.executemany(
            'INSERT OR IGNORE INTO index_scan_directories(path, device, inode) VALUES(?,?,?)',
            evidence.directories)
        conn.execute(
            'INSERT OR IGNORE INTO index_scan_roots(root, device, inode) VALUES(?,?,?)',
            evidence.directories[0])


@contextmanager
def document_write(conn: sqlite3.Connection):
    """失败不留下半篇文档或半份凭据,成功也不替调用方提交。"""
    if not conn.in_transaction:
        conn.execute('BEGIN')
    conn.execute('SAVEPOINT kb_document_upsert')
    try:
        yield
    except BaseException:
        conn.execute('ROLLBACK TO kb_document_upsert')
        conn.execute('RELEASE kb_document_upsert')
        raise
    else:
        conn.execute('RELEASE kb_document_upsert')
