# -*- coding: utf-8 -*-
"""HTTP 层的公共小工具:静态目录、vault 名、时间戳。

各 router 模块共用,单独拎出来避免互相 import 成环。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from kb.config import VAULT_ROOT, ScheduleUnreadable

_STATIC = Path(__file__).resolve().parent.parent / "static"

# obsidian://open 深链需要 vault 名(obs 目录名)
VAULT_NAME = VAULT_ROOT.name


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def unreadable_config(e: ScheduleUnreadable) -> HTTPException:
    """读不出配置 → 503 + 原因。**不许回 200 带一份缺省值。**

    回 200 的那一版里,界面没法把「读不出来」和「用户关掉了」分开:两处都写
    「未启用」、来源说明还断言「这一类来源目前不会自动同步」,而盘上那份其实开着
    (第 1 轮实测把配置文件 chmod 000)。请求失败时「不知道」那一档前端本来就有
    (`schedules.js` 读不到就把那条留成 null、`source_forms.js` 照它说"没法确定")。

    **两条定时共用这一句**(日报和页头):措辞在两处各写一份的话,改一处就会出现
    "一条说得清、另一条还在说未启用"。
    """
    return HTTPException(503, f'{e}。修好或删掉这个文件之后再试。')
