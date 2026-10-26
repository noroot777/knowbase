import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const root = new URL('../kb/static/js/', import.meta.url);
const docLinksSource = readFileSync(new URL('../kb/web/doc_links.py', import.meta.url), 'utf8');
const docLinks = docLinksSource.match(/_DOC_LINKS_JS = r"""([\s\S]*?)"""/)[1];

function target() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(type, listener) { listeners.set(listener, type); },
    removeEventListener(type, listener) { listeners.delete(listener); },
  };
}

function rig() {
  let now = 10000;
  const elements = new Map();
  const document = { ...target(), activeElement: null, getElementById: element,
    readyState: 'interactive', querySelectorAll: () => links, querySelector: () => null };
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      const attributes = new Map();
      const node = {
        ...target(), style: {}, dataset: {}, innerHTML: '', isConnected: true,
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
  document.body = element('body');
  document.documentElement = { dataset: {} };
  const links = [];
  const frameDoc = { ...target(), URL: 'https://kb.local/doc/article.md', title: 'Article',
    documentElement: { contains: () => false, dataset: { readerPath: 'article.md' } },
    querySelector: () => null, querySelectorAll: () => links };
  let foreign = false;
  const securityError = Object.assign(new Error('cross-origin frame'), { name: 'SecurityError' });
  const frameWindow = { ...target() };
  const remove = frameWindow.removeEventListener;
  for (const [name, value] of Object.entries({ scrollY: 0, document: frameDoc, removeEventListener: remove }))
    Object.defineProperty(frameWindow, name, { get() { if (foreign) throw securityError; return value; } });
  const frame = element('reader-frame');
  let frameHidden = false;
  Object.defineProperty(frame, 'hidden', { get: () => frameHidden, set(value) {
    frameHidden = value;
    if (value && document.activeElement === frame) document.activeElement = document.body;
  } });
  frame.contentWindow = frameWindow;
  Object.defineProperty(frame, 'contentDocument', { get: () => foreign ? null : frameDoc });
  const window = target();
  const popups = [];
  window.open = (...args) => popups.push(args);
  let disconnected = false;
  const state = vm.createContext({ document, window, URL, DOC_PATH: 'article.md',
    Date: class extends Date { static now() { return now; } },
    favSet: new Set(), setFavBtn() {},
    location: { origin: 'https://kb.local', href: 'https://kb.local/doc/article.md' },
    setTimeout: () => 1, clearTimeout() {}, queueMicrotask,
    ResizeObserver: class { observe() {} disconnect() { disconnected = true; } },
  });
  for (const name of ['scrollaway.js', 'core.js'])
    vm.runInContext(readFileSync(new URL(name, root), 'utf8'), state, { filename: name });
  const message = (data, overrides = {}) => {
    const handler = [...window.listeners].find(([, type]) => type === 'message')?.[0];
    handler?.({ source: frameWindow, origin: 'https://kb.local', data, ...overrides });
  };
  return { element, document, window, frameDoc, links, popups, message,
    advance: milliseconds => { now += milliseconds; },
    renderDocument(url) {
      foreign = false;
      frameDoc.URL = url;
      frameDoc.documentElement.dataset.readerPath = new URL(url).pathname.slice(5);
    },
    run: code => vm.runInContext(code, state),
    open(path) {
      vm.runInContext(`openReader({ getAttribute: () => ${JSON.stringify(path)}, dataset: {} })`, state);
    },
    load: () => [...frame.listeners].find(([, type]) => type === 'load')?.[0],
    navigateAway(url = 'https://external.test/blocked', notify = true) {
      if (notify) message({ type: 'reader-navigation', navigationId: frame.dataset.readerNavigation,
        documentUrl: frameDoc.URL, destination: url });
      foreign = true; document.activeElement = frame;
    },
    get disconnected() { return disconnected; },
  };
}

test('cross-origin load releases every accessible listener and returns Escape focus to the reader', () => {
  const app = rig();
  app.open('https://external.test/blocked');
  app.run('bindReaderScrollAway()');
  app.element('reader-head').setAttribute('inert', '');
  app.run('readerLastDocument = null');
  app.navigateAway('https://external.test/blocked', false);
  assert.doesNotThrow(() => app.run('bindReaderScrollAway()'));
  assert.equal(app.run('readerAway'), null);
  assert.equal(app.run('SCROLL_AWAY.length'), 0);
  assert.equal(app.frameDoc.listeners.size, 0);
  assert.equal(app.window.listeners.size, 2);
  assert.equal(app.disconnected, true);
  assert.equal(app.element('reader-head').hasAttribute('inert'), false);
  assert.equal(app.document.activeElement, app.element('reader-close'));
  assert.equal(app.element('reader-frame').hidden, true);
  assert.equal(app.element('reader-frame').inert, true);
  assert.equal(app.element('reader-frame').getAttribute('tabindex'), '-1');
  assert.equal(app.element('reader-error').hidden, false);
  assert.equal(app.element('reader-open').href, 'https://external.test/blocked');
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
  assert.equal(app.window.listeners.size, 2);
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

test('all article destinations open outside the reader except same-document anchors and Obsidian', () => {
  const app = rig();
  const link = href => {
    const attributes = new Map([['href', href]]);
    return { getAttribute: name => attributes.get(name) ?? null,
      setAttribute: (name, value) => attributes.set(name, value) };
  };
  const external = link('https://example.com/article');
  const protocolRelative = link('//example.org/');
  const local = link('/doc/next.md');
  const redirect = link('/go-external');
  const missing = link('/missing');
  const anchor = link('#section');
  const obsidian = link('obsidian://open?vault=notes');
  external.setAttribute('rel', 'nofollow');
  app.links.push(external, protocolRelative, local, redirect, missing, anchor, obsidian);
  app.run(docLinks);
  for (const externalLink of [external, protocolRelative, local, redirect, missing]) {
    assert.equal(externalLink.getAttribute('target'), '_blank');
    assert.ok(externalLink.getAttribute('rel').includes('noopener'));
  }
  assert.ok(external.getAttribute('rel').includes('nofollow'));
  for (const localLink of [anchor, obsidian]) assert.equal(localLink.getAttribute('target'), null);
});

test('a non-reader destination restores the verified article and retains the actual link', () => {
  const app = rig();
  app.open('/doc/article.md');
  app.load()();
  const navigationId = app.element('reader-frame').dataset.readerNavigation;
  app.navigateAway();
  assert.equal(app.element('reader-frame').src, '/doc/article.md');
  app.load()();
  assert.equal(app.element('reader-frame').src, 'https://kb.local/doc/article.md');
  assert.equal(app.element('reader-destination').href, 'https://external.test/blocked');
  assert.equal(app.element('reader-destination').style.display, '');
  assert.equal(app.popups.length, 0);
  assert.equal(app.document.activeElement, app.element('reader-close'));
  assert.equal(app.element('reader-error').hidden, true);
  app.message({ type: 'reader-navigation', navigationId, documentUrl: app.frameDoc.URL,
    destination: 'https://stale.test/' });
  assert.equal(app.run('readerPendingDestination'), null);
});

test('navigation notifications reject foreign sources, origins, unsafe URLs and obsolete documents', () => {
  const app = rig();
  app.open('/doc/article.md');
  app.load()();
  const notification = { type: 'reader-navigation',
    navigationId: app.element('reader-frame').dataset.readerNavigation,
    documentUrl: app.frameDoc.URL, destination: 'https://external.test/blocked' };
  for (const overrides of [{ source: {} }, { origin: 'https://external.test' }]) {
    app.message(notification, overrides);
    assert.equal(app.run('readerPendingDestination'), null);
  }
  for (const changes of [{ destination: 'javascript:alert(1)' }, { documentUrl: 'https://external.test/' },
    { documentUrl: 'https://kb.local/doc/old.md' }, { navigationId: 'obsolete' }]) {
    app.message({ ...notification, ...changes });
    assert.equal(app.run('readerPendingDestination'), null);
  }
  app.message(notification);
  assert.equal(app.run('readerPendingDestination'), notification.destination);
  app.message({ ...notification, destination: null });
  assert.equal(app.run('readerPendingDestination'), null);
  app.open('/doc/next.md');
  app.message(notification);
  assert.equal(app.run('readerPendingDestination'), null);
  app.run('closeReader()');
  app.message({ ...notification, navigationId: undefined });
  assert.equal(app.run('readerPendingDestination'), null);
});

test('article-side navigation reports script destinations without changing the iframe src attribute', () => {
  const app = rig();
  app.open('/doc/article.md');
  const notifications = [];
  app.window.frameElement = app.element('reader-frame');
  app.window.parent = { postMessage: (...args) => notifications.push(args) };
  app.window.navigation = target();
  app.run(docLinks);
  const navigate = [...app.window.navigation.listeners.keys()][0];
  navigate({ destination: { url: 'https://external.test/blocked' }, hashChange: false });
  assert.equal(notifications[0][0].destination, 'https://external.test/blocked');
  assert.equal(notifications[0][0].navigationId, app.element('reader-frame').dataset.readerNavigation);
  assert.equal(notifications[0][1], 'https://kb.local');
  assert.equal(app.element('reader-frame').src, '/doc/article.md');
  navigate({ destination: { url: 'https://kb.local/doc/article.md#section' }, hashChange: true });
  assert.equal(notifications.at(-1)[0].destination, null);
  navigate({ destination: { url: 'https://external.test/file' }, downloadRequest: 'file.txt' });
  assert.equal(notifications.at(-1)[0].destination, null);
});

test('capture-phase links report on beforeunload even without the Navigation API', async () => {
  const app = rig();
  app.open('/doc/article.md');
  const notifications = [];
  const synchronous = [];
  app.window.frameElement = app.element('reader-frame');
  app.window.parent = { recordReaderNavigation: event => synchronous.push(event),
    postMessage: message => notifications.push(message) };
  app.run(docLinks);
  const clickHandlers = [...app.document.listeners].filter(([, type]) => type === 'click');
  const click = clickHandlers.at(-1)[0];
  const beforeUnload = [...app.window.listeners].find(([, type]) => type === 'beforeunload')[0];
  const link = { href: 'https://external.test/blocked', hasAttribute: () => false, getAttribute: () => null };
  click({ button: 0, target: { closest: () => link } });
  assert.equal(synchronous[0].data.destination, link.href);
  assert.equal(synchronous[0].source, app.window);
  beforeUnload();
  assert.equal(notifications[0].destination, link.href);
  notifications.length = 0;
  click({ button: 0, target: { closest: () => link }, defaultPrevented: true });
  await Promise.resolve();
  beforeUnload();
  assert.equal(notifications.at(-1).destination, null);
});

test('a readable non-reader error page cannot become a reader document', () => {
  const app = rig();
  app.open('/missing');
  delete app.frameDoc.documentElement.dataset.readerPath;
  app.frameDoc.URL = 'https://kb.local/missing';
  app.document.activeElement = app.element('reader-frame');
  app.load()();
  assert.equal(app.element('reader-open').href, 'https://kb.local/missing');
  assert.equal(app.element('reader-error').hidden, false);
  assert.equal(app.document.activeElement, app.element('reader-close'));
  assert.equal(app.run('readerAway'), null);
});

test('reader headers follow the loaded article metadata instead of the entry link', () => {
  const app = rig();
  app.open('/doc/first.md');
  app.frameDoc.URL = 'https://kb.local/doc/next.md';
  app.frameDoc.title = 'Next article';
  app.frameDoc.documentElement.dataset.readerPath = 'next.md';
  app.frameDoc.querySelectorAll = () => [
    { href: 'https://source.test/next' }, { href: 'obsidian://open?file=next' },
  ];
  app.load()();
  assert.equal(app.element('reader-title').textContent, 'Next article');
  assert.equal(app.element('reader-open').href, 'https://kb.local/doc/next.md');
  assert.equal(app.element('reader-fav').dataset.path, 'next.md');
  assert.equal(app.element('reader-src').href, 'https://source.test/next');
  assert.equal(app.element('reader-obs').href, 'obsidian://open?file=next');
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

test('closing a restored page and reopening does not wait for load to restore the frame', () => {
  const app = rig();
  app.open('/doc/first.md');
  app.navigateAway();
  app.load()();
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-error').hidden, true);
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
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-error').hidden, true);
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

test('departure before the first load restores the rendered article and keeps its destination', () => {
  const app = rig();
  app.open('/doc/article.md');
  assert.equal(app.run('readerLastDocument'), null);
  assert.equal(app.run('readerAway'), null);
  app.navigateAway();
  assert.equal(app.run('readerLastDocument'), app.frameDoc.URL);
  app.load()();
  assert.equal(app.element('reader-frame').src, app.frameDoc.URL);
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-error').hidden, true);
  assert.equal(app.element('reader-destination').href, 'https://external.test/blocked');
  assert.equal(app.popups.length, 0);
});

test('repeated recovery stops reloading and opening popups until the reader is explicitly reopened', () => {
  const app = rig();
  app.open('/doc/article.md');
  app.navigateAway();
  app.load()();
  const recoveryId = app.element('reader-frame').dataset.readerNavigation;
  app.navigateAway('https://external.test/second');
  app.load()();
  assert.equal(app.element('reader-frame').dataset.readerNavigation, recoveryId);
  assert.equal(app.popups.length, 0);
  assert.equal(app.element('reader-frame').hidden, true);
  assert.equal(app.element('reader-frame').inert, true);
  assert.equal(app.element('reader-error').hidden, false);
  assert.equal(app.element('reader-destination').href, 'https://external.test/second');
  assert.equal(app.element('reader-open').href, 'https://external.test/second');
  assert.equal(app.document.activeElement, app.element('reader-close'));
  app.advance(6000);
  app.load()();
  assert.equal(app.element('reader-frame').dataset.readerNavigation, recoveryId);
  assert.equal(app.popups.length, 0);
  assert.equal(app.element('reader-return').hidden, false);
  assert.equal(app.element('reader-return').href, app.frameDoc.URL);
  app.run('closeReader()');
  app.open('/doc/article.md');
  app.navigateAway();
  app.load()();
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.popups.length, 0);
  assert.equal(app.element('reader-return').hidden, true);
});

test('a delayed departure cannot reset the automatic recovery budget', () => {
  const app = rig();
  app.open('/doc/article.md');
  app.navigateAway();
  app.load()();
  app.advance(5000);
  app.navigateAway('https://external.test/later');
  app.load()();
  assert.equal(app.element('reader-frame').hidden, true);
  assert.equal(app.element('reader-destination').href, 'https://external.test/later');
  assert.equal(app.popups.length, 0);
  assert.equal(app.run('readerRecoveryCount'), 1);
  assert.equal(app.element('reader-return').href, app.frameDoc.URL);
});

test('loading another article or hash cannot reset the recovery budget without an explicit open', () => {
  const app = rig();
  app.open('/doc/article.md');
  app.navigateAway();
  app.load()();
  app.renderDocument('https://kb.local/doc/other.md#section');
  app.load()();
  app.navigateAway('https://external.test/other');
  app.load()();
  assert.equal(app.element('reader-frame').hidden, true);
  assert.equal(app.run('readerRecoveryCount'), 1);
  assert.equal(app.element('reader-return').href, 'https://kb.local/doc/other.md#section');
  app.open(app.element('reader-return').href);
  assert.equal(app.run('readerRecoveryCount'), 0);
  assert.equal(app.element('reader-return').hidden, true);
  assert.equal(app.popups.length, 0);
});

test('a raw reader file is recognized by a successful matching server response header', async () => {
  const app = rig();
  app.open('/doc/asset.txt');
  app.frameDoc.URL = 'https://kb.local/doc/asset.txt';
  delete app.frameDoc.documentElement.dataset.readerPath;
  app.run(`fetch = async () => ({ ok: true, url: 'https://kb.local/doc/asset.txt',
    headers: { get: name => name === 'X-KB-Reader-Path' ? 'asset.txt' : null } })`);
  await app.load()();
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-error').hidden, true);
  assert.equal(app.element('reader-fav').dataset.path, 'asset.txt');
  assert.notEqual(app.run('readerAway'), null);
});

test('a missing raw file or mismatched header cannot impersonate a reader document', async () => {
  for (const [ok, path] of [[false, null], [true, 'other.txt'], [true, null]]) {
    const app = rig();
    app.open('/doc/asset.txt');
    app.frameDoc.URL = 'https://kb.local/doc/asset.txt';
    delete app.frameDoc.documentElement.dataset.readerPath;
    app.run(`fetch = async () => ({ ok: ${ok}, url: 'https://kb.local/doc/asset.txt',
      headers: { get: () => ${JSON.stringify(path)} } })`);
    await app.load()();
    assert.equal(app.element('reader-frame').hidden, true);
    assert.equal(app.element('reader-error').hidden, false);
    assert.equal(app.element('reader-return').hidden, true);
  }
});

test('a late raw-file identity response cannot alter a switched reader', async () => {
  const app = rig();
  app.open('/doc/asset.txt');
  app.frameDoc.URL = 'https://kb.local/doc/asset.txt';
  delete app.frameDoc.documentElement.dataset.readerPath;
  app.run('let finishFileCheck; fetch = () => new Promise(resolve => { finishFileCheck = resolve; })');
  const checking = app.load()();
  app.open('/doc/next.md');
  app.renderDocument('https://kb.local/doc/next.md');
  app.load()();
  const currentBinding = app.run('readerAway');
  app.run(`finishFileCheck({ ok: true, url: 'https://kb.local/doc/asset.txt',
    headers: { get: () => 'asset.txt' } })`);
  await checking;
  assert.equal(app.run('readerAway'), currentBinding);
  assert.equal(app.element('reader-frame').hidden, false);
  assert.equal(app.element('reader-fav').dataset.path, 'next.md');
});
