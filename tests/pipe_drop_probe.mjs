// 明细里那一条:点得开原文、删得掉、删完就不见。
//
// 「缺少正文」的条目会被按退避时间一轮一轮地重试,而其中确实有些原文已经没有内容
// 了。这份探针钉的是面板上那两件事:①标题是条真链接(去原文核对);②那颗「不再
// 抓取」按下去真的打到删除端点、而且**先弹自绘确认框**(全站不用原生 confirm)。
// 外加两道护栏:③来源给不出身份的条目两样都不许长出来;④历史按条目身份移除,
// 不重试或切换当前正在执行的那一轮。
//
// 自带 mock 后端,CDP 直连 playwright 缓存里的 chrome-headless-shell
// (不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/pipe_drop_probe.mjs
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, writeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { attachCdp } from "./pipe_history_cdp.mjs";
import { MIME, FIXTURES } from "./pipe_history_fixture.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
const KEYS = ["crawl", "export", "index", "extract", "projects", "wiki"];
const CHANNEL = process.env.PIPE_DROP_CHANNEL || "wechat_local";
const LINK = CHANNEL === "rss" ? "https://example.org/article?id=1"
  : "https://mp.weixin.qq.com/s?__biz=b&mid=1&idx=1";
const URLS = [LINK, LINK + "&n=2", LINK + "&n=3"];
const IDS = CHANNEL === "rss"
  ? URLS.map(url => createHash("sha256").update(`${CHANNEL}:${url}`).digest("hex").slice(0, 32))
  : ["wx-1", "wx-2", "wx-past"];
const ref = (index, account) => ({ channel: CHANNEL, id: IDS[index], link: URLS[index],
  ...(CHANNEL === "rss" ? {} : { account }) });

// 三条明细,刚好是三种形态:带身份的两条(点得开、删得掉)、来源给不出身份的一条
// (降级那一档,两样都不该有)。
const ITEMS = () => [
  { title: "某个号 · 缺正文的一篇", reason: "缺少正文，暂未导出", pending: true,
    url: LINK, drop: ref(0, "某个号") },
  { title: "另一个号 · 又一篇", reason: "缺少正文，暂未导出", pending: true,
    url: LINK + "&n=2", drop: ref(1, "另一个号") },
  { title: "没有身份的一条", reason: "缺少正文，暂未导出", pending: true },
];

const roundOf = (id, at, failures) => ({
  run_id: id, run_kind: "sync", stopped: false,
  started_at: "2026-10-15 09:00:00", finished_at: at, planned_stages: [...KEYS],
  crawl_filter: { channel: CHANNEL },
  stages: Object.fromEntries(KEYS.map(k => [k, k === "export"
    ? { status: "ok", detail: "完成: 导出 3, 待补正文 3", failures }
    : { status: "ok", detail: "完成" }])),
  log: ["[10-15 09:12:00] 管道结束"],
});

let latest = roundOf("run-now", "2026-10-15 09:12:00", ITEMS());
const HISTORY_ONLY = {
  title: "历史号 · 仅在上一轮缺正文的一篇", reason: "缺少正文，暂未导出", pending: true,
  url: LINK + "&n=3", drop: ref(2, "历史号"),
};
let past = roundOf("run-past", "2026-10-14 09:12:00",
  [...ITEMS().slice(0, 2), HISTORY_ONLY, ITEMS()[2]]);
const drops = [];
let state = { running: false, stop_supported: true, stop_requested: false, ...latest };
// 「当前待处理」:删掉一条它就该少一个。**故意答得慢、而且按请求到达那一刻取值**
// —— 面板的待处理查询是独立的一条请求,删除发生在它在途期间时,它带回来的是删除
// **之前**的数字。删除不产生新的一轮,运行版本一个字不变,所以那份过期回包照旧
// 对得上版本。这正是要钉的那一道闸(`invalidatePipePending`)。
const PENDING_DELAY = 700;
const pendingNow = () => latest.stages.export.failures.filter(f => f.drop).length;

