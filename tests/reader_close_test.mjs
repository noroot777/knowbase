import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const root = new URL('../kb/static/js/', import.meta.url);

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
  const document = { ...target(), activeElement: null, getElementById: element };
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
  const state = vm.createContext({ document, window, location: { origin: 'https://kb.local' },
    setTimeout: () => 1, clearTimeout() {},
    ResizeObserver: class { observe() {} disconnect() { disconnected = true; } },
  });
  for (const name of ['scrollaway.js', 'core.js'])
    vm.runInContext(readFileSync(new URL(name, root), 'utf8'), state, { filename: name });
  return { element, document, window, frameDoc, links,
    run: code => vm.runInContext(code, state),
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
  app.run('readerAway = { release() { throw new Error("cleanup failed"); } }');
  assert.throws(() => app.run('closeReader()'), /cleanup failed/);
  assert.equal(app.element('reader').style.display, 'none');
  assert.equal(app.element('reader-mask').style.display, 'none');
  assert.equal(app.element('reader-frame').src, 'about:blank');
  assert.equal(app.run('readerAway'), null);
  assert.doesNotThrow(() => app.run('closeReader()'));
});

test('reader external links open safely outside the frame without changing local anchors or Obsidian', () => {
  const app = rig();
  const link = href => Object.assign(new URL(href, 'https://kb.local/doc/article.md'),
    { target: '', relList: new Set() });
  const external = link('https://example.com/article');
  const protocolRelative = link('//example.org/');
  const local = link('/doc/next.md');
  const anchor = link('#section');
  const obsidian = link('obsidian://open?vault=notes');
  external.relList.add('nofollow');
  app.links.push(external, protocolRelative, local, anchor, obsidian);
  app.run('bindReaderScrollAway()');
  for (const externalLink of [external, protocolRelative]) {
    assert.equal(externalLink.target, '_blank');
    assert.equal(externalLink.relList.has('noopener'), true);
  }
  assert.equal(external.relList.has('nofollow'), true);
  for (const localLink of [local, anchor, obsidian]) assert.equal(localLink.target, '');
});
