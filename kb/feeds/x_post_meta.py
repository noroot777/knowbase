"""Version-bound X body provenance and conservative legacy audit eligibility."""
from __future__ import annotations

import hashlib
import json

from kb.feeds.x_article_links import quote_lines

FIELD = 'x_post_meta'


def _fingerprint(entry: dict) -> str:
    payload = json.dumps([entry['title'], entry['body']], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def context_prefix(body: str, context: str) -> str:
    """Use only source-declared context, never an author's own quoted text."""
    if not context:
        return ''
    prefix = context if body == context else context + '\n\n'
    if quote_lines(context) != context or not body.startswith(prefix):
        raise ValueError('X 上下文必须是正文开头的完整引用块')
    return prefix


def stamp(entry: dict, kind: str, prefix: str, *, author_text: str | None = None) -> dict:
    """Require an explicit boundary; leading authored quotes need source-text evidence."""
    body = entry['body']
    authored_quote = (not prefix and body.startswith('> ') and isinstance(author_text, str)
                      and author_text.startswith('> ') and body.startswith(author_text))
    if kind == 'post' and (not isinstance(prefix, str) or not body.startswith(prefix) or (
            not prefix and body.startswith('> ') and not authored_quote)):
        raise ValueError('X 正文的引用块必须有明确上下文边界或作者原文证据')
    meta = dict(version=1, kind=kind, prefix=prefix, fingerprint=_fingerprint(entry))
    if authored_quote:
        meta['authored_quote'] = True
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
                not entry['body'].startswith(prefix)) or (
                not prefix and entry['body'].startswith('> ') and meta.get('authored_quote') is not True):
            return 'invalid_provenance', ''
        return '', prefix
    return 'unclassified_legacy', ''
