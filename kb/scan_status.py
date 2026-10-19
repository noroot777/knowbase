"""扫描覆盖范围与删除授权。没有完整、可复核的扫描凭据就不清理存量。"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator

from kb.config import Source
from kb.scan_evidence import DocumentEvidence


def stat_signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


@dataclass
class ScanScope:
    source: Source
    vault: Path
    directories: dict[Path, tuple[int, ...]] = field(default_factory=dict)
    excluded: set[Path] = field(default_factory=set)
    failures: list[str] = field(default_factory=list)
    missing_root: str = ''
    uninitialized: bool = False
    finished: bool = False

    def fail(self, path: Path, error: object) -> None:
        message = f"{path}: {error}"
        if message not in self.errors:
            self.failures.append(message)

    @property
    def errors(self) -> list[str]:
        missing = [self.missing_root] if self.missing_root and not self.uninitialized else []
        return self.failures + missing

    def paths(self) -> Iterator[Path]:
        if not self.source.enabled:
            return
        try:
            self.source.root.relative_to(self.vault)
            yield from self._walk(self.source.root)
        except (OSError, ValueError) as error:
            self.fail(self.source.root, error)
        else:
            self.finished = True

    def _walk(self, directory: Path) -> Iterator[Path]:
        try:
            self.directories[directory] = stat_signature(directory.stat())
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
            for entry in children:
                full = Path(entry.path)
                if (directory == self.source.root
                        and entry.name in self.source.skip_dirs):
                    continue
                if entry.is_symlink():
                    self.excluded.add(full)
                elif entry.is_dir(follow_symlinks=False):
                    yield from self._walk(full)
                elif entry.name.endswith('.md') and entry.is_file(follow_symlinks=False):
                    yield full
        except OSError as error:
            if (directory == self.source.root and not self.directories
                    and isinstance(error, FileNotFoundError)):
                self.missing_root = f"{directory}: {error}"
            else:
                self.fail(directory, error)

    def validate(self) -> None:
        for directory, expected in self.directories.items():
            try:
                if stat_signature(directory.stat()) != expected:
                    self.fail(directory, "扫描期间目录发生变化")
            except OSError as error:
                self.fail(directory, error)

    def contains(self, full: Path) -> bool:
        return full.is_relative_to(self.source.root)

    def covers(self, full: Path) -> bool:
        if not self.contains(full):
            return False
        relative = full.relative_to(self.source.root)
        return ((not relative.parts or relative.parts[0] not in self.source.skip_dirs)
                and not any(full.is_relative_to(path) for path in self.excluded))

    def authorizes(self, full: Path) -> bool:
        return (self.source.enabled and self.finished and not self.errors
                and not self.missing_root and full != self.source.root and self.covers(full))


@dataclass
class ScanReport:
    scopes: list[ScanScope] = field(default_factory=list)
    alive_paths: set[str] = field(default_factory=set)
    complete: bool = False
    started: bool = False

    def start(self, sources: list[Source], vault: Path) -> None:
        if self.started:
            raise ValueError("扫描凭据不能复用")
        self.started = True
        vault = Path(os.path.abspath(vault))
        self.scopes = [ScanScope(replace(source, root=Path(os.path.abspath(source.root)),
                                         skip_dirs=frozenset(source.skip_dirs)),
                                 vault) for source in sources]

    def iter_md(self) -> Iterator[tuple[Source, Path]]:
        for scope in self.scopes:
            for full in scope.paths():
                self.alive_paths.add(str(full.relative_to(scope.vault)))
                yield scope.source, full

    def fail(self, source: Source, path: Path, error: object) -> None:
        for scope in self.scopes:
            if scope.source is source:
                scope.fail(path, error)
                return

    def validate(self) -> None:
        if not self.complete:
            raise ValueError("扫描尚未完整消费,不授权删除索引")
        for scope in self.scopes:
            scope.validate()

    def check_history(self, conn: sqlite3.Connection, documents) -> None:
        """已知目录必须连续可核验;从未初始化且无存量的根不算同步失败。"""
        history = {row['path']: (row['device'], row['inode']) for row in conn.execute(
            'SELECT root AS path, device, inode FROM index_scan_roots UNION ALL '
            'SELECT path, device, inode FROM index_scan_directories')}
        for scope in self.scopes:
            known = {scope.vault / path: identity for path, identity in history.items()
                     if scope.contains(scope.vault / path)}
            scope.uninitialized = bool(scope.missing_root and not known and not any(
                row['source'] == scope.source.name or scope.contains(scope.vault / row['path'])
                for row in documents))
            if (not scope.source.enabled or not scope.finished or scope.errors
                    or scope.missing_root):
                continue
            for directory, identity in known.items():
                if not scope.covers(directory):
                    continue
                current = scope.directories.get(directory)
                if current is not None and current[:2] == identity:
                    continue
                if any((scope.vault / row['path']).is_relative_to(directory)
                       and row['path'] not in self.alive_paths for row in documents):
                    reason = ('已知目录不可用' if current is None else
                              '来源根身份变化' if directory == scope.source.root else '目录身份变化')
                    scope.fail(directory, f'{reason}且仍有未核实存量,不授权清理')

    def remember_directories(self, conn: sqlite3.Connection) -> None:
        """仅在全部探测与最终复核之后接纳健康范围的目录身份。"""
        for scope in self.scopes:
            if (not scope.source.enabled or not scope.finished or scope.errors
                    or scope.missing_root):
                continue
            for directory, signature in scope.directories.items():
                path = str(directory.relative_to(scope.vault))
                conn.execute(
                    'INSERT INTO index_scan_directories(path, device, inode) VALUES(?,?,?) '
                    'ON CONFLICT(path) DO UPDATE SET device=excluded.device, inode=excluded.inode',
                    (path, *signature[:2]))
            root = str(scope.source.root.relative_to(scope.vault))
            conn.execute(
                'INSERT INTO index_scan_roots(root, device, inode) VALUES(?,?,?) '
                'ON CONFLICT(root) DO UPDATE SET device=excluded.device, inode=excluded.inode',
                (root, *scope.directories[scope.source.root][:2]))

    def check_document_evidence(self, conn: sqlite3.Connection, documents) -> None:
        """没有与已提交文档对应的完整目录链,就不能以缺失为由删除。"""
        receipts = {row['path']: DocumentEvidence.load(row)
                    for row in conn.execute('SELECT * FROM document_scan_evidence')}
        observed = {str(directory.relative_to(scope.vault)): signature[:2]
                    for scope in self.scopes for directory, signature in scope.directories.items()}
        for row in documents:
            if not self.allows_missing(row['path'], row['source']):
                continue
            evidence = receipts.get(row['path'])
            if evidence is None or not evidence.matches(row):
                reason = '缺少与已提交版本对应的入库目录凭据'
            elif any(observed.get(path) != (device, inode)
                     for path, device, inode in evidence.directories):
                reason = '入库目录身份或覆盖无法核实'
            else:
                continue
            for scope in self.matching_scopes(row['path']):
                scope.fail(scope.vault / row['path'], f'{reason},不授权清理')

    def matching_scopes(self, path: str) -> list[ScanScope]:
        relative = Path(path)
        if relative.is_absolute() or '..' in relative.parts:
            return []
        return [scope for scope in self.scopes if scope.contains(scope.vault / relative)]

    def allows_missing(self, path: str, source: str) -> bool:
        """只读最终范围状态,不做文件探测、不改变任何授权条件。"""
        if (path in self.alive_paths
                or any(not scope.source.enabled and scope.source.name == source
                       for scope in self.scopes)):
            return False
        matching = self.matching_scopes(path)
        return bool(matching) and all(scope.authorizes(scope.vault / path) for scope in matching)

    def probe_missing(self, path: str) -> bool:
        """候选探测可使范围失效,结果必须在所有探测结束后重新授权。"""
        matching = self.matching_scopes(path)
        if not matching:
            return False
        full = matching[0].vault / path
        try:
            full.lstat()
        except FileNotFoundError:
            return True
        except OSError as error:
            for scope in matching:
                scope.fail(full, error)
        return False

    @property
    def errors(self) -> list[str]:
        return [message for scope in self.scopes for message in scope.errors]

    @property
    def warning(self) -> str:
        errors = self.errors
        if not errors:
            return ""
        return f"; 扫描不完整,相关存量已保留({len(errors)} 项): " + "; ".join(errors[:3])
