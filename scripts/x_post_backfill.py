#!/usr/bin/env python3
"""Scan or repair stored X previews without overwriting concurrently replaced content.

    python scripts/x_post_backfill.py scan
    python scripts/x_post_backfill.py run --limit 10
    python scripts/x_post_backfill.py run --yes --channel browser --report /tmp/x-post.json

scan is local and read-only. run probes candidates; only --yes writes feeds.db.
Only eligible post bodies are audited; merged Articles and ambiguous legacy rows are skipped.
Ellipses and wrapped URLs only affect priority within eligible posts.
run --resolve-legacy independently verifies missing provenance before admission;
only --yes persists those version-bound records, without inferring type from titles.
--all is a compatibility alias; --url selects exact stored URLs. Translation, export and
indexing use the normal pipeline.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import grab_store, registry as reg, x_browser, x_channels, x_post, x_post_meta
from kb.feeds import x_post_provenance
from kb.feeds.x_cdp import CdpError
from kb.feeds.x_page_state import XRateLimited

_SCHEME = re.compile('|'.join(
    r'(?:\r?\n)*'.join(re.escape(char) for char in scheme)
    for scheme in ('https://', 'http://')))
_URL_TAIL = re.compile(r'[^\s]*(?:\r?\n[^\s]+)*')
_DETAIL_URL = re.compile(r'https?://[^\s…]+')
_BREAK = re.compile(r'\r?\n')
_ELLIPSIS = re.compile(r'…|\.{3}')
_BEFORE_URL = re.compile(r'(?:^|\r?\n)(…|\.{3})\r?\n\Z')
_AFTER_URL = re.compile(r'(?:\r?\n)?(…|\.{3})(?![A-Za-z0-9_])')


def _match_url(text: str, start: int, end: int, url: str):
    cuts = []
    cursor = start
    for char in url:
        while cursor < end and text[cursor] != char:
            line = _BREAK.match(text, cursor, end)
            if not line:
                return None
            cuts.append(line.span())
            cursor = line.end()
        if cursor >= end:
            return None
        cursor += 1
    return cursor, cuts


def link_artifacts(text: str, detail: str | None = None) -> list[tuple[int, int]]:
    """Suspect URL runs broadly; confirm removable ranges against the detail URLs."""
    ranges = set()
    urls = [_DETAIL_URL.match(detail, scheme.start()) for scheme in
            _SCHEME.finditer(detail)] if detail is not None else []
    for index, scheme in enumerate(_SCHEME.finditer(text)):
        end = _URL_TAIL.match(text, scheme.end()).end()
        before = _BEFORE_URL.search(text[:scheme.start()])
        if detail is None:
            ranges.update(match.span() for match in _BREAK.finditer(text, scheme.start(), end))
            ranges.update(match.span() for match in _ELLIPSIS.finditer(text, scheme.start(), end))
            if before:
                ranges.add(before.span(1))
            continue
        if index >= len(urls) or not urls[index]:
            continue
        url = urls[index].group()
        matched = _match_url(text, scheme.start(), end, url)
        if not matched:
            matched = _match_url(text, scheme.start(), end, url.rstrip('.,;:!?)]}'))
        if not matched:
            continue
        cursor, cuts = matched
        ranges.update(cuts)
        if before:
            ranges.add(before.span(1))
        after = _AFTER_URL.match(text, cursor)
        if after:
            ranges.add(after.span(1))
    return sorted(ranges)


def canonical_preview(text: str) -> str:
    for start, end in reversed(link_artifacts(text)):
        text = text[:start] + text[end:]
    return text


def aligned_preview(preview: str, detail: str) -> bool:
    ranges = link_artifacts(preview, detail)
    if not ranges:
        return False
    pattern, cursor = [], 0
    for start, end in ranges:
        if start < cursor:
            continue
        pattern.append(re.escape(re.sub(r'\s+', '', preview[cursor:start])))
        token = re.sub(r'\s+', '', preview[start:end])
        if token:
            pattern.append('(?:' + re.escape(token) + ')?')
        cursor = end
    pattern.append(re.escape(re.sub(r'\s+', '', preview[cursor:])))
    return re.match(''.join(pattern), re.sub(r'\s+', '', detail)) is not None


def proposal(entry: dict, text: str) -> tuple[str, dict | None, dict]:
    """Only bypass raw length when known link artifacts explain the difference."""
    if skipped := exclusion(entry):
        return skipped['state'], None, dict(reason=skipped['reason'])
    prefix = entry[x_post.HINT]
    preview = entry['body'].removeprefix(prefix).strip()
    canonical = canonical_preview(preview)
    info = dict(artifacts=canonical != preview, after=len(prefix + text))
    if text == preview:
        return 'identical', None, dict(info, reason='detail_matches_stored')
    if x_post.expanded(text, preview):
        return 'expanded', x_post.merge(entry, text), dict(info, reason='longer_detail')
    if info['artifacts'] and aligned_preview(preview, text):
        complete = dict(entry)
        complete.pop(x_post.HINT)
        complete.update(title=text.splitlines()[0][:500], body=prefix + text)
        return 'normalized', x_post_meta.stamp(
            complete, 'post', prefix, author_text=x_post_meta.AuthorText(text, 'detail')), dict(
            info, reason='legacy_link_artifacts')
    state = 'blocked_artifacts' if info['artifacts'] else 'blocked_length'
    return state, None, dict(info, reason='legacy_link_artifacts_not_reconciled'
                            if info['artifacts'] else 'detail_not_longer')


def exclusion(row: dict) -> dict | None:
    reason, _prefix = x_post_meta.eligibility(row)
    if not reason:
        return None
    return dict(url=row['url'], before=len(row['body']), reason=reason,
                state='skipped_article' if reason == 'article_merged' else 'skipped_ambiguous')


def candidates(conn, *, urls=(), limit=0, all_posts=False, excluded=None,
               resolve_legacy=False) -> list[dict]:
    """Apply eligibility before limit; all_posts never bypasses provenance checks."""
    rows = conn.execute("SELECT * FROM feed_items WHERE kind='social' AND content_kind='post'"
                        " ORDER BY published DESC,key").fetchall()
    selected = []
    wanted = set(urls)
    for row in rows:
        if not x_post._STATUS.match(row['url']):
            continue
        if wanted:
            if row['url'] not in wanted:
                continue
        row = dict(row)
        if skipped := exclusion(row):
            if not (resolve_legacy and skipped['reason'] in x_post_meta.RECOVERABLE):
                if excluded is not None:
                    excluded.append(skipped)
                continue
        selected.append(row)
    selected.sort(key=lambda row: not (row['body'].rstrip().endswith(('…', '...')) or
                                      canonical_preview(row['body']) != row['body']))
    return selected[:limit] if limit else selected


def entry_of(row: dict) -> dict:
    reason, prefix = x_post_meta.eligibility(row)
    if reason:
        raise ValueError(f'X post is not eligible for detail audit: {reason}')
    return {**{key: row[key] for key in
               ('url', 'title', 'body', 'author', 'published', 'content_kind')},
            x_post.HINT: prefix, x_post_meta.FIELD: row.get(x_post_meta.FIELD, '')}


def repair(rows: list[dict], session, *, apply=False, deadline=None,
           should_stop=None, progress=None, excluded=(), resolve_legacy=False) -> dict:
    stats = dict(candidates=len(rows), expanded=0, normalized=0, updated=0,
                 identical=0, blocked_length=0, blocked_artifacts=0, unavailable=0,
                 failed=0, stale=0, deferred=0, skipped_article=0, skipped_ambiguous=0,
                 provenance_resolved=0, provenance_saved=0, results=list(excluded))
    for skipped in excluded:
        stats[skipped['state']] += 1
    for index, row in enumerate(rows):
        if (should_stop and should_stop()) or (
                deadline is not None and time.monotonic() >= deadline):
            stats['deferred'] = len(rows) - index
            break
        skipped = exclusion(row)
        resolving = resolve_legacy and skipped and skipped['reason'] in x_post_meta.RECOVERABLE
        if skipped and not resolving:
            stats[skipped['state']] += 1
            stats['results'].append(skipped)
            if progress:
                progress(skipped, stats)
            continue
        result = dict(url=row['url'], before=len(row['body']))
        try:
            text = None
            if resolving:
                annotated, text = x_post_provenance.resolve(
                    row, session, deadline=deadline, should_stop=should_stop,
                    aligned_preview=aligned_preview)
                row = x_post_provenance.save(row, annotated) if apply else annotated
                if row is None:
                    stats['stale'] += 1
                    result.update(state='stale', reason='provenance_snapshot_changed')
                else:
                    stats['provenance_resolved'] += 1
                    stats['provenance_saved'] += int(apply)
                    if skipped := exclusion(row):
                        stats[skipped['state']] += 1
                        result.update(skipped)
            if not result.get('state') and text is None:
                text = x_post.fetch(row['url'], session, deadline=deadline, should_stop=should_stop)
        except x_post.PostGone as exc:
            stats['unavailable'] += 1
            result.update(state='unavailable', error=str(exc))
        except XRateLimited as exc:
            stats['failed'] += 1
            stats['deferred'] = len(rows) - index - 1
            stats['stop_reason'] = str(exc)
            result.update(state='failed', error=str(exc), scope='batch')
        except x_post.PostUnavailable as exc:
            stats['failed'] += 1
            result.update(state='failed', error=str(exc))
        else:
            if not result.get('state'):
                state, complete, info = proposal(entry_of(row), text)
                stats[state] += 1
                result.update(state=state, **info)
                if complete and apply:
                    state = 'updated' if grab_store.replace(row, complete) else 'stale'
                    stats[state] += 1
                    result['state'] = state
        stats['results'].append(result)
        if progress:
            progress(result, stats)
        if stats.get('stop_reason'):
            break
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cmd', choices=['scan', 'run'])
    parser.add_argument('--yes', action='store_true', help='确认用版本化替换写回正文')
    parser.add_argument('--url', action='append', default=[], help='只检查指定的存量 URL,可重复')
    parser.add_argument('--all', action='store_true', help='兼容旧命令;不绕过长文与来源不明行的排除')
    parser.add_argument('--resolve-legacy', action='store_true',
                        help='核实缺失或损坏来源:长文接口及详情须能对齐;--yes 才保存来源')
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
        excluded = []
        rows = candidates(conn, urls=args.url, limit=args.limit, all_posts=args.all, excluded=excluded,
                          resolve_legacy=args.resolve_legacy)
    print(f'找到 {len(rows)} 条候选;仅核对有来源的短推文,或显式核实无来源旧行。')
    print('旧行可用 run --resolve-legacy 核实来源;加 --yes 才保存,不是强制绕过保护。')
    for skipped in excluded:
        print(json.dumps(skipped, ensure_ascii=False))
    if args.limit:
        print('已按 limit 限制本批数量,不代表其余存量已核对;截断与否须逐条核对详情页。')
    if args.cmd == 'scan':
        for row in rows:
            print(f'{len(row["body"]):>6} 字符  {row["url"]}')
        return 0
    print('模式:真正回填' if args.yes else '模式:只读核对,不写库(加 --yes 才写回)')
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)

    def report(result, stats):
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.report:
            args.report.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')

    if not rows:
        stats = repair([], None, excluded=excluded)
        if args.report:
            args.report.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0
    settings = reg.get_settings('social')
    deadline = time.monotonic() + args.budget
    try:
        with x_browser.browser_session(
                str(x_channels.BROWSER_PROFILE), settings.get('x_chrome_path', ''),
                attach=args.channel == 'attach') as page:
            page.open_page('about:blank')
            stats = repair(rows, page, apply=args.yes, deadline=deadline, progress=report,
                           excluded=excluded, resolve_legacy=args.resolve_legacy)
    except (CdpError, OSError, EOFError) as exc:
        print(f'浏览器不可用,未继续回填:{exc}', file=sys.stderr)
        return 1
    if args.report:
        args.report.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'可扩写 {stats["expanded"]} 条,可清理旧链接产物 {stats["normalized"]} 条,'
          f'实际写入 {stats["updated"]} 条,'
          f'并发换版跳过 {stats["stale"]} 条,失败 {stats["failed"]} 条,'
          f'预算/停止/限流扣留 {stats["deferred"]} 条。')
    print(f'与详情逐字相同 {stats["identical"]} 条(不等于确认完整),'
          f'长度拦截 {stats["blocked_length"]} 条,'
          f'含旧链接产物但未能安全对齐 {stats["blocked_artifacts"]} 条,'
          f'远端明确不可用 {stats["unavailable"]} 条(保留原存量)。')
    if stats.get('stop_reason'):
        print(f'整轮已停止:{stats["stop_reason"]}')
    print('已写入的条目会在正常同步中重新翻译、导出、索引和提取;本脚本不直接改 vault。')
    return 1 if any(stats[key] for key in
                    ('failed', 'deferred', 'blocked_length', 'blocked_artifacts')) else 0


if __name__ == '__main__':
    raise SystemExit(main())
