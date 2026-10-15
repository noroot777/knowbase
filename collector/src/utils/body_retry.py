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


def failed(repo, article_id, link, *, unavailable=False) -> bool:
    """记一笔退避重试。**这一篇已经不在库里、或身份被永久排除时返回 False。**

    判据待在写语句自己的 WHERE 里(`repository.set_fetch_state`),不在这里先查一次
    —— 抓一篇正文要几秒到几十秒,用户完全可能在这中间按下面板上那颗「不再抓取」。
    原来那句无判据的 `INSERT OR REPLACE` 会因为外键**抛**出来,异常一路穿到下载
    循环的通用失败处理,把一次用户主动的永久移除记成同步失败、把整轮判红。

    **不管写没写进去都 commit**:那句受限写入即使影响 0 行也已经开着一个写事务,
    不收尾的话后面的退避等待和下一次网络请求全都攥着整库写锁(见
    `docs/decisions/failure-drop.md` 里事务边界那一条)。
    """
    old = state(repo, article_id)
    fingerprint = _link_hash(link)
    failures = old["failures"] + 1 if old and old["link_hash"] == fingerprint else 1
    # 临时失败从五分钟起退避至一天；明确不可用的页面一周后复查，不永久封死。
    delay = 7 * 86400 if unavailable else min(86400, 300 * 2 ** min(failures - 1, 9))
    wrote = repo.set_fetch_state(
        article_id, link_hash=fingerprint, failures=failures,
        retry_at=time.time() + delay,
        reason="unavailable" if unavailable else "request_failed",
        link=link)
    repo.commit()
    return wrote


def succeeded(repo, article_id):
    repo.conn.execute("DELETE FROM article_fetch_state WHERE article_id=?", (article_id,))
