"""Small compatibility adapter for the pinned upstream onboarding helper.

Upstream counts decryptable, unclassified auxiliary DBs as unreadable. Preserve
its discovery/verification flow, but distinguish successful reads from failures.
No database contents or key material are printed by this adapter.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import sys


class VerifiedKinds(list):
    def __bool__(self):
        return True  # Successfully read, even when there is no known core schema.


def install_discovery_fix(reader):
    def inspect(db, path):
        try:
            with db.connect(path) as conn:
                tables = {str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            return VerifiedKinds(reader.classify_database_tables(tables))
        except reader.ReaderError:
            return []
    reader.inspect_authorized_database = inspect


def main():
    source = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(source))
    reader = importlib.import_module("rion_wechat_reader")
    install_discovery_fix(reader)
    access = importlib.import_module("rion_wechat_access")
    return access.main(sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
