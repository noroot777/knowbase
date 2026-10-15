// 「☆ 收藏此段」的几类反例:**收下来的那条收藏,后来是不是还作数**。
//
// 四处正文「收得下、点得回去」在 tests/snip_probe.mjs。这一份专管另外几件事,
// 它们的共同形态是**当场看着成功、后来才发现不作数**:
//
//   1 收藏多行代码,库里却只剩接成一行的那一份(换行和缩进再也找不回来)。
//   2 一字不差重复两次的整段:整段锚点也命中两次,静默挑第一处 = 一个落在错
//     地方的高亮,而用户从页面上看不出来(两处的文字一模一样)。
//   3 订阅那篇 md 还是上一版(译文刚写回、还没重导出):绑了文档,点回去是旧
//     正文、0 个高亮。
//   4 生成中 / 点了停止 / 出错的问答气泡:会话号是真的,这一轮却没落库,刷新
//     之后那段话压根不存在。
//   5 内容自带的长标题、长链接:上限两头对不上,整条 POST 被拒成 422,而屏幕
//     上只有一句「收藏失败」。
//   6 正文里有 emoji:前后文窗口按 UTF-16 单元截,正好切在它中间 —— 切出来的
//     半个字后端存不下,整条收藏变成 500,而选中的那几个字里一个 emoji 都没有。
//   7 两边的位置证据互相矛盾(前文那一档指第一处、后文那一档指第二处):采信
//     档位顺序 = 又一个落在错地方的高亮。
//   8 三档前后文都分不出两处:存得下、点回去只能是一句「没找到」,收藏那一刻
//     不说清楚的话,用户要到点回去那一刻才发现。
//
// 判据一律落在后果上:**库里存的是什么字**、**高亮落在第几处**(不是"文字对不
// 对" —— 重复整段那一条,两处的文字都对)、**从库里重新读出来还在不在**。
//
// 外壳(起浏览器、连 CDP、页内帮手)和主探针共用 ./snip_probe_lib.mjs。
//
//   HEADLESS_SHELL=<bin> node tests/snip_probe_edges.mjs <baseURL> <manifest.json>
import {
  base, boot, check, clickJump, DOC_HELPERS, docSnip, docToast, docToastClear,
  evalJs, finish, fx, highlightText, js, openFavList, send, sleep, snip,
  snipHighlight, until,
} from "./snip_probe_lib.mjs";

const flat = s => String(s).replace(/\s+/g, " ");

async function waitDocNth(quote, want, ms = 20000) {
  const dl = Date.now() + ms;
  let got = await evalJs(`window.__docHiNth(${js(quote)})`);
  while (got !== want && Date.now() < dl) {
    await sleep(150);
    got = await evalJs(`window.__docHiNth(${js(quote)})`);
  }
  return got;
}

/** 等高亮落到 want 这个偏移上(回最后读到的那个,交给调用方断言)。
 *  **偏移而不是文字**:一字不差重复两次的那一段,比文字两处都为真。 */
async function waitOffset(sel, want, ms = 20000) {
  const dl = Date.now() + ms;
  let got = await evalJs(`window.__hiOffset(${js(sel)})`);
  while (got !== want && Date.now() < dl) {
    await sleep(150);
    got = await evalJs(`window.__hiOffset(${js(sel)})`);
  }
  return got;
}

await boot();

// ── 1. 多行代码:存进去的还得是那四行 ────────────────────────────────────────
// 压平空白是**匹配表示**(两头的正文空白本来就不一样,不压前后文锚点一碰到段落
// 边界就对不上),可它不是收藏原文。审查实测:选中四行 Python,库里那条 quote 是
// `if ready: run_task() else: stop_task()` —— 粘出来不是能跑的代码了。
await evalJs(`switchTab('daily'); true`);
const dailyReady = await until(
  `dailyCurrent === ${js(fx.dailyDate)}
   && $('daily-reader-body').textContent.includes(${js(fx.snipDaily)})`, 40000);
check("日报面板铺出那一份正文", dailyReady,
  await evalJs(`dailyCurrent + ' | ' + $('daily-reader-body').textContent.slice(0, 60)`));

// 先钉另一边:**字数判据按压平过的那一份算,而且弹按钮和存库是同一个口径**。
// 「冒号 + 四个空格 + 一个字母」原样数有七个字符、压平之后只剩三个 —— 这种选区
// 一个字也不该收,更不该弹出一颗点下去什么也不发生的按钮(引文按原文存之后,
// 原样数和压平数才第一次分叉;两处判据不一样的话就正好落在这中间)。
const THIN = ":\n    r";
const thinPicked = await evalJs(
  `window.__snipSelect('#daily-reader-body', ${js(THIN)})`);
