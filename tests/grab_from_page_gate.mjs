/* 扩展那颗「收录这条链接」:**判据一处都不在扩展里**。
 *
 * 钉这几件事:①这一页在不在知识库里由服务端答,不在才露出收录;②先让服务端自己
 * 去抓(只交地址、不交正文);③抓不到才轮到那两颗兜底,而且**不按失败原因分流**
 * —— 无论登录墙、关卡还是脚本渲染,人的下一步都一样;④「改用所选文字」真的把
 * `selection` 交给 `extract.js`(自动识别不出正文的页面只有人自己选那一段才取得
 * 到,少了这一颗,服务端那句「请选好全文再读一次」就指向一颗不存在的按钮);
 * ⑤读页之前先问一次服务端,预览上的字数用服务端那一份,而且不说「尚未上传」;
 * ⑥收下之后刷新失败不许报成提交失败;⑦待办那一问失败不许连「这一页」一起停掉;
 * ⑧反过来钉一边:已经在库里的那一页不给收录按钮。
 * 硬约束见 docs/decisions/link-grab.md。
 */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const PAGE = 'https://example.org/posts/hello';
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', checked: false, disabled: false, hidden: false, open: false,
    classList: {toggle() {}, add() {}, remove() {}},
    append() {}, replaceChildren() {},
    addEventListener(name, callback) { this[name] = callback; }
  });
  return elements.get(id);
}

let known = false;
let finished = true;
let grabReply = {status: 200, data: {ok: true, detail: '已收下，正在写入知识库'}};
let stateReply = null;      // 非空 = 这一问当场失败
let pendingReply = null;    // 非空 = 待办那一问当场失败
const calls = [];
const extracted = [];
const tab = {id: 7, url: PAGE};
const captured = {page_url: PAGE, title: '页面标题', body: 'x'.repeat(1500), method: 'article'};

const context = vm.createContext({
  URL, Date, JSON, encodeURIComponent,
  document: {getElementById: element, createElement: () => ({})},
  chrome: {
    tabs: {query: async () => [tab]},
    scripting: {executeScript: async ({files, args}) => {
      if (files) return [{}];
      extracted.push(args[0]);
      return [{result: {...captured, method: args[0]}}];
    }},
    storage: {local: {get: async () => ({server: 'http://127.0.0.1:8787', key: 'k'.repeat(44)}), set: async () => {}}}
  },
  apiResponse: async reply => {
    if (reply.status >= 400) { const error = new Error(reply.detail); error.status = reply.status; throw error; }
    return reply.data;
  },
  fetch: async (url, options = {}) => {
    const path = url.replace('http://127.0.0.1:8787', '');
    calls.push({path, method: options.method || 'GET', sent: options.body ? JSON.parse(options.body) : null});
    if (path.startsWith('/api/grab/state')) {
      if (stateReply) return stateReply;
      return {status: 200, data: {ok: true, known, done: known && finished, title: '库里那一篇',
                                  resume: '，稍后在「手动收录」里点一次「写入知识库」'}};
    }
    if (path === '/api/grab/from-page/precheck') return {status: 200, data: {ok: true, title: '服务端定的标题', characters: 880}};
    if (path === '/api/grab/from-page') return {status: 200, data: {ok: true, detail: '已收下，正在写入知识库'}};
    if (path === '/api/grab') return grabReply;
    if (path.endsWith('/pending')) return pendingReply || {status: 200, data: {intents: []}};
    throw new Error('unexpected request ' + path);
  }
});

const directory = new URL('../kb/static/browser-extension/knowbase-capture/', import.meta.url);
const source = await readFile(new URL('popup.js', directory), 'utf8');
vm.runInContext(source, context);
await new Promise(resolve => setImmediate(resolve));

// ① 不在库里:收录那一块露出来,兜底那一颗还藏着
assert.ok(calls.some(c => c.path.startsWith('/api/grab/state')), '该先问服务端这一页在不在库里');
assert.equal(element('here-actions').hidden, false);
assert.equal(element('collect-page').hidden, true, '没失败之前不该提前给兜底那一颗');
assert.equal(element('collect-selection').hidden, true);
assert.match(element('here-state').textContent, /还不在知识库/);

// ② 点收录:只交地址,正文一个字都不交 —— 内容由服务端自己去抓
calls.length = 0;
known = true;
await element('collect').click();
const posted = calls.find(c => c.path === '/api/grab' && c.method === 'POST');
assert.ok(posted, '该让服务端自己抓一次');
assert.deepEqual(posted.sent, {url: PAGE});
assert.match(element('status').textContent, /已收下/);
assert.equal(element('here-actions').hidden, true, '收下之后这一块该自己收起来');

// ③ 服务端抓不到:兜底那一颗露出来。**不按失败原因分流**
for (const detail of ['这一页没找到正文。先在浏览器里打开一次', '对方拦着你：请先完成验证', '这一页读不到']) {
  known = false;
  grabReply = {status: 400, detail};
  await element('collect').click();
  assert.equal(element('collect-page').hidden, false, `失败原因「${detail}」也该给兜底出口`);
  // 两颗一起露出:「服务端抓不到」和「它自动识别不出正文」是两回事,而这一刻
  // 还分不出是哪一种 —— 只给前一颗的话,后一种的人无处可点。
  assert.equal(element('collect-selection').hidden, false, `失败原因「${detail}」也该给选文字那一颗`);
  assert.match(element('status').textContent, new RegExp(detail.slice(0, 6)));
  element('collect-page').hidden = true;
  element('collect-selection').hidden = true;
}
element('collect-page').hidden = false;
element('collect-selection').hidden = false;

