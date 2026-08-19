# -*- coding: utf-8 -*-
"""设置面板后端:问答模型选择、各家 API key、MCP server 注册。

key 只落 kb.llm 的配置文件,读接口一律脱敏,绝不回传明文。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from kb import llm

router = APIRouter()


@router.get("/api/models")
def api_models():
    """问答可选模型列表(含各 provider 是否已配 API key),给前端下拉用。"""
    return {"models": llm.available_models(), "default": llm.get_default_model()}


@router.get("/api/models/remote")
def api_models_remote(provider: str):
    """从中转站拉取可用模型 id(需该家已配 key+base_url),给面板勾选用。"""
    try:
        return {"provider": provider, "models": llm.list_remote_models(provider)}
    except RuntimeError as e:
        raise HTTPException(502, str(e))


class ModelsSelectRequest(BaseModel):
    provider: str
    models: list[str]


@router.post("/api/models/select")
def api_models_select(body: ModelsSelectRequest):
    """保存某家要在下拉里暴露的模型 id 列表。"""
    llm.set_models(body.provider, body.models)
    return {"models": llm.available_models(), "default": llm.get_default_model()}


class KeysRequest(BaseModel):
    # 每个字段 None=不动;""=清除;有值=设置。base_url 用于中转站(留空=官方)。
    anthropic_key: str | None = None
    anthropic_base_url: str | None = None
    openai_key: str | None = None
    openai_base_url: str | None = None


@router.get("/api/keys")
def api_keys():
    """各 provider 的 key 状态(key 脱敏、绝不回传明文;base_url 原样),给设置面板用。"""
    return {"keys": llm.key_status()}


@router.post("/api/keys")
def api_keys_set(body: KeysRequest):
    updates: dict[str, dict] = {"anthropic": {}, "openai": {}}
    if body.anthropic_key is not None:
        updates["anthropic"]["key"] = body.anthropic_key
    if body.anthropic_base_url is not None:
        updates["anthropic"]["base_url"] = body.anthropic_base_url
    if body.openai_key is not None:
        updates["openai"]["key"] = body.openai_key
    if body.openai_base_url is not None:
        updates["openai"]["base_url"] = body.openai_base_url
    llm.set_config(updates)
    return {"keys": llm.key_status()}


# ── MCP 注册管理 ────────────────────────────────────────────────────────────────
# MCP server 是 stdio 服务,由 MCP 客户端(Claude Code/Desktop、Cursor、Cline…)
# 按需拉起。首选给出通用 stdio 配置(任何客户端都能粘),同时保留 claude CLI 一键注册。

MCP_NAME = "kb"
# 仓库根(pyproject 所在):本文件在 kb/web/ 下,要往上两级 —— 少一级会指到 kb/,
# 弹窗里给出的 `uv run --directory .../kb kb-mcp` 是跑不起来的
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 通用 stdio 启动命令(与具体客户端无关)
_MCP_COMMAND = "uv"
_MCP_ARGS = ["run", "--directory", str(_PROJECT_ROOT), "kb-mcp"]
# 通用客户端配置块:大多数 MCP 客户端的配置文件都是这个形状,直接粘进 mcpServers 即可
_MCP_CONFIG = {"mcpServers": {MCP_NAME: {"command": _MCP_COMMAND, "args": _MCP_ARGS}}}
# Claude Code 专用的一键注册命令(便捷项)
_MCP_ADD_CMD = [
    "claude", "mcp", "add", MCP_NAME, "--scope", "user", "--",
    _MCP_COMMAND, *_MCP_ARGS,
]


def _mcp_tools() -> list[dict]:
    """工具清单从真正的 MCP server 上取,不在这里手写一份。

    手写的那份漏了 kb_enumerate(全量枚举),面板上就看不到这个能力 ——
    以后加删工具也不用记着同步两处。取不到时给空表,弹窗少一行说明而已。
    """
    try:
        from kb.mcp_server import mcp
        out = []
        for t in mcp._tool_manager.list_tools():
            head = (t.description or "").strip().splitlines()
            out.append({"name": t.name, "desc": head[0].strip() if head else ""})
        return out
    except Exception:
        return []


@router.get("/api/mcp/status")
def mcp_status():
    # 通用配置永远可给出;registered 只反映 claude CLI 里的注册态(便捷项,失败不影响通用用法)
    base = {
        "config": json.dumps(_MCP_CONFIG, ensure_ascii=False, indent=2),
        "command": " ".join(_MCP_ADD_CMD),
        "tools": _mcp_tools(),
    }
    try:
        proc = subprocess.run(
            ["claude", "mcp", "get", MCP_NAME],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {**base, "registered": False, "cli_available": False,
                "error": f"claude CLI 不可用: {e}"}
    return {
        **base,
        "registered": proc.returncode == 0,
        "cli_available": True,
        "detail": proc.stdout.strip() if proc.returncode == 0 else "",
    }


@router.post("/api/mcp/register")
def mcp_register():
    proc = subprocess.run(_MCP_ADD_CMD, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise HTTPException(500, f"注册失败: {(proc.stderr or proc.stdout)[:300]}")
    return {"ok": True, "message": proc.stdout.strip()}


@router.post("/api/mcp/unregister")
def mcp_unregister():
    proc = subprocess.run(
        ["claude", "mcp", "remove", MCP_NAME, "--scope", "user"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise HTTPException(500, f"取消注册失败: {(proc.stderr or proc.stdout)[:300]}")
    return {"ok": True, "message": proc.stdout.strip()}
