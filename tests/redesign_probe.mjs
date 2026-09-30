// 这一轮改版的回归:配色 token 是否真的落到页面上、正字计数的虚笔有没有算错、
// 搜索摘要去重会不会把正经摘要吞掉、各面板有没有报错、窄屏有没有横向溢出。
// CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、不连网、
// 始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/redesign_probe.mjs <baseURL>
import { spawn } from "node:child_process";

const BIN = process.env.HEADLESS_SHELL;
const base = process.argv[2];

const proc = spawn(BIN, ["--headless", "--remote-debugging-port=0", "--disable-gpu",
                         "--hide-scrollbars", "--window-size=1440,1000", "about:blank"],
                   { stdio: ["ignore", "pipe", "pipe"] });
const port = await new Promise((res, rej) => {
  let buf = "";
  proc.stderr.on("data", d => {
    buf += d;
    const m = buf.match(/ws:\/\/127\.0\.0\.1:(\d+)\//);
    if (m) res(+m[1]);
  });
  proc.on("exit", c => rej(new Error(`exit ${c}\n${buf}`)));
  setTimeout(() => rej(new Error("no devtools port")), 20000);
});

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));

let id = 0;
const pend = new Map();
let pageErrors = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails?.exception?.description
                    || m.params.exceptionDetails?.text);
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

await send("Runtime.enable");
await send("Page.enable");

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));

async function goHome(w = 1440, h = 1000) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: w < 800 });
  await send("Page.navigate", { url: base + "/" });
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (await evalJs(`document.querySelectorAll('button.gcard').length > 8`)) return true;
    await sleep(400);
  }
  return false;
}

pageErrors = [];
check("图鉴能加载出卡片", await goHome());

// ── 1. 配色 token 真的落到页面上(不是只改了 :root 没人用) ──
const palette = await evalJs(`(() => {
  const cs = getComputedStyle(document.documentElement);
  const g = n => cs.getPropertyValue(n).trim();
  const body = getComputedStyle(document.body).backgroundColor;
  const on = document.querySelector('.tabs button.on');
  return { brand: g('--brand'), paper: g('--paper'), ink: g('--ink'),
           body, navOn: on && getComputedStyle(on).color,
           display: g('--font-display') };
})()`);
check("重点色是深松绿 #17604F", palette.brand.toUpperCase() === "#17604F", palette.brand);
check("页面底色是瓷白 #F3F5F4", palette.paper.toUpperCase() === "#F3F5F4", palette.paper);
check("body 真的用上了瓷白", palette.body === "rgb(243, 245, 244)", palette.body);
check("展示体是宋体", /Songti|STSong|Serif/i.test(palette.display), palette.display);
check("全站没有残留靛蓝 #3F43B9", !JSON.stringify(palette).toUpperCase().includes("3F43B9"));

// ── 2. 类型色真的互相拉开(上一版 project 和重点色撞成一个) ──
const kinds = await evalJs(`(() => {
  const out = {};
  for (const k of ['project','product','model','research','org','other']) {
    const el = document.createElement('span');
    el.className = 'gmark k-' + k; document.body.appendChild(el);
    out[k] = getComputedStyle(el).color; el.remove();
  }
  return out;
})()`);
check("六个类型色两两不同", new Set(Object.values(kinds)).size === 6, JSON.stringify(kinds));
check("开源项目=品牌松绿", kinds.project === "rgb(23, 96, 79)", kinds.project);

// ── 3. 正字计数:实笔 + 虚笔恒为 5,实笔数等于 n(封顶 5) ──
const tally = await evalJs(`(() => {
  const out = [];
  for (const n of [1,2,3,4,5]) {
    const d = document.createElement('div');
    d.innerHTML = tallySvg(n, 13); document.body.appendChild(d);
    const svg = d.querySelector('svg');
    out.push({ n, solid: svg.querySelectorAll('line:not(.ghost)').length,
               ghost: svg.querySelectorAll('line.ghost').length });
    d.remove();
  }
  return out;
})()`);
for (const t of tally) {
  check(`正字 n=${t.n}:实笔${t.solid}+虚笔${t.ghost}=5`, t.solid + t.ghost === 5, JSON.stringify(t));
  check(`正字 n=${t.n}:实笔数正确`, t.solid === t.n, JSON.stringify(t));
}

