"""Interactive states need fields that the server can actually consume."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from kb import wechat_runtime as runtime, wechat_setup as setup
from kb import wechat_setup_contract as contract


class InteractiveContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.roots = {"account-one": self.home / "one/db_storage",
                      "account-two": self.home / "two/db_storage"}
        for root in self.roots.values():
            root.mkdir(parents=True)
        for mocked in (patch.object(runtime, "runtime_home", return_value=self.home),
                       patch.object(runtime.platform, "system", return_value="Darwin"),
                       patch.object(setup, "account_roots", return_value=self.roots),
                       patch("kb.deployment.in_container", return_value=False)):
            mocked.start()
            self.addCleanup(mocked.stop)

    def authorization(self, **changes):
        return {"state": "authorization_required", "running": False,
                "confirmation_token": "a" * 43, "expires_at": time.time() + 800,
                "account_id": "account-one", "provider_revision": runtime.WXKEY_REV, **changes}

    def test_missing_empty_unknown_malformed_or_duplicate_candidates_are_retryable(self):
        for accounts in (None, [], {}, "account-one", [None], [{"id": []}], [{"id": "unknown"}],
                         [{"id": "account-one"}, {"id": "account-one"}]):
            with self.subTest(accounts=accounts):
                setup._write({"state": "account_selection_required", "running": False,
                              "candidate_count": 2, "accounts": accounts, "confirmation_token": "secret"})
                observed = setup.snapshot()
                self.assertEqual("needs_database_location", observed["state"])
                self.assertTrue(observed["can_connect"])
                self.assertTrue(observed["retryable"])
                self.assertIn("db_storage", observed["message"])
                self.assertNotIn("accounts", observed)
                self.assertNotIn("confirmation_token", observed)

    def test_nonempty_candidates_use_backend_ids_and_server_labels(self):
        setup._write({"state": "account_selection_required", "running": False,
                      "accounts": [{"id": "account-one", "label": "untrusted path"},
                                   {"id": "account-two"}], "candidate_count": 2})
        observed = setup.snapshot()
        self.assertEqual("account_selection_required", observed["state"])
        self.assertEqual([{"id": "account-one", "label": "one"},
                          {"id": "account-two", "label": "two"}], observed["accounts"])

    def test_stored_bad_selection_state_is_repaired_at_api_read_without_rewriting(self):
        payload = json.dumps({"state": "account_selection_required", "running": False}).encode()
        setup._state_file().write_bytes(payload)
        self.assertEqual("needs_database_location", setup.snapshot()["state"])
        self.assertEqual(payload, setup._state_file().read_bytes())

    def test_directory_discovery_failure_leaves_a_recheck_instead_of_reset(self):
        with patch.object(setup, "account_roots", side_effect=runtime.SetupError("bad root")):
            setup._write({"state": "account_selection_required", "accounts": [{"id": "account-one"}],
                          "running": False})
        self.assertEqual("needs_database_location", setup.snapshot()["state"])

    def test_authorization_requires_token_expiry_account_revision_and_platform(self):
        changes = [{"confirmation_token": None}, {"confirmation_token": ""},
                   {"confirmation_token": "a" * 129}, {"confirmation_token": "x y" * 16},
                   {"expires_at": None}, {"expires_at": True}, {"expires_at": float("nan")},
                   {"expires_at": float("inf")}, {"expires_at": 10 ** 400},
                   {"expires_at": time.time() - 1}, {"expires_at": time.time() + 10000},
                   {"account_id": ""}, {"account_id": []}, {"account_id": "stale"},
                   {"provider_revision": "old"}, {"provider_revision": None}, {"platform": "windows"}]
        for changed in changes:
            with self.subTest(changed=changed):
                setup._write(self.authorization(**changed))
                observed = setup.snapshot()
                self.assertEqual("blocked", observed["state"])
                self.assertTrue(observed["can_connect"])
                self.assertNotIn("confirmation_token", observed)
                with patch.object(setup.threading, "Thread") as worker:
                    with self.assertRaises(runtime.SetupError):
                        setup.start("acquire", confirmation_token="a" * 43, confirmed=True)
                    worker.assert_not_called()

    def test_valid_mac_and_windows_authorizations_are_preserved(self):
        for platform in ("darwin", "windows"):
            with self.subTest(platform=platform):
                state = self.authorization(platform=platform)
                self.assertEqual(state, contract.validate(state, self.roots, runtime.WXKEY_REV, platform))
        setup._write(self.authorization())
        self.assertEqual("authorization_required", setup.snapshot()["state"])

    def test_valid_authorization_starts_only_after_explicit_confirmation(self):
        setup._write(self.authorization())
        with patch.object(setup.threading, "Thread") as worker:
            with self.assertRaises(runtime.SetupError):
                setup.start("acquire", confirmation_token="a" * 43, confirmed=False)
            worker.assert_not_called()
            setup.start("acquire", confirmation_token="a" * 43, confirmed=True)
            arguments = worker.call_args.kwargs["args"]
            self.assertEqual(("account-one", True), arguments[1:])
            os.close(arguments[0])

    def test_non_ascii_or_malformed_confirmation_is_safely_rejected(self):
        setup._write(self.authorization())
        with patch.object(setup.threading, "Thread") as worker:
            for token in ("非 ASCII", None, [], True, "wrong-token"):
                with self.subTest(token=token), self.assertRaises(runtime.SetupError):
                    setup.start("acquire", confirmation_token=token, confirmed=True)
            worker.assert_not_called()

    def test_account_removed_after_publication_invalidates_authorization(self):
        setup._write(self.authorization())
        with patch.object(setup, "account_roots", return_value={}):
            self.assertEqual("blocked", setup.snapshot()["state"])
            with self.assertRaises(runtime.SetupError):
                setup.start("acquire", confirmation_token="a" * 43, confirmed=True)

    def test_mac_reader_enum_without_fields_is_checked_by_real_worker(self):
        for reader_state in ("account_selection_required", "authorization_required"):
            with (self.subTest(reader_state=reader_state),
                  patch.object(setup, "_reader_ready", return_value=False),
                  patch.object(setup, "_recovery_lock", return_value=self.home / "no-recovery-lock"),
                  patch.object(setup, "_config", return_value=self.home / "no-config"),
                  patch.object(runtime, "prepare_reader", return_value=["reader"]),
                  patch.object(runtime, "json_call", return_value={"state": reader_state}),
                  patch.object(runtime, "prepare_provider") as provider):
                setup._work(setup._lock(), "account-one", False)
                self.assertNotIn(setup.snapshot()["state"],
                                 {"account_selection_required", "authorization_required"})
                provider.assert_not_called()

    def test_valid_selected_account_is_accepted_by_backend(self):
        setup._write({"state": "account_selection_required", "running": False,
                      "accounts": [{"id": "account-one"}]})
        with patch.object(setup.threading, "Thread") as worker:
            setup.start("connect", account_id=setup.snapshot()["accounts"][0]["id"])
            self.assertEqual("account-one", worker.call_args.kwargs["args"][1])
            os.close(worker.call_args.kwargs["args"][0])

    def test_ready_and_other_noninteractive_states_are_unchanged(self):
        for kind in ("ready", "failed", "partial", "preparing"):
            state = {"state": kind, "running": False, "message": "original", "ready": kind == "ready"}
            self.assertEqual(state, contract.validate(state, {}, "revision", "darwin"))


if __name__ == "__main__":
    unittest.main()
