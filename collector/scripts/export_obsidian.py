#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把库里的公众号文章导出为 Obsidian Markdown（dr-gzh 树）。

目录结构（与 dr-youtube-dig 平行,按发布日期分组）:
    <out>/YYYY-MM-DD/<account>-<title>.md
    <out>/YYYY-MM-DD/assets/<md5>.png     # 当天共用

frontmatter 对齐 dr-youtube-dig 的 schema:
    title / source: dr-gzh / account / published / saved_time / url
    tags: source/dr-gzh, account/<slug>

去重规则（与最初 RPA 方案一致）:
    按 canonical URL 扫描 --dedup-root 指定的所有树（默认含实时 saver 的
    source/gzh/ 和本导出树），已存在的跳过。可重复运行,幂等。

用法（项目根目录）:
    uv run python scripts/export_obsidian.py --dry-run
    uv run python scripts/export_obsidian.py --limit 3 --out /tmp/dr-gzh-test
    uv run python scripts/export_obsidian.py           # 全量导出到 obs
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.repository import ArticleRepository  # noqa: E402
from src.export.obsidian import (  # noqa: E402
    account_tag_slug,
    build_frontmatter,
    canonicalize_url,
    download_images,
    html_to_markdown,
    read_frontmatter_url,
    replace_images_in_markdown,
    sanitize_filename,
    unique_md_path,
)

OBS_ROOT = Path.home() / "obs" / "new-start" / "source"
DEFAULT_OUT = OBS_ROOT / "dr-gzh"
DEFAULT_DEDUP_ROOTS = [OBS_ROOT / "gzh", DEFAULT_OUT]

def collect_existing_urls(roots: list[Path]) -> set[str]:
    """扫描已有 md 的 frontmatter url,构建去重集合。"""
    urls: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for md in root.rglob("*.md"):
            u = read_frontmatter_url(str(md))
            if u:
                urls.add(canonicalize_url(u))
    return urls


def load_articles(repo: ArticleRepository, account: str | None) -> list[dict]:
    where = "AND a.account_name = ?" if account else ""
    params = (account,) if account else ()
    rows = repo.conn.execute(
        f"""
        SELECT a.id, a.account_name, a.title, a.link, a.create_time,
               b.body_text, b.body_html
        FROM articles a
        LEFT JOIN article_bodies b ON b.article_id = a.id
        WHERE a.is_user_deleted = 0 AND a.is_wx_deleted = 0
          AND a.link != ''
          {where}
        ORDER BY a.create_time
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def body_text_to_markdown(body_text: str) -> str:
    """无 HTML 时的降级路径:纯文本段落直接拼 md。"""
    # body_text 可能是 JSON list 序列化(历史格式)或纯字符串
    import json
    try:
        parsed = json.loads(body_text)
        if isinstance(parsed, list):
            return "\n\n".join(str(p) for p in parsed if str(p).strip()) + "\n"
    except (json.JSONDecodeError, TypeError):
        pass
    return body_text.strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="导出根目录")
    parser.add_argument(
        "--dedup-root", type=Path, action="append", default=None,
        help="去重扫描的树(可多个);默认 source/gzh 和导出目录本身",
    )
    parser.add_argument("--account", default=None, help="只导出这个公众号")
    parser.add_argument("--limit", type=int, default=0, help="最多导出多少篇(0=全部)")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划,不写文件不下图")
    parser.add_argument("--no-images", action="store_true", help="不下载图片(保留远程 URL)")
    args = parser.parse_args()

    dedup_roots = args.dedup_root if args.dedup_root else list(DEFAULT_DEDUP_ROOTS)
    if args.out not in dedup_roots:
        dedup_roots.append(args.out)

    print(f"[export] 去重扫描: {', '.join(str(r) for r in dedup_roots)}")
    existing = collect_existing_urls(dedup_roots)
    print(f"[export] 已存在 {len(existing)} 个 URL")

    with ArticleRepository() as repo:
        articles = load_articles(repo, args.account)
    print(f"[export] 库内候选 {len(articles)} 篇")

    saved_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    exported = skipped = degraded = pending = 0

    for row in articles:
        if args.limit > 0 and exported >= args.limit:
            break
        canon = canonicalize_url(row["link"])
        if canon in existing:
            skipped += 1
            continue

        title = row["title"] or "untitled"
        account = row["account_name"]
        day = (row["create_time"] or "")[:10] or "unknown-date"
        day_dir = args.out / day
        base_name = sanitize_filename(f"{account}-{title}")

        if args.dry_run:
            print(f"  [plan] {day}/{base_name}.md")
            exported += 1
            continue

        # 1. 正文:优先 HTML 转 md,缺 HTML 用纯文本降级
        body_html = row["body_html"] or ""
        body_text = row["body_text"] or ""
        text_md = body_text_to_markdown(body_text) if body_text else ""
        images: list[str] = []
        if not body_html.strip() and not text_md.strip():
            # 发布得早不代表读取已多次失败；留在候选中，由抓取阶段按退避时间补正文。
            pending += 1
            print(f"  [export] 无正文,跳过 {account} · {title}")
            continue
        if body_html.strip():
            try:
                md_body, images = html_to_markdown(body_html)
            except Exception as e:
                print(f"  [export] HTML 解析失败,降级纯文本 {row['id']}: {e}")
                md_body = text_md
                degraded += 1
        else:
            md_body = text_md
            degraded += 1

        # 2. 图片:下到当天 assets/,替换为相对路径
        if images and not args.no_images:
            mapping = download_images(images, str(day_dir / "assets"))
            md_body = replace_images_in_markdown(md_body, mapping, "assets")

        # 3. frontmatter + 写文件
        fm = build_frontmatter(
            scalars=[
                ("title", title),
                ("source", "dr-gzh"),
                ("account", account),
                ("published", row["create_time"] or ""),
                ("saved_time", saved_time),
                ("url", canon),
            ],
            tags=["source/dr-gzh", f"account/{account_tag_slug(account)}"],
        )

        day_dir.mkdir(parents=True, exist_ok=True)
        path, skip = unique_md_path(str(day_dir), base_name, canon)
        if skip:
            skipped += 1
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(fm + md_body)
        existing.add(canon)
        exported += 1
        print(f"  [ok] {day}/{os.path.basename(path)}")

    print(
        f"[export] 完成: 导出 {exported}, 跳过(已存在) {skipped}, "
        f"降级纯文本 {degraded}, 待补正文 {pending}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
