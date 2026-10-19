"""Profile-only terminal states, separate from post absence and request failures."""
from kb.feeds.x_page_state import XBrowserError

ACCOUNT_JS = r'''
  const xAccountState = () => {
    const path = location.pathname;
    const handle = path.match(/^\/([A-Za-z0-9_]+)\/?$/)?.[1]?.toLowerCase() || '';
    const result = {state: '', handle, evidence: ''};
    if (/^\/i\/flow\/login(?:\/|$)/.test(path) ||
        (path === '/i/jf/onboarding/web' &&
         new URLSearchParams(location.search).get('mode') === 'login')) {
      return {...result, state: 'login', evidence: 'login_route'};
    }
    const primary = document.querySelector('[data-testid="primaryColumn"]');
    if (!handle || !primary || document.readyState !== 'complete' ||
        primary.querySelector('article[data-testid="tweet"], [role="progressbar"], [aria-busy="true"]')) {
      return result;
    }
    const empty = primary.querySelector('[data-testid="emptyState"]');
    const header = empty?.querySelector('[data-testid="empty_state_header_text"]');
    if (!header) return result;
    const links = [...empty.querySelectorAll('a[href]')].map(link => {
      try { return new URL(link.getAttribute('href'), location.href); }
      catch { return null; }
    }).filter(url => url && /^(?:help|support)\.(?:x|twitter)\.com$/.test(url.hostname));
    if (links.some(url => /\/articles\/18311\/?$|\/rules-and-policies\/(?:x|twitter)-rules\/?$/.test(url.pathname))) {
      return {...result, state: 'suspended', evidence: 'empty_state_rules_link'};
    }
    const profile = primary.querySelector('[data-testid="UserName"], ' +
      '[data-testid="UserProfileHeader_Items"], [data-testid="UserProfileHeader_JoinDate"], [role="tablist"]');
    if (profile && (links.some(url => /\/public-and-protected-(?:posts|tweets)\/?$/.test(url.pathname)) ||
        primary.querySelector('[data-testid="UserName"] [data-testid="icon-lock"]'))) {
      return {...result, state: 'protected', evidence: 'protected_profile_empty_state'};
    }
    if (!profile) {
      const missing = /此账号不存在|此帳號不存在|This account doesn[’']t exist|This account does not exist/i.test(header.textContent);
      return {...result, state: missing ? 'missing' : 'unavailable', evidence: 'empty_state_without_profile'};
    }
    return result;
  };
'''


class XAccountUnavailable(XBrowserError):
    def __init__(self, handle: str, state: str, evidence: str = ''):
        self.state, self.evidence = state, evidence
        reason = {
            'missing': '账号不存在(可能已改名或注销,不能据此区分)',
            'suspended': '账号已被冻结',
            'protected': '账号的帖子受保护,当前登录态无权读取',
            'unavailable': '账号页面明确不可用(可能已改名、注销或被冻结)',
            'login': '登录态已失效,请在浏览器通道设置里重新登录,或从日常浏览器再导入一次',
        }[state]
        super().__init__(f'@{handle} {reason}')


def terminal(snapshot: dict, handle: str) -> tuple[str, str] | None:
    account = snapshot.get('account') or {}
    state = account.get('state')
    if snapshot.get('ready') != 'complete':
        return None
    if state == 'login':
        return state, account.get('evidence', '')
    if (account.get('handle') == handle.lower() and not snapshot.get('articles')
            and state in {'missing', 'suspended', 'protected', 'unavailable'}):
        return state, account.get('evidence', '')
    return None
