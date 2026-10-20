#!/usr/bin/env python3
"""Scan or repair stored X previews without overwriting concurrently replaced content.

    python scripts/x_post_backfill.py scan
    python scripts/x_post_backfill.py run --limit 10
    python scripts/x_post_backfill.py run --yes --channel browser --report /tmp/x-post.json

scan lists all stored X posts and their current audit results, locally and read-only.
run probes candidates; only --yes writes feeds.db.
Every unaudited or changed X post is selected by default; failures remain retryable.
Audit receipts are saved beside feeds.db, without changing feed content in probe mode.
--recheck repeats completed audits; ellipses and wrapped URLs only affect priority.
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

from kb.feeds import grab_store, registry as reg, x_body, x_browser, x_channels, x_post
from kb.feeds.x_cdp import CdpError
from kb.feeds.x_page_state import XAccessBlocked, XRateLimited
from scripts import x_post_audit as audit

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
            ranges.add((before.start(1), before.end()))
        after = _AFTER_URL.match(text, cursor)
        if after:
            ranges.add(after.span())
    return sorted(ranges)


def canonical_preview(text: str) -> str:
    for start, end in reversed(link_artifacts(text)):
        text = text[:start] + text[end:]
    return text


def _covered_preview(preview: str, detail: str, *, keep_tail=False) -> str | None:
    ranges = link_artifacts(preview, detail)
    tail_range = None
    tail = re.search(r'(?:…|\.{3})\Z', preview)
    if (tail and tail.end() == len(preview) and x_post.expanded(detail, preview)
            and not any(start <= tail.start() < end for start, end in ranges)):
        tail_range = tail.span()
        ranges.append(tail_range)
        ranges.sort()
    pattern, pieces, cursor = [], [], 0
    for index, (start, end) in enumerate(ranges):
        if start < cursor:
            continue
        segment = preview[cursor:start]
        pattern.append(re.escape(re.sub(r'\s+', '', segment)))
        pieces.append((segment, None))
        token = re.sub(r'\s+', '', preview[start:end])
        if token:
            group = f'artifact_{index}'
            pattern.append(f'(?P<{group}>' + re.escape(token) + ')?')
            pieces.append((preview[start:end], None if keep_tail and
                           (start, end) == tail_range else group))
        cursor = end
    pattern.append(re.escape(re.sub(r'\s+', '', preview[cursor:])))
    pieces.append((preview[cursor:], None))
    match = re.match(''.join(pattern), re.sub(r'\s+', '', detail))
    if match is None:
        return None
    return ''.join(segment for segment, group in pieces if group is None or match[group])


def _gaps(text: str) -> dict[int, str]:
    gaps, offset = {}, 0
    for token in re.findall(r'\s+|\S', text):
        if token.isspace():
            gaps[offset] = token.replace('\r\n', '\n')
        else:
            offset += 1
    return gaps


def _alignment(preview: str, detail: str, evidence=None) -> str:
    fixed = _covered_preview(preview, detail)
    if fixed is None:
        return 'content'
    positions = set()
    legacy = None
    evidence_mismatch = isinstance(evidence, dict) and any(
        evidence.get(key) is not None for key in ('inline_plain', 'inline_legacy'))
    if (isinstance(evidence, dict) and evidence.get('text') == detail
            and evidence.get('inline_plain') == re.sub(r'\s+', '', detail)
            and isinstance(evidence.get('inline_breaks'), list)
            and isinstance(evidence.get('inline_legacy'), str)):
        positions = {offset for offset in evidence['inline_breaks']
                     if type(offset) is int and 0 < offset < len(re.sub(r'\s+', '', detail))}
        legacy = (evidence['inline_legacy'] if re.sub(r'\s+', '', evidence['inline_legacy'])
                  == evidence['inline_plain'] else None)
        evidence_mismatch = legacy is None
    source_gaps = _gaps(legacy) if legacy is not None else {}
    gaps = _gaps(detail)
    for offset, original in _gaps(fixed).items():
        replacement = gaps.get(offset, '')
        remaining = iter(replacement)
        if all(any(char == candidate for candidate in remaining) for char in original):
            continue
        source = source_gaps.get(offset, '')
        if (offset in positions and replacement.count('\n') < original.count('\n')
                <= source.count('\n')
                and not any(char in original for char in '\r\v\f\x85\u2028\u2029')):
            continue
        return 'evidence' if evidence_mismatch else 'whitespace'
    return 'matched'


def aligned_preview(preview: str, detail: str, evidence=None) -> bool:
    return _alignment(preview, detail, evidence) == 'matched'


def proposal(entry: dict, text: str, evidence=None, *,
             empty: x_post.PostEmpty | None = None) -> tuple[str, dict | None, dict]:
    """Require known fragments and source alignment before any stored-body replacement."""
    body = x_body.parts(entry)
    info = dict(after=len(entry['body']), artifacts=canonical_preview(body.text) != body.text)
    if empty is not None:
        info.update(empty_kind=empty.kind, detail=str(empty))
        if entry['body'] == '' or (body.source_known and not body.text):
            return 'empty', None, dict(info, reason='confirmed_empty')
    elif text == entry['body'] or text == body.text:
        return 'identical', None, dict(info, reason='detail_matches_stored')
    if not body.source_known:
        return 'unknown_provenance', None, dict(info, reason='body_provenance_unknown',
                                               detail=str(x_body.SourceUnknown()))
    if empty is not None:
        return 'blocked_length', None, dict(info, reason='remote_empty_stored_text')
    preview = body.text.strip()
    info.update(artifacts=canonical_preview(preview) != preview, after=len(body.render(text)))
    if text == preview:
        return 'identical', None, dict(info, reason='detail_matches_stored')
    alignment = _alignment(preview, text, evidence)
    if alignment in {'whitespace', 'evidence'}:
        fixed = _covered_preview(preview, text, keep_tail=True)
        if fixed is not None and fixed != preview:
            original = body.text
            start = len(original) - len(original.lstrip())
            end = len(original.rstrip())
            complete = x_body.with_text(entry, original[:start] + fixed + original[end:])
            complete['body'] += entry['body'][len(entry['body'].rstrip()):]
            complete.pop(x_post.HINT)
            complete.update(title=fixed.splitlines()[0][:500])
            return 'normalized', complete, dict(
                info, after=len(complete['body']), reason='confirmed_link_ranges',
                remaining='blocked_' + alignment)
    if alignment == 'evidence':
        return 'blocked_evidence', None, dict(info, reason='source_evidence_mismatch')
    if alignment == 'whitespace':
        return 'blocked_whitespace', None, dict(info, reason='unproven_whitespace_change')
    if alignment == 'content':
        return 'blocked_content', None, dict(info, reason='detail_does_not_cover_stored')
    if x_post.expanded(text, preview):
        return 'expanded', x_post.merge(entry, text), dict(info, reason='longer_detail')
    if alignment == 'matched':
        complete = x_body.with_text(entry, text)
        complete.pop(x_post.HINT)
        complete.update(title=text.splitlines()[0][:500])
        return 'normalized', complete, dict(info, reason='legacy_link_artifacts'
                                           if info['artifacts'] else 'legacy_whitespace')
    state = 'blocked_artifacts' if info['artifacts'] else 'blocked_length'
    return state, None, dict(info, reason='legacy_link_artifacts_not_reconciled'
                            if info['artifacts'] else 'detail_not_longer')


def candidates(conn, *, urls=(), limit=0, all_posts=False, apply=False, recheck=False,
               scan=False) -> list[dict]:
    """Audit all X posts; all_posts remains accepted for existing callers."""
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
        selected.append(dict(row))
    selected.sort(key=lambda row: not (row['body'].rstrip().endswith(('…', '...')) or
                                      canonical_preview(row['body']) != row['body']))
    if not scan:
        selected = audit.select(selected, apply=apply, recheck=recheck or bool(wanted))
    return selected[:limit] if limit else selected


def entry_of(row: dict) -> dict:
    return {**{key: row[key] for key in
               ('url', 'title', 'body', 'author', 'published', 'content_kind')},
            x_body.PROVENANCE: row.get(x_body.PROVENANCE, ''),
            x_post.HINT: x_body.parts(row).prefix}


def repair(rows: list[dict], session, *, apply=False, deadline=None,
           should_stop=None, progress=None, remember=False) -> dict:
    stats = dict(candidates=len(rows), expanded=0, normalized=0, updated=0,
                 identical=0, empty=0, blocked_length=0, blocked_artifacts=0, unavailable=0,
                 blocked_whitespace=0, blocked_content=0, blocked_evidence=0,
                 failed=0, unknown_structure=0, unknown_provenance=0,
                 stale=0, deferred=0, partial=0, results=[])
    for index, row in enumerate(rows):
        if (should_stop and should_stop()) or (
                deadline is not None and time.monotonic() >= deadline):
            stats['deferred'] += len(rows) - index
            break
        generation = audit.begin(row, apply=apply) if remember else None
        if remember and generation is None:
            stats['deferred'] += 1
            continue
        result = dict(url=row['url'], before=len(row['body']))
        evidence = {}
        complete = None
        try:
            text = x_post.fetch(row['url'], session, deadline=deadline, should_stop=should_stop,
                                evidence=evidence)
        except x_post.PostEmpty as exc:
            state, complete, info = proposal(entry_of(row), '', empty=exc)
            stats[state] += 1
            result.update(state=state, **info)
        except x_post.PostGone as exc:
            stats['unavailable'] += 1
            result.update(state='unavailable', error=str(exc))
        except (XRateLimited, XAccessBlocked, x_post.PostSessionUnavailable) as exc:
            stats['failed'] += 1
            stats['deferred'] += len(rows) - index - 1
            stats['stop_reason'] = str(exc)
            result.update(state='failed', error=str(exc), scope='batch')
        except x_post.PostHalted as exc:
            stats['deferred'] += len(rows) - index
            stats['stop_reason'] = str(exc)
            result.update(state='deferred', error=str(exc), scope='batch')
        except x_post.PostUnavailable as exc:
            stats['failed'] += 1
            result.update(state='failed', error=str(exc))
            if isinstance(exc, x_post.PostStructureUnknown):
                stats['unknown_structure'] += 1
                result['reason'] = 'unknown_structure'
                if stats['unknown_structure'] >= 3 and stats['unknown_structure'] / (index + 1) >= 0.5:
                    stats['deferred'] = len(rows) - index - 1
                    stats['stop_reason'] = '多条已加载详情的正文结构无法识别,疑似 X 结构变化;停止本批并保留原行'
                    result['scope'] = 'batch'
        else:
            state, complete, info = proposal(entry_of(row), text, evidence)
            stats[state] += 1
            result.update(state=state, **info)
            if complete and apply:
                state = 'updated' if grab_store.replace(row, complete) else 'stale'
                stats[state] += 1
                result['state'] = state
                if state == 'updated' and info.get('remaining'):
                    stats['partial'] += 1
        if remember:
            audit.record(row, result, complete=complete, generation=generation)
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
    parser.add_argument('--all', action='store_true', help='兼容旧命令;默认已检查全部存量 X 推文')
    parser.add_argument('--recheck', action='store_true', help='重新核对已记过结论的当前正文版本')
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
        rows = candidates(conn, urls=args.url, limit=args.limit, all_posts=args.all,
                          apply=args.yes, recheck=args.recheck, scan=args.cmd == 'scan')
    scope = ('列出存量 X 推文及当前版本的核对结论,不隐藏已核对项。' if args.cmd == 'scan' else
             '默认续查未核对或正文已变的存量 X 推文,失败与局部修复欠账排在新项之后重试。')
    print(f'找到 {len(rows)} 条候选;' + scope)
    print('核对记录:' + str(audit.path()) + ';--recheck 或 --url 可重查已有结论。')
    if args.limit:
        print('已按 limit 限制本批数量,不代表其余存量已核对;截断与否须逐条核对详情页。')
    if args.cmd == 'scan':
        receipts = audit.load()
        for row in rows:
            prior = receipts.get((row['key'], audit.version(row)))
            outcome = prior[1] if prior else dict(state='unaudited')
            label = ('不可用结论待复核' if outcome['state'] == 'unavailable' and
                     outcome.get('availability_policy') != audit.AVAILABILITY_POLICY else
                     '仍需补查' if outcome.get('remaining') else
                     '可写回' if outcome['state'] in audit.REPAIRABLE else
                     '已扣留' if outcome['state'].startswith('blocked_') else
                     '失败待重试' if outcome['state'] in audit.RETRY else
                     '未核对' if not prior else '已有结论(不保证完整)')
            print(f'{len(row["body"]):>6} 字符  {row["url"]}  [{label}] '
                  + json.dumps(outcome, ensure_ascii=False))
        return 0
    if not rows:
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(repair([], None), ensure_ascii=False, indent=2),
                                   encoding='utf-8')
        return 0
    print('模式:真正回填' if args.yes else '模式:只读核对,不写正文库,只记核对进度(加 --yes 才写回)')
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
            stats = repair(rows, page, apply=args.yes, deadline=deadline, progress=report,
                           remember=True)
    except (CdpError, OSError, EOFError) as exc:
        print(f'浏览器不可用,未继续回填:{exc}', file=sys.stderr)
        return 1
    if args.report:
        args.report.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'可扩写 {stats["expanded"]} 条,可清理旧链接产物 {stats["normalized"]} 条,'
          f'实际写入 {stats["updated"]} 条,'
          f'正文结构尚未确认 {stats["unknown_structure"]} 条,'
          f'原正文来源待核 {stats["unknown_provenance"]} 条(保留原行,需重新采集补证),'
          f'并发换版跳过 {stats["stale"]} 条,失败 {stats["failed"]} 条,'
          f'预算/停止/限流扣留 {stats["deferred"]} 条。')
    print(f'其中仅作局部修复 {stats.get("partial", 0)} 条,未确认的空白和截断标记仍保留。')
    print(f'与详情逐字相同 {stats["identical"]} 条(不等于确认完整),'
          f'长度拦截 {stats["blocked_length"]} 条,'
          f'含旧链接产物但未能安全对齐 {stats["blocked_artifacts"]} 条,'
          f'空白改动缺少证据 {stats["blocked_whitespace"]} 条,'
          f'来源读数证据不一致 {stats["blocked_evidence"]} 条,'
          f'正文内容无法对齐 {stats["blocked_content"]} 条,'
          f'远端明确不可用 {stats["unavailable"]} 条(保留原存量),'
          f'已确认无自写文字 {stats["empty"]} 条(保留原行,无需回填)。')
    if stats.get('stop_reason'):
        print(f'整轮已停止:{stats["stop_reason"]}')
    print('已写入的条目会在正常同步中重新翻译、导出、索引和提取;本脚本不直接改 vault。')
    empty_conflict = any(result.get('reason') == 'remote_empty_stored_text'
                         for result in stats['results'])
    return int(empty_conflict or any(stats[key] for key in
                                   ('failed', 'deferred', 'unknown_provenance')))


if __name__ == '__main__':
    raise SystemExit(main())
