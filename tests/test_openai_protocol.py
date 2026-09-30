"""协议配置与真实 OpenAI SDK 的请求、响应、SSE 和关闭行为;不访问网络。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import OpenAI

from kb import llm
from kb.web import settings_api


class ReplyStream(httpx.SyncByteStream):
    def __init__(self, payload: bytes):
        self.payload = payload
        self.closed = False

    def __iter__(self):
        yield self.payload

    def close(self):
        self.closed = True


def response_body(text="协议正常", status="completed"):
    return {
        "id": "resp_test", "object": "response", "model": "test-model",
        "created_at": 0, "status": status,
        "error": {"code": "server_error", "message": "上游失败"} if status == "failed" else None,
        "output": [{"id": "msg_test", "type": "message", "role": "assistant",
                    "status": "completed", "content": [{"type": "output_text",
                    "text": text, "annotations": []}]}],
    }


class OpenaiProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.secrets = Path(temporary.name) / "secrets.json"
        path_patch = mock.patch.object(llm, "_SECRETS_PATH", self.secrets)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        data_patch = mock.patch.object(llm, "DATA_DIR", self.secrets.parent)
        data_patch.start()
        self.addCleanup(data_patch.stop)
        environment = {name: value for name, value in os.environ.items()
                       if name not in (*llm._ENV_KEY.values(), *llm._ENV_URL.values())}
        env_patch = mock.patch.dict(os.environ, environment, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        clients_patch = mock.patch.object(llm, "_clients", {})
        clients_patch.start()
        self.addCleanup(clients_patch.stop)
        self.write({"openai": {"key": "sk-test-not-real", "models": ["test-model"]}})
        app = FastAPI()
        app.include_router(settings_api.router)
        self.api = TestClient(app)
        self.addCleanup(self.api.close)
        self.requests = []
        self.streams = []
        self.events = None
        self.failed = False
        self.response_status = "completed"
        sdk = OpenAI(api_key="sk-test-not-real", base_url="http://provider.test/v1",
                     max_retries=0,
                     http_client=httpx.Client(transport=httpx.MockTransport(self.upstream)))
        self.addCleanup(sdk.close)
        sdk_patch = mock.patch.object(llm, "_openai", return_value=sdk)
        sdk_patch.start()
        self.addCleanup(sdk_patch.stop)

    def write(self, value):
        self.secrets.write_text(json.dumps(value), encoding="utf-8")

    def set_conversion(self, enabled):
        result = self.api.post("/api/keys", json={"openai_protocol_conversion_enabled": enabled})
        self.assertEqual(result.status_code, 200)
        self.assertIs(result.json()["keys"]["openai"]["protocol_conversion_enabled"], enabled)

    def upstream(self, request):
        if request.method == "GET":
            self.requests.append((request.url.path, None))
            return httpx.Response(200, json={"data": [{"id": "test-model", "object": "model"}]})
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if self.failed:
            return httpx.Response(503, json={"error": {"message": "网关故障"}})
        is_chat = request.url.path.endswith("/chat/completions")
        if not body.get("stream"):
            payload = {"id": "chatcmpl-test", "object": "chat.completion", "model": "test-model",
                       "created": 0, "choices": [{"index": 0, "finish_reason": "stop",
                       "message": {"role": "assistant", "content": " 协议正常 "}}]}
            return httpx.Response(200, json=payload if is_chat else
                                  response_body(" 协议正常 ", status=self.response_status))
        events = self.events
        if events is None:
            if is_chat:
                events = [{"id": "chatcmpl-test", "object": "chat.completion.chunk",
                           "model": "test-model", "created": 0,
                           "choices": [{"index": 0, "delta": {"content": text}}]}
                          for text in ("协议", "正常")]
                events.append({"choices": [], "usage": {"total_tokens": 2}})
            else:
                events = [{"type": "response.created", "response": response_body("")},
                          {"type": "response.reasoning_summary_text.delta", "delta": "不应显示"},
                          {"type": "response.output_text.delta", "delta": "协议"},
                          {"type": "response.output_text.delta", "delta": "正常"},
                          {"type": "response.output_text.done", "text": "协议正常"},
                          {"type": "response.completed", "response": response_body()}]
        payload = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)
        stream = ReplyStream((payload + "data: [DONE]\n\n").encode())
        self.streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    def test_legacy_and_missing_configs_keep_chat_completions(self):
        for entry in ({"key": "sk-test-not-real"}, "sk-test-not-real", {}):
            with self.subTest(entry=entry):
                self.write({"openai": entry})
                self.assertTrue(llm.openai_conversion_enabled())
                status = self.api.get("/api/keys").json()["keys"]["openai"]
                self.assertTrue(status["protocol_conversion_enabled"])

    def test_toggle_persists_without_exposing_or_erasing_credentials(self):
        self.set_conversion(False)
        saved = json.loads(self.secrets.read_text())
        self.assertEqual(saved["openai"]["key"], "sk-test-not-real")
        self.assertEqual(saved["openai"]["models"], ["test-model"])
        self.assertEqual(self.secrets.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("sk-test-not-real", self.api.get("/api/keys").text)
        self.api.post("/api/keys", json={"openai_base_url": "http://provider.test/v1"})
        self.assertFalse(llm.openai_conversion_enabled())
        self.set_conversion(True)
        self.assertTrue(llm.openai_conversion_enabled())

    def test_missing_or_null_field_does_not_change_toggle(self):
        self.set_conversion(False)
        for body in ({}, {"openai_protocol_conversion_enabled": None}):
            self.assertEqual(self.api.post("/api/keys", json=body).status_code, 200)
            self.assertFalse(llm.openai_conversion_enabled())

    def test_boolean_validation_and_client_cache_invalidation(self):
        for value in ("false", "true", 0, 1, [], {}):
            with self.subTest(value=value):
                result = self.api.post("/api/keys", json={"openai_protocol_conversion_enabled": value})
                self.assertEqual(result.status_code, 422)
        with self.assertRaises(ValueError):
            llm.set_config({"openai": {"protocol_conversion_enabled": "false"}})
        llm._clients["openai"] = object()
        self.set_conversion(False)
        self.assertEqual(llm._clients, {})

    def test_environment_credentials_do_not_lock_protocol_toggle(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "env-secret",
                                         "OPENAI_BASE_URL": "http://env.test/v1"}):
            self.set_conversion(False)
            status = self.api.get("/api/keys").json()["keys"]["openai"]
            self.assertEqual(status["source"], "env")
            self.assertEqual(status["base_url_source"], "env")
            self.assertFalse(status["protocol_conversion_enabled"])

    def test_conversion_does_not_modify_anthropic(self):
        llm.set_config({"anthropic": {"key": "anthropic-test"}})
        self.set_conversion(False)
        self.assertEqual(json.loads(self.secrets.read_text())["anthropic"], {"key": "anthropic-test"})
        self.assertNotIn("protocol_conversion_enabled", llm.key_status()["anthropic"])

    def test_nonstream_endpoints_and_request_parameter_conversion(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.set_conversion(enabled)
                self.assertEqual(llm.chat("中文提示", "openai:test-model", max_tokens=20000), "协议正常")
                endpoint, body = self.requests[-1]
                self.assertEqual(body["model"], "test-model")
                self.assertIs(body["stream"], False)
                self.assertNotIn("temperature", body)
                if enabled:
                    self.assertEqual(endpoint, "/v1/chat/completions")
                    self.assertEqual(body["messages"], [{"role": "user", "content": "中文提示"}])
                    self.assertEqual(body["max_completion_tokens"], 20000)
                    self.assertNotIn("input", body)
                else:
                    self.assertEqual(endpoint, "/v1/responses")
                    self.assertEqual(body["input"], "中文提示")
                    self.assertEqual(body["max_output_tokens"], 20000)
                    self.assertIs(body["store"], False)
                    self.assertNotIn("messages", body)

    def test_both_streams_emit_only_text_and_close_on_completion(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.set_conversion(enabled)
                self.assertEqual(list(llm.chat_stream("提示", "openai:test-model")), ["协议", "正常"])
                endpoint, body = self.requests[-1]
                self.assertEqual(endpoint, "/v1/chat/completions" if enabled else "/v1/responses")
                self.assertTrue(body["stream"])
                self.assertEqual(body["max_completion_tokens" if enabled else "max_output_tokens"], 16000)
                self.assertTrue(self.streams[-1].closed)

    def test_both_streams_close_when_consumer_disconnects(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.set_conversion(enabled)
                generator = llm.chat_stream("提示", "openai:test-model")
                self.assertEqual(next(generator), "协议")
                self.assertFalse(self.streams[-1].closed)
                generator.close()
                self.assertTrue(self.streams[-1].closed)

    def test_responses_stream_failure_and_error_events_are_reported(self):
        self.set_conversion(False)
        for event in ({"type": "error", "message": "上游失败", "code": "server_error"},
                      {"type": "response.failed", "response": response_body(status="failed")}):
            with self.subTest(event=event["type"]):
                self.events = [event]
                with self.assertRaisesRegex(RuntimeError, "GPT 调用失败.*上游失败"):
                    list(llm.chat_stream("提示", "openai:test-model"))
                self.assertTrue(self.streams[-1].closed)

    def test_responses_nonstream_failure_is_reported(self):
        self.set_conversion(False)
        self.response_status = "failed"
        with self.assertRaisesRegex(RuntimeError, "GPT 调用失败.*上游失败"):
            llm.chat("提示", "openai:test-model")

    def test_http_errors_do_not_retry_using_another_protocol(self):
        self.failed = True
        for enabled in (True, False):
            self.set_conversion(enabled)
            for streaming in (True, False):
                before = len(self.requests)
                with self.assertRaisesRegex(RuntimeError, "GPT 调用失败.*网关故障"):
                    if streaming:
                        list(llm.chat_stream("提示", "openai:test-model"))
                    else:
                        llm.chat("提示", "openai:test-model")
                self.assertEqual(len(self.requests), before + 1)

    def test_model_discovery_is_independent_of_protocol(self):
        for enabled in (True, False):
            self.set_conversion(enabled)
            self.assertEqual(llm.list_remote_models("openai"), ["test-model"])
            self.assertEqual(self.requests[-1][0], "/v1/models")


if __name__ == "__main__":
    unittest.main()
