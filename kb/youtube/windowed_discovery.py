import json
import re
import time
from datetime import datetime, timezone

from kb.youtube import discovery_pages, login, media
from kb.youtube.settings import of
from kb.youtube.ranking import stamp


def lightweight(output, source):
    from kb.youtube.discovery import local_identity
    entries = []
    floor = stamp(source.get('discovery_after'))
    known = set(source.get('known_video_ids', ())) | set(source.get('checked_old_ids', ()))
    now = datetime.now(timezone.utc)
    for line in output.read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.startswith('{'):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get('_type') not in ('url', 'url_transparent'):
            continue
        video_id = str(payload.get('id') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id) or video_id in known:
            continue
        hint = discovery_pages.timestamp(payload)
        if hint is not None and floor is not None and hint + discovery_pages.DATE_MARGIN < floor:
            continue
        entries.append({'video_id': video_id, 'channel_id': str(payload.get('channel_id') or ''),
                        'url': 'https://www.youtube.com/watch?v=' + video_id,
                        'title': str(payload.get('title') or video_id),
                        'published': (hint or now).isoformat(), 'description': '', 'views': 0,
                        'duration': payload.get('duration'), 'approximate': True,
                        'listing_position': len(entries)})
        entries[-1]['channel_id'] = entries[-1]['channel_id'] or local_identity(source)
        known.add(video_id)
    return entries


def video_error(line):
    matched = re.match(r'ERROR: \[youtube\] ([A-Za-z0-9_-]{11}): (.+)', line)
    if matched:
        return {'video_id': matched[1], 'reason': matched[2], 'code': login.classify(matched[2])}
    return None


def benign(item):
    if item is None or item['code'] in login.AUTH_CODES:
        return False
    reason = item['reason'].strip().rstrip('.!').lower()
    return item['code'] == 'no_access' or reason in ('video unavailable', 'this video is unavailable') or any(
        reason.startswith(mark) for mark in ('this video has been removed', 'this video has been deleted',
                                             'the uploader has not made this video available in your country',
                                             'this video is available to music premium members only'))


def unavailable(text):
    items = [video_error(line) for line in text.splitlines()]
    return [{'video_id': item['video_id'], 'reason': item['reason']}
            for item in items if benign(item)]


def diagnostics(text):
    return '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('{'))


def blocked(text):
    for line in text.splitlines():
        if 'ERROR:' not in line:
            continue
        item = video_error(line)
        if not benign(item):
            return True
    return False


def stop_reason(text):
    text = diagnostics(text)
    if blocked(text):
        return ''
    if re.search(r'^\[download\] Finished downloading playlist:', text, re.M):
        return 'playlist'
    if re.search(r'^\[download\] .*does not pass filter', text, re.M):
        return 'window'
    return ''


def completed(output, returncode):
    try:
        reason = stop_reason(output.read_text(encoding='utf-8', errors='replace'))
        return reason == ('window' if returncode == 101 else 'playlist')
    except OSError:
        return False


def parsed(output, url, undated=None, source=None):
    text = output.read_text(encoding='utf-8', errors='replace')
    noise = diagnostics(text)
    archived = re.search(r'^\[download\] .*has already been recorded in the archive', noise, re.M)
    complete = not blocked(noise) and bool(archived or stop_reason(noise))
    flat = lightweight(output, source) if source is not None else []
    flat_ids = {entry['video_id'] for entry in flat}
    precise_output = media.unique(output)
    try:
        lines = []
        for line in text.splitlines():
            try:
                payload = json.loads(line) if line.startswith('{') else {}
            except ValueError:
                payload = {}
            if payload.get('_type') not in ('url', 'url_transparent'):
                lines.append(line)
        precise_output.write_text('\n'.join(lines), encoding='utf-8')
        precise = media._detailed(precise_output, url, allow_empty=complete or bool(flat), undated=undated)
    finally:
        media.heed(precise_output)
    return [entry for entry in precise if entry['video_id'] not in flat_ids] + flat


def fetch(source, settings, deadline, notes=None):
    output = media.DATA_DIR / 'youtube' / 'channels' / f"window-{int(source['id'])}.jsonl"
    evidence, entries, page = [], {}, {}
    floor = stamp(source.get('discovery_after'))
    arguments = ['--flat-playlist', '--extractor-args', 'youtubetab:approximate_date',
                 '--lazy-playlist', '--skip-download', '--dump-json', '--no-quiet']
    limit = discovery_pages.FIRST_BATCH
    source['discovery_pages'] = []
    source['discovery_stop'] = 'partial'
    source['discovery_unavailable'] = []

    def collect(output, url):
        text = output.read_text(encoding='utf-8', errors='replace')
        noise = diagnostics(text)
        missing = unavailable(noise)
        page.update(discovery_pages.inspect(text, floor, limit))
        page['blocked'] = noise if blocked(noise) else ''
        page['stop_reason'] = stop_reason(noise)
        found = parsed(output, url, missing, source)
        source['discovery_unavailable'].extend(item for item in missing if item not in source['discovery_unavailable'])
        return found

    while time.monotonic() < deadline:
        page.clear()
        evidence.clear()
        try:
            found = media._collect_listing(
                [*arguments, '--playlist-end', str(limit), media._channel_url(source)], output,
                of(settings)['cookies_browser'], deadline, lambda: False, collect,
                media._channel_url(source), evidence, completed=completed)
        except Exception as error:
            from kb.youtube.faults import local_fault
            if not entries or local_fault(error):
                raise
            evidence.append('近期列表扩展中断，未确认窗口覆盖：' + str(error))
            break
        entries.update((entry['video_id'], entry) for entry in found)
        source['discovery_pages'].append({'limit': limit, 'observed': page['observed']})
        if page['blocked'] and not evidence:
            evidence.append('列表含未确认的错误，不能确认完整覆盖：' + page['blocked'])
        if evidence:
            break
        if page['boundary'] or page['stop_reason'] == 'window':
            source['discovery_stop'] = 'window'
            break
        if page['exhausted']:
            source['discovery_stop'] = 'exhausted'
            break
        if page['unknown_tail']:
            evidence.append('近期列表缺少可靠的粗日期，未确认窗口覆盖；已停止扩展，未扫描全部历史')
            break
        if page['observed'] < limit:
            evidence.append('近期列表未给出完成凭据，未确认窗口覆盖')
            break
        limit *= 2
    else:
        evidence.append('频道时间预算已到，未确认近期窗口覆盖')
    if notes is not None:
        notes.extend(evidence)
    return list(entries.values())
