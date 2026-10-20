"""Versioned audit receipts, separate from feeds.db even in read-only probe mode.

Change POLICY when the content-comparison contract changes, not for queue/display fixes.
Remaining debt retries after failures; matching or accumulated safe reads settle that version.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing

from kb.feeds import grab_store, registry as reg

POLICY = 'confirmed-ranges-1'
AVAILABILITY_POLICY = 'content-http-conflict-1'
SAFE_RECHECKS = 2
RETRY = {'failed', 'stale', 'deferred'}
REPAIRABLE = {'expanded', 'normalized'}
SCHEMA = '''CREATE TABLE IF NOT EXISTS audits (
 item_key TEXT NOT NULL, version TEXT NOT NULL, checked REAL NOT NULL,
 result TEXT NOT NULL, PRIMARY KEY(item_key, version)
)'''


class Candidate(dict):
    def __init__(self, row, recheck):
        super().__init__(row)
        self.recheck = recheck


def pending(result: dict, *, apply=False) -> bool:
    return bool(result.get('remaining') or result['state'] in RETRY or (
        result['state'] == 'unavailable'
        and result.get('availability_policy') != AVAILABILITY_POLICY) or (
        apply and result['state'] in REPAIRABLE))


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
            if pending(result):
                selected.append(((1, checked, index), row))
            elif apply and result['state'] in REPAIRABLE:
                selected.append(((0, 0, index), row))
        else:
            selected.append(((0, 0, index), Candidate(row, recheck)))
    return [row for _, row in sorted(selected, key=lambda candidate: candidate[0])]


def begin(row: dict, *, apply=False) -> int | None:
    filename = path()
    filename.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(filename, timeout=15)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(SCHEMA)
        revision = version(row)
        prior = conn.execute('SELECT result FROM audits WHERE item_key=? AND version=?',
                             (row['key'], revision)).fetchone()
        previous = json.loads(prior[0]) if prior else {}
        recheck = getattr(row, 'recheck', False)
        if prior and not recheck and not pending(previous, apply=apply):
            return None
        generation = previous.get('generation', 0) + int(recheck)
        if recheck or not prior:
            result = dict(state='deferred', generation=generation)
            if remaining := previous.get('remaining') or previous.get('retained'):
                result['remaining'] = remaining
            conn.execute('INSERT INTO audits VALUES (?,?,?,?) ON CONFLICT(item_key,version) '
                         'DO UPDATE SET checked=excluded.checked,result=excluded.result',
                         (row['key'], revision, time.time(), json.dumps(result, ensure_ascii=False)))
        return generation


def record(row: dict, result: dict, *, complete=None, generation=None) -> None:
    if result['state'] == 'deferred':
        return
    if result['state'] == 'unavailable':
        result = dict(result, availability_policy=AVAILABILITY_POLICY)
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
            previous = json.loads(prior[0]) if prior else {}
            expected = (generation if revision == version(row) else 0) if generation is not None else (
                previous.get('generation', 0))
            if expected != previous.get('generation', 0):
                continue
            outcome = dict(outcome, generation=expected)
            if prior and not pending(previous, apply=True) and outcome['state'] not in {
                    'updated', 'expanded', 'normalized'}:
                continue
            if prior and outcome['state'] not in {'updated', 'expanded', 'normalized', 'unavailable'}:
                remaining = previous.get('remaining')
                if remaining:
                    state = outcome['state']
                    safe = state == 'identical' or state.startswith('blocked_')
                    checks = previous.get('safe_rechecks', int(
                        previous['state'] == 'identical' or previous['state'].startswith('blocked_')))
                    checks += int(safe)
                    outcome = dict(outcome, safe_rechecks=checks)
                    settled = safe and (state == remaining or checks >= SAFE_RECHECKS)
                    if settled:
                        outcome = dict(outcome, retained=remaining)
                        outcome.pop('remaining', None)
                    else:
                        outcome = dict(outcome, remaining=remaining)
            conn.execute('INSERT INTO audits VALUES (?,?,?,?) ON CONFLICT(item_key,version) '
                         'DO UPDATE SET checked=excluded.checked,result=excluded.result',
                         (seen['key'], revision, time.time(), json.dumps(outcome, ensure_ascii=False)))
