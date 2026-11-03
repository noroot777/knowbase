import json
import re
from datetime import datetime, timedelta, timezone


FIRST_BATCH = 60
OLD_PAGE = 30
DATE_MARGIN = timedelta(days=2)


def timestamp(payload):
    try:
        return datetime.fromtimestamp(payload['timestamp'], timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def inspect(text, floor, limit):
    payloads = []
    for line in text.splitlines():
        if not line.startswith('{'):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and re.fullmatch(r'[A-Za-z0-9_-]{11}', str(payload.get('id') or '')):
            payloads.append(payload)
    dates = [timestamp(payload) for payload in payloads]
    positions = [payload.get('playlist_index') for payload in payloads]
    positions.extend(int(position) for position in re.findall(r'^\[download\] Downloading item (\d+)', text, re.M))
    observed = max([len(payloads), *(position for position in positions if isinstance(position, int))])
    old_page = dates[-OLD_PAGE:]
    boundary = floor is not None and len(old_page) == OLD_PAGE and all(
        published is not None and published + DATE_MARGIN < floor for published in old_page)
    return {'observed': observed, 'boundary': boundary,
            'exhausted': observed < limit and bool(re.search(
                r'^\[download\] Finished downloading playlist:', text, re.M)),
            'unknown_tail': any(published is None for published in dates[-OLD_PAGE:])}
