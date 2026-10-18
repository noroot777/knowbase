#!/usr/bin/env python3
"""Scan or repair stored X previews without overwriting concurrently replaced content.

    python scripts/x_post_backfill.py scan
    python scripts/x_post_backfill.py run --limit 10
    python scripts/x_post_backfill.py run --yes --channel browser --report /tmp/x-post.json

scan is local and read-only. run probes candidates; only --yes writes feeds.db.
Ellipsis endings are suspects, not proof. --all audits posts without that signal;
--url selects exact stored URLs. Translation, export and indexing use the normal pipeline.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import grab_store, registry as reg, x_browser, x_channels, x_post
from kb.feeds.x_cdp import CdpError


def candidates(conn, *, urls=(), limit=0, all_posts=False) -> list[dict]:
    rows = conn.execute("SELECT * FROM feed_items WHERE kind='social' AND content_kind='post'"
                        " AND trim(body)<>'' ORDER BY published DESC,key").fetchall()
    selected = []
    wanted = set(urls)
    for row in rows:
        if not x_post._STATUS.match(row['url']):
            continue
        if wanted:
            if row['url'] not in wanted:
                continue
        elif not all_posts and not row['body'].rstrip().endswith(('…', '...')):
            continue
        selected.append(dict(row))
        if limit and len(selected) >= limit:
            break
    return selected


def entry_of(row: dict) -> dict:
    body = row['body']
    head, separator, tail = body.partition('\n\n')
    prefix = ''
    if separator and all(line.startswith('> ') for line in head.splitlines()) and (
            row['title'] and tail.startswith(row['title'])):
        prefix = head + separator
    return {**{key: row[key] for key in
               ('url', 'title', 'body', 'author', 'published', 'content_kind')},
            x_post.HINT: prefix}


def repair(rows: list[dict], session, *, apply=False, deadline=None,
           should_stop=None, progress=None) -> dict:
    stats = dict(candidates=len(rows), expanded=0, updated=0, unchanged=0,
                 failed=0, stale=0, deferred=0, results=[])
    for index, row in enumerate(rows):
        if (should_stop and should_stop()) or (
                deadline is not None and time.monotonic() >= deadline):
            stats['deferred'] = len(rows) - index
            break
        result = dict(url=row['url'], before=len(row['body']))
        try:
            text = x_post.fetch(row['url'], session, deadline=deadline, should_stop=should_stop)
        except x_post.PostUnavailable as exc:
            stats['failed'] += 1
            result.update(state='failed', error=str(exc))
        else:
            entry = entry_of(row)
            if not x_post.expanded(text, row['body'].removeprefix(entry[x_post.HINT])):
                stats['unchanged'] += 1
                result['state'] = 'unchanged'
            else:
                complete = x_post.merge(entry, text)
                stats['expanded'] += 1
                result.update(state='expanded', after=len(complete['body']))
                if apply:
                    state = 'updated' if grab_store.replace(row, complete) else 'stale'
                    stats[state] += 1
                    result['state'] = state
        stats['results'].append(result)
        if progress:
            progress(result, stats)
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cmd', choices=['scan', 'run'])
    parser.add_argument('--yes', action='store_true', help='确认用版本化替换写回正文')
    parser.add_argument('--url', action='append', default=[], help='只检查指定的存量 URL,可重复')
    parser.add_argument('--all', action='store_true', help='也检查没有省略号信号的推文')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--budget', type=float, default=600.0, help='整轮浏览器读取秒数')
    parser.add_argument('--channel', choices=['browser', 'attach'], default='browser')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    if args.limit < 0 or args.budget <= 0:
        parser.error('limit 必须非负,budget 必须为正数')
    if any(not x_post._STATUS.match(url) for url in args.url):
        parser.error('--url 只接受 X 推文的完整地址')
    with reg.db() as conn:
        rows = candidates(conn, urls=args.url, limit=args.limit, all_posts=args.all)
    print(f'找到 {len(rows)} 条候选;省略号不等于正文确实截断,须逐条核对详情页。')
    if args.cmd == 'scan':
        for row in rows:
            print(f'{len(row["body"]):>6} 字符  {row["url"]}')
        return 0
    if not rows:
        return 0
    print('模式:真正回填' if args.yes else '模式:只读核对,不写库(加 --yes 才写回)')
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)

    def report(result, stats):
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.report:
            args.report.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')

    settings = reg.get_settings('social')
    deadline = time.monotonic() + args.budget
    try:
        with x_browser.browser_session(
                str(x_channels.BROWSER_PROFILE), settings.get('x_chrome_path', ''),
                attach=args.channel == 'attach') as page:
            page.open_page('about:blank')
            stats = repair(rows, page, apply=args.yes, deadline=deadline, progress=report)
    except (CdpError, OSError, EOFError) as exc:
        print(f'浏览器不可用,未继续回填:{exc}', file=sys.stderr)
        return 1
    if args.report:
        args.report.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'可补全 {stats["expanded"]} 条,实际写入 {stats["updated"]} 条,'
          f'并发换版跳过 {stats["stale"]} 条,失败 {stats["failed"]} 条,'
          f'预算/停止扣留 {stats["deferred"]} 条。')
    print('已写入的条目会在正常同步中重新翻译、导出、索引和提取;本脚本不直接改 vault。')
    return 1 if stats['failed'] or stats['deferred'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