await sleep(400);
check("压平之后不满 4 字的选区不弹按钮", thinPicked === "ok"
  && !(await evalJs(`window.__snipBtnShown()`)), `picked=${thinPicked}`);

const code = await snip("#daily-reader-body", fx.snipCode);
check("四行代码选中后浮出收藏按钮", code.shown, `picked=${code.picked}`);
const codeFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipCode);
check("存下来的还是那四行代码,换行和缩进都在", codeFav.length === 1,
  JSON.stringify((await evalJs(`window.__favs()`))
    .filter(f => flat(f.quote) === flat(fx.snipCode)).map(f => f.quote)));
// 反过来钉一边:**前后文仍然是压平过的匹配表示**(它们只用来定位)。两边都按
// 原文存的话,段落边界那一个换行在两头不一样,锚点又对不上了。
check("前后文照旧是压平过的那一份", codeFav.length === 1
  && !/[\n\t]/.test(codeFav[0].prefix + codeFav[0].suffix),
  JSON.stringify(codeFav.map(f => ({ p: f.prefix, s: f.suffix }))));

// 原文存了,**定位照旧要落得回去**(匹配那一头自己会压一遍)
check("收藏面板列出了回跳按钮", await openFavList());
check("收藏面板里能点到代码那条的回跳", await clickJump("daily", fx.snipCode));
const hiCode = await snipHighlight(flat(fx.snipCode));
check("那四行代码在日报里被高亮出来", hiCode === flat(fx.snipCode), hiCode);

// ── 2. 一字不差重复两次的整段:落点必须是第二处 ──────────────────────────────
// 这一段连它前后各三十个字都一样,所以「整段锚点命中了」证明不了位置。采集那头
// 要把前后文加长到唯一,回跳那头要求整段唯一 —— 两道缺一道,高亮就落在第一处,
// 而两处的文字一模一样,用户看不出来(审查实测的正是这一处)。
await evalJs(`switchTab('daily'); true`);
await until(`$('daily-reader-body').textContent.includes(${js(fx.ambiQuote)})`, 25000);
const ambi = await snip("#daily-reader-body", fx.ambiQuote, 2);
check("重复整段里的第二处收得下", ambi.shown, `picked=${ambi.picked}`);
const ambiFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.ambiQuote);
// 前后文得长过三十字 —— 三十字那一档两处一模一样,分不出来
check("锚点前后文加长到分得出两处为止", ambiFav.length === 1
  && ambiFav[0].prefix.length > 30,
  JSON.stringify(ambiFav.map(f => ({ n: f.prefix.length, p: f.prefix }))));
await openFavList();
check("收藏面板里能点到重复整段那条的回跳",
  await clickJump("daily", fx.ambiQuote));
const wantAmbi = await evalJs(
  `window.__textOffset('#daily-reader-body', ${js(fx.ambiQuote)}, 2)`);
const gotAmbi = await waitOffset("#daily-reader-body", wantAmbi);
check("重复整段回跳落在第二处,不是第一处", wantAmbi > 0 && gotAmbi === wantAmbi,
  `期望偏移 ${wantAmbi},实际 ${gotAmbi}`);

// 反过来钉一边:**第一处收下来也要落回第一处**。这一处是不对称的那一面 ——
// 整段锚点在第一处上 `indexOf` 恰好就等于采集偏移,于是"命中、而且位置对"会把
// 一条**并不唯一**的锚点判成"能定位",前后文再也不会加长;等回跳那头按唯一性
// 一问,每一侧都不止一处候选,换来的是一句"没找到"。采集那头必须按回跳那套口径判。
// 先把上面那条删掉:两条收藏的引文一模一样,回跳按钮按引文找就分不清了。
await evalJs(`(async () => {
  const f = (await window.__favs()).filter(x => x.quote === ${js(fx.ambiQuote)});
  for (const x of f) await fetch('/api/favorites/' + x.id, { method: 'DELETE' });
  return true;
})()`);
await evalJs(`switchTab('daily'); true`);
await until(`$('daily-reader-body').textContent.includes(${js(fx.ambiQuote)})`, 25000);
const ambi1 = await snip("#daily-reader-body", fx.ambiQuote, 1);
check("重复整段里的第一处也收得下", ambi1.shown, `picked=${ambi1.picked}`);
await openFavList();
check("收藏面板里能点到第一处那条的回跳", await clickJump("daily", fx.ambiQuote));
const want1 = await evalJs(
  `window.__textOffset('#daily-reader-body', ${js(fx.ambiQuote)}, 1)`);
