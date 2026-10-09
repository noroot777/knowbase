// 跑得极快的那一轮结束之后,面板上的「当前待处理 N 项」要自己归零。
//
// 纯图片文章落终态那条路一次模型都不调,整轮几百毫秒就结束 —— 比面板的轮询间隔
// (空闲 6 秒、运行中 1.5 秒)短得多,于是页面**一次都没见到 running=true**。
// 原来刷新积压数的条件是 `wasRunning && !s.running` 这个跳变,这一轮压根不触发:
// 后端 `pending.extract` 已经是 0,面板还写着「当前待处理 1 项」,等多少轮空闲
// 轮询都不消失,只有手动刷新页面才归零(2026-10-09 审查实测)。
// 后台定时跑完的那一轮同理 —— 用户在别的页面上待着,回来看到的也是旧数字。
//
// 判据是**页面上那行字**,不是「有没有发过那个请求」:发了而没渲染、或者渲染了
// 旧值,在用户那儿是同一回事。反过来还钉一边:没有新结果时不许每轮都去拉积压数
// (那是个每次都要扫文件的查询)。
//
// 自带 mock 后端,CDP 直连 playwright 缓存里的 chrome-headless-shell
// (不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/pipe_pending_probe.mjs
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
const BIN = process.env.HEADLESS_SHELL;
if (!BIN) { console.error("需要 HEADLESS_SHELL=<chrome-headless-shell 路径>"); process.exit(2); }

// ── mock 后端 ────────────────────────────────────────────────────────────────
const KEYS = ["crawl", "export", "index", "extract", "projects", "wiki"];
const okStages = detail => Object.fromEntries(KEYS.map(
  k => [k, { status: "ok", detail: k === "extract" ? detail : "完成" }]));

// 「上一轮」:extract 报了一篇正文为空,积压数 1
let state = {
  running: false, run_kind: "sync", planned_stages: [...KEYS], stop_supported: true,
  stop_requested: false, started_at: "2026-10-09 08:00:00",
  finished_at: "2026-10-09 08:02:00", run_id: "run-A", crawl_filter: {},
  stages: okStages("成功 3,失败 0,跳过 1(正文空,需回填)"), log: ["管道结束"],
};
let pendingExtract = 1;
let pendingCalls = 0, retryCalls = 0;

// 重试那一轮:**从头到尾没有一个快照是 running=true** —— 它就是跑得比轮询快。
// 真后端这时已经把那篇落成终态、积压数归零。
const finishFastRun = (runId, detail, pending) => {
  state = { ...state, run_id: runId, run_kind: "retry", running: false,
            started_at: "2026-10-09 09:00:00", finished_at: "2026-10-09 09:00:01",
            stages: okStages(detail), log: ["管道结束"] };
  pendingExtract = pending;
};

const MIME = { html: "text/html; charset=utf-8", js: "text/javascript; charset=utf-8",
               css: "text/css; charset=utf-8", json: "application/json",
               svg: "image/svg+xml", ico: "image/x-icon", woff2: "font/woff2" };
const FIXTURES = {
  "/api/stats": { documents: 21674 },
  "/api/pipeline/channels": {
    channels: [{ id: "wechat_local", label: "公众号", hint: "读取本机已同步的公众号推送" }],
    default: "wechat_local", labels: {}, aliases: {},
  },
  "/api/pipeline/accounts": { accounts: [] },
  "/api/facets": { tags: [], entities: [], accounts: [] },
  // 图鉴是首屏那一块,接口形状不给全会在控制台留一串跟本探针无关的 TypeError,
  // 把「整轮没有 JS 错误」那一项淹掉
  "/api/projects": { tools: [], kinds: {}, total_entries: 0 },
  "/api/sources/catalog": { modules: [] },
  "/api/daily/list": { days: [] },
};