// ── 4. 搜索摘要去重:标题含摘要才吞,不能把正经摘要一起吞了 ──
const dedup = await evalJs(`(() => {
  const same = { title: 'foo:用于集中配置的命令行工具', summary: '用于集中配置的命令行工具' };
  const diff = { title: '某公众号的一篇长文', summary: '这是一段和标题完全不同的摘要' };
  return { same: dupSummary(same), diff: dupSummary(diff),
           empty: dupSummary({ title: 'x' }), nullish: dupSummary({}) };
})()`);
check("标题已含摘要 → 不再重复渲染", dedup.same === true);
check("摘要和标题不同 → 照常渲染", dedup.diff === false);
check("没有摘要 → 不渲染空行", dedup.empty === true);
check("字段缺失不抛异常", dedup.nullish === true);

// ── 4b. 下拉框的自绘箭头没有被 background 简写清掉 ──
// `background: transparent` 这种简写会连 background-image 一起清,下拉框就
// 退化成一段看不出可点的纯文字;各处覆盖只许改 background-color。
await evalJs(`switchTab('gallery')`);
await sleep(500);
const arrows = await evalJs(`(() => {
  const out = {};
  for (const sel of ['g-src','g-sort','ask-model','f-source']) {
    const el = document.getElementById(sel);
    out[sel] = el ? getComputedStyle(el).backgroundImage !== 'none' : 'missing';
  }
  return out;
})()`);
for (const [k, v] of Object.entries(arrows))
  check(`下拉框 ${k} 保留箭头`, v === true, String(v));

// ── 4c. 箭头一律是 icons.js 那份内联 SVG,页面上不许再出现箭头字符 ──
// ↗ ↑ ↓ → ▾ ▴ 这些字在 PingFang SC 里细长歪斜,缺字还会回退到系统 emoji 字体,
// 同一行里混着用就参差不齐。
const glyphs = await evalJs(`(() => {
  const bad = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    if (!/[\\u2191\\u2193\\u2192\\u2197\\u25b4\\u25be\\u203a]/.test(n.nodeValue)) continue;
    if (!n.parentElement?.offsetParent && n.parentElement?.tagName !== 'BODY') continue;
    bad.push(n.nodeValue.trim().slice(0, 30));
  }
  const ia = [...document.querySelectorAll('.ia')];
  return { bad, ia: ia.length, allSvg: ia.every(e => e.tagName.toLowerCase() === 'svg'),
           unrendered: document.querySelectorAll('[data-arrow],[data-icon]').length };
})()`);
check("页面上没有残留箭头字符", glyphs.bad.length === 0, glyphs.bad.join(" | "));
check("行内箭头都渲染成了 SVG", glyphs.ia > 0 && glyphs.allSvg, `${glyphs.ia} 个`);
check("没有未替换的 data-icon/data-arrow", glyphs.unrendered === 0, String(glyphs.unrendered));

// ── 4d. 齿轮真的是 8 齿等分的旋转对称形状 ──
// 原来那条 settings 路径是手画折线,齿不等分、首尾对不上,看着就像缺了一块。
// 判据:沿路径采样,把「每个角度上的最大半径」按 1° 分箱,r(θ) 必须以 45° 为周期
// —— 少一齿、齿距不匀、某一段塌进去,这条当场就不成立。
const gear = await evalJs(`(() => {
  const host = document.createElement('div');
  host.style.cssText = 'position:absolute;left:-9999px';
  host.innerHTML = icon('settings');
  document.body.appendChild(host);
  const svg = host.querySelector('svg');
  const path = svg.querySelector('path'), circle = svg.querySelector('circle');
  const L = path.getTotalLength(), bins = new Array(360).fill(0);
  for (let i = 0; i < 4000; i++) {
    const p = path.getPointAtLength(L * i / 4000);
    const dx = p.x - 12, dy = p.y - 12;
    const a = ((Math.atan2(dy, dx) * 180 / Math.PI) % 360 + 360) % 360 | 0;
    bins[a] = Math.max(bins[a], Math.hypot(dx, dy));
  }
  let worst = 0;
  for (let i = 0; i < 360; i++) worst = Math.max(worst, Math.abs(bins[i] - bins[(i + 45) % 360]));
  const rs = bins.filter(Boolean);
  host.remove();
  return { worst, rmin: Math.min(...rs), rmax: Math.max(...rs), filled: rs.length,
           hasHub: !!circle, closed: path.getAttribute('d').trim().endsWith('Z') };
})()`);
check("齿轮 8 齿旋转对称(r(θ) 以 45° 为周期)", gear.worst < 0.25, `最大偏差 ${gear.worst?.toFixed(3)}`);
check("齿轮每个角度都有轮廓(没有缺口)", gear.filled === 360, `${gear.filled}/360`);
check("齿轮有齿(齿顶明显高于齿根)", gear.rmax - gear.rmin > 1.5,
      `R=${gear.rmax?.toFixed(2)} r=${gear.rmin?.toFixed(2)}`);
