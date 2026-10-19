"""Shared X page states and errors for timeline and post-detail readers."""

STATE_JS = r'''
  const xPageState = () => {
    const text = (document.body?.innerText || '').slice(0, 4000);
    const articles = document.querySelectorAll('article[data-testid="tweet"]').length;
    return {
      url: location.href,
      ready: document.readyState,
      articles,
      login: !!document.querySelector('[data-testid="loginButton"], [data-testid="login"]')
        || /^\/i\/flow\/login/.test(location.pathname),
      empty: articles === 0 && /Hmm\.\.\.this page doesn|这个页面不存在|该页面不存在|Account suspended|账号已被冻结|doesn’t exist|does not exist/i.test(text),
      limited: articles === 0 && /Rate limit exceeded|Something went wrong\. Try reloading|请稍后再试/i.test(text),
    };
  };
'''


class XBrowserError(RuntimeError):
    """Browser-channel failure with a user-facing message."""


class XRateLimited(XBrowserError):
    """An explicit access limit aborts the remaining batch, not just one post."""
