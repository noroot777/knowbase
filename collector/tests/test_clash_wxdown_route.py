from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.services.clash_wxdown_route import (
    ClashRouteError,
    ClashWxdownRoute,
)


class ClashWxdownRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.route = ClashWxdownRoute(
            controller_socket=Path("/tmp/test-mihomo.sock"),
            route_group="WXDOWN-AUTO",
            local_proxy="WXDOWN-LOCAL",
            proxy_port=65000,
        )

    def test_status_reports_direct_route_as_recoverable(self) -> None:
        with patch.object(
            self.route,
            "_proxy",
            side_effect=[
                {"now": "DIRECT"},
                {"alive": True},
            ],
        ), patch.object(self.route, "_port_is_listening", return_value=True):
            result = self.route.status()

        self.assertTrue(result["available"])
        self.assertFalse(result["active"])
        self.assertEqual(result["current"], "DIRECT")
        self.assertIn("重新接管", result["message"])

    def test_activate_switches_fallback_group_to_local(self) -> None:
        responses = [
            {"now": "DIRECT"},
            {"alive": True},
            {"now": "WXDOWN-LOCAL"},
            {"alive": True},
        ]
        with patch.object(
            self.route,
            "_proxy",
            side_effect=responses,
        ), patch.object(
            self.route,
            "_port_is_listening",
            return_value=True,
        ), patch.object(
            self.route,
            "_request",
            return_value=(204, {}),
        ) as request:
            result = self.route.activate()

        self.assertTrue(result["active"])
        request.assert_called_once_with(
            "PUT",
            "/proxies/WXDOWN-AUTO",
            {"name": "WXDOWN-LOCAL"},
        )

    def test_activate_requires_running_wxdown_proxy(self) -> None:
        with patch.object(self.route, "_port_is_listening", return_value=False):
            with self.assertRaisesRegex(ClashRouteError, "65000"):
                self.route.activate()


if __name__ == "__main__":
    unittest.main()
