"""本地历史批量取文也按请求节奏限速，已入库或等待重试的项无需等待。"""

import random
import time


class RequestPacer:
    def __init__(self):
        self.last_request = None

    def wait(self):
        if self.last_request is not None:
            delay = random.uniform(2.5, 4.0) - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
        self.last_request = time.monotonic()
