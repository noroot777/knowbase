"""模型分配的整组默认方案:只从已选候选中按阶段档位挑选,不改变供应商限制。"""

from __future__ import annotations

from kb import llm, llm_stages


NAME_MAX_LENGTH = 40

_PRESETS = {
    "claude": ("Claude 默认", "anthropic", "claude"),
    "gpt": ("GPT 默认", "openai", "gpt"),
}


def _clean_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("组名必须是文字")
    name = name.strip()
    if not name or len(name) > NAME_MAX_LENGTH:
        raise ValueError(f"组名需为 1–{NAME_MAX_LENGTH} 个字符")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ValueError("组名不能包含换行或控制字符")
    return name


def rename(key: str, name: str, **write_options) -> str:
    """仅修改显示名称,保留预设标识、模型分配和其他设置。"""
    if key not in _PRESETS:
        raise KeyError(f"未知模型预设: {key}")
    name = _clean_name(name)

    def mutate(data: dict) -> None:
        labels = data.get("stage_preset_names")
        labels = dict(labels) if isinstance(labels, dict) else {}
        labels[key] = name
        data["stage_preset_names"] = labels

    llm.update_settings(mutate, **write_options)
    return name


def _describe(key: str) -> dict:
    if key not in _PRESETS:
        raise KeyError(f"未知模型预设: {key}")
    label, provider, family = _PRESETS[key]
    labels = llm.read_settings().get("stage_preset_names")
    if isinstance(labels, dict) and key in labels:
        try:
            label = _clean_name(labels[key])
        except ValueError:
            pass
    candidates = [(provider, model) for model in llm.configured_models(provider)
                  if family in model.lower()]
    fallback = llm_stages.recommended(llm_stages.stage("ask"), candidates)
    if not fallback and candidates:
        fallback = llm.qualify(llm.Model(*candidates[0]))
    assignments, preserved = {}, []
    for st in llm_stages.STAGES:
        if st.providers and provider not in st.providers:
            preserved.append(st.label)
            continue
        pick = llm_stages.recommended(st, candidates) or fallback
        if pick:
            model = llm.parse_model(pick)
            assignments[st.key] = {"provider": model.provider, "model": model.model_id}
    reason = ""
    if not llm.has_key(provider):
        reason = f"先在「API Key」中配置 {llm.PROVIDER_LABEL[provider]} 的 key"
    elif not candidates:
        reason = f"当前勾选的模型里没有 {family.upper() if key == 'gpt' else 'Claude'} 系列,请在「API Key」中选择"
    active = bool(assignments)
    for stage_key, entry in assignments.items():
        current = llm_stages.resolve_stage(stage_key)
        if not current or (current.provider, current.model_id) != (entry["provider"], entry["model"]):
            active = False
            break
    return {
        "key": key, "label": label, "ready": not reason, "reason": reason,
        "active": active and not reason, "assignments": assignments,
        "main_model": assignments.get("ask", {}).get("model", ""),
        "cheap_model": assignments.get("plan", {}).get("model", ""),
        "preserved": preserved,
    }


def describe() -> list[dict]:
    """面板预览、可用状态和当前是否匹配;不暴露 key,也不请求远程模型。"""
    return [_describe(key) for key in _PRESETS]


def apply(key: str, **write_options) -> dict:
    """重新按当前候选生成整组并一次落盘;不支持的阶段完整保留。"""
    with llm.settings_snapshot() as snapshot:
        preset = _describe(key)
        if not preset["ready"]:
            raise ValueError(preset["reason"])
        write_options.setdefault("expected_revision", llm.settings_revision(snapshot))
        llm_stages.set_stages(preset["assignments"], **write_options)
    return preset
