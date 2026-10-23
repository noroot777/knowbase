"""Standalone launcher copied into the isolated Windows Reader runtime."""

import sys

from wechat_access_adapter import install_discovery_fix
import rion_wechat_reader as reader


if __name__ == '__main__':
    install_discovery_fix(reader)
    raise SystemExit(reader.main(sys.argv[1:]))
