/* 扩展那一块「这一页」:**判据一处都不在扩展里,按钮按服务端那一答摆。**
 *
 * 钉这几件事:①这一页是什么情形由服务端 `/here` 一处答,扩展照着摆按钮、
 * 那句话原样显示(不自己另编);②不在库里时先让服务端自己去抓(只交地址、
 * 不交正文);③抓不到才轮到那两颗兜底,而且**不按失败原因分流** —— 无论登录墙、
 * 关卡还是脚本渲染,人的下一步都一样;④读页之前先问一次,**两问都过就直接收录**
 * (预览区连同那道「我已核对」撤了),标题和字数都用服务端那一份;⑤「改用所选
 * 文字」真的把 `selection` 交给 `extract.js`,也照样直接收录;⑥收下之后刷新失败
 * 不许报成提交失败;⑦正文已齐的那一页一颗按钮都不给;⑧**有待办指着别的地址时有几条
 * 就摆几条**(站点按浏览器语言跳转那一类,只说一句「不在库里」会让人收录出第二行同一
 * 篇文章),逐行那颗「改用它」交的是这一页的地址和**那一行**的条目,**而他点中的那个
 * 身份要带进后面每一问**(丢了就会被按创建时间代选成另一篇)、而且**只对他点的那一页
 * 有效**,服务端说不出是哪一篇时一颗采集按钮都不给;⑨配对密钥用一次不依赖当前标签的调用验;⑩结构上不许按失败文案
 * 分流,预览那一支也不许回来。
 * 硬约束见 docs/decisions/link-grab.md 与 browser-capture.md。
 */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {popupDom} from './popup_dom.mjs';

const PAGE = 'https://example.org/posts/hello';
const {element, document} = popupDom();

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
  document,
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
    if (path === '/api/browser-capture/precheck') return {status: 200, data: {ok: true, characters: 1200}};
    if (path === '/api/browser-capture/upload') return {status: 200, data: {ok: true, detail: '正文已补入原条目'}};
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

// ① 不在库里:主按钮写「采集」,兜底两颗还藏着,那句话是服务端那一份
assert.ok(calls.some(c => c.path === '/api/browser-capture/here' && c.method === 'POST'),
  '该一问一答地问服务端这一页是什么情形');
// 还没点过任何候选,所以身份那一栏是空的 —— 下面 ⑧ 钉的是点过之后它必须跟着走。
assert.deepEqual(calls.find(c => c.path === '/api/browser-capture/here').sent,
  {url: PAGE, intent_id: ''});
assert.equal(element('here-actions').hidden, false);
assert.equal(element('act').textContent, '采集');
assert.equal(element('here-state').textContent, here.detail, '那句话原样显示,不自己另编');
assert.equal(element('act-page').hidden, true, '没失败之前不该提前给兜底那一颗');
assert.equal(element('act-selection').hidden, true);
// 藏着的时候名字就已经写好了:放出来那一刻只改 `hidden`,没人再补名字。
assert.equal(element('act-selection').textContent, '改用所选文字');

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
  // **露出来的按钮必须有名字。** 「一开始不显示」和「叫什么」合成一个字段
  //(`selection: ''`)的话,失败那条路上只把 `hidden` 改掉 —— 屏幕上是一颗
  // 26×36 的空白小方块,而 `extract.js` 回的那句「请选好全文再读一次」正指着它。
  assert.equal(element('act-page').textContent, '改用这一页的正文',
    `失败原因「${detail}」露出来的兜底按钮不许没有名字`);
  assert.equal(element('act-selection').textContent, '改用所选文字',
    `失败原因「${detail}」露出来的选段按钮不许没有名字`);
  assert.match(element('status').textContent, new RegExp(detail.slice(0, 6)));
}

// ④ 兜底那条路:读页之前先问服务端,**两问都过就直接收录**
calls.length = 0;
extracted.length = 0;
await element('act-page').click();
const checked = calls.find(c => c.path === '/api/grab/from-page/precheck');
assert.ok(checked, '读完要先问服务端这份正文收不收得了');
assert.deepEqual(checked.sent, {url: PAGE, title: '页面标题', body: captured.body});
assert.deepEqual(extracted, ['article'], '这一颗读的是自动识别的文章区域');
// 预览区连同那道「我已核对」撤了:他就站在那一页上,把同一份正文塞回 450px 的
// 弹窗请他重读一遍,拦不住任何服务端拦不住的东西。
const sent = calls.find(c => c.path === '/api/grab/from-page' && c.method === 'POST');
assert.ok(sent, '两问都过就该直接收录,不再等第二道人工确认');
assert.deepEqual(sent.sent, {url: PAGE, title: '页面标题', body: captured.body});
assert.ok(!calls.some(c => c.path.endsWith('/upload')), '收录不该走补正文那条路');
// 写完摆出来的标题和字数都用**服务端那一份**(扩展自己数的是 1500)
assert.match(element('status').textContent, /^服务端定的标题：/, '标题要用服务端那一份');
assert.match(element('status').textContent, /880 字符/, '字数要用服务端那一份');
assert.doesNotMatch(element('status').textContent, /1500/);

