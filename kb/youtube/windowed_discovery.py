import re

from kb.youtube import login, media
from kb.youtube.settings import of


def unavailable(text):
    items = []
    pattern = (r'private video|members.only|join this channel|not available in your country|'
               r'not made this video available in your country|'
               r'video (?:is )?(?:unavailable|not available)|has been removed|has been deleted')
    for line in text.splitlines():
        matched = re.match(r'ERROR: \[youtube\] ([A-Za-z0-9_-]{11}): (.+)', line)
        if (matched and login.classify(matched[2]) not in login.AUTH_CODES and
                re.search(pattern, matched[2], re.I)):
            items.append({'video_id': matched[1], 'reason': matched[2]})
    return items


def stop_reason(text):
    text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('{'))
    errors = [line for line in text.splitlines() if 'ERROR:' in line]
    if len(errors) != len(unavailable(text)):
        return ''
    if re.search(r'^\[download\] Finished downloading playlist:', text, re.M):
        return 'playlist'
    if re.search(r'^\[download\] .*does not pass filter', text, re.M):
        return 'window'
    if 'has already been recorded in the archive' in text:
        return 'archive'
    return ''


def completed(output, returncode):
    try:
        reason = stop_reason(output.read_text(encoding='utf-8', errors='replace'))
        return reason == ('window' if returncode == 101 else 'playlist')
    except OSError:
        return False


def parsed(output, url):
    text = output.read_text(encoding='utf-8', errors='replace')
    if stop_reason(text) and not any(line.startswith('{') for line in text.splitlines()):
        return []
    return media._detailed(output, url)


def fetch(source, settings, deadline, notes=None):
    output = media.DATA_DIR / 'youtube' / 'channels' / f"window-{int(source['id'])}.jsonl"
    archive = None
    arguments = ['--skip-download', '--dump-json', '--no-quiet']

    def collect(output, url):
        source['discovery_unavailable'] = unavailable(output.read_text(encoding='utf-8', errors='replace'))
        return parsed(output, url)

    try:
        if known := sorted(set(source.get('known_video_ids', ())) | set(source.get('checked_old_ids', ()))):
            archive = media.unique(output.with_suffix('.archive'))
            archive.write_text(''.join(f'youtube {video_id}\n' for video_id in known), encoding='utf-8')
            arguments.extend(['--download-archive', str(archive)])
        arguments.append(media._channel_url(source))
        return media._collect_listing(arguments, output, of(settings)['cookies_browser'], deadline,
                                      lambda: False, collect, media._channel_url(source), notes, completed=completed)
    finally:
        if archive is not None:
            media.heed(archive)
