// 「☆ 收藏此段」在问答里:**这段话出自哪一条消息**。
//
// 和 `snip_probe_edges.mjs` 分开是为了 700 行上限,**执行顺序一点没动** ——
// 它紧跟在那一份后面跑,共用同一个沙箱服务和同一个库。两份的形态也确实不同:
// 那一份问「收下来的收藏后来还作不作数」,这一份问「它说的是哪一条消息」:
//
//   1 一条消息里住着相对时间、模型名、「存为笔记」按钮、按 refs 现拼的出处块 ——
//     它们不是回答正文,可收范围得收到**落了库的那一份**上。
//   2 同一个对话里两条回答一字不差:靠文字谁也分不出是哪一条,分得出它们的只有
//     消息编号(`favorites.origin_msg`)。采集和回跳都得收到那一条消息上。
//   3 记着的那条消息不在这个对话里了:**不许退回整个对话去搜** —— 那正是"把另一
//     条同句冒认成原处"的入口。
//   4 原对话整个被删掉了:说清楚,不要停在「加载对话…」上。
//
// 判据一律落在后果上:**高亮落在第几处**(两条回答的文字一模一样)、**库里那条记
// 的是哪个编号**、**屏幕上那句提示说的是哪一种**。
//
// 外壳(起浏览器、连 CDP、页内帮手)共用 ./snip_probe_lib.mjs。
//
//   HEADLESS_SHELL=<bin> node tests/snip_probe_chat.mjs <baseURL> <manifest.json>
import {
  appToastClear, boot, check, clickJump, evalJs, finish, fx, js, openFavList,
  sleep, snip, until,
} from "./snip_probe_lib.mjs";

await boot();

// ── 10. 问答:已保存的标记挂在整条消息上,界面文字可不是落了库的正文 ─────────
// 同一个 `.msg` 里还住着相对时间、模型名、「存为笔记」那颗按钮,以及按 refs 现拼
// 的「来源 N 篇 / Obsidian / 原文」那一块 —— 它们不是回答正文(相对时间过一会儿
// 就变成「3 小时前」,照着它找只会找不到)。可收范围得收到落了库的那一份正文上。
await evalJs(`switchTab('ask'); openConv(${fx.convId}, true); true`);
await until(`$('chat-msgs').textContent.includes(${js(fx.snipChat)})
  && $('chat-msgs').querySelectorAll('.msg-meta').length >= 1`, 30000);
// 选的是 `.msg-meta` 里的**模型名**那几个字(它旁边就是相对时间)。
// **不选「存为笔记」那颗按钮上的字**:按钮里的文字浏览器压根选不出来(选区当场
// 折叠),那个落点分不出"判据拦住了"和"选都没选上" —— 实测定点去掉下面那道
// 判据,拿按钮当落点红 0 条,拿模型名当落点红 1 条。
const metaSel = await evalJs(`window.__snipSelect('#chat-msgs', 'stub/none')`);
await sleep(400);
check("模型名和相对时间那行不算可收正文", metaSel === "ok"
  && !(await evalJs(`window.__snipBtnShown()`)), `picked=${metaSel}`);
const refsSel = await evalJs(`(() => {
  const d = document.querySelector('#chat-msgs .refs');
  if (!d) return 'no-refs';
  d.open = true;
  return window.__snipSelect('#chat-msgs', ${js(fx.refTitle)});
})()`);
await sleep(400);
check("按 refs 现拼的引用块也不算可收正文", refsSel === "ok"
  && !(await evalJs(`window.__snipBtnShown()`)), `picked=${refsSel}`);
