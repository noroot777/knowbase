"""Shared X page states and errors for timeline and post-detail readers."""
import json
from urllib.parse import urlsplit

from kb.feeds.page_gate import ASSETS_JS
from kb.feeds.x_cdp import Session

_CONTENT_REQUESTS = {'timeline': frozenset({'UserTweets'}), 'detail': frozenset({'TweetDetail'}),
                     'home': frozenset({'HomeTimeline', 'HomeLatestTimeline'})}
_AUXILIARY_REQUESTS = {'timeline': frozenset({'UserByScreenName', 'UserTweetsAndReplies'}),
                       'detail': frozenset({'TweetResultByRestId'}), 'home': frozenset()}

STATE_JS = r'''
  const xPageState = () => {
    const primary = document.querySelector('[data-testid="primaryColumn"]');
    const fallback = !primary && !document.querySelector(
      '[data-testid="sidebarColumn"], aside, nav, header, footer, article');
    const text = (primary?.innerText || (fallback ? document.body?.innerText : '') || '').slice(0, 4000);
    const articles = document.querySelectorAll('article[data-testid="tweet"]').length;
    const retryPath = 'M12 4c-4.418 0-8 3.58-8 8s3.582 8 8 8c3.806 0 6.993-2.66 '
      + '7.802-6.22l1.95.44C20.742 18.67 16.76 22 12 22 6.477 22 2 '
      + '17.52 2 12S6.477 2 12 2c3.272 0 6.176 1.57 8 4V3.5h2v6h-6v-2'
      + 'h2.616C17.175 5.39 14.749 4 12 4z';
    const retry = [...document.querySelectorAll('[data-testid="primaryColumn"] button svg path')]
      .some(node => node.getAttribute('d') === retryPath);
    const failed = articles === 0 && (retry
      || /Something went wrong\. Try reloading|出错了。请尝试重新加载。|请稍后再试/i.test(text));
    const limited = articles === 0 && /Rate limit exceeded/i.test(text);
    const scope = primary || (fallback ? document.body : null);
    const busy = !!scope?.querySelector('[role="progressbar"], [aria-busy="true"]');
    const explicit = !busy && (fallback || retry || primary?.querySelector('[role="alert"]'));
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
      limited,
      failed,
      dom_problem: !busy && limited ? 'limited' : explicit && failed ? 'failed' : '',
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

    def reset(self, url: str) -> None:
        parsed = urlsplit(url)
        self.origin = (parsed.scheme, parsed.netloc)
        kind = ('detail' if '/status/' in parsed.path else
                'home' if parsed.path == '/home' else 'timeline')
        self.content_operations = _CONTENT_REQUESTS[kind]
        self.operations = self.content_operations | _AUXILIARY_REQUESTS[kind]
        self.loader = ''
        self.statuses.clear()

    def receive(self, event: dict) -> None:
        """A retry replaces only that operation's evidence in the same navigation."""
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
        loader, status = params.get('loaderId'), response.get('status', 0)
        self.statuses[(loader, operation)] = status

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
        if http_problem:
            snapshot['http_problem'] = http_problem
        return snapshot


def navigate(session, url: str):
    if not isinstance(session, Session):
        return session.goto(url)
    responses = getattr(session, '_x_responses', None)
    if responses is None:
        session.call('Network.enable', {}, session._sid)
        responses = Responses(session._sid)
        session.observe_events(responses.receive)
        session._x_responses = responses
    elif responses.session_id != session._sid:
        session.call('Network.enable', {}, session._sid)
        responses.session_id = session._sid
    responses.reset(url)
    navigation = session.goto(url)
    responses.loader = navigation.get('loaderId', '')
    return navigation


def read(session, script: str) -> dict:
    snapshot = json.loads(session.eval(script))
    responses = getattr(session, '_x_responses', None)
    return responses.apply(snapshot) if responses else snapshot


def problem(state: dict) -> str:
    if state.get('challenge') and state.get('ready') == 'complete':
        return 'challenge'
    if state.get('dom_problem') == '' and state.get('empty'):
        return ''
    if state.get('http_content_limited'):
        return 'limited'
    http = state.get('http_problem', '')
    explicit = state.get('dom_problem', '')
    if explicit in ('limited', 'failed', 'challenge'):
        return explicit
    if http in ('limited', 'failed'):
        return http
    return ''


class ProblemConfirmation:
    """Only two consecutive observations of the same authoritative error are terminal."""

    def __init__(self):
        self.previous = ''

    def check(self, state: dict) -> str:
        current = problem(state)
        confirmed = current if current == self.previous else ''
        self.previous = current
        return confirmed


def escalate(state: dict, confirmed: str, failure: Exception, *, expired=False) -> Exception | None:
    """Use one scope policy during polling and timeout; only local failures wait for expiry."""
    if not confirmed:
        return None
    if confirmed == 'challenge':
        return access_blocked()
    if confirmed == 'limited' and (not state.get('http_only') or state.get('http_content_limited')):
        return XRateLimited('X 暂时限制了访问频率,请稍后重试或降低同步频率')
    if expired or not state.get('http_only'):
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
