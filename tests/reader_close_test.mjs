import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const root = new URL('../kb/static/js/', import.meta.url);
const pages = readFileSync(new URL('../kb/web/pages.py', import.meta.url), 'utf8');
const docLinks = pages.match(/_DOC_LINKS_JS = r"""([\s\S]*?)"""/)[1];

function target() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(type, listener) { listeners.set(listener, type); },
    removeEventListener(type, listener) { listeners.delete(listener); },
  };
}

function rig() {
  const elements = new Map();
  const document = { ...target(), activeElement: null, getElementById: element,
    readyState: 'interactive', querySelectorAll: () => links };
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      const attributes = new Map();
      const node = {
        ...target(), style: {}, innerHTML: '', isConnected: true,
        classList: {
          remove(name) { classes.delete(name); },
          contains(name) { return classes.has(name); },
          toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
        },
        setAttribute(name, value) { attributes.set(name, value); },
        removeAttribute(name) { attributes.delete(name); },
        hasAttribute(name) { return attributes.has(name); },
        getAttribute(name) { return attributes.get(name) ?? null; },
        getClientRects: () => [{}], getBoundingClientRect: () => ({ height: 60 }),
        focus() { document.activeElement = node; },
        querySelector: () => element('reader-close'),
      };
      elements.set(id, node);
    }
    return elements.get(id);
  }
  const links = [];
  const frameDoc = { ...target(), documentElement: { contains: () => false },
    querySelectorAll: () => links };
  let foreign = false;
  const securityError = Object.assign(new Error('cross-origin frame'), { name: 'SecurityError' });
  const frameWindow = { ...target() };
  const remove = frameWindow.removeEventListener;
  for (const [name, value] of Object.entries({ scrollY: 0, document: frameDoc, removeEventListener: remove }))
    Object.defineProperty(frameWindow, name, { get() { if (foreign) throw securityError; return value; } });
  const frame = element('reader-frame');
  frame.contentWindow = frameWindow;
  Object.defineProperty(frame, 'contentDocument', { get: () => foreign ? null : frameDoc });
  const window = target();
  let disconnected = false;
  const state = vm.createContext({ document, window, URL,
    location: { origin: 'https://kb.local', href: 'https://kb.local/doc/article.md' },
    setTimeout: () => 1, clearTimeout() {},
    ResizeObserver: class { observe() {} disconnect() { disconnected = true; } },
  });
  for (const name of ['scrollaway.js', 'core.js'])
    vm.runInContext(readFileSync(new URL(name, root), 'utf8'), state, { filename: name });
  return { element, document, window, frameDoc, links,
    run: code => vm.runInContext(code, state),
    open(path) {
      vm.runInContext(`openReader({ getAttribute: () => ${JSON.stringify(path)}, dataset: {} })`, state);
    },
    load: () => [...frame.listeners].find(([, type]) => type === 'load')?.[0],
    navigateAway() { foreign = true; document.activeElement = frame; },
    get disconnected() { return disconnected; },
  };
}

test('cross-origin load releases every accessible listener and returns Escape focus to the reader', () => {
  const app = rig();
  app.run('bindReaderScrollAway()');
  app.element('reader-head').setAttribute('inert', '');
  app.navigateAway();
  assert.doesNotThrow(() => app.run('bindReaderScrollAway()'));
  assert.equal(app.run('readerAway'), null);
  assert.equal(app.run('SCROLL_AWAY.length'), 0);
  assert.equal(app.frameDoc.listeners.size, 0);
  assert.equal(app.window.listeners.size, 1);
  assert.equal(app.disconnected, true);
  assert.equal(app.element('reader-head').hasAttribute('inert'), false);
  assert.equal(app.document.activeElement, app.element('reader-close'));
  assert.equal(app.element('reader-frame').hidden, true);
  assert.equal(app.element('reader-frame').inert, true);
  assert.equal(app.element('reader-frame').getAttribute('tabindex'), '-1');
  assert.equal(app.element('reader-error').hidden, false);
});

test('closing during cross-origin navigation always removes the drawer and mask', () => {
  const app = rig();
  app.run('bindReaderScrollAway()');
  app.navigateAway();
  assert.doesNotThrow(() => app.run('closeReader(); closeReader()'));
  assert.equal(app.element('reader').style.display, 'none');
  assert.equal(app.element('reader-mask').style.display, 'none');
  assert.equal(app.element('reader-frame').src, 'about:blank');
  assert.equal(app.run('readerAway'), null);
  assert.equal(app.run('SCROLL_AWAY.length'), 0);
  assert.equal(app.frameDoc.listeners.size, 0);
  assert.equal(app.window.listeners.size, 1);
  assert.equal(app.disconnected, true);
});

