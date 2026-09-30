"""OpenAI 文本调用:原生 Responses 或转换为 Chat Completions。"""

from __future__ import annotations

from collections.abc import Iterator


def _create(client, model_id: str, prompt: str, max_tokens: int,
            conversion_enabled: bool, stream: bool = False):
    output_limit = max(max_tokens, 16000)
    if conversion_enabled:
        return client.chat.completions.create(
            model=model_id, messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=output_limit, stream=stream,
        )
    return client.responses.create(
        model=model_id, input=prompt, max_output_tokens=output_limit,
        stream=stream, store=False,
    )


def _check_response(response) -> None:
    if getattr(response, "status", None) == "failed":
        error = getattr(response, "error", None)
        raise RuntimeError(getattr(error, "message", None) or "Responses 请求失败")


def complete(client, model_id: str, prompt: str, max_tokens: int,
             conversion_enabled: bool) -> str:
    response = _create(client, model_id, prompt, max_tokens, conversion_enabled)
    if conversion_enabled:
        text = response.choices[0].message.content
    else:
        _check_response(response)
        text = response.output_text
    return (text or "").strip()


def text_stream(client, model_id: str, prompt: str, max_tokens: int,
                conversion_enabled: bool) -> Iterator[str]:
    with _create(client, model_id, prompt, max_tokens,
                 conversion_enabled, stream=True) as stream:
        for event in stream:
            if conversion_enabled:
                choices = getattr(event, "choices", None)
                delta = choices[0].delta.content if choices else None
            else:
                if event.type == "error":
                    raise RuntimeError(getattr(event, "message", None) or "Responses 流式请求失败")
                if event.type == "response.failed":
                    _check_response(event.response)
                delta = event.delta if event.type == "response.output_text.delta" else None
            if delta:
                yield delta