// ⑤ 「改用所选文字」:同一套预检、同样直接收录,**而且真的交 `selection`**
calls.length = 0;
extracted.length = 0;
await element('refresh').click();
await element('act').click();            // 再撞一次失败,把兜底两颗露出来
await element('act-selection').click();
assert.deepEqual(extracted, ['selection'], '这一颗该把 selection 交给 extract.js');
assert.ok(calls.some(c => c.path === '/api/grab/from-page/precheck'),
  '选出来的文字也要先过服务端那一问');
assert.ok(calls.some(c => c.path === '/api/grab/from-page' && c.method === 'POST'),
  '选出来的文字该收录成新条目');
assert.ok(!calls.some(c => c.path.endsWith('/upload')), '新收录不许借补正文那条路');

// ⑥ 收下之后那一问失败:**不许报成提交失败**。已经收下了,说成失败他会再收一次,
//    库里多出一行同一篇文章。
calls.length = 0;
await element('refresh').click();
await element('act').click();
hereReply = {status: 500, detail: '刷新状态失败'};
await element('act-page').click();
assert.ok(calls.some(c => c.path === '/api/grab/from-page' && c.method === 'POST'));
assert.match(element('status').textContent, /已收下/, '收下了就该说收下了');
assert.doesNotMatch(element('status').textContent, /刷新状态失败/);
hereReply = null;

// ⑦ 有待办指着别的地址:**有几条就摆几条**,每行自带一颗「改用它」,交的是**这一页**
//    的地址加**那一行**的条目。站点按浏览器语言跳转那一类里,只说一句「不在库里」就
//    请人收录,库里立刻多出一行同一篇文章;而两个条目的待办同时指着别处时(人把最终
//    网址粘错了),他正站着的这一页偏偏可能是**第二条** —— 只画第一条等于一颗按钮都
//    对不上,只能回采集抽屉里一条条改地址,而扩展这一侧存在的理由恰恰是不必回去。
here = {ok: true, situation: 'absent', detail: '这一页还不在知识库里。', page_url: PAGE,
        item: null, intent: null,
        elsewhere: [{id: 'i1', key: 'k7', title: '那一篇', page_url: 'https://example.org/en/posts/hello'},
                    {id: 'i2', key: 'k8', title: '另一篇', page_url: 'https://example.org/en/posts/world'}]};
calls.length = 0;
await element('refresh').click();
assert.equal(element('elsewhere').hidden, false, '那些待办要摆在人眼前');
const rows = element('elsewhere-list').children;
assert.deepEqual(rows.map(row => row.children[0].textContent), ['那一篇', '另一篇'],
  '有几条就摆几条,顺序照服务端那一答');
assert.deepEqual(rows.map(row => row.children[1].textContent),
  ['https://example.org/en/posts/hello', 'https://example.org/en/posts/world']);
const adoptions = element('elsewhere-list').querySelectorAll('button');
assert.deepEqual(adoptions.map(one => one.textContent),
  ['这一页就是这一篇，改用它', '这一页就是这一篇，改用它'], '每行那颗都得有名字');
// 点**第二行**那一颗:只认 `elsewhere[0]` 的写法在这里会交成 k7。
await adoptions[1].click();
const adopted = calls.find(c => c.path === '/api/browser-capture/here/adopt');
assert.ok(adopted, '「改用它」该走 adopt');
assert.deepEqual(adopted.sent, {key: 'k8', page_url: PAGE},
  '交的是点中那一行的条目,和这一页的地址');

// 点完那一下之后的**每一问**都要带着他选中的那个身份。丢掉它的后果实测过:服务端
// 只收到一个网址,于是在同页那几个待办里按创建时间代选了最早那个 —— 「采集」把这一
// 页的正文写进了另一篇(前者 content_rev 0→1、正文是后者的,后者仍然缺正文)。
assert.equal(calls.filter(c => c.path === '/api/browser-capture/here').at(-1).sent.intent_id,
  'i9', 'adopt 交回的身份要带进下一问');
calls.length = 0;
await element('refresh').click();
assert.equal(calls.at(-1).sent.intent_id, 'i9', '「重新确认」那一下也不许把他的选择抹掉');

// 接着真走一遍采集:**写进去用的必须是他点中的那个待办**。这是客户端这一侧的端到端
// 判据 —— 上面那两条只证明身份发出去了,这一条才证明它一直走到了写入那一步。
here = {ok: true, situation: 'fill', detail: '知识库里有这一条，但正文还缺着。', page_url: PAGE,
        item: {key: 'k8', kind: 'rss', title: '另一篇', url: PAGE},
        intent: {id: 'i9', key: 'k8', title: '另一篇', page_url: PAGE}, elsewhere: []};
