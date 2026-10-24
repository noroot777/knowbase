"""隔离模型预设浏览器验证:临时设置、虚构凭证、无调度或外部模型调用。"""

from argparse import ArgumentParser
import contextlib
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import fixture_life  # noqa: E402


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18881)
    args = parser.parse_args()
    from tests.fixture_root import fixture_root

    with contextlib.ExitStack() as stack:
        temporary = fixture_root(stack, "kb-presets-qa-")
        os.environ["KB_DATA_DIR"] = str(Path(temporary) / "data")
        os.environ["KB_VAULT_ROOT"] = str(Path(temporary) / "vault")
        for name in tuple(os.environ):
            if name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_BASE_URL",
                        "OPENAI_BASE_URL") or (name.startswith("KB_") and name.endswith("_MODEL")):
                os.environ.pop(name, None)

        from fastapi import FastAPI
        import uvicorn
        from kb import llm, llm_presets
        from kb.web import pages, settings_api

        llm.set_config({
            "anthropic": {"key": "sk-fake-presets-a", "base_url": "http://provider.invalid",
                          "models": ["claude-sonnet-4-6", "claude-haiku-4-5"]},
            "openai": {"key": "sk-fake-presets-o", "base_url": "http://provider.invalid/v1",
                       "models": ["gpt-5.5", "gpt-5.3-codex-spark"]},
        })
        llm_presets.apply("claude")
        app = FastAPI()

        @app.get("/api/mcp/status")
        def mcp_status():
            return {"config": "{}", "command": "", "tools": [], "cli_available": False}

        app.include_router(settings_api.router)
        app.include_router(pages.router)

        @app.get("/api/favorites/settings")
        def favorite_settings():
            return {"model": llm.get_fav_model()}

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

        fixture_life.announce("Isolated model presets QA: fake credentials, no external calls")
        uvicorn.run(fixture_life.guard(app), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
