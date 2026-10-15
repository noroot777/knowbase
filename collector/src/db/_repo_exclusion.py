# -*- coding: utf-8 -*-
"""永久排除:判据、读侧两个问法、以及挂在文章上的附属状态写入。

**这一块的口径只有一份,就是下面那个 `_excluded_clause`。** 写语句把它取反拼进
自己的 WHERE(`repository` 里三处正文/元数据写入 + 这里的 `set_fetch_state`),读侧
`is_excluded` / `publishable` 直接用它。各写一遍的话两边只差一个键这件事在任何输出
上都看不出来。

从 `repository.py` 拆出来的:那个文件到了本仓库 700 行的上限。拆的是文件不是行为
—— mixin 里的方法仍然通过 self 相互调用,对调用方来说跟以前一模一样。

硬约束、事故形态和回归清单在 `docs/decisions/failure-drop.md` 第一节。
"""

from __future__ import annotations

from src.utils.helpers import time_now


def _excluded_clause(article_id: str, link: str) -> tuple[str, tuple[str, ...]]:
    """「这个身份在永久排除名单里吗」的 SQL 判据 + 参数。**整个仓储只有这一份。**

    写语句把它**取反**拼进自己的 WHERE(`NOT (...)` = 没被排除才写),不在 Python
    那边先 SELECT 一次 —— 后者在 legacy `isolation_level` 下不开事务、不持锁,查过
    之后到写入之间用户完全可能按下面板上那颗「不再抓取」,于是一条已经确认移除的
    文章被重新插回来(连正文一起),而两边都报成功。`cursor.rowcount == 0` 就是拒
    绝信号。读侧(`is_excluded` / `publishable`)直接用它,所以**两侧不会各写一份**
    —— 各写一遍的话两边只差一个键这件事在任何输出上都看不出来。

    键取 `article_identity.exclusion_keys`,和发现那一道查的是同一组。
    **算不出任何键时返回恒真** —— 证明不了这一篇没被排除,写入一侧据此拒写、读取
    一侧据此当成「已排除」,两边都落在安全的那一侧。
    """
    from src.utils.article_identity import exclusion_keys

    keys = exclusion_keys(article_id, link)
    if not keys:
        return "1", ()
    holes = ",".join("?" * len(keys))
    return (f"EXISTS (SELECT 1 FROM deleted_articles WHERE article_id IN ({holes}))",
            keys)


class ExclusionMixin:
    """永久排除那一组读写。`self.conn` 由 `ArticleRepository` 提供。"""

    def is_excluded(self, article_id: str, link: str = "") -> bool:
        """这个身份被永久排除了吗。**和写语句 WHERE 里那一份是同一个判据**
        (`_excluded_clause`)。

        给「读出来之后还要再核一次」的地方用(导出的候选筛选)。**它回答不了
        「现在能不能写」** —— 写入的判据必须待在它自己的 WHERE 里,见
        `_excluded_clause` 的说明。
        """
        clause, keys = _excluded_clause(article_id, link)
        return bool(self.conn.execute(f"SELECT {clause}", keys).fetchone()[0])

    def publishable(self, article_id: str, link: str = "") -> bool:
        """这一篇**此刻**还能发布到知识库吗:行还在、没被标删、身份不在排除名单里。

        导出拿它在一个 `BEGIN IMMEDIATE` 写事务里复核一次再写文件 —— 候选是几分钟
        前读出来的快照,而发布是不可撤销的(md 写进 Vault 之后下一次同步也不会自己
        消失)。判据和 `load_articles` 的候选条件、和写语句里那一份都是同一组
        (`_excluded_clause`),理由同上。
        """
        clause, keys = _excluded_clause(article_id, link)
        return self.conn.execute(
            f"""SELECT 1 FROM articles
                 WHERE id=? AND is_user_deleted=0 AND is_wx_deleted=0
                   AND NOT {clause}
                 LIMIT 1""",
            (article_id, *keys),
        ).fetchone() is not None

    def set_fetch_state(self, article_id: str, *, link_hash: str, failures: int,
                        retry_at: float, reason: str, link: str = "") -> bool:
        """记一笔正文重试状态。**文章不在了、或身份被永久排除,一律写不进来。**

        `article_fetch_state.article_id` 外键指向 `articles`,所以原来那句无判据的
        `INSERT OR REPLACE` 在文章刚被删掉时**抛** `FOREIGN KEY constraint failed`
        —— 那个异常一路穿到下载循环的通用失败处理,把一次「用户主动永久移除」记成
        同步失败、把整轮同步判红(第 2 轮审查实测:`errors` 里就是这句外键报错,
        `excluded_articles=0`、`download_failed=1`)。

        **把外键那条检查从「异常」改写成「WHERE」**:行不在就是 0 行,`rowcount`
        就是拒绝信号,调用方照常收尾。排除判据和三处正文写入同一份
        (`_excluded_clause`)—— 附属状态也挂在这篇文章上,不能只守住正文那一道。
        """
        clause, keys = _excluded_clause(article_id, link)
        guard = f"EXISTS (SELECT 1 FROM articles WHERE id=?) AND NOT {clause}"
        cur = self.conn.execute(
            f"""INSERT INTO article_fetch_state
                  (article_id, link_hash, failures, retry_at, reason)
                SELECT ?, ?, ?, ?, ? WHERE {guard}
                ON CONFLICT(article_id) DO UPDATE SET
                  link_hash=excluded.link_hash,
                  failures=excluded.failures,
                  retry_at=excluded.retry_at,
                  reason=excluded.reason
                WHERE {guard}""",
            (article_id, link_hash, failures, retry_at, reason,
             article_id, *keys, article_id, *keys),
        )
        return bool(cur.rowcount)

    def has_article(self, article_id: str) -> bool:
        """库里还有这一行吗。**只给统计用** —— 判据在写语句自己的 WHERE 里
        (`_excluded_clause`),这一条回答的是「刚才那次写入到底落没落」。
        """
        return self.conn.execute(
            "SELECT 1 FROM articles WHERE id=? LIMIT 1", (article_id,)
        ).fetchone() is not None

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

    def exclude_identities(self, *keys: str) -> None:
        """把这几个身份记进永久排除名单(幂等)。

        `add_deleted_blacklist` 是它在「articles 还有那一行」时的版本 —— 那一条
        能从库里读出规范链接自己算身份,这一条只能用调用方给的。行已经不在时
        (库被重建、回退到旧备份、被别的路径删过)只有这条路还能把承诺兑现。
        """
        now = time_now()
        for key in keys:
            if key:
                self.conn.execute(
                    "INSERT OR IGNORE INTO deleted_articles (article_id, deleted_at) "
                    "VALUES (?, ?)", (key, now))
