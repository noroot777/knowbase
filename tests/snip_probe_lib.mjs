// 「收藏此段」那几份探针共用的外壳:起无头浏览器、连 CDP、在页面里装一套帮手。
//
// 抽出来是因为判据分了两份(`snip_probe.mjs` 验四处正文收得下点得回去,
// `snip_probe_edges.mjs` 验几类反例),而这段外壳**一个字都不许有两份** ——
// 两份各自演化的话,"同一套选中回路"就不再是同一套了,而那正是这条链唯一的
// 共同前提。改这里要想着两份探针都在用。
//
// CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、不连网、
// 始终无头、临时 profile),口径同 tests/probe_shell.mjs。
//
//   用法:import { boot, check, snip, finish } from "./snip_probe_lib.mjs";
//   命令行约定 <baseURL> <manifest.json>,两份探针一样。
import { readFileSync, writeSync } from "node:fs";
import { launchShell } from "./probe_shell.mjs";

const base = process.argv[2];
const fx = JSON.parse(readFileSync(process.argv[3], "utf8"));

const { proc, port } = await launchShell(["--window-size=1440,1000"]);

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));

let id = 0;
const pend = new Map();
const pageErrors = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails.exception?.description
                    || m.params.exceptionDetails.text || "");
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    pageErrors.push(m.params.args.map(a => a.value ?? a.description ?? "").join(" "));
});
const send = (method, params = {}) => {
  const mid = ++id;
  return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
};
const evalJs = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (expr, ms = 15000) => {
  const dl = Date.now() + ms;
  while (Date.now() < dl) { if (await evalJs(expr)) return true; await sleep(120); }
  return false;
};
await send("Runtime.enable");
await send("Page.enable");

const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok: !!ok, detail: String(detail) });
const js = s => JSON.stringify(s);

