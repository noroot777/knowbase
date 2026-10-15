// 「☆ 收藏此段」在**列表里那几截原文**上也收得下:订阅列表卡片的那一段正文、
// 搜索结果卡片的命中片段。
//
// 前四份探针验的是四个「完整正文」的容器(阅读器、日报、问答、图鉴条目)。这一份
// 管的是另一类入口:**列表上也摊着真原文**,不是另生成的摘要 —— 订阅列表那张卡片
// 直接显示正文开头那 380 字(短社交观点往往整条都在里面),搜索卡片直接显示命中
// chunk 的前 160 字。需求是「只要能展示原文的地方都能收」,漏掉它们的形态是:
// 用户在列表里读到一句想收,选中之后**什么都不发生**(按钮压根不浮出),而右侧
// 同一句话收得下 —— 屏幕上看不出这是漏了一处还是功能坏了。
//
// 判据分三类,一类都不许少:
//   1 **收得下、库里那条对、点回去落得回**(这一截正文的出处是命中那一篇文档);
//   2 **边界**:标题、作者、日期、LLM 摘要选中之后按钮不许浮出 —— 它们不是原文,
//     收了点回去一定找不到;
//   3 **装饰字符不算原文**:两处在正文被截断时都补了一个「…」,它在那一篇里压根
//     不存在,跨过它的选区存下来的引文里不许有它。
// 外加一条交互上的:**拖选不是点击** —— 订阅列表卡片整块可点(点开右侧阅读器),
// 一次拖选松手时浏览器照样派一个 click,不挡住的话那一下会把选区和浮动按钮一起
// 带走(窄屏上还会把列表整块藏起来)。
//
// 外壳(起浏览器、连 CDP、页内帮手)共用 ./snip_probe_lib.mjs。
//
//   HEADLESS_SHELL=<bin> node tests/snip_probe_cards.mjs <baseURL> <manifest.json>
import {
  base, boot, check, evalJs, favDocHref, finish, fx, js, openFavList, send,
  sleep, snip, snipHighlight, until,
} from "./snip_probe_lib.mjs";

const flat = s => String(s).replace(/\s+/g, " ");

await boot();

// ── 1. 订阅列表那张卡片上的一段正文 ─────────────────────────────────────────
// 订阅面板默认落在公众号那一档(`subsKind` 初值 wechat),rss 那一档要自己点。
await evalJs(`switchTab('subs'); true`);
if (!await until(`!!document.getElementById('subs-switch-rss')`, 25000))
  throw new Error("订阅面板没列出 rss 那一档");
await evalJs(`document.getElementById('subs-switch-rss').click(); true`);
const listReady = await until(
  `$('source-list').textContent.includes(${js(fx.snipList)})`, 30000);
check("订阅列表的卡片上摊出了那一截正文", listReady,
  await evalJs(`$('source-list').textContent.slice(0, 80)`));
// **先钉住「拖选不是点击」**:一次拖选松手时浏览器**既派 mouseup 也派 click**,
// 而卡片整块可点 —— 那一下不挡住的话右侧阅读器会被切走(窄屏上还会把列表整块
// 藏起来),选中的那几个字和浮出来的按钮跟着一起没。所以这里**真派一次 click**:
// 只派 mouseup 的话这条判据什么也证明不了(注入"去掉那道闸"照旧全绿)。
const readerKey = () => evalJs(
  `(() => { try { return sourceReaderItem ? sourceReaderItem.key : ''; } catch { return 'err'; } })()`);
const readerBefore = await readerKey();
const dragClick = await evalJs(`(() => {
  const got = window.__snipSelect('#source-list', ${js(fx.snipList)}, 1);
  if (got !== 'ok') return got;
  const n = getSelection().anchorNode;
  const el = n && (n.nodeType === 1 ? n : n.parentElement);
  const card = el && el.closest('.source-entry[data-source-key]');
  if (!card) return 'no-card';
  card.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  return 'ok';
})()`);
await sleep(400);
const draggedTo = await readerKey();
check("拖选松手那一下 click 没有把右侧阅读器切走",
  dragClick === "ok" && readerBefore === draggedTo,
  `${dragClick} / 选之前 ${readerBefore} / 松手之后 ${draggedTo}`);
const listPick = await snip("#source-list", fx.snipList);
check("列表卡片里那一段正文选中后浮出收藏按钮", listPick.shown, `picked=${listPick.picked}`);
const readerAfter = await readerKey();
check("收完之后右侧阅读器也还是原样", readerBefore === readerAfter,
  `选之前 ${readerBefore} / 选之后 ${readerAfter}`);
const listFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipList);
check("列表里收下的那条:origin=doc + 真 path + 原文一字不差",
  listFav.length === 1 && listFav[0].origin === "doc"
  && listFav[0].path === fx.docPath && listFav[0].quote === fx.snipList,
  JSON.stringify(listFav.map(f => ({ o: f.origin, p: f.path, q: f.quote }))));