check("齿轮有轴心圆且路径闭合", gear.hasHub && gear.closed, JSON.stringify(gear));

// ── 4e. 图鉴列表视图:摘要单行、不留卡片视图的占位,行才能紧凑 ──
// `.list-view .gsum` 那条覆盖一度被误写成 `.list-view .list-view .gsum`,选择器
// 匹配不到任何元素 —— 页面不报错、不溢出,只是每行白白顶着卡片视图的两行摘要和
// 46px 占位,列表看着跟卡片一样松。所以按算出来的样式和真实行高各钉一道。
await evalJs(`setGalleryView('list')`);
await sleep(600);
const listView = await evalJs(`(() => {
  const grid = document.getElementById('g-grid');
  const cards = [...grid.querySelectorAll('button.gcard')];
  const sums = cards.map(c => c.querySelector('.gsum')).filter(s => s && s.textContent.trim());
  const cs = sums.map(s => getComputedStyle(s));
  const v = (f) => [...new Set(cs.map(f))];
  return { isList: grid.classList.contains('list-view'), n: sums.length,
           clamp: v(c => c.getPropertyValue('-webkit-line-clamp')),
           minH: v(c => c.minHeight), marginB: v(c => c.marginBottom),
           line: Math.max(...cs.map(c => parseFloat(c.lineHeight))),
           sumH: Math.max(...sums.map(s => s.getBoundingClientRect().height)),
           // 行高取中位数:带「全部出自软广?」角标的那几条会多占一行,是内容不是回归
           cardH: cards.map(c => c.getBoundingClientRect().height)
                       .sort((a, b) => a - b)[cards.length >> 1] };
})()`);
check("列表视图切得过去且有摘要", listView.isList && listView.n > 4, JSON.stringify(listView));
check("列表摘要单行截断", JSON.stringify(listView.clamp) === '["1"]', JSON.stringify(listView.clamp));
check("列表摘要不留卡片视图的占位",
      JSON.stringify(listView.minH) === '["0px"]' && JSON.stringify(listView.marginB) === '["0px"]',
      `min-height ${listView.minH} / margin-bottom ${listView.marginB}`);
check("最长的摘要也没换行", listView.sumH <= listView.line + 1,
      `${listView.sumH?.toFixed(1)}px > 一行 ${listView.line}px`);
// 实测 83.3px;选择器失效时是 116.4px(多一行摘要 23px + 卡片视图那 10px 下边距)
check("列表行紧凑(不到 96px)", listView.cardH < 96, `中位行高 ${listView.cardH?.toFixed(1)}px`);
await evalJs(`setGalleryView('grid')`);
await sleep(300);

// ── 5. 五个面板逐个切过去,不许报错、不许出横向滚动条 ──
for (const tab of ["gallery", "search", "ask", "fav", "pipe"]) {
  await evalJs(`switchTab('${tab}')`);
  await sleep(700);
  const over = await evalJs(`document.documentElement.scrollWidth - document.documentElement.clientWidth`);
  check(`${tab} 面板无横向溢出`, over <= 0, `溢出 ${over}px`);
}

// ── 6. 窄屏 375 / 390:图鉴与导航都不许横向溢出 ──
for (const w of [375, 390]) {
  await goHome(w, 780);
  const over = await evalJs(`document.documentElement.scrollWidth - document.documentElement.clientWidth`);
  check(`${w}px 图鉴无横向溢出`, over <= 0, `溢出 ${over}px`);
}

