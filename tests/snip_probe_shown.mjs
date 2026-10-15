// 「☆ 收藏此段」存下来的,到底是不是**屏幕上那份文字**。
//
// 四处正文「收得下、点得回去」在 tests/snip_probe.mjs,「收下来之后还作不作数」
// 在 tests/snip_probe_edges.mjs。这一份只管一件事,它的形态是**当场看着全对、
// 粘出来才发现不是那段话**:
//
//   `<br>` 和段落边界在 DOM 文本节点里**一个字符都不占**,用户看到的却是分行的。
//   拿文本节点串(或 `String(range)`)当原文存,两行当场接成一行 —— 收藏内容本身
//   就丢了,而面板上那条看起来一切正常(审查实测:`Selection.toString()` 是
//   「第一行\n第二行」、`Range.toString()` 是「第一行第二行」,落库的是后者)。
//
// 所以判据一律落在**库里那条 quote 逐字等于屏幕上那份文字**,不落在「有没有
// 换行」这种模糊说法上;同时每一处都钉住**定位没跟着坏**(匹配那一头自己压平
// 一遍,坐标系按同一份名单插分隔)。**两份阅读实现各有自己的那一格**:共享层
// (`kb/static/js/snip_text.js`)在问答正文上验,独立阅读页(`kb/web/pages.py`
// 的内联副本)在它自己那一页上验。
//
// 「选完了」那三种输入方式(鼠标 / 触摸 / 键盘)也在这一份里 —— 它和保真是同一
// 件事的两头:选区结算不了,后面整条都不发生。
//
// 外壳(起浏览器、连 CDP、页内帮手)和主探针共用 ./snip_probe_lib.mjs。
//
//   HEADLESS_SHELL=<bin> node tests/snip_probe_shown.mjs <baseURL> <manifest.json>
import {
  base, boot, check, clickJump, DOC_HELPERS, docSnip, docToast, docToastClear,
  evalJs, finish, fx, highlightText, js, openFavList, send, snip, until,
} from "./snip_probe_lib.mjs";

const flat = s => String(s).replace(/\s+/g, " ");

await boot();

// ── 1. 问答:屏幕上分了行、文本节点里一个分隔符都没有的那两种 ────────────────
// `<br>` 和段落边界在 DOM 文本节点里不占字符,用户看到的却是分行的 —— 拿文本节点
// 串当原文存,两行当场接成一行,**收藏内容本身就丢了**(审查实测:
// `Selection.toString()` 是「第一行\n第二行」、`Range.toString()` 是「第一行第二
// 行」,落库的是后者)。判据落在**库里那条 quote 逐字等于屏幕上那份文字**,不落在
// 「有没有换行」这种模糊说法上;同时钉住它照旧定位得回去(匹配那一头自己压一遍)。
await evalJs(`switchTab('ask'); openConv(${fx.convId}, true); true`);
await until(`$('chat-msgs').textContent.includes(${js(fx.snipBr1)})`, 30000);
// 跨 `<br>` 的选区落在两个文本节点上,所以用跨元素那一档选法;压平坐标系里这两
// 行之间就是一个空格。
const brSnip = await snip("#chat-msgs", fx.snipBr1 + " " + fx.snipBr2, 1, "span");
check("跨过一个 <br> 的两行选得下、也收得下", brSnip.shown, `picked=${brSnip.picked}`);
const brRows = (await evalJs(`window.__favs()`))
  .filter(f => flat(f.quote) === flat(fx.snipBrSeen));
check("库里那条 quote 逐字等于屏幕上那两行(<br> 处是一个换行)",
  brRows.length === 1 && brRows[0].quote === fx.snipBrSeen,
  JSON.stringify(brRows.map(f => f.quote)));
// 反过来钉一边:**前后文照旧是压平过的匹配表示**(它们只用来定位)
check("它的前后文照旧没有换行", brRows.length === 1
  && !/[\n\t]/.test((brRows[0].prefix || "") + (brRows[0].suffix || "")),
  JSON.stringify(brRows.map(f => ({ p: f.prefix, s: f.suffix }))));