const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const json = (data, code = 200) => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(code, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  if (path === "/api/pipeline/status" && req.method === "GET") {
    if (req.url.includes("pending=true")) {
      pendingCalls++;
      return json({ ...state, pending: { extract: pendingExtract }, last_run: null });
    }
    const extra = req.url.includes("source=false")
      ? {} : { source: { online: true, label: "测试来源", message: "已连接" } };
    return json({ ...state, ...extra, last_run: null });
  }
  if (path === "/api/pipeline/retry-failed" && req.method === "POST") {
    retryCalls++;
    finishFastRun("run-B", "成功 0,失败 0,无正文 1(仅图片等,已记录不再提示)", 0);
    return json({ ok: true });
  }
  if (path.startsWith("/api/")) return json(FIXTURES[path] ?? {});
  const file = path === "/" ? "index.html" : path.replace(/^\/static\//, "");
  const target = normalize(join(STATIC, file));
  if (!target.startsWith(STATIC)) { res.writeHead(403); return res.end(); }
  try {
    const body = readFileSync(target);
    res.writeHead(200, { "Content-Type": MIME[file.split(".").pop()] || "application/octet-stream",
                         "Content-Length": body.length, "Cache-Control": "no-store" });
    res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(r => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}`;

// ── 无头浏览器 ───────────────────────────────────────────────────────────────
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
const sleep = ms => new Promise(r => setTimeout(r, ms));
const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let msgId = 0;
const pend = new Map();
const pageErrors = [];
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
  const id = ++msgId;
  return new Promise(res => { pend.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
};
const evalJs = async expression => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const until = async (expr, ms = 8000) => {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) { if (await evalJs(expr)) return true; await sleep(120); }
  return false;
};
// 真鼠标点在元素中心:按钮的 disabled 状态只有真实事件才算数
const clickAt = async sel => {
  const box = await evalJs(`(() => { const e = document.querySelector('${sel}');
    if (!e || e.disabled) return null; const r = e.getBoundingClientRect();
    return JSON.stringify({ x: r.left + r.width / 2, y: r.top + r.height / 2 }); })()`);
  if (!box) return false;
  const { x, y } = JSON.parse(box);
  for (const type of ["mousePressed", "mouseReleased"])
    await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 });
  return true;
};
const pendingText = () => evalJs(
  `document.querySelector('#pipe-stage-extract .pipe-stage-pending').textContent`);
const toasts = () => evalJs(
  `[...document.querySelectorAll('.toast, #toasts > *')].map(e => e.textContent).join(' | ')`);

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride",
           { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));

await send("Page.navigate", { url: base + "/" });
if (!await until(`typeof refreshPipe === 'function' && typeof switchTab === 'function'`, 20000))
  throw new Error("页面没起来");

// ── 1. 打开同步中心,积压数显示上一轮留下的 1 ─────────────────────────────────
await evalJs(`switchTab('pipe'); true`);
check("进来时面板显示上一轮留下的待处理 1 项",
      await until(`document.querySelector('#pipe-stage-extract .pipe-stage-pending')
                   ?.textContent === '当前待处理 1 项'`),
      await pendingText());

// ── 2. 点「重试失败项」:那一轮快得页面从没见过 running ───────────────────────
const before = pendingCalls;
check("「重试失败项」点得动", await until(`!document.getElementById('pipe-retry').disabled`)
      && await clickAt("#pipe-retry"));
// 后果先断言:这才是本轮承诺的「提醒消失」
check("快速结束的那一轮之后,待处理数自己归零(不用手动刷新页面)",
      await until(`document.querySelector('#pipe-stage-extract .pipe-stage-pending')
                   ?.textContent === '当前待处理 0 项'`, 12000),
      await pendingText());
check("页面确实一次都没见过 running=true",
      await evalJs(`pipeSnapshot && pipeSnapshot.running === false`));
check("它真的去重新拉了一次积压数", pendingCalls > before, `${before} → ${pendingCalls}`);
check("阶段详情也换成了这一轮的文案",
      (await evalJs(`document.querySelector('#pipe-stage-extract .pipe-stage-detail').textContent`))
        .includes("无正文 1"));
check("没有谁冒出一句「已结束」—— 这一轮用户压根没见它开始",
      !(await toasts()).includes("已结束"), await toasts());

// ── 3. 后台跑完的那一轮同理(用户在别的页面上待着,回来看到的不许是旧数字) ──
finishFastRun("run-C", "成功 0,失败 0,无正文 3(仅图片等,已记录不再提示)", 3);
await evalJs(`refreshPipe(); true`);
check("后台跑完的一轮也把积压数带上来",
      await until(`document.querySelector('#pipe-stage-extract .pipe-stage-pending')
                   ?.textContent === '当前待处理 3 项'`, 12000),
      await pendingText());

// ── 4. 反过来钉一边:没有新结果时不许每轮都去扫一遍积压 ──────────────────────
await sleep(400);
const settled = pendingCalls;
for (let i = 0; i < 3; i++) { await evalJs(`refreshPipe(); true`); await sleep(250); }
check("同一轮结果反复轮询不会重复去扫积压数", pendingCalls === settled,
      `${settled} → ${pendingCalls}`);

check("重试端点只被打了一次", retryCalls === 1, String(retryCalls));
check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));

// ── 收尾 ─────────────────────────────────────────────────────────────────────
const out = [...oks.map(n => `  ✓ ${n}`), ...fails.map(n => `  ✗ ${n}`)].join("\n");
process.stdout.write(`${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`);
ws.close(); proc.kill(); server.close();
process.exitCode = fails.length ? 1 : 0;
