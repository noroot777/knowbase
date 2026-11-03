from bisect import bisect_right

from kb.youtube import fulltext, transcripts, translation


def coverage(candidates):
    candidates = sorted(candidates, key=lambda item: (item[1], item[0]))
    ends = [item[1] for item in candidates]
    scores = [(0, 0)]
    choices = []
    for position, (start, _, source, _) in enumerate(candidates):
        previous = bisect_right(ends, start, hi=position)
        score = (scores[previous][0] + sum(not character.isspace() for character in source),
                 scores[previous][1] - 1)
        if score > scores[-1]:
            scores.append(score)
            choices.append(previous)
        else:
            scores.append(scores[-1])
            choices.append(None)
    selected = []
    position = len(candidates)
    while position:
        previous = choices[position - 1]
        if previous is None:
            position -= 1
        else:
            selected.append(candidates[position - 1])
            position = previous
    return list(reversed(selected))


def plan(transcript, blocks, translator, cache):
    from kb.youtube import notes

    text = ' '.join(segment['text'].strip() for segment in transcript['segments']).strip()
    candidates = []
    grids = ((fulltext.STAGE, blocks, fulltext.validate),
             (translation.STAGE, transcripts.blocks(transcript['segments']), notes.translated_output))
    for stage, sources, validate in grids:
        cursor = 0
        for source in sources:
            start = text.index(source, cursor)
            cursor = start + len(source)
            if notes.native_chinese(source):
                value = source
            else:
                try:
                    value = validate(source, cache[notes.signature(stage, translator, source)])
                except (KeyError, RuntimeError, ValueError, TypeError):
                    continue
            candidates.append((start, cursor, source, value))
    if not candidates:
        return [(block, None) for block in blocks]
    result = []

    def append_gap(source):
        for block in transcripts.blocks([{'text': source}], limit=fulltext.MAX_SOURCE_CHARS):
            try:
                value = fulltext.validate(block, cache[notes.signature(fulltext.STAGE, translator, block)])
            except (KeyError, RuntimeError, ValueError, TypeError):
                value = None
            result.append((block, value))

    cursor = 0
    for start, end, source, value in coverage(candidates):
        append_gap(text[cursor:start])
        result.append((source, value))
        cursor = end
    append_gap(text[cursor:])
    return result
