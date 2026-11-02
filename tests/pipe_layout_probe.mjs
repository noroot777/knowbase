// 同步中心的**版面**回归:轮次列和被它改写的内容在不在同一屏里。
//
// `pipe_history_probe.mjs` 验的是**行为**(翻到哪一轮、那一轮的红读不读得到、重试按
// 不按得下去);这一份验的是**几何** —— 点一行之后屏幕上看不看得见变化。两件事分开,
// 因为那份探针贴着 700 行上限,而且这一份要换视口、改设备度量,和它那套状态机无关。
//
// 来由:点一行走的是 `viewPipeRun` → `renderPipeState`,**整屏**(概况卡、失败明细、进度、
// 阶段、日志)按那一轮重渲染 —— 它是个视图切换器。最早轮次列待在页尾,1440×900 实测那一行
// 在 y≥1047、被它改写的概况卡在 y=84,**点下去屏幕上什么都看不见**。现在历史是概况卡下面
// 那张整宽的表(一行一轮、一列一类,`pipe_matrix.js`,形态见 `docs/decisions/sync-flow.md`),
// 中间那一版「并排 + sticky」随之作废。
//
// **判据落在后果上,不落在「DOM 挂在哪个父节点下」**:点下去之后概况卡的标题和「正在查看」
// 那句话(退出入口就在它上面)在视口里。**两条路各钉一次**:标题本来就在眼前时页面一动
// 不动(`preventScroll`),被滚出去时靠 `viewPipeRun` 末尾那次 `focus()` 带回来 —— 后一档
// 要矮视口才摆得出来(1000 高时整页只比视口长几十像素,标题怎么滚都在眼前)。
//
// 自带只读 mock 后端(没有状态机:一份固定的两轮留档 + 静态回包),CDP 直连 playwright
// 缓存里的 chrome-headless-shell(不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/pipe_layout_probe.mjs
import { createServer } from "node:http";
import { readFileSync, writeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { attachCdp, sleep } from "./pipe_history_cdp.mjs";
import { MORNING, EVENING, MIME, FIXTURES } from "./pipe_history_fixture.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
// 倒序,和真后端一样:晚上那一轮在前,上午那个红在后
const RUNS = [EVENING, MORNING];
const versionOf = r => String((r && (r.run_id || r.finished_at)) || "") || "none";
// 非空 = 此刻有一轮在跑,值就是它的身份(换一个值 = 又起了新的一轮)
let liveRun = "";

const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const json = data => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(200, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  if (path === "/api/pipeline/status") {
    // 跑着的时候前端渲染的是**顶层这一份**(`pipeResult`),所以把正在跑那一轮摊进来,
    // 和真后端一样;`result_version` 报的也是它
    const live = liveRun
      ? { ...RUNS[0], run_id: liveRun, finished_at: "", log: ["[10-14 21:03] 正在导出…"] }
      : {};
    return json({ running: !!liveRun, stop_requested: false, stopped: false,
                  stop_supported: true, ...live, last_run: RUNS[0],
                  log: liveRun ? live.log : [],
                  result_version: liveRun || versionOf(RUNS[0]) });
  }
  if (path === "/api/pipeline/runs") {
    // `keep_days` 照真后端给:面板那句「保留最近 N 天」要念得出来
    // `is_current` 照真后端给:它过的是 `same_event(行, last_result())`,而这边状态
    // 回包的 `last_run` 就是 `RUNS[0]` —— 按**身份**相等算,不按下标
    return json({ runs: RUNS.map(r => ({ ...r, version: versionOf(r), key: versionOf(r),
                                         invalid: false,
                                         is_current: r.run_id === RUNS[0].run_id })),
                  error: "", version: versionOf(RUNS[0]), keep_days: 3 });
  }
  if (FIXTURES[path]) return json(FIXTURES[path]);
  if (path.startsWith("/api/")) return json({});
  // 静态文件:`normalize` 之后必须还在 STATIC 底下(别让 `..` 走出去)
  const file = normalize(join(STATIC, path === "/" ? "index.html" : path.replace(/^\/static/, "")));
  if (!file.startsWith(STATIC)) { res.writeHead(403); return res.end(); }
  try {
    const body = readFileSync(file);
    res.writeHead(200, { "Content-Type": MIME[file.split(".").pop()] || "application/octet-stream",
                         "Content-Length": body.length });
    res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(r => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}`;

const { proc, ws, send, evalJs, until, clickAt, pageErrors } = await attachCdp();
const oks = [], fails = [];
const check = (name, pass, info = "") => (pass ? oks : fails).push(pass ? name : `${name} — ${info}`);
const setup = async (name, promise) => {
  if (await promise) return;
  fails.push(`摆局没成:${name}`);
  throw new Error("stop");
};

try {
await send("Page.navigate", { url: `${base}/#pipe` });
await setup("面板加载出两轮留档",
            until(`document.querySelectorAll('.pipe-history-row').length === 2`, 8000));

// 点**那一行的时间格**:历史是一张一列一类的表,点在类型那几格上会顺手选中那一类
// (`pipeMatrixPick`)。这里要的是「换一轮」本身,不挑类型。
const ROW2 = "#pipe-history-list .pipe-history-row:nth-child(2) .pipe-history-when";
const sight = () => evalJs(`(() => {
  const vis = s => { const b = document.querySelector(s).getBoundingClientRect();
                     return b.height > 0 && b.top >= 0 && b.bottom <= innerHeight; };
  return { title: vis('#pipe-state-title'), say: vis('#pipe-viewing'), scrolled: Math.round(scrollY),
           overflowX: document.documentElement.scrollWidth > innerWidth };
})()`);

// ── 1. 点一行:被改写的那块和退出入口落在眼前 ─────────────────────────────────
await setup("点中上午那一行", clickAt(ROW2));
await setup("整屏换成上午那一轮", until(`$('pipe-stage-export').dataset.status === 'error'`));
const wide = await sight();
check("宽屏:点下去概况卡标题和「正在查看」都在视口里", wide.title && wide.say, JSON.stringify(wide));
// 「要不要滚」得用**不自己滚的那种点击**来量:`clickAt` 为了命中先做一次
// `scrollIntoView({ block: 'center' })`,拿它量出来的 scrollY 是探针自己滚的,
// 不是页面滚的。所以这一条单独用 DOM click,从 scrollY=0 起量。
await evalJs(`exitPipeHistory(); scrollTo(0, 0); true`);
await sleep(200);
await evalJs(`document.querySelector(${JSON.stringify(ROW2)}).click(); true`);
await setup("DOM click 也翻得过去", until(`$('pipe-stage-export').dataset.status === 'error'`));
await sleep(400);                      // focus() 的平滑滚动要是真发生了,给它时间发生
const quiet = await sight();
check("标题本来就在眼前时点下去页面一动不动",
      quiet.scrolled === 0 && quiet.title && quiet.say, JSON.stringify(quiet));
await evalJs(`scrollTo(0, 0); exitPipeHistory(); true`);
await sleep(200);

// ── 2. 失败明细只有一份:概况卡只说哪几步,不抄它们的 detail ──────────────────
await setup("再翻到上午那一轮", clickAt(ROW2));
await setup("整屏又换过去了", until(`$('pipe-stage-export').dataset.status === 'error'`));
const dedup = await evalJs(`({
  summary: $('pipe-issues-list').textContent.replace(/\\s+/g, ' ').trim(),
  stageDetail: $('pipe-stage-export').querySelector('.pipe-stage-detail').textContent,
  jumpShown: !$('pipe-issues-jump').hidden })`);
check("概况卡点名了是哪一步", /导出文章/.test(dedup.summary), dedup.summary);
check("但没把那一步的 detail 再抄一遍",
      !dedup.summary.includes('503') && dedup.stageDetail.includes('503'),
      `${dedup.summary} || ${dedup.stageDetail}`);
check("翻历史时「看第一个」照旧给(它只滚动和展开)", dedup.jumpShown);
await evalJs(`$('pipe-issues-jump').click(); true`);
await sleep(700);
const jumped = await evalJs(`(() => {
  const row = $('pipe-stage-export'), b = row.getBoundingClientRect();
  return { inView: b.top >= 0 && b.bottom <= innerHeight, open: row.querySelector('details').open };
})()`);
check("「看第一个」把那一步带进视口并展开它的明细",
      jumped.inView && jumped.open, JSON.stringify(jumped));

// ── 3. 保留口径由后端给,面板照它念 ───────────────────────────────────────────
check("面板按后端报的天数念「保留最近 3 天」",
      /保留最近 3 天/.test(await evalJs(`$('pipe-history-note').textContent`))
      && /2 轮/.test(await evalJs(`$('pipe-history-note').textContent`)),
      await evalJs(`$('pipe-history-note').textContent`));
// 反过来钉一边:后端没报就不许编一个数字出来(老后端、别的探针摆的桩)
await evalJs(`pipeKeepDays = 0; renderPipeHistory(); true`);
check("后端没报时只报轮数,不编天数",
      !/天/.test(await evalJs(`$('pipe-history-note').textContent`)),
      await evalJs(`$('pipe-history-note').textContent`));

// ── 4. 日志默认收起,不占整页的视觉重心 ───────────────────────────────────────
check("空闲时日志是收起的", await evalJs(`$('pipe-log-panel').open === false`));
check("收起时那一轮的日志照旧在 DOM 里(翻历史读得到)",
      /503/.test(await evalJs(`$('pipe-log').textContent`)),
      await evalJs(`$('pipe-log').textContent.slice(0, 80)`));

// ── 4b. 跑着的时候自动展开一次,**翻一趟历史不算又起了一轮** ─────────────────
//
// 自动展开记的是「哪一轮已经展开过」,不是一个布尔:布尔那一版的重置条件是「此刻这
// 一屏不在跑」,而翻历史也满足它 —— 手动收起、翻一轮、回到最新,日志又被顶开(第 1
// 轮审查实测 `open` 从 null 变回 "")。翻历史不是新起了一轮。
// **先退出只读。** 上一节点开过上午那一轮,而翻历史时这一栏按只读渲染
// (`syncPipeLogPanel` 收到的 running 是 false),不退出的话摆不出「跑着的时候」。
await evalJs(`exitPipeHistory(); true`);
liveRun = "run-live";
await evalJs(`refreshPipe()`);
await setup("摆出「有一轮在跑」", until(`$('pipe-log-panel').open === true`));
await evalJs(`$('pipe-log-panel').open = false; true`);          // 用户自己收起来
await setup("翻到上午那一轮", clickAt(ROW2));
await evalJs(`exitPipeHistory(); refreshPipe()`);
await sleep(200);
check("跑着的时候收起日志,翻一趟历史回来它还是收着的",
      await evalJs(`$('pipe-log-panel').open === false`),
      await evalJs(`String($('pipe-log-panel').open)`));
// 反过来钉一边:真又起了一轮(身份变了)照旧自动展开,别把判据钉成「只展开一次」
liveRun = "run-live-2";
await evalJs(`refreshPipe()`);
await sleep(200);
check("真起了新的一轮,日志照旧自动展开一次",
      await evalJs(`$('pipe-log-panel').open === true`),
      await evalJs(`String($('pipe-log-panel').open)`));
liveRun = "";
await evalJs(`refreshPipe(); exitPipeHistory(); scrollTo(0, 0); true`);
await sleep(200);

// ── 5. 反过来钉一边:标题被滚出视口时,focus 把视线带回被改写的那块 ─────────────
//
// 先滚到页底(人是在表上点的),这时概况卡标题在视口上方。两个宽度各量一次:窄一档
// 卡片换行更多、更高,同一句 `preventScroll` 判据要在两种卡高下都成立。
for (const width of [1100, 820]) {
  await evalJs(`exitPipeHistory(); true`);
  await send("Emulation.setDeviceMetricsOverride",
             { width, height: 600, deviceScaleFactor: 1, mobile: false });
  await sleep(320);
  await evalJs(`scrollTo(0, document.documentElement.scrollHeight); true`);
  await sleep(200);
  await setup(`${width}×600 下标题先被滚出视口`, evalJs(
    `$('pipe-state-title').getBoundingClientRect().bottom < 0`));
  await setup(`${width} 下再点那一行`, clickAt(ROW2));
  await setup(`${width} 也换过去了`, until(`$('pipe-stage-export').dataset.status === 'error'`));
  await sleep(400);
  const narrow = await sight();
  check(`${width}:靠 focus 把标题和「正在查看」带回视口`, narrow.title && narrow.say, JSON.stringify(narrow));
  check(`${width}:那张表不把整页撑出横向滚动`, !narrow.overflowX, JSON.stringify(narrow));
}
await send("Emulation.clearDeviceMetricsOverride");

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));

} catch (e) {
  if (e.message !== "stop") fails.push(`探针中断 — ${e.message}`);
}

const out = [...oks.map(n => `  ✓ ${n}`), ...fails.map(n => `  ✗ ${n}`)].join("\n");
// writeSync 而不是 process.exit:node 对管道上的 stdout 是异步写的,exit 会把没刷出去的丢掉
const text = `${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`;
let off = 0;
while (off < Buffer.byteLength(text)) {
  try { off += writeSync(1, text.slice(off)); }
  catch (e) { if (e.code !== "EAGAIN") throw e; }
}
ws.close(); proc.kill(); server.close();
process.exitCode = fails.length ? 1 : 0;
