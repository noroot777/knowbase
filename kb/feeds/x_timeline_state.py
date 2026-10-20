"""Consume timeline errors through collection and any later response from its requests."""
import time

from kb.feeds import x_account, x_page_state


def wait_state(page, handle: str, deadline: float, script: str) -> dict:
    last, previous = {}, None
    errors = x_page_state.ProblemConfirmation()
    confirmed = ''
    failure = x_page_state.XBrowserError(f'@{handle} 的 X 页面请求出错,请稍后重试')
    while time.monotonic() < deadline:
        last = x_page_state.read(page, script)
        if last['login']:
            raise x_account.XAccountUnavailable(handle, 'login', 'login_control_or_route')
        current = x_page_state.problem(last)
        account = x_account.terminal(last, handle) if not current else None
        if account and account == previous:
            raise x_account.XAccountUnavailable(handle, *account)
        previous = account
        confirmed = errors.check(last)
        matching = 'account' not in last or last['account'].get('handle') == handle.lower()
        if matching and not account and x_page_state.terminal(last, confirmed) == 'ok':
            return last
        if x_page_state.partial_content(last, confirmed):
            return dict(last, incomplete=str(failure))
        pending = not (last.get('empty') or x_account.terminal(last, handle))
        if error := x_page_state.escalate(last, confirmed, failure, content_pending=pending):
            raise error
        time.sleep(0.3)
    if error := x_page_state.escalate(last, confirmed, failure, expired=True):
        raise error
    if (last.get('account') or {}).get('state') == 'unknown':
        raise x_page_state.XBrowserError(
            f'@{handle} 的个人页结构无法识别,尚未确认账号状态,留待下次同步重试;未能判出原因')
    observed = '页面没有加载完' if last.get('ready') != 'complete' else '时间线上没有读到推文'
    raise x_page_state.XBrowserError(f'@{handle} 的{observed};未能判出原因,请稍后重试或点「检查连接」')


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
                evidence = {key: self.result[key] for key in (
                    'unavailable', 'unavailable_count', 'note', 'skipped_quotes') if key in self.result}
                self.result.clear()
                self.result.update(entries=[], error=str(error), **evidence)
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