const got1 = await waitOffset("#daily-reader-body", want1);
check("第一处回跳落在第一处(不是一句「没找到」)", want1 >= 0 && got1 === want1,
  `期望偏移 ${want1},实际 ${got1}`);

// ── 3. 我的订阅:盘上那篇不是这份正文时,不许绑文档 ───────────────────────────
// `path` 非空只说明"上一次导出留下了一篇",而译文写回只清 `exported_hash`、正文
// 补抓只换 `content_hash`,两条都保留 `path`。审查实测:重译完成、还没重导出的
// 那几分钟里收藏新译文里的一句,存成 origin=doc,点回去是旧英文、0 个高亮。
await evalJs(`switchTab('subs'); true`);
if (!await until(`!!document.getElementById('subs-switch-rss')`, 25000))
  throw new Error("订阅面板没列出 rss 那一档");
await evalJs(`document.getElementById('subs-switch-rss').click(); true`);
await until(`typeof sourceRows !== 'undefined'
  && sourceRows.some(r => r.key === ${js(fx.staleKey)})`, 30000);
await evalJs(`readSourceItem(${js(fx.staleKey)}); true`);
const staleReady = await until(
  `$('source-reader-body').textContent.includes(${js(fx.snipStale)})`, 20000);
check("订阅阅读器摊开的是重译之后那份正文", staleReady,
  await evalJs(`$('source-reader-body').textContent.slice(0, 60)`));
const stale = await snip("#source-reader-body", fx.snipStale);
check("重译还没重导出的那一条也收得下", stale.shown, `picked=${stale.picked}`);
const staleFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipStale);
// 判据:**不冒认能定位回那篇文档**。回跳走站外原文,而不是存一条点回去永远
// 「没找到」的收藏。
check("它退回 origin=feed,不绑那篇还没更新的 md", staleFav.length === 1
  && staleFav[0].origin === "feed" && staleFav[0].path === ""
  && staleFav[0].url === "https://example.org/retranslated",
  JSON.stringify(staleFav.map(f => ({ o: f.origin, p: f.path, u: f.url }))));

// 同一类的另一条,**判据立不住的那一半**:两个哈希相等,盘上那篇却已经是另一
// 份译文。`exported_hash=content_hash` 是**原文**的哈希,同一篇原文重译一遍、
// 重导出一遍它一个字节都不变 —— 旧判据(`exported_hash === content_hash`)于是
// 照旧说「盘上那篇就是屏幕上这份」。所以这条路不许靠客户端手里的字段推断,
// 得去读那一篇真正的正文(`snip.js` 的 `snipVerified` + `snipDocText`)。
await evalJs(`readSourceItem(${js(fx.swapKey)}); true`);
const swapReady = await until(
  `$('source-reader-body').textContent.includes(${js(fx.snipSwap)})`, 20000);
check("订阅阅读器摊开的是换过那份译文", swapReady,
  await evalJs(`$('source-reader-body').textContent.slice(0, 60)`));
// 先钉住这条夹具真是「哈希相等」那一种 —— 否则下面那条判据可能是靠别的理由过的
check("这一条的两个哈希相等 —— 旧判据会认它是最新的",
  await evalJs(`!!sourceReaderItem.exported_hash
    && sourceReaderItem.exported_hash === sourceReaderItem.content_hash`),
  await evalJs(`sourceReaderItem.exported_hash + ' | ' + sourceReaderItem.content_hash`));
const swap = await snip("#source-reader-body", fx.snipSwap);
check("换过译文的那一条也收得下", swap.shown, `picked=${swap.picked}`);
const swapFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipSwap);
check("哈希相等也不冒认绑得上那篇文档", swapFav.length === 1
  && swapFav[0].origin === "feed" && swapFav[0].path === ""
  && swapFav[0].url === fx.swapUrl,
  JSON.stringify(swapFav.map(f => ({ o: f.origin, p: f.path, u: f.url }))));
check("退的理由如实写进了提示", (await evalJs(`window.__toastText()`)).includes("核对不上"),
  await evalJs(`window.__toastText()`));

// 反过来钉一边:**同一个阅读器里,盘上那篇就是这份正文的那一条照旧绑文档**。
// 不钉的话"一律退 feed"也能过上面那条,而那是把这个功能关掉。
await evalJs(`readSourceItem(${js(fx.itemKey)}); true`);
await until(`$('source-reader-body').textContent.includes(${js(fx.snipFresh)})`, 20000);
const fresh = await snip("#source-reader-body", fx.snipFresh);
check("导出是最新的那一条照旧收得下", fresh.shown, `picked=${fresh.picked}`);
const freshFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipFresh);
check("它照旧绑那篇文档,走阅读页定位", freshFav.length === 1
  && freshFav[0].origin === "doc" && freshFav[0].path === fx.docPath,
  JSON.stringify(freshFav.map(f => ({ o: f.origin, p: f.path }))));

