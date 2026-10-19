from pathlib import Path


def export_path(kind: str, key: str) -> Path:
    return Path('source') / kind / (key + '.md')
