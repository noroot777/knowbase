import json
import re

from kb.youtube.model_response import OutputError


def object_payload(output, prompt=''):
    if not isinstance(output, str):
        raise ValueError('model output is not text')
    decoder = json.JSONDecoder()
    candidates, consumed = [], 0
    for matched in re.finditer(r'[\{\[]', output):
        start = matched.start()
        if start < consumed:
            continue
        try:
            payload, end = decoder.raw_decode(output, start)
        except ValueError:
            continue
        consumed = end
        candidates.append((start, end, payload))
    if not candidates:
        raise ValueError('model output has no JSON object')
    if len(candidates) != 1:
        raise OutputError('模型返回多个 JSON 结果，无法确定唯一成品')
    start, end, payload = candidates[0]
    if not isinstance(payload, dict):
        raise ValueError('model output is not a JSON object')
    context = output[:start] + '\n' + output[end:]
    context = re.sub(r'以上\s*(?:为|是)\s*(?:结果|译文|笔记)', '', context)
    if re.search(r'我会|我将|的格式|示例|模板|草稿|备选|以上|正式版|'
                 r'\b(?:will|would|example|template|draft|alternative)\b', context, re.I):
        raise ValueError('model output labels an example, draft or promised result')
    raw = output[start:end]
    if raw in prompt:
        raise ValueError('model output repeats a prompt object')
    return payload
