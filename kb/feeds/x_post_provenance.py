"""Resolve legacy X body provenance from remote content, never from title shape."""
from __future__ import annotations

import re
import time

from kb.feeds import registry as reg, x_article, x_post, x_post_meta


def _covers(preview: str, detail: str, aligned_preview=None) -> bool:
    compact = re.sub(r'\s+', '', preview)
    whole = re.sub(r'\s+', '', detail)
    if whole.startswith(compact):
        return True
    stem = compact.removesuffix('…').removesuffix('...')
    return (bool(stem) and stem != compact and whole.startswith(stem)) or (
        bool(aligned_preview) and aligned_preview(preview, detail))


def resolve(row: dict, session, *, deadline=None, should_stop=None,
            aligned_preview=None) -> tuple[dict, str | None]:
    """Prove a stored Article suffix or match stored text against a verified post detail."""
    x_post._check(deadline, should_stop)
    if not x_article.API:
        raise x_post.PostUnavailable('长文接口已关闭，无法核实旧行来源；原行保持来源不明')
    timeout = min(x_article.TIMEOUT, deadline - time.monotonic()) if deadline else x_article.TIMEOUT
    try:
        article = x_article.fetch(row['url'], timeout=max(0.001, timeout))
    except x_article.ArticleUnavailable as exc:
        raise x_post.PostUnavailable(f'旧行来源未核实:{exc}') from exc
    x_post._check(deadline, should_stop)
    if article is not None:
        article_body = article['body']
        if article_body and (row['body'] == article_body or row['body'].endswith('\n\n' + article_body)):
            return x_post_meta.stamp(row, 'article', ''), None
        raise x_post.PostUnavailable('远端存在长文，但存量正文不能与整篇文章精确对齐；来源仍未核实')
    detail = x_post.fetch(row['url'], session, deadline=deadline, should_stop=should_stop)
    if _covers(row['body'], detail, aligned_preview):
        return x_post_meta.stamp(row, 'post', '', author_text=x_post_meta.AuthorText(detail, 'detail')), detail
    head, separator, tail = row['body'].partition('\n\n')
    if separator and head and all(line.startswith('> ') for line in head.splitlines()) and (
            _covers(tail, detail, aligned_preview)):
        return x_post_meta.stamp(row, 'post', head + separator), detail
    raise x_post.PostUnavailable('详情不能覆盖存量正文，无法核实旧行来源；不会仅凭标题放行')


def save(seen: dict, annotated: dict) -> dict | None:
    """CAS only the proven metadata; a metadata change also retires older snapshots."""
    with reg.db() as conn:
        changed = conn.execute('UPDATE feed_items SET x_post_meta=?,content_rev=content_rev+1'
                               ' WHERE key=? AND content_hash=? AND content_rev=? AND x_post_meta=?',
                               (annotated[x_post_meta.FIELD], seen['key'], seen['content_hash'],
                                seen['content_rev'], seen[x_post_meta.FIELD])).rowcount
    return dict(annotated, content_rev=seen['content_rev'] + 1) if changed else None
