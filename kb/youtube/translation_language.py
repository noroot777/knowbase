import ast
import re

from kb.feeds.registry import text_language


_LITERALS = re.compile(r'```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`|https?://[^\s<>`]+')
_COMMAND = re.compile(
    r'(?<![\w-])(?:npm\s+(?:install|run|exec|test|ci)\b|npx\s+\S+|'
    r'git\s+(?:clone|rebase|push|pull|checkout|switch|commit|diff|status|add)\b|'
    r'(?:python[\d.]*|pip[\d.]*|curl|wget|docker|node|bash|sh)\s+-\S+|'
    r'export\s+[A-Za-z_]\w*=)[^\n]*')


def literal_spans(text):
    spans = [matched.span() for pattern in (_LITERALS, _COMMAND) for matched in pattern.finditer(text)]
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
    spans, cursor, result = literal_spans(text), 0, []
    for block in blocks:
        start = text.index(block, cursor)
        cursor = start + len(block)
        result.append([text[max(start, left):min(cursor, right)]
                       for left, right in spans if left < cursor and right > start])
    return result


def valid(value, literals):
    remainder = value
    for literal in sorted(literals, key=len, reverse=True):
        remainder = remainder.replace(literal.strip(), '')
    return text_language(remainder) == 'zh'


def joined(text, blocks, values):
    spans, cursor, result = literal_spans(text), 0, ''
    for block, value in zip(blocks, values):
        start = text.index(block, cursor)
        if result:
            shared = any(left < cursor and right > start for left, right in spans)
            result += text[cursor:start] if shared else '\n\n'
        result += value
        cursor = start + len(block)
    return result
