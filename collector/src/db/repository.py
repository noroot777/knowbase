# -*- coding: utf-8 -*-
"""文章 / 公众号 SQLite 仓储。"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from src.db.body_text import normalize_body_text
from src.db.connection import DATA_DIR, ensure_database_ready, get_connection
from src.db._repo_notes import NotesMixin
from src.db._repo_queries import QueryMixin
from src.db._repo_reading import ReadingMixin
from src.utils.helpers import time_now


def _parse_create_time_unix(create_time: str) -> int | None:
    create_time = (create_time or "").strip()
    if not create_time:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(create_time, fmt).timestamp())
        except ValueError:
            continue
    return None


class AccountNameTaken(Exception):
    """要订阅的号和一个已有档案同名,而那个档案属于另一个 `__biz`。

    `accounts.name` 是主键,`articles.account_name` 是指向它的外键、导出的 vault
    目录也按它归档,所以"同名"这件事没法靠改名绕开——把那一行的 fakeid 换成新号
    等于把旧号的全部历史文章连同导出目录一起接到新号的身份上,而外键全都合法、
    谁也不报错。只能拒掉,由调用方说清楚是怎么回事。
    """

    def __init__(self, name: str, held: str, wanted: str) -> None:
        super().__init__(name)
        self.name, self.held, self.wanted = name, held, wanted


class ArticleRepository(QueryMixin, ReadingMixin, NotesMixin):
    """文章 / 公众号仓储。

    列表搜索、已读收藏、笔记三块按职责拆成了同目录下的 _repo_*.py——这个类
    原来 1035 行,超过本仓库 700 行的上限。拆的是文件不是行为:mixin 里的方法
    仍然通过 self 相互调用,对调用方来说跟以前一模一样。
    """

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._own_conn = conn is None
        ensure_database_ready()
        self.conn = conn or get_connection()

    def close(self) -> None:
        if self._own_conn:
            self.conn.close()

    def __enter__(self) -> ArticleRepository:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── 公众号 ─────────────────────────────────────────────────────────────

    def upsert_account(
        self,
        name: str,
        fakeid: str,
        latest_crawl_at: str = "",
        *,
        in_crawl_config: int = 1,
    ) -> None:
        now = time_now()
        self.conn.execute(
            """
            INSERT INTO accounts (name, fakeid, latest_crawl_at, in_crawl_config, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              fakeid=excluded.fakeid,
              latest_crawl_at=COALESCE(NULLIF(excluded.latest_crawl_at,''), accounts.latest_crawl_at),
              in_crawl_config=MAX(accounts.in_crawl_config, excluded.in_crawl_config),
              updated_at=excluded.updated_at
            """,
            (name, fakeid, latest_crawl_at or "", int(in_crawl_config), now, now),
        )

    def upsert_import_account(self, name: str) -> None:
        """链接导入：仅满足外键的占位账号，不加入批量爬取配置。"""
        now = time_now()
        fakeid = f"import:{name}"
        self.conn.execute(
            """
            INSERT INTO accounts (name, fakeid, latest_crawl_at, in_crawl_config, created_at, updated_at)
            VALUES (?, ?, '', 0, ?, ?)
            ON CONFLICT(name) DO NOTHING
            """,
            (name, fakeid, now, now),
        )

    def enable_crawl_account(self, name: str, fakeid: str) -> str:
        """用户主动添加:加入批量爬取配置。返回**实际入库的档案键**。

        先按 fakeid 认号,认出来了就只更新那一行、把用户给的新名字记进
        display_name,不再插新行。公众号改名后用户就是拿新名字来添加的
        (列表上还是旧名、微信里搜不到旧名,这条路几乎必走),而 name 是主键、
        `articles.account_name` 指着它、导出的 vault 目录也按它归档——照新名字
        再插一行,库里就有两条同 fakeid 的订阅:同一个号的新文章会被分别写进
        两套主键,vault 目录跟着裂开,外键还全都合法,面板上像订了两个号。

        反过来,**这个名字已经是另一个 `__biz` 的档案时抛 `AccountNameTaken`**,
        不覆盖它(见那个异常)。移除一个没有历史文章的号会把那一行真删掉
        (`remove_account_from_list`),所以"同名两个号、第一次点错了"自己有退路:
        移除之后名字就空出来了,不需要在这里替用户判断谁该让位。
        """
        now = time_now()
        fakeid = (fakeid or "").strip()
        if fakeid:
            # removed=0 的优先:一个号被移除过又重新订阅时,该复活旧行而不是另起一行
            row = self.conn.execute(
                "SELECT name FROM accounts WHERE fakeid=? ORDER BY removed, created_at "
                "LIMIT 1", (fakeid,)
            ).fetchone()
            if row is not None and row["name"] != name:
                existing = str(row["name"])
                self.conn.execute(
                    "UPDATE accounts SET in_crawl_config=1, removed=0, updated_at=? "
                    "WHERE name=?", (now, existing),
                )
                # 用户搜到的这个名字就是微信当前昵称,顺手把改名记下来
                self.set_account_display_name(existing, name)
                return existing
        cur = self.conn.execute(
            """
            INSERT INTO accounts (name, fakeid, latest_crawl_at, in_crawl_config, created_at, updated_at)
            VALUES (?, ?, '', 1, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              -- WHERE 放行时只剩三种可能:这个名字没人占、占着的就是同一个
              -- fakeid、或者占着的是个占位键。空 fakeid 一律不许把已有归属抹掉
              -- ——那种调用只是在说"确保这个名字在采集列表里",没在声明身份。
              fakeid=CASE WHEN excluded.fakeid <> '' THEN excluded.fakeid
                          ELSE accounts.fakeid END,
              in_crawl_config=1,
              removed=0,
              updated_at=excluded.updated_at
            -- **归属判定就在这里,不在 Python 那边先 SELECT 一次。** 先查后写是
            -- TOCTOU:两个请求(面板上那个 tgtBusy 只管单个页面,管不到另一个标签
            -- 页、更管不到独立的 CLI 进程)都读到"这个名字没人占",随后第二条
            -- UPSERT 照样把第一条刚写进去的 fakeid 盖掉,两边都报「已添加」
            -- (2026-10-01 第 3 轮审查 50/50 复现,真实 HTTP→CLI→仓储一条链也
            -- 复现了)。SQLite 把写入串行化,所以判据只有待在写语句自己的 WHERE
            -- 里,才和那次写入同生共死。放行不了就是 0 行,由调用方抛出来。
            WHERE excluded.fakeid = ''
               OR accounts.fakeid = ''
               OR accounts.fakeid = excluded.fakeid
               OR substr(accounts.fakeid, 1, 7) = 'import:'
            """,
            (name, fakeid, now, now),
        )
        if cur.rowcount == 0:
            raise AccountNameTaken(name, self.name_held_by(name), fakeid)
        return name

    def name_held_by(self, name: str) -> str:
        """这个档案名现在挂在哪个 `__biz` 上。**只给错误文案用,不是闸门**——
        闸门是 `enable_crawl_account` 那条 UPSERT 自己的 WHERE(见那里的注释)。

        `import:` 开头的不算占着:那是链接导入留下的占位键(`upsert_import_account`),
        本来就是等着被真正的归属填上的,拿它当"另一个号"会让这类存量账号永远订阅
        不了。空 fakeid 同理——还没核实过归属,不是另一个号。两条豁免跟那条 WHERE
        里的两句一一对应。
        """
        row = self.conn.execute(
            "SELECT fakeid FROM accounts WHERE name=?", (name,)
        ).fetchone()
        held = str(row["fakeid"] or "") if row is not None else ""
        return "" if held.startswith("import:") else held

    def account_in_crawl_config(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT in_crawl_config FROM accounts WHERE name=? AND removed=0",
            (name,),
        ).fetchone()
        return bool(row and int(row["in_crawl_config"] or 0) == 1)

    def list_accounts(self, *, crawl_config_only: bool = True) -> list[dict[str, Any]]:
        # removed=1 是"已移出订阅列表"的行,只为撑住 articles 的外键才留着,不该露面
        where = "WHERE a.removed = 0"
        if crawl_config_only:
            where += " AND a.in_crawl_config = 1"
        rows = self.conn.execute(
            f"""
            SELECT a.name, a.fakeid, a.display_name, a.latest_crawl_at,
                   a.in_crawl_config, COUNT(ar.id) AS article_count
            FROM accounts a
            LEFT JOIN articles ar ON ar.account_name = a.name
              AND ar.is_wx_deleted = 0 AND ar.is_user_deleted = 0
            {where}
            GROUP BY a.name, a.fakeid, a.display_name, a.latest_crawl_at,
                     a.in_crawl_config
            ORDER BY a.name
            """
        ).fetchall()
        return [
            {
                "name": r["name"],
                "fakeid": r["fakeid"],
                "display_name": r["display_name"] or "",
                "latest_update_time": r["latest_crawl_at"] or "",
                "article_count": r["article_count"],
                "in_crawl_config": int(r["in_crawl_config"] or 0),
            }
            for r in rows
        ]

    def get_name2fakeid(self, *, crawl_config_only: bool = True) -> dict[str, str]:
        if crawl_config_only:
            rows = self.conn.execute(
                "SELECT name, fakeid FROM accounts "
                "WHERE removed = 0 AND in_crawl_config = 1"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT name, fakeid FROM accounts WHERE removed = 0"
            ).fetchall()
        return {r["name"]: r["fakeid"] for r in rows}

    def set_account_latest_crawl(self, name: str, t: str) -> None:
        self.conn.execute(
            "UPDATE accounts SET latest_crawl_at=?, updated_at=? WHERE name=?",
            (t, time_now(), name),
        )

    def set_account_display_name(self, name: str, display: str) -> bool:
        """记下公众号在微信上的当前昵称。改了返回 True,没变或读不到返回 False。

        不动 name:它是主键,articles.account_name 是指向它的外键(没有
        ON UPDATE CASCADE),已导出的 vault markdown 也按它归档。改名只改这一列。
        """
        display = (display or "").strip()
        if not display:
            return False
        row = self.conn.execute(
            "SELECT display_name FROM accounts WHERE name=?", (name,)
        ).fetchone()
        # 没这个号(链接导入的占位号就不在订阅列表里)或者昵称没变:不写,
        # 免得每篇文章都白白翻一次 updated_at
        if row is None or (row["display_name"] or "") == display:
            return False
        self.conn.execute(
            "UPDATE accounts SET display_name=?, updated_at=? WHERE name=?",
            (display, time_now(), name),
        )
        return True

    def remove_account_from_list(self, name: str) -> None:
        """仅从订阅列表移除，保留已爬文章。

        不能直接 DELETE 掉 accounts 那一行:articles.account_name 是指向它的外键,
        删父行、留文章就是一堆孤儿——foreign_key_check 会报出来,之后任何碰这些行
        的写入都可能直接失败。原来靠 PRAGMA foreign_keys=OFF 硬删,而这个 PRAGMA
        在事务里是被忽略的:同一连接上只要已有未提交的写,它照样抛 FOREIGN KEY
        constraint failed。所以有文章的号只打标记(列表查询按 removed=0 过滤,
        用户看不到它),一篇都没有的才真删,免得表里堆一串隐身的空行。
        """
        has_articles = self.conn.execute(
            "SELECT 1 FROM articles WHERE account_name=? LIMIT 1", (name,)
        ).fetchone()
        if has_articles:
            self.conn.execute(
                "UPDATE accounts SET removed=1, in_crawl_config=0, updated_at=? "
                "WHERE name=?",
                (time_now(), name),
            )
        else:
            self.conn.execute("DELETE FROM accounts WHERE name=?", (name,))

    # ── 文章写入 ─────────────────────────────────────────────────────────────

    def _tags_for_row(self, article_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT tag FROM article_llm_tags WHERE article_id=? ORDER BY tag",
            (article_id,),
        ).fetchall()
        return [r["tag"] for r in rows]

    def row_to_blog_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        tags = self._tags_for_row(row["id"])
        d: dict[str, Any] = {
            "id": row["id"],
            "title": row["title"],
            "digest": row["digest"],
            "link": row["link"],
            "cover": row["cover_url"],
            "create_time": row["create_time"],
            "is_deleted": bool(row["is_wx_deleted"]),
            "item_show_type": row["item_show_type"],
            "word_count": row["word_count"],
        }
        if row["llm_summary"]:
            d["summary"] = row["llm_summary"]
        if tags:
            d["tags"] = tags
        return d

    def row_to_feed_item(self, row: sqlite3.Row) -> dict[str, Any]:
        item = self.row_to_blog_dict(row)
        item["account"] = row["account_name"]
        keys = row.keys()
        if "is_read" in keys:
            item["is_read"] = bool(row["is_read"])
        if "starred" in keys:
            item["starred"] = bool(row["starred"])
        if "source" in keys:
            item["source"] = str(row["source"] or "crawl")
        if "import_listed" in keys:
            item["import_listed"] = bool(row["import_listed"])
        if "has_note" in keys:
            item["has_note"] = bool(row["has_note"])
        return item

    def upsert_article_from_blog(
        self,
        account_name: str,
        blog: dict[str, Any],
        *,
        source: str = "crawl",
    ) -> None:
        aid = str(blog.get("id") or "").strip()
        if not aid:
            return
        now = time_now()
        create_time = str(blog.get("create_time") or "")
        cover_url = str(blog.get("cover") or "")
        article_source = "import" if source == "import" else "crawl"
        import_listed = 1 if source == "import" else 0
        self.conn.execute(
            """
            INSERT INTO articles (
              id, account_name, title, digest, link, cover_url,
              create_time, create_time_unix, is_wx_deleted, is_user_deleted,
              item_show_type, llm_summary, word_count, ingested_at, updated_at,
              source, import_listed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              account_name=excluded.account_name,
              title=excluded.title,
              digest=excluded.digest,
              link=excluded.link,
              cover_url=excluded.cover_url,
              create_time=excluded.create_time,
              create_time_unix=excluded.create_time_unix,
              is_wx_deleted=excluded.is_wx_deleted,
              item_show_type=excluded.item_show_type,
              llm_summary=COALESCE(excluded.llm_summary, articles.llm_summary),
              word_count=COALESCE(excluded.word_count, articles.word_count),
              updated_at=excluded.updated_at,
              source=articles.source,
              import_listed=CASE
                WHEN excluded.import_listed = 1 THEN 1
                ELSE articles.import_listed
              END
            """,
            (
                aid,
                account_name,
                str(blog.get("title") or ""),
                str(blog.get("digest") or ""),
                str(blog.get("link") or ""),
                cover_url,
                create_time,
                _parse_create_time_unix(create_time),
                1 if blog.get("is_deleted") else 0,
                int(blog.get("item_show_type") or 0),
                str(blog.get("summary") or "") or None,
                blog.get("word_count"),
                now,
                now,
                article_source,
                import_listed,
            ),
        )
        tags = blog.get("tags")
        if isinstance(tags, list) and tags:
            self.set_llm_tags(aid, [str(t) for t in tags if str(t).strip()])

    def set_llm_tags(self, article_id: str, tags: list[str]) -> None:
        self.conn.execute(
            "DELETE FROM article_llm_tags WHERE article_id=?", (article_id,)
        )
        for tag in tags:
            t = tag.strip()
            if t:
                self.conn.execute(
                    "INSERT OR IGNORE INTO article_llm_tags (article_id, tag) VALUES (?, ?)",
                    (article_id, t),
                )

    def update_llm_fields(
        self, article_id: str, summary: str, tags: list[str]
    ) -> None:
        self.conn.execute(
            "UPDATE articles SET llm_summary=?, updated_at=? WHERE id=?",
            (summary or None, time_now(), article_id),
        )
        if tags:
            self.set_llm_tags(article_id, tags)

    def set_body_text(self, article_id: str, raw: Any) -> None:
        text = normalize_body_text(raw)
        now = time_now()
        self.conn.execute(
            """
            INSERT INTO article_bodies (article_id, body_text, source_kind, fetched_at, updated_at)
            VALUES (?, ?, 'crawl', ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
              body_text=excluded.body_text,
              updated_at=excluded.updated_at
            """,
            (article_id, text, now, now),
        )

    def get_body_text(self, article_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT body_text FROM article_bodies WHERE article_id=?",
            (article_id,),
        ).fetchone()
        return row["body_text"] if row else None

    def get_body_html(self, article_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT body_html FROM article_bodies WHERE article_id=?",
            (article_id,),
        ).fetchone()
        if not row:
            return None
        html = row["body_html"]
        return html if html else None

    def has_body_html(self, article_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM article_bodies
            WHERE article_id=? AND body_html IS NOT NULL AND length(trim(body_html)) > 0
            LIMIT 1
            """,
            (article_id,),
        ).fetchone()
        return row is not None

    def set_body_html(self, article_id: str, html: str) -> None:
        now = time_now()
        self.conn.execute(
            """
            INSERT INTO article_bodies (article_id, body_text, body_html, source_kind, fetched_at, updated_at)
            VALUES (?, '', ?, 'crawl', ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
              body_html=excluded.body_html,
              updated_at=excluded.updated_at
            """,
            (article_id, html, now, now),
        )

    def has_body(self, article_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM article_bodies WHERE article_id=? LIMIT 1",
            (article_id,),
        ).fetchone()
        return row is not None

    def get_blogs_for_account(self, account_name: str) -> list[dict[str, Any]]:
        """供增量抓取去重用的现有文章列表。"""
        rows = self.conn.execute(
            """
            SELECT * FROM articles
            WHERE account_name=? AND is_user_deleted=0
            ORDER BY create_time
            """,
            (account_name,),
        ).fetchall()
        return [self.row_to_blog_dict(r) for r in rows]

    def note_fetch_round(self, article_id: str, round_key: str) -> None:
        """记一笔「这一篇是知识库哪一次业务抓取头一回发现的」。

        **INSERT OR IGNORE = 首次发现那一轮说了算。** 同一篇后面会被反复 upsert
        (补正文、改标题、重新发现),改写这一行就等于把「哪一次抓到的」偷换成
        「最后一次看见的那一轮」—— 而药丸那句分解正是靠它分开「这一次新抓到的」
        和「之前抓到、这一次才补导进来的」。

        `round_key` 空串就什么都不记:那是「说不清」(不在管道里跑的 CLI 采集、
        手工导入链接),留一行空值反而会把"不知道"伪装成一个确定的身份。
        """
        aid, key = str(article_id or "").strip(), str(round_key or "").strip()
        if not aid or not key:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO article_fetch_rounds (article_id, round_key)"
            " VALUES (?, ?)",
            (aid, key),
        )

    def is_deleted_blacklist(self, article_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM deleted_articles WHERE article_id=?",
            (article_id,),
        ).fetchone()
        if row:
            return True
        row = self.conn.execute(
            "SELECT is_user_deleted FROM articles WHERE id=?", (article_id,)
        ).fetchone()
        return bool(row and row["is_user_deleted"])

    def add_deleted_blacklist(self, article_id: str) -> None:
        from src.utils.article_identity import remember_deleted_identity

        remember_deleted_identity(self, article_id)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO deleted_articles (article_id, deleted_at)
            VALUES (?, ?)
            """,
            (article_id, time_now()),
        )
        self.conn.execute(
            "UPDATE articles SET is_user_deleted=1, updated_at=? WHERE id=?",
            (time_now(), article_id),
        )

    def remove_article(self, article_id: str, account: str) -> bool:
        row = self.conn.execute(
            "SELECT id FROM articles WHERE id=? AND account_name=?",
            (article_id, account),
        ).fetchone()
        if not row:
            return False
        self.add_deleted_blacklist(article_id)
        self.conn.execute("DELETE FROM articles WHERE id=?", (article_id,))
        self.conn.execute(
            "DELETE FROM article_bodies WHERE article_id=?", (article_id,)
        )
        self.conn.execute(
            "DELETE FROM article_llm_tags WHERE article_id=?", (article_id,)
        )
        return True

    def set_word_count(self, article_id: str, count: int) -> None:
        self.conn.execute(
            "UPDATE articles SET word_count=?, updated_at=? WHERE id=?",
            (count, time_now(), article_id),
        )

    # ── 缓存清理 ─────────────────────────────────────────────────────────────

    def ids_older_than(self, cutoff: str) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT ar.id
            FROM articles ar
            LEFT JOIN article_annotations ann ON ann.article_id = ar.id
            WHERE ar.create_time < ?
              AND ar.is_user_deleted=0
              AND ar.import_listed=0
              AND COALESCE(ann.starred, 0)=0
              AND NOT EXISTS (
                SELECT 1 FROM article_notes nt
                WHERE nt.article_id=ar.id
                  AND length(trim(COALESCE(nt.content, ''))) > 0
              )
            """,
            (cutoff,),
        ).fetchall()
        return {r["id"] for r in rows}

    def delete_articles_by_ids(self, ids: set[str]) -> int:
        if not ids:
            return 0
        removed = 0
        for aid in ids:
            self.conn.execute("DELETE FROM article_bodies WHERE article_id=?", (aid,))
            self.conn.execute(
                "DELETE FROM article_llm_tags WHERE article_id=?", (aid,)
            )
            cur = self.conn.execute("DELETE FROM articles WHERE id=?", (aid,))
            removed += cur.rowcount
        return removed

    def commit(self) -> None:
        self.conn.commit()
