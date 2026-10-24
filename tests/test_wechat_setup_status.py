"""Explicit status recovery must not run onboarding or touch access material."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import wechat_runtime as runtime, wechat_setup as setup, wechat_setup_status as status
from kb.web.wechat_api import router


class StatusRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.path = self.home / "runtime/setup-state.json"
        self.path.parent.mkdir()
        for mocked in (patch.object(runtime, "runtime_home", return_value=self.path.parent),
                       patch("kb.deployment.in_container", return_value=False)):
            mocked.start()
            self.addCleanup(mocked.stop)

    def write_raw(self, payload):
        self.path.write_bytes(payload)

    def backups(self):
        return list(self.path.parent.glob("setup-state.json.corrupt-*.bak"))

    def test_bad_status_requires_explicit_lossless_reset(self):
        samples = [b"{", b"[]", b'{}', b'{"state": 3}', b"[" * 1100 + b"]" * 1100,
                   b'{"state":"checking","running":"yes"}',
                   json.dumps({"state": "failed", "message": "旧状态"}, ensure_ascii=False).encode("gbk")]
        for payload in samples:
            with self.subTest(payload=payload), patch.object(setup.threading, "Thread") as worker:
                self.write_raw(payload)
                previous = len(self.backups())
                for unused in range(2):
                    observed = setup.snapshot()
                    self.assertEqual("status_unavailable", observed["state"])
                    self.assertFalse(observed["can_connect"])
                    self.assertTrue(observed["can_reset_status"])
                    self.assertIsNone(observed["running"])
                    self.assertEqual(payload, self.path.read_bytes())
                    self.assertEqual(previous, len(self.backups()))
                with self.assertRaises(runtime.SetupError):
                    setup.start("connect")
                with self.assertRaises(runtime.SetupError):
                    setup.start("acquire", confirmed=True, confirmation_token="forged")
                restored = setup.start("reset_status")
                self.assertEqual("unchecked", restored["state"])
                self.assertFalse(restored["running"])
                self.assertEqual(previous + 1, len(self.backups()))
                self.assertIn(payload, [backup.read_bytes() for backup in self.backups()])
                setup.start("reset_status")
                self.assertEqual(previous + 1, len(self.backups()))
                worker.assert_not_called()

    def test_reset_preserves_material_configuration_and_recovery_lock(self):
        protected = {self.home / name: name.encode() for name in
                     ("config.json", "keys.json", "all_keys.json", "recovery-required.lock")}
        for path, payload in protected.items():
            path.write_bytes(payload)
        self.write_raw(b"{")
        with patch.object(setup, "_connect") as connect, patch.object(setup, "_backup") as backup:
            setup.start("reset_status", confirmed=True, confirmation_token="ignored", account_id="ignored")
            connect.assert_not_called()
            backup.assert_not_called()
        self.assertEqual(protected, {path: path.read_bytes() for path in protected})

    def test_actual_busy_lock_prevents_backup_and_reset(self):
        self.write_raw(b"{")
        descriptor = setup._lock()
        try:
            observed = setup.snapshot()
            self.assertTrue(observed["running"])
            self.assertFalse(observed["can_reset_status"])
            with self.assertRaises(runtime.SetupError):
                setup.start("reset_status")
            self.assertEqual(b"{", self.path.read_bytes())
            self.assertEqual([], self.backups())
        finally:
            os.close(descriptor)
        self.assertTrue(setup.snapshot()["can_reset_status"])
        self.assertEqual("unchecked", setup.start("reset_status")["state"])

    def test_reset_rechecks_lock_after_an_idle_snapshot(self):
        self.write_raw(b"{")
        self.assertTrue(setup.snapshot()["can_reset_status"])
        descriptor = setup._lock()
        try:
            with self.assertRaises(runtime.SetupError):
                setup.start("reset_status")
        finally:
            os.close(descriptor)
        self.assertEqual(b"{", self.path.read_bytes())
        self.assertEqual([], self.backups())

    def test_valid_state_is_never_reset_even_after_an_old_unavailable_response(self):
        self.write_raw(b"{")
        self.assertTrue(setup.snapshot()["can_reset_status"])
        payload = b'{"state":"ready","ready":true,"running":false}'
        self.write_raw(payload)
        self.assertEqual("ready", setup.start("reset_status")["state"])
        self.assertEqual(payload, self.path.read_bytes())
        self.assertEqual([], self.backups())

    def test_backup_failure_does_not_change_original_and_releases_lock(self):
        self.write_raw(b"{")
        for injected in ("copyfileobj", "fsync"):
            owner = status.shutil if injected == "copyfileobj" else status.os
            with self.subTest(injected=injected), patch.object(owner, injected, side_effect=OSError):
                with self.assertRaisesRegex(runtime.SetupError, "备份"):
                    setup.start("reset_status")
            self.assertEqual(b"{", self.path.read_bytes())
            descriptor = setup._lock()
            os.close(descriptor)

    def test_reset_write_failure_keeps_original_and_complete_backup(self):
        self.write_raw(b"{")
        with patch.object(setup, "_write", side_effect=OSError):
            with self.assertRaisesRegex(runtime.SetupError, "重建失败"):
                setup.start("reset_status")
        self.assertEqual(b"{", self.path.read_bytes())
        self.assertEqual([b"{"], [backup.read_bytes() for backup in self.backups()])

    def test_permission_error_does_not_mean_busy_or_resettable(self):
        self.write_raw(b"{")
        with patch.object(setup, "_lock", side_effect=PermissionError):
            observed = setup.snapshot()
            self.assertIsNone(observed["running"])
            self.assertFalse(observed["can_reset_status"])
            with self.assertRaises(runtime.SetupError):
                setup.start("reset_status")
        self.assertEqual([], self.backups())

    def test_persistent_read_denial_cannot_reset_without_reading_backup_bytes(self):
        self.write_raw(b"{")
        with (patch.object(Path, "read_text", side_effect=PermissionError),
              patch.object(Path, "open", side_effect=PermissionError), patch.object(status.time, "sleep")):
            with self.assertRaises(runtime.SetupError):
                setup.start("reset_status")
        self.assertEqual(b"{", self.path.read_bytes())
        self.assertEqual([], self.backups())

    def test_missing_status_remains_a_normal_first_run(self):
        self.assertEqual("unchecked", setup.snapshot()["state"])
        self.assertEqual("unchecked", setup.start("reset_status")["state"])
        self.assertFalse(self.path.exists())
        self.assertEqual([], self.backups())

    def test_api_requires_local_same_origin_action_header_and_rejects_docker(self):
        app = FastAPI()
        app.include_router(router)
        self.write_raw(b"{")
        with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
            for headers in ({}, {"X-KB-Wechat-Action": "1", "origin": "http://evil.test"},
                            {"X-KB-Wechat-Action": "1", "sec-fetch-site": "cross-site"}):
                self.assertEqual(403, client.post("/api/wechat/setup", json={"action": "reset_status"},
                                                 headers=headers).status_code)
            with patch("kb.web.wechat_api.in_container", return_value=True):
                self.assertEqual(409, client.post("/api/wechat/setup", json={"action": "reset_status"},
                                                 headers={"X-KB-Wechat-Action": "1"}).status_code)
            response = client.post("/api/wechat/setup", json={"action": "reset_status"},
                                   headers={"X-KB-Wechat-Action": "1"})
            self.assertEqual(202, response.status_code, response.text)
            self.assertEqual("unchecked", response.json()["state"])
        self.assertEqual([b"{"], [backup.read_bytes() for backup in self.backups()])


if __name__ == "__main__":
    unittest.main()
