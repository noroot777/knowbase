/* 扩展那一块「这一页」:**判据一处都不在扩展里,按钮按服务端那一答摆。**
 *
 * 钉这几件事:①这一页是什么情形由服务端 `/here` 一处答,扩展照着摆按钮、
 * 那句话原样显示(不自己另编);②不在库里时先让服务端自己去抓(只交地址、
 * 不交正文);③抓不到才轮到那两颗兜底,而且**不按失败原因分流** —— 无论登录墙、
 * 关卡还是脚本渲染,人的下一步都一样;④「改用所选文字」真的把 `selection` 交给
 * `extract.js`;⑤读页之前先问一次,预览上的字数用服务端那一份,而且不说「尚未
 * 上传」;⑥收下之后刷新失败不许报成提交失败;⑦正文已齐的那一页一颗按钮都不给;
 * ⑧**有待办指着另一个地址时把它摆出来**(站点按浏览器语言跳转那一类,只说一句
 * 「不在库里」会让人收录出第二行同一篇文章),「改用它」交的是这一页的地址;
 * ⑨配对密钥用一次不依赖当前标签的调用验。
 * 硬约束见 docs/decisions/link-grab.md 与 browser-capture.md。
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

//: 服务端那一答。每个用例改这一份,扩展只负责把它摆成界面。
let here = {ok: true, situation: 'absent', detail: '这一页还不在知识库里。',
            page_url: PAGE, item: null, intent: null, elsewhere: []};
let hereReply = null;      // 非空 = 这一问当场失败
let pendingReply = null;   // 非空 = 验密钥那一问当场失败
let grabReply = {status: 200, data: {ok: true, detail: '已收下，正在写入知识库'}};
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
    if (path === '/api/browser-capture/here') return hereReply || {status: 200, data: here};
    if (path === '/api/browser-capture/here/adopt') return {status: 200, data: {ok: true, id: 'i9'}};
    if (path.endsWith('/pending')) return pendingReply || {status: 200, data: {intents: []}};
    if (path === '/api/grab/from-page/precheck') return {status: 200, data: {ok: true, title: '服务端定的标题', characters: 880}};
    if (path === '/api/grab/from-page') return {status: 200, data: {ok: true, detail: '已收下，正在写入知识库'}};
    if (path === '/api/grab') return grabReply;
    throw new Error('unexpected request ' + path);
  }
});

const directory = new URL('../kb/static/browser-extension/knowbase-capture/', import.meta.url);
const source = await readFile(new URL('popup.js', directory), 'utf8');
vm.runInContext(source, context);
await new Promise(resolve => setImmediate(resolve));

// ① 不在库里:主按钮写「收录这条链接」,兜底两颗还藏着,那句话是服务端那一份
assert.ok(calls.some(c => c.path === '/api/browser-capture/here' && c.method === 'POST'),
  '该一问一答地问服务端这一页是什么情形');
assert.deepEqual(calls.find(c => c.path === '/api/browser-capture/here').sent, {url: PAGE});
assert.equal(element('here-actions').hidden, false);
assert.equal(element('act').textContent, '收录这条链接');
assert.equal(element('here-state').textContent, here.detail, '那句话原样显示,不自己另编');
assert.equal(element('act-page').hidden, true, '没失败之前不该提前给兜底那一颗');
assert.equal(element('act-selection').hidden, true);

// ② 点收录:只交地址,正文一个字都不交 —— 内容由服务端自己去抓
calls.length = 0;
here = {...here, situation: 'complete', detail: '这一条已经在知识库里，正文也齐了。'};
await element('act').click();
const posted = calls.find(c => c.path === '/api/grab' && c.method === 'POST');
assert.ok(posted, '该让服务端自己抓一次');
assert.deepEqual(posted.sent, {url: PAGE});
assert.match(element('status').textContent, /已收下/);
assert.equal(element('here-actions').hidden, true, '正文已齐的那一页一颗按钮都不给');

// ③ 服务端抓不到:兜底两颗一起露出。**不按失败原因分流** —— 「服务端抓不到」
//    和「扩展自己也认不出正文」是两回事,而这一刻还分不出是哪一种
here = {ok: true, situation: 'absent', detail: '这一页还不在知识库里。',
        page_url: PAGE, item: null, intent: null, elsewhere: []};
for (const detail of ['这一页没找到正文。先在浏览器里打开一次', '对方拦着你：请先完成验证', '这一页读不到']) {
  grabReply = {status: 400, detail};
  await element('refresh').click();
  await element('act').click();
  assert.equal(element('act-page').hidden, false, `失败原因「${detail}」也该给兜底出口`);
  assert.equal(element('act-selection').hidden, false, `失败原因「${detail}」也该给选文字那一颗`);
  assert.match(element('status').textContent, new RegExp(detail.slice(0, 6)));
}

// ④ 兜底那条路:读页之前先问服务端,预览上的字数用服务端那一份
calls.length = 0;
extracted.length = 0;
await element('act-page').click();
const checked = calls.find(c => c.path === '/api/grab/from-page/precheck');
assert.ok(checked, '读完要先问服务端这份正文收不收得了');
assert.deepEqual(checked.sent, {url: PAGE, title: '页面标题', body: captured.body});
assert.deepEqual(extracted, ['article'], '这一颗读的是自动识别的文章区域');
assert.equal(element('preview').hidden, false);
assert.match(element('count').textContent, /^880 字符/, '字数要用服务端那一份');
assert.doesNotMatch(element('count').textContent, /1500/);
assert.equal(element('page-title').textContent, '服务端定的标题');
// 这一刻正文**已经**交给本机服务核对过了,说成「尚未上传」会让人以为取消就
// 什么都没发生过
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

// ⑥ 「改用所选文字」:同一套预检、预览、确认,**而且真的交 `selection`**
calls.length = 0;
extracted.length = 0;
await element('refresh').click();
await element('act').click();            // 再撞一次失败,把兜底两颗露出来
await element('act-selection').click();
assert.deepEqual(extracted, ['selection'], '这一颗该把 selection 交给 extract.js');
assert.equal(element('preview').hidden, false, '选出来的文字也要先过预览');
assert.match(element('count').textContent, /你所选的文字/);
element('complete').checked = true;
await element('save').click();
assert.ok(calls.some(c => c.path === '/api/grab/from-page' && c.method === 'POST'),
  '选出来的文字该收录成新条目');
assert.ok(!calls.some(c => c.path.endsWith('/upload')), '新收录不许借补正文那条路');

// ⑦ 收下之后那一问失败:**不许报成提交失败**。已经收下了,而预览刚被清掉,
//    「可再次提交同一预览」他照着做不到,那句话还会让他以为没收下
calls.length = 0;
await element('refresh').click();
await element('act').click();
await element('act-page').click();
element('complete').checked = true;
hereReply = {status: 500, detail: '刷新状态失败'};
await element('save').click();
assert.ok(calls.some(c => c.path === '/api/grab/from-page' && c.method === 'POST'));
assert.match(element('status').textContent, /已收下/, '收下了就该说收下了');
assert.doesNotMatch(element('status').textContent, /再次提交/);
hereReply = null;

// ⑧ 有待办指着另一个地址:摆出来,并且「改用它」交的是**这一页**的地址。
//    站点按浏览器语言跳转那一类里,只说一句「不在库里」就请人收录,库里立刻
//    多出一行同一篇文章。
here = {ok: true, situation: 'absent', detail: '这一页还不在知识库里。', page_url: PAGE,
        item: null, intent: null,
        elsewhere: [{id: 'i1', key: 'k7', title: '那一篇', page_url: 'https://example.org/en/posts/hello'}]};
calls.length = 0;
await element('refresh').click();
assert.equal(element('elsewhere').hidden, false, '那个待办要摆在人眼前');
assert.equal(element('elsewhere-title').textContent, '那一篇');
assert.match(element('elsewhere-url').textContent, /\/en\/posts\/hello/);
await element('adopt').click();
const adopted = calls.find(c => c.path === '/api/browser-capture/here/adopt');
assert.ok(adopted, '「改用它」该走 adopt');
assert.deepEqual(adopted.sent, {key: 'k7', page_url: PAGE}, '交的是这一页的地址');

// ⑨ 反过来钉一边:没有这种待办时那一块不露面
here = {...here, elsewhere: []};
await element('refresh').click();
assert.equal(element('elsewhere').hidden, true);

// ⑩ 配对密钥用一次**不依赖当前标签**的调用验:人在扩展管理页或知识库页面上
//    配对时 `currentTab()` 先拒,拿 `/here` 验等于一个字都没验、还被当成连上了
calls.length = 0;
element('server').value = 'http://127.0.0.1:8787';
element('key').value = 'k'.repeat(44);
pendingReply = {status: 401, detail: '配对密钥不正确'};
await element('connect').click();
assert.ok(calls.some(c => c.path.endsWith('/pending')), '该先验一次密钥');
assert.match(element('status').textContent, /配对密钥不正确/);
pendingReply = null;

// ⑪ 结构:不许按失败文案分流。**钉形态,不钉关键词** —— 按词表匹配会连注释
//    一起误伤,而且换个说法就绕过去了;真正会出事的形态只有一种:把服务端那句
//    话拆开来自己判。
assert.doesNotMatch(source, /(message|detail)\s*\.\s*(includes|match|indexOf|startsWith|search)/,
  '按失败文案分流 = 把服务端的判据抄进扩展');
assert.doesNotMatch(source, /body_confirmed/);

console.log('grab from page gate: passed');
