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
            if not any(source['enabled'] and not reg.configuration_error(source, s)
                       for source in reg.subscriptions(kind)):
                continue
            due.append((s['next_run'],kind,s))
    for _,kind,s in sorted(due):
        # extract 跟着跑:要不要买提取由订阅级开关回答(默认关,见
        # `kb.extract.pending_docs`),没开过的库在这一步是「没有待提取的文档」。
        if start_pipeline(['crawl','export','index','extract'],
                          crawl_filter={'channel':kind, 'scheduled':True}):
            reg.save_settings(kind, {'next_run':now + s['interval_minutes'] * 60})
            return kind
    return None
