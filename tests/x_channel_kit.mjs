// 这份探针的**外壳**:起无头浏览器、CDP 那几个原语、判据工具,以及把页面摆到
// 「大V观点 → 订阅管理」那一档(下面每一条 `shown(...)` 都押在这一步上)。
// 场景按职责分在两份里:归属那一族在 `x_channel_race.mjs`,其余在
// `x_channel_probe.mjs`;**入口只有后者**,它 import 这一份时外壳就起来了。
// 单文件 700 行上限(CLAUDE.md),拆开的理由同 mock 那一份。
import { launchShell } from "./probe_shell.mjs";
import { CHANNELS, NEXT_STEP, posted, saved, used, server, base, mock }
  from "./x_channel_mock.mjs";

// ── 无头浏览器 ───────────────────────────────────────────────────────────────
const { proc, port } = await launchShell(["--window-size=1440,1000"]);
const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let msgId = 0; const pend = new Map(); const pageErrors = [];
ws.addEventListener("message", ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pend.has(msg.id)) { pend.get(msg.id)(msg); pend.delete(msg.id); return; }
  if (msg.method === "Runtime.exceptionThrown")
    pageErrors.push(msg.params.exceptionDetails?.exception?.description || msg.params.exceptionDetails?.text);
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error")
    pageErrors.push(msg.params.args.map(a => a.value ?? a.description ?? "").join(" "));
});
const send = (method, params = {}) => { const id = ++msgId;
  return new Promise(res => { pend.set(id, res); ws.send(JSON.stringify({ id, method, params })); }); };
const evalJs = async expression => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (expr, ms = 10000) => { const dl = Date.now() + ms;
  while (Date.now() < dl) { if (await evalJs(expr)) return true; await sleep(100); } return false; };
// 真鼠标点在元素中心:直接调 .click() 绕开了"这颗按钮在页面上点不点得到"。
const clickAt = async sel => {
  const box = await evalJs(`(() => { const e = document.querySelector('${sel}');
    if (!e || !e.getClientRects().length) return null; const r = e.getBoundingClientRect();
    return JSON.stringify({ x: r.left + r.width / 2, y: r.top + r.height / 2 }); })()`);
  if (!box) return false;
  const { x, y } = JSON.parse(box);
  for (const type of ["mousePressed", "mouseReleased"])
    await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 });
  return true;
};
await send("Runtime.enable"); await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride",
           { width: 1440, height: 1600, deviceScaleFactor: 1, mobile: false });

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));
const shown = sel => evalJs(`(() => { const e = document.querySelector('${sel}');
  return !!e && e.getClientRects().length > 0; })()`);
// 在「换一种」那张清单上选一档(和用户点那一行一样走 change)。
const pick = id => evalJs(`(() => {
  const r = document.querySelector('#source-x-picker input[value="${id}"]');
  if (!r) return false; r.checked = true; r.dispatchEvent(new Event('change')); return true; })()`);
const picked = () => evalJs(`xChannelValue()`);
const text = sel => evalJs(`document.querySelector('${sel}')?.textContent || ''`);
// 往输入框里填一个值。**要连 `input` 事件一起发** —— 只赋 `.value` 不发事件是
// JS 才做得到的事,真人敲字必然伴随这个事件,而状态卡正是靠它跟着草稿走的。
const fill = (sel, value) => evalJs(`(() => {
  const el = document.querySelector('${sel}');
  el.value = ${JSON.stringify(value)};
  el.dispatchEvent(new Event('input', { bubbles: true }));
  return el.value; })()`);
const state = () => text('#source-x-state');

// 每个新文档一开张就把三个原生弹窗换成记录器。用 addScriptToEvaluateOnNewDocument
// 是因为它跑在页面脚本之前 —— 导航之后再替换,中间那段时间的调用就漏掉了。
await send("Page.addScriptToEvaluateOnNewDocument", { source: `
  window.__nativeDialogs = [];
  for (const name of ['alert', 'confirm', 'prompt'])
    window[name] = (...args) => { window.__nativeDialogs.push(name + ':' + args.join(' ')); };
` });
await send("Page.navigate", { url: base + "/" });
await until(`typeof openSubsPanel === 'function' && typeof subsCatalog !== 'undefined'
             && subsCatalog.length === 3`, 25000);
await evalJs(`switchTab('subs')`);
if (!await until(`document.querySelectorAll('#subs-switch button').length === 3`, 20000))
  throw new Error("来源切换器没渲染出来:" + await evalJs(
    `JSON.stringify({tab: typeof activeTab !== 'undefined' ? activeTab : null,
                     cat: (subsCatalog||[]).map(m=>m.id),
                     html: document.getElementById('subs-switch').innerHTML.slice(0,200),
                     err: document.getElementById('subs-error-text').textContent})`));
await clickAt("#subs-switch-social");
if (!await until(`sourceKind === 'social' && xChan !== null`, 20000))
  throw new Error("大V观点视图没打开:" + await evalJs(
    `JSON.stringify({kind: sourceKind, chan: xChan,
                     err: document.getElementById('source-x-state').textContent})`));
// **来源设置整块只在「订阅管理」那一档显示**(`updateSourceReadingView`:
// `$('source-settings').hidden = sourceView !== 'manage'`)—— 不切过去的话这一页
// 默认停在「最新内容」,整个 `#source-settings` 是 hidden,于是下面每一条
// `shown(...)` 都假、每一条 `!shown(...)` 都真:**这份探针会一半红一半"通过"，
// 而两边都不是在验它想验的东西**。这就是 2026-10-15 实测的形态:HEAD 上
// 27/49 红,基线和改动同样红,红的清单里全是"这一块该显示"那一类,
// 而"改了选择不冒充当前事实"之类不碰可见性的照旧绿。
// 所以这一步不是摆盘的装饰,是这份探针成立的前提。
await evalJs(`switchSourceView('manage')`);
if (!await until(`sourceView === 'manage'
                  && !document.getElementById('source-settings').hidden`, 20000))
  throw new Error("订阅管理那一档没打开:" + await evalJs(
    `JSON.stringify({view: typeof sourceView !== 'undefined' ? sourceView : null,
                     hidden: document.getElementById('source-settings').hidden})`));


export { evalJs, send, sleep, until, clickAt, check, shown, pick, picked, text, fill,
         state, fails, oks, pageErrors };

// 收尾:打印判据、关浏览器和 mock。**只有入口那一份调它。**
export function finish() {
  check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 400));
  const out = [...oks.map(n => `  ok ${n}`), ...fails.map(n => `  FAIL ${n}`)].join("\n");
  process.stdout.write(`${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`);
  ws.close(); proc.kill(); server.close();
  process.exitCode = fails.length ? 1 : 0;
}
