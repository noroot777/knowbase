"""独立阅读页的链接策略与离页目标通知。"""

_DOC_LINKS_JS = r"""
for (const link of document.querySelectorAll('#content a[href]')) {
  let url;
  try { url = new URL(link.getAttribute('href'), location.href); } catch { continue; }
  if (!/^https?:$/.test(url.protocol)) continue;
  const current = new URL(location.href);
  if (url.origin === current.origin && url.pathname === current.pathname
      && url.search === current.search && (url.hash || link.getAttribute('href').startsWith('#'))) continue;
  link.setAttribute('target', '_blank');
  link.setAttribute('rel', [...new Set((link.getAttribute('rel') || '').split(/\s+/).filter(Boolean).concat('noopener'))].join(' '));
}
(() => {
  const navigationId = window.frameElement?.dataset.readerNavigation;
  if (!navigationId || window.parent === window) return;
  const documentUrl = location.href;
  let destination = null;
  let intent = 0;
  const departureUrl = value => {
    let url;
    try { url = new URL(value, location.href); } catch { return null; }
    if (!/^https?:$/.test(url.protocol)) return null;
    const current = new URL(location.href);
    if (url.origin === current.origin && url.pathname === current.pathname
        && url.search === current.search) return null;
    return url.href;
  };
  const notify = () => {
    const data = { type: 'reader-navigation', navigationId, documentUrl, destination };
    if (typeof window.parent.recordReaderNavigation === 'function') {
      window.parent.recordReaderNavigation({ source: window, origin: location.origin, data });
    }
    window.parent.postMessage(data, location.origin);
  };
  document.addEventListener('click', event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[href]');
    if (!link || link.hasAttribute('download')) return;
    const target = (link.getAttribute('target') || document.querySelector('base[target]')?.target || '_self').toLowerCase();
    if (target !== '_self' && target !== window.name) return;
    destination = departureUrl(link.href);
    if (!destination) return;
    const clickIntent = ++intent;
    notify();
    queueMicrotask(() => {
      if (event.defaultPrevented && intent === clickIntent) { destination = null; notify(); }
    });
  }, true);
  window.navigation?.addEventListener('navigate', event => {
    intent++;
    destination = event.hashChange || event.downloadRequest != null ? null : departureUrl(event.destination.url);
    notify();
  });
  window.addEventListener('beforeunload', () => { if (destination) notify(); });
})();
"""
