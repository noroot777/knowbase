"""Shared state interpretation must agree with serialization and explicit reset."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import wechat_runtime as runtime, wechat_setup as setup
from kb import wechat_setup_contract as contract, wechat_setup_status as status
from kb.web.wechat_api import router


class StateInterpretationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.path = self.home / "setup-state.json"
        self.roots = {"account-one": self.home / "one/db_storage"}
        self.roots["account-one"].mkdir(parents=True)
        for mocked in (patch.object(runtime, "runtime_home", return_value=self.home),
                       patch.object(runtime.platform, "system", return_value="Darwin"),
                       patch.object(setup, "account_roots", return_value=self.roots),
                       patch("kb.deployment.in_container", return_value=False)):
            mocked.start()
            self.addCleanup(mocked.stop)
        app = FastAPI()
        app.include_router(router)
        self.client = self.enterContext(TestClient(
            app, base_url="http://localhost", client=("127.0.0.1", 12345),
            raise_server_exceptions=False))

    def state(self, kind):
        result = {"state": kind, "running": False, "message": "previous state"}
        if kind == "account_selection_required":
            result["accounts"] = [{"id": "account-one", "label": "untrusted"}]
        elif kind == "authorization_required":
            result.update(confirmation_token="a" * 43, expires_at=time.time() + 800,
                          account_id="account-one", provider_revision=runtime.WXKEY_REV)
        return result

    def write(self, state):
        payload = json.dumps(state, ensure_ascii=True).encode()
        self.path.write_bytes(payload)
        return payload

    def reset(self):
        return self.client.post("/api/wechat/setup", json={"action": "reset_status"},
                                headers={"X-KB-Wechat-Action": "1"})

    def assert_recoverable(self, state):
        payload = self.write(state)
        before = set(self.home.glob("*.bak"))
        with patch.object(setup, "_connect") as connect:
            with self.assertRaises(runtime.SetupError):
                setup._read()
            response = self.client.get("/api/wechat/setup")
            self.assertEqual(200, response.status_code, response.text)
            observed = response.json()
            self.assertEqual("status_unavailable", observed["state"])
            self.assertTrue(observed["can_reset_status"])
            self.assertFalse(observed["can_connect"])
            self.assertIsNone(observed["running"])
            json.dumps(observed, allow_nan=False).encode("utf-8")
            self.assertEqual(payload, self.path.read_bytes())
            with self.assertRaises(runtime.SetupError):
                setup.start("connect")
            response = self.reset()
            self.assertEqual(202, response.status_code, response.text)
            self.assertEqual("unchecked", response.json()["state"])
            created = set(self.home.glob("*.bak")) - before
            self.assertEqual(1, len(created))
            self.assertEqual(payload, created.pop().read_bytes())
            self.assertEqual(202, self.reset().status_code)
            self.assertEqual(len(before) + 1, len(list(self.home.glob("*.bak"))))
            connect.assert_not_called()

    def test_common_fields_are_checked_across_ordinary_selection_and_authorization_states(self):
        invalid = {"message": [None, {}, [], False, "\ud800"],
                   "updated_at": [float("inf"), float("-inf"), float("nan"),
                                  10 ** 400, -1, True, "123", None],
                   "platform": [None, {}, False], "deployment": [[], 1],
                   "running": [None, "false"], "ready": [1, []],
                   "can_connect": ["false"], "can_reset_status": [1],
                   "retryable": [None], "provider_ready": [{}], "models_ready": [1],
                   "unresolved": [None, True, -1, 1.5, "2", 10 ** 400]}
        for kind in ("failed", "ready", "account_selection_required", "authorization_required"):
            for field, values in invalid.items():
                for value in values:
                    with self.subTest(kind=kind, field=field, value=repr(value)):
                        self.assert_recoverable({**self.state(kind), field: value})

    def test_persisted_unavailable_is_not_mistaken_for_a_healthy_noop(self):
        for message in ("old error", None, {}, 7):
            with self.subTest(message=message):
                self.assert_recoverable({"state": "status_unavailable", "running": False,
                                         "message": message})

    def test_overflow_json_literals_use_the_same_recovery_path(self):
        for kind in ("failed", "account_selection_required", "authorization_required"):
            with self.subTest(kind=kind):
                self.path.write_text('{"state":"' + kind + '","updated_at":1e999}', encoding="utf-8")
                self.assertTrue(setup.snapshot()["can_reset_status"])
                payload = self.path.read_bytes()
                self.assertEqual("unchecked", self.reset().json()["state"])
                self.assertIn(payload, [path.read_bytes() for path in self.home.glob("*.bak")])

    def test_irrelevant_interactive_and_unknown_fields_are_not_exposed(self):
        extras = {"accounts": {"id": "old"}, "confirmation_token": ["stale"],
                  "expires_at": float("inf"), "account_id": {}, "provider_revision": [],
                  "unexpected": {"nested": float("nan")}}
        for kind in ("failed", "unchecked", "ready", "partial", "interrupted"):
            for accounts in ({"id": "old"}, "old", [None], [{"id": []}], []):
                with self.subTest(kind=kind, accounts=accounts):
                    original = self.write({**self.state(kind), **extras, "accounts": accounts})
                    response = self.client.get("/api/wechat/setup")
                    self.assertEqual(200, response.status_code, response.text)
                    observed = response.json()
                    self.assertEqual(kind, observed["state"])
                    self.assertTrue(extras.keys().isdisjoint(observed))
                    json.dumps(observed, allow_nan=False).encode("utf-8")
                    self.assertEqual(kind, self.reset().json()["state"])
                    self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([], list(self.home.glob("*.bak")))

    def test_interactive_fields_are_projected_without_leaking_stale_fields(self):
        for kind, excluded in (("account_selection_required", "confirmation_token"),
                               ("authorization_required", "accounts")):
            with self.subTest(kind=kind):
                state = {**self.state(kind), excluded: {"unusable": float("inf")},
                         "unknown": float("nan")}
                observed = contract.validate(state, self.roots, runtime.WXKEY_REV, "darwin")
                self.assertEqual(kind, observed["state"])
                self.assertNotIn(excluded, observed)
                self.assertNotIn("unknown", observed)
                json.dumps(observed, allow_nan=False).encode("utf-8")

    def test_bad_specific_fields_still_degrade_to_recheck_not_unusable_interactions(self):
        for accounts in ({}, "old", [None], [{"id": "\ud800"}], [{"id": []}]):
            with self.subTest(accounts=accounts):
                self.write({**self.state("account_selection_required"), "accounts": accounts})
                self.assertEqual("needs_database_location", self.client.get("/api/wechat/setup").json()["state"])
        for field, value in (("confirmation_token", "\ud800"), ("expires_at", float("inf")),
                             ("account_id", []), ("provider_revision", {})):
            with self.subTest(field=field):
                self.write({**self.state("authorization_required"), field: value})
                observed = self.client.get("/api/wechat/setup").json()
                self.assertEqual("blocked", observed["state"])
                self.assertNotIn("confirmation_token", observed)

    def test_normal_publication_cannot_persist_bad_common_fields(self):
        original = self.write(self.state("ready"))
        for kind in ("ready", "account_selection_required", "authorization_required"):
            for invalid in ({"message": []}, {"updated_at": float("inf")}, {"ready": "yes"}):
                with self.subTest(kind=kind, invalid=invalid):
                    with self.assertRaises(runtime.SetupError):
                        setup._write({**self.state(kind), **invalid})
                    self.assertEqual(original, self.path.read_bytes())

    def test_common_field_failures_keep_real_busy_and_unknown_locks_closed(self):
        payload = self.write({**self.state("failed"), "updated_at": float("inf")})
        descriptor = setup._lock()
        try:
            self.assertFalse(setup.snapshot()["can_reset_status"])
            self.assertTrue(setup.snapshot()["running"])
            self.assertEqual(409, self.reset().status_code)
        finally:
            os.close(descriptor)
        with patch.object(setup, "_lock", side_effect=PermissionError):
            self.assertFalse(setup.snapshot()["can_reset_status"])
            self.assertIsNone(setup.snapshot()["running"])
            self.assertEqual(409, self.reset().status_code)
        self.assertEqual(payload, self.path.read_bytes())
        self.assertEqual([], list(self.home.glob("*.bak")))

    def test_healthy_public_fields_survive_without_rewriting_or_backing_up(self):
        for kind in ("ready", "failed", "account_selection_required", "authorization_required"):
            with self.subTest(kind=kind):
                state = {**self.state(kind), "updated_at": time.time(), "ready": kind == "ready",
                         "can_connect": True, "retryable": False, "platform": "darwin"}
                original = self.write(state)
                self.assertEqual(kind, self.client.get("/api/wechat/setup").json()["state"])
                self.assertEqual(kind, self.reset().json()["state"])
                self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([], list(self.home.glob("*.bak")))

    def test_main_terminal_fields_survive_normalization_and_republication(self):
        for kind, fields in (("partial", {"unresolved": 2}),
                             ("account_selection_required", {"accounts": [{"id": "account-one", "label": "one"}]})):
            with self.subTest(kind=kind):
                setup._write({**self.state(kind), **fields})
                published = json.loads(self.path.read_text(encoding="utf-8"))
                self.assertEqual(published, setup._read())
                for field, value in fields.items():
                    self.assertEqual(value, setup.snapshot()[field])

    def test_raw_windows_lock_failure_does_not_bypass_main_error_translation(self):
        self.write({"state": "checking", "running": True})
        failure = OSError("synthetic private failure")
        failure.winerror = 33
        with patch.object(setup.filelock, "flock", side_effect=failure):
            with self.assertRaises(OSError) as raised:
                setup._lock()
            self.assertIs(raised.exception, failure)
            observed = setup.snapshot()
        self.assertEqual("status_unavailable", observed["state"])
        self.assertIsNone(observed["running"])
        self.assertFalse(observed["can_reset_status"])
        self.assertNotIn("synthetic private failure", observed["message"])


if __name__ == "__main__":
    unittest.main()
