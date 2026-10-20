"""Shared X page states and errors for timeline and post-detail readers."""
import json
from urllib.parse import urlsplit

from kb.feeds.page_gate import ASSETS_JS
from kb.feeds.x_cdp import Session

_CONTENT_REQUESTS = {'timeline': frozenset({'UserTweets'}), 'detail': frozenset({'TweetDetail'})}
_AUXILIARY_REQUESTS = {'timeline': frozenset({'UserByScreenName', 'UserTweetsAndReplies'}),
                       'detail': frozenset({'TweetResultByRestId'})}

STATE_JS = r'''
  const xPageState = () => {
    const text = (document.body?.innerText || '').slice(0, 4000);
    const articles = document.querySelectorAll('article[data-testid="tweet"]').length;
    const retryPath = 'M12 4c-4.418 0-8 3.58-8 8s3.582 8 8 8c3.806 0 6.993-2.66 '
      + '7.802-6.22l1.95.44C20.742 18.67 16.76 22 12 22 6.477 22 2 '
      + '17.52 2 12S6.477 2 12 2c3.272 0 6.176 1.57 8 4V3.5h2v6h-6v-2'
      + 'h2.616C17.175 5.39 14.749 4 12 4z';
    const retry = [...document.querySelectorAll('[data-testid="primaryColumn"] button svg path')]
      .some(node => node.getAttribute('d') === retryPath);
    const failed = articles === 0 && (retry
      || /Something went wrong\. Try reloading|出错了。请尝试重新加载。|请稍后再试/i.test(text));
    const gate = __GATE_COMPONENTS__;
    return {
      url: location.href,
      ready: document.readyState,
      articles,
      challenge: /^\/account\/access(?:\/|$)/.test(location.pathname)
        || (articles === 0 && gate),
      login: !!document.querySelector('[data-testid="loginButton"], [data-testid="login"]')
        || /^\/i\/flow\/login/.test(location.pathname),
      empty: articles === 0 && !failed && /Hmm\.\.\.this page doesn|这个页面不存在|该页面不存在|Account suspended|账号已被冻结|doesn’t exist|does not exist/i.test(text),
      limited: articles === 0 && /Rate limit exceeded/i.test(text),
      failed,
    };
  };
'''.replace('__GATE_COMPONENTS__', ASSETS_JS)


class Responses:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.origin = ''
        self.operations = set()
        self.content_operations = frozenset()
        self.loader = ''
        self.statuses = {}
        self.consumer = None

    def reset(self, url: str) -> None:
        parsed = urlsplit(url)
        self.origin = (parsed.scheme, parsed.netloc)
        kind = 'detail' if '/status/' in parsed.path else 'timeline'
        self.content_operations = _CONTENT_REQUESTS[kind]
        self.operations = self.content_operations | _AUXILIARY_REQUESTS[kind]
        self.loader = ''
        self.statuses = {}

    def receive(self, event: dict) -> None:
        if event.get('method') != 'Network.responseReceived' or (
                event.get('sessionId') != self.session_id):
            return
        params = event.get('params', {})
        response = params.get('response', {})
        parsed = urlsplit(response.get('url', ''))
        operation = parsed.path.rsplit('/', 1)[-1]
        if ((parsed.scheme, parsed.netloc) != self.origin
                or not parsed.path.startswith('/i/api/graphql/')
                or operation not in self.operations or params.get('type') not in {'XHR', 'Fetch'}):
            return
        self.statuses[(params.get('loaderId'), operation)] = response.get('status', 0)

    def apply(self, snapshot: dict) -> dict:
        current = {operation: status for (loader, operation), status in self.statuses.items()
                   if self.loader and loader == self.loader}
        content = {operation: status for operation, status in current.items()
                   if operation in self.content_operations}
        auxiliary = {operation: status for operation, status in current.items()
                     if operation not in self.content_operations}
        if content:
            snapshot['http_content'] = content
        if auxiliary:
            snapshot['http_auxiliary'] = auxiliary
        statuses = list(content.values())
        http_problem = ('limited' if any(status == 429 for status in statuses) else
                        'failed' if any(400 <= status < 600 for status in statuses) else '')
        if http_problem:
            snapshot['dom_problem'] = problem(snapshot)
            if not snapshot['dom_problem']:
                snapshot['http_only'] = True
        if any(status == 429 for status in statuses):
            snapshot['limited'] = True
            snapshot['http_content_limited'] = True
        if any(400 <= status < 600 for status in statuses):
            snapshot['failed'] = True
        return snapshot


def navigate(session, url: str):
    timeline = getattr(session, '_x_timeline', None)
    tracking = timeline is not None and timeline.active
    if tracking:
        timeline.before_navigation()
    if not isinstance(session, Session):
        return session.goto(url)
    from kb.feeds.x_response_scope import ResponseScopes

    scopes = getattr(session, '_x_response_scopes', None)
    responses = getattr(session, '_x_responses', None)
    if scopes is None:
        session.call('Network.enable', {}, session._sid)
        scopes = ResponseScopes(session)
        session.observe_events(scopes.receive, after_call=scopes.check)
        session._x_response_scopes = scopes
    elif responses.session_id != session._sid:
        session.call('Network.enable', {}, session._sid)
    scopes.start(url)
    navigation = session.goto(url)
    scopes.navigated(navigation)
    return navigation


def read(session, script: str) -> dict:
    snapshot = json.loads(session.eval(script))
    responses = getattr(session, '_x_responses', None)
    return responses.apply(snapshot) if responses else snapshot


def problem(state: dict) -> str:
    if state.get('ready') != 'complete':
        return ''
    dom_problem = state.get('dom_problem')
    if state.get('challenge'):
        return 'challenge'
    if state.get('http_content_limited'):
        return 'limited'
    if dom_problem:
        return dom_problem
    return next((kind for kind in ('challenge', 'limited', 'failed') if state.get(kind)), '')


def unavailable(state: dict) -> bool:
    return bool(state.get('ready') == 'complete' and state.get('empty') and not problem(state))


def partial_content(state: dict, confirmed: str) -> bool:
    return bool(confirmed == 'failed' and state.get('ready') == 'complete' and state.get('articles'))


def escalate(state: dict, confirmed: str, failure: Exception, *, expired=False,
             content_pending=True) -> Exception | None:
    """Keep batch scope consistent; pending full content may wait after an HTTP-only failure."""
    if not confirmed:
        return None
    if confirmed == 'challenge':
        return access_blocked()
    if confirmed == 'limited' and (not state.get('http_only') or state.get('http_content_limited')):
        return XRateLimited('X 暂时限制了访问频率,请稍后重试或降低同步频率')
    if expired or not content_pending or not state.get('http_only'):
        return failure
    return None


class XBrowserError(RuntimeError):
    """Browser-channel failure with a user-facing message."""


class XRateLimited(XBrowserError):
    """An explicit access limit aborts the remaining batch, not just one post."""


class XAccessBlocked(XBrowserError):
    """A verified access gate stops this session without declaring content absent."""


def access_blocked():
    return XAccessBlocked('X 要求安全验证,本轮已停止;请在对应通道的浏览器中完成验证后重试,不要反复同步')
