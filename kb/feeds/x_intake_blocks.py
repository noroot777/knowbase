"""Typed durable access blockers; a receipt cannot clear a later observation."""
import json
import time
from uuid import uuid4

from kb.feeds import x_account, x_page_state, x_post


PHASES = ('discovery', 'history', 'body')


def phases(phase):
    return ('discovery', 'history') if phase == 'combined' else ('history' if phase == 'recovery' else phase,)


def describe(error, phase='discovery'):
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
    return dict(kind=kind, reason=str(error), token=uuid4().hex, observed_at=time.time(), phase=phase)


def decode(value):
    events = json.loads(value) if isinstance(value, str) and value else value or {}
    if events.get('kind'):
        return {events.get('phase', 'discovery'): events}
    return events


def active(value):
    return {phase: event for phase, event in decode(value).items() if event.get('kind')}


def primary(value):
    events = active(value)
    return next((events[phase] for phase in PHASES if phase in events), {})


def merge(current, incoming):
    result = dict(decode(current))
    for phase, event in decode(incoming).items():
        if event.get('observed_at', 0) >= result.get(phase, {}).get('observed_at', 0):
            result[phase] = event
    return result


def snapshot(value, phase):
    events = active(value)
    return {name: events.get(name, {}).get('token', '') for name in phases(phase)}


def clear(value, tokens):
    result = dict(decode(value))
    for phase, token in tokens.items():
        if token and result.get(phase, {}).get('token') == token:
            result[phase] = dict(phase=phase, observed_at=time.time(), cleared_token=token)
    return result


def receipt_tokens(value, phase):
    if value.startswith('{'):
        return {name: token for name, token in json.loads(value).items() if name in phases(phase)}
    return {'discovery': value} if 'discovery' in phases(phase) else {}


def resolve(conn, progress, batch_id, usable, phase):
    if not batch_id or not usable:
        return
    batch = conn.execute('SELECT block_token FROM x_intake_batches WHERE id=?', (batch_id,)).fetchone()
    if batch:
        resolved = clear(progress['block'], receipt_tokens(batch['block_token'], phase))
        conn.execute('UPDATE x_intake_sources SET block=? WHERE source_id=? AND identity=?',
                     (json.dumps(resolved, ensure_ascii=False), progress['source_id'], progress['identity']))


def replay(conn, source_id, identity, channel, value):
    events = decode(value)
    runs = conn.execute("SELECT summary FROM feed_runs WHERE kind='social' AND summary LIKE '%\"x_observations\"%' ORDER BY id")
    for run in runs:
        for observation in json.loads(run['summary']).get('x_observations', []):
            if (observation['source_id'], observation['identity'], observation['channel']) == (source_id, identity, channel):
                events = merge(events, observation['events'])
    return events


def project(state, events):
    state.update(block_events=events, blocks=active(events), block=primary(events))
    return state
