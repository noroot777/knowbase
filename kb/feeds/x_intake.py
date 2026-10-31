"""Bounded automatic X ingestion: discover first, rescan gaps, then complete durable jobs."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from kb.feeds import x_account, x_browser, x_cooldown, x_page_state, x_post
from kb.feeds import x_intake_store as store
from kb.feeds.x_cdp import CdpError
from kb.feeds.x_intake_metrics import Metrics, seconds
from kb.youtube.faults import local_fault

MIN_USEFUL = 2.0


class Intake:
    def __init__(self, sources, settings, *, budget, on_progress=None, should_stop=None):
        from kb.feeds import registry as reg, x_channels
        self.sources, self.settings = sources, settings
        self.channel = x_channels.channel_of(settings)
        self.callback, self.should_stop = on_progress, should_stop
        self.metrics = Metrics(budget)
        self.metrics.data['channel'] = 'browser' if self.channel == 'attach' else self.channel
        self.now = datetime.now(timezone.utc)
        self.started = reg.now()
        self.results, self.scans, self.attempted, self.local = {}, [], set(), {}
        self.read = set()
        self.timelines = {}
        self.notices = {}
        self.blocked, self.cool = '', None
        self.profile = str(x_channels.BROWSER_PROFILE)
        self.bounds = {source['id']: store.floor_for(source, self.now) for source in sources}

    def stop(self):
        return bool(self.should_stop and self.should_stop())

    def over(self, deadline):
        return self.stop() or time.monotonic() + MIN_USEFUL >= deadline

    def announce(self, text):
        if self.callback:
            self.callback(text)

    def local_error(self, source, error, phase):
        reason = f'本机{phase}失败，待办保留，未计入账号连接失败：{error}'[:500]
        self.local[source['id']] = reason
        self.metrics.data['local_errors'].append(dict(source_id=source['id'], phase=phase, reason=reason))

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
        field = 'scan_at' if phase == 'discovery' else 'history_at'
        queue = sorted(self.sources, key=lambda source: (store.progress(source).get(field, 0), source['id']))
        retried = set()
        while queue and not self.blocked and not self.over(deadline):
            source = queue.pop(0)
            if source['id'] in self.local or not self.wait(deadline, phase):
                continue
            handle = x_channels._handle(source)
            bound = self.bounds[source['id']]
            prior = store.progress(source)
            if phase == 'history' and prior.get('covered') and store.utc(prior['covered']) <= bound:
                fresh = [scan for scan in self.scans if scan[0]['id'] == source['id'] and scan[1] == 'discovery']
                if (fresh and not fresh[-1][4].get('incomplete') and prior.get('coverage_end') and
                        fresh[-1][2] <= store.utc(prior['coverage_end'])):
                    continue
            if phase == 'discovery':
                bound = max(bound, self.now - timedelta(days=1),
                            store.utc(x_channels.watermark(source, self.channel)) or bound)
                bound = min(bound, self.now)
            slice_seconds = seconds('KB_X_ACCOUNT_SECONDS', 8 if phase == 'discovery' else 20)
            if phase == 'discovery':
                slice_seconds = min(slice_seconds, (deadline - time.monotonic()) / (len(queue) + 1))
            end = min(deadline, time.monotonic() + slice_seconds)
            page._x_deadline = end
            self.announce(f'{"时间线发现" if phase == "discovery" else "五天历史追补"} · @{handle}')
            self.attempted.add(source['id'])
            hits = self.cool.hits
            try:
                with self.metrics.storage(phase):
                    store.attempt(source, phase)
                block = self.metrics.data['phases'].setdefault(phase, {})
                def ready(elapsed):
                    block['page_ready_seconds'] = block.get('page_ready_seconds', 0) + elapsed
                entries, missing = x_browser._harvest_one(
                    page, handle, bound - timedelta(microseconds=1), deadline=end,
                    max_scrolls=2 if phase == 'discovery' else x_browser.MAX_SCROLLS,
                    should_stop=self.should_stop, on_ready=ready, precise=True)
                observed = store.observed(entries)
                if skipped := getattr(entries, 'skipped_quotes', 0):
                    self.notices[source['id']] = f'跳过 {skipped} 条本人无正文的引用推文，未冒充本人观点入库'
                result = dict(entries=entries, incomplete=missing, error='', coverage_complete=missing == '')
                if timeline := getattr(page, '_x_timeline', None):
                    timeline.finalize(result, bound.isoformat())
                    self.timelines.setdefault(source['id'], []).append(timeline)
                with self.metrics.storage(phase):
                    reused = store.stage(source, entries, phase=phase,
                                         incomplete=missing or '等待会话收尾确认', observed=observed)
                if reused is None:
                    continue
                self.metrics.count('reused', reused)
                self.metrics.count('discovered', len(entries))
                self.metrics.count(f'{phase}_candidates', len(entries))
                self.scans.append((source, phase, bound, observed, result))
                self.read.add(source['id'])
                if hits == self.cool.hits:
                    self.cool.cleared()
            except x_page_state.XSessionThrottled as error:
                self.cool.throttled()
                store.attempt(source, phase, str(error))
                if source['id'] not in retried:
                    queue.append(source)
                    retried.add(source['id'])
            except (x_page_state.XRateLimited, x_page_state.XAccessBlocked, x_post.PostSessionUnavailable) as error:
                self.blocked = str(error)
                self.results[handle] = dict(entries=[], error='', skipped=self.blocked)
            except x_account.XAccountUnavailable as error:
                if error.state == 'login':
                    self.blocked = str(error)
                self.results[handle] = dict(entries=[], error=str(error))
            except Exception as error:
                if local_fault(error):
                    self.local_error(source, error, phase)
                elif isinstance(error, (x_page_state.XBrowserError, CdpError)):
                    if self.over(end):
                        store.attempt(source, phase, '本阶段预算用完,尚未读完')
                    else:
                        self.results[handle] = dict(entries=[], error=str(error))
                else:
                    raise

    def bodies(self, page, deadline):
        for source, task in store.pending(self.sources):
            if self.blocked or self.over(deadline):
                break
            if source['id'] in self.local:
                continue
            if not self.wait(deadline, 'body'):
                break
            entry = json.loads(task['entry'])
            page._x_deadline = min(deadline, time.monotonic() + seconds('KB_X_BODY_ITEM_SECONDS', 25))
            unavailable = []
            self.announce(f'正文补全 · {entry["url"]}')
            try:
                with store.db() as conn:
                    blocked, cached = store.excluded(conn, entry), store.existing(conn, entry)
                if blocked or cached:
                    entries, note = [cached] if cached else [], '永久排除' if blocked else ''
                    self.metrics.count('reused', bool(cached))
                else:
                    self.metrics.count('body_attempted')
                    entries, note = x_post.complete([entry], session=page, deadline=page._x_deadline,
                                                    should_stop=self.should_stop, unavailable=unavailable)
                if not entries and not unavailable and (self.stop() or self.over(page._x_deadline) or self.cool.remaining()):
                    continue
                with self.metrics.storage('body'):
                    store.finish(source, task, entries, unavailable, note)
                self.metrics.count('body_completed', sum(store.complete(entry) for entry in entries))
            except (x_page_state.XRateLimited, x_page_state.XAccessBlocked, x_post.PostSessionUnavailable) as error:
                self.blocked = str(error)
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
        session._x_deadline = self.metrics.deadline - 5
        with session as page:
            page._x_deadline = self.metrics.deadline - 5
            page.open_page('about:blank')
            self.cool = x_cooldown.of(page)
            self.metrics.data['phases']['startup'] = dict(wall_seconds=time.monotonic() - started)
            for phase, limit in [('discovery', 240), ('history', 120), ('body', 120)]:
                deadline = min(self.metrics.deadline - 5, time.monotonic() + seconds(f'KB_X_{phase.upper()}_SECONDS', limit))
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
        confirmed, usable = set(), set()
        for source, phase, bound, observed, result in self.scans:
            if result.get('error'):
                self.blocked = self.blocked or result['error']
                continue
            if source['id'] not in self.local:
                try:
                    if store.confirm(source, result['entries'], phase=phase,
                                     incomplete=result.get('incomplete'), observed=observed,
                                     boundary=bound.isoformat(), end=self.now.isoformat(),
                                     coverage_complete=result.get('coverage_complete') is True):
                        confirmed.add(source['id'])
                        if result['entries'] or result.get('coverage_complete') is True:
                            usable.add(source['id'])
                except Exception as error:
                    if not local_fault(error):
                        raise
                    self.local_error(source, error, '确认进度')
        for source in self.sources:
            handle = x_channels._handle(source)
            if self.results.get(handle, {}).get('error'):
                continue
            try:
                entries, pending, unavailable, expired = store.delivery(source)
                progress = store.progress(source)
            except Exception as error:
                if not local_fault(error):
                    raise
                self.local_error(source, error, '读取待办')
                entries, pending, unavailable, expired, progress = [], 0, [], 0, {}
            read = source['id'] in self.read & confirmed
            reason = self.results.get(handle, {}).get('skipped') or progress.get('error', '')
            core_error = next((timeline.incomplete for timeline in self.timelines.get(source['id'], [])
                               if getattr(timeline, 'incomplete', '')), '')
            notes = [progress.get('gap', ''), f'{pending} 条正文待补' if pending else '',
                     self.local.get(source['id'], ''), self.blocked, reason, core_error]
            if not read:
                notes.append('本轮时间线未读到，保留原有成功和失败账')
            notice = f'{expired} 条待办已超出自动五天窗口，未冒充完成' if expired else ''
            notice = '；'.join(filter(None, [notice, progress.get('outside', ''), self.notices.get(source['id'], ''),
                                             x_post.unavailable_note(unavailable)]))
            result = dict(entries=entries, error='', incomplete='；'.join(dict.fromkeys(filter(None, notes))),
                          note=notice, unavailable=unavailable, unavailable_count=len(unavailable), x_intake=True,
                          local_error=self.local.get(source['id'], ''),
                          pending=not read or source['id'] not in usable or bool(pending or self.local.get(source['id'])
                                  or self.blocked or core_error or self.stop()), deadline=self.metrics.deadline)
            result.update(run_started=self.started, run_monotonic=self.metrics.started)
            if not read and not entries and source['id'] not in self.local:
                result['skipped'] = reason or self.blocked or ('已按停止请求中断' if self.stop() else
                                     '本轮未轮到，待下轮' if source['id'] not in self.attempted else '本轮未读到，待下轮')
            if read and not core_error and progress.get('discovery') == 'complete' and progress.get('latest'):
                result['watermark'] = progress['latest']
            self.results[handle] = result
            self.metrics.count('pending', pending)
            self.metrics.count('expired', expired)
            self.metrics.count('gaps', bool(progress.get('gap')))
        self.metrics.count('accounts_attempted', len(self.attempted))
        self.metrics.count('accounts_unattempted', len(self.sources) - len(self.attempted))
        return self.results

    def run(self):
        try:
            with self.metrics.phase('prepare'):
                store.prepare(self.sources, 'browser' if self.channel == 'attach' else self.channel, self.now)
        except Exception as error:
            if not local_fault(error):
                raise
            for source in self.sources:
                self.local_error(source, error, '准备待办')
        if not self.stop() and not self.over(self.metrics.deadline):
            try:
                if self.channel == 'feedgrab':
                    from kb.feeds.x_intake_feedgrab import run
                    run(self)
                else:
                    self.browser()
            except Exception as error:
                if local_fault(error):
                    for source in self.sources:
                        self.local_error(source, error, '会话')
                elif isinstance(error, (x_page_state.XBrowserError, x_post.PostSessionUnavailable, CdpError, EOFError, OSError)):
                    self.blocked = str(error)
                else:
                    raise
        with self.metrics.phase('finalize'):
            result = self.package()
        try:
            store.save_metrics(self.started, self.metrics.finish(self.cool))
        except Exception as error:
            if not local_fault(error):
                raise
            for value in result.values():
                value.update(local_error=f'本机统计保存失败：{error}', pending=True)
                value.pop('skipped', None)
                value['incomplete'] = value.get('incomplete', '') + '；本机统计保存失败，下轮重算'
        return result


def harvest(sources, settings, **kwargs):
    return Intake(sources, settings, **kwargs).run()
