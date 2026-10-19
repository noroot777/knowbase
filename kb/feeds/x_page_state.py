"""Shared X page states and errors for timeline and post-detail readers."""
import json
from urllib.parse import urlsplit

from kb.feeds.x_cdp import Session

STATE_JS = r'''
  const xPageState = () => {
    const primary = document.querySelector('[data-testid="primaryColumn"]');
    const text = (primary?.innerText || '').slice(0, 4000);
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
    const busy = !!primary?.querySelector('[role="progressbar"], [aria-busy="true"]');
    const explicit = !busy && (retry || primary?.querySelector('[role="alert"]'));
    return {
      url: location.href,
      ready: document.readyState,
      articles,
      login: !!document.querySelector('[data-testid="loginButton"], [data-testid="login"]')
        || /^\/i\/flow\/login/.test(location.pathname),
      empty: articles === 0 && !failed && /Hmm\.\.\.this page doesn|这个页面不存在|该页面不存在|Account suspended|账号已被冻结|doesn’t exist|does not exist/i.test(text),
      limited,
      failed,
      dom_problem: !busy && limited ? 'limited' : explicit && failed ? 'failed' : '',
    };
  };
'''


class Responses:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.origin = ''
        self.operations = set()
        self.loader = ''
        self.statuses = {}

    def reset(self, url: str) -> None:
        parsed = urlsplit(url)
        self.origin = (parsed.scheme, parsed.netloc)
        if '/status/' in parsed.path:
            self.operations = {'TweetDetail', 'TweetResultByRestId'}
        elif parsed.path == '/home':
            self.operations = {'HomeTimeline', 'HomeLatestTimeline'}
        else:
            self.operations = {'UserTweets', 'UserTweetsAndReplies', 'UserByScreenName'}
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
        statuses = [status for (loader, _operation), status in self.statuses.items()
                    if self.loader and loader == self.loader]
        if any(status == 429 for status in statuses):
            snapshot['limited'] = True
            snapshot['http_problem'] = 'limited'
        if any(400 <= status < 600 for status in statuses):
            snapshot['failed'] = True
            snapshot['empty'] = False
            snapshot.setdefault('http_problem', 'failed')
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
    http = state.get('http_problem', '')
    if http in ('limited', 'failed'):
        return http
    explicit = state.get('dom_problem', '')
    if explicit in ('limited', 'failed'):
        return explicit
    if state.get('ready') != 'complete':
        return ''
    return next((kind for kind in ('limited', 'failed') if state.get(kind)), '')


class XBrowserError(RuntimeError):
    """Browser-channel failure with a user-facing message."""


class XRateLimited(XBrowserError):
    """An explicit access limit aborts the remaining batch, not just one post."""
