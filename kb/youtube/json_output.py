import json
import re


def object_payload(output, prompt=''):
    if not isinstance(output, str):
        raise ValueError('model output is not text')
    decoder = json.JSONDecoder()
    candidates, consumed = [], 0
    for matched in re.finditer(r'(?m)^[ \t]*(?=[\{\[])', output):
        start = matched.end()
        if start < consumed:
            continue
        try:
            payload, end = decoder.raw_decode(output, start)
        except ValueError:
            continue
        consumed = end
        tail = output[end:].split('\n', 1)[0].strip()
        if isinstance(payload, dict) and not tail:
            candidates.append((output[start:end], payload))
    if not candidates:
        raise ValueError('model output has no JSON object')
    raw, payload = candidates[-1]
    if raw in prompt:
        raise ValueError('model output repeats a prompt object')
    return payload
