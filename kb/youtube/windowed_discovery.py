from datetime import timezone

from kb.youtube import media
from kb.youtube.ranking import stamp
from kb.youtube.settings import of


def stop_reason(text):
    text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('{'))
    if 'ERROR:' in text:
        return ''
    if 'does not pass filter' in text:
        return 'window'
    if 'has already been recorded in the archive' in text:
        return 'archive'
    return ''


def completed(output):
    try:
        return stop_reason(output.read_text(encoding='utf-8', errors='replace')) == 'window'
    except OSError:
        return False


def parsed(output, url):
    text = output.read_text(encoding='utf-8', errors='replace')
    if stop_reason(text) and not any(line.startswith('{') for line in text.splitlines()):
        return []
    return media._detailed(output, url)


def fetch(source, settings, deadline, notes=None):
    floor = stamp(source['discovery_after'])
    day = floor.astimezone(timezone.utc).strftime('%Y%m%d')
    output = media.DATA_DIR / 'youtube' / 'channels' / f"window-{int(source['id'])}.jsonl"
    archive = None
    arguments = ['--skip-download', '--dump-json', '--no-quiet',
                 '--break-match-filters', f'upload_date>=?{day}']
    try:
        if known := source.get('known_video_ids'):
            archive = media.unique(output.with_suffix('.archive'))
            archive.write_text(''.join(f'youtube {video_id}\n' for video_id in known), encoding='utf-8')
            arguments.extend(['--download-archive', str(archive)])
        arguments.append(media._channel_url(source))
        return media._collect_listing(arguments, output, of(settings)['cookies_browser'], deadline,
                                      lambda: False, parsed, media._channel_url(source), notes, completed=completed)
    finally:
        if archive is not None:
            media.heed(archive)
