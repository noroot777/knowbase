import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event

from kb.youtube import media, throttle
from kb.youtube.diagnosis import MODEL


MAX_REQUESTS = 3
_POOL = ThreadPoolExecutor(max_workers=MAX_REQUESTS, thread_name_prefix='youtube-translation')


def run(keys, create, receive, deadline, stopped):
    """全进程共用三席，只有调用线程接收结果和持久化缓存。"""
    cancelled = Event()
    pending = {}
    results = {}
    remaining = iter(keys)
    exhausted = False

    def halted():
        return cancelled.is_set() or stopped()

    def check():
        if halted() or time.monotonic() >= deadline:
            raise media.Interrupted('视频翻译已停止或时间预算用尽')

    def execute(key):
        check()
        throttle.guard(MODEL)
        value = create(key, halted)
        check()
        return value

    try:
        while pending or not exhausted:
            check()
            while not exhausted and len(pending) < MAX_REQUESTS:
                try:
                    key = next(remaining)
                except StopIteration:
                    exhausted = True
                    break
                check()
                throttle.guard(MODEL)
                pending[_POOL.submit(execute, key)] = key
            if not pending:
                break
            completed, _ = wait(pending, timeout=min(.05, max(0, deadline - time.monotonic())),
                                return_when=FIRST_COMPLETED)
            check()
            errors = []
            for future in completed:
                key = pending.pop(future)
                try:
                    results[key] = receive(key, future)
                except Exception as error:
                    errors.append(error)
            if errors:
                raise next((error for error in errors if not isinstance(error, throttle.NotAttempted)), errors[0])
        return results
    except throttle.NotAttempted as error:
        cancelled.set()
        for future in pending:
            future.cancel()
        wait(pending)
        for future in pending:
            if not future.cancelled() and isinstance(cause := future.exception(), media.ToolFailure):
                if not isinstance(cause, throttle.NotAttempted):
                    raise cause
        raise error
    finally:
        cancelled.set()
        for future in pending:
            future.cancel()
        wait(pending)