test('unexpected cleanup errors remain observable but cannot trap the reader open', () => {
  const app = rig();
  app.open('/doc/first.md');
  const obsoleteLoad = app.load();
  app.run('setReaderUnavailable(true); readerAway = { release() { throw new Error("cleanup failed"); } }');
  assert.throws(() => app.run('closeReader()'), /cleanup failed/);
  assert.equal(app.element('reader').style.display, 'none');
  assert.equal(app.element('reader-mask').style.display, 'none');
  assert.equal(app.element('reader-frame').src, 'about:blank');
  assert.equal(app.run('readerAway'), null);
  obsoleteLoad();
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-error').hidden, true);
  assert.doesNotThrow(() => app.run('closeReader()'));
});

test('external links are safe before load without changing local anchors or Obsidian', () => {
  const app = rig();
  const link = href => {
    const attributes = new Map([['href', href]]);
    return { getAttribute: name => attributes.get(name) ?? null,
      setAttribute: (name, value) => attributes.set(name, value) };
  };
  const external = link('https://example.com/article');
  const protocolRelative = link('//example.org/');
  const local = link('/doc/next.md');
  const anchor = link('#section');
  const obsidian = link('obsidian://open?vault=notes');
  external.setAttribute('rel', 'nofollow');
  app.links.push(external, protocolRelative, local, anchor, obsidian);
  app.run(docLinks);
  for (const externalLink of [external, protocolRelative]) {
    assert.equal(externalLink.getAttribute('target'), '_blank');
    assert.ok(externalLink.getAttribute('rel').includes('noopener'));
  }
  assert.ok(external.getAttribute('rel').includes('nofollow'));
  for (const localLink of [local, anchor, obsidian]) assert.equal(localLink.getAttribute('target'), null);
});

test('a same-origin article restores the frame after an unavailable page', () => {
  const app = rig();
  app.run('setReaderUnavailable(true); bindReaderScrollAway()');
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-frame').inert, false);
  assert.equal(app.element('reader-frame').hasAttribute('tabindex'), false);
  assert.equal(app.element('reader-error').hidden, true);
  assert.equal(app.run('SCROLL_AWAY.length'), 1);
});

test('opening another article clears the error state before its load event', () => {
  const app = rig();
  app.run('setReaderUnavailable(true)');
  app.open('/doc/slow.md');
  assert.equal(app.element('reader-frame').src, '/doc/slow.md');
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-frame').inert, false);
  assert.equal(app.element('reader-frame').hasAttribute('tabindex'), false);
  assert.equal(app.element('reader-error').hidden, true);
  assert.equal(app.run('readerAway'), null);
});

test('closing an error page and reopening does not wait for load to restore the frame', () => {
  const app = rig();
  app.open('/doc/first.md');
  app.navigateAway();
  app.load()();
  assert.equal(app.element('reader-frame').hidden, true);
  app.run('closeReader()');
  assert.equal(app.element('reader-error').hidden, true);
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-frame').inert, false);
  assert.equal(app.element('reader-frame').hasAttribute('tabindex'), false);
  assert.equal(app.load(), undefined);
  app.open('/doc/slow.md');
  assert.equal(app.element('reader').style.display, '');
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-error').hidden, true);
  assert.equal(app.run('readerAway'), null);
});

test('obsolete load callbacks cannot change a switched, closed or reopened reader', () => {
  const app = rig();
  app.open('/doc/first.md');
  const firstLoad = app.load();
  firstLoad();
  const previousBinding = app.run('readerAway');
  app.open('/doc/second.md');
  const secondLoad = app.load();
  app.navigateAway();
  firstLoad();
  assert.equal(app.run('readerAway'), previousBinding);
  assert.equal(app.element('reader-error').hidden, true);
  secondLoad();
  assert.equal(app.element('reader-frame').hidden, true);
  assert.equal(app.run('readerAway'), null);
  app.run('closeReader()');
  secondLoad();
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-error').hidden, true);
  app.open('/doc/third.md');
  secondLoad();
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-error').hidden, true);
  assert.equal(app.run('readerAway'), null);
  assert.equal([...app.element('reader-frame').listeners.values()].filter(type => type === 'load').length, 1);
});
