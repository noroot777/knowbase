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
 *   整屏最下面的折叠块里,他只会点下发起,然后等一个永远不来的采集。**而「配过没有」
 *   只有扩展自己知道** —— 它在页面上留一个 data 属性,这里读那一个;原先问的是服务端
 *   「全库有没有一张采集回执」,那是另一个范畴的事实,换浏览器、重装扩展都看不出来。
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
    type: '', select() {},
    classList: {toggle() {}, add() {}, remove() {}},
    querySelectorAll: () => [], append() {},
    //: 真 DOM 的 `replaceChildren()` 会把这个元素腾空,**这里必须照做** —— 空实现的话
    //: 「抽屉开着时引导换位置」那几条测的是桩而不是代码(不关掉重开,旧的那一大块
    //: 还留在 `innerHTML` 里)。
    replaceChildren() { this.innerHTML = ''; },
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
//: 非空 = 扣住 `/setup` 那一答,好检查首次引导的位置是不是已经定下来了。
let releaseSetup = null;
let holdSetup = false;
//: 「发起浏览器采集」那一问:扣住不答,好让人在它在途的时候关掉抽屉。
let createGate = null;
const timers = new Map();
let nextTimer = 1;
//: 剪贴板那一问:扣住不答,好让人在它在途的时候关掉抽屉。
let clipboardGate = null;
const toasts = [];
//: 真浏览器里是 MutationObserver。照它的契约做:**只有还连着的才会被叫**,这样
//: 「关掉之后有没有断开」才问得出来。
const watching = new Set();
class FakeObserver {
  constructor(callback) { this.callback = callback; }
  observe() { watching.add(this); }
  disconnect() { watching.delete(this); }
}
const context = vm.createContext({
  URLSearchParams, Date, JSON, setImmediate,
  window: {}, overlayClosers: {},
  document: {getElementById: element, createElement: node, body: {append() {}},
             //: 扩展留标记的地方(`present.js` 的 `data-knowbase-capture`)。
             documentElement: {dataset: {}}},
  location: {search: '', origin: 'http://127.0.0.1'},
  navigator: {clipboard: {writeText: () => new Promise((ok, no) => { clipboardGate = {ok, no}; })}},
  toast: text => { toasts.push(text); }, confirmBox: async () => false,
  MutationObserver: FakeObserver,
  setTimeout(callback) { const id = nextTimer++; timers.set(id, callback); return id; },
  clearTimeout(id) { timers.delete(id); },
  apiResponse: async reply => reply,
  fetch: async (url, options = {}) => {
    if (url.endsWith('/setup')) {
      const answer = {json: async () => ({extension_dir: '/repo/ext'})};
      if (!holdSetup) return answer;
      return new Promise(ok => { releaseSetup = () => ok(answer); });
    }
    if (url.endsWith('/intents') && options.method === 'POST')
      return new Promise((ok, no) => { createGate = {ok, no}; });
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

// ⑤ 这个浏览器没配过扩展:装扩展那一块摆在最上面、摊开;配过了:收进底部折叠块。
const mark = context.document.documentElement.dataset;
context.window.closeCaptureDrawer();
delete mark.knowbaseCapture;
context.window.openCaptureDrawer('k1');
await settle();
assert.match(element('cap-setup-slot').innerHTML, /cap-setup/, '没配过时这一块要摆在最上面');
assert.doesNotMatch(element('cap-setup-slot').innerHTML, /<details/, '没配过时不许把它折起来');
assert.equal(element('cap-setup-folded').innerHTML, '', '没配过时底部那块应当是空的');

context.window.closeCaptureDrawer();
mark.knowbaseCapture = '1.2.0';
context.window.openCaptureDrawer('k1');
await settle();
assert.match(element('cap-setup-folded').innerHTML, /<details/, '配过之后它应当收进折叠块');
assert.equal(element('cap-setup-slot').innerHTML, '', '配过之后顶部不该再占一大块');

// ⑤b 上面两条的**结构面**:判据既然是本地事实,位置就该在 `/setup` 答之前定下来。
//     谁再把它挂回某个服务端字段(原先是 `used_before` —— 全库审计表里有没有过一行),
//     这一条当场红。那张表答的是另一个问题:另一个浏览器采成过一次,这个还没配对的
//     就看不到引导了;反过来只用「改用这一页的正文」收录的人一直有配对却从不写回执,
//     引导永远摊在他眼前。`/setup` 那一问只剩一件事:装哪个目录。
context.window.closeCaptureDrawer();
delete mark.knowbaseCapture;
holdSetup = true;
context.window.openCaptureDrawer('k1');
await settle();
assert.match(element('cap-setup-slot').innerHTML, /cap-setup/, '不许等服务端答完才决定摆在哪');
assert.equal(element('cap-ext-path').textContent, '', '要等那一问的只有「装哪个目录」这一栏');
releaseSetup();
await settle();
assert.equal(element('cap-ext-path').textContent, '/repo/ext', '答回来了就把目录填上');
holdSetup = false;
mark.knowbaseCapture = '1.2.0';

// ⑤c 抽屉**开着的时候**配对完成:引导要自己收起来。配对是在另一个窗口(扩展弹窗)
//     里做的 —— 不盯着那个标记的话,他回到这一页看到的还是整块摊开的引导,刚做完的
//     事在界面上没有任何回应,得关掉再打开才看得出来。反过来一边同样钉上(密钥被清
//     掉、改了服务地址),那时引导必须自己回到顶上。
async function markChanged(value) {
  if (value === null) delete mark.knowbaseCapture; else mark.knowbaseCapture = value;
  for (const one of [...watching]) one.callback([], one);
  await settle();
}
context.window.closeCaptureDrawer();
await markChanged(null);
context.window.openCaptureDrawer('k1');
await settle();
assert.match(element('cap-setup-slot').innerHTML, /cap-setup/);
await markChanged('1.2.0');
assert.match(element('cap-setup-folded').innerHTML, /<details/, '开着的时候配好了就该自己收起来');
assert.equal(element('cap-setup-slot').innerHTML, '', '顶上那一大块要让出来');
await markChanged(null);
assert.match(element('cap-setup-slot').innerHTML, /cap-setup/, '反过来一边:配不上了要自己回到顶上');

// ⑤d 关掉之后要断开。不断的话这个已经没了的抽屉还会在每次标记变化时跑一遍
//     `renderSetup`(白打一次 `/setup`,再往早就 remove 掉的那套 DOM 里写),开过几次
//     就是几个。
context.window.closeCaptureDrawer();
assert.equal(watching.size, 0, '关掉抽屉要把标记那个观察器一起断开');
mark.knowbaseCapture = '1.2.0';

// ⑥ **关掉抽屉时正有一个写动作在途**:它回来之后不许把轮询重新排起来。
//    `stopCapture()` 作废得了在途的那次 `refresh`,作废不了「发起 / 取消 / 改址 /
//    继续处理」这些写动作的后半段 —— 它们 `await` 完回来还要自己 `refresh()` 一次,
//    而那一次会在 `finally` 里排下一轮 `setTimeout`。实测形态:发起后立刻关掉,
//    几秒后冒出一次 `GET /target/null`,再隔 2 秒又来一次 —— 本该停掉的轮询被一个
//    已经关掉的抽屉重新点着了。
context.window.closeCaptureDrawer();
context.window.openCaptureDrawer('k1');
await settle();
element('cap-page-url').value = ITEMS.k1;
element('cap-create').click();            // 不 await:它卡在 /intents 上
await settle();
const beforeRace = fetches;
context.window.closeCaptureDrawer();
createGate.ok({ok: true});                // 迟到的成功
await settle();
await tick();
await tick();
assert.equal(fetches, beforeRace, '关掉之后,那次发起的迟到回包不许把轮询重新排起来');

// ⑦ 迟到的**失败**不许落在下一篇头上。A 的网址填错、发起后立刻关掉、打开 B:
//    B 的标题网址都对、待办也正常,顶上却突然红字写着 A 那一次的拒绝理由,而且
//    被 `actionError` 留着不走。两条分开钉:上面那条即使这条坏了也会绿。
context.window.openCaptureDrawer('k1');
await settle();
element('cap-page-url').value = 'not-a-valid-url';
element('cap-create').click();
await settle();
context.window.closeCaptureDrawer();
current = 'k2';
context.window.openCaptureDrawer('k2');
await settle();
const clean = element('cap-message').textContent;
createGate.no(new Error('请使用不含账号密码的完整 HTTP / HTTPS 文章网址'));
await settle();
await settle();
assert.equal(element('cap-message').textContent, clean,
  '上一篇那次发起的迟到错误不许写到这一篇头上');

// ⑧ 复制路径 / 复制密钥那两处的迟到反馈同理。剪贴板那一问可以迟迟不决(权限弹窗、
//    系统忙),人这时关掉抽屉、打开下一篇,旧的那句「已复制」就冒在新抽屉上;失败
//    那一支更进一步 —— 它还会 `select()` 一个早就 remove 掉的输入框,而屏幕上那句
//    「请复制选中的配对密钥」指着一处什么都没选中的地方。
context.window.closeCaptureDrawer();
context.window.openCaptureDrawer('k1');
await settle();
element('cap-copy-path').click();
await settle();
context.window.closeCaptureDrawer();
current = 'k2';
context.window.openCaptureDrawer('k2');
await settle();
toasts.length = 0;
clipboardGate.ok();
await settle();
assert.deepEqual(toasts, [], '上一个抽屉那次复制的迟到回答不许冒在这一篇上');

// 失败那一支单独钉一次:它和成功那一支是两条分支,只守住一条的话另一条照旧会写。
element('cap-copy-key').click();
await settle();
context.window.closeCaptureDrawer();
current = 'k1';
context.window.openCaptureDrawer('k1');
await settle();
toasts.length = 0;
clipboardGate.no(new Error('剪贴板被拒'));
await settle();
assert.deepEqual(toasts, [], '迟到的复制失败同理');

// 反过来钉一边:抽屉还开着时那句话照旧要出来 —— 否则上面两条在「反馈整个没了」
// 的时候同样全绿。
element('cap-copy-path').click();
await settle();
toasts.length = 0;
clipboardGate.ok();
await settle();
assert.equal(toasts.length, 1, '抽屉还开着时「已复制」照旧要说一句');

console.log('capture drawer shell: passed');
