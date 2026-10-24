import { writeFileSync } from "node:fs";
import { launchShell } from "../tests/probe_shell.mjs";

const base = "http://127.0.0.1:8791";
const out = "C:/Users/fanda/AppData/Local/Temp/kbrev2/shots";
const { port } = await launchShell(["--force-color-profile=srgb", "--window-size=1200,900"]);
const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let id = 0; const pend = new Map();
ws.addEventListener("message", ev => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } });
const send = (method, params = {}) => { const mid = ++id; return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); }); };
const evalJs = async e => (await send("Runtime.evaluate", { expression: e, awaitPromise: true, returnByValue: true })).result?.result?.value;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const errors = [];
await send("Page.enable"); await send("Runtime.enable"); await send("Log.enable");
ws.addEventListener("message", ev => { const m = JSON.parse(ev.data);
  if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails?.text || "ex");
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error") errors.push(m.params.entry.text); });
await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 900, deviceScaleFactor: 2, mobile: false });
await send("Page.navigate", { url: base + "/" });
await sleep(2500);
await evalJs("openWechatSetup()");
await sleep(2500);
const probe = await evalJs(`JSON.stringify({
  state: wechatSetup?.state,
  accounts: wechatSetup?.accounts ?? null,
  rowHidden: document.getElementById('wechat-account-row').hidden,
  options: [...document.getElementById('wechat-account').options].map(o => o.textContent),
  checkDisabled: document.getElementById('wechat-check').disabled,
  checkLabel: document.getElementById('wechat-check').textContent,
  dbState: document.getElementById('wechat-database-state').textContent,
  status: document.getElementById('wechat-status').textContent.slice(0, 80),
})`);
console.log(probe);
const shot = await send("Page.captureScreenshot", { format: "png" });
writeFileSync(`${out}/r2-01-account-deadend.png`, Buffer.from(shot.result.data, "base64"));
console.log("errors:", JSON.stringify(errors));
process.exit(0);
