"""模型设置的事务内版本、迟到提交和配置快照。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
import subprocess
import sys
import unittest

from kb import llm, llm_presets
from kb.web import favorites_api
from tests import test_llm_presets


class ModelSettingsRevisionTests(unittest.TestCase):
    setUp = test_llm_presets.ModelPresetTests.setUp
    read = test_llm_presets.ModelPresetTests.read
    write = test_llm_presets.ModelPresetTests.write

    def test_all_model_writers_reject_late_commits_inside_storage_transaction(self):
        self.api.app.include_router(favorites_api.router)
        routes = [
            ("/api/stages/preset", {"preset": "gpt"}),
            ("/api/stages/preset/name", {"preset": "gpt", "name": "过期组名"}),
            ("/api/stages", {"key": "ask", "provider": "openai", "model": "gpt-5.5"}),
            ("/api/favorites/settings", {"model": "openai:gpt-5.5"}),
        ]
        original = llm.update_settings
        for index, (route, body) in enumerate(routes):
            with self.subTest(route=route):
                entered, released = Event(), Event()
                operation_id = f"old-{index}"
                revision = self.api.get("/api/stages").json()["revision"]

                def delayed(mutate, **options):
                    if options.get("operation_id") == operation_id:
                        entered.set()
                        if not released.wait(10):
                            raise RuntimeError("未释放测试闸门")
                    return original(mutate, **options)

                with patch.object(llm, "update_settings", side_effect=delayed), ThreadPoolExecutor(1) as pool:
                    pending = pool.submit(self.api.post, route, json={
                        **body, "revision": revision, "operation_id": operation_id})
                    try:
                        self.assertTrue(entered.wait(5))
                        before = self.api.get("/api/stages", params={"operation_id": operation_id}).json()
                        self.assertEqual(before["operation"]["status"], "unconfirmed")
                        latest = self.api.post("/api/stages/preset", json={
                            "preset": "claude", "revision": revision, "operation_id": f"new-{index}"})
                        self.assertEqual(latest.status_code, 200, latest.text)
                    finally:
                        released.set()
                    self.assertEqual(pending.result(5).status_code, 409)
                current = self.api.get("/api/stages").json()
                self.assertEqual(current["revision"], revision + 1)
                self.assertTrue(next(preset for preset in current["presets"] if preset["key"] == "claude")["active"])
                self.assertNotIn(operation_id, self.read().get("settings_operations", {}))

    def test_operation_result_is_persisted_and_read_with_same_snapshot(self):
        result = self.api.post("/api/stages/preset", json={
            "preset": "gpt", "revision": 0, "operation_id": "committed-operation"})
        self.assertEqual(result.status_code, 200)
        result = result.json()
        current = self.api.get("/api/stages", params={"operation_id": "committed-operation"}).json()
        self.assertEqual(current["revision"], result["operation_revision"])
        self.assertEqual(current["operation"], {
            "id": "committed-operation", "status": "committed", "revision": current["revision"]})
        self.assertNotIn("sk-fake", str(current))
        self.assertEqual(self.api.post("/api/stages/preset", json={
            "preset": "claude", "revision": current["revision"],
            "operation_id": "committed-operation"}).status_code, 409)

    def test_every_settings_update_advances_revision_and_read_snapshot_is_stable(self):
        with llm.settings_snapshot() as initial:
            llm.set_config({"openai": {"models": ["gpt-5-mini"]}})
            self.assertEqual(llm.read_settings(), initial)
        current = llm.read_settings()
        self.assertEqual(llm.settings_revision(current), llm.settings_revision(initial) + 1)
        self.assertEqual(current["openai"]["models"], ["gpt-5-mini"])

    def test_preset_candidates_cannot_be_committed_after_their_configuration_changes(self):
        describe = llm_presets._describe

        def changed(key):
            preset = describe(key)
            llm.set_config({"openai": {"key": ""}})
            return preset

        with patch.object(llm_presets, "_describe", side_effect=changed):
            response = self.api.post("/api/stages/preset", json={"preset": "gpt", "revision": 0})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("stages", self.read())

    def test_query_derives_all_fields_from_one_file_read(self):
        original = llm._file_secrets
        reads = []

        def observed():
            if llm._settings_view.get() is None:
                reads.append(True)
            return original()

        with patch.object(llm, "_file_secrets", side_effect=observed):
            self.assertEqual(self.api.get("/api/stages").status_code, 200)
        self.assertEqual(len(reads), 1)

    def test_unicode_name_limit_is_code_points(self):
        name = "😀" * llm_presets.NAME_MAX_LENGTH
        self.assertEqual(self.api.post("/api/stages/preset/name", json={
            "preset": "gpt", "name": name}).status_code, 200)
        self.assertEqual(self.api.post("/api/stages/preset/name", json={
            "preset": "gpt", "name": name + "😀"}).status_code, 400)

    def test_revision_survives_a_fresh_process(self):
        self.assertEqual(self.api.post("/api/stages/preset", json={"preset": "claude"}).status_code, 200)
        code = """
from pathlib import Path
import sys
from kb import llm, llm_presets
llm._SECRETS_PATH = Path(sys.argv[1])
llm.DATA_DIR = llm._SECRETS_PATH.parent
try:
    llm_presets.apply('gpt', expected_revision=0)
except llm.SettingsConflict:
    sys.exit(42)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.secrets)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertTrue(self.api.get("/api/stages").json()["presets"][0]["active"])

    def test_operation_history_is_bounded(self):
        for index in range(130):
            llm.update_settings(lambda data: None, operation_id=f"operation-{index}")
        self.assertEqual(len(self.read()["settings_operations"]), 128)
        result = self.api.get("/api/stages", params={"operation_id": "operation-0"}).json()
        self.assertEqual(result["operation"]["status"], "unconfirmed")
