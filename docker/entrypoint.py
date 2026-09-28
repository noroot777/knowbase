"""Fail early on unwritable mounts and initialize a fresh persistent database."""

import os
from pathlib import Path
import sys
import tempfile


def main():
    from kb.config import DATA_DIR, VAULT_ROOT
    paths = (DATA_DIR, VAULT_ROOT,
             Path(os.environ.get('KB_COLLECTOR_DIR', '/app/collector')) / 'data',
             Path(os.environ.get('HF_HOME', '/home/kb/.cache/huggingface')))
    for path in paths:
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=path):
                pass
        except OSError:
            print(f'Cannot write {path}. Check volume ownership for container UID/GID '
                  f'{os.getuid()}:{os.getgid()}.', file=sys.stderr)
            return 1
    from kb import store
    store.connect().close()
    args = sys.argv[1:] or ['kb-web']
    os.execvp(args[0], args)


if __name__ == '__main__':
    raise SystemExit(main())
