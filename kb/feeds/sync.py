"""Incremental fetch and atomic Markdown export for all feed-backed source types."""
from __future__ import annotations

import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from kb.config import VAULT_ROOT
from kb.feeds import registry as reg
from kb.feeds.fetching import CollectionBudgetExceeded, SOURCE_SECONDS, fetch_source
from kb.pipeline_state import _state, _stage

FETCH_WORKERS = 4
COLLECT_SECONDS = 90
BRIDGE_FAILURE_LIMIT = 3
BUDGET_MESSAGE = '本轮采集时间预算不足，剩余订阅留待下次同步'


def _fetch_with_budget(source, settings, scope, collect_deadline):
    # Check again in the worker: submission/scheduling can use up the margin.
    # Each admitted source gets its own full allowance, never a clipped tail.
    deadline = time.monotonic() + SOURCE_SECONDS
    if deadline > collect_deadline:
        raise CollectionBudgetExceeded
    return fetch_source(source, settings, unconditional=scope.ranged,
                        deadline=deadline, collect_deadline=collect_deadline)


def _save_result(source, result, scope):
    entries, validators = result
    if scope.ranged:
        entries = [e for e in entries if e['published'] and scope.since <= e['published'][:10] <= scope.until]
        # A ranged import must not cache away entries outside the selected dates.
        validators = {k: source[k] for k in ('etag', 'modified', 'resolved_url')}
    added = reg.put_items(source, entries)
    with reg.db() as c:
        c.execute('''UPDATE subscriptions SET last_attempt=?,last_success=?,last_error='',
            failures=0,retry_at=0,etag=?,modified=?,resolved_url=? WHERE id=?''',
            (reg.now(),reg.now(),validators['etag'],validators['modified'],validators['resolved_url'],source['id']))
    return added


def _save_failure(source, error, summary):
    reason = str(error)[:1200] or type(error).__name__
    summary['errors'].append({'title': source['name'], 'reason': reason})
    with reg.db() as c:
        c.execute('''UPDATE subscriptions SET last_attempt=?,last_error=?,failures=failures+1,
            retry_at=? WHERE id=?''', (reg.now(),reason,
            time.time() + min(86400, 1800 * 2 ** min(source['failures'], 6)),source['id']))


