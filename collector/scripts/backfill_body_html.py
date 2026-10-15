#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回填 article_bodies.body_html。

历史抓取只存了纯文本正文（_fetch_article_detail_text 的旧实现丢弃了 html），
本脚本对缺 html 的文章重抓公开文章页（mp.weixin.qq.com/s/xxx，无需登录凭证），
只补 body_html，不动已有 body_text。

用法（项目根目录）:
    uv run python scripts/backfill_body_html.py [--limit N] [--dry-run]

反爬：每篇之间随机等待 2-4 秒；失败的文章记录后跳过，可重复运行续跑。
"""

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.repository import ArticleRepository  # noqa: E402
from src.utils.wechat_body import fetch_article_body  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少篇（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只列出待处理文章，不抓取")
    args = parser.parse_args()

    with ArticleRepository() as repo:
        rows = repo.conn.execute(
            """
            SELECT a.id, a.title, a.link
            FROM articles a
            JOIN article_bodies b ON b.article_id = a.id
            WHERE (b.body_html IS NULL OR b.body_html = '')
              AND a.link != ''
              AND a.is_user_deleted = 0
            ORDER BY a.create_time
            """
        ).fetchall()

    todo = [dict(r) for r in rows]
    if args.limit > 0:
        todo = todo[: args.limit]

    print(f"[backfill] 待回填 {len(todo)} 篇")
    if args.dry_run:
        for r in todo:
            print(f"  - {r['id']} {r['title'][:40]}")
        return 0

    ok = failed = 0
    with ArticleRepository() as repo:
        for i, row in enumerate(todo, 1):
            title_short = row["title"][:32]
            try:
                text, html = fetch_article_body(row["link"])
                if isinstance(text, str):
                    print(f"[{i}/{len(todo)}] SKIP({text}) {title_short}")
                    failed += 1
                elif html:
                    # 带上 link:排除判据在写语句自己的 WHERE 里(见
                    # `repository.set_body_html`),这一趟跑着的时候用户也可能在
                    # 面板上按下「不再抓取」。写不进去就如实记成跳过,不算成功。
                    if not repo.set_body_html(row["id"], html, link=row["link"]):
                        print(f"[{i}/{len(todo)}] SKIP(已永久排除) {title_short}")
                        failed += 1
                    else:
                        repo.commit()
                        ok += 1
                        print(f"[{i}/{len(todo)}] OK {title_short}")
                else:
                    print(f"[{i}/{len(todo)}] EMPTY-HTML {title_short}")
                    failed += 1
            except Exception as e:
                print(f"[{i}/{len(todo)}] FAIL {title_short}: {e}")
                failed += 1
            finally:
                # **一篇一个事务单元,下面那两件事之前一定要把它收掉。**
                # 受限写入即使影响 0 行也已经开着一个 SQLite 写事务(实测
                # `rowcount=0` 时 `in_transaction` 仍为 True、另一个连接当场
                # `database is locked`)。拒写和异常两条分支原来都没有收尾点,
                # 于是接下来那 2–4 秒退避、以及下一篇的网络请求,全程攥着整库写锁
                # —— 用户在这期间按「不再抓取」会被堵到超时(第 2 轮审查实测:
                # 第二次删除直接拿到 `database is locked`),连续失败时能一直占到
                # 脚本退出。成功那一支自己 commit 过,这里就什么都不剩。
                if repo.conn.in_transaction:
                    repo.conn.rollback()
            if i < len(todo):
                time.sleep(random.uniform(2.0, 4.0))

    print(f"[backfill] 完成：成功 {ok}，失败/跳过 {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
