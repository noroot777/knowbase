/* 扩展那颗「关掉这一页」:**许可只属于这一次真做完的那一项操作,凭据在按下去那一刻
 * 再核一遍。**
 *
 * 钉这几件事:只在**这一次真采成了、而且他还站在那一页上**时露面,露面那一次接焦点、
 * 已经露着就不许再抢;按下去只关当前这一个标签。对不上的维度一条都不许漏:
 * (a) 同一个标签换了一页;(b) 地址一样但不是那个标签;(c) 确认那一问失败时许可不许
 * 留在上一页上;(d)(d2) **只读查询不建、也不移交许可** —— 服务端那一答 `saved` 认得
 * 回执,但回执里没有标签身份,拿它授权就是把许可挪给人此刻在读的那个标签;
 * (e)(e2) 凭据绑真写进去的那一页和人眼前那个标签,不绑展示快照;(e3) 反过来钉一边 ——
 * 该给的不许不给;(f) HTTP 200 不代表这一次写了正文(`stored_body` 那一维);
 * (g)(g2)(g3)(g3b)(g3c)(g4)(g5)(g6) 操作身份与当下状态:换了目标当场作废、这一维不依赖
 * 服务端那一答、刷新失败不算换操作(**但按下去那一刻问不着一律拒,还得有界**)、同一
 * 条目上又有一件没采完时出口收回,而**这一否决落在凭据上、持久有效**,下一答说「齐了」
 * 也换不回来,只有新的一次成功才能再授权。
 *
 * 其余各档(不在库里、兜底两颗、候选列表、配对)在 `tests/grab_from_page_gate.mjs`,
 * 两份共用 `tests/popup_host.mjs` 那层桩。
 * 硬约束见 docs/decisions/browser-capture.md 与 link-grab.md。
 */
import assert from 'node:assert/strict';
import {popupHost, hereAnswer, GRABBED, HANG, PAGE} from './popup_host.mjs';

//: 服务端那一答。每个用例改这一份,扩展只负责把它摆成界面。
let here = {ok: true, situation: 'absent', detail: '这一页还不在知识库里。',
            page_url: PAGE, item: null, intent: null, elsewhere: []};
let hereReply = null;      // 非空 = 这一问当场失败
let grabReply = GRABBED;
let fromPageReply = GRABBED;
//: 「改用它」那一下服务端交回的待办 id。**可变**:同一轮里先采成一项、再改选另一项时
//: 它必须换一个 —— 写死一个常量的话「人换了操作」在这个桩里表达不出来。
let adoptId = 'i9';
const tab = {id: 7, url: PAGE};
const OTHER = 'https://example.org/posts/world';

