/* 扩展侧的准入口径:**它一处都不许自己判**。
 *
 * 钉这几件事:①读页之前先问服务端,不通过就一个字都不读(`injections` 不动);
 * ②正文那一问没过就不渲染预览;③预览上的字数是服务端报的那一份,不是扩展
 * 自己数的原始长度;④**待办是服务端那一答带回来的,不是人从下拉框里挑的** ——
 * 「这一页是哪一条」地址栏里就写着,让人再答一遍要跨三个界面走七步。
 * 事故形态与硬约束见 docs/decisions/browser-capture.md。
 */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

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

const INTENT = {id: 'i1', key: 'k1', title: '文章', page_url: 'https://e.org/%E6%96%87%E7%AB%A0',
  created_at: 1.79e9, expires_at: 1.79e9 + 900, status: 'pending'};
//: 服务端对「我站着这一页」那一答。`fill` = 库里有这一条、正文还缺着,待办
//: 由它自己建好带回来(`intent`)。
const HERE = {ok: true, situation: 'fill', detail: '知识库里有这一条，但正文还缺着。',
  page_url: INTENT.page_url, item: {key: 'k1', kind: 'rss', title: '文章', url: 'https://e.org/文章'},
  intent: INTENT, elsewhere: []};
let prechecks = [];
let replies = [];
let injections = 0;
let uploads = 0;
const tab = {id: 7, url: 'https://e.org/文章'};
const captured = {page_url: 'https://e.org/文章', title: '文章', body: 'x'.repeat(1200), method: 'article'};

const context = vm.createContext({
  URL, Date, JSON,
  document: {getElementById: element, createElement: () => ({})},
  chrome: {
    tabs: {query: async () => [tab]},
    scripting: {executeScript: async ({files}) => { injections += 1; return files ? [{}] : [{result: captured}]; }},
    storage: {local: {get: async () => ({server: 'http://127.0.0.1:8787', key: 'k'.repeat(44)}), set: async () => {}}}
  },
  apiResponse: async reply => {
    if (reply.status >= 400) { const error = new Error(reply.detail); error.status = reply.status; throw error; }
    return reply.data;
  },
  fetch: async (url, options = {}) => {
    const sent = options.body ? JSON.parse(options.body) : null;
    if (url.endsWith('/here')) return {status: 200, data: HERE};
    if (url.endsWith('/pending')) return {status: 200, data: {intents: [INTENT]}};
    if (url.endsWith('/precheck')) { prechecks.push(sent); return replies.shift() ?? {status: 200, data: {ok: true, characters: 694}}; }
    if (url.endsWith('/upload')) { uploads += 1; return {status: 200, data: {detail: '已补入'}}; }
    throw new Error('unexpected request ' + url);
  }
});

const directory = new URL('../kb/static/browser-extension/knowbase-capture/', import.meta.url);
const source = await readFile(new URL('popup.js', directory), 'utf8');
const extractor = await readFile(new URL('extract.js', directory), 'utf8');
vm.runInContext(source, context);
await new Promise(resolve => setImmediate(resolve));

// ⓞ 待办是服务端带回来的:屏幕上没有「补回哪个待办」那个下拉框,人一次都没挑
assert.equal(element('here-actions').hidden, false);
assert.equal(element('act').textContent, '读取这一页的正文');
assert.match(element('here-state').textContent, /正文还缺着/);

// ① 顺利那一次:交给服务端判的是**原始**标签网址(没在本地规范化过),
//    预览上的字数用服务端那一份。
await element('act').click();
assert.deepEqual(prechecks[0], {intent_id: 'i1', page_url: 'https://e.org/文章', method: 'article'});
assert.equal(prechecks[1].body, captured.body);
assert.equal(prechecks.length, 2);
assert.equal(injections, 2);
assert.equal(element('preview').hidden, false);
assert.match(element('count').textContent, /^694 字符/);
assert.doesNotMatch(element('count').textContent, /1200/);

// ② 读之前就被拒:一个字都没读
injections = 0; prechecks = [];
replies = [{status: 409, detail: '条目已被其他操作修改或排除。本次没有覆盖，请重新核对并发起采集'}];
await element('act').click();
assert.equal(injections, 0);
assert.equal(prechecks.length, 1);
assert.equal(element('preview').hidden, true);
assert.match(element('status').textContent, /条目已被其他操作修改或排除/);

// ③ 正文那一问没过:预览不渲染,也没上传
injections = 0;
replies = [{status: 200, data: {ok: true}}, {status: 409, detail: '正文不足 400 个不重复字符，或超过 20 万字符；请检查是否只采到了选段或提示页'}];
await element('act').click();
assert.equal(injections, 2);
assert.equal(element('preview').hidden, true);
assert.match(element('status').textContent, /不重复字符/);
assert.equal(uploads, 0);

// ④ 过期由服务端说,不靠本机时钟
replies = [{status: 404, detail: '采集请求已过期、服务已重启或请求不存在，请重新发起'}];
injections = 0;
await element('act').click();
assert.equal(injections, 0);
assert.match(element('status').textContent, /已过期/);

// ⑤ 结构:扩展里不许再出现第二份判据(网址比对、字数门槛)
assert.doesNotMatch(source, /new URL\([^)]*page_url/);
assert.doesNotMatch(source, /expires_at\s*\*/);
assert.doesNotMatch(extractor, /\b400\b|\b200000\b/);
// 下拉框连同「从清单里挑一个待办」那套一起退场:留着它就留着第二个入口,
// 而它信的是上一次刷新时缓存的那份清单(那正是 precheck 要补的事故形态)。
assert.doesNotMatch(source, /getElementById\('intent'\)|element\('intent'\)/);

console.log('browser capture gate: passed');
