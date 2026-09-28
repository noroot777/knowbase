"""Incremental fetch and atomic Markdown export for all feed-backed source types."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import yaml

from kb.config import VAULT_ROOT
from kb.feeds import registry as reg
from kb.feeds.fetching import fetch_source
from kb.pipeline_state import _state, _stage


def collect(kind, scope):
    started = reg.now()
    sources = [s for s in reg.subscriptions(kind) if s['enabled'] and
               (not scope.targets or str(s['id']) in scope.targets or s['name'] in scope.targets)]
    summary = {'sources': len(sources), 'checked': 0, 'added': 0, 'deferred': 0, 'errors': [], 'stopped': False}
    settings = reg.get_settings(kind)
    for source in sources:
        if _state.get('stop_requested'):
            summary['stopped'] = True
            break
        if source['retry_at'] > time.time() and not scope.force:
            summary['deferred'] += 1
            continue
        _stage('crawl', 'running', f"{reg.KINDS[kind]} {summary['checked'] + 1}/{len(sources)} · {source['name']}")
        try:
            entries, validators = fetch_source(source, settings, unconditional=scope.ranged)
            if scope.ranged:
                entries = [e for e in entries if e['published'] and scope.since <= e['published'][:10] <= scope.until]
                # A filtered import must never install validators that would hide
                # out-of-range entries from the next ordinary incremental run.
                validators = {k:source[k] for k in ('etag','modified','resolved_url')}
            summary['added'] += reg.put_items(source, entries)
            with reg.db() as c:
                c.execute('''UPDATE subscriptions SET last_attempt=?,last_success=?,last_error='',
                    failures=0,retry_at=0,etag=?,modified=?,resolved_url=? WHERE id=?''',
                    (reg.now(),reg.now(),validators['etag'],validators['modified'],validators['resolved_url'],source['id']))
        except Exception as e:
            reason = str(e)[:1200] or type(e).__name__
            summary['errors'].append({'title': source['name'], 'reason': reason})
            with reg.db() as c:
                c.execute('''UPDATE subscriptions SET last_attempt=?,last_error=?,failures=failures+1,
                    retry_at=? WHERE id=?''', (reg.now(),reason,
                    time.time() + min(86400, 1800 * 2 ** min(source['failures'], 6)),source['id']))
        summary['checked'] += 1
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
    _stage('crawl', 'stopped' if summary['stopped'] else ('error' if summary['errors'] else 'ok'),
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
        # Store plain text. Escape raw HTML so scripts can never become active in downstream readers.
        body = item['body'].replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        text += '# ' + item['title'].replace('<','&lt;') + '\n' + notice + '\n' + body + '\n'
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
