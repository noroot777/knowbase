"""Local-only onboarding API; no arbitrary commands, keys or paths as inputs."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from kb import llm, llm_stages, wechat_setup
from kb.wechat_runtime import SetupError
from kb.deployment import in_container, wechat_container_status


def local_request(request: Request):
    local = {"127.0.0.1", "::1", "localhost"}
    # Check Host as well as peer to prevent DNS rebinding. Never trust forwarded headers.
    if not request.client or request.client.host not in local or request.url.hostname not in local:
        raise HTTPException(403, "绿泡泡接入仅允许从运行项目的本机访问。")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "不允许跨站发起绿泡泡接入。")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不允许跨站发起绿泡泡接入。")
    if request.method == "POST" and request.headers.get("x-kb-wechat-action") != "1":
        raise HTTPException(403, "请从本项目的绿泡泡接入页面启动操作。")


router = APIRouter()


@router.get("/api/wechat/setup")
def status(request: Request):
    # Published Docker ports appear as a bridge peer, not loopback. This fixed
    # capability response has no local paths, job tokens or access material.
    if in_container():
        state = wechat_container_status()
    else:
        local_request(request)
        state = wechat_setup.snapshot()
    return {**state, "provider_ready": any(llm.has_key(p) for p in llm.PROVIDERS),
            "models_ready": all(s["ready"] for s in llm_stages.describe())}


class SetupRequest(BaseModel):
    action: str = "connect"
    account_id: str = Field(default="", max_length=32)
    confirmation_token: str = Field(default="", max_length=128)
    confirmed: bool = False


@router.post("/api/wechat/setup", status_code=202)
def start(body: SetupRequest, request: Request):
    if in_container():
        raise HTTPException(409, wechat_container_status()["message"])
    local_request(request)
    try:
        return wechat_setup.start(**body.model_dump())
    except SetupError as error:
        raise HTTPException(409, str(error)) from None