// 反过来钉一边:**同一条消息的正文照旧收得下**(不然"一律不收"也能过上面两条)
const bodySnip = await snip("#chat-msgs", fx.snipChat);
check("同一条消息的正文照旧收得下", bodySnip.shown, `picked=${bodySnip.picked}`);
// **那几块界面文字也不许进前后文锚点。** 可收范围(上面两条)和采锚点的范围是两
// 件事:这一句后面只隔着十个字就是它那条消息的 `.msg-meta`(模型名 + 相对时间 +
// 「存为笔记」),三十字的后文窗口正好探进去 —— 而**相对时间会自己变**(过一会儿
// 「3 小时前」就成了「5 小时前」),照着它找的那条收藏从此对不上。
const chatRows = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipChat);
check("问答那条收藏的前后文里没有模型名、相对时间和「存为笔记」",
  chatRows.length >= 1 && chatRows.every(f =>
    !/存为笔记|stub\/none|分钟前|小时前|来源 \d+ 篇/.test((f.prefix || "") + (f.suffix || ""))),
  JSON.stringify(chatRows.map(f => ({ p: f.prefix, s: f.suffix }))));

// 收藏列表里那条片段的标题取的是**这一轮的提问**,裁到 60 字 —— 这一问的第 60 个
// 字是个 emoji,裁口正好落在它中间。切出来的半个字后端存不下,整条 POST 被拒,
// 而用户选的是回答正文里一句普通的话。
const chat3 = await snip("#chat-msgs", fx.snipChat3);
check("提问第六十个字是 emoji 的那一轮也收得下", chat3.shown, `picked=${chat3.picked}`);
const chat3Fav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipChat3);
check("它的标题裁得干净,没把 emoji 切成半个",
  chat3Fav.length === 1 && !/[\uD800-\uDFFF]/.test(chat3Fav[0].title || ""),
  JSON.stringify(chat3Fav.map(f => ({ t: (f.title || "").slice(-4) }))));

// ── 10b. 同一个对话里两条回答一字不差:分得出它们的只有消息编号 ──────────────
// 这个对话最后两轮是同一个问题问两遍、回答一字不差(`serve_snippets.py` 的
// `CHAT_QUESTION_6`)。**靠文字谁也分不出是哪一条** —— 库里那条收藏记着后端核验
// 过的消息编号(`favorites.origin_msg`),采集和回跳都该收到那一条消息上:
//   - 采集:前后文只在那一条气泡里采(`chatSnipSource` 的 `root`)。摊整个消息区
//     的话,引文顶在气泡开头时它的前文来自**上一条消息**。
//   - 回跳:只在那一条消息里找(`chat.js` 的 `chatSnipUnit`)。在整个消息区里搜
//     的话,这两处都对得上 → 按「位置说不清」一个高亮也不给,而要找的那一条明明
//     记着(审查复现的那一条:屏幕上一句「没找到这段收藏」,库里那条好好记着编号)。
// 判据落在**高亮落在第几处**,不是"文字对不对"(两处的文字一模一样)。
await evalJs(`switchTab('ask'); openConv(${fx.convId}, true); true`);
await until(`$('chat-msgs').textContent.split(${js(fx.snipChatTwice)}).length === 3`,
  30000);
const twiceMids = await evalJs(`JSON.stringify(
  [...$('chat-msgs').querySelectorAll('.msg')]
    .filter(m => (m.querySelector('.bubble') || m).textContent
      .includes(${js(fx.snipChatTwice)}))
    .map(m => Number(m.getAttribute('data-snip-mid')) || 0))`);
check("那两条一字不差的回答各有自己的消息编号",
  JSON.parse(twiceMids).length === 2 && JSON.parse(twiceMids).every(n => n > 0)
  && JSON.parse(twiceMids)[0] !== JSON.parse(twiceMids)[1], twiceMids);
const secondMid = JSON.parse(twiceMids)[1];
// 收**第二条**那一句
const twiceTake = await snip("#chat-msgs", fx.snipChatTwice, 2);
check("第二条回答里那一句收得下", twiceTake.shown, `picked=${twiceTake.picked}`);
const twiceRows = (await evalJs(`window.__favs()`))
  .filter(f => f.quote === fx.snipChatTwice);
