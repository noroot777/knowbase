// 在无头 Chrome 里跑一遍「先筛选 → 打开一个详情 → 点它的关联项」,把 DOM 状态
// 打成一行 JSON 交给 tests/test_catgraph_browser.py 断言。
//
//   node catgraph_probe.mjs <page.html> <filter,filter,…> <A 的名字前缀> <B 的名字前缀>
//
// filter ∈ search|win|heat|repo|none:分别用搜索词、时间窗、热度、「只看有仓库」
// 把 B 挡住(none = 不筛)。B 是 A 的关联项,点它就是从筛选结果里跳到一个被挡住
// 的节点。几个 filter 共用一次浏览器启动,每个之间重新加载页面。
//
// 用 CDP 直接驱动 playwright 缓存里的 chrome-headless-shell(不装 playwright、
// 不连网、始终无头)。没有这个二进制时 Python 那边会跳过。
import { spawn } from "node:child_process";

const BIN = process.env.HEADLESS_SHELL;
const [page, filters, aName, bName] = process.argv.slice(2);

const proc = spawn(BIN, ["--headless", "--remote-debugging-port=0", "--disable-gpu",
                         "--hide-scrollbars", "--window-size=1400,1000", "about:blank"],
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
  ws.send(JSON.stringify({ id: mid, method, params }));
  return new Promise(r => pend.set(mid, r));
};
async function run(expr) {
  const r = await send("Runtime.evaluate",
                       { expression: expr, awaitPromise: true, returnByValue: true });
  const bad = r.result?.exceptionDetails;
  if (bad) throw new Error(JSON.stringify(bad));
  return r.result?.result?.value;
}

await send("Page.enable");
async function load() {
  const loaded = new Promise(r => {
    const on = ev => {
      if (JSON.parse(ev.data).method === "Page.loadEventFired") {
        ws.removeEventListener("message", on); r();
      }
    };
    ws.addEventListener("message", on);
  });
  await send("Page.navigate", { url: "file://" + page });
  await loaded;
}

const A = JSON.stringify(aName), B = JSON.stringify(bName);
const sleep = ms => `new Promise(r => setTimeout(r, ${ms}))`;
const pick = `(t => [...document.querySelectorAll(".pill")]
  .find(p => !p.classList.contains("hide") && p.textContent.trim().startsWith(t)))`;

async function probe(filter) {
  return run(`(async () => {
  const seg = k => document.querySelector('.seg[data-k="' + k + '"]');
  const hit = ${pick};
  if (${JSON.stringify(filter)} === "search") {
    const q = document.getElementById("q");
    q.value = ${A};
    q.dispatchEvent(new Event("input", {bubbles: true}));
  } else if (${JSON.stringify(filter)} === "win") {
    seg("win").querySelector('[data-v="30"]').click();
  } else if (${JSON.stringify(filter)} === "heat") {
    seg("heat").querySelector('[data-v="2"]').click();
  } else if (${JSON.stringify(filter)} === "repo") {
    document.getElementById("repoBtn").click();
  }
  await ${sleep(300)};
  const hiddenFirst = !hit(${B});                 // B 确实被挡住了(前提)
  hit(${A}).click();                              // 打开 A 的详情
  await ${sleep(500)};
  const btn = [...document.querySelectorAll("#panelBody button[data-go]")]
    .find(x => x.textContent.trim().startsWith(${B}));
  const hasButton = !!btn;
  if (btn) btn.click();
  await ${sleep(700)};
  const vis = [...document.querySelectorAll(".pill")].filter(p => !p.classList.contains("hide"));
  return {
    hiddenFirst, hasButton,
    panel: (document.querySelector("#panelBody h3") || {}).textContent || "",
    visibleSelected: vis.filter(p => p.classList.contains("sel")).length,
    visiblePills: vis.length,
    visibleDimmed: document.body.classList.contains("hasSel")
      ? vis.filter(p => !p.classList.contains("lit") && !p.classList.contains("sel")).length : 0,
    edgePaths: document.querySelectorAll("#edges path").length,
    query: document.getElementById("q").value,
    winOn: seg("win").querySelector("button.on").dataset.v,
    heatOn: seg("heat").querySelector("button.on").dataset.v,
    repoOn: document.getElementById("repoBtn").classList.contains("on"),
    bodyClass: document.body.className,
  };
})()`);
}

const out = {};
for (const f of filters.split(",")) {
  await load();
  out[f] = await probe(f);
}
console.log(JSON.stringify(out));
ws.close();
proc.kill("SIGTERM");
