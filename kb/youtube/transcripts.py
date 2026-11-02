import html
import json
import math
import re


def seconds(value):
    parts = str(value).replace(',', '.').split(':')
    return sum(float(part) * 60 ** position for position, part in enumerate(reversed(parts)))


def clean(text):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', '', text))).strip()


def parse(text, suffix):
    segments = []
    if suffix in ('.json', '.json3'):
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError('字幕 JSON 格式不正确')
        if 'events' in payload:
            for event in payload['events']:
                content = ''.join(segment.get('utf8', '') for segment in event.get('segs', []))
                if content.strip():
                    start = float(event.get('tStartMs', 0)) / 1000
                    segments.append({'start': start,
                                     'end': start + float(event.get('dDurationMs', 1000)) / 1000,
                                     'text': clean(content)})
        else:
            for segment in payload.get('segments', payload.get('transcription', [])):
                offsets = segment.get('offsets', {})
                segments.append({'start': float(segment.get('start', offsets.get('from', 0) / 1000)),
                                 'end': float(segment.get('end', offsets.get('to', 0) / 1000)),
                                 'text': clean(segment.get('text', ''))})
    else:
        for block in re.split(r'\n\s*\n', text.replace('\r\n', '\n')):
            lines = block.splitlines()
            for position, line in enumerate(lines):
                if '-->' not in line:
                    continue
                start, end = line.split('-->', 1)
                try:
                    segments.append({'start': seconds(start.strip()),
                                     'end': seconds(end.strip().split()[0]),
                                     'text': clean(' '.join(lines[position + 1:]))})
                except ValueError:
                    pass
                break
    return deduplicate(segments)


def deduplicate(segments):
    result = []
    for segment in sorted(segments, key=lambda item: (item['start'], item['end'])):
        if not segment['text'] or segment['end'] <= segment['start'] or segment['start'] < 0:
            continue
        if result and result[-1]['start'] == segment['start']:
            if len(segment['text']) >= len(result[-1]['text']):
                result[-1] = segment
            continue
        if result and result[-1]['end'] >= segment['start']:
            previous, current = result[-1]['text'].split(), segment['text'].split()
            if segment['text'] == result[-1]['text']:
                result[-1]['end'] = max(result[-1]['end'], segment['end'])
                continue
            for overlap in range(min(len(previous), len(current)), 2, -1):
                if previous[-overlap:] == current[:overlap]:
                    segment = {**segment, 'text': ' '.join(current[overlap:])}
                    break
        if segment['text']:
            result.append(segment)
    return result


def validate(segments, duration):
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('视频时长不正确')
    if not isinstance(segments, list) or any(not isinstance(item, dict) or
            not isinstance(item.get('text'), str) or any(isinstance(item.get(field), bool) or
            not isinstance(item.get(field), (float, int)) or not math.isfinite(item[field])
            for field in ('start', 'end')) or item['start'] < 0 or item['end'] <= item['start']
            for item in segments):
        raise ValueError('字幕内容或时间戳格式不正确')
    if not segments or sum(char.isalpha() for item in segments for char in item['text']) < 10:
        raise ValueError('字幕为空或没有足够的可读文字')
    if any(item['end'] > duration + 30 for item in segments):
        raise ValueError('字幕时间戳超出视频时长')
    if duration > 180 and max(item['end'] for item in segments) < duration * .75:
        raise ValueError('字幕疑似不完整，不能作为完整内容输入')
    ordered = sorted(segments, key=lambda item: item['start'])
    cursor, covered = 0, 0
    gap_limit = max(15, min(60, duration * .1))
    if ordered[0]['start'] > max(5, min(30, duration * .05)):
        raise ValueError('字幕缺少视频开头，不能确认完整')
    for segment in ordered:
        start, end = segment['start'], min(duration, segment['end'])
        if start - cursor > gap_limit:
            raise ValueError('字幕中间存在异常缺段，不能确认完整')
        covered += max(0, end - max(cursor, start))
        cursor = max(cursor, end)
    if covered < duration * .65:
        raise ValueError('字幕时间覆盖不足，不能确认完整')
    return segments


def blocks(segments, limit=3000):
    if limit < 1:
        raise ValueError('正文分块长度必须为正数')
    text = ' '.join(segment['text'].strip() for segment in segments).strip()
    result = []
    while len(text) > limit:
        head = text[:limit]
        boundaries = [head.rfind(ending) + len(ending)
                      for ending in ('。', '！', '？', '. ', '! ', '? ', '\n\n') if ending in head]
        boundary = max(boundaries, default=0)
        if boundary < max(1, limit // 2):
            boundary = head.rfind(' ') + 1 or limit
        result.append(text[:boundary].strip())
        text = text[boundary:].lstrip()
    if text:
        result.append(text)
    return result