// ④ 兜底那条路:读页之前先问服务端,预览上的字数用服务端那一份
calls.length = 0;
await element('collect-page').click();
const checked = calls.find(c => c.path === '/api/grab/from-page/precheck');
assert.ok(checked, '读完要先问服务端这份正文收不收得了');
assert.deepEqual(checked.sent, {url: PAGE, title: '页面标题', body: captured.body});
assert.equal(element('preview').hidden, false);
assert.match(element('count').textContent, /^880 字符/, '字数要用服务端那一份');
assert.doesNotMatch(element('count').textContent, /1500/);
assert.equal(element('page-title').textContent, '服务端定的标题');
assert.deepEqual(extracted, ['article'], '这一颗读的是自动识别的文章区域');
// 这一刻正文**已经**交给本机服务核对过了(上面那次预检)。说成「尚未上传」
// 会让人以为取消就什么都没发生过。
assert.doesNotMatch(element('count').textContent, /尚未上传/);
assert.match(element('count').textContent, /尚未收录/);

// ⑤ 勾确认之后提交,走的是 from-page 那条路
calls.length = 0;
element('complete').checked = true;
await element('save').click();
const sent = calls.find(c => c.path === '/api/grab/from-page' && c.method === 'POST');
assert.ok(sent, '该提交到 from-page');
assert.deepEqual(sent.sent, {url: PAGE, title: '页面标题', body: captured.body});
assert.ok(!calls.some(c => c.path.endsWith('/upload')), '收录不该走补正文那条路');

// ⑥ 「改用所选文字」:同一套预检、预览、确认,**而且真的交 `selection`**。
//    自动识别不出正文的页面(整页脚本渲染的壳、正文在 iframe 里、没有能认出的
//    文章容器)只有人自己选那一段才取得到。走到 `/upload` 就是暗中借了补正文
//    那条路 —— 那条路非得先有一个补抓待办,新收录这一页压根没有。
calls.length = 0;
extracted.length = 0;
await element('collect-selection').click();
assert.deepEqual(extracted, ['selection'], '这一颗该把 selection 交给 extract.js');
assert.equal(element('preview').hidden, false, '选出来的文字也要先过预览');
assert.ok(calls.some(c => c.path === '/api/grab/from-page/precheck'), '同一套预检');
assert.match(element('count').textContent, /你所选的文字/);
element('complete').checked = true;
await element('save').click();
assert.ok(calls.some(c => c.path === '/api/grab/from-page' && c.method === 'POST'),
  '选出来的文字该收录成新条目');
assert.ok(!calls.some(c => c.path.endsWith('/upload')), '新收录不许借补正文那条路');

// ⑦ 收下之后那一问失败:**不许报成提交失败**。已经收下了,而预览刚被清掉,
//    「可再次提交同一预览」他照着做不到,更要紧的是那句话会让他以为没收下。
calls.length = 0;
await element('collect-page').click();
element('complete').checked = true;
stateReply = {status: 500, detail: '刷新状态失败'};
await element('save').click();
assert.ok(calls.some(c => c.path === '/api/grab/from-page' && c.method === 'POST'));
assert.match(element('status').textContent, /已收下/, '收下了就该说收下了');
assert.doesNotMatch(element('status').textContent, /再次提交/);
stateReply = null;

// ⑧ 待办那一问失败,不许连「这一页」一起停掉:两件事互不依赖,一边连不上时
//    另一边照旧可用。写成 `await loadIntents()` 打头就会让这一整块永远停在
//    「正在确认这一页…」 —— 页面零报错,像卡死。
calls.length = 0;
known = false;
element('here-state').textContent = '正在确认这一页…';
pendingReply = {status: 500, detail: '待办读不到'};
await element('refresh').click();
assert.ok(calls.some(c => c.path.startsWith('/api/grab/state')), '待办失败也该照旧问这一页');
assert.match(element('here-state').textContent, /还不在知识库/);
assert.match(element('status').textContent, /待办读不到/, '待办那个错照旧要说出来');
pendingReply = null;

// ⑨ 收过但没写完:**服务端那句「下一步去哪儿点」原样说出来**,不自己另编一句
//    (那等于把它的口径抄进扩展,它改了这里不会跟着改)。
known = true;
finished = false;
await element('refresh').click();
assert.match(element('here-state').textContent, /还没写完，稍后在「手动收录」里点一次「写入知识库」/);
assert.equal(element('here-actions').hidden, true, '收过的那一页不给收录按钮');

// ⑩ 反过来钉一边:已经写完在库里的那一页,一颗收录按钮都不给
finished = true;
await element('refresh').click();
assert.equal(element('here-actions').hidden, true);
assert.match(element('here-state').textContent, /已在知识库/);

// ⑦ 结构:不许按失败文案分流。**钉形态,不钉关键词** —— 按词表匹配会连注释
//    一起误伤(上面那段注释说的正是「不分流」),而且换个说法就绕过去了;
//    真正会出事的形态只有一种:把服务端那句话拆开来自己判。
assert.doesNotMatch(source, /(message|detail)\s*\.\s*(includes|match|indexOf|startsWith|search)/,
  '按失败文案分流 = 把服务端的判据抄进扩展');
assert.doesNotMatch(source, /body_confirmed/);

console.log('grab from page gate: passed');
