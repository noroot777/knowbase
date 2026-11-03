"""Combined-phase intake for channels whose upstream can't split discovery from history.

feedgrab and the no-login channel both read one timeline from the top down to the five-day floor in
a single pass, so the whole scan is staged and confirmed as `combined`, then articles are completed
from the durable body queue. Only the per-account read differs; it is passed in as `read`.
"""
import json
import time
from uuid import uuid4

from kb.feeds import x_article, x_channels
from kb.feeds import x_intake_store as store
from kb.feeds import x_intake_capacity as capacity
from kb.feeds.x_intake_metrics import seconds
from kb.youtube.faults import local_fault


def run(intake, *, cursor, announce, read):
    """`read(handle, floor_iso, deadline, account_seconds)` returns one feedgrab-shaped result."""
    intake.metrics.data['cursor'] = cursor
    intake.metrics.data['timeline_phases_separable'] = False
    limits = intake.budgets(reader=seconds)
    with intake.metrics.phase('discovery'):
        deadline = min(intake.metrics.network_deadline - 5, time.monotonic() + limits['discovery'])
        sources = intake.ordered('discovery')
        for source in sources:
            if intake.over(deadline):
                break
            handle = x_channels._handle(source)
            intake.attempted.add(source['id'])
            intake.announce(f'{announce} · @{handle}')
            try:
                if not intake.attempt(source, 'combined'):
                    continue
                result = read(handle, intake.bounds[source['id']].isoformat(), deadline,
                              capacity.account_seconds(intake.channel, 'combined', reader=seconds))
                if result.get('error') or result.get('skipped'):
                    intake.outcome(source, 'combined', error=result.get('error', ''), skipped=result.get('skipped', ''))
                    intake.attempt(source, 'combined', result.get('error') or result['skipped'])
                    continue
                result['coverage_complete'] = result.get('incomplete') == ''
                if result.get('note'):
                    intake.notices[source['id']] = result['note']
                batch_id = uuid4().hex
                reused = store.stage(source, result['entries'], phase='combined',
                                     incomplete='等待上游批次收尾确认', batch_id=batch_id,
                                     boundary=intake.bounds[source['id']].isoformat(), end=intake.now.isoformat())
                if reused is None:
                    continue
                intake.metrics.count('reused', reused or 0)
                intake.metrics.count('discovered', len(result['entries']))
                observed = max((store.utc(entry['published']).isoformat() for entry in result['entries']
                                if store.utc(entry.get('published'))), default='')
                intake.scans.append((source, 'combined', intake.bounds[source['id']], observed, result, batch_id))
                intake.read.add(source['id'])
            except Exception as error:
                if not local_fault(error):
                    raise
                intake.local_error(source, error, '发现')
    intake.metrics.data['phases']['history'] = dict(wall_seconds=0, shared_with='discovery')
    with intake.metrics.phase('body'):
        body = intake.budgets(reader=seconds, phase='body')['body']
        deadline = min(intake.metrics.network_deadline - 5, time.monotonic() + body)
        for source, task in intake.pending_jobs():
            if intake.over(deadline):
                break
            if source['id'] in intake.local:
                continue
            try:
                intake.metrics.count('body_attempted')
                entries, note = x_article.fill(
                    [json.loads(task['entry'])], deadline=min(deadline, time.monotonic() + 25),
                    should_stop=intake.should_stop)
                store.finish(source, task, entries, [], note)
                intake.metrics.count('body_completed', sum(store.complete(entry) for entry in entries))
            except Exception as error:
                if not local_fault(error):
                    raise
                intake.local_error(source, error, '正文')
