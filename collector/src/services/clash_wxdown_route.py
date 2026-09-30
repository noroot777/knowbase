"""通过 Clash Verge / Mihomo 控制接口管理微信抓包路由。"""

from __future__ import annotations

import http.client
import json
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import quote


DEFAULT_CONTROLLER_SOCKET = Path("/tmp/verge/verge-mihomo.sock")
DEFAULT_ROUTE_GROUP = "WXDOWN-AUTO"
DEFAULT_LOCAL_PROXY = "WXDOWN-LOCAL"
DEFAULT_PROXY_PORT = 65000


class ClashRouteError(RuntimeError):
    """Clash 路由状态读取或切换失败。"""


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self.socket_path))
        self.sock = connection


class ClashWxdownRoute:
    """读取并切换 ``WXDOWN-AUTO`` 代理组。"""

    def __init__(
        self,
        *,
        controller_socket: Path | None = None,
        route_group: str | None = None,
        local_proxy: str | None = None,
        proxy_port: int | None = None,
        timeout: float = 2.0,
    ) -> None:
        socket_override = os.getenv("CLASH_CONTROLLER_SOCKET", "").strip()
        self.controller_socket = controller_socket or (
            Path(socket_override) if socket_override else DEFAULT_CONTROLLER_SOCKET
        )
        self.route_group = (
            route_group
            or os.getenv("CLASH_WXDOWN_GROUP", "").strip()
            or DEFAULT_ROUTE_GROUP
        )
        self.local_proxy = (
            local_proxy
            or os.getenv("CLASH_WXDOWN_PROXY", "").strip()
            or DEFAULT_LOCAL_PROXY
        )
        self.proxy_port = int(
            proxy_port
            or os.getenv("WXDOWN_PROXY_PORT", "").strip()
            or DEFAULT_PROXY_PORT
        )
        self.timeout = max(0.1, float(timeout))

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if not self.controller_socket.exists():
            raise ClashRouteError("未找到 Clash Verge 控制接口，请确认 Clash Verge 正在运行")
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = _UnixHTTPConnection(self.controller_socket, self.timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise ClashRouteError("无法连接 Clash Verge 控制接口") from exc
        finally:
            connection.close()
        data: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = {}
            if isinstance(parsed, dict):
                data = parsed
        return response.status, data

    def _proxy(self, name: str) -> dict[str, Any]:
        status, data = self._request("GET", f"/proxies/{quote(name, safe='')}")
        if status != 200:
            raise ClashRouteError(f"Clash 配置中未找到 {name}")
        return data

    def _port_is_listening(self) -> bool:
        try:
            with socket.create_connection(
                ("127.0.0.1", self.proxy_port), timeout=self.timeout
            ):
                return True
        except OSError:
            return False

    def status(self) -> dict[str, Any]:
        """返回适合直接展示给前端的路由状态。"""
        try:
            group = self._proxy(self.route_group)
            local = self._proxy(self.local_proxy)
        except ClashRouteError as exc:
            return {
                "available": False,
                "active": False,
                "current": "",
                "route_group": self.route_group,
                "local_proxy": self.local_proxy,
                "local_alive": False,
                "proxy_listening": self._port_is_listening(),
                "message": str(exc),
            }

        current = str(group.get("now") or "")
        local_alive = local.get("alive") is True
        listening = self._port_is_listening()
        active = current == self.local_proxy and local_alive and listening
        if active and local_alive and listening:
            message = "绿泡泡文章流量正在经过 wxdown"
        elif not listening:
            message = f"wxdown 代理端口 {self.proxy_port} 未启动"
        elif not local_alive:
            message = "Clash 暂未判定 WXDOWN-LOCAL 可用"
        else:
            message = f"当前走 {current or '未知路由'}，可点击重新接管"
        return {
            "available": True,
            "active": active,
            "current": current,
            "route_group": self.route_group,
            "local_proxy": self.local_proxy,
            "local_alive": local_alive,
            "proxy_listening": listening,
            "message": message,
        }

    def activate(self) -> dict[str, Any]:
        """立即把目标代理组切回 ``WXDOWN-LOCAL``。"""
        if not self._port_is_listening():
            raise ClashRouteError(
                f"wxdown 代理端口 {self.proxy_port} 未启动，请先启动整套服务"
            )
        # 先读取一次，给缺少组或节点时返回明确错误，而不是模糊的 400。
        self._proxy(self.route_group)
        self._proxy(self.local_proxy)
        status, _ = self._request(
            "PUT",
            f"/proxies/{quote(self.route_group, safe='')}",
            {"name": self.local_proxy},
        )
        if status not in (200, 204):
            raise ClashRouteError("Clash 未接受路由切换请求")
        result = self.status()
        if not result["active"]:
            raise ClashRouteError("路由切换后仍未选中 WXDOWN-LOCAL")
        return result


clash_wxdown_route = ClashWxdownRoute()
