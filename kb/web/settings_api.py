# -*- coding: utf-8 -*-
"""设置面板后端:各阶段的模型分配、各家 API key、MCP server 注册、出口与解析。

key 只落 kb.llm 的配置文件,读接口一律脱敏,绝不回传明文。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool

from kb import llm, llm_stages
from kb.feeds import egress

router = APIRouter()


@router.get("/api/models")
def api_models():
    """问答可选模型列表(含各 provider 是否已配 API key),给前端下拉用。"""
    return {"models": llm.available_models(), "default": llm.get_default_model()}


# ── 各阶段用哪个模型 ──────────────────────────────────────────────────────────
# 「阶段」= 代码里一处会调大模型的活,注册表在 kb.llm_stages。这里只做读写转发。

@router.get("/api/stages")
def api_stages():
    """所有阶段 + 当前取值 + 可选模型(按供应商分组),给「模型分配」面板用。

    可选模型走 llm.model_pool():除了「API Key」面板里勾选的候选,还带上各阶段
    当前实际指向的模型——不然一个没被勾选的阶段模型在下拉里根本没有对应项。
    """
    pool = llm.model_pool()
    return {
        "stages": llm_stages.describe(),
        "providers": [{"key": p, "label": llm.PROVIDER_LABEL.get(p, p),
                       "has_key": llm.has_key(p),
                       "models": pool.get(p, [])} for p in llm.PROVIDERS],
    }


class StageRequest(BaseModel):
    key: str
    provider: str = ""   # 显式指定供应商:同一个 id 两家都可能有,猜错就是用错 key
    model: str = ""      # 空 = 清除该阶段的配置,回落内置默认


@router.post("/api/stages")
def api_stage_set(body: StageRequest):
    try:
        llm_stages.set_stage(body.key, body.provider, body.model)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return api_stages()


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
    openai_protocol_conversion_enabled: StrictBool | None = None


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
    if body.openai_protocol_conversion_enabled is not None:
        updates["openai"]["protocol_conversion_enabled"] = body.openai_protocol_conversion_enabled
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


# ── 出口与解析 ──────────────────────────────────────────────────────────────────
# 这一块只做读写转发和「测一下」。**哪一项生效、来自哪里、坏值怎么判,全在
# kb.feeds.egress**(动之前先读 docs/decisions/dns-egress.md):判第二遍的那一版会在
# 页面上显示一套、抓取时用另一套。

#: 「测一下」默认测哪个域名。本机解析被污染的实测案例就是它(见决策文档),
#: 所以它比 example.com 有用:关着开关测一次就能看见假地址。
PROBE_HOST = "mistral.ai"


def _bad(e: egress.BadValue) -> JSONResponse:
    """坏值回 400,**`detail` 保持是一句话**(ui.js 的 `apiFail` 只认字符串,给它一个
    对象的话那句话会被静默换成「保存失败」),哪一项错了单独一个 `field` —— 面板按它
    把红字写在对应输入框下面。"""
    return JSONResponse({"detail": str(e), "field": e.field}, status_code=400)


@router.get("/api/egress")
def api_egress():
    """当前生效值 + 各项出处(代理的密码位脱敏)。"""
    return egress.view()


class EgressRequest(BaseModel):
    # 三项都是「原样回传」:空串 = 清掉这一项,于是环境变量重新成为兜底。
    endpoint: str = ""
    seconds: str = ""
    proxy: str = ""


@router.post("/api/egress")
def api_egress_set(body: EgressRequest):
    try:
        egress.save(body.endpoint, body.seconds, body.proxy)
    except egress.BadValue as e:
        return _bad(e)
    return egress.view()


class EgressProbeRequest(EgressRequest):
    host: str = ""


@router.post("/api/egress/probe")
def api_egress_probe(body: EgressProbeRequest):
    """按页面上**还没保存**的那几项真解析一次,报地址和耗时。不写盘。

    **走的是 `fetching.public_addresses`** —— 抓取每一跳问的是同一个函数,所以这里
    报的就是抓取会看到的东西(含 `is_global` 那道过滤和那几句人话错误)。另写一份
    探测实现的话,「测得通、抓不到」就会成为常态,而这个按钮存在的理由正好是回答
    「到底哪一步给错了」。

    端点留空也值得测:那一下走系统解析,**正好把本机被污染这件事摆出来**。
    """
    from kb.feeds import fetching   # 延迟:起服务不必为这一个按钮拉起整条采集栈
    # **`merge_proxy` 也在 try 里**:没有旧密码可沿用时它自己就抛 `BadValue`
    # (判不准一律拒),搁在外面那一下就是没人接的 500,而它本该是框下面一行红字。
    try:
        proxy_url = egress.merge_proxy(body.proxy)
        egress.check(body.endpoint, body.seconds, proxy_url)
    except egress.BadValue as e:
        return _bad(e)
    # 人往域名框里粘整条链接是常事,顺手剥掉 scheme 和 path —— 这是输入形态,
    # 不是第二份解析口径。
    host = (body.host or PROBE_HOST).strip().split("//", 1)[-1].split("/")[0].strip()
    if not host:
        raise HTTPException(400, "要测哪个域名?")
    started = time.monotonic()
    with egress.using(endpoint=body.endpoint, seconds=body.seconds, proxy=proxy_url):
        via = "doh" if egress.configured() else "system"
        try:
            _, found = fetching.public_addresses(f"https://{host}/")
            out: dict = {"ok": True, "addresses": found}
        except fetching.FeedError as e:
            out = {"ok": False, "error": str(e)}
    return {**out, "host": host, "via": via,
            "ms": round((time.monotonic() - started) * 1000)}
