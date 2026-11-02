from kb import llm, llm_openai
from kb.youtube.diagnosis import MODEL, ToolFailure


class OutputError(RuntimeError):
    pass


def service_failure(protocol, reason, response=None):
    detail = getattr(getattr(response, 'error', None), 'message', None) or ''
    return ToolFailure(f'模型服务未返回完整回复：{protocol}/{reason}', detail, origin=MODEL)


def complete(client, provider, model, prompt, max_tokens=16000):
    if provider == 'anthropic':
        ended = False
        with client.messages.stream(model=model, max_tokens=max_tokens,
                                    messages=[{'role': 'user', 'content': prompt}]) as stream:
            for event in stream:
                ended = ended or event.type == 'message_stop'
            if not ended:
                raise service_failure('anthropic', 'missing_message_stop')
            response = stream.get_final_message()
        if not response.stop_reason:
            raise service_failure('anthropic', 'missing_stop_reason')
        if response.stop_reason != 'end_turn':
            raise OutputError(f'模型回复未正常完成：anthropic/{response.stop_reason}')
        if any(block.type not in ('text', 'thinking', 'redacted_thinking') for block in response.content):
            raise OutputError('模型返回了非正文内容，未生成中文成品')
        text = ''.join(block.text for block in response.content if block.type == 'text')
    else:
        converted = llm.openai_conversion_enabled()
        response = llm_openai._create(client, model, prompt, max_tokens, converted)
        if converted:
            choices = response.choices or []
            if len(choices) != 1:
                raise service_failure('chat', f'choice_count_{len(choices)}', response)
            if not choices[0].finish_reason:
                raise service_failure('chat', 'missing_finish_reason', response)
            if choices[0].finish_reason != 'stop':
                raise OutputError(f'模型回复未正常完成：chat/{choices[0].finish_reason}')
            if choices[0].message.refusal:
                raise OutputError('模型拒绝生成正文，未生成中文成品')
            text = choices[0].message.content
        else:
            if response.status not in ('completed', 'incomplete') or getattr(response, 'error', None) is not None:
                raise service_failure('responses', response.status or 'missing_status', response)
            if response.status != 'completed' or response.incomplete_details is not None:
                reason = getattr(response.incomplete_details, 'reason', None) or response.status
                raise OutputError(f'模型回复未正常完成：responses/{reason}')
            if any(item.type == 'message' and item.status not in ('completed', 'incomplete')
                   for item in response.output):
                raise service_failure('responses', 'missing_message_completion', response)
            if any(item.type not in ('message', 'reasoning') or
                   (item.type == 'message' and (item.status != 'completed' or
                    any(part.type != 'output_text' for part in item.content))) for item in response.output):
                raise OutputError('模型返回了非正文内容，未生成中文成品')
            text = response.output_text
    if not isinstance(text, str) or not text.strip():
        raise OutputError('模型返回空内容，未生成中文成品')
    return text.strip()
