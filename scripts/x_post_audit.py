"""Versioned audit receipts, separate from feeds.db even in read-only probe mode.

Change POLICY when the content-comparison contract changes, not for queue/display fixes.
Remaining debt survives inconclusive rereads until a full repair proposal supersedes it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing

from kb.feeds import grab_store, registry as reg

POLICY = 'confirmed-ranges-1'
RETRY = {'failed', 'stale', 'deferred'}
REPAIRABLE = {'expanded', 'normalized'}
SCHEMA = '''CREATE TABLE IF NOT EXISTS audits (
 item_key TEXT NOT NULL, version TEXT NOT NULL, checked REAL NOT NULL,
 result TEXT NOT NULL, PRIMARY KEY(item_key, version)
)'''


def path():
    return reg.DB_PATH.with_suffix('.x-post-audit.db')


def version(row: dict) -> str:
    payload = [POLICY, *grab_store.work_version(row), row['url'], grab_store.item_version(row)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def load() -> dict:
    filename = path()
    if not filename.exists():
        return {}
    with closing(sqlite3.connect(filename.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audits'").fetchone():
            return {}
        return {(key, revision): (checked, json.loads(result))
                for key, revision, checked, result in conn.execute(
                    'SELECT item_key,version,checked,result FROM audits')}


def select(rows: list[dict], *, apply=False, recheck=False) -> list[dict]:
    receipts = load()
    selected = []
    for index, row in enumerate(rows):
        prior = receipts.get((row['key'], version(row)))
        if prior and not recheck:
            checked, result = prior
            if result.get('remaining') or result['state'] in RETRY:
                selected.append(((1, checked, index), row))
            elif apply and result['state'] in REPAIRABLE:
                selected.append(((0, 0, index), row))
        else:
            selected.append(((0, 0, index), row))
    return [row for _, row in sorted(selected, key=lambda candidate: candidate[0])]


def record(row: dict, result: dict, *, complete=None) -> None:
    if result['state'] == 'deferred':
        return
    receipts = [(row, result)]
    if result['state'] == 'updated' and complete is not None:
        with reg.db() as conn:
            current = conn.execute('SELECT * FROM feed_items WHERE key=?', (row['key'],)).fetchone()
        if (current and current['content_rev'] == row['content_rev'] + 1
                and current['content_hash'] == grab_store.item_version(complete)):
            receipts.append((dict(current), result))
    filename = path()
    filename.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(filename, timeout=15)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(SCHEMA)
        for seen, outcome in receipts:
            revision = version(seen)
            prior = conn.execute('SELECT result FROM audits WHERE item_key=? AND version=?',
                                 (seen['key'], revision)).fetchone()
            if prior and outcome['state'] not in {'updated', 'expanded', 'normalized', 'unavailable'}:
                remaining = json.loads(prior[0]).get('remaining')
                if remaining:
                    outcome = dict(outcome, remaining=remaining)
            conn.execute('INSERT INTO audits VALUES (?,?,?,?) ON CONFLICT(item_key,version) '
                         'DO UPDATE SET checked=excluded.checked,result=excluded.result',
                         (seen['key'], revision, time.time(), json.dumps(outcome, ensure_ascii=False)))
