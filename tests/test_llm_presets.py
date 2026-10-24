"""整组模型预设:档位、原子保存、供应商边界和面板接口。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import llm, llm_presets, llm_stages
from kb.web import settings_api


class ModelPresetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.secrets = Path(temporary.name) / "secrets.json"
        for name, value in (("_SECRETS_PATH", self.secrets), ("DATA_DIR", self.secrets.parent),
                            ("_clients", {})):
            context = patch.object(llm, name, value)
            context.start()
            self.addCleanup(context.stop)
        excluded = {*llm._ENV_KEY.values(), *llm._ENV_URL.values(),
                    *(stage.env for stage in llm_stages.STAGES)}
        context = patch.dict(os.environ, {name: value for name, value in os.environ.items()
                                         if name not in excluded}, clear=True)
        context.start()
        self.addCleanup(context.stop)
        self.write({
            "anthropic": {"key": "sk-fake-a", "base_url": "https://anthropic.invalid",
                          "models": ["claude-sonnet-4-6", "claude-haiku-4-5"]},
            "openai": {"key": "sk-fake-o", "base_url": "https://openai.invalid/v1",
                       "models": ["gpt-5.5", "gpt-5.3-codex-spark"]},
            "unrelated": {"keep": True},
        })
        app = FastAPI()
        app.include_router(settings_api.router)
        self.api = TestClient(app)
        self.addCleanup(self.api.close)

    def read(self):
        return json.loads(self.secrets.read_text(encoding="utf-8"))

    def write(self, data):
        self.secrets.write_text(json.dumps(data), encoding="utf-8")

    def presets(self):
        return {preset["key"]: preset for preset in llm_presets.describe()}

    def test_claude_assigns_every_stage_by_tier_with_one_write(self):
        original = self.read()
        with patch.object(llm, "_save_secrets", wraps=llm._save_secrets) as save:
            preset = llm_presets.apply("claude")
        self.assertEqual(save.call_count, 1)
        self.assertEqual(preset["preserved"], [])
        self.assertEqual(set(self.read()["stages"]), {stage.key for stage in llm_stages.STAGES})
        for stage in llm_stages.STAGES:
            expected = "claude-haiku-4-5" if stage.rec == "cheap" else "claude-sonnet-4-6"
            self.assertEqual(llm_stages.configured(stage.key),
                             {"provider": "anthropic", "model": expected})
        for key, value in original.items():
            self.assertEqual(self.read()[key], value)
        self.assertTrue(self.presets()["claude"]["active"])
        self.assertEqual(self.secrets.stat().st_mode & 0o777, 0o600)

    def test_gpt_assigns_supported_stages_and_preserves_agent(self):
        llm_stages.set_stage("agent", "anthropic", "claude-sonnet-4-6")
        preset = llm_presets.apply("gpt")
        self.assertEqual(preset["preserved"], [llm_stages.stage("agent").label])
        self.assertNotIn("agent", preset["assignments"])
        for stage in llm_stages.STAGES:
            entry = llm_stages.configured(stage.key)
            if stage.key == "agent":
                self.assertEqual(entry, {"provider": "anthropic", "model": "claude-sonnet-4-6"})
            else:
                expected = "gpt-5.3-codex-spark" if stage.rec == "cheap" else "gpt-5.5"
                self.assertEqual(entry, {"provider": "openai", "model": expected})
        self.assertTrue(self.presets()["gpt"]["active"])
        self.assertFalse(self.presets()["claude"]["active"])
        self.assertEqual(llm.get_fav_model(), "openai:gpt-5.5")

    def test_gpt_does_not_require_or_invent_anthropic_configuration(self):
        data = self.read()
        data.pop("anthropic")
        self.write(data)
        llm_presets.apply("gpt")
        self.assertNotIn("agent", self.read()["stages"])
        self.assertIsNone(llm_stages.resolve_stage("agent"))
        self.assertTrue(self.presets()["gpt"]["active"])

    def test_legacy_values_removed_only_for_changed_stages(self):
        data = self.read()
        data.update({"default_model": "claude-sonnet-4-6", "plan_model": "claude-haiku-4-5",
                     "fav_model": "claude-sonnet-4-6", "agent_model": "claude-sonnet-4-6"})
        self.write(data)
        llm_presets.apply("gpt")
        for key in ("default_model", "plan_model", "fav_model"):
            self.assertNotIn(key, self.read())
        self.assertEqual(self.read()["agent_model"], "claude-sonnet-4-6")

    def test_missing_key_disables_and_rejects_preset_without_writing(self):
        llm.set_config({"openai": {"key": ""}})
        before = self.secrets.read_bytes()
        preset = self.presets()["gpt"]
        self.assertFalse(preset["ready"])
        self.assertIn("API Key", preset["reason"])
        response = self.api.post("/api/stages/preset", json={"preset": "gpt"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.secrets.read_bytes(), before)

    def test_foreign_family_is_not_silently_selected(self):
        llm.set_models("openai", ["claude-sonnet-4-6", "unrelated-model"])
        before = self.secrets.read_bytes()
        self.assertFalse(self.presets()["gpt"]["ready"])
        with self.assertRaisesRegex(ValueError, "勾选"):
            llm_presets.apply("gpt")
        self.assertEqual(self.secrets.read_bytes(), before)

    def test_same_id_in_both_providers_keeps_explicit_provider(self):
        llm.set_models("anthropic", ["claude-sonnet-4-6", "gpt-5.5"])
        llm_presets.apply("gpt")
        self.assertEqual(llm_stages.configured("ask")["provider"], "openai")
        self.assertEqual(llm.get_default_model(), "openai:gpt-5.5")

    def test_missing_cheap_model_reuses_available_family_model(self):
        llm.set_models("openai", ["gpt-5.5"])
        llm_presets.apply("gpt")
        self.assertEqual(llm_stages.configured("plan"), {"provider": "openai", "model": "gpt-5.5"})
        self.assertEqual(self.presets()["gpt"]["cheap_model"], "gpt-5.5")

    def test_missing_cheap_model_prefers_main_over_first_candidate(self):
        llm.set_models("anthropic", ["claude-opus-4-8", "claude-sonnet-4-6"])
        llm_presets.apply("claude")
        self.assertEqual(llm_stages.configured("plan")["model"], "claude-sonnet-4-6")

    def test_restricted_stage_recommendation_cannot_use_foreign_candidates(self):
        self.assertEqual(llm_stages.recommended(llm_stages.stage("agent"),
                                               [("openai", "gpt-5.5")]), "")

    def test_unconfigured_provider_model_list_uses_existing_defaults(self):
        llm.set_models("openai", [])
        llm_presets.apply("gpt")
        for entry in self.presets()["gpt"]["assignments"].values():
            self.assertIn(entry["model"], llm.configured_models("openai"))

    def test_apply_rebuilds_from_current_candidates(self):
        self.presets()
        llm.set_models("openai", ["relay/gpt-5.5", "relay/gpt-mini"])
        llm_presets.apply("gpt")
        self.assertEqual(llm_stages.configured("ask")["model"], "relay/gpt-5.5")
        self.assertEqual(llm_stages.configured("gate")["model"], "relay/gpt-mini")

    def test_manual_tweak_leaves_preset_and_reapply_restores_it(self):
        llm_presets.apply("claude")
        self.api.post("/api/stages", json={"key": "ask", "provider": "openai", "model": "gpt-5.5"})
        self.assertFalse(self.presets()["claude"]["active"])
        self.assertFalse(self.presets()["gpt"]["active"])
        llm_presets.apply("claude")
        self.assertTrue(self.presets()["claude"]["active"])

    def test_panel_overrides_environment_without_changing_environment(self):
        with patch.dict(os.environ, {"KB_ASK_MODEL": "anthropic:claude-haiku-4-5"}):
            llm_presets.apply("gpt")
            self.assertEqual(llm.get_default_model(), "openai:gpt-5.5")
            self.assertEqual(os.environ["KB_ASK_MODEL"], "anthropic:claude-haiku-4-5")

    def test_invalid_stage_or_provider_rejects_entire_batch(self):
        for key, entry in (("not-a-stage", {"provider": "openai", "model": "gpt-5.5"}),
                           ("agent", {"provider": "openai", "model": "gpt-5.5"})):
            with self.subTest(key=key):
                before = self.secrets.read_bytes()
                with self.assertRaises((KeyError, ValueError)):
                    llm_stages.set_stages({"ask": {"provider": "openai", "model": "gpt-5.5"},
                                           key: entry})
                self.assertEqual(self.secrets.read_bytes(), before)

    def test_api_exposes_preview_and_refreshes_every_stage(self):
        response = self.api.get("/api/stages")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["presets"]), 2)
        self.assertNotIn("sk-fake", response.text)
        response = self.api.post("/api/stages/preset", json={"preset": "gpt"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["applied_preset"], "GPT 默认")
        self.assertEqual(len(body["stages"]), len(llm_stages.STAGES))
        self.assertTrue(next(preset for preset in body["presets"] if preset["key"] == "gpt")["active"])
        for stage in body["stages"]:
            if stage["key"] != "agent":
                self.assertTrue(stage["recommend"].startswith("openai:gpt-"))
        self.assertEqual(self.api.get("/api/models").json()["default"], "openai:gpt-5.5")

    def test_unknown_preset_and_malformed_request_do_not_change_settings(self):
        before = self.secrets.read_bytes()
        response = self.api.post("/api/stages/preset", json={"preset": "missing"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.api.post("/api/stages/preset", json={}).status_code, 422)
        self.assertEqual(self.secrets.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
