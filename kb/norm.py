# -*- coding: utf-8 -*-
"""实体名与 kind 的归一。

同一个工具在不同文章里写法不一(Skill Creator / skill-creator / Skill-Creator),
检索过滤、聚合枚举、展示卡片都需要一个稳定的归一键(norm)。
kind 取值来自多轮不同 prompt,词表不完全一致,统一映射到固定集合。
"""

from __future__ import annotations

import re

# 查询层/展示层公认的 kind 集合
KIND_CANON = {"project", "product", "org", "model", "research", "other"}

# LLM 输出过的野生 kind → 规范值
_KIND_MAP = {
    "open-source": "project",
    "open-source project": "project",
    "开源项目": "project",
    "开源": "project",
    "company": "org",
    "组织": "org",
    "大模型": "model",
    "tool": "product",
}


def kind_norm(kind: str | None) -> str:
    k = (kind or "").strip().lower()
    k = _KIND_MAP.get(k, k)
    return k if k in KIND_CANON else "other"


def entity_norm(name: str) -> str:
    """归一键:小写、空白/下划线折叠成单个 -、去引号括号冒号等装饰符。"""
    s = (name or "").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[#,\[\]{}|^\"'`“”‘’()（）:：!！?？]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return s or "unnamed"
