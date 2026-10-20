"""Consume timeline errors through collection and any later response from its requests."""
from kb.feeds import x_page_state


class TimelineState:
    def __init__(self, page, handle, state, script):
        self.page = page
        self.script = script
        self.failure = x_page_state.XBrowserError(f'@{handle} 的 X 页面请求出错,请稍后重试')
        self.incomplete = state.get('incomplete', '')
        self.active = True
        self.result = None
        self.watermark = ''
        self.state = state
        page._x_timeline = self
        if responses := getattr(page, '_x_responses', None):
            responses.consumer = self

    def eval(self, script):
        result = self.page.eval(script)
        self.check()
        return result

    def check(self):
        if not self.active:
            self.update_result()
            return
        state = x_page_state.read(self.page, self.script)
        problem = x_page_state.problem(state)
        confirmed = ''
        if problem:
            state = x_page_state.read(self.page, self.script)
            confirmed = problem if x_page_state.problem(state) == problem else ''
        self.state = state
        self.consume(state, confirmed)

    def consume(self, state, confirmed):
        error = x_page_state.escalate(state, confirmed, self.failure, content_pending=False)
        if isinstance(error, (x_page_state.XRateLimited, x_page_state.XAccessBlocked)):
            if self.result is not None:
                self.result.clear()
                self.result.update(entries=[], error=str(error))
            raise error
        if error:
            self.incomplete = str(error)
        self.update_result()

    def drain(self, responses):
        state = responses.apply(dict(self.state))
        self.consume(state, x_page_state.problem(state))

    def update_result(self):
        if self.result is not None and self.incomplete:
            previous = self.result.get('incomplete', '')
            if self.incomplete not in previous:
                previous = ';'.join(filter(None, [previous, self.incomplete]))
            self.result['incomplete'] = previous
            self.result['watermark'] = self.watermark

    def finalize(self, result, watermark):
        self.result, self.watermark = result, watermark
        self.check()

    def before_navigation(self):
        self.check()
        self.active = False
