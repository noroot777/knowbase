"""微信数据库采集通道；共用公众号订阅、文章库与导出。"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace

from kb.collectors import FetchScope, SourceStatus
from kb.collectors.wechat_oa import WeChatOACollector, _backend_problem, _run_cli


class WeChatLocalCollector(WeChatOACollector):
    name = "wechat_local"
    channel_label = "绿泡泡本地数据库"
    cli_channel = "local"
    channel_hint = "读取本机已同步的公众号推送，再获取原文；无需 wxdown 或逐号文章凭证。"
    range_hint = "只能覆盖本机已同步的推送，按文章发布时间筛选；新订阅不能保证补齐历史。"
    _local_cache = None

    def fetch(self, scope: FetchScope) -> None:
        try:
            super().fetch(scope)
        finally:
            self._local_cache = None

    def status(self) -> SourceStatus:
        if self._local_cache and time.monotonic() - self._local_cache[0] < 30:
            return self._local_cache[1]
        status = self._probe()
        self._local_cache = (time.monotonic(), status)
        return status

    def _probe(self) -> SourceStatus:
        problem = _backend_problem()
        if problem:
            return SourceStatus(online=False, message="采集器还没装好", hint=problem)
        try:
            proc = _run_cli("status", "--channel", "local", timeout=75)
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            if proc.returncode:
                raise ValueError
            return SourceStatus(online=True, ready=bool(data.get("ready")),
                                message=data.get("message", ""), hint=data.get("hint", ""),
                                targets=tuple(data.get("targets") or []))
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return SourceStatus(online=False, message="绿泡泡数据库状态读取失败",
                                hint="请检查采集器和 wechat-cli doctor")

    def list_targets(self):
        # 本地库可读 ≠ 每个公众号有推送，不能把全局可用伪装成逐号就绪。
        return [replace(t, ready=None, state="按本机推送采集", tone="")
                for t in super().list_targets()]

    def _mutate(self, *args, **kwargs):
        self._local_cache = None
        return super()._mutate(*args, **kwargs)


collector = WeChatLocalCollector()
