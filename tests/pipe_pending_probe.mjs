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
let pendingCalls = 0, retryCalls = 0, heldCalls = 0;
// 非 null = 下一次积压查询的回包按在这儿,等放行。回包内容在**收到请求那一刻**
// 就生成好(和真后端一样),所以放行时它带的是旧那一轮的事实。
let holdPending = null;

// 重试那一轮:**从头到尾没有一个快照是 running=true** —— 它就是跑得比轮询快。
// 真后端这时已经把那篇落成终态、积压数归零。每轮一个不同的 started_at(真后端
// 本来如此);要摆「两轮落在同一秒」那一局时显式传同一个值。
let fakeClock = 0;
const finishFastRun = (runId, detail, pending, startedAt) => {
  const at = startedAt || `2026-10-09 09:${String(++fakeClock).padStart(2, "0")}:00`;
  state = { ...state, run_id: runId, run_kind: "retry", running: false,
            started_at: at, finished_at: at,
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
      const body = { ...state, pending: { extract: pendingExtract }, last_run: null };
      if (holdPending) {
        const gate = holdPending; holdPending = null; heldCalls++;
        return void gate.then(() => json(body));
      }
      return json(body);
    }
    const extra = req.url.includes("source=false")
      ? {} : { source: { online: true, label: "测试来源", message: "已连接" } };
    return json({ ...state, ...extra, last_run: null });
  }
  if (path === "/api/pipeline/retry-failed" && req.method === "POST") {
    retryCalls++;
    // 每次点都是一轮新运行,身份跟着换(真后端如此)
    finishFastRun(`run-retry-${retryCalls}`,
                  "成功 0,失败 0,无正文 1(仅图片等,已记录不再提示)", 0);
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
// 摆局中断(前提没成立)也要落成一条红,别让它把已经跑过的那些检查一起带走
const setup = async (name, ok) => { if (!await ok) { fails.push(`摆局没成:${name}`); throw new Error("stop"); } };

await send("Page.navigate", { url: base + "/" });
if (!await until(`typeof refreshPipe === 'function' && typeof switchTab === 'function'`, 20000))
  throw new Error("页面没起来");
try {

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

// ── 5. 交错:一次积压查询还在途时点重试,放行旧回包之后照旧要归零 ────────────
// 在途那次查询只能回答它开跑那一刻的事实,而「出现了一轮新的已结束运行」一轮
// 只说一次 —— 那一次刷新被在途查询吞掉,面板就永久停在旧数字,再轮询多少次也
// 不消失,只有手动刷新页面才归零(2026-10-09 第 2 轮审查实测)。
const armHold = () => {
  let release = () => {};
  holdPending = new Promise(r => { release = r; });
  const t = setTimeout(release, 20000);          // 失败路径上别把服务挂死
  return () => { clearTimeout(t); release(); };
};
const pendingIs = n => until(`document.querySelector('#pipe-stage-extract .pipe-stage-pending')
                              ?.textContent === '当前待处理 ${n} 项'`, 12000);

finishFastRun("run-D", "成功 3,失败 0,跳过 1(正文空,需回填)", 1);
await evalJs(`refreshPipe(); true`);
await setup("面板没先显示 1", pendingIs(1));

const release = armHold();
await evalJs(`switchTab('search'); switchTab('pipe'); true`);   // 真实路径:切走再切回
check("切回同步中心引发的那次积压查询卡在途",
      await until(`pipePendingRequest !== null`, 4000));
const atClick = pendingCalls;
check("在途期间「重试失败项」仍点得动",
      await until(`!document.getElementById('pipe-retry').disabled`) && await clickAt("#pipe-retry"));
check("新结果已经到了", await until(`pipeDoneRun === 'run-retry-2'`, 8000),
      await evalJs(`pipeDoneRun`));
check("这一刻刷新确实被在途查询挡着(所以得记一笔欠账)",
      pendingCalls === atClick && await evalJs(`pipePendingWanted === true`),
      `${atClick} → ${pendingCalls}`);
release();
check("放行旧回包之后,待处理数照旧自己归零", await pendingIs(0), await pendingText());
check("欠的那次查询真的补发出去了", pendingCalls > atClick, `${atClick} → ${pendingCalls}`);
check("补上了就把欠账清掉", await evalJs(`pipePendingWanted === false`));
check("按住的那次回包确实是旧那一轮的事实(不是摆设)", heldCalls === 1, String(heldCalls));

const settled5 = pendingCalls;
for (let i = 0; i < 3; i++) { await evalJs(`refreshPipe(); true`); await sleep(250); }
check("补完之后反复轮询同一结果仍然不重复扫积压", pendingCalls === settled5,
      `${settled5} → ${pendingCalls}`);

// ── 6. 两轮落在同一秒里:回包的身份认 run_id,不拿秒级时间戳当身份 ───────────
finishFastRun("run-F", "成功 3,失败 0,跳过 1(正文空,需回填)", 1, "2026-10-09 10:00:00");
await evalJs(`refreshPipe(); true`);
await setup("面板没先显示 1", pendingIs(1));
const release2 = armHold();
await evalJs(`refreshPipePending(); true`);
check("第二次也卡住了一个在途查询", await until(`pipePendingRequest !== null`, 4000));
finishFastRun("run-G", "成功 0,失败 0,无正文 1(仅图片等,已记录不再提示)", 0, "2026-10-09 10:00:00");
await evalJs(`refreshPipe(); true`);
// **放行之前必须等页面真的看到了新那一轮**:不等的话旧回包可能赶在状态读完之前
// 回来,那时快照还是上一轮、身份当然对得上,随后那次状态读又顺手补了一次查询 ——
// 两种身份口径在这条时序上都是绿的,这一项就对判据不敏感了
check("页面已经看到同一秒里的那一轮新结果",
      await until(`pipeDoneRun === 'run-G' && pipePendingRequest !== null`, 8000),
      await evalJs(`pipeDoneRun + '/' + (pipePendingRequest !== null)`));
release2();
check("同一秒里换了一轮时,旧回包不许被当成当前事实", await pendingIs(0), await pendingText());

check("重试端点一共被打了两次(两个场景各一次)", retryCalls === 2, String(retryCalls));
check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));

} catch (e) {
  if (e.message !== "stop") fails.push(`探针中断 — ${e.message}`);
}

// ── 收尾 ─────────────────────────────────────────────────────────────────────
const out = [...oks.map(n => `  ✓ ${n}`), ...fails.map(n => `  ✗ ${n}`)].join("\n");
process.stdout.write(`${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`);
ws.close(); proc.kill(); server.close();
process.exitCode = fails.length ? 1 : 0;