// **容器只是那一篇的一截**,所以唯一性不按这一截断言 —— 这一句在整篇里确实只有
// 一处,所以重算之后该是 1(按盘上那一篇算,不是按卡片上那 380 字算)
check("唯一性是按盘上那一篇重算的", listFav.length === 1 && listFav[0].quote_unique === 1,
  JSON.stringify(listFav.map(f => f.quote_unique)));
// **卡片只是那一篇的一截,所以从它身上算不出整篇的事。** 两条各管一头:
// ① 核得过的那一类:唯一性按**盘上那一篇**重算 —— 这一句在卡片那 380 字里只有
//    一处,整篇里还有一处,照切片存下去就是给裸引文那一档发一张它不配的通行证
//    (回跳那一刻会命中整篇里的另一处)。
const dupPick = await snip("#source-list", fx.snipListDup);
check("卡片里那句「切片唯一、整篇有两处」的话收得下", dupPick.shown, `picked=${dupPick.picked}`);
const dupFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipListDup);
check("它记的是「采集那会儿不只一处」(按盘上那一篇算,不按卡片那一截)",
  dupFav.length === 1 && dupFav[0].quote_unique === 0,
  JSON.stringify(dupFav.map(f => ({ u: f.quote_unique, o: f.origin }))));
// ② 核不过的那一类(卡片按 textContent 原样摊,星号看得见;阅读页渲染过,星号
//    没了)退回 origin=feed —— 那一类不绑文档、也就**没有重算的机会**,唯一性只能
//    按切片算,所以一律按「说不清」存,不许声称唯一。
const markPick = await snip("#source-list", fx.snipListMarked);
check("卡片里那句夹着星号的话也收得下", markPick.shown, `picked=${markPick.picked}`);
const markFav = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipListMarked);
check("它退回 origin=feed,而且没有声称唯一",
  markFav.length === 1 && markFav[0].origin === "feed" && markFav[0].path === ""
  && markFav[0].quote_unique === 0,
  JSON.stringify(markFav.map(f => ({ o: f.origin, p: f.path, u: f.quote_unique }))));

// 边界:卡片上那些**不是正文**的字一个都不许收(逐处各选一次、各判一次 ——
// 一次选三处的话按钮的末态只说得清最后那一处)
for (const [name, sel] of [["标题", ".source-entry-title"],
                           ["作者和日期", ".source-entry-meta"],
                           ["页脚那一行", ".source-entry-footer"]]) {
  const picked = await evalJs(`(() => {
    const card = document.querySelector('.source-entry[data-source-key]');
    const el = card && card.querySelector(${js(sel)});
    if (!el) return 'no-el';
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if ((n.nodeValue || '').trim().length < 4) continue;
      const r = document.createRange();
      r.setStart(n, 0); r.setEnd(n, Math.min(n.nodeValue.length, 8));
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
      document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      return s.toString();
    }
    return 'no-text';
  })()`);
  await sleep(300);
  check(`卡片上的${name}不算正文`,
    picked !== "no-el" && picked !== "no-text"
    && !(await evalJs(`window.__snipBtnShown()`)), `picked=${picked}`);
}

// 回跳:**绑了文档的那一类在面板里是一条带 `#kbq=` 的阅读页链接**(不是
// `.fav-jump` 按钮,那是没有 path 的那几类才有的)。href 由 `fav.js` 现拼,所以
// 顺带把那一头也钉上 —— 直接导航到它,验那一页上高亮落没落在这一段。
await openFavList();
const listHref = await favDocHref(fx.snipList);
check("收藏面板里那条的回跳链接带着这一段的锚点", !!listHref, listHref);
await send("Page.navigate", { url: base + listHref });
await until(`!!document.getElementById('content')
  && document.getElementById('content').textContent.includes(${js(fx.snipList)})`, 25000);
const listHi = await snipHighlight(fx.snipList);
check("点回去那段话在阅读页上被高亮出来", listHi === fx.snipList, listHi);
await boot();

// ── 2. 搜索结果卡片上那一段命中原文 ─────────────────────────────────────────
// `.snippet` 摊的是命中 chunk 的前 160 字(`kb/search.py` 的 `best_chunk`),那是
// 文件正文里的一段真原文 —— 不是 `.summary` 那块模型写的摘要。
await evalJs(`switchTab('search'); $('q').value = '技能'; doSearch(); true`);
const hitsReady = await until(
  `$('results').querySelectorAll('.hit[data-hit]').length >= 1`, 30000);
check("搜索出了结果卡片", hitsReady,
  await evalJs(`$('results').textContent.slice(0, 80)`));