const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const json = (data, code = 200) => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(code, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  if (path === "/api/pipeline/status") {
    const body = { ...state, last_run: latest, result_version: latest.run_id,
                   restore_gap: "" };
    if (!/[?&]pending=true/.test(req.url)) return json(body);
    const snapshot = { export: pendingNow(), extract: 0 };
    return void setTimeout(() => json({ ...body, pending: snapshot }), PENDING_DELAY);
  }
  if (path === "/api/pipeline/runs") {
    const rows = [latest, past].map(r => ({ ...r, version: r.run_id, key: r.run_id,
                                            invalid: false }));
    return json({ runs: rows, error: "", version: latest.run_id });
  }
  if (path === "/api/pipeline/failures/drop" && req.method === "POST") {
    let raw = "";
    req.on("data", c => { raw += c; });
    return void req.on("end", () => {
      const body = JSON.parse(raw || "{}");
      drops.push(body);
      // 后端的口径:删成功之后那一条在**任何一轮**里都不再送出来。
      const keep = r => ({ ...r, stages: { ...r.stages, export: {
        ...r.stages.export,
        failures: r.stages.export.failures.filter(f => f.drop?.id !== body.id) } } });
      latest = keep(latest);
      past = keep(past);
      state = { ...state, stages: keep(state).stages };
      json({ ok: true, detail: "已移除,以后不再采集:某个号 · 缺正文的一篇" });
    });
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

const { proc, ws, send, evalJs, until, clickAt, pageErrors } = await attachCdp();
const pendingText = () => evalJs(
  `$('pipe-stage-export').querySelector('.pipe-stage-pending').textContent`);
const rows = () => evalJs(
  `[...document.querySelectorAll('#pipe-stage-export .pipe-failures p')]
     .map(p => ({ text: p.textContent.replace(/\\s+/g, ' ').trim(),
                  href: p.querySelector('a')?.getAttribute('href') || '',
                  target: p.querySelector('a')?.getAttribute('target') || '',
                  arrow: !!p.querySelector('a svg.ia-ext'),
                  drop: !!p.querySelector('.pipe-failure-drop') }))`);

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

// ── 1. 明细展开:带身份的点得开、带按钮,没身份的两样都没有 ──────────────────
await evalJs(`switchTab('pipe'); true`);
await setup("明细里有三条", until(
  `document.querySelectorAll('#pipe-stage-export .pipe-failures p').length === 3`));
await setup("待处理数读到了(2 项)", until(
  `$('pipe-stage-export').querySelector('.pipe-stage-pending').textContent
     === '当前待处理 2 项'`, 8000));
await evalJs(`$('pipe-stage-export').querySelector('details').open = true; true`);
let listed = await rows();
check("第一条标题是条真链接,指向原文", listed[0].href === LINK, listed[0].href);
check("链接在新标签打开", listed[0].target === "_blank", listed[0].target);
check("外链箭头走的是 icons.js 的 svg,不是 ↗ 字符",
      listed[0].arrow && !/[↗↑↓→▾▴]/.test(listed[0].text), listed[0].text);
check("带身份的两条各有一颗「不再抓取」",
      listed[0].drop && listed[1].drop);
check("来源给不出身份的那一条:点不开、也删不掉",
      !listed[2].href && !listed[2].drop, JSON.stringify(listed[2]));
check("原因那一句照旧写着", /缺少正文/.test(listed[2].text), listed[2].text);

// ── 2. 点按钮先弹自绘确认框,取消什么都不发 ───────────────────────────────────
await setup("点中了第一条的「不再抓取」",
            clickAt("#pipe-stage-export .pipe-failures p:nth-child(1) .pipe-failure-drop"));
check("弹的是自绘确认框,不是原生 confirm",
      await until(`document.querySelectorAll('.confirm-mask').length === 1`));
const asked = await evalJs(`document.querySelector('.confirm-body')?.textContent || ''`);
check("确认框里写明这是不可撤销的", /不可撤销/.test(asked), asked);
check("确认框念的是标题,没把按钮自己那几个字念进去",
      /缺正文的一篇。/.test(asked) && !/一篇不再抓取/.test(asked), asked);
await setup("点了取消", clickAt(".confirm-mask [data-act=cancel]"));
check("取消之后一个请求都没发", drops.length === 0, JSON.stringify(drops));
check("取消之后那一条还在",
      (await rows()).length === 3 && (await rows())[0].drop);

// ── 3. 确认之后:请求带着那份 drop,面板上那一条随即消失 ───────────────────────
// **故意让一次待处理查询在途**:它带回来的是删除之前那个数字,而删除不产生新的
// 一轮、运行版本一个字不变 —— 没有另一道身份闸的话那份过期回包照旧被应用,面板
// 永久停在删除前的「当前待处理 2 项」(整页刷新才归零)。
await evalJs(`refreshPipePending(); true`);
await setup("再点一次「不再抓取」",
            clickAt("#pipe-stage-export .pipe-failures p:nth-child(1) .pipe-failure-drop"));
await setup("确认框又出来了", until(`document.querySelectorAll('.confirm-mask').length === 1`));
await setup("点了确认", clickAt(".confirm-mask [data-act=ok]"));
check("请求原样带着那条明细给的 drop(含 link)",
      drops.length === 1 && JSON.stringify(drops[0]) === JSON.stringify(ITEMS()[0].drop),
      JSON.stringify(drops));
check("删完那一条就从面板上消失了", await until(
  `document.querySelectorAll('#pipe-stage-export .pipe-failures p').length === 2`, 8000),
      JSON.stringify(await rows()));
listed = await rows();
check("剩下的两条没被牵连",
      listed.length === 2 && /又一篇/.test(listed[0].text) && /没有身份/.test(listed[1].text),
      JSON.stringify(listed.map(r => r.text)));
check("明细标题跟着念剩下几条",
      /2 项/.test(await evalJs(
        `$('pipe-stage-export').querySelector('summary').textContent`)),
      await evalJs(`$('pipe-stage-export').querySelector('summary').textContent`));
check("待处理数跟着删完的结果走,不是在途那份旧数字",
      await until(
        `$('pipe-stage-export').querySelector('.pipe-stage-pending').textContent
           === '当前待处理 1 项'`, 12000),
      await pendingText());
await until(`document.querySelectorAll('.toast').length > 0`, 6000);
await evalJs(`document.querySelectorAll('.toast').forEach(e => e.remove()); true`);

state = { ...state, running: true };
await evalJs(`refreshPipe(); true`);
await setup("最新一轮正在运行", until(`pipeSnapshot.running === true`));
await setup("点中了上一轮那一行", clickAt(".pipe-history-row:nth-child(2)"));
await setup("整屏换成了上一轮",
            until(`!$('pipe-viewing').hidden`));
await evalJs(`$('pipe-stage-export').querySelector('details').open = true; true`);
await setup("上一轮那三条明细都在", until(
  `document.querySelectorAll('#pipe-stage-export .pipe-failures p').length === 3`));
listed = await rows();
check("翻历史时有身份的条目仍可移除", listed[0].drop && listed[1].drop,
      JSON.stringify(listed.map(r => r.drop)));
check("历史里已移除的条目不再出现", listed.every(r => !/某个号 · 缺正文/.test(r.text)));
check("历史里没有身份的条目仍不可操作", !listed[2].drop && !listed[2].href);
check("链接照旧指向历史条目的原文", listed[0].href === LINK + "&n=2", listed[0].href);
check("翻历史时仍不能重试整轮", await evalJs(`$('pipe-retry').hidden && $('pipe-retry').disabled`));
check("历史提示说明仍可永久移除条目", await evalJs(
  `$('pipe-state-hint').textContent.includes('不再抓取')`));
if (process.env.PIPE_DROP_SCREENSHOT_DIR) {
  mkdirSync(process.env.PIPE_DROP_SCREENSHOT_DIR, { recursive: true });
  await evalJs(`$('pipe-stage-export').scrollIntoView({ block: 'center' }); true`);
  const shot = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(process.env.PIPE_DROP_SCREENSHOT_DIR,
    CHANNEL === "rss" ? "rss-history-drop.png" : "history-drop.png"),
    Buffer.from(shot.result.data, "base64"));
}

await setup("点中仅在历史里出现的条目", clickAt(
  "#pipe-stage-export .pipe-failures p:nth-child(2) .pipe-failure-drop"));
await setup("历史条目也弹出确认框", until(`document.querySelectorAll('.confirm-mask').length === 1`));
const historyAsked = await evalJs(`document.querySelector('.confirm-body')?.textContent || ''`);
check("确认框对应历史标题并说明不可撤销", /历史号 · 仅在上一轮缺正文的一篇。/.test(historyAsked)
  && /不可撤销/.test(historyAsked), historyAsked);
await setup("取消历史移除", clickAt(".confirm-mask [data-act=cancel]"));
check("取消历史移除不发请求", drops.length === 1, JSON.stringify(drops));
await setup("再次点中历史条目", clickAt(
  "#pipe-stage-export .pipe-failures p:nth-child(2) .pipe-failure-drop"));
await setup("历史确认框再次出现", until(`document.querySelectorAll('.confirm-mask').length === 1`));
await setup("确认移除历史条目", clickAt(".confirm-mask [data-act=ok]"));
check("历史操作原样发送历史条目的身份", drops.length === 2
  && JSON.stringify(drops[1]) === JSON.stringify(HISTORY_ONLY.drop), JSON.stringify(drops));
check("历史条目移除后就地消失", await until(
  `pipeViewing?.key === 'run-past'
   && document.querySelectorAll('#pipe-stage-export .pipe-failures p').length === 2`, 8000));
check("不会误删最新一轮的其他条目", latest.stages.export.failures.length === 2
  && latest.stages.export.failures[0].drop.id === IDS[1]);

await evalJs(`document.querySelectorAll('.toast').forEach(e => e.remove()); true`);
await setup("从历史移除两轮共有的条目", clickAt(
  "#pipe-stage-export .pipe-failures p:nth-child(1) .pipe-failure-drop"));
await setup("共有条目弹出确认框", until(`document.querySelectorAll('.confirm-mask').length === 1`));
await setup("确认移除共有条目", clickAt(".confirm-mask [data-act=ok]"));
check("共有条目从历史视图消失", await until(
  `pipeViewing?.key === 'run-past'
   && document.querySelectorAll('#pipe-stage-export .pipe-failures p').length === 1`, 8000));
check("历史操作没有停止或替换正在运行的一轮", await evalJs(
  `pipeSnapshot.running && pipeSnapshot.run_id === 'run-now'`));
await evalJs(`document.querySelectorAll('.toast').forEach(e => e.remove()); true`);
await setup("回到最新一轮", clickAt("#pipe-viewing .ghost"));
check("最新视图也不再出现历史里移除的共有条目", await until(
  `!pipeViewing && document.querySelectorAll('#pipe-stage-export .pipe-failures p').length === 1`));
check("最新视图保留无身份条目且无移除入口", (await rows())[0].text.includes("没有身份")
  && !(await rows())[0].drop);
state = { ...state, running: false };
await evalJs(`refreshPipe(); true`);
await setup("最新一轮已结束", until(`pipeSnapshot.running === false`));
await evalJs(`refreshPipePending(); true`);
check("待处理数随历史移除更新", await until(
  `pipePending.export === 0`, 12000));

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
