// 同步中心的**版面**回归:轮次列和被它改写的内容在不在同一屏里。
//
// `pipe_history_probe.mjs` 验的是**行为**(翻到哪一轮、那一轮的红读不读得到、重试按
// 不按得下去);这一份验的是**几何** —— 点一行之后屏幕上看不看得见变化。两件事分开,
// 因为那份探针贴着 700 行上限,而且这一份要换视口、改设备度量,和它那套状态机无关。
//
// 来由:轮次列原来待在页尾。点一行走的是 `viewPipeRun` → `renderPipeState`,**整屏**
// (概况卡、失败明细、进度、阶段、日志)按那一轮重渲染 —— 它是个视图切换器。1440×900
// 实测那一行在 y≥1047、被它改写的概况卡在 y=84,**点下去屏幕上什么都看不见**;退出
// 入口(`#pipe-viewing`)又在页顶,一次「这轮不对,看上一轮」是两趟整页滚动。
//
// **判据落在后果上,不落在「DOM 挂在哪个父节点下」** —— 搬了位置而没解决同屏,那一条
// 照旧该红。**反过来钉一边**:窄屏单列时并排不可能,那一档改为要求 `focus()` 真把视线
// 带到被改写的那块。
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

// ── 1. 并排那一档:点一行,被点的行和「这是哪一轮」那句话同屏 ──────────────────
await setup("点中上午那一行", clickAt(".pipe-history-row:nth-child(2)"));
await setup("整屏换成上午那一轮", until(`$('pipe-stage-export').dataset.status === 'error'`));
const wide = await evalJs(`(() => {
  const row = document.querySelector('.pipe-history-row:nth-child(2)').getBoundingClientRect();
  const say = document.querySelector('#pipe-viewing').getBoundingClientRect();
  const vis = b => b.top >= 0 && b.bottom <= innerHeight;
  return { row: vis(row), say: vis(say), scrolled: Math.round(scrollY),
           sideBySide: getComputedStyle(document.querySelector('.pipe-workbench'))
             .gridTemplateColumns.split(' ').length > 1 };
})()`);
check("宽屏:轮次列和步骤并排", wide.sideBySide, JSON.stringify(wide));
check("宽屏:被点的那一行和「正在查看」那句话同屏可见", wide.row && wide.say, JSON.stringify(wide));
// 「要不要滚」得用**不自己滚的那种点击**来量:`clickAt` 为了命中先做一次
// `scrollIntoView({ block: 'center' })`,拿它量出来的 scrollY 是探针自己滚的
// (实测 80),不是页面滚的。所以这一条单独用 DOM click,从 scrollY=0 起量。
await evalJs(`exitPipeHistory(); scrollTo(0, 0); true`);
await sleep(200);
await evalJs(`document.querySelector('.pipe-history-row:nth-child(2)').click(); true`);
await setup("DOM click 也翻得过去", until(`$('pipe-stage-export').dataset.status === 'error'`));
await sleep(400);                      // focus() 的平滑滚动要是真发生了,给它时间发生
const quiet = await evalJs(`(() => {
  const row = document.querySelector('.pipe-history-row:nth-child(2)').getBoundingClientRect();
  const say = document.querySelector('#pipe-viewing').getBoundingClientRect();
  const vis = b => b.top >= 0 && b.bottom <= innerHeight;
  return { scrolled: Math.round(scrollY), row: vis(row), say: vis(say) };
})()`);
check("并排那一档点下去页面一动不动(两边本来就都在眼前)",
      quiet.scrolled === 0 && quiet.row && quiet.say, JSON.stringify(quiet));

// 那一列要钉住:展开一条问题明细能把左边拉长几百像素,选择器不能跟着滚出去
await evalJs(`document.querySelectorAll('#pipe-stages details').forEach(d => { if (!d.hidden) d.open = true; });
  scrollTo(0, document.documentElement.scrollHeight); true`);
await sleep(320);
const pinned = await evalJs(`(() => {
  const card = document.querySelector('.pipe-history-panel').getBoundingClientRect();
  return { visible: card.top < innerHeight && card.bottom > 0, scrolled: Math.round(scrollY),
           position: getComputedStyle(document.querySelector('.pipe-history-panel')).position };
})()`);
check("滚到底之后轮次列还钉在视口里",
      pinned.position === 'sticky' && pinned.visible && pinned.scrolled > 0, JSON.stringify(pinned));
await evalJs(`scrollTo(0, 0); exitPipeHistory(); true`);
await sleep(200);

// ── 2. 失败明细只有一份:概况卡只说哪几步,不抄它们的 detail ──────────────────
await setup("再翻到上午那一轮", clickAt(".pipe-history-row:nth-child(2)"));
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
await setup("翻到上午那一轮", clickAt(".pipe-history-row:nth-child(2)"));
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

// ── 5. 反过来钉一边:窄屏单列,选择器排在步骤之前,focus 把视线带过去 ─────────
//
// **两个宽度都要量,而且 1100 这一档是有来由的**:塌列原来写在 `@media (max-width:
// 1180px)`,而「排到上面 + 钉住关掉」写在另一个更窄的断点里 —— 两条都命中时后者赢,
// 于是 1000~1180 这一段**列已经塌成单列、选择器却还钉着、还排在它改写的内容后面**,
// 正是「单列下它反过来挡住内容」那一档。只量 820 的话这一段一个字都测不到:实测定点
// 注入(把窄屏那条 grid 改回两列)时探针**全绿**,因为 820 那一档被另一条规则兜住了。
for (const width of [1100, 820]) {
  await evalJs(`exitPipeHistory(); scrollTo(0, 0); true`);
  await send("Emulation.setDeviceMetricsOverride",
             { width, height: 900, deviceScaleFactor: 1, mobile: false });
  await sleep(320);
  await setup(`窄屏 ${width} 下再点那一行`, clickAt(".pipe-history-row:nth-child(2)"));
  await setup(`窄屏 ${width} 也换过去了`, until(`$('pipe-stage-export').dataset.status === 'error'`));
  const narrow = await evalJs(`(() => {
    const t = $('pipe-state-title').getBoundingClientRect();
    const y = s => Math.round(document.querySelector(s).getBoundingClientRect().top + scrollY);
    return { oneColumn: getComputedStyle(document.querySelector('.pipe-workbench'))
               .gridTemplateColumns.split(' ').length === 1,
             selectorFirst: y('.pipe-history-panel') < y('.pipe-stage-panel'),
             position: getComputedStyle(document.querySelector('.pipe-history-panel')).position,
             titleInView: t.top >= 0 && t.bottom <= innerHeight };
  })()`);
  check(`${width}:回落单列`, narrow.oneColumn, JSON.stringify(narrow));
  check(`${width}:选择器排在步骤之前`, narrow.selectorFirst, JSON.stringify(narrow));
  check(`${width}:钉住关掉(单列时它会反过来挡住内容)`,
        narrow.position === 'static', JSON.stringify(narrow));
  check(`${width}:靠 focus 把视线带到被改写的那块`, narrow.titleInView, JSON.stringify(narrow));
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
