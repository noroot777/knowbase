// 一轮同步的红,在下一轮跑完之后还要有地方查得到。
//
// 面板那份当前状态只留最后一轮:上午那轮翻译报了 503、导出阶段判了红,晚上一次
// 「重试失败项」跑完全绿,面板上就只剩那一轮的结论 —— 那个红没有任何地方查得到。
//
// 判据是**页面上那几行字**:翻到上午那一轮时,阶段卡片要显示它那个红和「503」,
// 而不是最新那一轮的绿。外加一道**同形于确认框那件事**的护栏:「重试失败项」
// 重试的永远是最后一轮的范围,所以翻历史时它必须按不下去 —— 「看到的那一轮」和
// 「按下去会动的那一轮」必须是同一轮。反过来钉一边:最新视图下它照旧能点。
//
// 自带 mock 后端,CDP 直连 playwright 缓存里的 chrome-headless-shell
// (不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/pipe_history_probe.mjs
import { createServer } from "node:http";
import { readFileSync, writeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { launchShell } from "./probe_shell.mjs";
// 摆盘(四轮终态、阶段常量、静态回包)在隔壁,见那份文件的抬头
import { MORNING, EVENING, LATER, FOURTH, MIME, FIXTURES }
  from "./pipe_history_fixture.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");

let runs = [EVENING, MORNING];
let retryCalls = 0, runsCalls = 0;
// 非 null = 下一次历史查询的回包按在这儿等放行。**回包内容在收到请求那一刻就生成
// 好**(和真后端一样),所以放行时它带的是请求发出时那一刻的事实 —— 这正是「旧回包
// 迟到」那一局要的。
let holdRuns = null;
// >0 = 接下来这几次历史查询返回 503(每次扣 1)。摆的是「那一轮唯一的一次补刷恰好
// 失败」:后端记录没丢,纯粹是这一屏没取到 —— 判据就落在「它自己补不补得回来」上。
let failRuns = 0;
// 同上,作用在待处理数那一条(`?pending=true`)。两条走同一套新鲜度,所以各摆一次。
let failPending = 0;
// 非空 = 后端最近一次留档失败了,`/api/pipeline/runs` 要如实带出来
let runsError = '';
// 非 null = 下一次待处理数查询的回包按在这儿等放行。**内容在收到请求那一刻就生成
// 好**(和 `holdRuns` 一样、和 `holdStatus` 相反)—— 摆的就是「这份回包属于上一轮」:
// 放行时它带的是请求发出时那一刻的身份和数字。
let holdPending = null;
let pendingExtract = 2;
// 非 null = 下一次状态轮询(`refreshPipe` 那一条)的回包按在这儿等放行。**和
// `holdRuns` 的生成时机刚好相反:它的回包是放行那一刻才生成的**,于是带的是那一轮
// 已经结束之后的事实。两个合起来摆的就是生产那个窗口:两个请求一起发出,服务端先
// 答完历史(此刻那一轮还没结束、回包里没有它),那一轮随后结束,服务端才答状态
// (回包里有它)。顺序反过来也是同一个窗口,所以两种放行顺序各验一遍。
let holdStatus = null;
let state = {
  running: false, stop_supported: true, stop_requested: false, ...EVENING,
};


