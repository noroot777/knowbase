"""Keep request and loader ownership for the lifetime of one collecting session."""
from urllib.parse import urlsplit

from kb.feeds import x_page_state


class ResponseScopes:
    def __init__(self, session):
        self.session = session
        self.current = None
        self.pages = {}
        self.requests = {}
        self.failure = None

    def start(self, url):
        self.current = x_page_state.Responses(self.session._sid)
        self.current.reset(url)
        self.session._x_responses = self.current

    def navigated(self, navigation):
        self.current.loader = navigation.get('loaderId', '')
        if self.current.loader:
            self.pages[(self.current.session_id, self.current.loader)] = self.current

    def owner(self, session_id, loader):
        known = self.pages.get((session_id, loader))
        if known is not None:
            return known
        current = self.current
        if current and current.session_id == session_id and (
                not current.loader or current.loader == loader):
            return current
        return None

    def receive(self, event):
        method = event.get('method', '')
        params = event.get('params', {})
        session_id = event.get('sessionId')
        request = (session_id, params.get('requestId'))
        if method in {'Network.loadingFinished', 'Network.loadingFailed'}:
            self.requests.pop(request, None)
            return
        if method not in {'Network.requestWillBeSent', 'Network.responseReceived'}:
            return
        owner = self.requests.get(request) or self.owner(session_id, params.get('loaderId'))
        if owner is None:
            return
        payload = params.get('request' if method == 'Network.requestWillBeSent' else 'response', {})
        parsed = urlsplit(payload.get('url', ''))
        operation = parsed.path.rsplit('/', 1)[-1]
        if ((parsed.scheme, parsed.netloc) != owner.origin
                or not parsed.path.startswith('/i/api/graphql/')
                or operation not in owner.operations or params.get('type') not in {'XHR', 'Fetch'}):
            return
        if params.get('requestId'):
            self.requests[request] = owner
        if method == 'Network.requestWillBeSent':
            return
        owner.receive(event)
        if operation not in owner.content_operations:
            return
        if owner.consumer is not None and (
                not owner.consumer.active or owner.consumer.result is not None):
            try:
                owner.consumer.drain(owner)
            except (x_page_state.XRateLimited, x_page_state.XAccessBlocked) as error:
                self.failure = error
        elif owner is not self.current and payload.get('status') == 429:
            self.failure = x_page_state.escalate(owner.apply({}), 'limited', None)

    def check(self, method):
        """Raise only after the command reply is consumed, without interrupting owned-page cleanup."""
        if method in {'Browser.close', 'Target.closeTarget'}:
            return
        failure, self.failure = self.failure, None
        if failure is not None:
            raise failure
