"""只恢复扫描清理留下的关联,路径、来源、整文指纹及引文版本须同时吻合。"""

from __future__ import annotations

import sqlite3


DDL = """
CREATE TABLE IF NOT EXISTS favorite_document_recovery (
  favorite_id INTEGER PRIMARY KEY REFERENCES favorites(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  source TEXT NOT NULL,
  url TEXT NOT NULL,
  content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_favorite_recovery_path ON favorite_document_recovery(path);
"""


def remember(conn: sqlite3.Connection, path: str, *, recoverable: bool) -> None:
    if not recoverable:
        conn.execute("DELETE FROM favorite_document_recovery WHERE path=?", (path,))
        return
    conn.execute(
        "INSERT OR REPLACE INTO favorite_document_recovery"
        " (favorite_id, path, source, url, content_hash)"
        " SELECT f.id, d.path, d.source, d.url, d.content_hash"
        " FROM favorites f JOIN documents d ON d.path=f.path WHERE d.path=?",
        (path,))


def restore(conn: sqlite3.Connection, doc) -> None:
    if not doc.content_hash:
        return
    conn.execute(
        "UPDATE favorites SET path=? WHERE path IS NULL AND origin='doc'"
        " AND id IN (SELECT favorite_id FROM favorite_document_recovery"
        " WHERE path=? AND source=? AND url=? AND content_hash=?)"
        " AND (quote='' OR (doc_version IS NOT NULL AND doc_version<>'' AND doc_version=?))"
        " AND NOT EXISTS (SELECT 1 FROM favorites linked"
        " WHERE linked.path=? AND linked.quote='' AND favorites.quote='')",
        (doc.path, doc.path, doc.source, doc.url, doc.content_hash,
         getattr(doc, 'body_version', ''), doc.path))
    conn.execute(
        "DELETE FROM favorite_document_recovery WHERE favorite_id IN"
        " (SELECT id FROM favorites WHERE path=?)", (doc.path,))
