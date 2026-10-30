/* 扩展那颗「收录这条链接」:**判据一处都不在扩展里**。
 *
 * 钉五件事:①这一页在不在知识库里由服务端答,不在才露出收录;②先让服务端自己去
 * 抓(只交地址、不交正文);③抓不到才轮到「改用这一页的正文」,而且**不按失败原因
 * 分流** —— 无论登录墙、关卡还是脚本渲染,人的下一步都一样;④那条路读页之前先
 * 问一次服务端,预览上的字数用服务端那一份;⑤反过来钉一边:已经在库里的那一页
 * 不给收录按钮。硬约束见 docs/decisions/link-grab.md。
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
let grabReply = {status: 200, data: {ok: true, detail: '已收下，正在写入知识库'}};
const calls = [];
const tab = {id: 7, url: PAGE};
const captured = {page_url: PAGE, title: '页面标题', body: 'x'.repeat(1500), method: 'article'};

const context = vm.createContext({
  URL, Date, JSON, encodeURIComponent,
  document: {getElementById: element, createElement: () => ({})},
  chrome: {
    tabs: {query: async () => [tab]},
    scripting: {executeScript: async ({files}) => (files ? [{}] : [{result: captured}])},
    storage: {local: {get: async () => ({server: 'http://127.0.0.1:8787', key: 'k'.repeat(44)}), set: async () => {}}}
  },
  apiResponse: async reply => {
    if (reply.status >= 400) { const error = new Error(reply.detail); error.status = reply.status; throw error; }
    return reply.data;
  },
  fetch: async (url, options = {}) => {
    const path = url.replace('http://127.0.0.1:8787', '');
    calls.push({path, method: options.method || 'GET', sent: options.body ? JSON.parse(options.body) : null});
    if (path.startsWith('/api/grab/state')) return {status: 200, data: {ok: true, known, done: known, title: '库里那一篇'}};
    if (path === '/api/grab/from-page/precheck') return {status: 200, data: {ok: true, title: '服务端定的标题', characters: 880}};
    if (path === '/api/grab/from-page') return {status: 200, data: {ok: true, detail: '已收下，正在写入知识库'}};
    if (path === '/api/grab') return grabReply;
    if (path.endsWith('/pending')) return {status: 200, data: {intents: []}};
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
  assert.match(element('status').textContent, new RegExp(detail.slice(0, 6)));
  element('collect-page').hidden = true;
}

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

// ⑤ 勾确认之后提交,走的是 from-page 那条路
calls.length = 0;
element('complete').checked = true;
await element('save').click();
const sent = calls.find(c => c.path === '/api/grab/from-page' && c.method === 'POST');
assert.ok(sent, '该提交到 from-page');
assert.deepEqual(sent.sent, {url: PAGE, title: '页面标题', body: captured.body});
assert.ok(!calls.some(c => c.path.endsWith('/upload')), '收录不该走补正文那条路');

// ⑥ 反过来钉一边:已经在库里的那一页,一颗收录按钮都不给
known = true;
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
