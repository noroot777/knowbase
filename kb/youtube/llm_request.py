import json
import subprocess
import sys
import time

from kb.youtube import media


def request(prompt, selected, deadline, stopped):
    if stopped() or deadline - time.monotonic() < .1:
        raise media.Interrupted('已停止或本轮视频处理时间预算不足')
    payload = json.dumps({'prompt': prompt, 'provider': selected.provider,
                          'model': selected.model_id, 'deadline': deadline})
    process = subprocess.Popen([sys.executable, '-m', 'kb.youtube.llm_request'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=sys.platform != 'win32')
    try:
        pending = payload
        while True:
            if stopped() or time.monotonic() >= deadline:
                raise media.Interrupted('已停止或本轮视频处理时间预算用尽，模型连接已关闭')
            try:
                output, errors = process.communicate(pending, timeout=max(.001, min(.1, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                pending = None
        if stopped() or time.monotonic() >= deadline:
            raise media.Interrupted('已停止或本轮视频处理时间预算用尽，未保存迟到回复')
        if process.returncode:
            raise RuntimeError('视频内容模型调用失败：' + (errors or output)[-1000:])
        result = json.loads(output)
        if result.get('error'):
            raise RuntimeError(result['error'])
        text = result.get('text', '').strip()
        if not text:
            raise RuntimeError('模型返回空内容，未生成中文成品')
        return text
    finally:
        try:
            media.terminate(process)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def main():
    from kb import llm
    values = json.load(sys.stdin)
    client = None
    try:
        provider = values['provider']
        remaining = values['deadline'] - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('模型请求开始前处理预算已用尽')
        client = (llm._anthropic() if provider == 'anthropic' else llm._openai()).with_options(
            timeout=min(180, remaining), max_retries=0)
        llm._clients[provider] = client
        text = llm.chat(values['prompt'], llm.Model(provider, values['model']), max_tokens=6000,
                        timeout=min(180, remaining))
        if time.monotonic() >= values['deadline']:
            raise RuntimeError('模型回复超过处理预算')
        print(json.dumps({'text': text}))
    except Exception as error:
        print(json.dumps({'error': str(error)}))
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    main()
