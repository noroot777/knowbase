"""X-only save outcome integration; partial/local work cannot clear old failure accounts."""
import json
import time
from kb.feeds import registry as reg
from kb.feeds import x_intake_store as store
from kb.youtube.faults import local_fault, record_local


def check_budget(validators):
    from kb.feeds.fetching import CollectionBudgetExceeded
    if validators.get('x_deadline') and time.monotonic() >= validators['x_deadline']:
        raise CollectionBudgetExceeded('X 整轮预算用完，已持久化的成品留待下轮投递')


def filtered(conn, source, entries, validators):
    check_budget(validators)
    if not validators.get('x_intake'):
        return entries
    row = conn.execute('SELECT history_floor FROM subscriptions WHERE id=?', (source['id'],)).fetchone()
    floor = row['history_floor'] if row else ''
    return [entry for entry in entries if not floor or entry.get('published', '')[:10] >= floor[:10]]


def partial(conn, source, entries, validators):
    if not validators.get('x_intake'):
        return False
    if not store.acknowledge(conn, source, entries):
        raise Repointed()
    check_budget(validators)
    if validators.get('x_run_started') and validators.get('x_save_started'):
        row = conn.execute('SELECT * FROM x_intake_runs WHERE started=? ORDER BY id DESC LIMIT 1',
                           (validators['x_run_started'],)).fetchone()
        if row:
            metrics = json.loads(row['metrics'])
            wall = metrics['phases'].setdefault('delivery', {}).get('wall_seconds', 0)
            metrics['phases']['delivery']['wall_seconds'] = wall + time.monotonic() - validators['x_save_started']
            metrics['elapsed_seconds'] = round(time.monotonic() - validators['x_run_monotonic'], 3)
            metrics['overrun_seconds'] = round(max(0, time.monotonic() - validators['x_deadline']), 3)
            conn.execute('UPDATE x_intake_runs SET metrics=? WHERE id=?',
                         (json.dumps(metrics, ensure_ascii=False), row['id']))
    if validators.get('x_pending'):
        conn.execute('UPDATE subscriptions SET last_attempt=?,last_note=?,last_notice=? WHERE id=?',
                     (reg.now(), validators.get('note', ''), validators.get('notice', ''), source['id']))
        return True
    return False


class Repointed(LookupError):
    pass


def saved(summary, source, validators):
    if not validators.get('x_pending'):
        summary['succeeded_ids'].append(str(source['id']))
    else:
        summary['deferred'] += 1
    if reason := validators.get('x_local_error'):
        record_local(summary, RuntimeError(reason), [source['id']], 'X')


def local(source, error, summary):
    from kb.feeds import x_channels
    if source['kind'] != 'social' or not x_channels._handle(source) or source.get('feed_url') or not local_fault(error):
        return False
    record_local(summary, error, [source['id']], 'X读写')
    summary['deferred'] += 1
    return True


def telemetry(kind, summary):
    from kb.feeds import x_channels
    if kind == 'social' and any(value.get('x_intake') for value in x_channels._harvest.values()):
        summary['x_intake'] = store.latest_metrics()