// ── 4. 同一条锚点,在独立阅读页那一头也要落在第二处 ──────────────────────────
// `/doc/…` 是脱离 kb-web 也能直接打开的独立产物,自带一份内联的采集 + 定位。
// 重复整段这件事两份实现各自判一次 —— 这一节拿**订阅那边存下来的那条锚点**去
// 那一页点回去(审查实测:那一页同样落在第一处)。
await evalJs(`switchTab('subs'); true`);
await evalJs(`readSourceItem(${js(fx.itemKey)}); true`);
await until(`$('source-reader-body').textContent.includes(${js(fx.ambiQuote)})`, 20000);
const ambiDoc = await snip("#source-reader-body", fx.ambiQuote, 2);
check("订阅正文里第二处也收得下", ambiDoc.shown, `picked=${ambiDoc.picked}`);
const docFav = (await evalJs(`window.__favs()`))
  .filter(f => f.quote === fx.ambiQuote && f.origin === "doc");
check("它绑的是那篇导出稿", docFav.length === 1 && docFav[0].path === fx.docPath,
  JSON.stringify(docFav.map(f => ({ o: f.origin, p: f.path }))));
await openFavList();
await until(`$('fav-list').querySelectorAll('a[data-doc]').length >= 1`, 15000);
// 用的是收藏面板自己拼出来的那条链接(`fav.js` 的 `#kbq=`),不是探针另拼一份
const docHref = await evalJs(`(() => {
  const a = [...$('fav-list').querySelectorAll('a[data-doc]')].find(x =>
    decodeURIComponent(x.getAttribute('href') || '').includes(${js(fx.ambiQuote)}));
  return a ? a.getAttribute('href') : '';
})()`);
check("收藏面板给出了带锚点的阅读页链接", !!docHref, String(docHref));
if (docHref) {
  await send("Page.navigate", { url: base + docHref });
  await until(`!!document.getElementById('content')
    && document.getElementById('content').textContent.includes(${js(fx.ambiQuote)})`, 25000);
  await evalJs(DOC_HELPERS);
  const gotDoc = await waitDocNth(fx.ambiQuote, 2);
  check("阅读页上也落在第二处,不是第一处", gotDoc === 2, `落在第 ${gotDoc} 处`);
  // 回首页接着往下跑:`boot()` 把帮手重新装一遍(导航把页面里那一套冲掉了)
  await boot();
}

// ── 4b. 锚点只有一边被另一种渲染扰动:那一档「只用后文」必须顶上 ─────────────
// 同一篇里还有一对**靠小标题分隔**的重复句(`MARK_DUP`)。订阅阅读器摊的是 md
// 原文(`## 乙一节` 带着井号),阅读页把它渲染成 <h2>(井号没了):整段那一档和
// 「只用前文」那一档在那一头都失配,引文自己又重复两次(裸引文那一档不唯一),
// **只剩「只用后文」那一档活着**;而它的后文窗口够到正文结尾,那儿的空白两头也
// 不一样(外侧空白不剪就连这一档也对不上)。判据是后果:存之前那道核对认它定位
// 得到(没退回 feed),点回去真落在第二处。
// 上一节导航去了阅读页、`boot()` 重新加载了首页 —— 所以这一节要**从头把订阅面板
// 铺好**(切到 rss 那一档、等 `sourceRows` 回来)再开条目,不能直接 `readSourceItem`:
// 名单还没回来时它压根打不开阅读器,而判据会表现成"选不中那段话"。
await evalJs(`switchTab('subs'); true`);
if (!await until(`!!document.getElementById('subs-switch-rss')`, 25000))
  throw new Error("订阅面板没列出 rss 那一档");
await evalJs(`document.getElementById('subs-switch-rss').click(); true`);
await until(`typeof sourceRows !== 'undefined'
  && sourceRows.some(r => r.key === ${js(fx.itemKey)})`, 30000);
await evalJs(`readSourceItem(${js(fx.itemKey)}); true`);
const markReady = await until(
  `$('source-reader-body').textContent.includes(${js(fx.markDup)})`, 20000);
check("订阅阅读器摊开了带小标题的那一段", markReady,
  await evalJs(`$('source-reader-body').textContent.slice(-80)`));
