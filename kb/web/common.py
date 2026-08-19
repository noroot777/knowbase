# -*- coding: utf-8 -*-
"""HTTP 层的公共小工具:静态目录、vault 名、时间戳。

各 router 模块共用,单独拎出来避免互相 import 成环。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from kb.config import VAULT_ROOT

_STATIC = Path(__file__).resolve().parent.parent / "static"

# obsidian://open 深链需要 vault 名(obs 目录名)
VAULT_NAME = VAULT_ROOT.name


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
