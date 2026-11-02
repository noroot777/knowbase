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
    type: '', selected: false, select() { this.selected = true; },
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
//: 每次 build() 往 body 上挂的那两样(遮罩 + 抽屉)。留着是为了问得出「模板里那一栏
//: 长什么样」—— 第 ⑨ 条要比的正是模板和复位那一行说的是不是同一个默认。
const built = [];
let current = 'k1';
//: 服务端那一答里的回执。非空 = 这一条的正文已经补进去了。
let receipt = null;
const state = () => ({key: current, title: '文章', url: ITEMS[current], kind: 'rss',
  pending: [], saved: receipt, saved_error: ''});
//: 「同时在新标签打开这一篇」真的开出来的那些标签。
const opens = [];
let fetches = 0;
//: 真 = `/target` 那一问当场红(网络断了、服务重启中)。关抽屉那一下要现问一次
//: 服务端,所以「问不着的时候闭不闭嘴」得问得出来。
let targetFails = false;
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
  window: {open(...args) { opens.push(args); }}, overlayClosers: {},
  document: {getElementById: element, createElement: node, body: {append(...kids) { built.push(...kids); }},
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
    if (targetFails) throw new Error('读取目标条目失败');
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

// ② 关掉之后一次都不许再打接口。**关掉那一下本身也不许白问**:下面第 ⑩ 条那句
//    「这一次采成了没有」要现问一次服务端,而这里一个回调都没挂 —— 没人听的时候
//    连问都不该问。比的是**次数**:只问「还在不在轮询」的话,「每关一次多打一次」
//    看着跟干净停住一模一样。
const beforeClose = fetches;
context.window.closeCaptureDrawer();
assert.equal(fetches, beforeClose, '没人听那一声时,关掉这一下不该多问一次');
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

// ⑧b 同一个抽屉里的那条边界:他在扩展弹窗里把配对做完了,引导整套 DOM 换了一遍(收进
//     底部折叠块),而上一次渲染里那次复制还在等。抽屉一次都没关过 —— 只认「这一次
//     抽屉」的号码在这里恒绿,而迟到的失败照旧 `select()` 一个早就 remove 掉的输入框,
//     屏幕上那句「请复制选中的配对密钥」指着一处什么都没选中的地方。
await markChanged(null);
element('cap-copy-key').click();
await settle();
await markChanged('1.2.1');
toasts.length = 0;
element('cap-pairing-key').selected = false;
clipboardGate.no(new Error('剪贴板被拒'));
await settle();
assert.deepEqual(toasts, [], '引导重绘之后,上一套 DOM 上那次复制的迟到失败不许再说话');
assert.equal(element('cap-pairing-key').selected, false,
  '更不许去选一个早就 remove 掉的输入框');

// 反过来钉一边:中间没有重绘的话,那一句照旧要出来、那行密钥照旧要选上。
element('cap-copy-key').click();
await settle();
toasts.length = 0;
clipboardGate.no(new Error('剪贴板被拒'));
await settle();
assert.equal(toasts.length, 1, '没重绘时「请复制选中的配对密钥」照旧要说');
assert.equal(element('cap-pairing-key').selected, true, '也照旧要把那行密钥选上');

// 再钉一边:标记被重写成**同一个值**(`present.js` 每次重读都写一遍版本号)不算重绘。
// 当成重绘的话,位置根本没变,却把摊开的密钥收回去、把正在等的那次复制一起作废。
element('cap-copy-key').click();
await settle();
await markChanged('1.2.1');
toasts.length = 0;
clipboardGate.ok();
await settle();
assert.equal(toasts.length, 1, '同一个值再写一遍不算重绘,等着的那次复制照旧有回应');

// ⑨ 「同时在新标签打开这一篇」**默认勾着**,而那个默认写在两处,必须说同一句话。
//    模板里那个 `checked` 是读代码的人看到的默认;真正落到屏幕上的是 `startCapture()`
//    里那一次复位 —— 每次打开都重建一套 DOM,随后立刻跑它。所以只改模板的话,这一行
//    会不声不响把它顶回来,而模板从此是一句假话(反过来只改复位那一行同理)。
//    默认勾着的理由:这颗按钮发出的待办**只有人站到那一页上、在那儿点扩展才兑现得了**,
//    不开那一页按下去什么都不会发生。
context.window.closeCaptureDrawer();
current = 'k1';
context.window.openCaptureDrawer('k1');
await settle();
assert.equal(element('cap-also-open').checked, true, '打开抽屉时那个框就该是勾上的');
assert.match(built.at(-1).innerHTML, /id="cap-also-open"[^>]*\schecked/,
  '模板里那一栏也要带着 checked,别让它和复位那一行说两套话');

// 反过来钉一边:人把它取消掉之后,**这一次**不许被任何重画勾回去(面板每 2 秒重画
// 一次;勾回去的话他点发起就多开一个他已经说过不要的标签)。
element('cap-also-open').checked = false;
await tick();
assert.equal(element('cap-also-open').checked, false, '人取消掉之后不许被重画勾回去');

// ⑨b 取消掉之后按发起:**一个标签都不许开**(此刻它正是取消状态)。
element('cap-page-url').value = ITEMS.k1;
opens.length = 0;
element('cap-create').click();
await settle();
assert.deepEqual(opens, [], '他说过不要,就一个标签都不许开');
createGate.ok({ok: true});
await settle();

// ⑨c 勾着的时候真的把那一页开出来,而且**就在点下去那一瞬间**(不 await 就已经开了)。
//     `window.open` 一旦离开这次点击的同步调用栈就不再算用户手势,会被弹窗拦截挡掉 ——
//     而人看到的是「发起了,那一页没开」,接着在一个没有那一页的浏览器里等扩展。
context.window.closeCaptureDrawer();
context.window.openCaptureDrawer('k1');
await settle();
element('cap-page-url').value = ITEMS.k1;
opens.length = 0;
element('cap-create').click();            // 故意不 await
assert.deepEqual(opens, [[ITEMS.k1, '_blank', 'noopener']],
  '勾着就该开那一页,而且是同步开 —— 等一问一答回来就被拦截挡掉了');
createGate.ok({ok: true});
await settle();

// ⑩ 关掉抽屉时**回头告诉开它的那一方**。开它的那几处(「问题与待办」面板、订阅页)
//    在空闲时都不自己刷新:不回这一声的话,正文已经补上、那一条已经换了档位,屏幕上
//    还写着「重试已停止」,人唯一的出路是刷新整页。
//
//    两个口子**职责不同、能到的时间也不同**,所以分开钉:`onClose` 无条件、同步(底下
//    那一页正确与否全靠它,不许押在网络上);`onSettled` 只在「这一次真采成了」的时候
//    叫,而那件事的答案在服务端 —— 关得比一轮轮询(2 秒)快时这边一轮都没看见。
//
//    **下面一律比次数,不比有无**:「每次都叫、参数是空串」和「该叫时才叫」在只问
//    「叫没叫」的用例里长得一模一样,而消费方那边收到就是一句 toast。
let tells = [];
let closes = 0;
const hooks = () => ({onClose: () => { closes += 1; }, onSettled: note => { tells.push(note); }});
const fresh = () => { tells = []; closes = 0; };
const receiptOf = id => ({id, receipt: {characters: 900, captured_at: 1.79e12}});
let asked = 0;

// ⑩a 什么都没发生:那一声一次都不许有。
fresh();
context.window.closeCaptureDrawer();
current = 'k1';
receipt = null;
context.window.openCaptureDrawer('k1', hooks());
await settle();
asked = fetches;
context.window.closeCaptureDrawer();
assert.equal(closes, 1, '关掉就该让开它的那块面板重读一遍,不问条件、也不等那一问');
assert.equal(fetches, asked + 1, '有人听的时候,关掉这一下要现问一次服务端');
await settle();
assert.deepEqual(tells, [], '什么都没采就一声都不该有 —— 关掉抽屉本身不是采成了');

// ⑩b 正文真的补上了,而且这一轮轮询已经读回来了:这一声必须说得出所以然,
//     **并且不必再问一遍** —— 答案已经在手上。
fresh();
context.window.openCaptureDrawer('k1', hooks());
await settle();
receipt = receiptOf('rcpt-00000001');
await tick();
asked = fetches;
context.window.closeCaptureDrawer();
assert.equal(fetches, asked, '轮询已经看见了的事,关掉那一下不必再问服务端');
await settle();
assert.equal(tells.length, 1, '补上了就该回一声,而且只一声');
assert.match(tells[0], /正文/, '那一声要说得清是什么事');

// ⑩c **关得比一轮轮询快的那个窗口**(这一条是这整节的由来)。人在扩展弹窗里点完
//     回来,随手就把抽屉关了 —— 服务端此刻已经把回执写下了,而这边 2 秒一轮、一轮
//     都还没轮到。只认「关掉前那次轮询快照」的写法在这里**一声都没有**:正文明明补
//     上了,「问题与待办」里那一行还写着补抓失败的原话、右边还摆着「从浏览器采集」,
//     人以为没采成,再点一次。
fresh();
receipt = null;
context.window.openCaptureDrawer('k1', hooks());
await settle();
receipt = receiptOf('rcpt-00000002');   // 服务端写下了,故意不 tick:谁都没看见
asked = fetches;
context.window.closeCaptureDrawer();
assert.equal(fetches, asked + 1, '轮询没看见过的,必须现问一次服务端');
await settle();
assert.equal(tells.length, 1, '服务端这一刻有新回执,就该回一声');
assert.match(tells[0], /正文/, '那一声要说得清是什么事');

// ⑩d 反过来钉一边:**打开那一刻就已经在那儿**的回执不算这一次采的。算的话,每次
//     打开一条早就补过正文的条目、随手关掉,都会报一次「正文已补入原条目」,而其实
//     什么都没发生 —— 而这块面板上点进来的条目,相当一部分正是已经补过的。
//     **两条路各钉一次**(轮询那一路和关掉现问那一路,基准都只有一份)。
fresh();
receipt = receiptOf('rcpt-00000003');
context.window.openCaptureDrawer('k1', hooks());
await settle();
//: 轮询那一路要**再轮一轮**才问得出东西:只刷一次的话,基线那一道无论写对写错都还
//: 没被用到(实测把它里头记下的那个回执号删掉 —— 下一轮就会把一张老回执当成新的
//: —— 这条用例照旧全绿)。
await tick();
context.window.closeCaptureDrawer();
await settle();
assert.deepEqual(tells, [], '开抽屉时就在那儿的回执不算这一次的(轮询这一路)');

fresh();
context.window.openCaptureDrawer('k1', hooks());
await settle();
asked = fetches;
context.window.closeCaptureDrawer();   // 不 tick:走的是关掉现问那一路
assert.equal(fetches, asked + 1, '这一路确实问过了 —— 否则下一条断言恒绿');
await settle();
assert.deepEqual(tells, [], '开抽屉时就在那儿的回执不算这一次的(关掉现问这一路)');

// ⑩e **问不着就不报喜,不猜。** 漏一句提示的代价是人自己再看一眼那一行;认成采成了
//     的代价是指着一条根本没补上的文章说「正文已补入原条目」。
//     同时钉住 `onClose` 不受它牵连:底下那一页照旧重读。
fresh();
receipt = null;
context.window.openCaptureDrawer('k1', hooks());
await settle();
receipt = receiptOf('rcpt-00000004');
targetFails = true;
context.window.closeCaptureDrawer();
await settle();
targetFails = false;
assert.equal(closes, 1, '那一问红了也不该拖累底下那一页的重读');
assert.deepEqual(tells, [], '问不着的时候不许报喜');

// ⑩f **连基准都没拿到就关了**(第一轮轮询还在途)。这时候库里那张回执可能是上一次
//     留下的,跟刚写进去的长得一模一样 —— 没有基准就分不清,所以连问都不必问。
fresh();
receipt = receiptOf('rcpt-00000005');
context.window.openCaptureDrawer('k1', hooks());
asked = fetches;                        // 故意不 settle:这一次没有基准
context.window.closeCaptureDrawer();
assert.equal(fetches, asked, '没有基准时问回来也分不清新旧,不该问');
await settle();
assert.deepEqual(tells, [], '没有基准就不报喜');
receipt = null;

// ⑩g 再钉一边:**换一条打开时那一声不许串台**,而且上一次那一声要结清。回调跟着
//     「这一次打开」走,挂在模块上只认最后一个的写法会把 k1 那一声回给 k2 的开启方。
//
//     这一条还顺带钉住**那一问的头几行必须是同步的**:k1 的那一问在途时 `startCapture`
//     已经把模块状态换成 k2 了(基准变成 k2 看到的那张回执,`itemKey` 变成 k2)——
//     把「读结论、读基准、读 key」挪到 `await` 后面的话,k1 这一声当场消失。
const first = [];
const second = [];
receipt = null;
context.window.openCaptureDrawer('k1', {onSettled: note => { first.push(note); }});
await settle();
receipt = receiptOf('rcpt-00000006');   // k1 开着的时候补上了,没有哪一轮轮询看见
current = 'k2';
context.window.openCaptureDrawer('k2', {onSettled: note => { second.push(note); }});
await settle();
assert.equal(first.length, 1, '换一条打开 = 上一次那一声当场结清,不许欠着');
assert.match(first[0], /正文/, '结清的那一声说的是 k1 那一次的事');
assert.deepEqual(second, [], 'k1 那一声不许回给 k2 的开启方');
context.window.closeCaptureDrawer();
await settle();
assert.deepEqual(second, [], 'k2 这一次什么都没采(它进来就看见那张回执),不该有声');

console.log('capture drawer shell: passed');