const mark = await snip("#source-reader-body", fx.markDup, 2);
check("跨小标题那一处也收得下", mark.shown, `picked=${mark.picked}`);
const markFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.markDup);
check("存之前那道核对认它定位得到,没退回 feed", markFav.length === 1
  && markFav[0].origin === "doc" && markFav[0].path === fx.docPath,
  JSON.stringify(markFav.map(f => ({ o: f.origin, p: f.path }))));
await openFavList();
await until(`$('fav-list').querySelectorAll('a[data-doc]').length >= 1`, 15000);
const markHref = await evalJs(`(() => {
  const a = [...$('fav-list').querySelectorAll('a[data-doc]')].find(x =>
    decodeURIComponent(x.getAttribute('href') || '').includes(${js(fx.markDup)}));
  return a ? a.getAttribute('href') : '';
})()`);
check("收藏面板给出了它的阅读页链接", !!markHref, String(markHref));
if (markHref) {
  await send("Page.navigate", { url: base + markHref });
  await until(`!!document.getElementById('content')
    && document.getElementById('content').textContent.includes(${js(fx.markDup)})`, 25000);
  await evalJs(DOC_HELPERS);
  const gotMark = await waitDocNth(fx.markDup, 2);
  check("阅读页上落在第二处 —— 前文那一边在这头对不上,靠后文那一档顶住",
    gotMark === 2, `落在第 ${gotMark} 处`);
  await boot();
}

// ── 5. 问答:会话在库里 ≠ 这一轮在库里 ───────────────────────────────────────
// 追问时会话号早就有了,而后端只在 NDJSON 收完之后才落库。生成中的半截回答、
// 「已停止(未保存)」、出错气泡、还没落库的那条提问,收下来都是"当场成功、刷新
// 之后那段话压根不存在"。沙箱里换了一条假的问答流(`serve_snippets._stub_stream`,
// 不连网不调模型),所以这几种状态是真走出来的,不是摆出来的 DOM。
await evalJs(`switchTab('ask'); openConv(${fx.convId}, true); true`);
await until(`$('chat-msgs').textContent.includes(${js(fx.snipChat)})`, 30000);
const ASK_HANG = "追问一句然后" + fx.hangMark;
await evalJs(`$('chat-input').value = ${js(ASK_HANG)}; sendChat(); true`);
const streaming = await until(
  `!!document.getElementById('chat-pending-body')
   && document.getElementById('chat-pending-body').textContent.includes(${js(fx.streamText)})`,
  25000);
check("假的问答流真的在流", streaming,
  await evalJs(`($('chat-msgs').textContent || '').slice(-80)`));
const midSel = await evalJs(`window.__snipSelect('#chat-msgs', ${js(fx.streamText)})`);
await sleep(400);
check("生成中的半截回答不许收藏", midSel === "ok"
  && !(await evalJs(`window.__snipBtnShown()`)), `picked=${midSel}`);
// 这一轮的**提问**也还没落库(它和回答是同一条 `_save_qa` 写进去的)
const midAsk = await evalJs(`window.__snipSelect('#chat-msgs', ${js(ASK_HANG)})`);
await sleep(400);
check("同一轮那条还没落库的提问也不许收藏", midAsk === "ok"
  && !(await evalJs(`window.__snipBtnShown()`)), `picked=${midAsk}`);

await evalJs(`stopChat(); true`);
const stopped = await until(`$('chat-msgs').textContent.includes('已停止(未保存)')`, 20000);
const stopSel = await evalJs(`window.__snipSelect('#chat-msgs', '已停止(未保存)')`);
await sleep(400);
check("「已停止(未保存)」不算正文", stopped && stopSel === "ok"
  && !(await evalJs(`window.__snipBtnShown()`)),
  `stopped=${stopped} picked=${stopSel}`);

// 正常收完那一轮:**这一刻起它才收得下**,而且回跳落得回去
const ASK_DONE = "再问一句,这次让它正常收完";
await evalJs(`$('chat-input').value = ${js(ASK_DONE)}; sendChat(); true`);
const done = await until(`$('chat-msgs').textContent.includes(${js(fx.streamTail)})
  && !document.getElementById('chat-pending')`, 30000);
check("那一轮正常收完了", done, await evalJs(`($('chat-msgs').textContent || '').slice(-80)`));
const doneSnip = await snip("#chat-msgs", fx.streamTail);
check("落库之后那段回答收得下", doneSnip.shown, `picked=${doneSnip.picked}`);
const doneFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.streamTail);
check("它记的是这个会话", doneFav.length === 1 && doneFav[0].origin === "chat"
  && doneFav[0].origin_ref === String(fx.convId),
  JSON.stringify(doneFav.map(f => ({ o: f.origin, r: f.origin_ref }))));
