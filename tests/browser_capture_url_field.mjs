/* 补抓页那一栏网址:**人动过就不许被轮询填回去**。
 *
 * 本页每 2 秒重画一次。原来的判据是「框是空的就当还没填过」,于是人清空、切出去
 * 复制浏览器里真正的地址、切回来一粘贴,贴的那一截接在被填回的原网址后面,拼出
 * 一个谁也打不开的地址。两个触发点都要钉:平常那一次,和点过「重新采集正文」
 * 进草稿态之后。反过来也钉一边:没人动过的空框照旧填默认值。
 */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', dataset: {}, hidden: false, disabled: false,
    classList: {toggle() {}, add() {}, remove() {}},
    querySelectorAll: () => [], replaceChildren() {}, append() {},
    addEventListener(name, callback) { this[name] = callback; },
    select() {}
  });
  return elements.get(id);
}
const node = () => ({className: '', textContent: '', dataset: {}, append() {},
  classList: {toggle() {}, add() {}, remove() {}}});

const ORIGINAL = 'https://e.org/文章/第一篇';
let state = {key: 'k1', title: '文章', url: ORIGINAL, kind: 'rss', pending: [], saved: null, saved_error: ''};
let scheduled;
let resolveConfirmation;
const context = vm.createContext({
  URLSearchParams, Date, JSON,
  document: {getElementById: element, createElement: node},
  location: {search: '?item=k1', origin: 'http://127.0.0.1'},
  navigator: {clipboard: {writeText: async () => {}}},
  toast: () => {}, confirmBox: () => new Promise(resolve => { resolveConfirmation = resolve; }),
  setTimeout(callback) { scheduled = callback; return 1; }, clearTimeout() {},
  apiResponse: async reply => reply,
  fetch: async () => structuredClone(state)
});
const source = await readFile(new URL('../kb/static/js/browser_capture.js', import.meta.url), 'utf8');
vm.runInContext(source, context);
await new Promise(resolve => setImmediate(resolve));
const field = element('page-url');

// ① 反向钉:没人动过的空框照旧填上条目原网址
assert.equal(field.value, ORIGINAL);
await scheduled();
assert.equal(field.value, ORIGINAL);

// ② 人清空:轮询不许把原网址填回来
field.value = '';
await scheduled();
await scheduled();
assert.equal(field.value, '');

// ③ 人粘贴真实最终网址:原封不动,不会被拼接
field.value = 'https://real.example.org/final';
await scheduled();
assert.equal(field.value, 'https://real.example.org/final');

// ④ 草稿态(已保存 + 点过「重新采集正文」)是第二个触发点
state = {...state, saved: {id: 's1', receipt: {characters: 666, captured_at: '2026-10-26T12:00:00+08:00'}}};
await scheduled();
const restarting = element('restart').click();
resolveConfirmation(true);
await restarting;
assert.equal(field.disabled, false, '草稿态下这一栏应当可以改');
field.value = '';
await scheduled();
await scheduled();
assert.equal(field.value, '');

// ⑤ 已经有请求在等:框里一律显示服务端记下的那一份(百分号编码的中文显示成可读形式)
field.value = '人随手打的半截';
state = {...state, saved: null, pending: [{id: 'p1', key: 'k1', page_url: 'https://e.org/%E6%96%87%E7%AB%A0/%E7%AC%AC%E4%B8%80%E7%AF%87',
  created_at: 1.79e9, expires_at: 1.79e9 + 900, status: 'pending'}]};
await scheduled();
assert.equal(field.value, ORIGINAL);
assert.equal(field.disabled, true);

// ⑥ 请求取消之后,框里留着上次那一份,不被原网址顶掉
state = {...state, pending: []};
field.value = 'https://real.example.org/final';
await scheduled();
assert.equal(field.value, 'https://real.example.org/final');

console.log('browser capture url field: passed');
