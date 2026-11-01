"""Source failure policy shared by failure-only and durable-delivery transactions."""
import time

from kb.feeds import registry as reg

CAP = 1200


def reason(error):
    return str(error)[:CAP] or type(error).__name__


def write(conn, source, error):
    conn.execute('''UPDATE subscriptions SET last_attempt=?,last_error=?,failures=failures+1,
        retry_at=? WHERE id=?''', (reg.now(), reason(error),
        time.time() + min(86400, 1800 * 2 ** min(source['failures'], 6)), source['id']))


def note(summary, source, error):
    if unavailable := getattr(error, 'unavailable', None):
        summary.setdefault('notices', []).append(dict(title=source['name'], reason=error.notice,
                                                      count=len(unavailable), items=unavailable))
    summary['errors'].append(dict(title=source['name'], reason=reason(error), url=source['url']))
    summary['failed_ids'].append(str(source['id']))