// 同一轮的提问这一刻也落库了 —— 标记是跟着落库那一刻补上的,不是渲染时猜的
const doneAsk = await snip("#chat-msgs", ASK_DONE);
check("同一轮的提问也跟着收得下了", doneAsk.shown, `picked=${doneAsk.picked}`);

// 最硬那一条:**把对话从库里重新读一遍**,那段话真的在(标记没撒谎)
await evalJs(`openConv(${fx.convId}, true); true`);
const reloaded = await until(`$('chat-msgs').textContent.includes(${js(fx.streamTail)})`, 25000);
check("从库里重新读出来,那段话真的在", reloaded,
  await evalJs(`($('chat-msgs').textContent || '').slice(-80)`));
await openFavList();
check("收藏面板里能点到这条新回答的回跳",
  await clickJump("chat", fx.streamTail));
const hiDone = await snipHighlight(fx.streamTail);
check("点回去那段话被高亮出来", hiDone === fx.streamTail, hiDone);

// ── 6. 标题和链接是内容自带的,长度上限两头必须对齐 ─────────────────────────
// 后端给 `origin_title`/`origin_url` 设了 `max_length`,而这两个值来自订阅条目、
// 图鉴条目 —— 带一长串跟踪参数的真实 URL 过 2048 是现实存在的。两头对不上的
// 后果是**整条 POST 被拒成 422**(404 那条退路也接不住它),用户选完才发现存不
// 下,屏幕上只有一句「收藏失败」。两头各钉一条:**真实长度的链接要原样存下来**
// (后端上限这一半)、**超过约定上限的链接整条丢掉而不是截一半**(采集层这一半,
// 半截 URL 点过去是另一个地方,比没有链接更坏),标题则一律截到后端认的长度。
// 直接调采集层那个发送函数 —— 这一条验的是契约对齐,不是选区。
const LONG_TITLE = "长".repeat(400);
const URL_REAL = "https://e.org/a?x=" + "b".repeat(3000);     // 真实可能出现的长度
const URL_HUGE = "https://e.org/a?x=" + "b".repeat(5000);     // 超过约定上限
const CLAMP_Q1 = "这一条钉的是长标题和真实长链接";
const CLAMP_Q2 = "这一条钉的是超过上限的链接整条丢掉";
const post = (quote, url) => evalJs(`(async () => {
  const r = await postSnip(
    { origin: 'daily', origin_ref: ${js(fx.dailyDate)}, path: '',
      origin_title: ${js(LONG_TITLE)}, origin_url: ${js(url)} },
    { quote: ${js(quote)}, prefix: '', suffix: '' });
  return r.status;
})()`);
const st1 = await post(CLAMP_Q1, URL_REAL);
check("内容自带的长标题长链接不会把整条收藏拒成 422", st1 === 200, `status=${st1}`);
const favs1 = (await evalJs(`window.__favs()`)).filter(f => f.quote === CLAMP_Q1);
check("标题截到后端认的长度,真实长度的链接原样存下来",
  favs1.length === 1 && favs1[0].title.length === 300 && favs1[0].url === URL_REAL,
  JSON.stringify(favs1.map(f => ({ t: f.title.length, u: (f.url || "").length }))));
// 标题裁口**正好落在一个字中间**:第 300 位是个 emoji 的前一半。切出来的半个字
// 后端存不下(整条 POST 被拒),而这个标题是内容自带的、用户什么也没做错。
const TITLE_SPLIT = "长".repeat(299) + "😀" + "尾".repeat(50);
const CLAMP_Q3 = "这一条钉的是标题裁口切在一个字中间";
const st3 = await evalJs(`(async () => {
  const r = await postSnip(
    { origin: 'daily', origin_ref: ${js(fx.dailyDate)}, path: '',
      origin_title: ${js(TITLE_SPLIT)}, origin_url: '' },
    { quote: ${js(CLAMP_Q3)}, prefix: '', suffix: '' });
  return r.status;
})()`);
const favs3 = (await evalJs(`window.__favs()`)).filter(f => f.quote === CLAMP_Q3);
check("标题裁到上限时不许切开一个字", st3 === 200 && favs3.length === 1
  && !/[\uD800-\uDFFF]/.test(favs3[0].title),
  `status=${st3} ` + JSON.stringify(favs3.map(f => ({ n: (f.title || "").length }))));

const st2 = await post(CLAMP_Q2, URL_HUGE);
const favs2 = (await evalJs(`window.__favs()`)).filter(f => f.quote === CLAMP_Q2);
check("超过上限的链接整条丢掉,不截成半截", st2 === 200
  && favs2.length === 1 && favs2[0].url === "",
  `status=${st2} url=${JSON.stringify(favs2.map(f => f.url))}`);

