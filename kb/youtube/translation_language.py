import ast
import re

from kb.feeds.registry import text_language

_LITERALS = re.compile(r'```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`|https?://[^\s<>`，,。；;）)"“”「」『』]+|'
                       r'\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[^\s<>`，,。；;）)"“”「」『』]*)?')
_TOKENS = re.compile(r'(?<![A-Za-z0-9_-])(?:--?[A-Za-z][A-Za-z0-9_-]*|'
                     r'[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+)(?![A-Za-z0-9_-])')
_COMMAND = re.compile(
    r'(?<![\w-])(?:npm\s+(?:install|run|exec|test|ci)\b|npx\s+\S+|'
    r'git\s+(?:clone|rebase|push|pull|checkout|switch|commit|diff|status|add)\b|'
    r'(?:python[\d.]*|pip[\d.]*|curl|wget|docker|node|bash|sh)\s+-\S+|'
    r'export\s+[A-Za-z_]\w*=)')
_COMMAND_END = re.compile(r'[.!?。！？](?=\s|$)|[;\r\n]')
_COMMAND_ARG = re.compile(r'''[ \t]*("(?:\\.|[^"\\])*"|'[^']*'|[A-Za-z0-9_@%+=:/.$~*?{}\\-]+)''')
_FOREIGN_WORDS = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")
_FOREIGN_SCRIPTS = re.compile(r'[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\u31f0-\u31ff\uff66-\uff9d'
                             r'\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7ff]')
_QUOTES = re.compile(r'"[^"\n]+"|“[^”]+”|「[^」]+」|『[^』]+』')
_HAN = re.compile(r'[\u4e00-\u9fff]')
_WORDS = re.compile(r'[^\W\d_]+')


def literal_spans(text):
    spans = [matched.span() for pattern in (_LITERALS, _TOKENS) for matched in pattern.finditer(text)]
    for matched in _COMMAND.finditer(text):
        boundary = _COMMAND_END.search(text, matched.end())
        end = boundary.start() if boundary else len(text)
        cursor, operands = matched.end(), 0
        while argument := _COMMAND_ARG.match(text, cursor, end):
            operands += argument[1].isalpha()
            if operands > (2 if matched[0].startswith('git ') else 1):
                break
            cursor = argument.end()
        spans.append((matched.start(), cursor))
    offset = 0
    for line in text.splitlines(keepends=True):
        try:
            statements = ast.parse(line.strip()).body
        except (SyntaxError, ValueError):
            statements = []
        if statements and all(isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign,
                                                       ast.Import, ast.ImportFrom)) or
                              isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                              for statement in statements):
            spans.append((offset, offset + len(line.rstrip())))
        offset += len(line)
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _without(text, fragments):
    for fragment in sorted(fragments, key=len, reverse=True):
        if fragment.strip():
            text = text.replace(fragment.strip(), '', 1)
    return text


def quote_spans(text):
    literals, spans = literal_spans(text), []
    for quoted in _QUOTES.finditer(text):
        start, end = quoted.start() + 1, quoted.end() - 1
        if (not re.search(r'[:：][ \t]*$', text[:quoted.start()]) or end - start > 200 or
                any(left < end and right > start and not start <= left <= right <= end
                    for left, right in literals)):
            continue
        spans.append((start, end))
    narrative = _without(text, [text[start:end] for start, end in literals])
    quoted_letters = sum(character.isalpha() for start, end in spans for character in
                         _without(text[start:end], [text[left:right] for left, right in literals
                                                   if start <= left <= right <= end]))
    return spans if quoted_letters * 3 <= sum(character.isalpha() for character in narrative) else []


def contexts(text, blocks, quoted=False, inherited_quotes=()):
    quotes = set(quote_spans(text))
    cursors = {}
    for fragment in inherited_quotes:
        start = text.find(fragment, cursors.get(fragment, 0))
        if fragment and start >= 0:
            end = start + len(fragment)
            quotes.add((start, end))
            cursors[fragment] = end
    quotes = sorted(quotes)
    spans = quotes if quoted else [(left, right) for left, right in literal_spans(text)
                                   if not any(start <= left <= right <= end for start, end in quotes)]
    cursor, result = 0, []
    for block in blocks:
        start = text.index(block, cursor)
        cursor = start + len(block)
        result.append([text[max(start, left):min(cursor, right)]
                       for left, right in spans if left < cursor and right > start])
    return result


def foreign_script(text):
    for start, end in reversed(literal_spans(text)):
        text = text[:start] + text[end:]
    return bool(_FOREIGN_SCRIPTS.search(text))


def _chinese(text):
    if text_language(text) != 'zh':
        return False
    if not any(character.isalpha() for character in text):
        return True
    return any(_HAN.search(word) and
               len(re.findall(_FOREIGN_SCRIPTS.pattern + '+', word)) <= 1
               for word in _WORDS.findall(text))


def _narrative(value, literals, quotes):
    quote_free = _without(value, quotes)
    return _without(quote_free, literals), quote_free != value


def chinese_output(text, source='', inherited_quotes=(), chinese_source=False):
    literals = contexts(source, [source], inherited_quotes=inherited_quotes)[0] if source else []
    quotes = contexts(source, [source], quoted=True, inherited_quotes=inherited_quotes)[0] if source else []
    return valid(text, literals, source, quotes, chinese_source=chinese_source)


def _term_list(span, source):
    terms = [term.strip(' \t\r\n.：:「」『』“”"') for term in re.split(r'[、，,；;（）()]', span)]
    for term in terms:
        words = _FOREIGN_WORDS.findall(term)
        if not words:
            continue
        pattern = r'(?<![A-Za-z0-9_])' + re.escape(term) + r'(?![A-Za-z0-9_])'
        if (len(words) > 4 or any(len(word) > 64 for word in words) or not re.search(pattern, source) or
                len(words) > 1 and not any(character.isupper() or character.isdigit() for character in term)):
            return False
        source = re.sub(pattern, '', source, count=1)
    return True


def valid(value, literals, source, quotes=None, chinese_source=False):
    if quotes is None:
        quotes = contexts(source, [source], quoted=True)[0] if source else []
    remainder, quoted = _narrative(value, literals, quotes)
    if quoted and not _HAN.search(remainder):
        return False
    if (not chinese_source and value.split() == source.split() and
            any(character.isalpha() for character in remainder) and
            not (_HAN.search(remainder) and not foreign_script(remainder))):
        return False
    if not chinese_source and any(len(re.findall(_FOREIGN_SCRIPTS.pattern + '+', sentence)) > 1 and
                                  _HAN.search(sentence) and sentence.strip() in remainder
                                  for sentence in re.split(r'[。！？.!?\n]', source) if sentence.strip()):
        return False
    if any(len(_FOREIGN_WORDS.findall(span)) > 10 and not _term_list(span, source)
           for span in re.split(r'[\u4e00-\u9fff]', remainder)):
        return False
    return _chinese(remainder)


def joined(text, blocks, values):
    spans = literal_spans(text) + quote_spans(text)
    cursor, result = 0, ''
    for block, value in zip(blocks, values):
        start = text.index(block, cursor)
        if result:
            shared = any(left < cursor and right > start for left, right in spans)
            result += text[cursor:start] if shared else '\n\n'
        result += value
        cursor = start + len(block)
    return result
