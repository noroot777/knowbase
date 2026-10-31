import json

from kb.youtube import translation


def encoded(text, value):
    return json.dumps({'translations': [{'id': unit['id'], 'text': value}
                                       for unit in translation.units(text)]}, ensure_ascii=False)


def response(prompt, value):
    segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
    return json.dumps({'translations': [value] * len(segments)}, ensure_ascii=False)
