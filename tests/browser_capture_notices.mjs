/* 采集抽屉的提示语:**toast 不许被每 2 秒那一次重画冲掉**,回执变了就不许发起处理。
 *
 * 这块界面每 2 秒重画一次(`render()`)。提示语要是写在那条路径上,人刚看见的
 * 「无法自动复制」「保存回执已经变化」两秒后就没了 —— 而那两句恰恰要他照着做事。
 *
 * 搬进抽屉之后这些话分在两个文件里:配对密钥那几句在 `capture_drawer.js`,
 * 「继续导出与索引」那几句在 `browser_capture.js`。**所以这条探针同时加载两份**
 * —— 拆成两条的话,「轮询会不会冲掉另一个文件写的 toast」就没有人问了,而那正是
 * 这条探针的全部理由。
 */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', dataset: {}, hidden: false, disabled: false, innerHTML: '',
    classList: {toggle() {}, add() {}, remove() {}},
    querySelectorAll: () => [], replaceChildren() {}, append() {},
    addEventListener(name, callback) { this[name] = callback; },
    select() { this.selected = true; }
  });
  return elements.get(id);
}
// 抽屉外壳建的那两个容器:innerHTML 在假 DOM 里不会真的生出子节点,但 element()
// 是按需造的,后面 getElementById('cap-pair') 照样拿得到同一个对象。
const node = () => ({
  className: '', textContent: '', innerHTML: '', dataset: {}, style: {}, isConnected: true,
  classList: {toggle() {}, add() {}, remove() {}},
  setAttribute() {}, append() {}, remove() {},
  querySelector: () => ({addEventListener() {}}),
  addEventListener(name, callback) { this[name] = callback; }
});

let state = {key: 'fixture', title: 'Article', url: 'https://e.org/post', kind: 'rss',
  pending: [], saved: null, saved_error: ''};
let scheduled;
let pairingFails = false;
let resolveConfirmation;
let continueCalls = 0;
const notices = [];
const context = vm.createContext({
  URLSearchParams, Date, JSON, setImmediate,
  window: {},
  overlayClosers: {},
  document: {getElementById: element, createElement: node, body: {append() {}}},
  location: {search: '?item=fixture', origin: 'http://127.0.0.1'},
  navigator: {clipboard: {writeText: async () => { throw new Error('Permission denied'); }}},
  toast: (text, options) => notices.push({text, options}),
  confirmBox: () => new Promise(resolve => { resolveConfirmation = resolve; }),
  setTimeout(callback) { scheduled = callback; return 1; }, clearTimeout() {},
  apiResponse: async response => response,
  fetch: async url => {
    if (url.endsWith('/setup')) return {json: async () => ({extension_dir: '/repo/ext', used_before: true})};
    if (url.endsWith('/pairing')) {
      if (pairingFails) throw new Error('Pairing <unavailable>');
      return {key: 'fixture-secret'};
    }
    if (url.endsWith('/continue')) { continueCalls++; return {detail: 'started', run: 'fixture'}; }
    return structuredClone(state);
  }
});
for (const name of ['browser_capture.js', 'capture_drawer.js']) {
  vm.runInContext(await readFile(new URL(`../kb/static/js/${name}`, import.meta.url), 'utf8'), context);
}
// 搬进抽屉之后这两段都不自启动了(条目 key 原先从地址栏取)——由入口调外壳。
context.window.openCaptureDrawer('fixture');
await new Promise(resolve => setImmediate(resolve));
await new Promise(resolve => setImmediate(resolve));

// ① 剪贴板被拒:要留一句能照着做的话,而且不许被两秒后那一次重画冲掉。
await element('cap-pair').click();
assert.equal(element('cap-pairing-key').value, 'fixture-secret');
await element('cap-copy-key').click();
assert.equal(element('cap-pairing-key').selected, true);
assert.match(notices.at(-1).text, /无法自动复制/);
assert.equal(notices.at(-1).options.type, 'info');
const copyNotice = notices.at(-1);
await scheduled();
assert.equal(notices.at(-1), copyNotice);
assert.match(element('cap-message').textContent, /核对文章网址/);

// ② 读密钥失败同理:服务端那句话原样留着。
pairingFails = true;
await element('cap-pair').click();
assert.equal(notices.at(-1).text, 'Pairing <unavailable>');
const pairingNotice = notices.at(-1);
await scheduled();
assert.equal(notices.at(-1), pairingNotice);

// ③ 确认框还开着的时候回执被换掉了:**一次都不许发起**,并且说清为什么。
state = {...state, saved: {id: 'first', receipt: {characters: 1400, captured_at: '2026-10-26T12:00:00+08:00'}}};
await scheduled();
const continuing = element('cap-continue').click();
state = {...state, saved: {...state.saved, id: 'replacement'}};
await scheduled();
resolveConfirmation(true);
await continuing;
assert.equal(continueCalls, 0);
assert.match(notices.at(-1).text, /保存回执已经变化/);
const receiptNotice = notices.at(-1);
await scheduled();
assert.equal(notices.at(-1), receiptNotice);
assert.match(element('cap-message').textContent, /正文已补回/);

console.log('browser capture notices: passed');
