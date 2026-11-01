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
 * 有效**,服务端说不出是哪一篇时一颗采集按钮都不给;⑨配对密钥用一次不依赖当前标签的调用验;
 * ⑩「关掉这一页」只在**这一次真采成了、而且他还站在那一页上**时露面,露面那一次接焦点,
 * 按下去只关当前这一个标签;⑪结构上不许按失败文案分流,预览那一支也不许回来。
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
//: 真实 `_store` 那一支的回包形状(`kb/web/grab_api.py`)。**`stored_body` 非带不可**
//: —— 它是「这一次把正文写进去了」那个事实(服务端一处算,两个写入入口共用),而那一
//: 档的**每条出口 HTTP 都是 200**:撞上 `DO NOTHING`、库里只有一段订阅同步先落下的
//: 摘要、重抓失败、已经走完整条链的幂等答复。桩里不带它,「200 却没写」和「真写了」
//: 在这儿就长得一模一样,(f) 那几条红永远出不来。
const GRABBED = {status: 200, data: {ok: true, added: 1, duplicate: false, done: false,
                                     content_ok: true, stored_body: true,
                                     detail: '已收下，正在写入知识库'}};
let grabReply = GRABBED;
let fromPageReply = GRABBED;
const calls = [];
const extracted = [];
const removed = [];
const tab = {id: 7, url: PAGE};
const OTHER = 'https://example.org/posts/world';
const captured = {page_url: PAGE, title: '页面标题', body: 'x'.repeat(1500), method: 'article'};

