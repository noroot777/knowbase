"""Bounded automatic X ingestion: discover first, rescan gaps, then complete durable jobs."""
from __future__ import annotations

import json
import logging
import time
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kb.feeds import x_account, x_browser, x_cooldown, x_page_state, x_post
from kb.feeds import x_intake_store as store
from kb.feeds import x_intake_blocks as blocks
from kb.feeds import x_intake_capacity as capacity
from kb.feeds.x_cdp import CdpError, XBudgetExceeded
from kb.feeds.x_intake_metrics import Metrics, seconds
from kb.youtube.faults import local_fault

MIN_USEFUL = 2.0
MIN_TIMELINE_SECONDS = 8.0


class Intake:
    def __init__(self, sources, settings, *, budget, on_progress=None, should_stop=None):
        from kb.feeds import registry as reg, x_channels
        self.sources, self.settings = sources, settings
        self.channel = x_channels.channel_of(settings)
        self.callback, self.should_stop = on_progress, should_stop
        self.metrics = Metrics(budget)
        self.metrics.data['window_capacity'] = capacity.plan(len(sources), self.channel, budget=budget, reader=seconds)
        self.metrics.data['channel'] = 'browser' if self.channel == 'attach' else self.channel
        self.now = datetime.now(timezone.utc)
        self.started = reg.now()
        self.run_id = uuid4().hex
        self.results, self.scans, self.attempted, self.local = {}, [], set(), {}
        self.outcomes = {}
        self.read = set()
        self.timelines = {}
        self.recovery = set()
        self.notices = {}
        self.observations, self.receipts = {}, {}
        self.blocked, self.cool = '', None
        self.profile = str(x_channels.BROWSER_PROFILE)
        self.bounds = {source['id']: store.floor_for(source, self.now) for source in sources}

    def stop(self):
        return bool(self.should_stop and self.should_stop())

    def over(self, deadline):
        return self.stop() or time.monotonic() + MIN_USEFUL >= deadline

    def budgets(self, reader=None, *, phase='discovery'):
        value = capacity.plan(len(self.sources), self.channel, budget=self.metrics.data['budget_seconds'],
                              remaining=self.metrics.network_deadline - 5 - time.monotonic(), reader=reader or seconds,
                              discovery_done=phase != 'discovery', history_done=phase == 'body',
                              body_pending=sum(1 for _ in self.pending_jobs()) if phase == 'body' else None)
        if phase == 'discovery':
            self.metrics.data['window_capacity'] = value
        self.metrics.data.setdefault('phase_allocations', {})[phase] = value
        return value['phase_seconds']

    def announce(self, text):
        if self.callback:
            self.callback(text)

    def local_error(self, source, error, phase):
        reason = f'本机{phase}失败，待办保留，未计入账号连接失败：{error}'[:500]
        self.local[source['id']] = reason
        self.metrics.data['local_errors'].append(dict(source_id=source['id'], phase=phase, reason=reason))

    def progress(self, source, phase):
        try:
            return store.progress(source)
        except Exception as error:
            if not local_fault(error):
                raise
            self.local_error(source, error, phase)
            return None

    def ordered(self, phase):
        field = 'scan_at' if phase == 'discovery' else 'history_at'
        ranked = []
        for source in self.sources:
            if source['id'] in self.local:
                continue
            progress = self.progress(source, phase)
            if progress is not None:
                ranked.append((source['id'] not in self.recovery if phase == 'history' else False,
                               progress.get(field, 0), source['id'], source))
        return [item[-1] for item in sorted(ranked, key=lambda item: item[:3])]

    def pending_jobs(self):
        groups, seen = [], set()
        for source in self.sources:
            if source['id'] in self.local:
                continue
            try:
                groups.append(store.pending([source]))
            except Exception as error:
                if not local_fault(error):
                    raise
                self.local_error(source, error, '读取正文待办')
        while any(groups):
            for group in groups:
                if group:
                    source, task = group.pop(0)
                    if task['post_id'] not in seen:
                        seen.add(task['post_id'])
                        yield source, task

    def attempt(self, source, phase, reason=''):
        try:
            store.attempt(source, phase, reason)
            return True
        except Exception as error:
            if not local_fault(error):
                raise
            self.local_error(source, error, phase)
            return False

    def mark_block(self, source, error, phase):
        if block := blocks.describe(error, phase):
            self.observations.setdefault(source['id'], {})[phase] = block
            try:
                store.set_block(source, block)
            except Exception as fault:
                if not local_fault(fault):
                    raise
                logging.getLogger(__name__).warning('X 阻断持久化失败，观测随本轮结果交付：%s', fault)

    def block_events(self, source, progress):
        return blocks.merge(progress.get('block_events', {}), self.observations.get(source['id'], {}))

    def outcome(self, source, phase, *, error='', skipped=''):
        self.outcomes.setdefault(source['id'], {})[phase] = dict(error=error, skipped=skipped)

    def screen_budget(self, source, phase, error):
        try:
            count = store.screen_wait(source, phase, self.run_id)
        except Exception as fault:
            if not local_fault(fault):
                raise
            self.local_error(source, fault, phase)
            return
        self.attempt(source, phase, str(error))
        reason = ''
        if count >= 3:
            from kb.feeds import x_channels
            reason = f'@{x_channels._handle(source)} 连续{count}轮首屏未确认，未能判出原因；请检查账号或页面结构：{error}'
        self.outcome(source, phase, error=reason, skipped=str(error))

    def wait(self, deadline, phase):
        remaining = self.cool.remaining()
        if self.stop() or time.monotonic() + remaining + MIN_USEFUL >= deadline:
            return False
        start = time.monotonic()
        self.cool.wait(deadline=deadline, should_stop=self.should_stop, announce=self.announce)
        block = self.metrics.data['phases'].setdefault(phase, {})
        block['cooldown_seconds'] = block.get('cooldown_seconds', 0) + time.monotonic() - start
        return not self.over(deadline)

    def scan(self, page, phase, deadline):
        from kb.feeds import x_channels
        queue = self.ordered(phase)
        retried = set()
        while queue and not self.blocked and not self.over(deadline):
            source = queue.pop(0)
            if source['id'] in self.local or not self.wait(deadline, phase):
                continue
            handle = x_channels._handle(source)
            if self.outcomes.get(source['id'], {}).get(phase, {}).get('error'):
                continue
            bound = self.bounds[source['id']]
            prior = self.progress(source, phase)
            if prior is None:
                continue
            if phase == 'history' and source['id'] not in self.recovery and prior.get('covered') and store.utc(prior['covered']) <= bound:
                fresh = [scan for scan in self.scans if scan[0]['id'] == source['id'] and scan[1] == 'discovery']
                if (fresh and not fresh[-1][4].get('incomplete') and prior.get('coverage_end') and
                        fresh[-1][2] <= store.utc(prior['coverage_end'])):
                    continue
            if phase == 'discovery':
                marker = store.utc(x_channels.watermark(source, self.channel))
                bound = max(bound, marker if marker and marker >= bound else self.now - timedelta(days=1))
                bound = min(bound, self.now)
            quota_seconds = capacity.account_seconds(self.channel, phase, reader=seconds)
            slice_seconds = quota_seconds
            minimum = min(MIN_TIMELINE_SECONDS, slice_seconds)
            remaining = deadline - time.monotonic()
            if remaining < minimum:
                break
            if phase == 'discovery':
                slice_seconds = min(slice_seconds, max(minimum, remaining / (len(queue) + 1)))
            allocated = time.monotonic()
            end = min(deadline, allocated + slice_seconds)
            first_ready = False
            receipt = blocks.snapshot(self.block_events(source, prior), phase)
            page._x_deadline = end
            self.announce(f'{"时间线发现" if phase == "discovery" else "五天历史追补"} · @{handle}')
            self.attempted.add(source['id'])
            hits = self.cool.hits
            try:
                with self.metrics.storage(phase):
                    if not self.attempt(source, phase):
                        continue
                block = self.metrics.data['phases'].setdefault(phase, {})
                def ready(elapsed):
                    nonlocal first_ready
                    first_ready = True
                    block['page_ready_seconds'] = block.get('page_ready_seconds', 0) + elapsed
                entries, missing = x_browser._harvest_one(
                    page, handle, bound - timedelta(microseconds=1), deadline=end,
                    max_scrolls=2 if phase == 'discovery' else x_browser.MAX_SCROLLS,
                    should_stop=self.should_stop, on_ready=ready, precise=True,
                    account_seconds=quota_seconds, budget_limited=end < allocated + quota_seconds)
                observed = store.observed(entries)
                if skipped := getattr(entries, 'skipped_quotes', 0):
                    self.notices[source['id']] = f'跳过 {skipped} 条本人无正文的引用推文，未冒充本人观点入库'
                result = dict(entries=entries, incomplete=missing, error='', coverage_complete=missing == '')
                if timeline := getattr(page, '_x_timeline', None):
                    timeline.finalize(result, bound.isoformat())
                    self.timelines.setdefault(source['id'], []).append((phase, timeline))
                batch_id = uuid4().hex
                with self.metrics.storage(phase):
                    reused = store.stage(source, entries, phase=phase,
                                         incomplete=missing or '等待会话收尾确认', observed=observed,
                                         boundary=bound.isoformat(), end=self.now.isoformat(), batch_id=batch_id,
                                         block_tokens=receipt)
                if reused is None:
                    continue
                self.metrics.count('reused', reused)
                self.metrics.count('discovered', len(entries))
                self.metrics.count(f'{phase}_candidates', len(entries))
                self.scans.append((source, phase, bound, observed, result, batch_id))
                self.receipts[batch_id] = receipt
                self.read.add(source['id'])
                if hits == self.cool.hits:
                    self.cool.cleared()
            except x_page_state.XSessionThrottled as error:
                self.cool.throttled()
                self.mark_block(source, error, phase)
                self.attempt(source, phase, str(error))
                if source['id'] not in retried:
                    queue.append(source)
                    retried.add(source['id'])
            except (x_page_state.XRateLimited, x_page_state.XAccessBlocked, x_post.PostSessionUnavailable) as error:
                self.blocked = str(error)
                self.mark_block(source, error, phase)
                self.outcome(source, phase, skipped=self.blocked)
            except x_account.XAccountUnavailable as error:
                self.mark_block(source, error, phase)
                if error.state == 'login':
                    self.blocked = str(error)
                self.outcome(source, phase, error=str(error), skipped=str(error))
            except Exception as error:
                if local_fault(error):
                    self.local_error(source, error, phase)
                elif isinstance(error, XBudgetExceeded):
                    if first_ready:
                        self.attempt(source, phase, str(error))
                    else:
                        self.screen_budget(source, phase, error)
                elif isinstance(error, (x_page_state.XBrowserError, CdpError)):
                    self.attempt(source, phase, str(error))
                    self.outcome(source, phase, error=str(error))
                else:
                    raise

    def bodies(self, page, deadline):
        for source, task in self.pending_jobs():
            if self.blocked or self.over(deadline):
                break
            if source['id'] in self.local:
                continue
            if not self.wait(deadline, 'body'):
                break
            entry = json.loads(task['entry'])
            page._x_deadline = min(deadline, time.monotonic() + seconds('KB_X_BODY_ITEM_SECONDS', 25))
            unavailable = []
            progress = self.progress(source, '正文阻断凭据')
            if progress is None:
                continue
            receipt = blocks.snapshot(self.block_events(source, progress), 'body')
            self.announce(f'正文补全 · {entry["url"]}')
            try:
                with store.read_db() as conn:
                    blocked, cached = store.excluded(conn, entry), store.existing(conn, entry)
                if blocked or cached:
                    entries, note = [cached] if cached else [], '永久排除' if blocked else ''
                    self.metrics.count('reused', bool(cached))
                else:
                    self.metrics.count('body_attempted')
                    entries, note = x_post.complete([entry], session=page, deadline=page._x_deadline,
                                                    should_stop=self.should_stop, unavailable=unavailable,
                                                    on_block=lambda error: self.mark_block(source, error, 'body'))
                if not entries and not unavailable and (self.stop() or self.over(page._x_deadline) or self.cool.remaining()):
                    continue
                with self.metrics.storage('body'):
                    completed = store.finish(source, task, entries, unavailable, note, block_tokens=receipt)
                if completed:
                    self.observations[source['id']] = blocks.clear(self.observations.get(source['id'], {}), receipt)
                self.metrics.count('body_completed', sum(store.complete(entry) for entry in entries))
            except XBudgetExceeded:
                continue
            except x_page_state.XSessionThrottled as error:
                self.cool.throttled()
                self.mark_block(source, error, 'body')
            except (x_page_state.XRateLimited, x_page_state.XAccessBlocked, x_post.PostSessionUnavailable) as error:
                self.blocked = str(error)
                self.mark_block(source, error, 'body')
            except Exception as error:
                if local_fault(error):
                    self.local_error(source, error, '正文')
                else:
                    raise

    def browser(self):
        started = time.monotonic()
        if self.channel != 'attach':
            self.announce(x_browser.release_login_window(self.profile) or '准备 X 浏览器会话')
        session = x_browser.browser_session(self.profile, self.settings.get('x_chrome_path', ''),
                                             attach=self.channel == 'attach')
        session._x_deadline = self.metrics.network_deadline - 5
        with session as page:
            page._x_deadline = self.metrics.network_deadline - 5
            page.open_page('about:blank')
            self.cool = x_cooldown.of(page)
            self.metrics.data['phases']['startup'] = dict(wall_seconds=time.monotonic() - started)
            for phase in ('discovery', 'history', 'body'):
                limits = self.budgets(phase=phase)
                deadline = min(self.metrics.network_deadline - 5, time.monotonic() + limits[phase])
                if self.blocked or self.over(deadline):
                    continue
                with self.metrics.phase(phase):
                    if phase == 'body':
                        self.bodies(page, deadline)
                    else:
                        self.scan(page, phase, deadline)
            page._x_deadline = self.metrics.deadline
            if timeline := getattr(page, '_x_timeline', None):
                timeline.before_navigation()
            started = time.monotonic()
        self.metrics.data['phases']['cleanup'] = dict(wall_seconds=time.monotonic() - started)

    def package(self):
        from kb.feeds import x_channels
        for source in self.sources:
            for phase, timeline in self.timelines.get(source['id'], []):
                if error := getattr(timeline, 'blocker', None):
                    self.mark_block(source, error, phase)
        confirmed, usable, checkpoints = set(), set(), {}
        for source, phase, bound, observed, result, batch_id in self.scans:
            if result.get('error'):
                continue
            if source['id'] not in self.local:
                try:
                    if store.confirm(source, result['entries'], phase=phase,
                                     incomplete=result.get('incomplete'), observed=observed,
                                     boundary=bound.isoformat(), end=self.now.isoformat(),
                                     coverage_complete=result.get('coverage_complete') is True, batch_id=batch_id):
                        confirmed.add(source['id'])
                        if phase in ('discovery', 'combined') and observed and result.get('coverage_complete') is True:
                            checkpoints[source['id']] = observed
                        if result['entries'] or result.get('coverage_complete') is True:
                            usable.add(source['id'])
                            self.observations[source['id']] = blocks.clear(
                                self.observations.get(source['id'], {}), self.receipts.get(batch_id, {}))
                except Exception as error:
                    if not local_fault(error):
                        raise
                    self.local_error(source, error, '确认进度')
        for source in self.sources:
            handle = x_channels._handle(source)
            outcomes = self.outcomes.get(source['id'], {})
            phase_errors = {phase: value['error'] for phase, value in outcomes.items() if value.get('error')}
            source_error = next((reason for phase, reason in phase_errors.items()
                                 if 'discovery' in blocks.phases(phase)), '')
            labels = dict(discovery='最新发现', history='五天历史追补', combined='时间线扫描', body='正文补全')
            phase_notes = [f'{labels.get(phase, phase)}：{value["error"] or value["skipped"]}'
                           for phase, value in outcomes.items() if value['error'] or value['skipped']]
            try:
                entries, pending, unavailable, expired = store.delivery(source)
                progress = store.progress(source)
            except Exception as error:
                if not local_fault(error):
                    raise
                self.local_error(source, error, '读取待办')
                entries, pending, unavailable, expired, progress = [], 0, [], 0, {}
            read = source['id'] in self.read & confirmed
            reason = progress.get('error', '')
            blocks.project(progress, self.block_events(source, progress))
            core_error = next((timeline.incomplete for phase, timeline in self.timelines.get(source['id'], [])
                               if phase == 'discovery' and getattr(timeline, 'incomplete', '')), '')
            if core_error:
                usable.discard(source['id'])
            notes = [progress.get('gap', ''), f'{pending} 条正文待补' if pending else '',
                     f"{progress['unconfirmed']} 条候选待确认" if progress.get('unconfirmed') else '',
                     self.local.get(source['id'], ''), reason, core_error, *phase_notes]
            if not read:
                notes.append('本轮时间线未读到，保留原有成功和失败账')
            notice = '；'.join(filter(None, [progress.get('outside', ''), self.notices.get(source['id'], ''),
                                             x_post.unavailable_note(unavailable)]))
            result = dict(entries=entries, error=source_error, phase_errors=phase_errors,
                          incomplete='；'.join(dict.fromkeys(filter(None, notes))),
                          note=notice, unavailable=unavailable, unavailable_count=len(unavailable), x_intake=True,
                          local_error=self.local.get(source['id'], ''),
                          pending=not read or source['id'] not in usable or bool(progress.get('unconfirmed') or self.local.get(source['id'])
                                  or progress.get('blocks', {}).get('discovery') or self.stop()), deadline=self.metrics.deadline)
            result.update(run_started=self.started, run_monotonic=self.metrics.started)
            if not read and not entries and not source_error and source['id'] not in self.local:
                result['skipped'] = reason or '；'.join(phase_notes) or ('已按停止请求中断' if self.stop() else
                                     '本轮未轮到，待下轮' if source['id'] not in self.attempted else '本轮未读到，待下轮')
            if not result['pending'] and not source_error and not core_error and source['id'] in checkpoints:
                checkpoint = checkpoints[source['id']]
                result['watermark'] = (store.utc(checkpoint).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
                                       if self.channel == 'feedgrab' else checkpoint)
            self.results[handle] = result
            self.metrics.count('pending', pending)
            self.metrics.count('unconfirmed', progress.get('unconfirmed', 0))
            self.metrics.count('expired', expired)
            self.metrics.count('gaps', bool(progress.get('gap')))
        self.metrics.count('accounts_attempted', len(self.attempted))
        self.metrics.count('accounts_unattempted', len(self.sources) - len(self.attempted))
        return self.results

    def run(self):
        with self.metrics.phase('prepare'):
            for source in self.sources:
                try:
                    store.prepare([source], 'browser' if self.channel == 'attach' else self.channel, self.now)
                    if (progress := self.progress(source, '准备待办')) and progress.get('unconfirmed'):
                        self.recovery.add(source['id'])
                except Exception as error:
                    if not local_fault(error):
                        raise
                    self.local_error(source, error, '准备待办')
        if not self.stop() and not self.over(self.metrics.network_deadline):
            try:
                if self.channel == 'feedgrab':
                    from kb.feeds.x_intake_feedgrab import run
                    run(self)
                elif self.channel == 'guest':
                    from kb.feeds.x_intake_guest import run
                    run(self)
                else:
                    self.browser()
            except Exception as error:
                if local_fault(error):
                    for source in self.sources:
                        self.local_error(source, error, '会话')
                elif isinstance(error, XBudgetExceeded):
                    pass
                elif isinstance(error, (x_page_state.XBrowserError, x_post.PostSessionUnavailable, CdpError, EOFError, OSError)):
                    self.blocked = str(error)
                else:
                    raise
        with self.metrics.phase('finalize'):
            result = self.package()
        from kb.feeds import x_channels
        for source in self.sources:
            if events := self.observations.get(source['id']):
                result[x_channels._handle(source)]['observation'] = dict(
                    source_id=source['id'], identity=store.identity(source), channel=self.metrics.data['channel'], events=events)
        self.metrics.data['blocked'] = self.blocked
        try:
            store.save_metrics(self.started, self.metrics.finish(self.cool))
        except Exception as error:
            logging.getLogger(__name__).warning('X 统计保存失败，不改变来源结论：%s', error)
        return result


def harvest(sources, settings, **kwargs):
    return Intake(sources, settings, **kwargs).run()
