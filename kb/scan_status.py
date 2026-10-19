"""扫描覆盖范围与删除授权。没有完整、可复核的扫描凭据就不清理存量。"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator

from kb.config import Source


def stat_signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


@dataclass
class ScanScope:
    source: Source
    vault: Path
    directories: dict[Path, tuple[int, ...]] = field(default_factory=dict)
    excluded: set[Path] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    finished: bool = False

    def fail(self, path: Path, error: object) -> None:
        message = f"{path}: {error}"
        if message not in self.errors:
            self.errors.append(message)

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

    def authorizes(self, full: Path) -> bool:
        relative = full.relative_to(self.source.root)
        return (self.source.enabled and self.finished and not self.errors
                and bool(relative.parts)
                and relative.parts[0] not in self.source.skip_dirs
                and not any(full.is_relative_to(path) for path in self.excluded))


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

    def authorize_roots(self, conn: sqlite3.Connection, documents) -> None:
        """来源目录换了实体时,不能拿新建的空壳宣布旧目录里的文档全没了。"""
        for scope in self.scopes:
            if not scope.source.enabled or not scope.finished or scope.errors:
                continue
            root = str(scope.source.root.relative_to(scope.vault))
            device, inode = scope.directories[scope.source.root][:2]
            conn.execute(
                'INSERT OR IGNORE INTO index_scan_roots(root, device, inode) VALUES(?,?,?)',
                (root, device, inode))
            previous = conn.execute(
                'SELECT device, inode FROM index_scan_roots WHERE root=?', (root,)).fetchone()
            if (previous['device'], previous['inode']) == (device, inode):
                continue
            if any(scope.contains(scope.vault / row['path'])
                   and row['path'] not in self.alive_paths for row in documents):
                scope.fail(scope.source.root, '来源根身份变化且仍有未核实存量,不授权清理')
                continue
            conn.execute('UPDATE index_scan_roots SET device=?, inode=? WHERE root=?',
                         (device, inode, root))

    def allows_missing(self, path: str, source: str) -> bool:
        relative = Path(path)
        if (path in self.alive_paths or relative.is_absolute()
                or '..' in relative.parts
                or any(not scope.source.enabled and scope.source.name == source
                       for scope in self.scopes)):
            return False
        matching = [scope for scope in self.scopes
                    if scope.contains(scope.vault / relative)]
        if not matching or not all(scope.authorizes(scope.vault / relative)
                                   for scope in matching):
            return False
        scope = matching[0]
        full = scope.vault / relative
        try:
            full.lstat()
        except FileNotFoundError:
            return True
        except OSError as error:
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
