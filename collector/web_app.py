#!/usr/bin/env python
"""ASGI 组合入口：在旧 API 上挂载拆分后的新路由。

采集前的环境准备（profile_ext 通道、微信域名绕过代理）统一在
src/collect/bootstrap 里，CLI 与本入口共用同一份，避免两边漂移。
"""

from __future__ import annotations

import api as legacy_api

from src.api.profile_credentials import router as profile_credentials_router
from src.collect.bootstrap import install as install_collect_env

install_collect_env()
app = legacy_api.app
if not any(route.path == "/api/profile-credentials/status" for route in app.routes):
    app.include_router(profile_credentials_router)
