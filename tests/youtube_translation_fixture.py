import json

from kb.youtube import fulltext, translation


def encoded(text, value):
    return json.dumps({'translations': [{'id': unit['id'], 'text': value}
                                       for unit in translation.units(text)]}, ensure_ascii=False)


def source_text(prompt):
    if fulltext.SOURCE_START in prompt:
        return prompt.split(fulltext.SOURCE_START, 1)[1].rsplit('\n</source>', 1)[0]
    return '\n\n'.join(json.loads(prompt.split('\n\n', 1)[1])['segments'])


def response(prompt, value):
    if fulltext.SOURCE_START in prompt:
        return value
    segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
    return json.dumps({'translations': [value] * len(segments)}, ensure_ascii=False)
