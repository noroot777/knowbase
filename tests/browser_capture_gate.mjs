/* 扩展侧的准入口径:**它一处都不许自己判**。
 *
 * 钉这几件事:①读页之前先问服务端,不通过就一个字都不读(`injections` 不动);
 * ②正文那一问没过就不渲染预览;③预览上的字数是服务端报的那一份,不是扩展
 * 自己数的原始长度;④**待办是服务端那一答带回来的,不是人从下拉框里挑的** ——
 * 「这一页是哪一条」地址栏里就写着,让人再答一遍要跨三个界面走七步;⑤补完正文才给
 * 「关掉这一页」。
 * 事故形态与硬约束见 docs/decisions/browser-capture.md。
 */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {popupDom} from './popup_dom.mjs';

const {element, document} = popupDom();

const INTENT = {id: 'i1', key: 'k1', title: '文章', page_url: 'https://e.org/%E6%96%87%E7%AB%A0',
  created_at: 1.79e9, expires_at: 1.79e9 + 900, status: 'pending'};
//: 服务端对「我站着这一页」那一答。`fill` = 库里有这一条、正文还缺着,待办
//: 由它自己建好带回来(`intent`)。
const HERE = {ok: true, situation: 'fill', detail: '知识库里有这一条，但正文还缺着。',
  page_url: INTENT.page_url, item: {key: 'k1', kind: 'rss', title: '文章', url: 'https://e.org/文章'},
  intent: INTENT, elsewhere: []};
//: 正文真写进去之后服务端那一答:**`complete`,不再是 `fill`** —— `/upload` 落完
//: `fulltext_at` 就不欠全文了,下一问走的是「收过了,只是还没导出成笔记文件」那一支
//: (`saved` 要人点过候选、带着 `intent_id` 才可能,最常走的这条路上它是空的)。
//: 桩里**让 `/here` 跟着写入变**,而不是永远答 `fill`:关页许可的一半依据正是「服务端
//: 此刻没说这一页还欠着事」,桩写死 `fill` 就把这一半恒定成假,红永远出不来。
const DONE = {ok: true, situation: 'complete', detail: '这一条收过了，只是还没导出成笔记文件。',
  page_url: INTENT.page_url, item: HERE.item, intent: null, elsewhere: []};
let hereReply = HERE;
let prechecks = [];
let replies = [];
let injections = 0;
let uploads = 0;
const uploaded = [];
//: 非空 = 提取那一刻页面已经同源换到这个地址。`currentTab()` 读到一个、注入脚本
//: 执行时页面已经在另一个,这段窗口是真实的(站点采失败后自动翻篇最常见)。
let switchOnExtract = '';
const removed = [];
const tab = {id: 7, url: 'https://e.org/文章'};
const captured = {page_url: 'https://e.org/文章', title: '文章', body: 'x'.repeat(1200), method: 'article'};

