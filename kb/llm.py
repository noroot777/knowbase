# -*- coding: utf-8 -*-
"""统一 LLM 调用层:问答的意图规划与综合回答、同步管道的批量提取都走这里。

不再调本地 `claude` CLI,两家都用 API key:
- Anthropic(Claude):官方 anthropic SDK,读环境变量 ANTHROPIC_API_KEY
- OpenAI(GPT):官方 openai SDK,读环境变量 OPENAI_API_KEY

前端下拉里的模型 = 中转站实际能调的 id。给了内置兜底 id,但更推荐在「API Key」面板
点「拉取可用模型」,从各自中转站的 /v1/models 拉取并勾选(见 list_remote_models)。
GPT 侧走 chat.completions,用 max_completion_tokens、不传 temperature,兼容 gpt-5 推理系。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass

from kb.config import DATA_DIR


@dataclass(frozen=True)
class Model:
    provider: str   # "anthropic" | "openai"
    model_id: str   # 传给对应 SDK 的完整模型 id
    label: str      # 前端显示名(这里用 id 本身)


# 内置兜底模型 id:面板没配 / 没从中转站拉取时用这些。面板里配置会覆盖它们。
_DEFAULT_MODELS = {
    "anthropic": ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-8"],
    "openai": ["gpt-5.5", "gpt-5.6-luna", "gpt-5.4-mini"],
}

# ── API key / base_url 存取 ───────────────────────────────────────────────────
# 解析顺序:环境变量优先,其次本地 data/secrets.json(应用内设置面板写入,已被
# .gitignore 忽略,权限 600)。base_url 用于中转站:留空=各家官方地址。
#   Anthropic 侧:base_url 填到根(SDK 自动补 /v1/messages)
#   OpenAI 侧:base_url 一般带 /v1(SDK 自动补 /chat/completions)
_ENV_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
_ENV_URL = {"anthropic": "ANTHROPIC_BASE_URL", "openai": "OPENAI_BASE_URL"}
_SECRETS_PATH = DATA_DIR / "secrets.json"

_clients: dict[str, object] = {}


def _file_secrets() -> dict:
    try:
        return json.loads(_SECRETS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _file_entry(provider: str) -> dict:
    """secrets.json 里某家的配置。兼容旧格式(值直接是 key 字符串)。"""
    v = _file_secrets().get(provider)
    if isinstance(v, str):
        return {"key": v}
    return v if isinstance(v, dict) else {}


def get_key(provider: str) -> str | None:
    """有效 key:环境变量优先,其次 secrets.json;都没有返回 None。"""
    return os.environ.get(_ENV_KEY[provider]) or _file_entry(provider).get("key") or None


def get_base_url(provider: str) -> str | None:
    """有效 base_url(中转站地址):环境变量优先,其次文件;都没有返回 None=官方。"""
    return os.environ.get(_ENV_URL[provider]) or _file_entry(provider).get("base_url") or None


def has_key(provider: str) -> bool:
    return bool(get_key(provider))


def _save_secrets(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SECRETS_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        _SECRETS_PATH.chmod(0o600)  # 只有本人可读
    except OSError:
        pass
    _clients.clear()


def set_config(updates: dict) -> None:
    """写 secrets.json。updates 形如 {"openai": {"key": "sk-...", "base_url": "..."}}。
    只改传入的子字段;某子字段传空串=清除它;整家清空则删掉该家。
    写完清缓存 client,新配置立即生效,无需重启。"""
    data = _file_secrets()
    for prov, fields in updates.items():
        if prov not in _ENV_KEY or not isinstance(fields, dict):
            continue
        entry = _file_entry(prov)  # 已归一成 dict
        for sub in ("key", "base_url"):
            if sub not in fields:
                continue
            val = (fields[sub] or "").strip()
            if val:
                entry[sub] = val
            else:
                entry.pop(sub, None)
        if "models" in fields:  # 下拉可选模型 id 列表
            cleaned = [s for s in (str(x).strip() for x in (fields["models"] or [])) if s]
            if cleaned:
                entry["models"] = cleaned
            else:
                entry.pop("models", None)
        if entry:
            data[prov] = entry
        else:
            data.pop(prov, None)
    _save_secrets(data)


def _mask(k: str) -> str:
    return f"{k[:6]}…{k[-4:]}" if len(k) > 12 else "已设置"


def key_status() -> dict:
    """给设置面板用:每家 key 是否已设 + 来源 + 脱敏预览(绝不回传明文),
    以及当前 base_url(中转站地址,非密文,原样回显)。"""
    out = {}
    for prov, env_key in _ENV_KEY.items():
        env_k = os.environ.get(env_key)
        file_k = _file_entry(prov).get("key")
        k = env_k or file_k
        env_u = os.environ.get(_ENV_URL[prov])
        file_u = _file_entry(prov).get("base_url")
        out[prov] = {
            "set": bool(k),
            "source": "env" if env_k else ("file" if file_k else None),
            "masked": _mask(k) if k else "",
            "env": env_key,
            "base_url": env_u or file_u or "",
            "base_url_source": "env" if env_u else ("file" if file_u else None),
            "env_url": _ENV_URL[prov],
            "models": configured_models(prov),
        }
    return out


def _anthropic():
    if "anthropic" not in _clients:
        from anthropic import Anthropic
        # base_url=None 时 SDK 用官方地址
        _clients["anthropic"] = Anthropic(
            api_key=get_key("anthropic"), base_url=get_base_url("anthropic"))
    return _clients["anthropic"]


def _openai():
    if "openai" not in _clients:
        from openai import OpenAI
        _clients["openai"] = OpenAI(
            api_key=get_key("openai"), base_url=get_base_url("openai"))
    return _clients["openai"]


def configured_models(provider: str) -> list[str]:
    """该家当前可选的模型 id 列表:面板配置优先,否则内置兜底。"""
    m = _file_entry(provider).get("models")
    return list(m) if isinstance(m, list) and m else list(_DEFAULT_MODELS.get(provider, []))


def set_models(provider: str, models: list[str]) -> None:
    """保存某家要在下拉里暴露的模型 id 列表(面板「拉取可用模型」勾选后调用)。"""
    if provider in _ENV_KEY:
        set_config({provider: {"models": models}})


def _provider_of(model_id: str) -> str | None:
    """判断一个模型 id 属于哪家:先查配置列表,再按前缀兜底。"""
    for prov in _ENV_KEY:
        if model_id in configured_models(prov):
            return prov
    mid = (model_id or "").lower()
    if mid.startswith("claude"):
        return "anthropic"
    if mid.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-")):
        return "openai"
    return None


def get_default_model() -> str:
    """请求未指定模型时的兜底:显式配置 > 环境变量 > 第一个有 key 的模型 > 第一个模型。"""
    cfg = _file_secrets().get("default_model") or os.environ.get("KB_ASK_MODEL")
    if cfg:
        return cfg
    with_key = [m for prov in _ENV_KEY for m in configured_models(prov) if has_key(prov)]
    if with_key:
        return with_key[0]
    allm = [m for prov in _ENV_KEY for m in configured_models(prov)]
    return allm[0] if allm else "claude-sonnet-4-5"


def get_plan_model(fallback: str) -> str:
    """规划步骤用的模型:显式配置 > 环境变量 > 自动挑个便宜的(haiku/mini 等)
    > 传入的回答模型。规划只是解析意图,用便宜模型省钱,且一定要有 key。"""
    cfg = _file_secrets().get("plan_model") or os.environ.get("KB_PLAN_MODEL")
    if cfg:
        return cfg
    cheap = [m for prov in _ENV_KEY if has_key(prov) for m in configured_models(prov)
             if any(t in m.lower() for t in ("haiku", "mini", "flash", "lite", "nano"))]
    return cheap[0] if cheap else fallback


def get_fav_model() -> str:
    """收藏整理(行动卡提取/分诊)用的模型:显式配置 > 环境变量 > 默认问答模型。"""
    return (_file_secrets().get("fav_model")
            or os.environ.get("KB_FAV_MODEL")
            or get_default_model())


def set_fav_model(model: str) -> None:
    """保存收藏整理模型(设置面板下拉);传空 = 清除,回落默认问答模型。"""
    data = _file_secrets()
    model = (model or "").strip()
    if model:
        data["fav_model"] = model
    else:
        data.pop("fav_model", None)
    _save_secrets(data)


def resolve(model: str) -> Model:
    """把下拉传来的模型 id 解析成 Model,未知则回退默认。"""
    prov = _provider_of(model) if model else None
    if prov:
        return Model(prov, model, model)
    d = get_default_model()
    return Model(_provider_of(d) or "anthropic", d, d)


def available_models() -> list[dict]:
    """给前端下拉用:每家 configured_models 展开,标注 provider 是否已配 key。"""
    default = get_default_model()
    out = []
    for prov in _ENV_KEY:
        for mid in configured_models(prov):
            out.append({"key": mid, "label": mid, "provider": prov,
                        "enabled": has_key(prov), "default": mid == default})
    return out


def list_remote_models(provider: str) -> list[str]:
    """从中转站拉取可用模型 id(GET /v1/models)。需该家已配 key(+base_url)。"""
    if provider not in _ENV_KEY:
        raise RuntimeError(f"未知 provider: {provider}")
    if not has_key(provider):
        raise RuntimeError("该家还没配 key——先填好 key 和 base_url 保存,再拉取")
    client = _anthropic() if provider == "anthropic" else _openai()
    try:
        page = client.models.list()
    except Exception as e:
        raise RuntimeError(f"拉取失败(可能中转站不支持 /v1/models): {e}")
    data = getattr(page, "data", None) or list(page)
    ids = []
    for m in data:
        mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
        if mid:
            ids.append(mid)
    return sorted(ids)


def chat(prompt: str, model: str, max_tokens: int = 8192, timeout: int = 180) -> str:
    """单轮补全。prompt 已是完整提示词(资料都拼进去了),返回纯文本答案。

    失败统一抛 RuntimeError(上层转 502),含 provider 便于定位。
    """
    m = resolve(model)
    if not has_key(m.provider):
        raise RuntimeError(
            f"{m.provider} 未配置 API key——在网页右上角「API Key」面板填入,"
            f"或设置环境变量 {_ENV_KEY[m.provider]}"
        )
    if m.provider == "anthropic":
        return _chat_anthropic(m.model_id, prompt, max_tokens, timeout)
    return _chat_openai(m.model_id, prompt, max_tokens, timeout)


def _chat_anthropic(model_id: str, prompt: str, max_tokens: int, timeout: int) -> str:
    # 走流式接收:部分中转站(如 sealos 版 Claude 网关)对非流式请求也只回
    # text/event-stream,SDK 拿非流式 cast 会把 SSE 原文当 str 返回,后续 resp.content
    # 直接 AttributeError。流式对 SSE 网关和官方端点都兼容,顺带规避非流式长请求上限。
    # 整段收在 try 内:任何失败统一转 RuntimeError(上层 502),绝不逃逸成 500。
    try:
        parts: list[str] = []
        with _anthropic().with_options(timeout=timeout).messages.stream(
            model=model_id, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for chunk in stream.text_stream:
                parts.append(chunk)
        return "".join(parts).strip()
    except Exception as e:  # SDK 各类异常统一上抛
        raise RuntimeError(f"Claude 调用失败: {e}")


def _chat_openai(model_id: str, prompt: str, max_tokens: int, timeout: int) -> str:
    # max_completion_tokens 对 gpt-4o 和 gpt-5 都通用;gpt-5 推理系不接受 temperature,
    # 故这里一律不传 temperature。推理 token 也计入该上限,给足余量。
    try:
        resp = _openai().with_options(timeout=timeout).chat.completions.create(
            model=model_id,
            max_completion_tokens=max(max_tokens, 16000),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise RuntimeError(f"GPT 调用失败: {e}")
    return (resp.choices[0].message.content or "").strip()


def chat_stream(prompt: str, model: str, max_tokens: int = 8192,
                timeout: int = 180) -> Iterator[str]:
    """流式补全:逐块 yield 文本增量,供问答边生成边推给前端。

    与 chat() 同样的校验/路由;失败统一抛 RuntimeError(上层转 error 帧)。
    关键:底层用 with/迭代持有 HTTP 流——调用方 gen.close()(客户端断开)时
    GeneratorExit 会穿透到这里关闭流,后端 LLM 生成随之停止(真停止的根基)。
    """
    m = resolve(model)
    if not has_key(m.provider):
        raise RuntimeError(
            f"{m.provider} 未配置 API key——在网页右上角「API Key」面板填入,"
            f"或设置环境变量 {_ENV_KEY[m.provider]}"
        )
    if m.provider == "anthropic":
        yield from _stream_anthropic(m.model_id, prompt, max_tokens, timeout)
    else:
        yield from _stream_openai(m.model_id, prompt, max_tokens, timeout)


def _stream_anthropic(model_id: str, prompt: str, max_tokens: int,
                      timeout: int) -> Iterator[str]:
    try:
        with _anthropic().with_options(timeout=timeout).messages.stream(
            model=model_id, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for chunk in stream.text_stream:
                yield chunk
    except GeneratorExit:  # 客户端断开:让上下文管理器关闭流,不当错误
        raise
    except Exception as e:  # SDK 各类异常统一上抛
        raise RuntimeError(f"Claude 调用失败: {e}")


def anthropic_tool_turn(model_id: str, system: str, messages: list[dict],
                        tools: list[dict], max_tokens: int = 4096,
                        timeout: int = 120):
    """跑一轮带工具的 Anthropic 对话,返回完整的 Message(含 tool_use 块)。

    问答 agent 的研究阶段用:每轮拿回 stop_reason 决定是执行工具继续、
    还是资料已够进入综合。底层仍走流式(sealos 网关对非流式只回 SSE,
    见 _chat_anthropic 注释),get_final_message 会消费整个流。
    """
    try:
        with _anthropic().with_options(timeout=timeout).messages.stream(
            model=model_id, max_tokens=max_tokens, system=system,
            messages=messages, tools=tools,
        ) as stream:
            return stream.get_final_message()
    except Exception as e:
        raise RuntimeError(f"Claude 调用失败: {e}")


def anthropic_model_for_tools(prefer: str | None = None) -> str | None:
    """研究阶段(工具循环)用的 Anthropic 模型。

    选择顺序:显式配置 agent_model > 用户所选模型(若本就是 Anthropic 的)
    > 配置列表里第一个 sonnet > 第一个 Anthropic 模型。没配 anthropic key
    返回 None(上层退回旧的规划管线)。
    """
    if not has_key("anthropic"):
        return None
    cfg = _file_secrets().get("agent_model") or os.environ.get("KB_AGENT_MODEL")
    if cfg:
        return cfg
    if prefer and _provider_of(prefer) == "anthropic":
        return prefer
    models = configured_models("anthropic")
    return next((m for m in models if "sonnet" in m.lower()),
                models[0] if models else None)


def _stream_openai(model_id: str, prompt: str, max_tokens: int,
                   timeout: int) -> Iterator[str]:
    # 同 _chat_openai:max_completion_tokens 通用、不传 temperature 兼容 gpt-5 推理系。
    try:
        resp = _openai().with_options(timeout=timeout).chat.completions.create(
            model=model_id,
            max_completion_tokens=max(max_tokens, 16000),
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        )
        for chunk in resp:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = choices[0].delta.content
            if delta:
                yield delta
    except GeneratorExit:
        raise
    except Exception as e:
        raise RuntimeError(f"GPT 调用失败: {e}")
