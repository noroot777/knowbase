"""OpenAI 兼容的本地桩:只为把日报/翻译的整条链路跑通,不连外网、不花钱。

    python3 tests/llm_stub.py --port 8801 --reply "..."
"""
import json
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = ''
SEEN = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        SEEN.append(json.loads(body or b'{}'))
        payload = json.dumps({
            'id': 'stub', 'object': 'chat.completion', 'created': 0, 'model': 'stub',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': REPLY}}],
        }).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        payload = json.dumps({'calls': len(SEEN), 'prompts': [
            m['messages'][0]['content'] for m in SEEN if m.get('messages')]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8801)
    parser.add_argument('--reply', default='## 今日要点\n\n- **桩回答**:验证链路 [1]')
    args = parser.parse_args()
    global REPLY
    REPLY = args.reply
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
