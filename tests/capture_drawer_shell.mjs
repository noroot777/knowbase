/* 采集抽屉的外壳:**关掉要真的停**,**重开要能用**,首次使用那块要摆在看得见的地方。
 *
 * 这三件事都是「补抓页」变成抽屉之后才有的,而且回退之后**界面上一点都看不出来**:
 *
 * - 独立页关掉标签,轮询跟着没了;抽屉关掉之后底下那一页还活着,`setTimeout` 链
 *   不停就会一直每 2 秒打一次 `/target/<key>`,开过几次之后变成几条并行的链。
 * - 抽屉是建了就 append、关了就 remove 的(workspace.js 的 observer 只盯页面初始
 *   那批浮层的 style,切 display 它看不见)。于是每次打开拿到的是一套**全新元素**
 *   —— 状态机里那个「只绑一次」的标志会让第二次打开起每一颗按钮都没有监听器:
 *   界面完整、数据也在、点下去什么都不发生。
 * - 扩展没装、密钥没配的人,那一栏网址和「发起采集」对他毫无意义;那一块要是收在
 *   整屏最下面的折叠块里,他只会点下发起,然后等一个永远不来的采集。
 *
 * 所以这里的假 DOM **照着真实生命周期走**:抽屉 remove 掉,它底下那些元素就跟着
 * 作废。否则第二条根本测不出来 —— 元素留着的话,第一次绑的监听器也还留着。
 */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

let elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', dataset: {}, hidden: false, disabled: false, innerHTML: '',
    classList: {toggle() {}, add() {}, remove() {}},
    querySelectorAll: () => [], replaceChildren() {}, append() {},
    addEventListener(name, callback) { this[name] = callback; }
  });
  return elements.get(id);
}
const node = () => ({
  className: '', textContent: '', innerHTML: '', dataset: {}, style: {}, isConnected: true,
  classList: {toggle() {}, add() {}, remove() {}},
  setAttribute() {}, append() {},
  // 抽屉被移除 = 它底下那套 DOM 作废。真实浏览器里就是这样,而第二次打开要不要
  // 重新绑定,全看这一点。
  remove() { this.isConnected = false; elements = new Map(); },
  querySelector: () => ({addEventListener() {}}),
  addEventListener(name, callback) { this[name] = callback; }
});

const ITEMS = {k1: 'https://e.org/第一篇', k2: 'https://e.org/第二篇'};
let current = 'k1';
const state = () => ({key: current, title: '文章', url: ITEMS[current], kind: 'rss',
  pending: [], saved: null, saved_error: ''});
let fetches = 0;
let usedBefore = true;
const timers = new Map();
let nextTimer = 1;
const context = vm.createContext({
  URLSearchParams, Date, JSON, setImmediate,
  window: {}, overlayClosers: {},
  document: {getElementById: element, createElement: node, body: {append() {}}},
  location: {search: '', origin: 'http://127.0.0.1'},
  navigator: {clipboard: {writeText: async () => {}}},
  toast: () => {}, confirmBox: async () => false,
  setTimeout(callback) { const id = nextTimer++; timers.set(id, callback); return id; },
  clearTimeout(id) { timers.delete(id); },
  apiResponse: async reply => reply,
  fetch: async url => {
    if (url.endsWith('/setup')) return {json: async () => ({extension_dir: '/repo/ext', used_before: usedBefore})};
    fetches += 1;
    return state();
  }
});
for (const name of ['browser_capture.js', 'capture_drawer.js']) {
  vm.runInContext(await readFile(new URL(`../kb/static/js/${name}`, import.meta.url), 'utf8'), context);
}
const settle = async () => { for (let i = 0; i < 4; i += 1) await new Promise(r => setImmediate(r)); };
// 把此刻排着的那些回调都走一遍(真实里就是 2 秒后那一次重画)。
async function tick() {
  const due = [...timers.entries()];
  timers.clear();
  for (const [, callback] of due) await callback();
  await settle();
}

// ① 开着的时候**确实在轮询** —— 反过来钉这一边,否则下面那条在「轮询整个坏了」
//    的时候同样全绿。
context.window.openCaptureDrawer('k1');
await settle();
const opened = fetches;
assert.ok(opened > 0, '打开抽屉应当读一次目标条目');
await tick();
assert.ok(fetches > opened, '开着的时候应当继续轮询');

// ② 关掉之后一次都不许再打接口。
const beforeClose = fetches;
context.window.closeCaptureDrawer();
await tick();
await tick();
assert.equal(fetches, beforeClose, '关掉抽屉之后不许再轮询');

// ③ 重新打开:每一颗按钮都得重新绑上。上面那次 remove 已经把元素全作废了,
//    「只绑一次」的写法在这里会让 click 根本不存在。
context.window.openCaptureDrawer('k1');
await settle();
assert.equal(typeof element('cap-create').click, 'function', '重新打开后「发起采集」必须还能点');
assert.equal(typeof element('cap-continue').click, 'function', '重新打开后「继续导出与索引」必须还能点');
assert.ok(fetches > beforeClose, '重新打开应当重新读一次条目');

// ④ 换一条条目打开,那一栏要填上**新这条**的原网址。
//    测的是模块级状态有没有跟着换:DOM 每次都是新的(上面那次 remove 清掉了),
//    所以「上一条的值还在不在」问不出东西 —— 真正会串台的是 `urlAuto` 那个自动
//    填入基准。它留着上一篇的网址时,这一次 `autofillUrl` 会被自己那道「人动过就
//    不填」的闸挡掉(新框是空的,和基准对不上),于是网址栏**空着**,人点发起就是
//    拿一个空地址去发。
current = 'k2';
context.window.openCaptureDrawer('k2');
await settle();
assert.equal(element('cap-page-url').value, ITEMS.k2, '换一条打开时这一栏要填上新这条的原网址');
current = 'k1';

// ⑤ 没采成过:装扩展那一块摆在最上面、摊开;采成过:收进底部折叠块。
context.window.closeCaptureDrawer();
usedBefore = false;
context.window.openCaptureDrawer('k1');
await settle();
assert.match(element('cap-setup-slot').innerHTML, /cap-setup/, '首次使用时这一块要摆在最上面');
assert.doesNotMatch(element('cap-setup-slot').innerHTML, /<details/, '首次使用时不许把它折起来');
assert.equal(element('cap-setup-folded').innerHTML, '', '首次使用时底部那块应当是空的');

context.window.closeCaptureDrawer();
usedBefore = true;
context.window.openCaptureDrawer('k1');
await settle();
assert.match(element('cap-setup-folded').innerHTML, /<details/, '用过之后它应当收进折叠块');
assert.equal(element('cap-setup-slot').innerHTML, '', '用过之后顶部不该再占一大块');

console.log('capture drawer shell: passed');
