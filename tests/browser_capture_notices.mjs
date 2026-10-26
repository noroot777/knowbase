import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', dataset: {},
    classList: {toggle() {}, add() {}, remove() {}},
    querySelectorAll: () => [], replaceChildren() {},
    addEventListener(name, callback) { this[name] = callback; },
    select() { this.selected = true; }
  });
  return elements.get(id);
}

let state = {key: 'fixture', title: 'Article', url: 'https://e.org/post', kind: 'rss',
  pending: [], saved: null, saved_error: ''};
let scheduled;
let pairingFails = false;
let resolveConfirmation;
let continueCalls = 0;
const notices = [];
const context = vm.createContext({
  URLSearchParams, Date, JSON,
  document: {getElementById: element}, location: {search: '?item=fixture', origin: 'http://127.0.0.1'},
  navigator: {clipboard: {writeText: async () => { throw new Error('Permission denied'); }}},
  toast: (text, options) => notices.push({text, options}),
  confirmBox: () => new Promise(resolve => { resolveConfirmation = resolve; }),
  setTimeout(callback) { scheduled = callback; return 1; }, clearTimeout() {},
  apiResponse: async response => response,
  fetch: async url => {
    if (url.endsWith('/pairing')) {
      if (pairingFails) throw new Error('Pairing <unavailable>');
      return {key: 'fixture-secret'};
    }
    if (url.endsWith('/continue')) { continueCalls++; return {detail: 'started', run: 'fixture'}; }
    return structuredClone(state);
  }
});
const source = await readFile(new URL('../kb/static/js/browser_capture.js', import.meta.url), 'utf8');
vm.runInContext(source, context);
await new Promise(resolve => setImmediate(resolve));

await element('pair').click();
assert.equal(element('pairing-key').value, 'fixture-secret');
await element('copy-key').click();
assert.equal(element('pairing-key').selected, true);
assert.match(notices.at(-1).text, /无法自动复制/);
assert.equal(notices.at(-1).options.type, 'info');
const copyNotice = notices.at(-1);
await scheduled();
assert.equal(notices.at(-1), copyNotice);
assert.match(element('message').textContent, /核对文章网址/);

pairingFails = true;
await element('pair').click();
assert.equal(notices.at(-1).text, 'Pairing <unavailable>');
const pairingNotice = notices.at(-1);
await scheduled();
assert.equal(notices.at(-1), pairingNotice);

state = {...state, saved: {id: 'first', receipt: {characters: 1400, captured_at: '2026-10-26T12:00:00+08:00'}}};
await scheduled();
const continuing = element('continue').click();
state = {...state, saved: {...state.saved, id: 'replacement'}};
await scheduled();
resolveConfirmation(true);
await continuing;
assert.equal(continueCalls, 0);
assert.match(notices.at(-1).text, /保存回执已经变化/);
const receiptNotice = notices.at(-1);
await scheduled();
assert.equal(notices.at(-1), receiptNotice);
assert.match(element('message').textContent, /正文已补回/);

console.log('browser capture notices: passed');
