/* 浏览器停在另一个地址时,补抓页上**那条出路**。
 *
 * 站点按语言跳转(`openai.com/index/x` 在中文浏览器里落到 `/zh-Hans-CN/index/x`)
 * 之后,扩展只说一句「网址不同」。上面那一栏在有待办时是禁用的,所以出路只能开在
 * 待办那一行里:服务端在拒绝那一刻记下的真实地址摆出来,一颗按钮直接改用它 ——
 * 走的是同一个 `create`(旧待办由服务端顶掉),不是先取消再发起。
 * 反过来钉一边:没撞上(`seen_url` 为空、或和指定地址一样)时,这颗按钮不许出现。
 */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const ORIGINAL = 'https://openai.com/index/openai-models-google-cloud/';
const LANDED = 'https://openai.com/zh-Hans-CN/index/openai-models-google-cloud/';

let made = [];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', dataset: {}, hidden: false, disabled: false,
    classList: {toggle() {}, add() {}, remove() {}},
    querySelectorAll: () => [], replaceChildren() {}, append() {},
    addEventListener(name, callback) { this[name] = callback; }
  });
  return elements.get(id);
}
function node() {
  const fresh = {className: '', textContent: '', dataset: {}, children: [],
    classList: {toggle() {}, add() {}, remove() {}},
    append(...kids) { this.children.push(...kids); }};
  made.push(fresh);
  return fresh;
}
const switchButton = () => made.find(one => one.dataset.useUrl !== undefined);

const calls = [];
let state = {key: 'k1', title: '文章', url: ORIGINAL, kind: 'rss', saved: null, saved_error: '',
  pending: [{id: 'p1', key: 'k1', page_url: ORIGINAL, seen_url: LANDED,
             created_at: 1.79e9, expires_at: 1.79e9 + 900, status: 'pending'}]};
let scheduled;
const context = vm.createContext({
  URLSearchParams, Date, JSON,
  document: {getElementById: element, createElement: node},
  location: {search: '?item=k1', origin: 'http://127.0.0.1'},
  navigator: {clipboard: {writeText: async () => {}}},
  toast: () => {}, confirmBox: async () => true,
  setTimeout(callback) { scheduled = callback; return 1; }, clearTimeout() {},
  apiResponse: async reply => reply,
  fetch: async (path, options = {}) => { calls.push({path, ...options}); return structuredClone(state); }
});
const source = await readFile(new URL('../kb/static/js/browser_capture.js', import.meta.url), 'utf8');
vm.runInContext(source, context);
await new Promise(resolve => setImmediate(resolve));

// ① 拒绝那一刻的真实地址摆在待办里,带一颗改用它的按钮
const offer = switchButton();
assert.ok(offer, '浏览器停在另一个地址时应当给出改用它的出口');
assert.equal(offer.dataset.useUrl, LANDED);
assert.ok(made.some(one => one.textContent === LANDED), '那个地址要原样显示出来给人核对');

// ② 点它 = 用那个地址另发一个请求(同一个 create,不是先取消)
calls.length = 0;
await element('pending-list').click({target: {closest: selector =>
  selector === '[data-use-url]' ? offer : null}});
const posted = calls.find(call => call.path === '/api/browser-capture/intents');
assert.ok(posted, '应当发起新的采集请求');
assert.equal(posted.method, 'POST');
assert.deepEqual(JSON.parse(posted.body), {key: 'k1', page_url: LANDED});
assert.ok(!calls.some(call => call.method === 'DELETE'), '不该先取消再发起,中间那一下谁都接不住');

// ③ 反向钉一边:没撞上网址不同时,这颗按钮不许出现
for (const seen of ['', ORIGINAL]) {
  state = {...state, pending: [{...state.pending[0], seen_url: seen}]};
  made = [];
  await scheduled();
  assert.equal(switchButton(), undefined, `seen_url=${JSON.stringify(seen)} 时不该给改用按钮`);
}

console.log('browser capture other page: passed');
