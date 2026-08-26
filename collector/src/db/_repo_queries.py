# -*- coding: utf-8 -*-
"""文章列表、计数与搜索。

ArticleRepository 的一部分,以 mixin 的形式拆出来——它整个类曾经 1035 行,
超过本仓库 700 行的上限。切分按职责走,方法之间靠 self 互相调用,行为不变。
"""

from __future__ import annotations

from typing import Any

from src.db import fts as fts_mod
from src.utils.helpers import time_now


class QueryMixin:
    """文章列表、计数与搜索。"""


    def _articles_where(
        self,
        *,
        account: str | None = None,
        tag: str | None = None,
        date_from: str = "",
        date_to: str = "",
        article_ids: list[str] | None = None,
        cursor: str | None = None,
        read_filter: str | None = None,
        starred_only: bool = False,
        import_only: bool = False,
        noted_only: bool = False,
    ) -> tuple[str, list[str], list[Any]]:
        where = ["ar.is_wx_deleted=0", "ar.is_user_deleted=0"]
        if import_only:
            where.append("ar.import_listed=1")
        params: list[Any] = []
        need_ann = bool(read_filter) or starred_only

        if account:
            where.append("ar.account_name=?")
            params.append(account)
        if date_from:
            where.append("ar.create_time>=?")
            params.append(date_from)
        if date_to:
            where.append("ar.create_time<=?")
            params.append(date_to + " 23:59" if len(date_to) <= 10 else date_to)
        if tag:
            where.append(
                """
                EXISTS (
                  SELECT 1 FROM article_llm_tags t
                  WHERE t.article_id=ar.id AND t.tag=?
                )
                """
            )
            params.append(tag)
        if article_ids is not None:
            if not article_ids:
                where.append("0")
            else:
                placeholders = ",".join("?" * len(article_ids))
                where.append(f"ar.id IN ({placeholders})")
                params.extend(article_ids)

        if read_filter == "unread":
            where.append("COALESCE(ann.is_read, 0) = 0")
            need_ann = True
        elif read_filter == "read":
            where.append("COALESCE(ann.is_read, 0) = 1")
            need_ann = True
        if starred_only:
            where.append("COALESCE(ann.starred, 0) = 1")
            need_ann = True
        if noted_only:
            where.append(
                """
                EXISTS (
                  SELECT 1 FROM article_notes nt
                  WHERE nt.article_id=ar.id
                    AND length(trim(COALESCE(nt.content, ''))) > 0
                )
                """
            )

        if cursor:
            try:
                left, aid = cursor.split("|", 1)
                if import_only:
                    order_no = int(left)
                    where.append(
                        "(COALESCE(ar.import_order, 0) < ? OR (COALESCE(ar.import_order, 0) = ? AND ar.id < ?))"
                    )
                    params.extend([order_no, order_no, aid])
                else:
                    ct = left
                    where.append(
                        "(ar.create_time < ? OR (ar.create_time = ? AND ar.id < ?))"
                    )
                    params.extend([ct, ct, aid])
            except ValueError:
                pass

        from_sql = (
            """
            FROM articles ar
            LEFT JOIN article_annotations ann ON ann.article_id = ar.id
            """
            if need_ann
            else "FROM articles ar"
        )
        return from_sql, where, params

    def count_articles(
        self,
        *,
        account: str | None = None,
        tag: str | None = None,
        date_from: str = "",
        date_to: str = "",
        article_ids: list[str] | None = None,
        read_filter: str | None = None,
        starred_only: bool = False,
        import_only: bool = False,
        noted_only: bool = False,
    ) -> int:
        if article_ids is not None and not article_ids:
            return 0
        from_sql, where, params = self._articles_where(
            account=account,
            tag=tag,
            date_from=date_from,
            date_to=date_to,
            article_ids=article_ids,
            read_filter=read_filter,
            starred_only=starred_only,
            import_only=import_only,
            noted_only=noted_only,
        )
        row = self.conn.execute(
            f"SELECT COUNT(*) AS c {from_sql} WHERE {' AND '.join(where)}",
            params,
        ).fetchone()
        return int(row["c"])

    def list_articles(
        self,
        *,
        limit: int = 30,
        cursor: str | None = None,
        account: str | None = None,
        tag: str | None = None,
        date_from: str = "",
        date_to: str = "",
        article_ids: list[str] | None = None,
        read_filter: str | None = None,
        starred_only: bool = False,
        import_only: bool = False,
        noted_only: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        limit = max(1, min(limit, 100))
        from_sql, where, params = self._articles_where(
            account=account,
            tag=tag,
            date_from=date_from,
            date_to=date_to,
            article_ids=article_ids,
            cursor=cursor,
            read_filter=read_filter,
            starred_only=starred_only,
            import_only=import_only,
            noted_only=noted_only,
        )
        if article_ids is not None and not article_ids:
            return [], None

        order_by = (
            "COALESCE(ar.import_order, 0) DESC, ar.id DESC"
            if import_only
            else "ar.create_time DESC, ar.id DESC"
        )
        sql = f"""
            SELECT ar.*,
                   COALESCE(ann.is_read, 0) AS is_read,
                   COALESCE(ann.starred, 0) AS starred,
                   EXISTS (
                     SELECT 1 FROM article_notes nt
                     WHERE nt.article_id=ar.id
                       AND length(trim(COALESCE(nt.content, ''))) > 0
                   ) AS has_note
            FROM articles ar
            LEFT JOIN article_annotations ann ON ann.article_id = ar.id
            WHERE {' AND '.join(where)}
            ORDER BY {order_by}
            LIMIT ?
        """
        params.append(limit + 1)
        rows = self.conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self.row_to_feed_item(r) for r in rows]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            if import_only:
                next_cursor = f"{int(last['import_order'] or 0)}|{last['id']}"
            else:
                next_cursor = f"{last['create_time']}|{last['id']}"
        return items, next_cursor

    def search_articles(
        self,
        query: str,
        *,
        limit: int = 30,
        cursor: str | None = None,
        account: str | None = None,
        read_filter: str | None = None,
        starred_only: bool = False,
        import_only: bool = False,
        noted_only: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        ids = fts_mod.search_article_ids(self.conn, query, limit=500)
        if not ids:
            return [], None
        return self.list_articles(
            limit=limit,
            cursor=cursor,
            account=account,
            article_ids=ids,
            read_filter=read_filter,
            starred_only=starred_only,
            import_only=import_only,
            noted_only=noted_only,
        )

    def find_article_by_link(self, link: str) -> dict[str, Any] | None:
        from src.utils.article_import import extract_mp_article_slug, link_lookup_variants

        for variant in link_lookup_variants(link):
            row = self.conn.execute(
                """
                SELECT id, account_name, title, link FROM articles
                WHERE link=? AND is_user_deleted=0
                """,
                (variant,),
            ).fetchone()
            if row:
                return dict(row)

        slug = extract_mp_article_slug(link)
        if slug:
            row = self.conn.execute(
                """
                SELECT id, account_name, title, link FROM articles
                WHERE is_user_deleted=0 AND link LIKE ?
                ORDER BY create_time DESC
                LIMIT 1
                """,
                (f"%/s/{slug}%",),
            ).fetchone()
            if row:
                return dict(row)
        return None

    def mark_article_as_imported(self, article_id: str) -> None:
        """链接导入命中已存在文章时，仅加入「导入」列表，不改变 origin。"""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(import_order), 0) AS max_import_order FROM articles"
        ).fetchone()
        next_order = int(row["max_import_order"] or 0) + 1 if row else 1
        self.conn.execute(
            """
            UPDATE articles SET import_listed=1, import_order=?, updated_at=?
            WHERE id=? AND is_user_deleted=0
            """,
            (next_order, time_now(), article_id),
        )

    def remove_from_import(self, article_id: str, account: str) -> str | None:
        """
        从「导入」移出。
        - origin=import：彻底删除
        - origin=crawl：仅 import_listed=0
        返回 'deleted' | 'unlisted'；不存在时返回 None。
        """
        row = self.conn.execute(
            """
            SELECT id, source FROM articles
            WHERE id=? AND account_name=? AND is_user_deleted=0
            """,
            (article_id, account),
        ).fetchone()
        if not row:
            return None
        origin = str(row["source"] or "crawl")
        if origin == "import":
            if not self.remove_article(article_id, account):
                return None
            return "deleted"
        self.conn.execute(
            """
            UPDATE articles SET import_listed=0, updated_at=?
            WHERE id=? AND account_name=?
            """,
            (time_now(), article_id, account),
        )
        return "unlisted"

    def get_article_detail(self, article_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT ar.*,
                   COALESCE(ann.is_read, 0) AS is_read,
                   COALESCE(ann.starred, 0) AS starred
            FROM articles ar
            LEFT JOIN article_annotations ann ON ann.article_id = ar.id
            WHERE ar.id=?
            """,
            (article_id,),
        ).fetchone()
        if not row:
            return None
        item = self.row_to_feed_item(row)
        item["body_text"] = self.get_body_text(article_id) or ""
        item["body_html"] = self.get_body_html(article_id) or ""
        return item

    def all_llm_tags(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT tag FROM article_llm_tags ORDER BY tag"
        ).fetchall()
        return [r["tag"] for r in rows]
