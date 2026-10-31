from kb import llm, llm_openai


def complete(client, provider, model, prompt, max_tokens=6000):
    if provider == 'anthropic':
        ended = False
        with client.messages.stream(model=model, max_tokens=max_tokens,
                                    messages=[{'role': 'user', 'content': prompt}]) as stream:
            for event in stream:
                ended = ended or event.type == 'message_stop'
            response = stream.get_final_message()
        if not ended or response.stop_reason != 'end_turn':
            raise RuntimeError(f'模型回复未正常完成：anthropic/{response.stop_reason or "missing_stop"}')
        if any(block.type not in ('text', 'thinking', 'redacted_thinking') for block in response.content):
            raise RuntimeError('模型返回了非正文内容，未生成中文成品')
        text = ''.join(block.text for block in response.content if block.type == 'text')
    else:
        converted = llm.openai_conversion_enabled()
        response = llm_openai._create(client, model, prompt, max_tokens, converted)
        if converted:
            choices = response.choices
            if len(choices) != 1 or choices[0].finish_reason != 'stop':
                reason = choices[0].finish_reason if choices else 'missing_choice'
                raise RuntimeError(f'模型回复未正常完成：chat/{reason}')
            if choices[0].message.refusal:
                raise RuntimeError('模型拒绝生成正文，未生成中文成品')
            text = choices[0].message.content
        else:
            if response.status != 'completed' or response.incomplete_details is not None:
                reason = getattr(response.incomplete_details, 'reason', None) or response.status
                raise RuntimeError(f'模型回复未正常完成：responses/{reason}')
            if any(item.type not in ('message', 'reasoning') or
                   (item.type == 'message' and (item.status != 'completed' or
                    any(part.type != 'output_text' for part in item.content))) for item in response.output):
                raise RuntimeError('模型返回了非正文内容，未生成中文成品')
            text = response.output_text
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError('模型返回空内容，未生成中文成品')
    return text.strip()
