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
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { launchShell } from "./probe_shell.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");

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
// 非空 = 这个进程启动时只从历史恢复到最新那一轮,它的重试范围没留下(真后端的
// `restore_gap`)。面板据此把「重试失败项」按不下去并如实说一句。
let restoreGap = "";
// 非空 = 待处理数那个回包的版本固定报这个旧值(摆「请求成功了、取回来的却一直是
// 另一轮」那一局)。真后端不该这样,但它是这块预算反馈的边界。
let stalePending = "";
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
  // 「这份回包对应哪一轮终态」,口径抄后端 `result_version`:身份没有就用收尾时间,
  // 再没有就是「从没跑过」。前端拿它当应用凭据。
  const versionOf = r => String((r && (r.run_id || r.finished_at)) || "") || "none";
  if (path === "/api/pipeline/status" && req.method === "GET") {
    if (req.url.includes("pending=true")) {
      pendingCalls++;
      const body = { ...state, pending: { extract: pendingExtract }, last_run: null,
                     result_version: stalePending || versionOf(state),
                     restore_gap: restoreGap };
      if (holdPending) {
        const gate = holdPending; holdPending = null; heldCalls++;
        return void gate.then(() => json(body));
      }
      return json(body);
    }
    const extra = req.url.includes("source=false")
      ? {} : { source: { online: true, label: "测试来源", message: "已连接" } };
    return json({ ...state, ...extra, last_run: null, result_version: versionOf(state),
                  restore_gap: restoreGap });
  }
  // 历史清单。**本探针不验历史渲染**(那是 `pipe_history_probe.mjs` 的事),给一份
  // 自洽的回包只为了让新鲜度那块能追平 —— 不给的话它永远落后,缺口提示条一直顶着,
  // 下面那几项「不存在缺口」就无从断言了。
  if (path === "/api/pipeline/runs") {
    const rows = state.finished_at ? [{ ...state, version: versionOf(state) }] : [];
    return json({ runs: rows, error: "", version: versionOf(state) });
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
const { proc, port } = await launchShell(["--window-size=1440,1000"]);
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

// ── 7. 一轮跑得久不许把补读预算耗掉 ─────────────────────────────────────────
// 跑着的时候没有终态可追平(目标是空的),而两块附属结果都算落后 —— 每次状态轮询
// 都扣一次额度的话,一轮 29 秒的同步就把五次机会花在一个根本读不了的目标上(待处理
// 数那个加载器在运行中本来就直接返回,一次请求都没发),跑完目标前移时额度已满:
// 面板永久写着「还有 N 项内容待提取 · 已停止自动重试」,点一次「重新读取」才回来。
state = { ...state, running: true, run_id: "run-H", run_kind: "sync",
          started_at: "2026-10-09 11:00:00", finished_at: "" };
for (let i = 0; i < 7; i++) { await evalJs(`refreshPipe(); true`); await sleep(160); }
await setup("页面没进入运行中", until(`pipeSnapshot && pipeSnapshot.running === true`));
check("跑着的那几轮轮询没有扣掉补读预算", await evalJs(
  `PIPE_FRESH.pending.tries === 0 && PIPE_FRESH.history.tries === 0`),
  await evalJs(`PIPE_FRESH.pending.tries + '/' + PIPE_FRESH.history.tries`));

finishFastRun("run-I", "成功 2,失败 0,跳过 3(正文空,需回填)", 3);
await evalJs(`refreshPipe(); true`);
check("跑完之后待处理数照旧自己追平", await pendingIs(3), await pendingText());
check("面板上没有「已停止自动重试」那句话", await evalJs(
  `document.getElementById('pipe-fresh').hidden`),
  await evalJs(`document.getElementById('pipe-fresh-text').textContent`));

// ── 8. 从没跑过任何一轮的库:待处理数照旧显示,而且不许忙循环 ─────────────────
// 新进程/新库里顶层身份是空的。前端原来拿「身份非空」当应用凭据,于是这一档既不
// 应用、也不清欠账,循环立刻发下一次查询 —— 实测 1.3 秒里两千多次,每次都去扫一遍
// 库,而页面上那个数始终是空的。版本改由服务端给之后这一档是「none」,是个确定的值。
state = { running: false, run_kind: "", planned_stages: [...KEYS], stop_supported: true,
          stop_requested: false, started_at: "", finished_at: "", run_id: "",
          crawl_filter: {}, log: [],
          stages: Object.fromEntries(KEYS.map(k => [k, { status: "", detail: "" }])) };
pendingExtract = 2;
// **基线取在导航之前**:页面加载时 `initWorkspace` 收尾那次状态读取就会引发一次
// 待处理数查询,取在它之后的话忙循环已经发生完了,这一项就什么也拦不住。
const n0 = pendingCalls;
await send("Page.navigate", { url: base + "/" });
await setup("页面没重建起来",
            until(`typeof refreshPipe === 'function' && typeof switchTab === 'function'`, 20000));
await evalJs(`switchTab('pipe'); true`);
check("从没跑过一轮的库里,待处理数照旧显示得出来", await pendingIs(2), await pendingText());
await sleep(1200);
check("而且没有一直重查", pendingCalls - n0 <= 5, `${pendingCalls - n0} 次查询`);
check("两边都报「从没跑过」,所以不存在缺口", await evalJs(
  `PIPE_FRESH.pending.applied === 'none' && PIPE_FRESH.pending.desired === 'none'`),
  await evalJs(`PIPE_FRESH.pending.applied + '/' + PIPE_FRESH.pending.desired`));

// ── 9. 机制单测:补读预算属于**目标版本** ───────────────────────────────────
// 手工摆 `PIPE_FRESH` 的槽位、直接调那三个出口:摆的是真实时序里要等上百秒才攒
// 出来的状态(退避到第 5 次)。**射程只到这一层的判据** —— 它在真页面上确实生效
// 由第 7、8 组管。
const mech = JSON.parse(await evalJs(`(() => {
  const r = {};
  // ① 目标一换,预算重新给:上一个目标耗光了不许把后面每一轮都锁死
  PIPE_FRESH.history = { label: '历史运行', applied: 'run-x', desired: 'run-old',
                         tries: PIPE_FRESH_TRIES, due: 9999, reason: 'boom' };
  let called = 0;
  pipeFreshCheck('run-new', { history: () => { called++; } });
  r.a = called === 1 && PIPE_FRESH.history.tries === 0 && PIPE_FRESH.history.reason === '';
  // ② 跑着的时候(目标是空的)不读也不扣
  PIPE_FRESH.pending = { label: '待处理数', applied: 'run-x', desired: 'run-x',
                         tries: 0, due: 0, reason: '' };
  let n = 0;
  pipeFreshCheck('', { pending: () => { n++; } });
  r.b = n === 0 && PIPE_FRESH.pending.tries === 0;
  // ③ 预算由加载器回报结果时扣,不在调度这一步扣 —— 它可能一次请求都没发出去
  PIPE_FRESH.history = { label: '历史运行', applied: 'a', desired: 'b',
                         tries: 0, due: 0, reason: '' };
  pipeFreshCheck('b', { history: () => {} });
  r.c = PIPE_FRESH.history.tries === 0;
  // ④ 「取到的还是旧那份」和「这次没取到」各算一次用掉的尝试
  pipeFreshOk('history', 'a');
  const afterOk = PIPE_FRESH.history.tries;
  pipeFreshMiss('history', 'boom');
  r.d = afterOk === 1 && PIPE_FRESH.history.tries === 2;
  return JSON.stringify(r);
})()`));
check("目标一换,补读预算重新给", mech.a);
check("跑着的时候不读也不扣预算", mech.b);
check("加载器一次请求都没发时不扣预算", mech.c);
check("取到旧数据、取不到,各算一次用掉的尝试", mech.d);

// ── 10. 恢复不完整:重试按不下去,而且说清楚为什么 ───────────────────────────
// 状态文件写失败那一轮,磁盘上最新的终态只在历史里 —— 它没有导出范围和失败名单。
// 拿上一轮的范围去抓会把失败那个来源摘掉(面板报「已保存 0 条」,而那几篇一个都没
// 落盘),所以后端那边直接拒绝起跑;这里是**先说清楚**,别让人点了才知道。
restoreGap = "run-I";
await evalJs(`refreshPipe(); true`);
await setup("页面没读到恢复欠账", until(`pipeRestoreGap === 'run-I'`, 6000));
check("恢复不完整时「重试失败项」按不下去",
      await evalJs(`document.getElementById('pipe-retry').disabled === true`));
check("而且如实说明了原因", await evalJs(
  `!document.getElementById('pipe-fresh').hidden
   && document.getElementById('pipe-fresh-text').textContent.includes('重试范围没能留档')`),
  await evalJs(`document.getElementById('pipe-fresh-text').textContent`));
check("「重新读取」不会把这一句擦掉(它不是读取问题)", await evalJs(
  `pipeFreshRetry(); pipeRestoreGap === 'run-I'
   && document.getElementById('pipe-fresh-text').textContent.includes('重试范围没能留档')`));
restoreGap = "";
await evalJs(`refreshPipe(); true`);
check("跑过一轮之后那句话收起来、按钮也回来了", await until(
  `pipeRestoreGap === '' && document.getElementById('pipe-retry').disabled === false`, 6000),
  await evalJs(`pipeRestoreGap + '/' + document.getElementById('pipe-retry').disabled`));

// ── 11. 「请求成功了、取回来的是另一轮」也要扣预算 ──────────────────────────
// 不扣的话这一块的预算一次都不扣:每轮状态轮询都再补三次查询,既到不了上限、也到
// 不了「连试两次才说」那个提示阈值 —— 面板一个字不说,而后台一直在扫库。历史那边
// 的同类回包是扣预算的,两个 loader 要一个口径。
stalePending = "run-stale";
const staleBefore = pendingCalls;
await evalJs(`refreshPipePending(); true`);
const staleSettled = await until(`pipePendingWanted === false`, 8000);
// 先钉那道循环封口:对不齐时最多再查几次就退出,交给有界补读
check("对不齐时不会一直在循环里重查", staleSettled && pendingCalls - staleBefore <= 3,
      `收尾=${staleSettled}，${pendingCalls - staleBefore} 次查询`);
check("取到的一直是另一轮时,这次尝试要算进预算里", staleSettled && await evalJs(
  `PIPE_FRESH.pending.tries >= 1 && PIPE_FRESH.pending.reason !== ''`),
  await evalJs(`PIPE_FRESH.pending.tries + '/' + PIPE_FRESH.pending.reason`));
stalePending = "";
await sleep(300);

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));

} catch (e) {
  if (e.message !== "stop") fails.push(`探针中断 — ${e.message}`);
}

// ── 收尾 ─────────────────────────────────────────────────────────────────────
const out = [...oks.map(n => `  ✓ ${n}`), ...fails.map(n => `  ✗ ${n}`)].join("\n");
process.stdout.write(`${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`);
ws.close(); proc.kill(); server.close();
process.exitCode = fails.length ? 1 : 0;