// 还有**退路自己**那一格:浏览器那条路只在"当前选区正好是这一段"时可用,离线
// 核验(`snipRenderedText`)和任何不经选区的入口走的都是 `snipFragText`。两条路
// 对同一段 DOM 必须给出同一份文字 —— 判据落在**两份文字逐字相同**,不落在"有
// 没有换行"。(只在 `<br>` 这一种上成立:段落之间还夹着一个渲染时看不见的空白
// 文本节点,那一格的差异正是浏览器优先那条路存在的理由。)
await evalJs(`window.__snipSelectSpan('#chat-msgs', ${js(fx.snipBr1 + " " + fx.snipBr2)}, 1)`);
const brTwoWays = JSON.parse(await evalJs(`(() => {
  const s = getSelection();
  if (!s || s.isCollapsed || !s.rangeCount) return JSON.stringify({ err: 'no-sel' });
  return JSON.stringify({ sel: s.toString(),
    frag: snipFragText(s.getRangeAt(0).cloneContents()) });
})()`));
check("跨 <br> 那一段:退路给的文字和浏览器给的逐字一致",
  brTwoWays.sel === fx.snipBrSeen && brTwoWays.frag === brTwoWays.sel,
  JSON.stringify(brTwoWays));

// 段落边界那一种:屏幕上空着一行,`Selection.toString()` 给两个换行
const paraSnip = await snip("#chat-msgs", fx.snipPara1 + "。 " + fx.snipPara2, 1, "span");
check("跨过一个段落边界的两段也收得下", paraSnip.shown, `picked=${paraSnip.picked}`);
const paraRows = (await evalJs(`window.__favs()`))
  .filter(f => flat(f.quote) === flat(fx.snipParaSeen));
check("库里那条 quote 逐字等于屏幕上那两段(段落边界处是两个换行)",
  paraRows.length === 1 && paraRows[0].quote === fx.snipParaSeen,
  JSON.stringify(paraRows.map(f => f.quote)));
// **保真改对了,定位不许跟着坏。** 坐标系按同一份名单插分隔,所以 quote 压平一遍
// 还是坐标系里连着的那一段 —— 收藏那一刻 `locatable` 就该是真的,回跳也落得回去。
check("收藏面板里能点到跨行那条的回跳",
  (await openFavList()) && (await clickJump("chat", fx.snipBrSeen)));
// 判据落在**落回去的是哪一处**(偏移),不落在「高亮出来的文字对不对」——
// 高亮是一个 Range,`toString()` 照旧不含元素边界那几处换行,拿它比文字只会
// 比出那个已知差异来。两头同一个压平坐标系(`__hiOffset` / `__textOffset`)。
// 回跳是异步的(切面板 + 重读对话),等高亮真出现再比
await until(`window.__hiOffset('#chat-msgs') >= 0`, 20000);
const hiAt = await evalJs(`window.__hiOffset('#chat-msgs')`);
const wantAt = await evalJs(
  `window.__textOffset('#chat-msgs', ${js(fx.snipBr1 + " " + fx.snipBr2)}, 1)`);
check("那两行在问答里被高亮出来,落点正是它那一处",
  hiAt >= 0 && hiAt === wantAt, `高亮 ${hiAt} / 期望 ${wantAt}`);

// ── 2. 独立阅读页那一份:它自带一份内联的采集 ────────────────────────────────
// 上面那一节验的是 app 那份共享实现(`snip_text.js`)。**这一页自带另一份**
// (`pages.py` 的 `fragText`/`shown`),两份各要有自己的红。`<br>` 那一种在这一页
// 上是字面文字(原始 HTML 当文字显示),所以这一头拿**段落边界**验:md 的两个段落
// 渲染成两个 `<p>`,屏幕上空着一行,DOM 文本节点里却只有各段自己的文字。
// 判据落在**库里那条 quote 逐字等于屏幕上那两段**,外加「保真改对了、定位不许
// 跟着坏」(坐标系按同一份名单插分隔,quote 压平一遍还是连着的那一段)。
await send("Page.navigate", { url: base + "/doc/"
  + fx.docPath.split("/").map(encodeURIComponent).join("/") });
await until(`!!document.getElementById('content')
  && document.getElementById('content').textContent.includes(${js(fx.pagePara1)})`, 25000);
await evalJs(DOC_HELPERS);   // 导航过来是一份新文档,页内帮手得重装
await evalJs(`CSS.highlights.delete('kb-quote');
  history.replaceState(null, '', location.pathname); true`);
