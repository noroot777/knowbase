# -*- coding: utf-8 -*-
"""统一 LLM 调用层:问答的意图规划与综合回答、同步管道的批量提取都走这里。

不再调本地 `claude` CLI,两家都用 API key:
- Anthropic(Claude):官方 anthropic SDK,读环境变量 ANTHROPIC_API_KEY
- OpenAI(GPT):官方 openai SDK,读环境变量 OPENAI_API_KEY

前端下拉里的模型 = 中转站实际能调的 id。给了内置兜底 id,但更推荐在「API Key」面板
点「拉取可用模型」,从各自中转站的 /v1/models 拉取并勾选(见 list_remote_models)。
OpenAI 侧支持原生 Responses 或转换为 Chat Completions;旧配置默认开启转换。

**哪一步用哪个模型不在这里决定**:提取、条目化、wiki、问答各阶段的分配在
`kb.llm_stages`(面板「模型」),本模块只管解析 + 真正发请求。chat/chat_stream
既收模型 id 字符串,也收 llm_stages 给的 Model(供应商已定死)。
"""

from __future__ import annotations

import contextlib
import copy
import contextvars
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from kb import filelock
from kb.config import DATA_DIR


@dataclass(frozen=True)
class Model:
    provider: str   # "anthropic" | "openai"
    model_id: str   # 传给对应 SDK 的完整模型 id
    label: str = ""  # 前端显示名(留空=用 id 本身)

    def __str__(self) -> str:
        # 脚本里有 f"…({model})" 这样的日志,直接打出 id 而不是 dataclass repr
        return self.model_id


# 内置兜底模型 id:面板没配 / 没从中转站拉取时用这些。面板里配置会覆盖它们。
_DEFAULT_MODELS = {
    "anthropic": ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-8"],
    "openai": ["gpt-5.5", "gpt-5.6-luna", "gpt-5.3-codex-spark"],
}

# ── API key / base_url 存取 ───────────────────────────────────────────────────
# 解析顺序:环境变量优先,其次本地 data/secrets.json(应用内设置面板写入,已被
# .gitignore 忽略,权限 600)。base_url 用于中转站:留空=各家官方地址。
#   Anthropic 侧:base_url 填到根(SDK 自动补 /v1/messages)
#   OpenAI 侧:base_url 一般带 /v1(SDK 自动补 /chat/completions)
_ENV_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
_ENV_URL = {"anthropic": "ANTHROPIC_BASE_URL", "openai": "OPENAI_BASE_URL"}
PROVIDERS = tuple(_ENV_KEY)          # 供应商全集,面板与 llm_stages 都按它遍历
PROVIDER_LABEL = {"anthropic": "Anthropic / Claude", "openai": "OpenAI / GPT"}
_SECRETS_PATH = DATA_DIR / "secrets.json"

_clients: dict[str, object] = {}
_settings_view = contextvars.ContextVar("llm_settings_view", default=None)


class SettingsConflict(RuntimeError):
    pass


@contextlib.contextmanager
def settings_snapshot(data: dict | None = None):
    snapshot = _file_secrets() if data is None else data
    token = _settings_view.set(snapshot)
    try:
        yield snapshot
    finally:
        _settings_view.reset(token)


def settings_revision(data: dict | None = None) -> int:
    return (read_settings() if data is None else data).get("settings_revision", 0)


def _file_secrets() -> dict:
    snapshot = _settings_view.get()
    if snapshot is not None:
        return copy.deepcopy(snapshot)
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


def openai_conversion_enabled() -> bool:
    """旧配置默认沿用 Chat Completions;只有显式关闭才走 Responses。"""
    return _file_entry("openai").get("protocol_conversion_enabled") is not False


def has_key(provider: str) -> bool:
    return bool(get_key(provider))


