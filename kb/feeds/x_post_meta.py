"""Version-bound X body provenance and conservative legacy audit eligibility."""
from __future__ import annotations

import hashlib
import json

FIELD = 'x_post_meta'


def _fingerprint(entry: dict) -> str:
    payload = json.dumps([entry['title'], entry['body']], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def stamp(entry: dict, kind: str, prefix: str = '') -> dict:
    meta = dict(version=1, kind=kind, prefix=prefix, fingerprint=_fingerprint(entry))
    return {**entry, FIELD: json.dumps(meta, ensure_ascii=False, sort_keys=True)}


def eligibility(entry: dict) -> tuple[str, str]:
    """Return (exclusion reason, proven prefix); mismatched titles never prove Article."""
    raw = entry.get(FIELD, '')
    if raw:
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            return 'invalid_provenance', ''
        if not isinstance(meta, dict) or meta.get('version') != 1 or (
                meta.get('fingerprint') != _fingerprint(entry)):
            return 'invalid_provenance', ''
        if meta.get('kind') == 'article':
            return 'article_merged', ''
        prefix = meta.get('prefix')
        if meta.get('kind') != 'post' or not isinstance(prefix, str) or (
                not entry['body'].startswith(prefix)):
            return 'invalid_provenance', ''
        return '', prefix
    body, title = entry['body'], entry['title']
    if not body:
        return '', ''

    def matches(text: str) -> bool:
        return bool(title) and title in (text[:500], text.split('\n', 1)[0][:500])

    if matches(body):
        return '', ''
    head, separator, tail = body.partition('\n\n')
    if separator and head and all(line.startswith('> ') for line in head.splitlines()) and (
            matches(tail)):
        return '', head + separator
    return 'unclassified_legacy', ''