// 先验这一页的「选完了」:**三种输入方式各报一次**(报告的 N2 —— 这一页原先只认
// mouseup,键盘选字和手机上长按选词全程没有 mouseup,按钮压根不露头)。判据是那颗
// 按钮露不露出来;不点它,免得多出一条收藏扰动下面那几条。
for (const [how, ev] of [
  ["键盘(松开 Shift)", "new KeyboardEvent('keyup', { key: 'Shift', bubbles: true })"],
  ["触摸(手指离开)", "new Event('touchend', { bubbles: true })"]]) {
  await evalJs(`document.getElementById('snipbtn').style.display = 'none';
    getSelection().removeAllRanges(); true`);
  const kbPick = await evalJs(`(() => {
    const host = document.getElementById('content');
    const w = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      const i = n.nodeValue.indexOf(${js(fx.pagePara1)});
      if (i < 0) continue;
      const r = document.createRange();
      r.setStart(n, i); r.setEnd(n, i + ${js(fx.pagePara1)}.length);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
      document.dispatchEvent(${ev});
      return 'ok';
    }
    return 'no-text';
  })()`);
  const kbShown = await until(
    `document.getElementById('snipbtn').style.display === ''`, 4000);
  check(`阅读页上用${how}选完也弹得出按钮`, kbPick === "ok" && kbShown,
    `picked=${kbPick} shown=${kbShown}`);
}
await evalJs(`document.getElementById('snipbtn').style.display = 'none';
  getSelection().removeAllRanges(); true`);
const pagePara = await docSnip(fx.pagePara1, fx.pagePara2);
check("阅读页上跨段那一选也收得下", pagePara.shown, `picked=${pagePara.picked}`);
const paraDocRows = JSON.parse(await evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  return JSON.stringify(d.favorites
    .filter(x => x.path === ${js(fx.docPath)} && x.quote.includes(${js(fx.pagePara2)}))
    .map(x => ({ q: x.quote, p: x.prefix, s: x.suffix })));
})()`));
check("阅读页库里那条 quote 逐字等于屏幕上那两段(段落边界两个换行)",
  paraDocRows.length === 1 && paraDocRows[0].q === fx.pageParaSeen,
  JSON.stringify(paraDocRows.map(r => r.q)));
// 反过来钉一边:**前后文照旧是压平过的匹配表示**(它们只用来定位)
check("阅读页那条的前后文不带换行",
  paraDocRows.length === 1 && !/[\n\r]/.test(paraDocRows[0].p + paraDocRows[0].s),
  JSON.stringify(paraDocRows.map(r => ({ p: r.p, s: r.s }))));
// 定位不许跟着坏:拿库里那条锚点在这一页上点回去,落在它自己那一处
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(${js(JSON.stringify(paraDocRows[0] ? {
    q: paraDocRows[0].q, p: paraDocRows[0].p, s: paraDocRows[0].s } : {}))}); true`);
const paraDocHit = await until(
  `window.__docHiNth(${js(paraDocRows[0] ? paraDocRows[0].q : "")}) >= 1`, 15000);
check("阅读页上那条跨段收藏照旧点得回去(保真没把定位弄坏)",
  paraDocHit && (await evalJs(`window.__docHiNth(${js(paraDocRows[0] ? paraDocRows[0].q : "")})`)) === 1,
  `hi=${await evalJs(`window.__docHiNth(${js(paraDocRows[0] ? paraDocRows[0].q : "")})`)} toast=${await evalJs(`(document.getElementById('kbtoast')||{}).textContent || ''`)}`);

// ── 3. 同一页上换一条找不到的锚点:上一次的高亮必须先清掉 ───────────────────
// 定位失败那几条早退(没找到、参数坏、空引文)原先直接提示完就 return —— 旧高亮
// 还亮着,屏幕上成了"提示说没找到、可有一段话亮着",而亮着的是**上一条收藏**的
// 位置(审查实测;共享层那一份一直是先清后定位)。判据落在屏幕上还有没有高亮。
// **接着上一节**:那一节刚成功定位过,旧高亮此刻正亮着。
await docToastClear();
await evalJs(`location.hash = '#kbq=' + encodeURIComponent(JSON.stringify(
  { q: '这一页上压根没有的一句话' + Date.now(), p: '', s: '' })); true`);
const goneDocToast = await docToast();
check("阅读页上换一条找不到的锚点,先把上一次的高亮清掉",
  (await highlightText()) === "(没有高亮)", await highlightText());
check("找不到那一条也如实说了没找到", goneDocToast.includes("没找到这段收藏"),
  goneDocToast);

finish();