const context = vm.createContext({
  URL, Date, JSON, encodeURIComponent,
  document,
  //: 弹窗关掉自己那一下。记次数而不是空函数:标签关了、弹窗还指着一个不存在的
  //: 页面留在屏幕上,是这颗按钮唯一会留下的后遗症。
  window: {closed: 0, close() { this.closed += 1; }},
  chrome: {
    //: **每次交回一份新快照,不是同一个对象** —— 真实 `chrome.tabs.query` 就是这样。
    //: 共用一个对象的话,`here`(上一次渲染时问到的那一份)和此刻这一问**永远相等**,
    //: 于是「标签在这中间跳走了」在这个桩里根本表达不出来:改 `tab.url` 会把历史那
    //: 一份也一起改掉,按下去那一刻的重新核验恒真。
    tabs: {query: async () => [{...tab}], remove: async id => { removed.push(id); }},
    scripting: {executeScript: async ({target, files, args}) => {
      if (files) return [{}];
      extracted.push(args[0]);
      //: **按 `tabId` 交回那个标签的东西。** 提取目标传错时(拿上一次 `/here` 留下
      //: 的快照里那个 id 去读),读到的是**另一个标签**的正文、会被收成这一篇;桩不
      //: 分标签的话这种错一个字都看不出来。
      if (target.tabId !== tab.id) {
        return [{result: {page_url: 'https://example.org/posts/other-tab',
                          title: '另一个标签里那一篇', body: 'y'.repeat(1500), method: args[0]}}];
      }
      //: **地址报的是「页面自己此刻在哪」**(真实 `extract.js` 交回的是
      //: `location.href`),不是摆局时写死的那一个 —— 站点同源换篇之后,提取和收录
      //: 走的就是换到的那一页,而展示快照还停在原来那一页。桩里写死一个常量的话,
      //: 「真写进去的是哪一页」和「屏幕上摆的是哪一页」永远分不开。
      return [{result: {...captured, page_url: tab.url, method: args[0]}}];
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
    if (path === '/api/grab/from-page') return fromPageReply;
    if (path === '/api/grab') return grabReply;
    throw new Error('unexpected request ' + path);
  }
});

const directory = new URL('../kb/static/browser-extension/knowbase-capture/', import.meta.url);
const source = await readFile(new URL('popup.js', directory), 'utf8');
const markup = await readFile(new URL('popup.html', directory), 'utf8');
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
assert.equal(element('close-tab').hidden, true, '什么都还没采,不许摆关页按钮');

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
// 写成功之后那一问的答复:**做完了**。要在点「采集」之前换上 —— `read()` 用的是上一次
// 渲染里那个待办,只有写完之后那一问才会读到这一份。
here = {ok: true, situation: 'saved', page_url: PAGE, intent: null,
        detail: '这一次采集已经写进这一条了。正文已补入原条目；翻译、导出与索引仍待执行，不代表已经完成入库。',
        item: {key: 'k8', kind: 'rss', title: '另一篇', url: PAGE},
        elsewhere: [{id: 'i1', key: 'k7', title: '那一篇', page_url: PAGE}]};
await element('act').click();
assert.equal(calls.find(c => c.path === '/api/browser-capture/upload').sent.intent_id, 'i9',
  '写进去用的是他点中那个待办');
for (const one of calls.filter(c => c.path === '/api/browser-capture/precheck'))
  assert.equal(one.sent.intent_id, 'i9', '两次预检也都用它');
// **写成功之后不许把身份放掉。** 放掉之后下一问只按网址和「还剩哪几个待办」重判,同一
// 颗「采集」就被另一篇接管:实测保存完他选的那一篇、不做任何重新选择再按一下,正文就
// 写进了另一篇(那一篇 content_rev 0→1、正文是这一页的),而屏幕上只是标题换了一行。
assert.equal(calls.filter(c => c.path === '/api/browser-capture/here').at(-1).sent.intent_id,
  'i9', '写完之后那一问照旧带着他选的那个身份');
assert.equal(element('here-actions').hidden, true, '做完了这一档不许摆采集按钮');
assert.match(element('here-state').textContent, /已经写进这一条/, '原样显示服务端那句话');
calls.length = 0;
await element('act').click();
await element('act-page').click();
await element('act-selection').click();
assert.deepEqual(calls, [], '做完了之后再按一下,一个请求都不许发');
assert.equal(element('elsewhere-list').children.length, 1,
  '另一篇只作为候选摆着,不许接管那颗按钮');

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

// ⑩ 「关掉这一页」:**只在这一次真采成了之后露面**,而且只认他此刻站着的那一页。
//    判据不是「服务端说这一条齐了」—— 那一句在他只是路过一篇早就收过的文章时也成立,
//    而那时摆出一颗默认接焦点的关页按钮,一个回车就把他正在读的东西关掉了。
tab.url = OTHER;
here = {ok: true, situation: 'complete', page_url: OTHER, intent: null, elsewhere: [],
        item: {key: 'k9', kind: 'rss', title: '早就收过的那一篇', url: OTHER},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('refresh').click();
assert.equal(element('close-tab').hidden, true,
  '只是路过一篇早就齐了的文章,不许给关页按钮');

// 在这一页上真采一次(服务端自己抓那条路)。三条路都算采完,这一条最容易被漏掉:
// 没点过「改用它」时 `intent_id` 是空的,服务端跳过回执查找、按网址答回的是
// `complete` 而不是 `saved` —— 只认 `saved` 的写法在最常走的那条路上一颗都不给。
here = {ok: true, situation: 'absent', page_url: OTHER, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
grabReply = GRABBED;
await element('refresh').click();
here = {...here, situation: 'complete', detail: '这一条已经在知识库里，正文也齐了。'};
await element('act').click();
assert.equal(element('close-tab').hidden, false, '采完了就该给这个出口');
assert.equal(element('close-tab').focused, true, '「默认选中」= 这一刻回车走的就是它');
// 名字和初次打开时的 hidden 都写死在标记里(这一份假 DOM 不解析 HTML,所以钉在
// 那一份上):露出来那一刻只改 `hidden`,没有谁再去补名字。
assert.match(markup, /<button id="close-tab" hidden>关掉这一页<\/button>/,
  '名字写在 popup.html 里、初次打开藏着');

// 已经露着的时候**不许再抢焦点**:这一块每问一次就整个重画一次,每次都抢的话,
// 人刚把焦点挪到「重新确认」上就被拽回来了。
element('close-tab').focused = false;
await element('refresh').click();
assert.equal(element('close-tab').hidden, false, '照旧露着');
assert.equal(element('close-tab').focused, false, '已经露着就不许再抢一次焦点');

// 采完之后标签自己跳走了(站内重定向、站点自动翻下一篇、他自己点开了另一条):
// **收回去**。关的是「刚刚采的那一页」,不是「碰巧现在开着的那一页」—— 关错一个
// 标签是人正在读的东西没了,不可撤销。
tab.url = PAGE;
here = {...here, page_url: PAGE};
await element('refresh').click();
assert.equal(element('close-tab').hidden, true, '采的那一页已经不在眼前,就不许再摆');

// **跳走之后不刷新、直接按下去**:这一条才是承重的。上面那条先点了「重新确认」才
// 断言,而真实形态里没人会先点它 —— 按钮露不露面是**上一次渲染**时算的,标签在这
// 中间跳走了,屏幕上那颗照旧在,按下去关掉的是他正读着的新页面(实测:关闭接口收
// 到的是新那一页的标签)。所以许可要在**按下去那一刻**再核一遍。
tab.id = 7; tab.url = OTHER;
here = {ok: true, situation: 'complete', page_url: OTHER, intent: null, elsewhere: [],
        item: {key: 'k9', kind: 'rss', title: '另一篇', url: OTHER},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('refresh').click();
assert.equal(element('close-tab').hidden, false, '先回到露面那一档');
// 「对不上」有**两个**维度,各钉一条 —— 只比一边的写法在另一边上照旧关错标签。
// (a) 同一个标签换了一页:站内 SPA 路由、站点采完自动翻下一篇。标签 id 一个字都
//     没变,单比 id 的写法拦不住。
tab.url = 'https://example.org/posts/next-one';
here = {ok: true, situation: 'absent', page_url: tab.url, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
removed.length = 0;
const closedBefore = context.window.closed;
await element('close-tab').click();
assert.deepEqual(removed, [], '同一个标签换了一页,按下去一个都不许关');
assert.equal(context.window.closed, closedBefore, '更不许把弹窗一起收掉');
assert.match(element('status').textContent, /不是刚才采的那一篇/,
  '要说清为什么没关 —— 不然看着就是这颗按钮坏了');
assert.equal(element('close-tab').hidden, true, '顺手按新这一页重画,这颗按钮自己收回去');

// (b) 地址一样、标签不是那一个:同一篇他开了两个标签(或者采完那一个已经被他关了,
//     眼前这个是后开的)。地址逐字相等,单比地址的写法会去关另一个标签。
tab.url = OTHER;
here = {ok: true, situation: 'complete', page_url: OTHER, intent: null, elsewhere: [],
        item: {key: 'k9', kind: 'rss', title: '另一篇', url: OTHER},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('refresh').click();
assert.equal(element('close-tab').hidden, false, '回到露面那一档');
tab.id = 7002;
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '地址一样但不是那个标签,照旧不许关');
assert.equal(context.window.closed, closedBefore, '也不许收掉弹窗');
assert.match(element('status').textContent, /不是刚才采的那一篇/, '同样要说清为什么没关');
// 顺手那次重画也要认出「不是那个标签」—— 记的是哪个标签的哪一页,不是光一个地址。
assert.equal(element('close-tab').hidden, true, '地址一样也不摆:采的那一页不在这个标签里');
tab.id = 7;

// (c) **确认那一问失败时,许可不许留在上一页上。** 这一条是上面两条的承重墙:
//     刷新这件事是「清掉上一答 → 把身份换成当前标签 → 问服务端」,中间那一步换完了
//     身份,而那一问完全可能当场失败(503、断网、超时、401)。拿「最近一次查到的
//     当前标签」当参照的写法在这里会**两两相等地放行** —— 实测形态:第一下说「没有
//     关掉它」,第二下就把那个还没采的新页面关掉了(第一下那句话顺手又刷了一次,
//     把参照也换成了新页)。凭据只在真采成那一刻写下来,所以它不跟着跑。
tab.id = 7; tab.url = OTHER;
here = {ok: true, situation: 'complete', page_url: OTHER, intent: null, elsewhere: [],
        item: {key: 'k9', kind: 'rss', title: '另一篇', url: OTHER},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('refresh').click();
assert.equal(element('close-tab').hidden, false, '回到露面那一档');
// 站点自己翻到了下一篇,而这一问从这里开始失败
tab.id = 7001; tab.url = 'https://example.org/posts/next-one';
hereReply = {status: 503, detail: '服务暂不可用'};
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '确认失败时第一下不许关');
assert.equal(element('close-tab').hidden, true, '第一下之后这颗按钮就该收回去');
await element('close-tab').click();
assert.deepEqual(removed, [], '第二下照旧不许关 —— 参照不许跟着那次失败的刷新换掉');
assert.equal(context.window.closed, closedBefore, '两下都不许收掉弹窗');

// 同一个洞的另一个入口:他自己先点「重新确认」,那一问失败
tab.id = 7; tab.url = OTHER;
hereReply = null;
await element('refresh').click();
assert.equal(element('close-tab').hidden, false, '先回到露面那一档');
tab.id = 7001; tab.url = 'https://example.org/posts/next-one';
hereReply = {status: 503, detail: '服务暂不可用'};
await element('refresh').click();
assert.equal(element('close-tab').hidden, true, '身份已经换了,这颗按钮不许留在上一页的许可上');
assert.match(element('status').textContent, /服务暂不可用/, '那一问为什么失败照旧要说');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '手动刷新失败之后按下去,照旧一个都不许关');
assert.equal(context.window.closed, closedBefore, '也不许收掉弹窗');
hereReply = null;
tab.id = 7;

// 按下去:只关**当前这一个**标签,顺手把弹窗也收掉
tab.url = OTHER;
here = {...here, page_url: OTHER};
await element('refresh').click();
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [tab.id], '只关当前这一个标签,一个都不许多关');
assert.equal(context.window.closed, 1, '标签没了,弹窗不许留在屏幕上');

// (d) **服务端那一答 `saved` 是凭据的第二个来源**,它也得落下来、也得带标签身份。
//     走到这一档的是:这个弹窗刚上传完、那一答在路上丢了(或服务重启了),人点一下
//     「重新确认」—— 服务端读审计表认得那张回执。只在当下看一眼 `situation` 的写法
//     在这里会摆出**一颗按不动的按钮**:动作边界只认凭据,而凭据那时还是空的。
const SAVED = 'https://example.org/posts/saved-one';
tab.id = 8; tab.url = SAVED;
here = {ok: true, situation: 'saved', page_url: SAVED, intent: null, elsewhere: [],
        item: {key: 'k8', kind: 'rss', title: '补过的那一篇', url: SAVED},
        detail: '这一次采集已经写进这一条了。正文已补入原条目。'};
await element('refresh').click();
assert.equal(element('close-tab').hidden, false, '服务端说这一次已经写进去了,照样给出口');
removed.length = 0;
const closedSoFar = context.window.closed;
await element('close-tab').click();
assert.deepEqual(removed, [8], '按下去真的关得掉 —— 不许摆一颗按不动的按钮');
assert.equal(context.window.closed, closedSoFar + 1, '标签没了,弹窗照旧跟着收');
// 反过来钉一边:这一档也一样跟标签身份绑着,跳走之后不许关。
tab.id = 8001; tab.url = 'https://example.org/posts/after-saved';
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], 'saved 那一档也一样:跳走之后按下去一个都不许关');
tab.id = 7;
tab.url = PAGE;

// (e) **凭据绑的是真写进去的那一页,不是屏幕上摆着的那一页。** 站点会在弹窗开着的
//     时候同源换篇(采失败之后自动翻下一篇最常见):提取、预检、收录走的全是换到的
//     那一页(`extract.js` 报的是页面自己的地址),而展示快照还停在原来那一页。拿
//     快照当凭据的后果是**两头都错**:真采成的那一页没有出口,而那篇还没采的原页
//     在人点一次「重新确认」之后摆出一颗接着焦点的关页按钮 —— 一个回车就关掉它。
const FIRST = 'https://example.org/posts/first';
const SWITCHED = 'https://example.org/posts/switched';
tab.id = 9; tab.url = FIRST;
here = {ok: true, situation: 'absent', page_url: FIRST, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
grabReply = {status: 503, detail: '抓不到这一页'};
await element('refresh').click();
assert.equal(element('close-tab').hidden, true, '还没采,不给出口');
await element('act').click();
assert.equal(element('act-page').hidden, false, '服务端抓不到,才轮到兜底那两颗');
// 站点自己翻到了下一篇(同一个标签、同源),弹窗一个字都不知道
tab.url = SWITCHED;
here = {ok: true, situation: 'complete', page_url: SWITCHED, intent: null, elsewhere: [],
        item: {key: 'k7', kind: 'rss', title: '换到的那一篇', url: SWITCHED},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('act-page').click();
const fromPage = calls.filter(one => one.path === '/api/grab/from-page');
assert.equal(fromPage[fromPage.length - 1].sent.url, SWITCHED,
  '收录的是换到的那一页 —— 提取在页面里现报的那个地址');
assert.equal(element('close-tab').hidden, false, '真采成的那一页才该有出口');
assert.equal(element('close-tab').focused, true, '它也该接走焦点');
// 回到那篇**还没采的**原页:服务端照旧说不在库里,这颗按钮一个也不许给
tab.url = FIRST;
here = {ok: true, situation: 'absent', page_url: FIRST, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
await element('refresh').click();
assert.equal(element('close-tab').hidden, true, '没采的那一页不许摆出口');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '按下去也一个都不许关 —— 这一页还没采');

// (e2) 站点在**另一个标签**里打开下一篇(连 id 都换了):提取目标也必须现读。拿
//      展示快照里那个 id 去读的话,读到的是另一个标签的正文,而它会被收成这一篇。
tab.id = 9; tab.url = FIRST;
here = {ok: true, situation: 'absent', page_url: FIRST, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
grabReply = {status: 503, detail: '抓不到这一页'};
await element('refresh').click();
await element('act').click();
assert.equal(element('act-page').hidden, false, '照旧先轮到兜底那两颗');
tab.id = 9001; tab.url = SWITCHED;
here = {ok: true, situation: 'complete', page_url: SWITCHED, intent: null, elsewhere: [],
        item: {key: 'k7', kind: 'rss', title: '换到的那一篇', url: SWITCHED},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('act-page').click();
const other = calls.filter(one => one.path === '/api/grab/from-page');
assert.equal(other[other.length - 1].sent.url, SWITCHED,
  '读的是人眼前这个标签 —— 拿上一次问到的那个 id 去读,交上去的会是另一个标签那一篇');
assert.equal(element('close-tab').hidden, false, '真采成的那一页该有出口');

tab.id = 7; tab.url = PAGE;
grabReply = GRABBED;

// (f) **HTTP 200 不代表这一次写了正文。** 这一档的每条出口都是 200,而只有「重抓真
//     换掉了」那一条动过正文。实测形态:打开扩展时这条还没收录,后台同步先收下它的
//     一段摘要,再点「采集」—— 服务端回 200 / `added:0` / `duplicate:true` /
//     `content_ok:true`,一个字的正文都没采到,随后那一问照旧说「正文还缺着」,而
//     「关掉这一页」露着面、接走了焦点,一个回车关掉的就是那篇**还没采的**原文。
//     `content_ok` 顶不住这一档:它是**库里那一份**的质量,RSS 摘要也能为真。
const SUMMARY_ONLY = {status: 200, data: {ok: true, added: 0, duplicate: true, done: false,
  content_ok: true, stored_body: false, title: '只收到一段摘要',
  detail: '这条之前被「RSS 订阅」收过了，还没写进知识库'}};
const REFETCH_FAILED = {status: 200, data: {ok: true, added: 0, duplicate: true, done: false,
  content_ok: false, replaced: false, stored_body: false,
  detail: '这条之前被「收件桶」收过了，库里那一份看不出是正文；这次重抓还是不行'}};
let seat = 13;
for (const [answer, why] of [[SUMMARY_ONLY, '订阅同步先落下一段摘要'],
                             [REFETCH_FAILED, '重抓明确失败']]) {
  tab.id = seat; tab.url = `https://example.org/posts/not-written-${seat}`;
  seat += 1;
  here = {ok: true, situation: 'absent', page_url: tab.url, item: null, intent: null,
          detail: '这一页还不在知识库里。', elsewhere: []};
  grabReply = answer;
  await element('refresh').click();
  await element('act').click();
  assert.equal(element('close-tab').hidden, true, `${why}:200 也不许给关页按钮`);
  removed.length = 0;
  await element('close-tab').click();
  assert.deepEqual(removed, [], `${why}:按下去一个都不许关`);
}

// 「改用这一页的正文」那一侧是同一份契约:预检放行之后、真正写入之前别人先落了一份
// 坏正文,服务端转去恢复、没有替换 —— 提取的那份正文压根没写进去。
tab.id = 15; tab.url = 'https://example.org/posts/raced';
here = {ok: true, situation: 'absent', page_url: tab.url, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
grabReply = {status: 503, detail: '抓不到这一页'};
await element('refresh').click();
await element('act').click();
assert.equal(element('act-page').hidden, false, '先轮到兜底那两颗');
fromPageReply = REFETCH_FAILED;
await element('act-page').click();
assert.equal(element('close-tab').hidden, true, '写入时撞上别人那一份:不许给关页按钮');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '按下去一个都不许关');
fromPageReply = GRABBED;

// 反过来钉一边:**重抓真把正文换掉了**也算采成 —— 那一档 `added` 恒 0,靠 `added`
// 推的写法在这里一颗都不给。
const REFETCHED = {status: 200, data: {ok: true, added: 0, duplicate: true, done: false,
  content_ok: true, replaced: true, stored_body: true,
  detail: '库里那一份看不出是正文，已经按这次重抓到的内容换掉'}};
tab.id = 16; tab.url = 'https://example.org/posts/refetched';
here = {ok: true, situation: 'absent', page_url: tab.url, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
grabReply = REFETCHED;
await element('refresh').click();
await element('act').click();
assert.equal(element('close-tab').hidden, false,
  '重抓真换掉了就该给出口 —— 这一档 added 恒 0');
assert.equal(element('close-tab').focused, true, '也该接走焦点');
grabReply = GRABBED;
tab.id = 7; tab.url = PAGE;

// (e3) **反过来钉一边:凭据的标签身份取「人眼前这一个」,不是上一次问到的那一个。**
//      问过 `/here` 之后他切到了另一个开着同一篇的标签(同一地址、不同 id),在那儿
//      点「采集」—— 收进去的是同一页,出口理应给他;取上一次问到的那个 id 的话,采完
//      了反倒一颗都没有。这一头和上面几条反着:那几条防「关错一个」,这一条防「该给
//      的不给」。
const TWICE = 'https://example.org/posts/opened-twice';
tab.id = 12; tab.url = TWICE;
here = {ok: true, situation: 'absent', page_url: TWICE, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
await element('refresh').click();
tab.id = 1200;
here = {ok: true, situation: 'complete', page_url: TWICE, intent: null, elsewhere: [],
        item: {key: 'k6', kind: 'rss', title: '开了两个标签那一篇', url: TWICE},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('act').click();
assert.equal(element('close-tab').hidden, false, '在哪个标签上采的,出口就给那一个');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [1200], '关的是他眼前这一个标签');
tab.id = 7; tab.url = PAGE;

// ⑪ 结构:不许按失败文案分流。**钉形态,不钉关键词** —— 按词表匹配会连注释
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
