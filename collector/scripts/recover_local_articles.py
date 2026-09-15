#!/usr/bin/env python3
"""复位旧本地通道因缺 chksm 且无正文而自动退休的文章；默认只预览。"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.utils.article_identity import article_identity, stable_article_id


def candidates(conn):
    tombstones = {r[0] for r in conn.execute("SELECT article_id FROM deleted_articles")}
    rows = conn.execute(
        "SELECT a.id, a.link, b.body_text, b.body_html FROM articles a "
        "LEFT JOIN article_bodies b ON b.article_id=a.id "
        "WHERE a.is_wx_deleted=1 AND a.is_user_deleted=0 AND a.source='crawl'",
    ).fetchall()
    selected = []
    for row in rows:
        aid, link, text, html = row
        if aid != stable_article_id(link) or {aid, article_identity(link)} & tombstones:
            continue
        if parse_qs(urlparse(link).query).get("chksm"):
            continue
        try:
            parsed = json.loads(text or '""')
        except ValueError:
            parsed = text
        paragraphs = parsed if isinstance(parsed, list) else [parsed]
        if (html or "").strip() or any(str(p or "").strip() for p in paragraphs):
            continue
        selected.append(aid)
    return selected


def recover(db_path, *, backup=None):
    # mode=rw 防止路径拼错时创建空库；预览使用只读连接。
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode={'rw' if backup else 'ro'}", uri=True)
    try:
        ids = candidates(conn)
        if backup and ids:
            backup = Path(backup)
            backup.parent.mkdir(parents=True, exist_ok=True)
            # 不覆盖已有备份。
            with backup.open("xb"):
                pass
            backup.chmod(0o600)
            with sqlite3.connect(backup) as dest:
                conn.backup(dest)
            conn.execute("BEGIN IMMEDIATE")
            ids = candidates(conn)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.executemany("UPDATE articles SET is_wx_deleted=0, updated_at=? WHERE id=?",
                             [(now, aid) for aid in ids])
            conn.commit()
        return {"candidates": len(ids), "restored": len(ids) if backup else 0}
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error("--apply 必须同时指定 --backup")
    print(json.dumps(recover(args.db, backup=args.backup if args.apply else None), ensure_ascii=False))


if __name__ == "__main__":
    main()
