// 「☆ 收藏此段」**绑得对不对**:位置绑在哪一处、版本绑在哪一版。
//
// 这一份和前两份(`snip_probe.mjs` 四处正文收得下点得回去、`snip_probe_edges.mjs`
// 收下来之后还作不作数)管的是同一条链上最后两种"当场看着成功"的半成品:
//
//   1 **位置证据只听了一半。** 前文那一边说"只可能是第一处或第二处",后文那一边
//     唯一指向第三处 —— 把"有两处"和"没有"压成同一个值的话,前文那个集合就丢了,
//     于是高亮落在一处前文已经明确排除掉的地方(审查实测:日报三处偏移
//     809/912/1015,收藏的是 912、高亮落在 1015;独立阅读页 223/326/429,收藏的
//     是 326、高亮落在 429)。**两份定位实现各判一次**。
//   2 **核对和落库之间正文换了一版。** 核对读的是服务端那一篇、写入是另一个请求,
//     中间那一下重译写回/正文补抓照样会把那一篇换掉 —— 没有共同的版本号的话,
//     收藏照旧落成 origin=doc 返回 200,立刻回看是新正文、原句不在、0 个高亮
//     (审查实测)。**三个绑文档的入口各判一次**:订阅阅读器(有退路 → 退回
//     feed)、独立阅读页和图鉴抽屉(没有退路 → 如实拒,并说清怎么办)。
//
// 判据一律落在后果上:**库里那条收藏是什么**(有没有、origin 是哪一类、绑没绑
// path)、**高亮落在第几处**(不是"文字对不对" —— 三处的文字都对)、**屏幕上那
// 句提示说的是哪一种退法**。
//
// 「盘上那一篇换了一版」靠沙箱独有的两条路由摆成确定时序(`tests/serve_snippets.py`
// 的 `install_test_routes`):`touch` 真改文件,`swap-after-read` 把 touch 挪到
// 「那一次 `/api/doc` 的响应已经生成好、POST 还没发出」之间 —— 一个响应都不伪造。
//
// 外壳(起浏览器、连 CDP、页内帮手、独立阅读页那一头)共用 ./snip_probe_lib.mjs。
//
//   HEADLESS_SHELL=<bin> node tests/snip_probe_bind.mjs <baseURL> <manifest.json>
import {
  base, boot, check, clickJump, DOC_HELPERS, docSnip, docToastClear, evalJs,
  finish, fx, highlightText, js, openFavList, send, sleep, snip, until,
} from "./snip_probe_lib.mjs";

const docUrl = rel => base + "/doc/" + rel.split("/").map(encodeURIComponent).join("/");

/** 沙箱那几条路由(真改盘上那一篇 / 真删一句 / 把那一改挪到核对之后)。
 *  回 HTTP 状态。`text` 只有 `drop` 要。*/
const sandbox = (route, path, text = "") => evalJs(
  `fetch('/__snip__/' + ${js(route)} + '?path=' + encodeURIComponent(${js(path)})
     + (${js(text)} ? '&text=' + encodeURIComponent(${js(text)}) : ''),
     { method: 'POST' }).then(r => r.status)`);

/** 把提示区清空再做下一件事:「这一条**没有**说刷新」那类判据,不清的话会读到
 *  上一条留下的那句话。*/
const appToastClear = () => evalJs(
  `(() => { const h = document.getElementById('toast-host');
     if (h) h.innerHTML = ''; return true; })()`);

/** 库里引文等于 q 的那一条的前后文(判「锚点是从哪儿采的」用)。*/
const favRowOf = q => evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  const f = d.favorites.find(x => x.quote === ${js(q)});
  return f ? JSON.stringify({ pre: f.prefix, suf: f.suffix }) : 'null';
})()`);

/** 手搭一条 origin=daily 的收藏(锚点由探针给,不经过采集那一步)。
 *  正文改稿之后锚点就是这个形状,而改稿本身不是这条判据要验的东西。*/
const putDaily = (q, p, s, title) => evalJs(
  `fetch('/api/favorites', { method: 'POST',
     headers: { 'Content-Type': 'application/json' },
     body: JSON.stringify({ quote: ${js(q)}, prefix: ${js(p)}, suffix: ${js(s)},
       origin: 'daily', origin_ref: ${js(fx.dailyDate)},
       origin_title: ${js(title)} }) }).then(r => r.status)`);

/** 库里引文等于 q 的那几条,整成一段 JSON 文本(只留判据要看的那两个字段)。
 *  **自带 fetch,不走外壳装在页面里那套帮手**:独立阅读页是另一个页面上下文,
 *  那套帮手在那儿压根不存在(上一版就撞在这儿 —— 第 3 节一上来就 Uncaught)。*/
const favsOf = q => evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  return JSON.stringify(d.favorites.filter(f => f.quote === ${js(q)})
    .map(f => ({ o: f.origin, p: f.path || null })));   // 接口把 NULL 给成空串
})()`);