def collect(kind, scope):
    started = reg.now()
    sources = [s for s in reg.subscriptions(kind) if s['enabled'] and
               (not scope.targets or str(s['id']) in scope.targets or s['name'] in scope.targets)]
    summary = {'sources': len(sources), 'checked': 0, 'added': 0, 'deferred': 0,
               'unconfigured': 0, 'skipped': 0, 'message': '', 'errors': [], 'stopped': False}
    settings = reg.get_settings(kind)
    pending = []
    for source in sources:
        if reg.configuration_error(source, settings):
            summary['unconfigured'] += 1
        elif source['retry_at'] > time.time() and not scope.force:
            summary['deferred'] += 1
        else:
            pending.append(source)
    # Budget-limited runs continue with never/least recently attempted sources.
    pending.sort(key=lambda s: (s['last_attempt'], s['id']))
    deadline = time.monotonic() + COLLECT_SECONDS
    bridge_failures = 0
    # Fetches alone run concurrently; persistence and pipeline state have one writer.
    # Submit only one bounded batch so failures/stop never leave a 51-job backlog.
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix='feed') as pool:
        while pending:
            if _state.get('stop_requested'):
                summary['stopped'] = True
                break
            if deadline - time.monotonic() < SOURCE_SECONDS:
                summary['skipped'] += len(pending)
                summary['message'] = BUDGET_MESSAGE
                break
            if bridge_failures >= BRIDGE_FAILURE_LIMIT:
                independent = [s for s in pending if not reg.uses_bridge(s)]
                summary['skipped'] += len(pending) - len(independent)
                summary['message'] = 'X 桥接连续失败，已暂停本轮其余桥接请求；请检查桥接设置后重试'
                pending = independent
            batch, pending = pending[:FETCH_WORKERS], pending[FETCH_WORKERS:]
            if not batch:
                break
            _stage('crawl', 'running', f"{reg.KINDS[kind]} · 正在读取 {len(batch)} 个来源（已检查 {summary['checked']}/{len(sources)}）")
            futures = {pool.submit(_fetch_with_budget, s, settings, scope, deadline): s for s in batch}
            shared_ok, shared_errors = False, 0
            for future in as_completed(futures):
                source = futures[future]
                try:
                    result = future.result()
                    shared_ok |= reg.uses_bridge(source)
                    summary['added'] += _save_result(source, result, scope)
                except CollectionBudgetExceeded:
                    summary['skipped'] += 1
                    summary['message'] = BUDGET_MESSAGE
                    continue
                except Exception as e:
                    shared_errors += int(reg.uses_bridge(source))
                    _save_failure(source, e, summary)
                summary['checked'] += 1
                _stage('crawl', 'running', f"{reg.KINDS[kind]} {summary['checked']}/{len(sources)} · {source['name']}")
            bridge_failures = 0 if shared_ok else bridge_failures + shared_errors
    summary['stopped'] |= bool(_state.get('stop_requested'))
    with reg.db() as c:
        c.execute('INSERT INTO feed_runs(kind,started_at,finished_at,summary) VALUES(?,?,?,?)',
                  (kind,started,reg.now(),json.dumps(summary,ensure_ascii=False)))
        c.execute('DELETE FROM feed_runs WHERE kind=? AND id NOT IN '
                  '(SELECT id FROM feed_runs WHERE kind=? ORDER BY id DESC LIMIT 30)', (kind,kind))
    label = f"检查 {summary['checked']}/{len(sources)} 个来源，新增 {summary['added']} 条"
    if summary['deferred']:
        label += f"，{summary['deferred']} 个等待重试"
    if summary['errors']:
        label += f"，失败 {len(summary['errors'])} 个"
    if summary['unconfigured']:
        label += f"，待配置 {summary['unconfigured']} 个（请填写 X 桥接或专用订阅链接）"
    if summary['message']:
        label += f"，跳过 {summary['skipped']} 个；{summary['message']}"
    status = 'error' if summary['errors'] or summary['skipped'] else 'skipped' if summary['unconfigured'] else 'ok'
    _stage('crawl', 'stopped' if summary['stopped'] else status,
           label, failures=summary['errors'])
    return summary


def export(kind: str):
    with reg.db() as c:
        rows = c.execute('SELECT * FROM feed_items WHERE kind=? AND exported_hash<>content_hash', (kind,)).fetchall()
    errors, done = [], 0
    root = VAULT_ROOT / 'source' / kind
    root.mkdir(parents=True, exist_ok=True)
    for item in rows:
        if _state.get('stop_requested'):
            break
        path = root / (item['key'] + '.md')
        rel = str(path.relative_to(VAULT_ROOT))
        meta = dict(source=kind, title=item['title'], account=item['author'], published=item['published'],
                    url=item['url'], fetched_at=item['fetched_at'], content_kind=item['content_kind'])
        notice = '\n> 以下为订阅源提供的摘要；完整内容请打开原文。\n' if item['content_kind'] == 'summary' else ''
        text = '---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n\n'
        # Keep the parsed text intact for the vault, indexing and extraction.
        # HTML safety belongs to the reader (textContent / DOMPurify).
        text += '# ' + item['title'] + '\n' + notice + '\n' + item['body'] + '\n'
        temp = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=root, suffix='.tmp', encoding='utf-8', delete=False) as f:
                temp = Path(f.name)
                f.write(text)
            os.replace(temp, path)
            with reg.db() as c:
                c.execute('UPDATE feed_items SET exported_hash=content_hash,path=? WHERE key=?', (rel,item['key']))
            done += 1
        except Exception as e:
            errors.append({'title': item['title'], 'reason': str(e)[:300]})
        finally:
            if temp:
                temp.unlink(missing_ok=True)
    _stage('export', 'error' if errors else 'ok', f'已保存 {done} 条到知识库', failures=errors)
