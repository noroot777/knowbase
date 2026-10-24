"""Standalone launcher copied into the isolated Windows Reader runtime."""

import sys

import rion_wechat_reader as reader


if __name__ == '__main__':
    raise SystemExit(reader.main(sys.argv[1:]))