// ── 7. 正文里有 emoji:截断不许切开一个字 ────────────────────────────────────
// 选中的那几个字里一个 emoji 都没有,可**三十字那一档的前文窗口正好切在它中间**
// (emoji 占两个 UTF-16 单元)—— 切出来的半个字 UTF-8 编不出来,sqlite3 在 INSERT
// 那一句当场抛,整条 POST 变成 500(审查实测:日报和独立阅读页都收藏失败,收藏
// 数还是 0,而用户看不出自己选的字有什么问题)。判据落在后果上:**存下来了**、
// **库里那条前后文是整字**。两份实现各验一次(下面第 7b 节是阅读页那一份)。
await evalJs(`switchTab('daily'); true`);
const wideReady = await until(
  `$('daily-reader-body').textContent.includes(${js(fx.snipWide)})`, 40000);
check("日报里铺出了带 emoji 那一段", wideReady,
  await evalJs(`$('daily-reader-body').textContent.slice(0, 60)`));
const wide = await snip("#daily-reader-body", fx.snipWide);
check("emoji 后面那句普通文字收得下", wide.shown, `picked=${wide.picked}`);
const wideFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipWide);
check("它真落了库,不是一句 500", wideFav.length === 1,
  JSON.stringify(wideFav.map(f => ({ q: f.quote, p: f.prefix }))));
check("存下来的前后文是整字,没有半个字符",
  wideFav.length === 1 && wideFav[0].prefix === fx.widePrefix
  && !/[\uD800-\uDFFF]/.test(wideFav[0].prefix + wideFav[0].suffix),
  JSON.stringify(wideFav.map(f => ({ p: f.prefix, s: f.suffix.slice(0, 8) }))));
// 反过来钉一边:**定得准的那一条不许乱提示**(不然"一律提示定位不到"也能过
// 下面第 9 节那条,而那是把一句准话换成一句含糊话)
const wideToast = await evalJs(`window.__toastText()`);
check("定得准的那一条不说「可能定位不到」", !wideToast.includes("一模一样"), wideToast);
// 后端那一道:**半个字就该如实拒成 400**,不该等到 INSERT 抛一句 500
const half = await evalJs(`(async () => {
  const r = await fetch('/api/favorites', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ quote: '这一条的前后文里有半个字',
      prefix: '\uD83D' + 'zz', suffix: '', origin: 'daily',
      origin_ref: ${js(fx.dailyDate)}, origin_title: '日报 · 半个字' }) });
  return r.status + ' ' + (await r.text()).slice(0, 80);
})()`);
check("后端对半个字符如实拒成 400,不是 500", String(half).startsWith("400"), half);

// ── 8. 两边的位置证据互相矛盾:不许按档位顺序挑一处 ──────────────────────────
// 「只用前文」那一档唯一指向第一处、「只用后文」那一档唯一指向第二处,整段那一
// 档压根不存在。先采信前文那一档的话,后文那一边明明还在、明明指着别处,谁都没
// 去看 —— 换来的又是一个落在错地方的高亮(审查实测:app 高亮落在偏移 509 而收藏
// 的是 606,独立阅读页 227 / 324)。手搭这条锚点(正文改稿之后它就是这个形状),
// 断言两份实现都如实说没找到、**不许高亮到任何一处**。
await evalJs(`fetch('/api/favorites', { method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ quote: ${js(fx.confQ)}, prefix: ${js(fx.confP)},
    suffix: ${js(fx.confS)}, origin: 'daily', origin_ref: ${js(fx.dailyDate)},
    origin_title: '日报 · 两边矛盾' }) }).then(r => r.ok)`);
await openFavList();
const clickedConf = await clickJump("daily", fx.confQ);
check("收藏面板里能点到两边矛盾那条的回跳", clickedConf);
const confToast = await until(`window.__toastText().includes('没找到这段收藏')`, 20000);
check("两边矛盾时如实说没找到", confToast, await evalJs(`window.__toastText()`));
check("两边矛盾时一处也不许高亮", (await highlightText()) === "(没有高亮)",
  await highlightText());

// ── 9. 三档都分不出来:收藏那一刻就得说清楚 ─────────────────────────────────
// 同一句话在一篇里出现两次,两头各两百多个字一模一样 —— 前后文长到 200 字还是
// 不唯一。存照旧存(引文本身才是收藏的主要用处),但点回去只会是一句「没找到」,
// **收藏那一刻就得说**,不能让用户到点回去那一刻才发现。
await evalJs(`switchTab('subs'); true`);
if (!await until(`!!document.getElementById('subs-switch-rss')`, 25000))
  throw new Error("订阅面板没列出 rss 那一档");
