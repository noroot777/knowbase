import json
import re

from kb.youtube.model_response import OutputError


_LABEL = re.compile(
    r'(?:^|[，,:：])\s*(?:(?:以下|以上|这|下面)(?:仅|只)?(?:是|为)\s*)?'
    r'(?:一[个份版段]\s*)?(?:JSON\s*)?(?:格式)?(?:示例|模板|草稿|备选)'
    r'(?:答案|结果|笔记|译文|版本)?(?:如下)?(?=$|[\s:：，,。.!！?？;；])|'
    r'(?:^|[,:])\s*(?:(?:this|the above|below)\s+is\s+(?:(?:only|just)\s+)?(?:an?\s+)?)?'
    r'(?:JSON\s+)?(?:example|sample|template|draft|alternative)(?=$|[\s:.,!?;])', re.I)
_PROMISE = re.compile(
    r'(?:^|[，,:：])\s*(?:我(?:会|将)[^。！？!?;\n]*(?:输出|给出|返回|提供)|'
    r'(?:正式版|答案|译文|笔记|结果)(?:将|会)[^。！？!?;\n]*(?:输出|给出|返回|提供)|'
    r'I\s+(?:will|would)\s+(?:output|return|provide)\b)', re.I)


def labeled_result(prefix, suffix):
    contexts = [re.sub(r'(?:```|~~~)(?:json)?', '', text, flags=re.I).strip(' \t\r\n。.!！?？;；')
                for text in (prefix, suffix)]
    boundary = r'[\r\n。！？!?;；]|\.(?=\s|$)'
    before = re.split(boundary, contexts[0])[-1].strip(' \t*_()（）')
    after = re.split(boundary, contexts[1])[0].strip(' \t*_()（）')
    return bool(any(_LABEL.search(text) for text in (before, after)) or _PROMISE.search(before + ' ' + after))


def object_payload(output, prompt='', required_fields=()):
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
        if isinstance(payload, dict) and all(field in payload for field in required_fields):
            candidates.append((start, end, payload))
    if not candidates:
        raise ValueError('model output has no JSON object')
    if len(candidates) != 1:
        raise OutputError('模型返回多个 JSON 结果，无法确定唯一成品')
    start, end, payload = candidates[0]
    if labeled_result(output[:start], output[end:]):
        raise ValueError('model output labels an example, draft or promised result')
    raw = output[start:end]
    if raw in prompt:
        raise ValueError('model output repeats a prompt object')
    return payload