const {element, calls, removed, context, markup} = await popupHost({tab, answer: (path, sent) => {
  if (path === '/api/browser-capture/here') return hereReply || hereAnswer(here, sent);
  if (path === '/api/browser-capture/here/adopt') return {status: 200, data: {ok: true, id: adoptId}};
  if (path === '/api/grab/from-page') return fromPageReply;
  if (path === '/api/grab') return grabReply;
}});

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
// 先在这一页上真采一次 —— 上一段那个凭据已经作废了(服务端在那儿明确答「还不在
// 知识库里」,那一刻就撤掉了,换回地址也复活不了,见 (g5))。
tab.url = OTHER;
here = {ok: true, situation: 'absent', page_url: OTHER, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
await element('refresh').click();
here = {ok: true, situation: 'complete', page_url: OTHER, intent: null, elsewhere: [],
        item: {key: 'k9', kind: 'rss', title: '另一篇', url: OTHER},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('act').click();
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

// (d) **只读查询不建许可。** 服务端那一答 `saved` 认得审计表里那张回执,所以它说得出
//     「这一篇的正文写进去了」—— 说不出「写进去的那一次发生在你眼前这个标签里」(回执
//     里没有 Chrome 标签身份)。原先这一档会就地授权一次,理由是「上传那一答在路上丢
//     了、或者服务重启了,人点一下「重新确认」也该有出口」;那条路撤了,代价写在
//     popup.js 的 `loadHere()` 里:这种时候屏幕上照旧写着「已经写进这一条了」,但不给
//     关页出口。漏一个出口是多按一下,关错一个标签不可撤销。
const SAVED = 'https://example.org/posts/saved-one';
tab.id = 8; tab.url = SAVED;
here = {ok: true, situation: 'saved', page_url: SAVED, intent: null, elsewhere: [],
        item: {key: 'k8', kind: 'rss', title: '补过的那一篇', url: SAVED},
        detail: '这一次采集已经写进这一条了。正文已补入原条目。'};
await element('refresh').click();
assert.match(element('here-state').textContent, /已经写进这一条了/, '那句话照旧说');
assert.equal(element('close-tab').hidden, true, '光读到一张回执不算许可:不许摆出关页出口');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '按下去也一个都不许关');

// (d2) 承重的那一半:**那一下原先把许可挪到了人此刻在读的那个标签上。** 同一篇他开了
//      两个标签(或者采完那一个已经被他关了、眼前这个是后开的):地址逐字相等,只有
//      标签不是那一个。挪过去之后 `granted()` 拿标签 id 去比的是它自己刚写进去的值,
//      等于没比 —— 按下去关掉的是那个压根没参与这一次采集的标签。
//      这一条跟 (b) 不是一回事:(b) 的那一答是 `complete`(按网址答的),走不到原先那
//      条授权路;只有 `saved` 走得到,而 `saved` 的前提是他点过「改用它」(服务端那边
//      `intent_id` 非空才查回执),所以这里照真机把那一步走完。
const SAMEURL = 'https://example.org/posts/two-tabs';
const pendingOn = (id, key) => ({id, key, title: '还在等的那一篇', page_url: SAMEURL});
tab.id = 71; tab.url = SAMEURL;
here = {ok: true, situation: 'undecided', page_url: SAMEURL, item: null, intent: null,
        detail: '下面还在等的条目里点一下要补的那一篇。', elsewhere: [pendingOn('iT', 'kT')]};
await element('refresh').click();
adoptId = 'iT2';
here = {ok: true, situation: 'fill', page_url: SAMEURL, elsewhere: [],
        detail: '知识库里有这一条，但正文还缺着。',
        item: {key: 'kT', kind: 'rss', title: '还在等的那一篇', url: SAMEURL},
        intent: pendingOn('iT2', 'kT')};
await element('elsewhere-list').querySelectorAll('button')[0].click();
//: 采完那一下自己会再问一次 `/here` —— 他点过「改用它」,所以服务端这一答是 `saved`
//: (`intent_id` 非空才去查回执)。
here = {ok: true, situation: 'saved', page_url: SAMEURL, intent: null, elsewhere: [],
        item: {key: 'kT', kind: 'rss', title: '还在等的那一篇', url: SAMEURL},
        detail: '这一次采集已经写进这一条了。正文已补入原条目。'};
await element('act').click();                 // 真采一次:凭据记在 71 号标签上
assert.equal(element('close-tab').hidden, false, '这一次真采成了,出口照旧给');
tab.id = 72;                                  // 同一地址的另一个标签
removed.length = 0;
const closedSoFar = context.window.closed;
await element('close-tab').click();
assert.deepEqual(removed, [], '只读刷新不许把许可挪到眼前这个标签');
assert.equal(context.window.closed, closedSoFar, '什么都没关,弹窗也不该自己收');
assert.match(element('status').textContent, /不是刚才采的那一篇/, '要说清为什么没关');
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
for (const [answer, why, settled] of [
  [SUMMARY_ONLY, '订阅同步先落下一段摘要',
   {situation: 'fill', detail: '知识库里有这一条，但正文还缺着。'}],
  [REFETCH_FAILED, '重抓明确失败',
   {situation: 'complete', detail: '这一条收过了，只是收下的那一份看不出是正文。'}],
]) {
  tab.id = seat; tab.url = `https://example.org/posts/not-written-${seat}`;
  seat += 1;
  here = {ok: true, situation: 'absent', page_url: tab.url, item: null, intent: null,
          detail: '这一页还不在知识库里。', elsewhere: []};
  grabReply = answer;
  await element('refresh').click();
  //: **写完之后服务端那一答按真机摆**:这两档都是 `duplicate`,库里本来就有这一行,
  //: 所以它不会再答「不在库里」。摘要那一档订阅还在等全文 → 「正文还缺着」;重抓
  //: 失败那一档没有订阅等它 → 「收过了,只是收下的那一份看不出是正文」。后者落在
  //: `SETTLED` 白名单里,于是这一条**只靠 `stored_body` 那一维**挡住,前一条两道都挡
  //: —— 桩里偷懒继续答 `absent` 的话,两条都会被白名单顺手挡住,`stored_body` 那一维
  //: 的红再也出不来(元规则「两份实现各有自己的回归」在同一处的两道闸上同样成立)。
  here = {ok: true, page_url: tab.url, intent: null, elsewhere: [], ...settled,
          item: {key: `kf${tab.id}`, kind: 'inbox', title: '库里那一份', url: tab.url}};
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
// 同上:库里已经有别人那一份,所以写完之后服务端答的是「收过了」那一档(白名单里)
//  —— 挡住它的只有 `stored_body` 那一维。
here = {ok: true, situation: 'complete', page_url: tab.url, intent: null, elsewhere: [],
        item: {key: 'kr15', kind: 'inbox', title: '别人那一份', url: tab.url},
        detail: '这一条收过了，只是收下的那一份看不出是正文。'};
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
// 换完要重新走后半条链,所以写完之后服务端答的是「收过了,只是还没导出成笔记文件」
here = {ok: true, situation: 'complete', page_url: tab.url, intent: null, elsewhere: [],
        item: {key: 'kx16', kind: 'inbox', title: '换好了那一份', url: tab.url},
        detail: '这一条收过了，只是还没导出成笔记文件。'};
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

// (g) **一次真的成功,不许给后来另一项还没完成的操作授权。**
//     实测形态:A 采成了,而服务端那边 B 的待办也指着这一页;人点 B 那一行的「这一页
//     就是这一篇,改用它」—— 屏幕上同时写着 B 的「正文还缺着」和一颗「关掉这一页」,
//     一次采集都没再发生,按下去关掉的就是 B **还没采的**那一页(后端独立查:B 仍
//     `pending`、回执为空、正文还是原来那 11 个字)。同一条目上点一次「重新采集正文」
//     再回弹窗「重新确认」是同一个缺口。许可只属于**这一次做完的那一项操作**。
const BOTH = 'https://example.org/posts/two-waiting';
const waiting = (id, key) => ({id, key, title: '还在等的那一篇', page_url: BOTH});
tab.id = 21; tab.url = BOTH;
here = {ok: true, situation: 'absent', page_url: BOTH, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
grabReply = GRABBED;
await element('refresh').click();
here = {ok: true, situation: 'complete', page_url: BOTH, intent: null,
        item: {key: 'kA', kind: 'rss', title: '这一页那一篇', url: BOTH},
        detail: '这一条已经在知识库里，正文也齐了。', elsewhere: [waiting('iB', 'kB')]};
await element('act').click();
assert.equal(element('close-tab').hidden, false, '这一次真采成了,出口照旧给');
// 点那一行的「改用它」:服务端为 B 新建一个指着这一页的待办,并答「B 的正文还缺着」
adoptId = 'iB2';
here = {ok: true, situation: 'fill', page_url: BOTH, detail: '知识库里有这一条，但正文还缺着。',
        item: {key: 'kB', kind: 'rss', title: '还在等的那一篇', url: BOTH},
        intent: waiting('iB2', 'kB'), elsewhere: []};
await element('elsewhere-list').querySelectorAll('button')[0].click();
assert.equal(element('close-tab').hidden, true, '换了操作:上一次那个出口当场作废');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '按下去一个都不许关');
assert.match(element('status').textContent, /还有一件没采完/,
  '两句分得清:这一次不是「换页了」,是「这一页还有事没做完」');

// (g2) **这一维不依赖服务端那一答。** 改选之后 `/here` 正好失败(503、断网、超时)时,
//      只问「服务端此刻说没说还欠着事」的写法什么也判不出来 —— 旧许可留着,而人明明
//      已经换到另一项还没做完的操作上了。他点的那一下是**他自己**的动作,不经过网络。
tab.id = 22; tab.url = BOTH;
here = {ok: true, situation: 'absent', page_url: BOTH, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
await element('refresh').click();
here = {ok: true, situation: 'complete', page_url: BOTH, intent: null,
        item: {key: 'kA', kind: 'rss', title: '这一页那一篇', url: BOTH},
        detail: '这一条已经在知识库里，正文也齐了。', elsewhere: [waiting('iC', 'kC')]};
await element('act').click();
assert.equal(element('close-tab').hidden, false, '先回到露面那一档');
adoptId = 'iC2';
hereReply = {status: 503, detail: '服务暂不可用'};
await element('elsewhere-list').querySelectorAll('button')[0].click();
assert.equal(element('close-tab').hidden, true, '换了操作:哪怕这一问失败也不许把许可留着');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '按下去一个都不许关');
hereReply = null;

// (g3) **「问不着」在展示上和在按下去那一刻不是一回事 —— 两头都钉。**
//      他没换目标、一个候选都没点,只是这一问连不上(503、断网、超时)。
//      - **那颗按钮照旧摆着**:一次有效的成功不许被一次网络抖动丢掉,否则采完之后网络
//        一抖出口就没了,而那一页正是采集抽屉刚为它开出来的。摆着它什么也关不掉。
//      - **按下去一个都不许关**:服务端那道否决(这一页又有一件没采完的事)正是靠这
//        一问送过来的,503 把它一并吞掉 —— 「什么都没变、只是断网」和「已经又发起了
//        一次重采、偏巧也断网」在弹窗这边长得一模一样。分不清就不许关:拒了是人自己
//        关一下那个标签,关错了是那一页再也采不成(那个待办只有人站在那一页上才兑现
//        得了)。**原先这里钉的是反面**(「照旧关得掉」),那是把「没问着」当成了「问
//        着了、答的是齐了」。
const MINE = 'https://example.org/posts/still-mine';
tab.id = 23; tab.url = MINE;
here = {ok: true, situation: 'absent', page_url: MINE, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
await element('refresh').click();
here = {ok: true, situation: 'complete', page_url: MINE, intent: null, elsewhere: [],
        item: {key: 'kM', kind: 'rss', title: '刚收下那一篇', url: MINE},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('act').click();
assert.equal(element('close-tab').hidden, false, '采完了就该给这个出口');
hereReply = {status: 503, detail: '服务暂不可用'};
await element('refresh').click();
assert.equal(element('close-tab').hidden, false, '同一页、同一项操作:这一问失败不许收走出口');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '问不着就不许关 —— 那一道否决正是靠这一问送过来的');
assert.match(element('status').textContent, /没能跟知识库确认/, '三句话分得清:这一句是「问不着」');
assert.equal(element('close-tab').disabled, false, '拒了之后那颗按钮要能再按一次');

// (g3b) 上面那一条**为什么必须是拒**:同一个局面下,服务端那边确实又挂上了一件没做完
//       的事(他在采集抽屉里点过一次「重新采集正文」),只是这一问 503、那句否决送不
//       过来。弹窗这边看到的和 (g3) 一模一样 —— 所以只要分不清就得拒。
//       实测形态(真机、真服务端):采完一篇后再为它发起重采,不点「重新确认」直接按
//       这颗按钮而这一问 503,那一页被关掉了;后端独立查:新请求仍 `pending`、回执为空。
here = {ok: true, situation: 'fill', page_url: MINE, elsewhere: [],
        detail: '知识库里有这一条，但正文还缺着。',
        item: {key: 'kM', kind: 'inbox', title: '刚被要求重采那一篇', url: MINE},
        intent: {id: 'iM2', key: 'kM', title: '刚被要求重采那一篇', page_url: MINE}};
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '服务端此刻说还欠着事,而这一问失败 —— 更不该关');
hereReply = null;
// 这一问通了之后,出口确实该收回:刚才那一拒拒对了。
await element('refresh').click();
assert.equal(element('close-tab').hidden, true, '那一问真通了,答的就是「还欠着事」');

// (g3c) **那一问还得有界。** 服务端接了连接却一直不答(卡在维护锁上、磁盘满)时,
//       `fetch` 自己不会醒,而这一下已经把按钮按住了(`busy`):没有截止时间的话人等到
//       的是一颗永远按不动的按钮,连「再试一次」都点不了。到点按「问不着」处理 ——
//       那是拒绝,不是放行。没给信号的写法在这儿会一直挂着,下面那场赛跑把它变成红
//       (桩把时限压到 150ms,钉的是「有界」不是那个秒数,见 `popup_host.mjs`)。
tab.id = 25; tab.url = MINE;
here = {ok: true, situation: 'absent', page_url: MINE, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
await element('refresh').click();
here = {ok: true, situation: 'complete', page_url: MINE, intent: null, elsewhere: [],
        item: {key: 'kM', kind: 'rss', title: '刚收下那一篇', url: MINE},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('act').click();
assert.equal(element('close-tab').hidden, false, '先回到露面那一档');
hereReply = HANG;
removed.length = 0;
const raced = await Promise.race([
  element('close-tab').click().then(() => 'ended'),
  new Promise(resolve => setTimeout(resolve, 1500, 'hung'))]);
hereReply = null;
assert.equal(raced, 'ended', '服务一直不答时,这一下自己要结束 —— 不许把按钮永远按住');
assert.deepEqual(removed, [], '到点了也算问不着:一个都不许关');
assert.equal(element('close-tab').disabled, false, '结束之后那颗按钮要能再按一次');

// (g4) **同一条目上又有一项没做完的采集 —— 这一维只有服务端答得了。** 人一个候选都
//      没点(身份那一维两头都是空串),他是在采集抽屉里点了一次「重新采集正文」:服务端
//      这一答于是变回「正文还缺着」,并带上那个新待办。实测形态:旧的「关掉这一页」照旧
//      摆着、还接着焦点,而那一页此刻正等着被重新采一遍 —— 一个回车关掉的就是它。
const AGAIN = 'https://example.org/posts/recaptured';
tab.id = 24; tab.url = AGAIN;
here = {ok: true, situation: 'absent', page_url: AGAIN, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
await element('refresh').click();
here = {ok: true, situation: 'complete', page_url: AGAIN, intent: null, elsewhere: [],
        item: {key: 'kR', kind: 'inbox', title: '刚收下那一篇', url: AGAIN},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('act').click();
assert.equal(element('close-tab').hidden, false, '先回到露面那一档');
here = {ok: true, situation: 'fill', page_url: AGAIN, elsewhere: [],
        detail: '知识库里有这一条，但正文还缺着。',
        item: {key: 'kR', kind: 'inbox', title: '刚被要求重采那一篇', url: AGAIN},
        intent: {id: 'iR', key: 'kR', title: '刚被要求重采那一篇', page_url: AGAIN}};
await element('refresh').click();
assert.equal(element('close-tab').hidden, true, '这一页又有一件没采完的事:出口当场收回');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '又有一件没采完:按下去一个都不许关');
assert.match(element('status').textContent, /还有一件没采完/, '要说清为什么没关');

// (g5) **「这一页还有事没做完」读到过一次就作数 —— 不许被下一次查询失败抹掉。**
//      实测形态:上面那一步按钮已经正确收回了,**再点一次「重新确认」而这一问 503**,
//      按钮回来了、还接走了焦点,一个回车真发出了 `tabs.remove`(后端查:那个新请求仍
//      `pending`、回执为空,只有上一次操作那张旧回执)。根因是「不允许」只活在那一答
//      里,而下一刻的「未知」照旧沿用凭据 —— 撤销必须落在凭据上。
hereReply = {status: 503, detail: '服务暂不可用'};
await element('refresh').click();
assert.equal(element('close-tab').hidden, true, '已经否决过了:这一问失败不许让它复活');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '复活之后按下去一个都不许关');
hereReply = null;
// 服务端下一答又说「齐了」也换不回来:**撤销是持久的,只有新的成功才能再授权**。
here = {ok: true, situation: 'complete', page_url: AGAIN, intent: null, elsewhere: [],
        item: {key: 'kR', kind: 'inbox', title: '刚被要求重采那一篇', url: AGAIN},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('refresh').click();
assert.equal(element('close-tab').hidden, true, '撤销之后,光靠服务端说「齐了」也换不回出口');
// 反过来钉一边:**真再采一次就该给** —— 不然「一撤就永久没有」这种写法在这儿全绿。
here = {ok: true, situation: 'absent', page_url: AGAIN, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
await element('refresh').click();
here = {ok: true, situation: 'complete', page_url: AGAIN, intent: null, elsewhere: [],
        item: {key: 'kR', kind: 'inbox', title: '刚被要求重采那一篇', url: AGAIN},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('act').click();
assert.equal(element('close-tab').hidden, false, '真再采一次,出口照旧给');
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [24], '按下去关的就是它');

// (g6) **按下去那一刻也要现问一次 —— 他可能压根没点过「重新确认」。** 实测形态:采完
//      之后他去采集抽屉里点了一次「重新采集正文」,再回弹窗直接按这颗还摆着、还接着
//      焦点的按钮。屏幕上那一份答复停在采成那一刻,只在「重新确认」那一下更新的话,
//      这一下关掉的就是一页**正等着被重采的原文**。服务端那一侧另有自己的一道(旧回执
//      不为新欠账背书,`browser_capture.here` 那一答改成 `undecided`,回归在
//      `tests/test_browser_capture_here.py`);这里这一道管的是「他压根没再问过」那一段
//      窗口 —— 元规则「两份实现各有自己的回归」。
const AGAIN_WAITING = 'https://example.org/posts/asked-again';
tab.id = 25; tab.url = AGAIN_WAITING;
here = {ok: true, situation: 'absent', page_url: AGAIN_WAITING, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
await element('refresh').click();
here = {ok: true, situation: 'complete', page_url: AGAIN_WAITING, intent: null, elsewhere: [],
        item: {key: 'kG', kind: 'inbox', title: '刚收下那一篇', url: AGAIN_WAITING},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('act').click();
assert.equal(element('close-tab').hidden, false, '先回到露面那一档');
// 他在采集抽屉里点了「重新采集正文」—— 弹窗一个字都不知道,**一次「重新确认」都没点**。
// 真实服务端这一答就是这一档(旧回执照旧写在那句话里,那个新待办只作为候选摆着)。
const waitingAgain = {id: 'iG2', key: 'kG', title: '刚收下那一篇', page_url: AGAIN_WAITING};
here = {ok: true, situation: 'undecided', page_url: AGAIN_WAITING, intent: null,
        detail: '这一次采集已经写进这一条了，但这一篇又有一次采集在等着。'
                + '下面点一下它再采一遍，或者回采集抽屉里取消那一次。',
        item: {key: 'kG', kind: 'inbox', title: '刚收下那一篇', url: AGAIN_WAITING},
        elsewhere: [waitingAgain]};
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [], '一次「重新确认」都没点:按下去那一刻现问一次,一个都不许关');
assert.match(element('status').textContent, /还有一件没采完/,
  '两句分得清:不是「换页了」,是「这一页还有事没做完」');
assert.equal(element('close-tab').hidden, true, '顺手那次重画把这颗按钮收回去');
// 反过来钉一边:**多出来的那一问不许挡住正常那一下**。真再采一次,按下去照旧关得掉。
here = {ok: true, situation: 'absent', page_url: AGAIN_WAITING, item: null, intent: null,
        detail: '这一页还不在知识库里。', elsewhere: []};
await element('refresh').click();
here = {ok: true, situation: 'complete', page_url: AGAIN_WAITING, intent: null, elsewhere: [],
        item: {key: 'kG', kind: 'inbox', title: '刚收下那一篇', url: AGAIN_WAITING},
        detail: '这一条已经在知识库里，正文也齐了。'};
await element('act').click();
removed.length = 0;
await element('close-tab').click();
assert.deepEqual(removed, [25], '真再采一次,按下去关的就是它');

tab.id = 7; tab.url = PAGE;

console.log('popup close tab gate: passed');
