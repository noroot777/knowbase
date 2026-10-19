import hashlib
from pathlib import Path


def item_key(kind: str, url: str) -> str:
    return hashlib.sha256((kind + ':' + url).encode()).hexdigest()[:32]


def export_path(kind: str, key: str) -> Path:
    return Path('source') / kind / (key + '.md')


def document_key(kind: str, relative: str, url: str) -> str:
    if not relative or not url:
        return ''
    key = Path(relative).stem
    if relative != str(export_path(kind, key)) or key != item_key(kind, url):
        return ''
    return key
