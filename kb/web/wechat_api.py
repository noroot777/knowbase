"""Local-only onboarding API; no arbitrary commands, keys or paths as inputs."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from kb import llm, llm_stages, wechat_setup
from kb.wechat_runtime import SetupError


def local_request(request: Request):
    local = {"127.0.0.1", "::1", "localhost"}
    # Check Host as well as peer to prevent DNS rebinding. Never trust forwarded headers.
    if not request.client or request.client.host not in local or request.url.hostname not in local:
        raise HTTPException(403, "微信接入仅允许从运行项目的本机访问。")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "不允许跨站发起微信接入。")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不允许跨站发起微信接入。")
    if request.method == "POST" and request.headers.get("x-kb-wechat-action") != "1":
        raise HTTPException(403, "请从本项目的微信接入页面启动操作。")


router = APIRouter(dependencies=[Depends(local_request)])


@router.get("/api/wechat/setup")
def status():
    return {**wechat_setup.snapshot(), "provider_ready": any(llm.has_key(p) for p in llm.PROVIDERS),
            "models_ready": all(s["ready"] for s in llm_stages.describe())}


class SetupRequest(BaseModel):
    action: str = "connect"
    account_id: str = Field(default="", max_length=32)
    confirmation_token: str = Field(default="", max_length=128)
    confirmed: bool = False


@router.post("/api/wechat/setup", status_code=202)
def start(body: SetupRequest):
    try:
        return wechat_setup.start(**body.model_dump())
    except SetupError as error:
        raise HTTPException(409, str(error)) from None
