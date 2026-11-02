import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from kb import llm_openai
from kb.youtube import llm_request, model_response


class YouTubeModelResponse(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = []
        self.requests = []

    def ask(self, protocol, reason, *, terminal=True, refusal=False):
        paths = self.paths
        requests = self.requests
        text = '接口预算为六十四。'

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *arguments):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                paths.append(self.path)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if protocol == 'anthropic'
                                 else 'application/json')
                self.end_headers()
                if protocol == 'anthropic':
                    events = [
                        {'type': 'message_start', 'message': {'id': 'msg_fixture', 'type': 'message',
                         'role': 'assistant', 'model': 'fixture', 'content': [], 'stop_reason': None,
                         'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 0}}},
                        {'type': 'content_block_start', 'index': 0,
                         'content_block': {'type': 'text', 'text': ''}},
                        {'type': 'content_block_delta', 'index': 0,
                         'delta': {'type': 'text_delta', 'text': text}},
                        {'type': 'content_block_stop', 'index': 0},
                        {'type': 'message_delta', 'delta': {'stop_reason': reason, 'stop_sequence': None},
                         'usage': {'output_tokens': 10}}]
                    if terminal:
                        events.append({'type': 'message_stop'})
                    for event in events:
                        self.wfile.write(('event: ' + event['type'] + '\ndata: ' +
                                          json.dumps(event) + '\n\n').encode())
                elif protocol == 'chat':
                    payload = {'id': 'chat_fixture', 'object': 'chat.completion', 'created': 0,
                               'model': 'fixture', 'choices': [{'index': 0, 'finish_reason': reason,
                               'message': {'role': 'assistant', 'content': text,
                                           'refusal': '拒绝' if refusal else None}}]}
                    self.wfile.write(json.dumps(payload).encode())
                else:
                    content = [{'type': 'refusal', 'refusal': '拒绝'}] if refusal else [
                        {'type': 'output_text', 'text': text, 'annotations': []}]
                    payload = {'id': 'resp_fixture', 'object': 'response', 'created_at': 0,
                               'model': 'fixture', 'status': reason,
                               'incomplete_details': {'reason': 'max_output_tokens'}
                               if reason == 'incomplete' else None,
                               'output': [{'type': 'message', 'id': 'msg_fixture',
                                           'role': 'assistant', 'status': 'completed', 'content': content}]}
                    self.wfile.write(json.dumps(payload).encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        (self.root / 'secrets.json').write_text(json.dumps({
            'openai': {'protocol_conversion_enabled': protocol == 'chat'}}))
        selected = SimpleNamespace(provider='anthropic' if protocol == 'anthropic' else 'openai',
                                   model_id='fixture')
        with patch.dict(os.environ, {'KB_DATA_DIR': str(self.root), 'OPENAI_API_KEY': 'fixture',
                                     'OPENAI_BASE_URL': url + '/v1', 'ANTHROPIC_API_KEY': 'fixture',
                                     'ANTHROPIC_BASE_URL': url, 'NO_PROXY': '127.0.0.1,localhost'}):
            return llm_request.request('fixture', selected, time.monotonic() + 8, lambda: False)

    def test_legacy_text_extraction_discards_an_explicit_incomplete_status(self):
        partial = SimpleNamespace(status='incomplete', incomplete_details=SimpleNamespace(
            reason='max_output_tokens'), output_text='只有开头一句。')
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **values: partial))
        self.assertEqual('只有开头一句。', llm_openai.complete(client, 'fixture', '原文', 6000, False))

    def test_chat_non_stop_reasons_never_cross_the_real_request_process(self):
        for reason in ('length', 'content_filter', 'tool_calls', None):
            with self.subTest(reason=reason), self.assertRaisesRegex(model_response.OutputError, '未正常完成'):
                self.ask('chat', reason)
        self.assertEqual(['/v1/chat/completions'] * 4, self.paths)

    def test_responses_incomplete_failed_and_unknown_status_are_not_text_success(self):
        for reason in ('incomplete', 'failed', 'in_progress', None):
            with self.subTest(reason=reason), self.assertRaisesRegex(model_response.OutputError, '未正常完成'):
                self.ask('responses', reason)
        self.assertEqual(['/v1/responses'] * 4, self.paths)

    def test_anthropic_truncation_refusal_and_missing_terminal_event_are_rejected(self):
        for reason in ('max_tokens', 'refusal', 'pause_turn', None):
            with self.subTest(reason=reason), self.assertRaisesRegex(model_response.OutputError, '未正常完成'):
                self.ask('anthropic', reason)
        with self.assertRaisesRegex(model_response.OutputError, '未正常完成'):
            self.ask('anthropic', 'end_turn', terminal=False)
        self.assertEqual(['/v1/messages'] * 5, self.paths)

    def test_all_protocols_accept_normal_completion_and_natural_chinese(self):
        for protocol, reason, field in (('chat', 'stop', 'max_completion_tokens'),
                                         ('responses', 'completed', 'max_output_tokens'),
                                         ('anthropic', 'end_turn', 'max_tokens')):
            with self.subTest(protocol=protocol):
                self.assertEqual('接口预算为六十四。', self.ask(protocol, reason))
                self.assertEqual(16000, self.requests[-1][field])

    def test_refusal_is_not_a_success_even_with_a_normal_status(self):
        for protocol, reason in (('chat', 'stop'), ('responses', 'completed')):
            with self.subTest(protocol=protocol), self.assertRaisesRegex(model_response.OutputError, '拒绝|非正文'):
                self.ask(protocol, reason, refusal=True)
