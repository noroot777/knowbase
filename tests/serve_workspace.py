"""Read-only worktree preview; never starts a scheduler or forwards writes.

python3 tests/serve_workspace.py --upstream http://127.0.0.1:8787
Omit --upstream when running the fully mocked workspace_probe.js checks.
"""

import sys
from argparse import ArgumentParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 起了不关就是孤儿:什么时候自行退出,口径和理由都在 tests/fixture_life.py。
# **必须在模块加载时就 import** —— 所有权看门狗装在它的模块级。
from tests import fixture_life  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "kb" / "static"


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18877)
    parser.add_argument("--upstream", default="", help="Optional read-only backend URL")
    args = parser.parse_args()

    class Handler(fixture_life.TouchingHandlerMixin, SimpleHTTPRequestHandler):
        def send_content(self, data, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/" or path.startswith("/static/"):
                target = (STATIC / ("index.html" if path == "/" else path[8:])).resolve()
                if target.is_relative_to(STATIC) and target.is_file():
                    self.send_content(target.read_bytes(), self.guess_type(str(target)))
                else:
                    self.send_error(404)
                return
            if not path.startswith(("/api/", "/doc/", "/graph")):
                self.send_error(404)
                return
            if not args.upstream:
                self.send_content(b'{"detail":"Preview has no backend"}', "application/json", 503)
                return
            try:
                with urlopen(args.upstream.rstrip("/") + self.path, timeout=90) as response:
                    self.send_content(response.read(), response.headers.get_content_type(), response.status)
            except HTTPError as error:
                self.send_content(error.read(), error.headers.get_content_type(), error.code)
            except OSError:
                self.send_error(502)

        def do_POST(self):
            self.send_error(405, "Preview is read-only")

        do_PUT = do_PATCH = do_DELETE = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    fixture_life.install()
    print(f"Read-only preview: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
