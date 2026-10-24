"""模型分配的整组默认方案:只从已选候选中按阶段档位挑选,不改变供应商限制。"""

from __future__ import annotations

from kb import llm, llm_stages


_PRESETS = {
    "claude": ("Claude 默认", "anthropic", "claude"),
    "gpt": ("GPT 默认", "openai", "gpt"),
}


def _describe(key: str) -> dict:
    if key not in _PRESETS:
        raise KeyError(f"未知模型预设: {key}")
    label, provider, family = _PRESETS[key]
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
        reason = f"先在「API Key」中勾选可用的 {family.upper() if key == 'gpt' else 'Claude'} 模型"
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


def apply(key: str) -> dict:
    """重新按当前候选生成整组并一次落盘;不支持的阶段完整保留。"""
    preset = _describe(key)
    if not preset["ready"]:
        raise ValueError(preset["reason"])
    llm_stages.set_stages(preset["assignments"])
    return preset
