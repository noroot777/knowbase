import re

from kb.youtube import login, media
from kb.youtube.settings import of


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


def parsed(output, url):
    text = output.read_text(encoding='utf-8', errors='replace')
    noise = diagnostics(text)
    archived = re.search(r'^\[download\] .*has already been recorded in the archive', noise, re.M)
    if (not any(line.lstrip().startswith('{') for line in text.splitlines()) and
            not blocked(noise) and (archived or stop_reason(noise))):
        return []
    return media._detailed(output, url)


def fetch(source, settings, deadline, notes=None):
    output = media.DATA_DIR / 'youtube' / 'channels' / f"window-{int(source['id'])}.jsonl"
    archive = None
    evidence, blocking = [], ''
    arguments = ['--skip-download', '--dump-json', '--no-quiet']

    def collect(output, url):
        nonlocal blocking
        noise = diagnostics(output.read_text(encoding='utf-8', errors='replace'))
        source['discovery_unavailable'] = unavailable(noise)
        if blocked(noise):
            blocking = noise
        return parsed(output, url)

    try:
        if known := sorted(set(source.get('known_video_ids', ())) | set(source.get('checked_old_ids', ()))):
            archive = media.unique(output.with_suffix('.archive'))
            archive.write_text(''.join(f'youtube {video_id}\n' for video_id in known), encoding='utf-8')
            arguments.extend(['--download-archive', str(archive)])
        arguments.append(media._channel_url(source))
        entries = media._collect_listing(arguments, output, of(settings)['cookies_browser'], deadline,
                                         lambda: False, collect, media._channel_url(source), evidence, completed=completed)
        if blocking and not evidence:
            evidence.append('列表含未确认的错误，不能确认完整覆盖：' + blocking)
        if notes is not None:
            notes.extend(evidence)
        return entries
    finally:
        if archive is not None:
            media.heed(archive)
