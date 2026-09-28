"""Independent schedules sharing the pipeline lock. Busy jobs stay due, never lost."""
import time

from kb.feeds import registry as reg
from kb.source_types import feed_types


def tick(start_pipeline, current_time=None):
    now = time.time() if current_time is None else current_time
    due = []
    for kind in feed_types():
        s = reg.get_settings(kind)
        if s['enabled'] and s['next_run'] <= now:
            due.append((s['next_run'],kind,s))
    for _,kind,s in sorted(due):
        if start_pipeline(['crawl','export','index'], crawl_filter={'channel':kind, 'scheduled':True}):
            reg.save_settings(kind, {'next_run':now + s['interval_minutes'] * 60})
            return kind
    return None