await boot();

// ── 1. 两侧候选不相交:不许只听还说得上话的那一边(app 那一侧) ──────────────
await evalJs(`switchTab('daily'); true`);
const dailyReady = await until(
  `dailyCurrent === ${js(fx.dailyDate)}
   && $('daily-reader-body').textContent.includes(${js(fx.disjQ)})`, 40000);
check("日报面板铺出了不相交那三处", dailyReady,
  await evalJs(`$('daily-reader-body').textContent.slice(0, 60)`));
// 同一个 q、同一个 p,两条只差后文:一条的后文指第三处(和前文的候选不相交),
// 一条的后文指第一处(落在前文的候选里)。**两条都得判对** —— 只拒不放行的话,
// "一有歧义就全拒"也能过上面那条,而那是把能定位的收藏也废掉。
await putDaily(fx.disjQ, fx.disjP, fx.disjSFar, "日报 · 两侧不相交");
await putDaily(fx.disjQ, fx.disjP, fx.disjSNear, "日报 · 两侧有交集");
await openFavList();
const clickedFar = await clickJump("daily", fx.disjQ, fx.disjSFar);
check("收藏面板里能点到不相交那条的回跳", clickedFar);
const farToast = await until(`window.__toastText().includes('没找到这段收藏')`, 20000);
check("两侧候选不相交时如实说没找到", farToast,
  await evalJs(`window.__toastText()`));
check("两侧候选不相交时一处也不许高亮",
  (await highlightText()) === "(没有高亮)", await highlightText());
// 反过来钉一边:**前文那一边照旧有两处候选**,可后文指的正是其中一处 —— 交集
// 唯一,这一条必须照旧落回第一处(偏移按压平坐标系比,文字三处都一样)
await openFavList();
const clickedNear = await clickJump("daily", fx.disjQ, fx.disjSNear);
check("收藏面板里能点到有交集那条的回跳", clickedNear);
const wantFirst = await evalJs(
  `window.__textOffset('#daily-reader-body', ${js(fx.disjQ)}, 1)`);
let gotNear = -1;
for (let i = 0; i < 120 && gotNear !== wantFirst; i++) {
  gotNear = await evalJs(`window.__hiOffset('#daily-reader-body')`);
  if (gotNear !== wantFirst) await sleep(150);
}
check("前文有两处候选、后文指着其中一处时照旧落回那一处",
  gotNear === wantFirst && wantFirst >= 0, `高亮偏移 ${gotNear},期望 ${wantFirst}`);

// ── 2. 同一件事,独立阅读页自己判一次 ───────────────────────────────────────
// `/doc/…` 自带一份内联的定位实现(`kb/web/pages.py` 的 `_DOC_SNIP_JS`,那一页
// 脱离 kb-web 也要能用)—— **两份实现各要有自己的红**。
await send("Page.navigate", { url: docUrl(fx.docPath) });
await until(`!!document.getElementById('content')
  && document.getElementById('content').textContent.includes(${js(fx.disjQ)})`, 25000);
await evalJs(DOC_HELPERS);
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.disjQ)}, p: ${js(fx.disjP)},
    s: ${js(fx.disjSFar)} })); true`);
const docFar = await until(
  `(document.getElementById('kbtoast').textContent || '').includes('没找到这段收藏')`,
  20000);
check("阅读页上两侧候选不相交时一处也不许高亮",
  (await highlightText()) === "(没有高亮)", await highlightText());
check("阅读页上两侧候选不相交时也如实说没找到", docFar,
  await evalJs(`document.getElementById('kbtoast').textContent`));
// 反过来钉一边(同第 1 节末尾):交集唯一的那一条照旧落回第一处
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.disjQ)}, p: ${js(fx.disjP)},
    s: ${js(fx.disjSNear)} })); true`);
let docNth = -1;
for (let i = 0; i < 120 && docNth !== 1; i++) {
  docNth = await evalJs(`window.__docHiNth(${js(fx.disjQ)})`);
  if (docNth !== 1) await sleep(150);
}
check("阅读页上交集唯一的那一条照旧落回第一处", docNth === 1,
  `高亮落在第 ${docNth} 处`);