const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const json = (data, code = 200) => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(code, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  // 「这份回包对应哪一轮终态」,口径抄后端 `result_version`:身份没有就用收尾时间,
  // 再没有就是「从没跑过」。空闲时报最新那一轮的、跑着时报正在跑这一轮的。
  const versionOf = r => String((r && (r.run_id || r.finished_at)) || "") || "none";
  const statusVersion = () => versionOf(state.running ? state : runs?.[0]);
  if (path === "/api/pipeline/status") {
    // 最新那一轮全绿,但还有 2 项待提取 —— 于是「重试失败项」在最新视图下是**可见**的,
    // 翻历史时藏没藏掉才看得出来
    const extra = {};
    // 只按住 `refreshPipe` 那一条。待处理数(`pending=true`)和来源连接探测(带 channel)
    // 走的是同一个路径,按住它们会把摆局卡在别处。
    if (req.url.includes("pending=true")) {
      if (failPending > 0) { failPending--; return json({ detail: "pending unavailable" }, 503); }
      const body = { ...state, last_run: runs[0] || null, result_version: statusVersion(),
                     pending: { extract: pendingExtract } };
      if (holdPending) {
        const gate = holdPending; holdPending = null;
        return void gate.then(() => json(body));
      }
      return json(body);
    }
    const poll = req.url.includes("source=false") && !req.url.includes("pending=true");
    if (holdStatus && poll) {
      const gate = holdStatus; holdStatus = null;
      return void gate.then(() => json({ ...state, last_run: runs[0] || null,
                                         result_version: statusVersion(), ...extra }));
    }
    return json({ ...state, last_run: runs[0] || null, result_version: statusVersion(), ...extra });
  }
  if (path === "/api/pipeline/runs") {
    runsCalls++;
    if (failRuns > 0) { failRuns--; return json({ detail: "history unavailable" }, 503); }
    // 每条记录带一个选择键(真后端如此):面板拿它认点的是哪一行
    const taken = new Set();
    const keyOf = r => {
      const base = r?.run_seq ? `seq-${r.run_seq}` : versionOf(r);
      let key = base, n = 1;
      while (taken.has(key)) key = `${base}#${++n}`;
      taken.add(key);
      return key;
    };
    const body = { runs: Array.isArray(runs)
                     ? runs.map(r => ({ ...r, version: versionOf(r), key: keyOf(r) })) : runs,
                   error: runsError, version: versionOf(runs?.[0]) };
    if (holdRuns) { const gate = holdRuns; holdRuns = null; return void gate.then(() => json(body)); }
    return json(body);
  }
  if (path === "/api/pipeline/retry-failed" && req.method === "POST") {
    retryCalls++; return json({ ok: true });
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
// 等**这一侧**(node 侧)某个事实成立 —— 典型是「那个 hold 被哪个请求消费掉了」,
// 那是服务端这边的事,页面上看不出来。
const untilHere = async (fn, ms = 6000) => {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) { if (fn()) return true; await sleep(100); }
  return false;
};
// 真鼠标点在元素中心:按钮的 disabled / hidden 只有真实事件才算数。
//
// **先滚到它跟前** —— `dispatchMouseEvent` 用的是视口坐标,而历史列表在整页底部
// (日志框自己就 440px 高),不滚的话算出来的 y 落在视口下方,点出去谁也没点到。
//
// **滚完还要做真实命中测试**:算出来的坐标上可能坐着别的东西(浮在页面底部的
// toast 就正好压住历史列表),点出去的那一下落在它身上。这两种情况下
// `getBoundingClientRect()` 照旧给一个有宽高、在视口里的矩形,于是"点了但什么也
// 没发生"会静默绿着 —— 命中测试把它变成红。
const clickAt = async sel => {
  const box = await evalJs(`(() => { const e = document.querySelector('${sel}');
    if (!e) return null;
    e.scrollIntoView({ block: 'center', behavior: 'instant' });
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (y < 0 || y > innerHeight) return null;
    const at = document.elementFromPoint(x, y);
    if (!at || (at !== e && !e.contains(at))) return null;
    return JSON.stringify({ x, y }); })()`);
  if (!box) return false;
  const { x, y } = JSON.parse(box);
  for (const type of ["mousePressed", "mouseReleased"])
    await send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 });
  return true;
};
const exportRow = () => evalJs(
  `document.querySelector('#pipe-stage-export .pipe-stage-detail').textContent`);
const exportStatus = () => evalJs(`$('pipe-stage-export').dataset.status`);
const rows = () => evalJs(
  `[...document.querySelectorAll('.pipe-history-row')].map(e => e.textContent.replace(/\\s+/g, ' ').trim())`);

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride",
           { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: base + "/" });