check("库里那条记的是第二条那个消息编号",
  twiceRows.length === 1 && Number(twiceRows[0].origin_msg) === secondMid,
  JSON.stringify(twiceRows.map(f => ({ m: f.origin_msg, want: secondMid }))));
// 前后文只许来自那一条气泡:提问那句话、上一条回答的字一个都不许进来
check("它的前后文只从那一条消息里采(没有混进提问和别的消息)",
  twiceRows.length === 1
  && !((twiceRows[0].prefix || "") + (twiceRows[0].suffix || ""))
    .includes(fx.chatQuestion6),
  JSON.stringify(twiceRows.map(f => ({ p: f.prefix, s: f.suffix }))));
// 回跳:落点必须是第二处。**期望值在点之前量**:`openConv` 一上来就把消息区换成
// 「加载对话…」,点完立刻量的话这一句压根还不在屏幕上(量出 -1,而高亮也还没落下来
// 也是 -1 —— 两个 -1 相等,这条判据就会在什么都没发生的情况下变绿)。
const wantTwice = await evalJs(
  `window.__textOffset('#chat-msgs', ${js(fx.snipChatTwice)}, 2)`);
check("量到了第二处那一句在消息区里的偏移", wantTwice > 0, String(wantTwice));
await openFavList();
check("收藏面板里点得到这条回跳", await clickJump("chat", fx.snipChatTwice));
let gotTwice = -1;
for (let i = 0; i < 120 && gotTwice !== wantTwice; i++) {
  gotTwice = await evalJs(`window.__hiOffset('#chat-msgs')`);
  if (gotTwice !== wantTwice) await sleep(150);
}
check("点回去落在记着的那一条消息上(不是另一条一字不差的)",
  gotTwice === wantTwice && wantTwice >= 0,
  `高亮偏移 ${gotTwice},期望 ${wantTwice}(第二处)`);