// ── 7. 详情抽屉能打开,标题用上展示体 ──
await goHome();
await evalJs(`document.querySelector('button.gcard').click()`);
await sleep(2200);
const drawer = await evalJs(`(() => {
  const n = document.querySelector('#tool-drawer .name');
  if (!n) return null;
  const cs = getComputedStyle(n);
  return { text: n.textContent.slice(0,40), font: cs.fontFamily, weight: cs.fontWeight };
})()`);
check("详情抽屉打得开", !!drawer, JSON.stringify(drawer));
check("抽屉工具名用展示体 900", drawer && /Songti|STSong|serif/i.test(drawer.font) && drawer.weight === "900",
      JSON.stringify(drawer));


// ── 8. 焦点只有一条环 ──
// 输入框外面套一圈 outline、自己再变一次色,就是两道「选中」痕迹:「API Key 设置」
// 那几个框上下挨着时,选中一个看着像选中了一整块。现在统一成「那条边变成 2px 绿」
// —— border 1px + 紧贴内沿的 inset 1px。复选框太小,描边画进去等于没有提示,
// 它们保留外圈 outline(这是无障碍底线,别一起删掉)。
await send("Emulation.setFocusEmulationEnabled", { enabled: true });
await goHome();
// 先来一次真键盘,让 :focus-visible 进入键盘模态(下拉和复选框只认这个)
await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
await sleep(200);
const focusRings = await evalJs(`(async () => {
  const G = '23, 96, 79';
  const read = el => { const cs = getComputedStyle(el); return {
    outline: cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0 && cs.outlineColor.includes(G),
    inset: cs.boxShadow.includes(G) && cs.boxShadow.includes('inset'),
    glow: cs.boxShadow.includes(G) && !cs.boxShadow.includes('inset'),
    border: parseFloat(cs.borderTopWidth) > 0 && cs.borderTopColor.includes(G) }; };
  const focus = async el => { el.focus(); await new Promise(r => setTimeout(r, 320)); };
  const out = {};
  switchTab('search');
  await new Promise(r => setTimeout(r, 400));
  const sel = document.getElementById('f-topic');
  await focus(sel); out.select = read(sel);
  const q = document.getElementById('q');          // 边框画在 .searchbar 上
  await focus(q); out.qSelf = read(q); out.qBar = read(q.closest('.searchbar'));
  switchTab('gallery');
  await new Promise(r => setTimeout(r, 500));
  const gq = document.getElementById('g-q');       // 边框画在 .gallery-search 上
  await focus(gq); out.gqSelf = read(gq); out.gqBox = read(gq.closest('.gallery-search'));
  const cb = document.createElement('input');
  cb.type = 'checkbox'; document.body.appendChild(cb);
  await focus(cb); out.checkbox = read(cb); cb.remove();
  return out;
})()`);
check("下拉聚焦不套外圈 outline", focusRings.select && !focusRings.select.outline, JSON.stringify(focusRings.select));
check("下拉聚焦是 border + 内描边", focusRings.select?.border && focusRings.select?.inset, JSON.stringify(focusRings.select));
check("容器画边的搜索框自己不再描一圈", !focusRings.qSelf?.inset && !focusRings.gqSelf?.inset,
      JSON.stringify({ q: focusRings.qSelf, gq: focusRings.gqSelf }));
check("这两个框的焦点由容器画成同一条边", focusRings.qBar?.border && focusRings.qBar?.inset
      && focusRings.gqBox?.border && focusRings.gqBox?.inset, JSON.stringify({ bar: focusRings.qBar, box: focusRings.gqBox }));
check("容器不再外发光", !focusRings.qBar?.glow && !focusRings.gqBox?.glow, JSON.stringify(focusRings.gqBox));
check("复选框保留外圈焦点环", focusRings.checkbox?.outline, JSON.stringify(focusRings.checkbox));

check("整轮没有页面报错", pageErrors.length === 0, pageErrors.slice(0, 4).join(" | "));

console.log(`\n通过 ${oks.length} 项`);
if (fails.length) {
  console.log(`\n失败 ${fails.length} 项:`);
  for (const f of fails) console.log("  ✗ " + f);
} else {
  console.log("全部通过");
}
ws.close();
proc.kill();
process.exit(fails.length ? 1 : 0);