if (!await until(`typeof refreshPipe === 'function' && typeof switchTab === 'function'`, 20000))
  throw new Error("页面没起来");

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));
const setup = async (name, ok) => { if (!await ok) { fails.push(`摆局没成:${name}`); throw new Error("stop"); } };
try {

// ── 1. 打开同步中心:面板显示最新那一轮,历史列出两轮 ─────────────────────────
await evalJs(`switchTab('pipe'); true`);
await setup("面板显示最新那一轮(绿)", until(`$('pipe-stage-export').dataset.status === 'ok'`));
check("历史列出两轮", await until(`document.querySelectorAll('.pipe-history-row').length === 2`),
      JSON.stringify(await rows()));
const listed = await rows();
check("最新那一轮排在最前", /21:04/.test(listed[0] || ""), listed[0]);
check("上午那一轮写明有步骤需处理", /09:16/.test(listed[1] || "") && /需处理/.test(listed[1] || ""),
      listed[1]);
check("列表上写出了保留几轮", /2 轮/.test(await evalJs(`$('pipe-history-note').textContent`)),
      await evalJs(`$('pipe-history-note').textContent`));

// ── 2. 最新视图下「重试失败项」照旧能点(反过来钉一边) ─────────────────────────
check("最新视图下重试按钮可见", !await evalJs(`$('pipe-retry').hidden`));
check("最新视图下重试按钮能点", await clickAt("#pipe-retry") && retryCalls === 1,
      `retryCalls=${retryCalls}`);
// 那一下弹出的 toast 浮在页面底部(`#toast-host` 是 fixed)、正好压住历史列表,
// 而它几秒后才自己消失。收掉它不是在掩盖什么:上面那道命中测试会把"被挡住"如实
// 报成红,而这一族要验的是翻历史,不是 toast 的停留时长。
// **要等它真的弹出来再收** —— `retryFailed()` 是异步的,点完立刻清是清在回包之前,
// 清了个空,随后那条 toast 照样压上来。
await until(`document.querySelectorAll('.toast').length > 0`, 6000);
await evalJs(`document.querySelectorAll('.toast').forEach(e => e.remove()); true`);

// ── 3. 翻到上午那一轮:阶段卡片换成它那个红,原因读得到 ───────────────────────
await setup("点中了上午那一行", clickAt(".pipe-history-row:nth-child(2)"));
check("整屏换成上午那一轮:导出判红", await until(`$('pipe-stage-export').dataset.status === 'error'`),
      await exportStatus());
check("当时为什么红,读得到", /503/.test(await exportRow()), await exportRow());
check("失败明细也在", /HTTP 503 from the model gateway/.test(
  await evalJs(`$('pipe-stage-export').querySelector('.pipe-failures').textContent`)));
check("那一轮自己的日志", /503/.test(await evalJs(`$('pipe-log').textContent`)));
check("顶上说清这不是最新状态",
      !await evalJs(`$('pipe-viewing').hidden`)
      && /09:16/.test(await evalJs(`$('pipe-viewing-text').textContent`)),
      await evalJs(`$('pipe-viewing-text').textContent`));
check("那一行标成当前在看", await evalJs(
  `document.querySelector('.pipe-history-row:nth-child(2)').classList.contains('on')`));

// ── 4. 翻历史时「重试失败项」按不下去 ────────────────────────────────────────
// 它重试的永远是**最后一轮**的范围,而屏幕上是上午那一轮
// 那句提示也要跟着换成只读语义:最新视图下写的是「可重新配置一次同步,或按上次抓取
// 范围重试失败项」,而这里那颗按钮已经藏掉也禁用了 —— 提示指着一个按不到的操作,说的
// 又是**另一轮**的失败。
check("翻历史时提示换成只读语义",
      /只读/.test(await evalJs(`$('pipe-state-hint').textContent`))
      && !/重试失败项/.test(await evalJs(`$('pipe-state-hint').textContent`)),
      await evalJs(`$('pipe-state-hint').textContent`));
check("翻历史时重试按钮藏起来了", await evalJs(`$('pipe-retry').hidden`));
check("翻历史时重试按钮也是禁用的", await evalJs(`$('pipe-retry').disabled`));
const before = retryCalls;
await clickAt("#pipe-retry");
check("翻历史时点不出一个重试请求", retryCalls === before, `${before} → ${retryCalls}`);
check("此刻的待处理数不贴在上午那一轮旁边",
      await evalJs(`$('pipe-stage-extract').querySelector('.pipe-stage-pending').hidden`));

// ── 5. 轮询不许把屏幕抢回最新 ───────────────────────────────────────────────
for (let i = 0; i < 3; i++) { await evalJs(`refreshPipe(); true`); await sleep(220); }
check("轮询几轮之后还停在上午那一轮", await exportStatus() === "error", await exportStatus());

// ── 6. 回到最新 ─────────────────────────────────────────────────────────────
await setup("点了「回到最新」", clickAt("#pipe-viewing .ghost"));
check("回到最新那一轮", await until(`$('pipe-stage-export').dataset.status === 'ok'`),
      await exportStatus());
check("提示条收起来了", await evalJs(`$('pipe-viewing').hidden`));
check("重试按钮又能点了", !await evalJs(`$('pipe-retry').hidden`)
      && !await evalJs(`$('pipe-retry').disabled`));

// ── 7. 同一行再点一次 = 回到最新 ────────────────────────────────────────────
await setup("又翻到上午那一轮", clickAt(".pipe-history-row:nth-child(2)"));
await setup("确实翻过去了", until(`$('pipe-stage-export').dataset.status === 'error'`));
await setup("再点同一行", clickAt(".pipe-history-row:nth-child(2)"));
check("同一行再点一次就回到最新", await until(`$('pipe-stage-export').dataset.status === 'ok'`),
      await exportStatus());

// ── 8. 正在看的那一轮被挤出记录:退回最新,不留一个指向空处的提示条 ───────────
await setup("再翻到上午那一轮", clickAt(".pipe-history-row:nth-child(2)"));
await setup("确实翻过去了", until(`$('pipe-stage-export').dataset.status === 'error'`));
runs = [EVENING];                       // 它被 _HISTORY_KEEP 挤掉了
await evalJs(`loadPipeHistory(); true`);
check("被挤出记录之后退回最新", await until(`$('pipe-stage-export').dataset.status === 'ok'`),
      await exportStatus());
check("那条提示条跟着收起来", await evalJs(`$('pipe-viewing').hidden`));
check("列表只剩一轮", await evalJs(`document.querySelectorAll('.pipe-history-row').length === 1`));

// ── 9. 历史读不出来时如实说一句,不是空白 ────────────────────────────────────
await evalJs(`pipeHistory = []; $('pipe-history-list').innerHTML = ''; true`);
runs = null;                            // 让回包塞不出 runs 数组
await evalJs(`loadPipeHistory(); true`);
check("读不到历史时列表里有一句说明",
      await until(`/历史记录|还没有跑完过/.test($('pipe-history-list').textContent)`),
      await evalJs(`$('pipe-history-list').textContent.trim()`));

// ── 10. 旧历史回包迟到、期间又跑完一轮:列表要自己追平 ─────────────────────
// 「又跑完一轮」这件事一轮只说一次(`refreshPipe` 的那两个分支)。在途时复用旧
// promise 的话那一次机会就没了:旧回包把列表设成它取到的两轮,而 `pipeDoneRun`
// 已经更新成第三轮,后续轮询再也不进那个分支 —— 列表永远少一轮,得切走再回来。
runs = [EVENING, MORNING];
await evalJs(`pipeHistory = []; pipeViewing = null; true`);
await evalJs(`loadPipeHistory(); true`);
await setup("列表先回到两轮", until(`document.querySelectorAll('.pipe-history-row').length === 2`));
let release;
holdRuns = new Promise(r => { release = r; });
const runsBefore = runsCalls;
await evalJs(`loadPipeHistory(); true`);          // 这一次的回包被按住了
await setup("有一次历史查询卡在途中", until(`pipeHistoryRequest !== null`, 4000));
// 它卡着的这段时间里,第三轮在后台跑完了
runs = [LATER, EVENING, MORNING];
state = { ...state, ...LATER };
await evalJs(`refreshPipe(); true`);
await setup("页面已经看到第三轮(顶上换了)",
            until(`pipeDoneRun === 'run-later' && pipeHistoryRequest !== null`, 8000));
release();
check("旧回包迟到时,历史列表自己追平成三轮",
      await until(`document.querySelectorAll('.pipe-history-row').length === 3`, 8000),
      `rows=${await evalJs(`document.querySelectorAll('.pipe-history-row').length`)}`);
check("追平是靠补一次查询,不是靠下一次切页", runsCalls > runsBefore + 1,
      `${runsBefore} → ${runsCalls}`);
check("第三轮那个红也列出来了",
      /09:12/.test(await evalJs(`document.querySelector('.pipe-history-row').textContent`)),
      await evalJs(`document.querySelector('.pipe-history-row').textContent.replace(/\s+/g, ' ').trim()`));

// ── 11. 还没有快照那一刻又来一次调用:在途那一笔欠账不许被吞掉(机制) ───────
// **这一组摆的是机制,不是生产的首次加载**:它手工清掉快照、手工补调一次
// `loadPipeHistory()`,验的只是「在途期间又来一次调用时,那一笔欠账保不保得住」
// (`runKey` 是空的,身份校验那一道在这一档短路,能救它的只有欠账)。
// **生产的首次加载路径上那第二次调用从哪来**,是另一件事 —— 它来自 `refreshPipe`
// 的 firstRead 分支,由下面第 12、13 组验:那两组整局只有一个真实点击,不碰任何
// 页面变量,也不手工补调。两边都得有,少哪边都会让另一边的红没人接。
runs = [EVENING, MORNING];
// **开局先把列表清空并渲染一次。** 上一组收尾时它正好是三行,不清的话下面那句
// 「追平成三轮」在摆局那一刻就已经为真 —— 这一项会静默绿着,而它本来要验的是
// 「从两行追平到三行」。
await evalJs(`pipeHistory = []; pipeViewing = null; pipeSnapshot = null;
             renderPipeHistory(); true`);
await setup("列表先清空了", until(`document.querySelectorAll('.pipe-history-row').length === 0`));
let release2;
holdRuns = new Promise(r => { release2 = r; });
const runsBefore2 = runsCalls;
await evalJs(`loadPipeHistory(); true`);          // runKey 是空的,回包被按住
await setup("又有一次历史查询卡在途中", until(`pipeHistoryRequest !== null`, 4000));
runs = [LATER, EVENING, MORNING];                 // 这期间第三轮跑完了
await evalJs(`loadPipeHistory(); true`);          // 这一下只能记一笔欠账
release2();
check("没有快照那一刻的欠账也要补上",
      await until(`document.querySelectorAll('.pipe-history-row').length === 3`, 8000),
      `rows=${await evalJs(`document.querySelectorAll('.pipe-history-row').length`)}`);
check("它也是靠补一次查询追平的", runsCalls > runsBefore2 + 1, `${runsBefore2} → ${runsCalls}`);

// ── 12/13 组共用的摆局:刷新页面后立刻打开同步中心,一轮恰好在两个回包之间结束 ──
// **整局只有一个真实动作(点「同步中心」)**,之后全靠页面自己的初始化和回包推进:
// 不给页面变量赋值、不手工补调 `loadPipeHistory()`。这两组要验的恰恰是「生产那条
// 首次加载路径自己会不会补上这一次历史对齐」—— 手工补一次调用就把它验没了。
const freshOpen = async label => {
  // 后端先回到两轮,面板上那一轮是晚上那次
  runs = [EVENING, MORNING];
  state = { running: false, stop_supported: true, stop_requested: false, ...EVENING };
  await evalJs(`window.__beforeReload = 1; true`);
  // **状态那次查询在页面加载时就发出了**,所以这个 hold 必须设在**导航之前**:
  // `initWorkspace` 收尾处本来就有一次 `refreshPipe()`(「别的 tab 或调度器起的活,
  // 在图鉴页也要发现」),而 `refreshPipe` 在途时复用同一个 promise —— 点「同步中心」
  // 不会再发一次状态查询,只会发历史那一条。按住的正是生产里那次首次状态读取。
  let releaseStatus, releaseRuns;
  holdStatus = new Promise(r => { releaseStatus = r; });
  await send("Page.navigate", { url: base + "/" });
  // 判据是「上一张页面留的那个标记没了」—— 只看 `switchTab` 在不在会被旧页面蒙过去
  await setup(`${label}:页面整个重建了`, until(
    `typeof window.__beforeReload === 'undefined' && typeof switchTab === 'function'`, 20000));
  await setup(`${label}:页面加载那次状态查询卡在途中`, untilHere(() => holdStatus === null));
  const before = runsCalls;
  holdRuns = new Promise(r => { releaseRuns = r; });
  await setup(`${label}:点开了同步中心`, clickAt("#tab-pipe"));
  await setup(`${label}:历史查询也卡在途中`, until(`pipeHistoryRequest !== null`, 6000));
  // 历史这一条是**那一下点击**发出来的,不是页面加载时就有的(地址栏里残留的 #pipe
  // 会让 `restoreWorkspaceLocation` 自己打开同步中心,那样这一局的起点就不干净了)
  check(`${label}:历史查询是点开那一下发出来的`, runsCalls === before + 1,
        `${before} → ${runsCalls}`);
  // 真实的初始值如实记一笔:这两组成立的前提就是它 —— 快照和身份都还是初始的 null
  check(`${label}:刚进页面时确实什么都还没有`, await evalJs(
    `pipeSnapshot === null && pipeDoneRun === null && pipeHistoryWanted === false`),
    await evalJs(`JSON.stringify({ snap: pipeSnapshot, done: pipeDoneRun, wanted: pipeHistoryWanted })`));
  // 卡着的这段时间里,第三轮在后台跑完了
  runs = [LATER, EVENING, MORNING];
  state = { ...state, ...LATER };
  return { releaseStatus, releaseRuns, before };
};

// ── 12. 全新页面、状态先到:首次那一次读取也要去对齐历史 ─────────────────────
{
  const { releaseStatus, releaseRuns, before } = await freshOpen("状态先到");
  releaseStatus();
  await setup("状态先到:顶上换成了第三轮", until(`pipeDoneRun === 'run-later'`, 8000));
  // 历史这会儿还在途,首次那一次对齐只能先记一笔欠账
  check("状态先到:首次读取登记了一次历史补刷", await evalJs(`pipeHistoryWanted === true`),
        await evalJs(`JSON.stringify({ wanted: pipeHistoryWanted, inflight: pipeHistoryRequest !== null })`));
  releaseRuns();
  check("状态先到:历史列表自己补上了第三轮",
        await until(`document.querySelectorAll('.pipe-history-row').length === 3`, 8000),
        `rows=${await evalJs(`document.querySelectorAll('.pipe-history-row').length`)}`);
  check("状态先到:补齐不靠切走再回来", runsCalls > before + 1, `${before} → ${runsCalls}`);
  check("状态先到:第三轮那个红就在列表第一行",
        /09:12/.test(await evalJs(`document.querySelector('.pipe-history-row')?.textContent || ''`)),
        await evalJs(`(document.querySelector('.pipe-history-row')?.textContent || '').replace(/\s+/g, ' ').trim()`));
}

// ── 13. 全新页面、历史先到:在途已经空了,这一次对齐得自己发一次查询 ───────────
// 这一档单靠「去掉历史那边的空身份短路」治不了 —— 旧回包落地的时候快照还是 null,
// 拿什么都比不出来。
{
  const { releaseStatus, releaseRuns, before } = await freshOpen("历史先到");
  releaseRuns();
  await setup("历史先到:列表先按旧回包显示两轮", until(
    `document.querySelectorAll('.pipe-history-row').length === 2 && pipeHistoryRequest === null`, 8000));
  releaseStatus();
  check("历史先到:历史列表自己补上了第三轮",
        await until(`document.querySelectorAll('.pipe-history-row').length === 3`, 8000),
        `rows=${await evalJs(`document.querySelectorAll('.pipe-history-row').length`)}`);
  check("历史先到:补齐不靠切走再回来", runsCalls > before + 1, `${before} → ${runsCalls}`);
}

// ── 14. 反过来钉一边:这一次对齐只发生在首次,之后每轮轮询不许都多查一次 ───────
// `pipeDoneRun === null` 按定义只有一次,但把条件写宽(比如漏掉 firstRead 那一档、
// 改成每次都调)也能让上面两组绿 —— 那样空闲时每 6 秒多读一次历史,白费。
const quiet = runsCalls;
await evalJs(`refreshPipe(); true`);
await sleep(500);
await evalJs(`refreshPipe(); true`);
await sleep(500);
check("状态没变时后续轮询不再重复查历史", runsCalls === quiet, `${quiet} → ${runsCalls}`);

// ── 15. 那一次历史补刷失败:缺口要留着、靠退避补回来、面板上还得说一句 ─────────
// 原来这一档什么都不记:后续轮询只知道「这一轮见过了」、不知道「屏幕上还没对上」,
// 列表长期停在旧那份清单,等多久都不补,页面上一个字都不说(实测后端 6 轮、页面 5 行,
// 切走再回来才补齐)。所以判据有三条:**说了没有、补没补回来、靠的是不是退避补读**。
runs = [EVENING, MORNING];
state = { running: false, stop_supported: true, stop_requested: false, ...EVENING };
await evalJs(`pipeHistory = []; pipeViewing = null; renderPipeHistory(); true`);
await evalJs(`refreshPipe(); true`);
await setup("列表先追平成两轮", until(`document.querySelectorAll('.pipe-history-row').length === 2`));

failRuns = 1;                                 // 这一轮唯一的那次补刷会 503
runs = [LATER, EVENING, MORNING];
state = { ...state, ...LATER };
const runsBefore4 = runsCalls;
await evalJs(`refreshPipe(); true`);          // newResult → 补刷 → 503
await setup("那次补刷确实失败了", until(`pipeFreshGaps().length === 1`, 8000));
check("顶上已经换成第三轮了", await evalJs(`pipeDoneRun === 'run-later'`));
check("读失败时列表如实停在旧那两行",
      await evalJs(`document.querySelectorAll('.pipe-history-row').length === 2`),
      `rows=${await evalJs(`document.querySelectorAll('.pipe-history-row').length`)}`);
check("面板上如实说了一句这块是旧的",
      await evalJs(`!$('pipe-fresh').hidden && /历史运行/.test($('pipe-fresh-text').textContent)`),
      await evalJs(`JSON.stringify({ hidden: $('pipe-fresh').hidden, text: $('pipe-fresh-text').textContent })`));

await evalJs(`refreshPipe(); true`);          // 退避到点,这一轮补读
check("后续轮询把它补回来了",
      await until(`document.querySelectorAll('.pipe-history-row').length === 3`, 8000),
      `rows=${await evalJs(`document.querySelectorAll('.pipe-history-row').length`)}`);
check("补回来之后那句提示撤掉", await until(`$('pipe-fresh').hidden`, 4000),
      await evalJs(`JSON.stringify({ hidden: $('pipe-fresh').hidden, gaps: pipeFreshGaps().length })`));
check("补齐靠的是多读一次,不是下一次切页", runsCalls > runsBefore4 + 1,
      `${runsBefore4} → ${runsCalls}`);

// ── 16. 待处理数那条是同一个形状(对称实例) ─────────────────────────────────────
// 它那条 catch 原来也是「清掉欠账、保留旧值、什么都不说」。两者走同一套,所以各摆一次。
failPending = 1;
runs = [FOURTH, LATER, EVENING, MORNING];     // 第四轮跑完了
state = { ...state, ...FOURTH };
await evalJs(`refreshPipe(); true`);          // newResult → 待处理数那次 503、历史照旧
await setup("待处理数那次失败了、而历史追平了",
            until(`pipeFreshGaps().length === 1
                   && document.querySelectorAll('.pipe-history-row').length === 4`, 8000));
check("待处理数读失败时也如实说了一句",
      await evalJs(`!$('pipe-fresh').hidden && /待处理数/.test($('pipe-fresh-text').textContent)`),
      await evalJs(`$('pipe-fresh-text').textContent`));
await evalJs(`refreshPipe(); true`);
check("待处理数也是靠退避补回来的", await until(`$('pipe-fresh').hidden`, 8000),
      await evalJs(`$('pipe-fresh-text').textContent`));

// ── 17. 后端留档失败要如实说,但不许声张成同步失败 ─────────────────────────────
runsError = "UnicodeDecodeError";
await evalJs(`loadPipeHistory(); true`);
await setup("那条诊断传到前端了", until(`pipeFreshNote === 'UnicodeDecodeError'`, 8000));
check("留档失败在面板上说了一句",
      await evalJs(`!$('pipe-fresh').hidden && /没能留档/.test($('pipe-fresh-text').textContent)`),
      await evalJs(`$('pipe-fresh-text').textContent`));
check("说清了是什么坏了",
      /UnicodeDecodeError/.test(await evalJs(`$('pipe-fresh-text').textContent`)));
// **反过来钉一边**:留档失败不许改写那一轮的结论
check("同步结论没被改写", await exportStatus() === "ok" && /12 条/.test(await exportRow()),
      `${await exportStatus()} / ${await exportRow()}`);
runsError = "";
check("「重新读取」真会再查一次", await (async () => {
  const before = runsCalls;
  if (!await clickAt("#pipe-fresh button")) return false;
  return await until(`$('pipe-fresh').hidden`, 8000) && runsCalls > before;
})(), `runsCalls=${runsCalls}`);

// ── 18. 旧的待处理数回包迟到:不许拿此刻的页面快照给它盖章 ────────────────────
// 这一局整个是报告那条的形状:全新页面、快照还是 null 时发出 pending 查询(于是回包
// 带的是**上一轮**的身份和数字),按住它,这期间新一轮跑完把它清成 0,再放行。
// 原来的判据是「发请求前记下的快照身份为空就放行」+「成功后拿当前快照盖章」——
// 旧那个 1 被应用上去,还被标成「已追平最新那一轮」:缺口不存在、提示不出现、补读
// 也不会发生,面板永久写着「还有 1 项内容待提取」,切走再回来才归零。
runs = [EVENING, MORNING];
state = { running: false, stop_supported: true, stop_requested: false, ...EVENING };
pendingExtract = 1;                           // 上一轮留下 1 项待提取
await evalJs(`window.__beforeReload = 1; true`);
let releaseStatus3, releasePending;
holdStatus = new Promise(r => { releaseStatus3 = r; });
holdPending = new Promise(r => { releasePending = r; });
await send("Page.navigate", { url: base + "/" });
await setup("页面整个重建了", until(
  `typeof window.__beforeReload === 'undefined' && typeof switchTab === 'function'`, 20000));
await setup("页面加载那次状态查询卡在途中", untilHere(() => holdStatus === null));
await setup("点开了同步中心", clickAt("#tab-pipe"));
await setup("待处理数那次查询也卡在途中(带的是上一轮的身份)",
            untilHere(() => holdPending === null));
check("这一刻确实还没有快照", await evalJs(`pipeSnapshot === null`),
      await evalJs(`JSON.stringify({ snap: pipeSnapshot })`));

// 这期间第三轮跑完了,待处理数被清成 0
runs = [LATER, EVENING, MORNING];
state = { ...state, ...LATER };
pendingExtract = 0;
releaseStatus3();
await setup("顶上换成了第三轮", until(`pipeVersion(pipeSnapshot) === 'run-later'`, 8000));
releasePending();                             // 旧回包这才落地

check("旧那份待处理数不许被应用上去",
      await until(`pipePending.extract === 0`, 8000),
      await evalJs(`JSON.stringify({ pending: pipePending, applied: PIPE_FRESH.pending.applied,
                                     desired: PIPE_FRESH.pending.desired })`));
check("面板上那行字也跟着归零了",
      !/待处理 1 项/.test(await evalJs(`$('pipe-issues')?.textContent || ''`)),
      await evalJs(`($('pipe-issues')?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60)`));
check("追平之后 applied 是这一轮的身份、不是旧回包那一轮",
      await evalJs(`PIPE_FRESH.pending.applied === 'run-later'
                    && PIPE_FRESH.pending.applied === PIPE_FRESH.pending.desired`),
      await evalJs(`JSON.stringify({ applied: PIPE_FRESH.pending.applied, desired: PIPE_FRESH.pending.desired })`));

// ── 19. 历史清单落后于当前这一轮、而后端没报错:也要算落后 ─────────────────────
// 这一条钉 history 侧的凭据。上一组钉的是 pending:那边的「对不上就不应用」已经
// 挡住了旧回包,所以**盖章用什么都一样**(定点注入只换盖章红 0 项,真冗余)。history
// 不同:它没有「清单身份必须等于快照身份」这道条件(留档失败那一档本来就等不了),
// 于是盖章是唯一的凭据 —— 拿此刻的页面快照盖的话,一份少了最新那一轮的清单就成了
// 「已追平」,补读再也不会发生。
runs = [EVENING, MORNING];                    // 清单落后一轮,而 state 还是第三轮
runsError = "";
await evalJs(`loadPipeHistory(); true`);
await setup("那份落后的清单已经应用上去了",
            until(`document.querySelectorAll('.pipe-history-row').length === 2`, 8000));
check("applied 记的是清单那一轮,不是页面快照那一轮",
      await evalJs(`PIPE_FRESH.history.applied === 'run-evening'`),
      await evalJs(`JSON.stringify({ applied: PIPE_FRESH.history.applied,
                                     desired: PIPE_FRESH.history.desired })`));
check("所以它算落后、补读还会继续",
      await evalJs(`PIPE_FRESH.history.applied !== PIPE_FRESH.history.desired`),
      await evalJs(`PIPE_FRESH.history.applied + ' vs ' + PIPE_FRESH.history.desired`));

// ── 20. 升级前落的记录没有身份:照旧要追得平 ─────────────────────────────────
// `run_id` 是后来才加的字段。applied 原来取清单首条的 `run_id`,碰上这种记录就是
// 空串 —— 缺口永远在、补读永远继续,而清单其实已经是最新的那一份了。版本改由后端
// 给之后,这一档降级到收尾时间。
const OLD_A = { ...EVENING }, OLD_B = { ...MORNING };
for (const r of [OLD_A, OLD_B]) { delete r.run_id; delete r.run_seq; }
OLD_B.finished_at = OLD_A.finished_at;        // 同一秒 —— 完成版本就此撞上
runs = [OLD_A, OLD_B];
state = { ...state, run_id: "", run_seq: 0, finished_at: OLD_A.finished_at,
          stopped: false, stages: OLD_A.stages, log: OLD_A.log };
// **开局先清到已知状态**:上一组收尾时列表也是两行,不清的话下面那道 setup 在摆局
// 那一刻就已为真,追平那一项会判在旧 DOM 上(两次跑诊断值还不一样,因为诊断是
// 判据之后第二次求值 —— 那时新清单往往已经应用上了,于是红得像偶发)
await evalJs(`pipeHistory = []; renderPipeHistory(); true`);
await setup("列表没清空(开局得是已知状态)",
            until(`document.querySelectorAll('.pipe-history-row').length === 0`, 6000));
await evalJs(`refreshPipe(); loadPipeHistory(); true`);
// 等「状态查询回来了」是摆局前提,「清单追平到它」才是下面要验的
await setup("那份旧格式清单没应用上去",
            until(`document.querySelectorAll('.pipe-history-row').length === 2
                   && PIPE_FRESH.history.desired === ${JSON.stringify(OLD_A.finished_at)}`, 8000));
check("没有身份的记录按收尾时间追平,不留一个永久缺口", await evalJs(
  `PIPE_FRESH.history.applied === PIPE_FRESH.history.desired
   && PIPE_FRESH.history.applied === ${JSON.stringify(OLD_A.finished_at)}`),
  await evalJs(`PIPE_FRESH.history.applied + ' vs ' + PIPE_FRESH.history.desired`));
// 选择键得逐行不同:原来按 `run_id`(这种记录属性里是空字符串,点下去要求
// `undefined === ''`,那一行看得到却打不开),换成完成版本之后这两行又会**撞**。
check("两行的选择键不一样", await evalJs(
  `(() => { const k = [...document.querySelectorAll('.pipe-history-row')]
      .map(e => e.dataset.run); return k.length === 2 && k[0] && k[1] && k[0] !== k[1]; })()`),
  await evalJs(`[...document.querySelectorAll('.pipe-history-row')]
                 .map(e => e.dataset.run).join(' | ')`));
// 点**第二行**:键撞的话查找只找得到第一条,打开的就是另一轮
check("点第二行打开的是第二行(不是撞上的第一行)",
      await clickAt(".pipe-history-row:nth-of-type(2)")
      && await until(`pipeViewing && pipeViewing.started_at === ${JSON.stringify(OLD_B.started_at)}`, 4000),
      await evalJs(`JSON.stringify(pipeViewing && pipeViewing.started_at)`));
await evalJs(`exitPipeHistory(); true`);

// ── 21. 一条坏记录只许坏它自己那一行 ────────────────────────────────────────
// 这些字段全来自落盘文件,形状没有任何保证(`finished_at` 落成数字时 `slice` 当场
// 抛 TypeError)。原来是一次 map 渲染整表,于是一条坏记录把整屏历史换成一句
// 「没能读到历史记录」,连那些好的一起 —— 而文件其实读到了。
await evalJs(`pipeHistory = []; renderPipeHistory(); true`);
await setup("列表没清空(开局得是已知状态)",
            until(`document.querySelectorAll('.pipe-history-row').length === 0`, 6000));
runs = [{ ...EVENING, finished_at: 20261014, started_at: 20261014 }, MORNING];
await evalJs(`loadPipeHistory(); true`);
const twoRows = await until(
  `document.getElementById('pipe-history-list').children.length === 2`, 8000);
check("好的那一行照旧渲染得出来", twoRows && await evalJs(
  `[...document.querySelectorAll('.pipe-history-row')].length === 1`),
  await evalJs(`document.getElementById('pipe-history-list').innerHTML.slice(0, 120)`));
check("坏的那一行就地说明,不替换整屏", twoRows && await evalJs(
  `document.getElementById('pipe-history-list').textContent.includes('有一轮记录读不出来')`),
  await evalJs(`document.getElementById('pipe-history-list').textContent.slice(0, 160)`));

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));

} catch (e) {
  if (e.message !== "stop") fails.push(`探针中断 — ${e.message}`);
}

const out = [...oks.map(n => `  ✓ ${n}`), ...fails.map(n => `  ✗ ${n}`)].join("\n");
// writeSync 而不是 process.exit:node 对管道上的 stdout 是异步写的,exit 会把没刷
// 出去的直接丢掉(见 docs/decisions/frontend-feedback.md)
const text = `${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`;
let off = 0;
while (off < Buffer.byteLength(text)) {
  try { off += writeSync(1, text.slice(off)); }
  catch (e) { if (e.code !== "EAGAIN") throw e; }
}
ws.close(); proc.kill(); server.close();
process.exitCode = fails.length ? 1 : 0;
