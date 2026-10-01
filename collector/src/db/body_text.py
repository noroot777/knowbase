# -*- coding: utf-8 -*-
"""把抓回来的正文收成 `article_bodies.body_text` 那一列要的单段文本。"""

from __future__ import annotations

from typing import Any


def normalize_body_text(raw: Any) -> str:
    """抓正文拿到的是段落列表,这一列存的是单段 TEXT,所以按换行拼起来。

    三种入参都要收:
    - `list`:正常形态(`fetch_article_body_full` 返回 `list[str]`),逐段拼;
      元素不保证是字符串,所以逐个 `str()`——半截数据也该落库,不该在这里抛。
    - `None`:上游判定「这篇没正文」,存空串而不是 NULL,好让
      `get_body_text()` 的「有没有这一行」和「这一行有没有内容」是两个问题。
    - 其它:直接 `str()`。库里存量有一批是 JSON 序列化过的 list(老导出器写的),
      所以读回那头(`scripts/export_obsidian.body_text_to_markdown`)至今还要先
      试一次 `json.loads` 再回退纯文本——**这里不要再 json.dumps**,那会给
      存量格式再添一种。
    """
    if raw is None:
        return ""
    if isinstance(raw, list):
        return "\n".join(str(paragraph) for paragraph in raw)
    return str(raw)
