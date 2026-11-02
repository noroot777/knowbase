import ast
import re

_LITERALS = re.compile(r'```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`|https?://[^\s<>`]+')
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
    spans = [matched.span() for matched in _LITERALS.finditer(text)]
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


def contexts(text, blocks):
    spans = literal_spans(text) + [(quoted.start() + 1, quoted.end() - 1) for quoted in _QUOTES.finditer(text)]
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


def chinese_output(text):
    if not any(character.isalpha() for character in text):
        return True
    return any(_HAN.search(word) and
               len(re.findall(_FOREIGN_SCRIPTS.pattern + '+', word)) <= 1
               for word in _WORDS.findall(text))


def valid(value, literals, source):
    remainder = value
    for literal in sorted(literals, key=len, reverse=True):
        remainder = remainder.replace(literal.strip(), '')
    for quoted in _QUOTES.finditer(source):
        content = quoted[0][1:-1]
        for output_quote in _QUOTES.finditer(remainder):
            if output_quote[0][1:-1] == content:
                remainder = remainder.replace(output_quote[0], '')
    if value.split() == source.split() and any(character.isalpha() for character in remainder):
        return False
    if any(len(re.findall(_FOREIGN_SCRIPTS.pattern + '+', sentence)) > 1 and
           _HAN.search(sentence) and sentence.strip() in remainder
           for sentence in re.split(r'[。！？.!?\n]', source) if sentence.strip()):
        return False
    if any(len(_FOREIGN_WORDS.findall(span)) > 10
           for span in re.split(r'[\u4e00-\u9fff、，,；;（）()]', remainder)):
        return False
    return chinese_output(remainder)


def joined(text, blocks, values):
    spans = literal_spans(text) + [(quoted.start() + 1, quoted.end() - 1) for quoted in _QUOTES.finditer(text)]
    cursor, result = 0, ''
    for block, value in zip(blocks, values):
        start = text.index(block, cursor)
        if result:
            shared = any(left < cursor and right > start for left, right in spans)
            result += text[cursor:start] if shared else '\n\n'
        result += value
        cursor = start + len(block)
    return result