calls.length = 0;
await element('refresh').click();
await element('act').click();
assert.equal(calls.find(c => c.path === '/api/browser-capture/upload').sent.intent_id, 'i9',
  '写进去用的是他点中那个待办');
for (const one of calls.filter(c => c.path === '/api/browser-capture/precheck'))
  assert.equal(one.sent.intent_id, 'i9', '两次预检也都用它');
// 用掉之后要把身份放掉:再指名一个已经用掉的待办会被答成「选不出来」,而人此刻只想
// 看见刚写进去那张回执。
assert.equal(calls.filter(c => c.path === '/api/browser-capture/here').at(-1).sent.intent_id, '',
  '用掉之后不该再指名它');

// 身份只对**他点的那一页**有效。再点一次候选,然后让标签自己跳走(站内重定向、推送式
// 改地址):还带着它去问的话,服务端对「带了身份却兑现不了」一律答「你选的那个失效
// 了」—— 其实什么都没失效,他只是站到了另一页上,而那时摆出来的候选属于上一页,点下去
// 是把这一页认给了另一篇。
here = {ok: true, situation: 'absent', detail: '这一页还不在知识库里。', page_url: PAGE,
        item: null, intent: null,
        elsewhere: [{id: 'i1', key: 'k7', title: '那一篇', page_url: 'https://example.org/en/x'}]};
await element('refresh').click();
await element('elsewhere-list').querySelectorAll('button')[0].click();
calls.length = 0;
await element('refresh').click();
assert.equal(calls.at(-1).sent.intent_id, 'i9', '还站在原地,照旧带着它');
tab.url = PAGE + '?utm=1';
calls.length = 0;
await element('refresh').click();
assert.equal(calls.at(-1).sent.intent_id, '', '标签自己跳走了就当他没点过');
tab.url = PAGE;

// 服务端说不出这一页是哪一篇(同页两个条目都在等、他还没点):**一颗采集按钮都不给**。
// 给了就是让他按一下赌一把,而赌错是正文被另一篇覆盖、不可恢复;出路全在候选列表里。
here = {ok: true, situation: 'undecided', page_url: PAGE, item: null, intent: null,
        detail: '有 2 个条目都在等这一页的正文，点一下你要补的那一篇。',
        elsewhere: [{id: 'i1', key: 'k7', title: '那一篇', page_url: PAGE},
                    {id: 'i2', key: 'k8', title: '另一篇', page_url: PAGE}]};
await element('refresh').click();
assert.equal(element('here-actions').hidden, true, '这一档不许摆采集按钮');
assert.match(element('here-state').textContent, /点一下你要补的那一篇/, '原样显示服务端那句话');
assert.equal(element('elsewhere-list').children.length, 2, '候选是唯一的出路,必须摆出来');
// 藏起来不等于按不到(程序化点击、辅助技术)。这一档按下去**一个请求都不许发** ——
// 落进 `absent` 那一支的话,它会拿 `/api/grab` 把同一篇收成第二行。
calls.length = 0;
await element('act').click();
await element('act-page').click();
await element('act-selection').click();
assert.deepEqual(calls, [], '这一档的按钮按下去不许做任何事');

// ⑧ 反过来钉一边:没有这种待办时那一块不露面
here = {...here, elsewhere: []};
await element('refresh').click();
assert.equal(element('elsewhere').hidden, true);

// ⑨ 配对密钥用一次**不依赖当前标签**的调用验:人在扩展管理页或知识库页面上
//    配对时 `currentTab()` 先拒,拿 `/here` 验等于一个字都没验、还被当成连上了
calls.length = 0;
element('server').value = 'http://127.0.0.1:8787';
element('key').value = 'k'.repeat(44);
pendingReply = {status: 401, detail: '配对密钥不正确'};
await element('connect').click();
assert.ok(calls.some(c => c.path.endsWith('/pending')), '该先验一次密钥');
assert.match(element('status').textContent, /配对密钥不正确/);
pendingReply = null;

// ⑩ 结构:不许按失败文案分流。**钉形态,不钉关键词** —— 按词表匹配会连注释
//    一起误伤,而且换个说法就绕过去了;真正会出事的形态只有一种:把服务端那句
//    话拆开来自己判。
assert.doesNotMatch(source, /(message|detail)\s*\.\s*(includes|match|indexOf|startsWith|search)/,
  '按失败文案分流 = 把服务端的判据抄进扩展');
assert.doesNotMatch(source, /body_confirmed/);
// 预览区和那道「我已核对」不许回来:留着它们等于留着一条「读完先不写」的分支,
// 而服务端那边 `confirmed_complete` 这个字段已经没有了(它是扩展签给自己的证词)。
assert.doesNotMatch(source, /element\('(preview|complete|save|count|page-title)'\)|confirmed_complete/,
  '人工核对那一支撤了,不许从扩展这一侧偷偷留回来');

console.log('grab from page gate: passed');
