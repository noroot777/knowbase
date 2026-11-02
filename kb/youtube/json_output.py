import json
import re


def object_payload(output, prompt=''):
    if not isinstance(output, str):
        raise ValueError('model output is not text')
    decoder = json.JSONDecoder()
    candidates, wrapped, consumed = [], [], 0
    for matched in re.finditer(r'[\{\[]', output):
        start = matched.start()
        if start < consumed:
            continue
        try:
            payload, end = decoder.raw_decode(output, start)
        except ValueError:
            continue
        consumed = end
        prefix = output[output.rfind('\n', 0, start) + 1:start].strip()
        tail = output[end:].split('\n', 1)[0].strip()
        if not isinstance(payload, dict):
            continue
        candidate = (output[start:end], payload)
        if not prefix and not tail:
            candidates.append(candidate)
        elif not re.search(r'我会|我将|的格式|示例|\b(?:will|would|example|template)\b',
                           prefix + ' ' + tail, re.I):
            wrapped.append(candidate)
    candidates = candidates or wrapped
    if not candidates:
        raise ValueError('model output has no JSON object')
    raw, payload = candidates[-1]
    if raw in prompt:
        raise ValueError('model output repeats a prompt object')
    return payload