// ── 3. 独立阅读页:页面上这份正文已经旧了,不许把收藏绑上去 ──────────────────
// 这一页把「我摊的是哪一版正文」烤在 HTML 里(`DOC_VERSION`),所以它天然就是
// 「手里拿着旧版本」那个局面 —— 不用任何时序栅栏。先钉另一边:**新开的页面照旧
// 收得下**(不然"一律拒"也能过下面那条)。
await send("Page.navigate", { url: docUrl(fx.toolPath) });
await until(`!!document.getElementById('content')
  && document.getElementById('content').textContent.includes(${js(fx.toolVFresh)})`,
  25000);
const freshDoc = await docSnip(fx.toolVFresh);
check("新开的阅读页照旧收得下", freshDoc.shown && freshDoc.toast.includes("已收藏"),
  `picked=${freshDoc.picked} toast=${freshDoc.toast}`);
check("它真绑上了那一篇",
  (await favsOf(fx.toolVFresh)) === JSON.stringify([{ o: "doc", p: fx.toolPath }]),
  await favsOf(fx.toolVFresh));
// 现在真把盘上那一篇换一版(页面不刷新,手里那个 DOC_VERSION 就旧了)
check("沙箱真改掉了盘上那一篇", (await sandbox("touch", fx.toolPath)) === 200);
const staleDoc = await docSnip(fx.toolVStale);
check("正文换版之后阅读页如实拒掉,并说清刷新再收",
  staleDoc.shown && staleDoc.toast.includes("刷新"),
  `picked=${staleDoc.picked} toast=${staleDoc.toast}`);
check("被拒的那一条一行都没落库", (await favsOf(fx.toolVStale)) === "[]",
  await favsOf(fx.toolVStale));

// ── 4. 图鉴抽屉:版本号来自条目接口,**而能不能回看要按阅读页那一头的文本算** ──
// 抽屉可以开着好几分钟,正文在这期间被换掉不会通知这一页;它也**没有退路**
// (回跳只有阅读页这一条)。所以这一节分四件事各钉一遍:
//   4a 同一个文件版本,两头的文本却不是同一份(正文里带原始 HTML)→ 照存、
//      但当场说清"点回去可能定位不到" —— 这是审查实测那一条:抽屉里选到的整句
//      在阅读页那一头中间插着四个尖括号字,核对本来就该判定位不到。
//   4b 可收的范围和采锚点的范围**都只在正文上**:摘要行(账号 / 日期 / 文章标题)
//      选中不许弹按钮,抽屉里那三颗链接按钮和小节标题不许进锚点。
//   4c 盘上那一篇**补了一段、原句还在** → 照存、绑新那一版(版本号不等不等于
//      回看不了)。
//   4d 盘上那一篇把那句话**删掉了** → 这一下才真的回看不了:如实拒、0 行。
await boot();
await evalJs(`switchTab('gallery'); true`);
if (!await until(`typeof openToolDrawer === 'function'`, 15000))
  throw new Error("图鉴面板的脚本没加载起来");
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
const drawerReady = await until(
  `$('td-entries').textContent.includes(${js(fx.toolVStale)})`, 25000);
check("图鉴抽屉铺出了那一条记录的正文", drawerReady,
  await evalJs(`$('td-entries').textContent.slice(0, 60)`));
check("抽屉里那一条记录带着它的正文版本号",
  await evalJs(`!!document.querySelector('.td-entry').dataset.snipVersion`),
  await evalJs(`JSON.stringify(document.querySelector('.td-entry').dataset)`));
// 4a 原始 HTML:屏幕上是连着的一句,阅读页那一头不是
check("抽屉里那一句显示成了粗体(标签本身看不见)",
  await evalJs(`!!document.querySelector('.td-entry .body strong')
    && $('td-entries').textContent.includes(${js(fx.toolHtmlSeen)})`),
  await evalJs(`$('td-entries').textContent.slice(0, 200)`));
await appToastClear();
// 这一句落在三个 text node 上(<strong> 把它切成了三段),所以用跨元素那一档选法
const htmlTool = await snip("#td-entries", fx.toolHtmlSeen, 1, "span");
check("跨过原始 HTML 的那一句照旧收得下", htmlTool.shown, `picked=${htmlTool.picked}`);
const htmlToast = await evalJs(`window.__toastText()`);
check("并且当场说清了点回去可能定位不到这一处",
  htmlToast.includes("定位不到"), htmlToast);