// ── 10c. 记着的那条消息不在这个对话里了:一个高亮都不给,不退回整个对话去搜 ────
// 这是「文字对得上不等于位置就是它」的最后一格:收藏记着消息编号,可那一条消息在
// 这个对话里找不着了(半截落库、历史被改过)。退回"在整个对话里搜"的话,屏幕上会
// 亮起**另一条一字不差的回答** —— 那不是用户收的那一段,高亮本身在撒谎,比一句
// 「没找到」坏得多。没有沙箱接口能单独删掉一条消息,所以这儿照 `fav.js` 的 chat
// 分支那一句把 `chatPendingSnip` 摆好(编号给一个库里不存在的),再走同一个
// `openConv` —— 判的是 `chat.js` 里那一格,不是绕过它。
await appToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); true`);
// 锚点**故意按「整个对话」摆成有唯一解**:后文从第一处那一句往后取,一直伸进下一
// 轮提问里,所以在整个对话的坐标系里它只对得上**第一处** —— 而这条收藏收的是第二
// 处(上面那一节)。于是"退回整对话搜"的后果看得见:亮在第一处,一个用户没收过的
// 位置。(拿这条收藏自己的前后文反而看不出来:两条回答一字不差,它那一份在整个
// 对话里两处都对得上,按"位置说不清"也是一个高亮都不给 —— 退路和正路一样绿。)
// **这段后文要按定位那一头的坐标系取**:`consumeSnipJump` 把 `CHAT_SNIP_SKIP`
// 一路传进 `locateSnip`,界面文字(出处、时间戳、生成态)在那一头是跳过的。
// 不带 skip 取出来的 60 个字里混着 `.msg-meta`,到那一头一个字也对不上 ——
// 退路于是"因为锚点自己不成立"而失败,这条判据就钉不到任何东西(实测红 0)。
const q2len = fx.snipChatTwice.length;
const convTail = JSON.parse(await evalJs(`(() => {
  const idx = snipTextIndex($('chat-msgs'), CHAT_SNIP_SKIP);
  const at = idx.text.indexOf(${js(fx.snipChatTwice)});
  return JSON.stringify(idx.text.slice(at + ${q2len}, at + ${q2len} + 60));
})()`));
const tailNth = await evalJs(`(() => {
  const idx = snipTextIndex($('chat-msgs'), CHAT_SNIP_SKIP);
  return idx.text.split(${js(fx.snipChatTwice)} + ${js(convTail)}).length; })()`);
check("这段后文按整个对话算只认第一处(退路真有唯一解可采信)", tailNth === 2,
  `${tailNth - 1} 处 / ${convTail.slice(0, 20)}`);
await evalJs(`chatPendingSnip = { ref: ${fx.convId}, msg: 99999999, spec: {
    q: ${js(fx.snipChatTwice)}, p: '', s: ${js(convTail)}, u: 0 } };
  openConv(${fx.convId}, true); true`);
const lostToast = await until(`window.__toastText().includes('没找到这段收藏')`, 20000);
check("记着的消息不在了:当场说一句「没找到这段收藏」", lostToast,
  await evalJs(`window.__toastText()`));
check("并且说清收藏里那段原文还在",
  (await evalJs(`window.__toastText()`)).includes("原文还在"),
  await evalJs(`window.__toastText()`));
await sleep(600);
const lostHi = await evalJs(`window.__hiOffset('#chat-msgs')`);
const firstTwiceAt = await evalJs(
  `window.__textOffset('#chat-msgs', ${js(fx.snipChatTwice)}, 1)`);
check("一个高亮都没给(退回整个对话去搜的话,亮的是第一处 —— 收的是第二处)",
  lostHi === -1, `高亮偏移 ${lostHi},第一处在 ${firstTwiceAt}`);
check("这次待定位丢掉了(不会挂到下一个对话上)",
  await evalJs(`chatPendingSnip === null`),
  await evalJs(`JSON.stringify(chatPendingSnip)`));

// ── 原对话已经删掉了:说清楚,不要停在「加载对话…」上 ────────────────────────
// 这个接口对"会话不在了"回的是 **404 带一份非空 JSON**,所以 `r.json()` 正常解析
// 得出 `{detail: …}` —— 只判 `!d` 的那一版接着就去 `d.messages.map`,当场
// TypeError:屏幕永远停在加载态,而这次待定位还挂着(审查实测,报告里的 N1)。
// **排在最后**:这一节真把那个对话删掉,上面几节都要用它。
const delStatus = await evalJs(
  `fetch('/api/conversations/' + ${fx.convId}, { method: 'DELETE' }).then(r => r.status)`);
check("沙箱真把那个对话删掉了", delStatus === 200 || delStatus === 204, String(delStatus));
await openFavList();
const clickedDead = await clickJump("chat", fx.snipChat);
check("收藏面板里照旧点得到那条问答回跳", clickedDead);
const goneShown = await until(
  `activeTab === 'ask' && !$('chat-msgs').textContent.includes('加载对话')`, 15000);
check("问答面板没有停在「加载对话…」上", goneShown,
  await evalJs(`activeTab + ' / ' + $('chat-msgs').textContent.slice(0, 40)`));
check("屏幕上说清了那个对话已经不在了",
  await evalJs(`$('chat-msgs').textContent.includes('不在了')`),
  await evalJs(`$('chat-msgs').textContent.slice(0, 60)`));
const goneToast = await evalJs(`window.__toastText()`);
check("提示里也说清了,并且说明收藏里那段原文还在",
  goneToast.includes("删掉") && goneToast.includes("原文还在"), goneToast);
check("这次待定位被丢掉了(不会挂到下一个对话上)",
  await evalJs(`chatPendingSnip === null`),
  await evalJs(`JSON.stringify(chatPendingSnip)`));

finish();
