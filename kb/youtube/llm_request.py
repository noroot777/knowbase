import json
import subprocess
import sys
import time

from kb.youtube import media, throttle
from kb.youtube.diagnosis import LOCAL, MODEL, ToolFailure


def failed(message):
    """模型这一侧的失败。**标上上游**(`diagnosis.MODEL`),并把限流记进本轮。

    不标的话,`429` 这个状态码会被 YouTube 那套判据一口认下:面板说「YouTube 对本机出口
    返回了 429、网页根本没下下来、换出口也有用」,还把一个无辜的频道记进登录态提醒
    (审查实测:所有 YouTube 请求都成功的那一轮也会这么说)。**状态码相同不能证明请求
    来自同一个服务。**

    认出是限流就记进本轮(`throttle`):接着问下一段、下一条视频只会一路撞同一道配额,
    换成几十条失败和退避。判据只有一份,在 `throttle.model_limited`。
    """
    if throttle.model_limited(message):
        throttle.trip(MODEL, message)
    return ToolFailure(message, '', '', MODEL)


def request(prompt, selected, deadline, stopped):
    if stopped() or deadline - time.monotonic() < .1:
        raise media.Interrupted('已停止或本轮视频处理时间预算不足')
    # 口径同 `media.ytdlp` 那一道:本轮已经撞上模型配额就不再起下一个请求。一条视频要调
    # 好几次模型(逐段翻译、再成稿),第一段撞上之后余下几段是白撞。
    throttle.guard(MODEL)
    payload = json.dumps({'prompt': prompt, 'provider': selected.provider,
                          'model': selected.model_id, 'deadline': deadline})
    try:
        process = subprocess.Popen([sys.executable, '-m', 'kb.youtube.llm_request'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=sys.platform != 'win32')
    except OSError as error:
        # 起不了子进程是**本机**的事(句柄用尽、解释器被改权限)。不标来源的话 `login.coded`
        # 按 YouTube 算,`permission denied` 会被判成「读不出 cookie」,面板让人去钥匙串放行。
        raise ToolFailure(f'起不了模型请求子进程：{error}', '', '', LOCAL) from None
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
            raise failed('视频内容模型调用失败：' + (errors or output)[-1000:])
        result = json.loads(output)
        if result.get('error'):
            raise failed(result['error'])
        text = result.get('text', '').strip()
        if not text:
            raise failed('模型返回空内容，未生成中文成品')
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