// 在页面里装一套帮手:真选中一段文字(构造 Selection + 派 mouseup,走的就是
// snip.js 那条事件回路),以及"按 origin 取库里那条收藏"。
//
// **整段是模板字符串**,所以里面的正则要双写反斜杠:模板里 \s 就是字面 s、
// \/ 就是 /(于是一个 /\/api\// 会被读成注释,整段当场语法错)。
const HELPERS = `
window.__snipSelect = (sel, text, nth) => {
  const host = document.querySelector(sel);
  if (!host) return 'no-host';
  let left = (nth || 1) - 1;          // 跳过前面 nth-1 次命中
  const w = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    // **同一个 text node 里也要数**:订阅阅读器按 textContent 摊开正文,整篇
    // 就是一个 text node —— 按"每个节点只看第一处"数的话,nth=2 永远落空。
    for (let i = n.nodeValue.indexOf(text); i >= 0;
         i = n.nodeValue.indexOf(text, i + 1)) {
      if (left > 0) { left--; continue; }
      const r = document.createRange();
      r.setStart(n, i); r.setEnd(n, i + text.length);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
      // **先滚到眼前再派 mouseup**:showSnipBtn 会拿选区和容器的矩形比一次
      // (容器自己滚出视口时不弹,免得那颗 fixed 按钮飘在别的面板上面),而真
      // 用户只可能选中看得见的字。不滚的话长正文靠后那几段一律弹不出按钮。
      // 这段注释在模板字符串里,**不许出现反引号** —— 它会把整段当场截断。
      if (n.parentElement) n.parentElement.scrollIntoView({ block: 'center' });
      document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      return 'ok';
    }
  }
  return 'no-text';
};
// **跨元素选**:正文里夹着 <strong> 一类元素时,一句话落在好几个 text node 上,
// 上面那两种选法(都在同一个节点里找)一律 'no-text'。按**压平坐标系**量
// (snipTextIndex + snipOffsetRange 就是采集那一侧用的那两个函数;这段注释在模板
// 字符串里,不许出现反引号),于是选出来的 Range 和真人拖过那一句得到的是同一个。
window.__snipSelectSpan = (sel, text, nth) => {
  const host = document.querySelector(sel);
  if (!host) return 'no-host';
  const idx = snipTextIndex(host);
  let at = -1;
  for (let k = 0; k < (nth || 1); k++) {
    at = idx.text.indexOf(text, at + 1);
    if (at < 0) return 'no-text';
  }
  const r = snipOffsetRange(idx, at, at + text.length);
  if (!r) return 'no-range';
  const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  const el = r.startContainer.parentElement;
  if (el) el.scrollIntoView({ block: 'center' });
  document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  return 'ok';
};
// 高亮那一段在容器全文里的偏移。判据要落在"落回去的是哪一处",而不是
// "文字对不对" —— 同一句话出现两次时后者两处都为真。
window.__hiOffset = (sel) => {
  const h = window.CSS && CSS.highlights && CSS.highlights.get('kb-quote');
  if (!h) return -1;
  const r = [...h][0];
  if (!r) return -1;
  // 用的就是采集那一侧那个函数(压平坐标系),所以和 __textOffset 对得上
  const idx = snipTextIndex(document.querySelector(sel));
  return snipOffsetAt(idx, r.startContainer, r.startOffset);
};
// 容器全文里第 nth 次出现 text 的偏移(算期望值用,和上面同一个坐标系)
window.__textOffset = (sel, text, nth) => {
  const idx = snipTextIndex(document.querySelector(sel));
  let at = -1;
  for (let k = 0; k < (nth || 1); k++) {
    at = idx.text.indexOf(text, at + 1);
    if (at < 0) return -1;
  }
  return at;
};
// 连着**首尾空白**一起选的那种选法(审查复现用的正是它):引文两头的空白必须
// 挪进前后文,否则后端只 strip 引文、前后文照旧,整段锚点就拼不回原文了。
// 回选中的那段文字,好让失败详情说得出到底选到了什么。
window.__snipSelectLoose = (sel, text, nth) => {
  const host = document.querySelector(sel);
  if (!host) return 'no-host';
  let left = (nth || 1) - 1;
  const w = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
  const nodes = [];
  for (let n = w.nextNode(); n; n = w.nextNode()) nodes.push(n);
  for (let k = 0; k < nodes.length; k++) {
    const i = nodes[k].nodeValue.indexOf(text);
    if (i < 0) continue;
    if (left > 0) { left--; continue; }
    const r = document.createRange();
    r.setStart(nodes[k], i); r.setEnd(nodes[k], i + text.length);
    if (i === 0 && k > 0 && /\\s$/.test(nodes[k - 1].nodeValue))
      r.setStart(nodes[k - 1], nodes[k - 1].nodeValue.length - 1);
    const tail = i + text.length;
    if (tail === nodes[k].nodeValue.length && k + 1 < nodes.length
        && /^\\s/.test(nodes[k + 1].nodeValue))
      r.setEnd(nodes[k + 1], 1);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    if (nodes[k].parentElement) nodes[k].parentElement.scrollIntoView({ block: 'center' });
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    return r.toString();
  }
  return 'no-text';
};
// 把"读某一份日报"那次请求按住 ms 毫秒(只按它,别的接口照旧)。换日那一刻
// 屏幕上还是旧那一份正文,这正是出处最容易记错的时刻。
window.__delayDaily = (ms) => {
  const orig = window.pipeJson;
  window.__restoreDaily = () => { window.pipeJson = orig; };
  window.pipeJson = async (path, ...rest) => {
    if (/\\/api\\/daily\\/reports\\//.test(String(path))) await new Promise(r => setTimeout(r, ms));
    return orig(path, ...rest);
  };
  return true;
};
// 按钮没弹出来时,说清是哪一关没过(选区空了?这一段不认出处?矩形在容器外?)
// —— 不然失败详情只有一句"按钮没出现",而那一步有四道判断。
window.__snipDiag = (sel) => {
  const s = getSelection();
  const r = s && s.rangeCount ? s.getRangeAt(0) : null;
  const zone = document.querySelector(sel);
  const rb = r && r.getBoundingClientRect(), zb = zone && zone.getBoundingClientRect();
  let src = 'no-zone';
  try {
    const z = snipZones.find(x => x.el === zone);
    src = z && r ? JSON.stringify(z.describe(r)) : 'no-zone';
  } catch (e) { src = 'describe 抛了: ' + e.message; }
  return JSON.stringify({
    picked: s ? s.toString().slice(0, 24) : null, collapsed: !s || s.isCollapsed,
    rect: rb && { top: Math.round(rb.top), bottom: Math.round(rb.bottom) },
    zone: zb && { top: Math.round(zb.top), bottom: Math.round(zb.bottom) },
    src,
  });
};
window.__toastText = () => (document.getElementById('toast-host') || {}).textContent || '';
window.__snipBtnShown = () => {
  const b = document.getElementById('snipbtn');
  return !!b && !b.hidden;
};
window.__favs = async (status) => {
  const d = await fetch('/api/favorites?status=' + (status || 'inbox')).then(r => r.json());
  return d.favorites;
};
window.__favBy = async (origin) => (await window.__favs()).filter(f => f.origin === origin);
true`;

