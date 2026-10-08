// 截一组工作区截图,给设计评审用。CDP 直连 playwright 缓存里的
// chrome-headless-shell(不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/shots.mjs <baseURL> <outDir> [label]
//
// 每个视图一张:图鉴/搜索/问答/收藏/管道 + 详情抽屉 + 手机导航,
// 外加两张图谱页(如果 vault 里有产物)。
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";

const BIN = process.env.HEADLESS_SHELL;
const [base, outDir, label = "now"] = process.argv.slice(2);
mkdirSync(outDir, { recursive: true });

const proc = spawn(BIN, ["--headless", "--remote-debugging-port=0", "--disable-gpu",
                         "--hide-scrollbars", "--force-color-profile=srgb",
                         "--window-size=1440,1000", "about:blank"],
                   { stdio: ["ignore", "pipe", "pipe"] });
const port = await new Promise((res, rej) => {
  let buf = "";
  proc.stderr.on("data", d => {
    buf += d;
    const m = buf.match(/ws:\/\/127\.0\.0\.1:(\d+)\//);
    if (m) res(+m[1]);
  });
  proc.on("exit", c => rej(new Error(`headless shell 退出 ${c}\n${buf}`)));
  setTimeout(() => rej(new Error(`没等到 DevTools 端口\n${buf}`)), 20000);
});

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));

let id = 0;
const pend = new Map();
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
});
const send = (method, params = {}) => {
  const mid = ++id;
  return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
};
const evalJs = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

await send("Page.enable");
await send("Runtime.enable");

async function shoot(name, { w = 1440, h = 1000, url = "/", prep = "", wait = 1400,
                             ready = "" } = {}) {
  await send("Emulation.setDeviceMetricsOverride",
             { width: w, height: h, deviceScaleFactor: 2, mobile: w < 800 });
  await send("Page.navigate", { url: base + url });
  // 等真内容到位再截,别用固定 sleep:5000+ 个工具经代理拉一次要好几秒,
  // 赶早了截到的是一屏骨架屏
  const deadline = Date.now() + 25000;
  if (ready) {
    while (Date.now() < deadline) {
      if (await evalJs(ready)) break;
      await sleep(400);
    }
  }
  await sleep(wait);
  if (prep) { await evalJs(prep); await sleep(1100); }
  const r = await send("Page.captureScreenshot", { format: "png" });
  const data = r.result?.data;
  if (!data) { console.log(`!! ${name} 没截到`); return; }
  writeFileSync(`${outDir}/${label}-${name}.png`, Buffer.from(data, "base64"));
  console.log(`ok ${label}-${name}.png`);
}

const GRID = `document.querySelectorAll('button.gcard').length > 8`;

await shoot("gallery", { ready: GRID });
await shoot("gallery-list", { ready: GRID, prep: `document.querySelector('.view-switch button:last-child')?.click()` });
await shoot("drawer", { ready: GRID, prep: `document.querySelector('button.gcard')?.click()`, wait: 1800 });
await shoot("search", { ready: GRID, prep: `switchTab('search'); document.getElementById('q').value='claude code'; doSearch();`, wait: 2200 });
await shoot("ask", { ready: GRID, prep: `switchTab('ask')` });
await shoot("fav", { ready: GRID, prep: `switchTab('fav')` });
await shoot("subs", { ready: GRID, prep: `switchTab('subs')`, wait: 1800 });
await shoot("pipe", { ready: GRID, prep: `switchTab('pipe')` });
await shoot("mobile-gallery", { w: 390, h: 844, ready: GRID });
await shoot("mobile-nav", { w: 390, h: 844, ready: GRID, prep: `document.getElementById('nav-toggle')?.click()||document.querySelector('.mobile-menu')?.click()` });

// 图谱页:直接走 kb-web 的 /graph 路由(只读 GET)
for (const [n, u] of [["graph", "/graph"], ["graph-projects", "/graph/projects"], ["graph-skills", "/graph/skills"]]) {
  await shoot(n, { url: u, wait: 2600 });
}

ws.close();
proc.kill();
