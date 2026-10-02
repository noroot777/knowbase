# -*- coding: utf-8 -*-
"""笔记（article_notes）。

ArticleRepository 的一部分,以 mixin 的形式拆出来——它整个类曾经 1035 行,
超过本仓库 700 行的上限。切分按职责走,方法之间靠 self 互相调用,行为不变。
"""

from __future__ import annotations

from typing import Any

from src.utils.helpers import time_now


class NotesMixin:
    """笔记（article_notes）。"""

    def list_notes(
        self,
        *,
        limit: int = 100,
        account: str | None = None,
        article_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """笔记时间线：按笔记最近编辑时间倒序，返回文章信息 + 笔记内容。"""
        limit = max(1, min(limit, 200))
        where = [
            "ar.is_user_deleted=0",
            "length(trim(COALESCE(nt.content, ''))) > 0",
        ]
        params: list[Any] = []
        if account:
            where.append("ar.account_name=?")
            params.append(account)
        if article_ids is not None:
            if not article_ids:
                return []
            placeholders = ",".join("?" * len(article_ids))
            where.append(f"ar.id IN ({placeholders})")
            params.extend(article_ids)
        sql = f"""
            SELECT ar.*,
                   COALESCE(ann.is_read, 0) AS is_read,
                   COALESCE(ann.starred, 0) AS starred,
                   1 AS has_note,
                   nt.content AS note_content,
                   nt.updated_at AS note_updated_at
            FROM article_notes nt
            JOIN articles ar ON ar.id = nt.article_id
            LEFT JOIN article_annotations ann ON ann.article_id = ar.id
            WHERE {' AND '.join(where)}
            ORDER BY nt.updated_at DESC, ar.id DESC
            LIMIT ?
        """
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            item = self.row_to_feed_item(r)
            item["note_content"] = str(r["note_content"] or "")
            item["note_updated_at"] = str(r["note_updated_at"] or "")
            items.append(item)
        return items

    def get_article_note(self, article_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT content, updated_at
            FROM article_notes
            WHERE article_id=?
            """,
            (article_id,),
        ).fetchone()
        if not row:
            return {"content": "", "updated_at": ""}
        return {
            "content": str(row["content"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def set_article_note(self, article_id: str, content: str) -> dict[str, Any]:
        now = time_now()
        body = str(content or "")
        self.conn.execute(
            """
            INSERT INTO article_notes (article_id, content, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
              content=excluded.content,
              updated_at=excluded.updated_at
            """,
            (article_id, body, now, now),
        )
        return {"content": body, "updated_at": now}

    def article_has_note(self, article_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1
            FROM article_notes
            WHERE article_id=?
              AND length(trim(COALESCE(content, ''))) > 0
            LIMIT 1
            """,
            (article_id,),
        ).fetchone()
        return row is not None