const context = vm.createContext({
  URL, Date, JSON,
  document,
  window: {closed: 0, close() { this.closed += 1; }},
  chrome: {
    //: **每次交回一份新快照,不是同一个对象** —— 真实 `chrome.tabs.query` 就是这样。
    //: 共用一个对象的话,`here`(上一次渲染时问到的那一份)和此刻这一问**永远相等**,
    //: 于是「标签在这中间跳走了」在这个桩里根本表达不出来:改 `tab.url` 会把历史那
    //: 一份也一起改掉,按下去那一刻的重新核验恒真。
    tabs: {query: async () => [{...tab}], remove: async id => { removed.push(id); }},
    //: 地址报的是**页面自己此刻在哪**(真实 `extract.js` 交回 `location.href`),
    //: 桩里写死一个常量就把「真写进去的是哪一页」和「屏幕上摆的是哪一页」黏在一起了。
    scripting: {executeScript: async ({files}) => {
      injections += 1;
      if (files) return [{}];
      if (switchOnExtract) tab.url = switchOnExtract;
      return [{result: {...captured, page_url: tab.url}}];
    }},
    storage: {local: {get: async () => ({server: 'http://127.0.0.1:8787', key: 'k'.repeat(44)}), set: async () => {}}}
  },
  apiResponse: async reply => {
    if (reply.status >= 400) { const error = new Error(reply.detail); error.status = reply.status; throw error; }
    return reply.data;
  },
  fetch: async (url, options = {}) => {
    const sent = options.body ? JSON.parse(options.body) : null;
    if (url.endsWith('/here')) return {status: 200, data: hereReply};
    if (url.endsWith('/pending')) return {status: 200, data: {intents: [INTENT]}};
    if (url.endsWith('/precheck')) { prechecks.push(sent); return replies.shift() ?? {status: 200, data: {ok: true, characters: 694}}; }
    if (url.endsWith('/upload')) { uploads += 1; uploaded.push(sent); hereReply = DONE; return {status: 200, data: {detail: '已补入'}}; }
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
assert.equal(element('act').textContent, '采集');
assert.match(element('here-state').textContent, /正文还缺着/);
assert.equal(element('close-tab').hidden, true, '正文还缺着,这一页还有用');

// ① 顺利那一次:交给服务端判的是**原始**标签网址(没在本地规范化过),
//    两问都过就**直接写进去**(预览区连同那道「我已核对」撤了),而写完那句话里
//    的字数用服务端那一份 —— 扩展自己数的是 1200,服务端按行去重之后是 694。
await element('act').click();
assert.deepEqual(prechecks[0], {intent_id: 'i1', page_url: 'https://e.org/文章', method: 'article'});
assert.equal(prechecks[1].body, captured.body);
assert.equal(prechecks.length, 2);
assert.equal(injections, 2);
assert.equal(uploads, 1, '两问都过就该直接提交,不再等第二道人工确认');
assert.match(element('status').textContent, /694 字符/);
assert.doesNotMatch(element('status').textContent, /1200/);
// ⑤ 补完正文那一刻给出「关掉这一页」—— 这一页多半就是采集抽屉刚为它开出来的那个
//    标签。服务端这一答是 `complete`(真写进去之后它就不答 `fill` 了),所以这一条
//    同时钉两件事:**这个弹窗刚刚采成的那一次**算依据(只认 `saved` 的写法在这条
//    最常走的路上一颗都不给,而这正是图一里的那条),以及**`complete` 也算「这一页
//    此刻没有待做的采集」** —— 白名单少写一档,这里当场红。
assert.equal(element('close-tab').hidden, false, '补完了就该给这个出口');
assert.equal(element('close-tab').focused, true, '「默认选中」= 这一刻回车走的就是它');
await element('close-tab').click();
assert.deepEqual(removed, [tab.id], '只关当前这一个标签,一个都不许多关');
assert.equal(context.window.closed, 1, '标签没了,弹窗不许留在屏幕上');

// ⑤b **凭据跟「真上传的那个地址」走,不跟「操作开头读到的那个」走。** 两者中间
//     有一段真实的窗口:`currentTab()` 读到 A,注入脚本执行时页面已经同源换到 B,
//     而 `/upload` 交的是 `captured.page_url`(= B,服务端按它认页)。凭据记成 A 的
//     话,**A 那一篇的正文根本没补上,人回到 A 却拿到一颗接着焦点的关页按钮**;
//     真补上的 B 反倒没有出口。这一条和「改用这一页的正文」那一路是同一类。
const MOVED = 'https://e.org/moved-on';
hereReply = HERE;                   // 另一条还缺正文的条目:下面要再采一次
tab.id = 11; tab.url = 'https://e.org/文章';
await element('refresh').click();
assert.equal(element('close-tab').hidden, true, '换了标签,先回到不给出口那一档');
switchOnExtract = MOVED;
uploads = 0; uploaded.length = 0; prechecks = []; injections = 0;
await element('act').click();
switchOnExtract = '';
assert.equal(uploads, 1, '照旧写进去了');
assert.equal(uploaded[0].page_url, MOVED, '服务端收的是提取时页面自己报的那个地址');
assert.equal(element('close-tab').hidden, false, '真补上的那一页(此刻就是它)该有出口');
tab.url = 'https://e.org/文章';
await element('refresh').click();
assert.equal(element('close-tab').hidden, true, '操作开头那一页并没有被补上,不许摆出口');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '按下去也一个都不许关');
tab.id = 7;

// ② 读之前就被拒:一个字都没读,也没写
hereReply = HERE;                   // 又是一条还缺正文的条目:重新问一次才摆得出主动作
await element('refresh').click();
injections = 0; prechecks = []; uploads = 0;
replies = [{status: 409, detail: '条目已被其他操作修改或排除。本次没有覆盖，请重新核对并发起采集'}];
await element('act').click();
assert.equal(injections, 0);
assert.equal(prechecks.length, 1);
assert.equal(uploads, 0);
assert.match(element('status').textContent, /条目已被其他操作修改或排除/);

// ③ 正文那一问没过:读了,但**一个字都没写进去**。
//    这一条是撤掉人工核对之后最吃紧的一道 —— 原先还有人在预览上把关,现在只剩它。
injections = 0; uploads = 0;
replies = [{status: 200, data: {ok: true}}, {status: 409, detail: '正文不足 400 个不重复字符，或超过 20 万字符；请检查是否只采到了选段或提示页'}];
await element('act').click();
assert.equal(injections, 2);
assert.equal(uploads, 0);
assert.match(element('status').textContent, /不重复字符/);

// ④ 过期由服务端说,不靠本机时钟
replies = [{status: 404, detail: '采集请求已过期、服务已重启或请求不存在，请重新发起'}];
injections = 0; uploads = 0;
await element('act').click();
assert.equal(injections, 0);
assert.equal(uploads, 0);
assert.match(element('status').textContent, /已过期/);

// ⑥ 结构:扩展里不许再出现第二份判据(网址比对、字数门槛)
assert.doesNotMatch(source, /new URL\([^)]*page_url/);
assert.doesNotMatch(source, /expires_at\s*\*/);
assert.doesNotMatch(extractor, /\b400\b|\b200000\b/);
// 下拉框连同「从清单里挑一个待办」那套一起退场:留着它就留着第二个入口,
// 而它信的是上一次刷新时缓存的那份清单(那正是 precheck 要补的事故形态)。
assert.doesNotMatch(source, /getElementById\('intent'\)|element\('intent'\)/);
// 预览区和那道「我已核对」撤了:留着它们就等于留着一条「读完先不写」的分支,
// 而服务端那边 `confirmed_complete` 已经没有了。
assert.doesNotMatch(source, /element\('preview'\)|element\('complete'\)|confirmed_complete/);

console.log('browser capture gate: passed');
