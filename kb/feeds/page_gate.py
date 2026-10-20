"""Live gate components shared by HTML ingestion and browser page-state readers."""
import json

# grab_page._gate_assets and x_page_state.STATE_JS share these markers and liveness rules.
# Only orchestration components count; generic captcha widgets and jsd/main.js do not.
ASSETS = ('/cdn-cgi/challenge-platform/h/', '_cf_chl_opt', 'captcha-delivery.com/captcha')
ATTRS = (('script', 'src'), ('iframe', 'src'), ('link', 'href'),
         ('form', 'action'), ('img', 'src'))
EXEC_TYPES = ('', 'text/javascript', 'application/javascript', 'module',
              'text/ecmascript', 'application/ecmascript', 'text/jscript')


def live(node) -> bool:
    return node.find_parent('template') is None and (
        node.name != 'script' or str(node.get('type') or '').strip().lower() in EXEC_TYPES)


def has_assets(soup) -> bool:
    for tag, attr in ATTRS:
        for node in soup.find_all(tag):
            value = str(node.get(attr) or '').lower()
            if live(node) and any(marker in value for marker in ASSETS):
                return True
    return any(not node.has_attr('src') and live(node)
               and any(marker in (node.string or '').lower() for marker in ASSETS)
               for node in soup.find_all('script'))


ASSETS_JS = r'''(() => {
  const live = node => !node.closest('template') && (node.localName !== 'script'
    || __EXEC_TYPES__.includes((node.getAttribute('type') || '').trim().toLowerCase()));
  const matches = value => __ASSETS__.some(marker => (value || '').toLowerCase().includes(marker));
  return __ATTRS__.some(([tag, attr]) =>
    [...document.querySelectorAll(`${tag}[${attr}]`)].some(node =>
      live(node) && matches(node.getAttribute(attr))))
    || [...document.querySelectorAll('script')].some(node =>
      !node.hasAttribute('src') && live(node) && matches(node.textContent));
})()'''.replace('__EXEC_TYPES__', json.dumps(EXEC_TYPES)).replace(
    '__ASSETS__', json.dumps(ASSETS)).replace('__ATTRS__', json.dumps(ATTRS))
