import json


def object_payload(output):
    if not isinstance(output, str):
        raise ValueError('model output is not text')
    start, end = output.find('{'), output.rfind('}')
    if start < 0 or end < start:
        raise ValueError('model output has no JSON object')
    payload = json.loads(output[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError('model output is not a JSON object')
    return payload
