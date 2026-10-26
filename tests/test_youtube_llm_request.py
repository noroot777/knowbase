import json
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from kb.youtube import llm_request, media


class YouTubeModelDeadline(unittest.TestCase):
    def test_deadline_and_stop_reap_the_running_request_process(self):
        original = subprocess.Popen
        child_code = 'import sys,json,time; json.load(sys.stdin); time.sleep(20)'
        selected = SimpleNamespace(provider='openai', model_id='fixture')
        for stop in (False, True):
            processes = []

            def launch(arguments, **values):
                child = original([sys.executable, '-c', child_code], **values)
                processes.append(child)
                return child

            started = time.monotonic()
            stopped = lambda: stop and time.monotonic() - started > .15
            deadline = started + (5 if stop else .3)
            with self.subTest(stop=stop), patch.object(llm_request.subprocess, 'Popen', side_effect=launch):
                with self.assertRaises(media.Interrupted):
                    llm_request.request('fixture prompt', selected, deadline, stopped)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertIsNotNone(processes[0].poll())
            for stream in (processes[0].stdin, processes[0].stdout, processes[0].stderr):
                self.assertTrue(stream.closed)

    def test_request_result_and_shared_absolute_deadline(self):
        original = subprocess.Popen
        selected = SimpleNamespace(provider='openai', model_id='fixture')
        code = 'import sys,json; json.load(sys.stdin); print(json.dumps({"text":"完整中文结果"}))'
        with patch.object(llm_request.subprocess, 'Popen', side_effect=lambda arguments, **values:
                          original([sys.executable, '-c', code], **values)):
            deadline = time.monotonic() + 5
            self.assertEqual('完整中文结果', llm_request.request('fixture', selected, deadline, lambda: False))
            with self.assertRaises(media.Interrupted):
                llm_request.request('next chunk', selected, time.monotonic() - .1, lambda: False)

    def local_model(self, mode):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *arguments):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                requests.append(self.path)
                if mode == 'error':
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"fixture error","type":"server_error"}}')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                events = [
                    {'type': 'message_start', 'message': {'id': 'msg_fixture', 'type': 'message',
                     'role': 'assistant', 'content': [], 'model': 'fixture', 'stop_reason': None,
                     'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 0}}},
                    {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}]
                events += [{'type': 'content_block_delta', 'index': 0,
                            'delta': {'type': 'text_delta', 'text': '持续输出但始终不结束。'}}] * 200
                try:
                    for event in events:
                        self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
                        self.wfile.flush()
                        time.sleep(.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}'
        return url, requests

    def test_sdk_retries_are_disabled_for_retryable_errors(self):
        url, requests = self.local_model('error')
        selected = SimpleNamespace(provider='openai', model_id='fixture')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fixture', 'OPENAI_BASE_URL': url + '/v1',
                                     'NO_PROXY': '127.0.0.1,localhost'}):
            with self.assertRaisesRegex(RuntimeError, 'fixture error'):
                llm_request.request('fixture', selected, time.monotonic() + 8, lambda: False)
        self.assertEqual(1, len(requests))

    def test_continuous_anthropic_stream_cannot_extend_deadline(self):
        url, requests = self.local_model('stream')
        selected = SimpleNamespace(provider='anthropic', model_id='fixture')
        started = time.monotonic()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fixture', 'ANTHROPIC_BASE_URL': url,
                                     'NO_PROXY': '127.0.0.1,localhost'}):
            with self.assertRaises(media.Interrupted):
                llm_request.request('fixture', selected, started + 2, lambda: False)
        self.assertTrue(requests)
        self.assertLess(time.monotonic() - started, 3.5)