await evalJs(`document.getElementById('subs-switch-rss').click(); true`);
await until(`typeof sourceRows !== 'undefined'
  && sourceRows.some(r => r.key === ${js(fx.itemKey)})`, 30000);
await evalJs(`readSourceItem(${js(fx.itemKey)}); true`);
const deadReady = await until(
  `$('source-reader-body').textContent.includes(${js(fx.deadQ)})`, 20000);
check("订阅阅读器摊开了怎么都分不出来那一段", deadReady,
  await evalJs(`$('source-reader-body').textContent.slice(-60)`));
const dead = await snip("#source-reader-body", fx.deadQ);
check("分不出是第几处的那一段照旧收得下", dead.shown, `picked=${dead.picked}`);
const deadToast = await evalJs(`window.__toastText()`);
check("收藏那一刻就说了点回去可能定位不到",
  deadToast.includes("几处一模一样的文字"), deadToast);

// ── 9b. 原文链接超长:退回 feed 之后那是唯一一条回原文的路 ──────────────────
// 超长就整条不送(半截 URL 点过去是另一个地方),可这一类**没有 path** ——
// 链接丢了就只能回到来源面板,收藏那一刻不说的话,用户要到点回去那一刻才发现。
await evalJs(`readSourceItem(${js(fx.longUrlKey)}); true`);
const longReady = await until(
  `$('source-reader-body').textContent.includes(${js(fx.snipLongUrl)})`, 20000);
check("订阅阅读器摊开了链接超长那一条", longReady,
  await evalJs(`$('source-reader-body').textContent.slice(0, 60)`));
const longSnip = await snip("#source-reader-body", fx.snipLongUrl);
check("链接超长那一条也收得下", longSnip.shown, `picked=${longSnip.picked}`);
const longFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipLongUrl);
check("超长链接整条没存(不截成半截)", longFav.length === 1 && longFav[0].url === "",
  JSON.stringify(longFav.map(f => ({ o: f.origin, u: (f.url || "").length }))));
const longToast = await evalJs(`window.__toastText()`);
check("收藏那一刻就说了链接没能一起存下", longToast.includes("链接太长"), longToast);

// ── 7b/9b. 独立阅读页那一份:同样两件事,它自己判一次 ────────────────────────
// `/doc/…` 自带一份内联的采集(`kb/web/pages.py` 的 `_DOC_SNIP_JS`),**那一头
// 也得不切开字符、也得在分不出第几处时说一句** —— 两份实现各有自己的红。
await send("Page.navigate", { url: base + "/doc/" + fx.docPath.split("/").map(encodeURIComponent).join("/") });
await until(`!!document.getElementById('content')
  && document.getElementById('content').textContent.includes(${js(fx.snipWide)})`, 25000);
const docWide = await docSnip(fx.snipWide);
check("阅读页上 emoji 后面那句也收得下", docWide.shown, `picked=${docWide.picked}`);
check("阅读页那一条也真落了库(不是一句失败)",
  docWide.toast.includes("已收藏") && !docWide.toast.includes("一模一样"),
  docWide.toast);
const docWideFav = await evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  const f = d.favorites.filter(x => x.quote === ${js(fx.snipWide)}
    && x.path === ${js(fx.docPath)});
  return JSON.stringify(f.map(x => ({ p: x.prefix, bad: /[\uD800-\uDFFF]/.test(x.prefix + x.suffix) })));
})()`);
check("阅读页存下来的前后文也是整字",
  docWideFav.includes(`"p":"${fx.widePrefix}"`) && docWideFav.includes('"bad":false'),
  docWideFav);
const docDead = await docSnip(fx.deadQ);
check("阅读页上分不出第几处的那一段照旧收得下", docDead.shown, `picked=${docDead.picked}`);
check("阅读页收藏那一刻也说了可能定位不到",
  docDead.toast.includes("几处一模一样的文字"), docDead.toast);
// 这一页的定位也要求两边一致(同第 8 节,那条验的是 app 那一侧)
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.confQ)}, p: ${js(fx.confP)},
    s: ${js(fx.confS)} })); true`);
const confDocToast = await docToast();
check("阅读页上两边矛盾时一处也不许高亮",
  (await highlightText()) === "(没有高亮)", await highlightText());
check("阅读页上两边矛盾时也如实说没找到",
  confDocToast.includes("没找到这段收藏"), confDocToast);
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
