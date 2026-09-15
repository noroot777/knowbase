"""正文抓取的临时失败状态；换链接立即重试，用户删除另行处理。"""

import hashlib
import time


def _link_hash(link):
    return hashlib.sha256(link.encode()).hexdigest()


def state(repo, article_id):
    row = repo.conn.execute(
        "SELECT * FROM article_fetch_state WHERE article_id=?", (article_id,),
    ).fetchone()
    return dict(row) if row else None


def deferred(repo, article_id, link):
    row = state(repo, article_id)
    return bool(row and row["link_hash"] == _link_hash(link) and row["retry_at"] > time.time())


def failed(repo, article_id, link, *, unavailable=False):
    old = state(repo, article_id)
    fingerprint = _link_hash(link)
    failures = old["failures"] + 1 if old and old["link_hash"] == fingerprint else 1
    # 临时失败从五分钟起退避至一天；明确不可用的页面一周后复查，不永久封死。
    delay = 7 * 86400 if unavailable else min(86400, 300 * 2 ** min(failures - 1, 9))
    repo.conn.execute(
        "INSERT OR REPLACE INTO article_fetch_state VALUES (?, ?, ?, ?, ?)",
        (article_id, fingerprint, failures, time.time() + delay,
         "unavailable" if unavailable else "request_failed"),
    )
    repo.commit()


def succeeded(repo, article_id):
    repo.conn.execute("DELETE FROM article_fetch_state WHERE article_id=?", (article_id,))