check("它绑的还是那一篇(文件是对的,只是两头的文本不一样)",
  (await favsOf(fx.toolHtmlSeen)) === JSON.stringify([{ o: "doc", p: fx.toolPath }]),
  await favsOf(fx.toolHtmlSeen));
// 反过来钉一边:核对那一步**不许动全局那套渲染**。核对要按阅读页的规则算
// (原始 HTML 当字面文字),而 `marked.use()` 是全局的 —— 拿它换规则的话,图鉴、
// 日报、问答、日报素材账本就都跟着变了(那几处要的正是"标签当标签")。核过一次
// 之后原地重渲染这一条,那一句照旧得是粗体。
await evalJs(`renderToolEntries(toolMeta); true`);
check("核对过之后再渲染一次,那一句照旧是粗体(没把全局那套渲染改掉)",
  await evalJs(`!!document.querySelector('.td-entry .body strong')`),
  await evalJs(`document.querySelector('.td-entry .body').innerHTML.slice(0, 200)`));

// 4b 可收的范围、采锚点的范围,都只在正文上
await appToastClear();
// 反过来钉一边:这条记录的 `<details>` 里**正文之外还住着别的字** —— 下面那三
// 颗链接按钮(「原文 / Obsidian / 在阅读器打开」)。它们在目标文档里压根不存在,
// 选中不许弹按钮,存下来就是一条点回去必定落空的收藏。
// **判据落在那三颗按钮上,不落在摘要行上**:摘要行(账号 / 日期 / 文章标题)住在
// `<summary>` 里,那一段文字连浏览器都不交出来(程序设好 Range 之后
// `getSelection().toString()` 是空串,于是字数那道判据先把按钮按下去了),
// 拿它当判据的话,去掉收窄那一句照旧全绿 —— 验的是浏览器而不是我们这段代码。
const linkTool = await snip("#td-entries", "在阅读器打开");
check("抽屉里那颗链接按钮的文案确实选中了(不然下面那条判据说不清)",
  String(linkTool.picked).startsWith("ok"), `picked=${linkTool.picked}`);
check("正文之外那几颗按钮的文案选中不弹按钮", !linkTool.shown,
  `picked=${linkTool.picked}`);
check("它们一行都没落库", (await favsOf("在阅读器打开")) === "[]",
  await favsOf("在阅读器打开"));
const tailTool = await snip("#td-entries", fx.toolTail);
check("正文最后一句照旧收得下", tailTool.shown, `picked=${tailTool.picked}`);
const tailRow = await favRowOf(fx.toolTail);
check("库里有这一条(先钉住,不然下面那条判据没有行可看、永远成立)",
  tailRow !== "null", tailRow);
// 正则里**那三颗按钮的文案要一个不少**:后文只取三十来个字,漏了打头那一颗
// (「原文」)的话,窗口压根够不到后面两颗 —— 这条判据就跟采集范围无关了
// (实测注入"采集范围不收窄"之后 suffix 是 `。 …(第 1 次) 原文`,照旧全绿)。
check("前后文里没有抽屉那几颗按钮的文案",
  !/原文|Obsidian|在阅读器打开|就这个工具提问/.test(tailRow), tailRow);
// 4c 补了一段(原句还在):该放行
check("沙箱给盘上那一篇补了一段", (await sandbox("touch", fx.toolPath)) === 200);
await appToastClear();
const keptTool = await snip("#td-entries", fx.toolVKept);
check("补过一段之后那一句照旧收得下", keptTool.shown, `picked=${keptTool.picked}`);
const keptToast = await evalJs(`window.__toastText()`);
check("没有拿「版本号不等」当理由拒掉它", !keptToast.includes("刷新"), keptToast);
check("它绑的是那一篇",
  (await favsOf(fx.toolVKept)) === JSON.stringify([{ o: "doc", p: fx.toolPath }]),
  await favsOf(fx.toolVKept));
// 4d 那句话被改稿删掉了:这才是真的回看不了
check("沙箱把那一句从盘上那一篇里删掉了",
  (await sandbox("drop", fx.toolPath, fx.toolVStale)) === 200);
await appToastClear();
const staleTool = await snip("#td-entries", fx.toolVStale);
check("改稿把那句话删掉之后,图鉴这一条如实拒掉", staleTool.shown,
  `picked=${staleTool.picked}`);
