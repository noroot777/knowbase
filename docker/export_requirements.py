"""Export locked dependencies, omitting CUDA packages supplied by non-CPU torch.

torch itself and all other versions remain pinned by uv.lock. uv pip's explicit
--torch-backend cpu selects the corresponding official CPU wheel at build time.
"""

from pathlib import Path
import re
import subprocess
import sys


def without_cuda(requirements: str) -> str:
    return ''.join(line + '\n' for line in requirements.splitlines()
                   if not re.match(r'^(?:cuda[-_]|nvidia[-_]|triton(?:==|\[))', line))


def main():
    root, target = map(Path, sys.argv[1:])
    result = subprocess.run(
        ['uv', 'export', '--directory', str(root), '--frozen', '--no-dev',
         '--no-emit-project', '--no-hashes', '--no-annotate', '--no-header'],
        check=True, capture_output=True, text=True,
    )
    target.write_text(without_cuda(result.stdout))


if __name__ == '__main__':
    main()
