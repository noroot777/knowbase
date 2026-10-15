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
import json
import os
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

OBS_ROOT = Path(os.environ.get("KB_VAULT_ROOT") or Path.home() / "obs" / "new-start").expanduser() / "source"
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
    """要导出的文章。

    **候选筛选不是不变量,发布那一步才是**(`publish_one`)。这里读出来的是快照:
    一轮导出要下几百张图、写几百个文件,中间用户完全可能在面板上按下「不再抓取」。
    不变量在 `publish_one` 的写事务里。

    筛选本身按**完整身份**走(`repo.is_excluded` → `exclusion_keys`):SQL 里那句
    只能比 `a.id`,而名单里另外两个键(链接身份、按链接算的稳定 ID)是 Python 算
    出来的。只按 ID 筛会漏掉「同一原文以另一个档案 ID 存在库里」这种存量情形
    (第 2 轮审查实测:删除之后才启动的导出仍写出 1 篇 md)。SQL 那句留着是为了
    让大头在库里就被滤掉,Python 这一道补齐剩下两个键。

    `fetch_round` 是知识库哪一次业务抓取头一回发现这一篇(见 migrations v19)。
    `LEFT JOIN` + `COALESCE`:这张表建出来之前就在库里的文章没有这一行,它们的身份
    是空串「说不清」—— 不是「不是本次」,所以不能在这里替它们编一个。
    """
    where = "AND a.account_name = ?" if account else ""
    params = (account,) if account else ()
    rows = repo.conn.execute(
        f"""
        SELECT a.id, a.account_name, a.title, a.link, a.create_time,
               b.body_text, b.body_html,
               COALESCE(r.round_key, '') AS fetch_round
        FROM articles a
        LEFT JOIN article_bodies b ON b.article_id = a.id
        LEFT JOIN article_fetch_rounds r ON r.article_id = a.id
        WHERE a.is_user_deleted = 0 AND a.is_wx_deleted = 0
          AND a.link != ''
          AND NOT EXISTS (SELECT 1 FROM deleted_articles d WHERE d.article_id = a.id)
          {where}
        ORDER BY a.create_time
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows if not repo.is_excluded(r["id"], r["link"])]


def publish_one(repo: ArticleRepository, article_id: str, link: str, day_dir: Path,
                base_name: str, canon: str, content: str) -> tuple[str, str]:
    """把一篇写进 Vault。返回 `(结果, 路径)`:`("ok", 写出来的 md)`、
    `("excluded", "")`(已永久移除)、`("skip", "")`(同一篇已经在树里)。

    **这是永久排除的最后一道,也是唯一一道真不变量。** 发布不可撤销:md 写进 Vault
    之后,用户下一次同步不会让它自己消失,而面板上那颗「不再抓取」承诺的是「以后不
    再采集」。候选和正文都是几分钟前的快照,所以「此刻还能不能发布」必须和写文件在
    同一个临界区里问 —— 第 2 轮审查实测:导出读到正文之后、写文件之前完成移除,
    删除报 `ok=true, removed=true`、库里 `remaining_articles=0`,导出照旧写出 1 篇
    带正文的 md。只把检查挪到循环末尾是同一类 TOCTOU,不算修。

    串行化靠 SQLite 自己的单写者:`BEGIN IMMEDIATE` 当场拿写锁,而删除那一侧
    (`remove_article` + `commit`、`exclude_identities` + `commit`)也跑在写事务里。
    于是两者天然互斥:要么我们先拿到锁、在它之前写完这一个文件;要么它先删完,
    我们复核当场得到 False。不新开一把文件锁 —— 删除本来就在写事务里,多一个原语
    就多一处锁序。

    **图片下载刻意留在临界区外**(调用方的第 2 步):攥着整库写锁去等网络,会把面板
    上那颗按钮堵到超时(见 `docs/decisions/failure-drop.md` 里补 HTML 那一节)。
    """
    if repo.conn.in_transaction:
        repo.conn.commit()
    repo.conn.execute("BEGIN IMMEDIATE")
    try:
        if not repo.publishable(article_id, link):
            repo.conn.rollback()
            return "excluded", ""
        day_dir.mkdir(parents=True, exist_ok=True)
        path, skip = unique_md_path(str(day_dir), base_name, canon)
        if skip:
            repo.conn.rollback()
            return "skip", ""
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        repo.conn.commit()
        return "ok", path
    except BaseException:
        # 写锁一定要放掉:下一篇开头是图片下载,攥着锁等网络就是 F3 那条占锁路径。
        if repo.conn.in_transaction:
            repo.conn.rollback()
        raise


def body_text_to_markdown(body_text: str) -> str:
    """无 HTML 时的降级路径:纯文本段落直接拼 md。"""
    # body_text 可能是 JSON list 序列化(历史格式)或纯字符串
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
    parser.add_argument(
        "--report", type=Path, default=None,
        help="把这一轮的结构化结果写成 JSON(给上游程序读,人看的照旧打在 stdout)",
    )
    args = parser.parse_args()

    dedup_roots = args.dedup_root if args.dedup_root else list(DEFAULT_DEDUP_ROOTS)
    if args.out not in dedup_roots:
        dedup_roots.append(args.out)

    # 输出目录要打出来:上层(cli.py 的 cmd_export)靠它把下面那些 [ok] 行里的
    # 「天/文件名」还原成 vault 相对路径,当成这一轮的成果回执交给知识库记账。
    # 一篇都没导的时候也得有这一行,所以放在循环之前。
    print(f"[export] 输出目录: {args.out}")
    print(f"[export] 去重扫描: {', '.join(str(r) for r in dedup_roots)}")
    existing = collect_existing_urls(dedup_roots)
    print(f"[export] 已存在 {len(existing)} 个 URL")

    saved_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    exported = skipped = degraded = pending = excluded = 0
    # 没正文那几篇的**身份**。stdout 那行「无正文,跳过 账号 · 标题」只够给人看:
    # 标题会重名、`·` 本身也可能出现在号名或标题里,拿它反查是哪一篇一定出错。
    # 面板要能点开原文核对、能把这一条从采集范围里删掉,靠的就是这里的 id 和 link。
    no_body: list[dict] = []

    # **连接要一直开到循环结束。** 发布那一步要在一个写事务里复核「此刻还能不能
    # 发布」(`publish_one`),连接先关掉就只剩读候选时那一次快照检查 —— 那正是
    # 第 2 轮审查实测到的漏洞。读候选、下图片这些都不持事务,开着不占锁。
    with ArticleRepository() as repo:
        articles = load_articles(repo, args.account)
        print(f"[export] 库内候选 {len(articles)} 篇")
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
                no_body.append({"id": row["id"], "account": account,
                                "title": title, "link": row["link"]})
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

            # 4. 发布:复核 + 写文件在同一个写事务里(`publish_one` 的 docstring)
            outcome, path = publish_one(repo, row["id"], row["link"], day_dir,
                                        base_name, canon, fm + md_body)
            if outcome == "excluded":
                # 这一篇在本轮导出期间被永久移除了。如实计一笔:它既不是「导出」
                # 也不是「已存在跳过」,混进后者会让「承诺兑现了没有」看不出来。
                excluded += 1
                print(f"  [export] 已永久移除,不发布 {account} · {title}")
                continue
            if outcome == "skip":
                skipped += 1
                continue
            existing.add(canon)
            exported += 1
            name = f"{day}/{os.path.basename(path)}"
            print(f"  [ok] {name}")
            # 给机器的那一行:上层(cli.py 的 cmd_export)收它当这一轮的成果回执。
            # `id` 是稳定内容身份(账本靠它去重,路径随标题变、当不了身份),
            # `round` 是发现这一篇的那一次抓取。**打在发布成功之后**:写进 Vault 了
            # 才算,中途挂掉就停在这里,上层据此报「不可确认」而不是一个少记的确定值。
            print("  [item] " + json.dumps(
                {"id": row["id"], "round": row["fetch_round"], "name": name},
                ensure_ascii=False))

    print(
        f"[export] 完成: 导出 {exported}, 跳过(已存在) {skipped}, "
        f"降级纯文本 {degraded}, 待补正文 {pending}, 已永久移除 {excluded}"
    )
    # 报告是旁路:写不出来(磁盘满、目录没了)只让上游退回解析 stdout 那几行,
    # 这一轮导出的结论照旧成立,所以不往上抛。
    if args.report:
        try:
            args.report.write_text(
                json.dumps({"no_body": no_body}, ensure_ascii=False),
                encoding="utf-8")
        except OSError as e:
            print(f"[export] 结构化报告没写成: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
