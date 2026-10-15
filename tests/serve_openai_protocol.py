"""隔离协议设置浏览器验证;临时配置,无真实凭证、外部网络或调度线程。"""

from argparse import ArgumentParser
import contextlib
import os
from pathlib import Path
import sys


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18879)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from tests.fixture_root import fixture_root
    # 临时配置**优先由父进程拥有**:口径和理由都在 tests/fixture_root.py
    with contextlib.ExitStack() as stack:
        temporary = fixture_root(stack, "kb-openai-qa-")
        os.environ["KB_DATA_DIR"] = str(Path(temporary) / "data")
        os.environ["KB_VAULT_ROOT"] = str(Path(temporary) / "vault")
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            os.environ.pop(name, None)
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
        from openai import OpenAI
        import uvicorn
        from kb import llm
        from kb.web import settings_api

        def upstream(request):
            if request.method == "GET" and request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "test-model", "object": "model"}]})
            raise RuntimeError("隔离验证环境禁止外部模型请求")

        sdk = OpenAI(api_key="sk-fake-browser-qa", base_url="http://fixture.test/v1",
                     http_client=httpx.Client(transport=httpx.MockTransport(upstream)))
        llm._openai = lambda: sdk
        llm.set_config({"openai": {"key": "sk-fake-browser-qa", "models": ["test-model"],
                                    "base_url": "http://fixture.test/v1"}})
        app = FastAPI()

        @app.get("/api/mcp/status")
        def mcp_status():
            return {"config": "{}", "command": "", "tools": [], "cli_available": False}

        app.include_router(settings_api.router)
        app.mount("/static", StaticFiles(directory=root / "kb/static"), name="static")

        @app.get("/")
        def index():
            return FileResponse(root / "kb/static/index.html")

        @app.get("/api/{path:path}")
        def fixture(path: str):
            return {
                "stats": {"documents": 0, "vectors": 0, "entities": 0, "extracted": 0},
                "facets": {"sources": [], "accounts": [], "topics": [], "entities": []},
                "projects": {"tools": [], "kinds": {}, "total_entries": 0, "topics": []},
                "favorites": {"doc_paths": [], "favorites": [], "counts": {"inbox": 0}},
                "conversations": {"conversations": []},
                "wechat/setup": {"state": "unchecked", "running": False,
                                  "message": "隔离验证环境", "provider_ready": True},
            }.get(path, {})

        print("Isolated OpenAI protocol QA: temporary settings, no external requests", flush=True)
        try:
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            sdk.close()


if __name__ == "__main__":
    main()