// 选 `.snippet` 里中间那 40 个字(避开两头的空白和省略号),按**屏幕上读到的**那份
// 文字断言 —— chunk 切在哪儿由索引决定,不该写进判据里
const picked = await evalJs(`(() => {
  const card = [...document.querySelectorAll('.hit[data-hit]')]
    .find(c => ((c.querySelector('.snippet') || {}).textContent || '').length >= 60);
  if (!card) return JSON.stringify({ err: 'no-long-snippet' });
  const body = card.querySelector('.snippet');
  const node = [...body.childNodes].find(n => n.nodeType === 3 && n.nodeValue.length >= 60);
  if (!node) return JSON.stringify({ err: 'no-text-node' });
  const r = document.createRange();
  r.setStart(node, 10); r.setEnd(node, 50);
  const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  body.scrollIntoView({ block: 'center' });
  document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  return JSON.stringify({ sel: s.toString(), path: card.querySelector('.favbtn').dataset.path });
})()`);
const hit = JSON.parse(picked);
check("搜索卡片上那段命中原文选得出来", !hit.err, picked);
const hitShown = await until(`window.__snipBtnShown()`, 5000);
if (hitShown) {
  await evalJs(`document.getElementById('snipbtn').click(); true`);
  await until(`!window.__snipBtnShown()`, 10000);
}
check("搜索卡片上那段命中原文选中后浮出收藏按钮", hitShown, picked);
const hitFav = (await evalJs(`window.__favs()`)).filter(f => flat(f.quote) === flat(hit.sel));
check("搜索卡里收下的那条:origin=doc + 命中那一篇的 path + 屏幕上那份文字",
  hitFav.length === 1 && hitFav[0].origin === "doc"
  && hitFav[0].path === decodeURIComponent(hit.path || "") && hitFav[0].quote === hit.sel,
  JSON.stringify({ got: hitFav.map(f => ({ o: f.origin, p: f.path, q: f.quote })),
                   want: hit }));

// ── 3. 那个「…」不是原文里的字 ───────────────────────────────────────────────
// 正文被截断时两处都补了一个省略号。它在那一篇里压根不存在 —— 跨过它的选区存下来
// 的引文里一个都不许有,不然点回去必然定位不到(而面板上那条看着好好的)。
const moreSel = await evalJs(`(() => {
  const more = document.querySelector('.hit[data-hit] .snippet .snip-more');
  if (!more) return JSON.stringify({ err: 'no-more' });
  const body = more.parentElement;
  const node = [...body.childNodes].find(n => n.nodeType === 3 && n.nodeValue.length >= 20);
  if (!node) return JSON.stringify({ err: 'no-text-node' });
  const r = document.createRange();
  r.setStart(node, node.nodeValue.length - 20);
  r.setEnd(more.firstChild, more.firstChild.nodeValue.length);
  const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  body.scrollIntoView({ block: 'center' });
  document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  return JSON.stringify({ sel: s.toString() });
})()`);
const more = JSON.parse(moreSel);
check("屏幕上那个省略号真的在选区里(好让下面那条判得准)",
  !more.err && String(more.sel).includes("…"), moreSel);
const moreShown = await until(`window.__snipBtnShown()`, 5000);
if (moreShown) {
  await evalJs(`document.getElementById('snipbtn').click(); true`);
  await until(`!window.__snipBtnShown()`, 10000);
}
check("跨过省略号的那一段照旧收得下", moreShown, moreSel);
const tailWant = String(more.sel).replace(/…+$/, "");
const moreFav = (await evalJs(`window.__favs()`))
  .filter(f => flat(f.quote) === flat(tailWant));
check("库里那条引文里没有那个省略号",
  moreFav.length === 1 && !moreFav[0].quote.includes("…"),
  JSON.stringify({ got: (await evalJs(`window.__favs()`))
    .filter(f => f.quote.includes("…")).map(f => f.quote), want: tailWant }));

// ── 4. 搜索卡上只有 `.snippet` 那一块是原文 ─────────────────────────────────
// 标题、那行元信息、**模型写的摘要**(`.summary`)、实体按钮,一处都不许收 ——
// 它们不是原文,收了点回去一定找不到。换一个搜索词:工具那一篇的卡片上摘要和
// 实体都在(上面那一轮命中的是订阅那篇,它没有摘要),**不然这一节会因为"一处
// 都没取到"而看着是绿的**。
await evalJs(`switchTab('search'); $('q').value = ${js(fx.toolNorm)}; doSearch(); true`);
if (!await until(`$('results').querySelectorAll('.hit[data-hit]').length >= 1`, 30000))
  throw new Error("按工具名搜不出结果卡片");
/** 在卡片的某一块界面文字上选几个字,回选中的那段(没有那一块回 `no-el`)。*/
const pickIn = sel => evalJs(`(() => {
  const body = [...document.querySelectorAll('.hit[data-hit] ' + ${js(sel)})]
    .find(el => (el.textContent || '').trim().length >= 4);
  if (!body) return 'no-el';
  const w = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    if ((n.nodeValue || '').trim().length < 4) continue;
    const r = document.createRange();
    r.setStart(n, 0); r.setEnd(n, Math.min(n.nodeValue.length, 8));
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    body.scrollIntoView({ block: 'center' });
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    return s.toString();
  }
  return 'no-text';
})()`);
for (const [name, sel] of [["标题", "h3"], ["那行元信息", ".meta"],
                           ["模型写的摘要", ".summary"], ["实体按钮那一行", ".ents"]]) {
  const picked = await pickIn(sel);
  await sleep(300);
  check(`搜索卡上的${name}不算原文`,
    picked !== "no-el" && picked !== "no-text"
    && !(await evalJs(`window.__snipBtnShown()`)), `picked=${picked}`);
}

finish();