/** 收藏面板:切过去、把筛选收回默认,等它把这些收藏画成卡片(**幂等**,可以反复
 *  调)。筛选要自己收干净 —— 搜索框、来源下拉和类型筛选都是页面上的持久状态,
 *  上一格留下的一个词会把这一格要点的那张卡过滤掉,红出来的样子是「面板里没有
 *  这条收藏」。
 *
 *  **等的是这一轮加载自己 resolve,不是屏幕上出现了一颗回跳按钮。** 列表非空时
 *  那一轮不清屏(见 `fav.js` 的 `loadFavPanel`:只有 `favItems` 空才铺占位),
 *  所以「按钮 ≥ 1」在旧卡片上立刻就成立 —— 刚收下来那条还没进 DOM,随后那次
 *  `clickJump` 只会红在「点不到」,真因是这一步压根没等。 */
async function openFavList() {
  await evalJs(`(async () => {
    switchTab('fav');
    favView = 'all'; favFilter = 'all';
    $('fav-search').value = ''; $('fav-source').value = '';
    await loadFavPanel();
    return true;
  })()`);
  return until(`$('fav-list').querySelectorAll('.fav-jump').length >= 1`, 15000);
}

/** 收藏面板里**那一条**收藏的阅读页链接(`fav.js` 现拼的那条带 `#kbq=` 的 href,
 *  不是探针另拼一份)。回 href,没找到回 `''`。
 *
 *  **按 `#kbq=` 里的 `q` 精确比,不许拿整条 href 做 includes。** 锚点里还带着
 *  prefix/suffix,而相邻两段话互为彼此的前后文 —— 用 includes 的话"前一段"那条
 *  收藏会先被另一条的前文匹配走,随后那一页高亮的是另一句,红出来的样子是
 *  「回跳落点不对」,真因在挑链接这一步(实测:订阅列表那一节挑到了下一段那条)。 */
async function favDocHref(quote, suffix) {
  return evalJs(`(() => {
    const want = ${js(quote)}, suf = ${js(suffix === undefined ? null : suffix)};
    const a = [...document.querySelectorAll('#fav-list a[data-doc]')].find(x => {
      const h = x.getAttribute('href') || '';
      const at = h.indexOf('#kbq=');
      if (at < 0) return false;
      try {
        const j = JSON.parse(decodeURIComponent(h.slice(at + 5)));
        return j.q === want && (suf === null || j.s === suf);
      } catch { return false; }
    });
    return a ? a.getAttribute('href') : '';
  })()`);
}

/** 独立阅读页那一头的帮手。**那一页的采集和定位是另一份内联实现**(闭包里,
 *  全局拿不到),所以不能复用 app 那边的 `snipTextIndex` —— 这里只按 Range 量
 *  「高亮之前这段话已经出现过几次」,不碰它的内部函数。 */
const DOC_HELPERS = `
window.__docHiNth = (quote) => {
  const h = window.CSS && CSS.highlights && CSS.highlights.get('kb-quote');
  if (!h) return -1;
  const r = [...h][0];
  if (!r) return -1;
  const content = document.getElementById('content');
  const pre = document.createRange();
  pre.setStart(content, 0);
  pre.setEnd(r.startContainer, r.startOffset);
  const before = String(pre).replace(/\\s+/g, ' ');
  const q = String(quote).replace(/\\s+/g, ' ');
  let n = 0, at = -1;
  while ((at = before.indexOf(q, at + 1)) >= 0) n++;
  return n + 1;            // 高亮落在第几处
};
true`;

/** 在**独立阅读页**上真选中一段、真点那颗按钮,回它弹出来没、以及页面上那句
 *  提示。那一页的按钮是个 div、靠行内 `style.display` 藏(app 那边是
 *  `hidden` 属性),提示落在它自己的 `#kbtoast` 里 —— 所以不能复用外壳里那套
 *  `__snipBtnShown`/`__toastText`:它们按 app 的形状读,在这一页会一律为真。*/
