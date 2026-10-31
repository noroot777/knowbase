"""feedgrab is an opaque timeline scanner, not a replayable native-cursor interface."""
import json
import time

from kb.feeds import x_article, x_channels, x_feedgrab
from kb.feeds import x_intake_store as store
from kb.feeds.x_intake_metrics import seconds
from kb.youtube.faults import local_fault


def run(intake):
    intake.metrics.data['cursor'] = 'feedgrab_bounded_rescan'
    intake.metrics.data['timeline_phases_separable'] = False
    with intake.metrics.phase('discovery'):
        deadline = min(intake.metrics.deadline - 5, time.monotonic() + seconds('KB_X_DISCOVERY_SECONDS', 240))
        sources = sorted(intake.sources, key=lambda source: (store.progress(source).get('scan_at', 0), source['id']))
        for source in sources:
            if intake.over(deadline):
                break
            handle = x_channels._handle(source)
            intake.attempted.add(source['id'])
            intake.announce(f'feedgrab 五天有界扫描（上游未拆分页） · @{handle}')
            try:
                store.attempt(source, 'discovery')
                result = x_feedgrab.harvest(
                    [(handle, intake.bounds[source['id']].isoformat())],
                    binary=intake.settings.get('x_feedgrab_bin', ''),
                    data_dir=str(x_channels.FEEDGRAB_HOME / 'sessions'),
                    workdir=str(x_channels.FEEDGRAB_HOME / 'work'), budget=deadline - time.monotonic(),
                    should_stop=intake.should_stop, complete_articles=False,
                    account_seconds=seconds('KB_X_FEEDGRAB_ACCOUNT_SECONDS', 25))[handle]
                if result.get('error'):
                    intake.results[handle] = result
                    continue
                reused = store.stage(source, result['entries'], phase='discovery',
                                     incomplete='等待上游批次收尾确认')
                intake.metrics.count('reused', reused or 0)
                intake.metrics.count('discovered', len(result['entries']))
                observed = max((store.utc(entry['published']).isoformat() for entry in result['entries']
                                if store.utc(entry.get('published'))), default='')
                intake.scans.append((source, 'discovery', intake.bounds[source['id']], observed, result))
            except Exception as error:
                if not local_fault(error):
                    raise
                intake.local_error(source, error, '发现')
    intake.metrics.data['phases']['history'] = dict(wall_seconds=0, shared_with='discovery')
    with intake.metrics.phase('body'):
        deadline = min(intake.metrics.deadline - 5, time.monotonic() + seconds('KB_X_BODY_SECONDS', 120))
        for source, task in store.pending(intake.sources):
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
