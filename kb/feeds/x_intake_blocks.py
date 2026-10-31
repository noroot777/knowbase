"""Typed durable access blockers; a receipt cannot clear a later observation."""
import json
import time
from uuid import uuid4

from kb.feeds import x_account, x_page_state, x_post


def describe(error):
    if isinstance(error, x_page_state.XSessionThrottled):
        kind = 'throttled'
    elif isinstance(error, x_page_state.XRateLimited):
        kind = 'rate'
    elif isinstance(error, x_page_state.XAccessBlocked):
        kind = 'challenge'
    elif isinstance(error, x_account.XAccountUnavailable):
        kind = 'login' if error.state == 'login' else 'account'
    elif isinstance(error, x_post.PostSessionUnavailable):
        kind = 'session'
    else:
        return None
    return dict(kind=kind, reason=str(error), token=uuid4().hex, observed_at=time.time())


def decode(value):
    return json.loads(value) if value else {}


def token(value):
    return decode(value).get('token', '')


def resolve(conn, progress, batch_id, usable):
    if not progress['block'] or not batch_id or not usable:
        return
    batch = conn.execute('SELECT block_token FROM x_intake_batches WHERE id=?', (batch_id,)).fetchone()
    if batch and batch['block_token'] == token(progress['block']):
        conn.execute("UPDATE x_intake_sources SET block='' WHERE source_id=?", (progress['source_id'],))