async function docSnip(text, tail = "") {
  await docToastClear();
  // `tail` 给了就是**跨元素选**:从含 text 的那个文本节点起、到后面含 tail 的那个
  // 节点止(段落边界那一类分隔落在两个 <p> 之间,一个节点里找不着)。
  const picked = await evalJs(`(() => {
    const host = document.getElementById('content');
    const w = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
    const tail = ${js(tail)};
    let head = null, at = 0;
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if (!head) {
        const i = n.nodeValue.indexOf(${js(text)});
        if (i < 0) continue;
        head = n; at = i;
        if (tail) continue;
      } else {
        const j = n.nodeValue.indexOf(tail);
        if (j < 0) continue;
        const r2 = document.createRange();
        r2.setStart(head, at); r2.setEnd(n, j + tail.length);
        const s2 = getSelection(); s2.removeAllRanges(); s2.addRange(r2);
        if (n.parentElement) n.parentElement.scrollIntoView({ block: 'center' });
        document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        return 'ok';
      }
      const r = document.createRange();
      r.setStart(n, at); r.setEnd(n, at + ${js(text)}.length);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
      if (n.parentElement) n.parentElement.scrollIntoView({ block: 'center' });
      document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      return 'ok';
    }
    return 'no-text';
  })()`);
  if (picked !== "ok") return { picked, shown: false, toast: "" };
  const shown = await until(
    `document.getElementById('snipbtn').style.display === ''`, 5000);
  if (!shown) return { picked, shown: false, toast: "" };
  await evalJs(`document.getElementById('snipbtn').click(); true`);
  return { picked, shown: true, toast: await docToast() };
}

/** 把阅读页那条提示清空(见 docSnip:它自己挂 2.2 秒,不清就读到上一句)。*/
function docToastClear() {
  return evalJs(`(() => { const t = document.getElementById('kbtoast');
    t.textContent = ''; t.style.display = 'none'; return true; })()`);
}

/** 等阅读页那条提示重新出现,回它的文字(清空之后才有意义)。*/
async function docToast(ms = 10000) {
  await until(`(document.getElementById('kbtoast').textContent || '') !== ''`, ms);
  return evalJs(`document.getElementById('kbtoast').textContent`);
}

/** 点收藏面板里那条回跳按钮(按 origin + 引文定位)。没有那颗按钮回 false,不抛。
 *
 * **引文必须一起比**:同一份日报里收了两句话,它们 origin 和 origin_ref 都一样、
 * 落在同一张卡上,只按 origin 找到的是"排在前面那一条" —— 而排序是
 * `created_at DESC, id DESC`,同一秒收的两条只靠 id 分先后。拿它当判据就成了
 * "碰巧点中想点的那一条"(实测:一轮里点中的是重复句那条,高亮对不上而真因
 * 在选择器上)。
 *
 * `suffix` 是第三道:同一句话可以存下**只有后文不同**的两条(位置证据相不相交
 * 那一组就是这么摆的 —— 同一个 q、同一个 p,一条的 s 指向另一处)。
 */
function clickJump(origin, quote = '', suffix = '') {
  return evalJs(`(() => {
    const btn = [...$('fav-list').querySelectorAll('.fav-jump')].find(b => {
      try {
        const j = JSON.parse(decodeURIComponent(b.dataset.jump));
        return j.origin === ${js(origin)} && (!${js(quote)} || j.q === ${js(quote)})
          && (!${js(suffix)} || j.s === ${js(suffix)});
      } catch { return false; }
    });
    if (!btn) return false;
    btn.click();
    return true;
  })()`);
}

/** 现在高亮着的那段文字(没有高亮回一句话,好让失败详情说得清)。
 *  **按压平坐标系比**:高亮那个 Range 覆盖的是 DOM 里的原文,跨段落时中间是
 *  真换行,而锚点和判据里的期望值都是压平过的(同 `snipCollapse`)。*/
function highlightText() {
  return evalJs(`(() => {
    const h = CSS.highlights && CSS.highlights.get('kb-quote');
    if (!h) return '(没有高亮)';
    return [...h].map(r => String(r).replace(/\\s+/g, ' ')).join('|');
  })()`);
}