const toolToast = await evalJs(`window.__toastText()`);
check("并且说清了刷新再收藏", toolToast.includes("刷新"), toolToast);
check("图鉴那一条也一行都没落库", (await favsOf(fx.toolVStale)) === "[]",
  await favsOf(fx.toolVStale));
// 反过来钉一边:**刷新之后又收得下**(版本号是真的在跟着那一篇走,不是"绑了
// 文档就一律拒")。**刷整页而不是关掉重开抽屉** —— 同一个工具重开直接用现成
// 视图(`openToolDrawer` 的 `!changed && toolMeta` 那一句),版本号跟着那份视图
// 一起还是旧的。提示里那句话也只好说"刷新"(说"重新打开这条记录"就不灵了)。
await boot();
await evalJs(`switchTab('gallery'); true`);
await until(`typeof openToolDrawer === 'function'`, 15000);
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
await until(`$('td-entries').textContent.includes(${js(fx.toolVAgain)})`, 25000);
const againTool = await snip("#td-entries", fx.toolVAgain);
check("刷新之后照旧收得下", againTool.shown, `picked=${againTool.picked}`);
check("它绑的还是那一篇",
  (await favsOf(fx.toolVAgain)) === JSON.stringify([{ o: "doc", p: fx.toolPath }]),
  await favsOf(fx.toolVAgain));
await evalJs(`closeToolDrawer(); true`);

// ── 5. 订阅阅读器:它是三个入口里唯一**有退路**的那个 ───────────────────────
// 核对那一次 `/api/doc` 读到的是服务端那一篇,而写入是另一个请求 —— 下面两节各
// 摆一种"中间出了事":读不出来(5b)、正文换了一版(6)。两种都该退回站外原文,
// 并**如实说退的是哪一种**。
await evalJs(`switchTab('subs'); true`);
if (!await until(`!!document.getElementById('subs-switch-rss')`, 25000))
  throw new Error("订阅面板没列出 rss 那一档");
await evalJs(`document.getElementById('subs-switch-rss').click(); true`);
await until(`typeof sourceRows !== 'undefined'
  && sourceRows.some(r => r.key === ${js(fx.itemKey)})`, 30000);
await evalJs(`readSourceItem(${js(fx.itemKey)}); true`);
const raceReady = await until(
  `$('source-reader-body').textContent.includes(${js(fx.snipRace)})`, 20000);
check("订阅阅读器摊开了竞态那一句", raceReady,
  await evalJs(`$('source-reader-body').textContent.slice(0, 60)`));
// ── 5b. 「这一刻读不出来」不等于「这一篇不在索引里」 ─────────────────────────
// 两种都退回站外原文,可**说给用户的话不一样**:后者是个已经证明了的结论(等索引
// 跑完就好了),前者只是一次失败。把前者说成后者,用户据此做的判断就是错的。
check("沙箱把盘上那一篇变成读不出来的",
  (await sandbox("unreadable", fx.docPath)) === 200);
const unread = await snip("#source-reader-body", fx.snipUnreadable);
check("读不出盘上那一篇时照旧收得下", unread.shown, `picked=${unread.picked}`);
check("它退成了 origin=feed",
  (await favsOf(fx.snipUnreadable)) === JSON.stringify([{ o: "feed", p: null }]),
  await favsOf(fx.snipUnreadable));
const unreadToast = await evalJs(`window.__toastText()`);
check("说的是「读不出来」,不是「还没进检索索引」",
  unreadToast.includes("读不出") && !unreadToast.includes("检索索引"), unreadToast);
check("沙箱把那一篇换回来了", (await sandbox("unreadable", fx.docPath)) === 200);

// ── 6. 订阅阅读器:核对完到落库这一下,盘上那一篇被换了一版 ──────────────────
check("沙箱把「换一版」挂在了那一次核对之后",
  (await sandbox("swap-after-read", fx.docPath)) === 200);
const race = await snip("#source-reader-body", fx.snipRace);
check("竞态那一句照旧收得下(不是一句失败)", race.shown, `picked=${race.picked}`);
check("它退成了 origin=feed、没绑那一篇",
  (await favsOf(fx.snipRace)) === JSON.stringify([{ o: "feed", p: null }]),
  await favsOf(fx.snipRace));
const raceToast = await evalJs(`window.__toastText()`);
check("并且如实说了是「刚换了一版正文」才退的",
  raceToast.includes("换了一版"), raceToast);

finish();
