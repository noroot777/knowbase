"""只读阅读器点检夹具：生产文章模板、可控慢图和本地拒绝嵌入页。"""

import json
import sys
import threading
from argparse import ArgumentParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import fixture_life  # noqa: E402
from kb.web import pages  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "kb" / "static"


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18883)
    args = parser.parse_args()
    image_ready = threading.Event()

    class Handler(fixture_life.TouchingHandlerMixin, SimpleHTTPRequestHandler):
        def respond(self, data, content_type, status=200, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/blocked":
                self.respond(b"Embedding denied", "text/html", headers={"X-Frame-Options": "DENY"})
            elif path == "/release-image":
                image_ready.set()
                self.respond(b"ok", "text/plain")
            elif path == "/hold-image":
                image_ready.clear()
                self.respond(b"ok", "text/plain")
            elif path == "/slow-image.svg":
                image_ready.wait(20)
                self.respond(b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>', "image/svg+xml")
            elif path.startswith("/doc/"):
                markdown = f"[外链](http://localhost:{args.port}/blocked)\n\n[页内锚点](#section)"
                if path == "/doc/slow.md":
                    markdown += "\n\n![等待中的图片](/slow-image.svg)"
                rendered = pages._DOC_TMPL.format(
                    title="阅读器关闭点检", meta="本地隔离夹具", md_json=pages._script_json(markdown),
                    path_json=json.dumps(path[5:]), version_json='"fixture"',
                    literal_js=pages._DOC_LITERAL_JS, links_js=pages._DOC_LINKS_JS,
                    snip_js=pages._DOC_SNIP_JS,
                )
                self.respond(rendered.encode(), "text/html; charset=utf-8")
            elif path == "/" or path.startswith("/static/"):
                target = (STATIC / ("index.html" if path == "/" else path[8:])).resolve()
                if target.is_relative_to(STATIC) and target.is_file():
                    self.respond(target.read_bytes(), self.guess_type(str(target)))
                else:
                    self.send_error(404)
            elif path.startswith("/api/"):
                self.respond(b'{"detail":"Read-only reader fixture"}', "application/json", 503)
            else:
                self.send_error(404)

        def do_POST(self):
            self.send_error(405, "Reader fixture is read-only")

        do_PUT = do_PATCH = do_DELETE = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    fixture_life.announce(f"Read-only reader fixture: http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        image_ready.set()
        server.server_close()


if __name__ == "__main__":
    main()