/** 等到高亮变成 want(或超时)。回最后读到的那一份,交给调用方断言。*/
async function snipHighlight(want, ms = 20000) {
  const dl = Date.now() + ms;
  let last = await highlightText();
  while (last !== want && Date.now() < dl) { await sleep(150); last = await highlightText(); }
  return last;
}

/** 选中 → 等按钮 → 点 → 等它收完(按钮文字复位 + 隐藏)。回按钮有没有弹出来。
 *
 *  **选区自己空掉时重选一次**:上一节那次回跳会重渲染同一个容器(innerHTML 一换,
 *  选中的那几个 text node 就不在了,选区当场折叠),而那跟"这一段能不能收藏"
 *  没有关系。只重试这一种 —— 选区还在而按钮没弹,是判据真的不成立,照旧记红
 *  并把四道判断的现场写进详情。 */
async function snip(sel, text, nth = 1, how = false) {
  // how:false = 严选(同一个 text node 里一字不差)| true = 连首尾空白一起选
  //     | 'span' = 跨元素选(正文里夹着 <strong> 一类元素时用它)
  const fn = how === "span" ? "__snipSelectSpan"
    : how ? "__snipSelectLoose" : "__snipSelect";
  for (let attempt = 1; ; attempt++) {
    const got = await evalJs(`window.${fn}(${js(sel)}, ${js(text)}, ${nth})`);
    // 松选法回的是选中的那段文字(带首尾空白),另两种回 'ok'
    if (how === true ? !String(got).includes(text) : got !== "ok")
      return { picked: got, shown: false };
    if (await until(`window.__snipBtnShown()`, 3000)) {
      await evalJs(`document.getElementById('snipbtn').click(); true`);
      await until(`!window.__snipBtnShown()`, 8000);
      return { picked: got, shown: true };
    }
    const diag = await evalJs(`window.__snipDiag(${js(sel)})`);
    let collapsed = false;
    try { collapsed = JSON.parse(diag).collapsed; } catch {}
    if (collapsed && attempt < 3) { await sleep(600); continue; }
    return { picked: `${got} / 第 ${attempt} 次 / ${diag}`, shown: false };
  }
}


/** 打开首页、等 app 起来、装好页内帮手。两份探针的第一句都是 `await boot()`。 */
export async function boot() {
  await send("Page.navigate", { url: base + "/" });
  await until(`typeof switchTab === 'function' && typeof attachSnip === 'function'`, 25000);
  // **等首次地址栏定位跑完再驱动标签页。** `restoreWorkspaceLocation()` 在
  // `initWorkspace` 收尾时才读 hash,而图鉴那几个接口和它并排起跑 —— 抢在它前面
  // `switchTab('daily')` 的话,随后那一次定位会把面板切回 `#gallery`,而探针已经
  // 在往日报正文上选字了(审查实测:首跑 14 条判据红、独立重跑全绿,时序候选正是
  // 这一处)。`workspaceLocated` 是 workspace.js 的模块级 let,脚本还没执行到那里
  // 时读它会抛 —— 所以包一层 try。
  await until(`(() => { try { return !!workspaceLocated; } catch { return false; } })()`, 30000);
  await evalJs(HELPERS);

}

/** 把提示区清空再做下一件事。**`__toastText()` 读的是整个 `#toast-host` 的
 *  `textContent`** —— 一轮里弹过的每一句都还摊在里面,所以「这一条**没有**说什么」
 *  那类判据必须先清:不清的话读到的是上一条留下的话,两份实现各自的红也分不开
 *  (实测:只改问答面板那一句文案,图鉴抽屉那条判据也跟着红)。*/
export const appToastClear = () => evalJs(
  `(() => { const h = document.getElementById('toast-host');
     if (h) h.innerHTML = ''; return true; })()`);

/** 收尾:把结果写到 stdout(整份 JSON 一次写完,调用方整段解析)、关浏览器。 */
export function finish() {
  writeSync(1, JSON.stringify({ results, pageErrors }, null, 1) + "\n");
  proc.kill();
  ws.close();
  process.exit(0);
}

export { base, fx, send, evalJs, sleep, until, check, results, pageErrors, js,
         clickJump, highlightText, snipHighlight, snip,
         openFavList, favDocHref, DOC_HELPERS, docSnip, docToastClear, docToast };
