# -*- coding: utf-8 -*-
"""已读 / 收藏（article_annotations）。

ArticleRepository 的一部分,以 mixin 的形式拆出来——它整个类曾经 1035 行,
超过本仓库 700 行的上限。切分按职责走,方法之间靠 self 互相调用,行为不变。
"""

from __future__ import annotations

from typing import Any

from src.utils.helpers import time_now


class ReadingMixin:
    """已读 / 收藏（article_annotations）。"""

    def set_article_read(self, article_id: str, is_read: bool) -> None:
        now = time_now()
        row = self.conn.execute(
            "SELECT article_id FROM article_annotations WHERE article_id=?",
            (article_id,),
        ).fetchone()
        if row:
            self.conn.execute(
                """
                UPDATE article_annotations
                SET is_read=?, read_at=?, updated_at=?
                WHERE article_id=?
                """,
                (1 if is_read else 0, now if is_read else None, now, article_id),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO article_annotations (
                  article_id, is_read, read_at, starred, updated_at
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (article_id, 1 if is_read else 0, now if is_read else None, now),
            )

    def set_article_starred(self, article_id: str, starred: bool) -> None:
        now = time_now()
        row = self.conn.execute(
            "SELECT article_id FROM article_annotations WHERE article_id=?",
            (article_id,),
        ).fetchone()
        if row:
            self.conn.execute(
                """
                UPDATE article_annotations
                SET starred=?, starred_at=?, updated_at=?
                WHERE article_id=?
                """,
                (1 if starred else 0, now if starred else None, now, article_id),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO article_annotations (
                  article_id, is_read, starred, starred_at, updated_at
                ) VALUES (?, 0, ?, ?, ?)
                """,
                (article_id, 1 if starred else 0, now if starred else None, now),
            )

    def mark_articles_read(self, article_ids: list[str]) -> int:
        n = 0
        for aid in article_ids:
            if aid.strip():
                self.set_article_read(aid.strip(), True)
                n += 1
        return n

    def count_unread(self, **kwargs: Any) -> int:
        return self.count_articles(read_filter="unread", **kwargs)

    def count_starred(self, **kwargs: Any) -> int:
        return self.count_articles(starred_only=True, **kwargs)

    def get_reading_state(self) -> dict[str, Any]:
        read_rows = self.conn.execute(
            "SELECT article_id FROM article_annotations WHERE is_read=1"
        ).fetchall()
        starred_rows = self.conn.execute(
            "SELECT article_id FROM article_annotations WHERE starred=1"
        ).fetchall()
        return {
            "read_ids": [r["article_id"] for r in read_rows],
            "starred_ids": [r["article_id"] for r in starred_rows],
            "unread_count": self.count_unread(),
            "starred_count": len(starred_rows),
        }

    def import_reading_state(
        self, read_ids: list[str], starred_ids: list[str]
    ) -> dict[str, int]:
        read_n = self.mark_articles_read(read_ids)
        star_n = 0
        for aid in starred_ids:
            if aid.strip():
                self.set_article_starred(aid.strip(), True)
                star_n += 1
        return {"read_imported": read_n, "starred_imported": star_n}
