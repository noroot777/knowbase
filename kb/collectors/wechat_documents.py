from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from collector.src.utils.article_identity import article_identity
from kb import config, store
from kb.collectors import CollectorError
from kb.scanner import parse_frontmatter, read_md_text


def identity(link: str) -> str:
    try:
        return article_identity(link) if urlparse(link).scheme in ('http', 'https') else ''
    except ValueError:
        return ''


def _path(source: str, relative: str) -> Path:
    vault = config.VAULT_ROOT.resolve()
    root = (vault / 'source' / source).resolve()
    path = (config.VAULT_ROOT / relative).resolve()
    if (Path(relative).is_absolute() or not root.is_relative_to(vault)
            or not path.is_relative_to(root) or path.suffix != '.md'):
        raise CollectorError('导出路径不属于这一公众号来源，未执行清理')
    return path


def document_failure(collector, sources: tuple[str, ...], document, reason: str) -> dict | None:
    document = dict(document)
    source, link = document.get('source'), document.get('url') or ''
    relative = document.get('path') or ''
    if (source not in sources or not identity(link)
            or not relative.startswith(f'source/{source}/')):
        return None
    _path(source, relative)
    data = collector._accounts_raw('articles', 'resolve', '--link', link, timeout=20)
    if not data.get('id') or identity(data.get('link') or '') != identity(link):
        return None
    ref = {'channel': collector.name, 'id': data['id'], 'link': link}
    if data.get('account'):
        ref['account'] = data['account']
    return {'title': document.get('title') or '未命名内容', 'reason': reason,
            'url': link, 'drop': ref}


def cleanup(link: str, sources: tuple[str, ...]) -> None:
    target = identity(link)
    if not target:
        return
    sources = tuple(dict.fromkeys((*sources, *config.SOURCE_GROUPS['gzh'])))
    relatives = set()
    for source in sources:
        root = config.VAULT_ROOT / 'source' / source
        for candidate in root.rglob('*.md'):
            relative = str(candidate.relative_to(config.VAULT_ROOT))
            path = _path(source, relative)
            meta, _ = parse_frontmatter(read_md_text(path))
            if identity(str(meta.get('url') or '')) == target:
                path.unlink()
                relatives.add(relative)
    if config.DB_PATH.is_file():
        conn = store.connect(config.DB_PATH)
        try:
            marks = ','.join('?' for source in sources)
            for row in conn.execute(f'SELECT path,source,url FROM documents WHERE source IN ({marks})',
                                    sources).fetchall():
                if identity(row['url'] or '') == target:
                    _path(row['source'], row['path'])
                    relatives.add(row['path'])
            store.remove_documents(conn, relatives)
            conn.commit()
        finally:
            conn.close()