def _save_secrets(data: dict) -> None:
    """写盘。**原子替换**:先写同目录的临时文件再 rename。

    直接 write_text 是截断后再写,中间有一段时间文件是空的或半截的——并发的
    读者(另一个请求、另一个进程)这时读到的是坏 JSON,`_file_secrets` 把它
    吞成 `{}`,接着那边一存盘就把整份 key 抹了。rename 在同一文件系统上是原子的,
    读者要么看到旧的完整文件,要么看到新的。

    临时文件名必须是唯一的:固定叫 `secrets.json.tmp` 的话,两个并发写者会写
    同一个文件,先 rename 的那个把它搬走,后一个当场 FileNotFoundError。进程内
    的锁挡得住同进程的并发,挡不住 kb-web 和一个命令行脚本同时存盘。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(_SECRETS_PATH.parent),
                                    prefix=_SECRETS_PATH.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        tmp.chmod(0o600)   # 先收权限再就位,别留一个短暂可读的窗口
        tmp.replace(_SECRETS_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)   # 失败不留垃圾文件
        raise
    _clients.clear()


# secrets.json 不只装 key:各阶段的模型分配(见 kb.llm_stages)也落在这里。
# 读随便读,**改一律走 update_settings**(见下),别在外面 read → 改 → write。
def read_settings() -> dict:
    return _file_secrets()


# 所有改写共用一把进程内锁。secrets.json 是整文件读改写:两个请求(面板里连着
# 改两个阶段、或者一边存 key 一边改阶段)各自基于同一份旧内容写回,后写的那次
# 就把前一次的改动整个吃掉——两个请求都返回成功,用户只看到一个生效
# (2026-09-14 第 3 轮审查发现)。锁住「读→改→写」这一整段,而不只是写。
_settings_lock = threading.RLock()
_lock_depth = 0                       # >0 = 本进程已持着 flock(同线程重入别再抢)


@contextlib.contextmanager
def _settings_guard() -> Iterator[None]:
    """把「读→改→写」圈成临界区,**同进程内和跨进程都互斥**。

    进程内锁只挡得住同一个 kb-web 里的并发。两个独立进程(两个 kb-web 实例、
    或一个服务进程 + 一条命令行)同时保存时,各自读到同一份旧 JSON、各自整文件
    写回,后写的照样把前一次的阶段/key/base_url 吃掉,两边还都返回成功
    (2026-09-14 第 4 轮审查发现)。所以另加一把 `secrets.json.lock` 上的 flock:
    锁在独立的 lockfile 上而不是 secrets.json 自己,因为写盘是 rename 换文件,
    锁着的那个 inode 会被换走,后来者锁到的就是另一个对象了。

    lockfile 只创建不删除:unlink 会让「A 拿到锁 → B 删文件 → C 新建并拿到锁」
    这种交错出现两个持锁者,留一个空文件在那儿的代价则是零。
    """
    global _lock_depth
    with _settings_lock:                  # 先把同进程的并发挡在外面
        # 重入(外层已持锁)/ 这个平台没有文件锁:只靠进程内锁
        if _lock_depth or not filelock.HAVE_FLOCK:
            _lock_depth += 1
            try:
                yield
            finally:
                _lock_depth -= 1
            return
        lock_path = _SECRETS_PATH.with_name(_SECRETS_PATH.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            filelock.flock(fd, filelock.LOCK_EX)
            _lock_depth += 1
            try:
                yield
            finally:
                _lock_depth -= 1
        finally:
            os.close(fd)                  # 关 fd 即释放 flock


def update_settings(mutate, *, expected_revision: int | None = None,
                    operation_id: str | None = None) -> dict:
    """在锁内重新读盘 → 交给 mutate 就地改 → 原子写回,返回写进去的那份。

    mutate 收到的是**临界区里刚读出来的**最新内容,所以并发的两次改动会叠加,
    不会互相覆盖。校验之类的活在调用前做完,别放进 mutate 里拖长临界区,
    更别在 mutate 里再调一次 update_settings(临界区里干等自己)。
    """
    token = _settings_view.set(None)
    try:
        with _settings_guard():
            data = _file_secrets()
            revision = settings_revision(data)
            if expected_revision is not None and expected_revision != revision:
                raise SettingsConflict("配置已被其他操作更新,请重新读取后再保存")
            operations = dict(data.get("settings_operations") or {})
            if operation_id and operation_id in operations:
                raise SettingsConflict("这次保存已经提交,请重新读取当前配置")
            with settings_snapshot(data):
                mutate(data)
            data["settings_revision"] = revision + 1
            if operation_id:
                operations[operation_id] = revision + 1
                data["settings_operations"] = dict(list(operations.items())[-128:])
            _save_secrets(data)
            return data
    finally:
        _settings_view.reset(token)


def set_config(updates: dict) -> None:
    """写 secrets.json。updates 形如 {"openai": {"key": "sk-...", "base_url": "..."}}。
    只改传入的子字段;某子字段传空串=清除它;整家清空则删掉该家。
    写完清缓存 client,新配置立即生效,无需重启。"""
    def mutate(data: dict) -> None:
        for prov, fields in updates.items():
            if prov not in _ENV_KEY or not isinstance(fields, dict):
                continue
            cur = data.get(prov)
            entry = {"key": cur} if isinstance(cur, str) else (
                dict(cur) if isinstance(cur, dict) else {})   # 兼容旧格式(值直接是 key)
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
            if prov == "openai" and "protocol_conversion_enabled" in fields:
                enabled = fields["protocol_conversion_enabled"]
                if not isinstance(enabled, bool):
                    raise ValueError("协议转换开关必须是布尔值")
                entry["protocol_conversion_enabled"] = enabled
            if entry:
                data[prov] = entry
            else:
                data.pop(prov, None)

    update_settings(mutate)


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
        if prov == "openai":
            out[prov]["protocol_conversion_enabled"] = openai_conversion_enabled()
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
    """判断一个模型 id 属于哪家:先查配置列表,再按前缀兜底。

    注意这是**猜**:同一个 id 在两家中转站都可能存在。要确定性地指定供应商,
    用 "openai:gpt-5.5" 这种带前缀的写法,或直接传 Model(见 resolve)。
    """
    for prov in _ENV_KEY:
        if model_id in configured_models(prov):
            return prov
    mid = (model_id or "").lower()
    if mid.startswith("claude"):
        return "anthropic"
    if mid.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-")):
        return "openai"
    return None


def provider_of(model_id: str) -> str | None:
    """_provider_of 的公开名(llm_stages / 设置面板用)。"""
    return _provider_of(model_id)


def _split_prefix(model: str) -> tuple[str | None, str]:
    """拆 "openai:gpt-5.5" 这种显式写法;冒号前不是已知供应商则原样返回。"""
    head, sep, rest = model.partition(":")
    if sep and head in _ENV_KEY and rest.strip():
        return head, rest.strip()
    return None, model


def qualify(m: Model) -> str:
    """Model → "provider:model_id" 的字符串形式。

    凡是模型标识要出 Python 的边界(HTTP 响应、前端下拉的 value、落进
    messages 表),都得用这个带家的形式:裸 id 一旦回到 resolve() 就只能靠
    前缀猜,而同一个 id 两家中转站都可能有——面板明明选了 openai,问答却按
    遍历顺序打到 anthropic 去(2026-09-14 审查抓到的就是这条)。
    """
    return f"{m.provider}:{m.model_id}"


def _default_model() -> Model:
    """问答回答阶段的模型(= llm_stages 的 "ask" 阶段),带供应商。"""
    from kb import llm_stages
    return llm_stages.resolve_stage("ask") or Model(
        "anthropic", "claude-sonnet-4-5", "claude-sonnet-4-5")


def get_default_model() -> str:
    """问答未指定模型时的兜底,形如 "openai:gpt-5.5"(带家,见 qualify)。"""
    return qualify(_default_model())


def get_plan_model(fallback: str) -> str:
    """规划步骤用的模型:阶段配置 > 环境变量 > 自动挑个便宜的(haiku/mini 等)
    > 传入的回答模型。规划只是解析意图,用便宜模型省钱,且一定要有 key。"""
    from kb import llm_stages
    m = llm_stages.resolve_stage("plan", prefer=fallback)
    return qualify(m) if m else fallback


def get_fav_model() -> str:
    """收藏整理(行动卡提取/分诊)用的模型,同样带家。"""
    from kb import llm_stages
    m = llm_stages.resolve_stage("fav")
    return qualify(m) if m else get_default_model()


def set_fav_model(model: str, **write_options) -> dict:
    """保存收藏整理模型(收藏页下拉);传空 = 清除,回落默认问答模型。

    和设置面板里的「收藏 · 分诊与行动卡」写的是同一处,两边不会各存一份。
    收藏页传来的就是 "provider:model_id",set_stage 认这个前缀。
    """
    from kb import llm_stages
    return llm_stages.set_stage("fav", None, model, **write_options)


def parse_model(model: str, provider: str | None = None) -> Model | None:
    """和 resolve 同样的解析,但**认不出供应商时返回 None 而不是回退默认**。

    llm_stages 逐条试「环境变量 / 旧配置 / 内置默认」时必须用这个:用 resolve
    的话,一个认不出的 id 会去问默认模型,而默认模型自己也在走这套解析 —— 死循环。
    """
    model = (model or "").strip()
    if not model:
        return None
    pre, mid = _split_prefix(model)
    prov = provider or pre or _provider_of(mid)
    return Model(prov, mid, mid) if prov in _ENV_KEY and mid else None


def resolve(model, provider: str | None = None) -> Model:
    """把模型标识解析成 Model。接受三种写法:

    - Model 实例:原样返回(阶段配置走这条,供应商已经定死)
    - "openai:gpt-5.5":显式带家(接口/下拉传回来的都是这种)
    - "gpt-5.5":按 _provider_of 猜(命令行手敲、老数据);猜不出来回退默认模型
    """
    if isinstance(model, Model):
        return model
    return parse_model(model, provider) or _default_model()


def model_pool() -> dict[str, list[str]]:
    """每家在下拉里该出现的模型 id:面板勾选的候选 + **各阶段当前实际指向的**。

    后半截不能少。阶段模型不要求出现在候选列表里(API/命令行写进来的、老配置
    迁移过来的、或者用户先选了它、之后在「API Key」面板重新拉取时没再勾它),
    而问答下拉只要没有一个 option 匹配当前默认,浏览器就会自动选中第一个候选,
    chat.js 每次发送又都把下拉的值带上——用户在「模型分配」里配好的问答模型
    当场被顶掉,还看不出是谁改的(2026-09-14 第 2 轮审查发现)。
    """
    from kb import llm_stages
    pool = {p: list(configured_models(p)) for p in _ENV_KEY}
    for st in llm_stages.STAGES:
        m = llm_stages.resolve_stage(st.key)
        if m and m.provider in pool and m.model_id not in pool[m.provider]:
            pool[m.provider].append(m.model_id)
    return pool


def available_models() -> list[dict]:
    """给前端下拉用:model_pool 展开,标注 provider 是否已配 key。

    key 带家("openai:gpt-5.5")且全局唯一——两家有同名 id 时,裸 id 当 value
    会撞成两个一模一样的选项,连"哪个是默认"都会两边同时为真。
    label 只给人看,同名时补上供应商以示区分。
    """
    default = get_default_model()
    pool = model_pool()
    dupes = {mid for mid in pool["anthropic"] if mid in pool["openai"]}
    out = []
    for prov, mids in pool.items():
        for mid in mids:
            key = f"{prov}:{mid}"
            out.append({"key": key, "label": mid + (f"({prov})" if mid in dupes else ""),
                        "model": mid, "provider": prov,
                        "enabled": has_key(prov), "default": key == default})
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


def chat(prompt: str, model, max_tokens: int = 8192, timeout: int = 180) -> str:
    """单轮补全。prompt 已是完整提示词(资料都拼进去了),返回纯文本答案。

    model 可以是下拉传来的 id 字符串,也可以是 llm_stages.stage_model() 给的
    Model(供应商已定死)。失败统一抛 RuntimeError(上层转 502),含 provider 便于定位。
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
    from kb.llm_openai import complete

    try:
        return complete(
            _openai().with_options(timeout=timeout), model_id, prompt,
            max_tokens, openai_conversion_enabled(),
        )
    except Exception as e:
        raise RuntimeError(f"GPT 调用失败: {e}") from e


def chat_stream(prompt: str, model, max_tokens: int = 8192,
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
    """研究阶段(工具循环)用的 Anthropic 模型 id(= llm_stages 的 "agent" 阶段)。

    选择顺序:阶段配置 > 环境变量/旧 agent_model > 用户所选模型(若本就是
    Anthropic 的)> 配置列表里第一个 sonnet > 第一个 Anthropic 模型。
    没配 anthropic key 返回 None(上层退回旧的规划管线)。
    """
    if not has_key("anthropic"):
        return None
    from kb import llm_stages
    m = llm_stages.resolve_stage("agent", prefer=prefer)
    return m.model_id if m else None


def _stream_openai(model_id: str, prompt: str, max_tokens: int,
                   timeout: int) -> Iterator[str]:
    from kb.llm_openai import text_stream

    try:
        yield from text_stream(
            _openai().with_options(timeout=timeout), model_id, prompt,
            max_tokens, openai_conversion_enabled(),
        )
    except GeneratorExit:
        raise
    except Exception as e:
        raise RuntimeError(f"GPT 调用失败: {e}") from e
