// 明细里那一条:点得开原文、删得掉、删完就不见。
//
// 「缺少正文」的条目会被按退避时间一轮一轮地重试,而其中确实有些原文已经没有内容
// 了。这份探针钉的是面板上那两件事:①标题是条真链接(去原文核对);②那颗「不再
// 抓取」按下去真的打到删除端点、而且**先弹自绘确认框**(全站不用原生 confirm)。
// 外加两道护栏:③来源给不出身份的条目两样都不许长出来;④翻历史时按钮一律不出现
// (口径同「重试失败项」:看到的那一轮和按下去会动的那一轮必须是同一轮),而链接
// 照旧能点 —— 它只是读。
//
// 自带 mock 后端,CDP 直连 playwright 缓存里的 chrome-headless-shell
// (不装 playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/pipe_drop_probe.mjs
import { createServer } from "node:http";
import { readFileSync, writeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { attachCdp } from "./pipe_history_cdp.mjs";
import { MIME, FIXTURES } from "./pipe_history_fixture.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
const KEYS = ["crawl", "export", "index", "extract", "projects", "wiki"];
const LINK = "https://mp.weixin.qq.com/s?__biz=b&mid=1&idx=1";

// 三条明细,刚好是三种形态:带身份的两条(点得开、删得掉)、来源给不出身份的一条
// (降级那一档,两样都不该有)。
const ITEMS = () => [
  { title: "某个号 · 缺正文的一篇", reason: "缺少正文，暂未导出", pending: true,
    url: LINK, drop: { channel: "wechat_local", id: "wx-1", account: "某个号" } },
  { title: "另一个号 · 又一篇", reason: "缺少正文，暂未导出", pending: true,
    url: LINK + "&n=2", drop: { channel: "wechat_local", id: "wx-2", account: "另一个号" } },
  { title: "没有身份的一条", reason: "缺少正文，暂未导出", pending: true },
];

const roundOf = (id, at, failures) => ({
  run_id: id, run_kind: "sync", stopped: false,
  started_at: "2026-10-15 09:00:00", finished_at: at, planned_stages: [...KEYS],
  crawl_filter: { channel: "wechat_local" },
  stages: Object.fromEntries(KEYS.map(k => [k, k === "export"
    ? { status: "ok", detail: "完成: 导出 3, 待补正文 3", failures }
    : { status: "ok", detail: "完成" }])),
  log: ["[10-15 09:12:00] 管道结束"],
});

let latest = roundOf("run-now", "2026-10-15 09:12:00", ITEMS());
// 上一轮:同样有那几条明细,但它是**历史**,翻到它时按钮不许出现
const PAST = roundOf("run-past", "2026-10-14 09:12:00", ITEMS());
const drops = [];
let state = { running: false, stop_supported: true, stop_requested: false, ...latest };

const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const json = (data, code = 200) => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(code, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  if (path === "/api/pipeline/status") {
    return json({ ...state, last_run: latest, result_version: latest.run_id,
                  restore_gap: "", pending: { extract: 0 } });
  }
  if (path === "/api/pipeline/runs") {
    const rows = [latest, PAST].map(r => ({ ...r, version: r.run_id, key: r.run_id,
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
await setup("再点一次「不再抓取」",
            clickAt("#pipe-stage-export .pipe-failures p:nth-child(1) .pipe-failure-drop"));
await setup("确认框又出来了", until(`document.querySelectorAll('.confirm-mask').length === 1`));
await setup("点了确认", clickAt(".confirm-mask [data-act=ok]"));
check("请求原样带着那条明细给的 drop",
      drops.length === 1 && drops[0].channel === "wechat_local"
      && drops[0].id === "wx-1" && drops[0].account === "某个号", JSON.stringify(drops));
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
await until(`document.querySelectorAll('.toast').length > 0`, 6000);
await evalJs(`document.querySelectorAll('.toast').forEach(e => e.remove()); true`);

// ── 4. 翻历史:按钮一律不出现,链接照旧能点 ────────────────────────────────────
await setup("点中了上一轮那一行", clickAt(".pipe-history-row:nth-child(2)"));
await setup("整屏换成了上一轮",
            until(`!$('pipe-viewing').hidden`));
await evalJs(`$('pipe-stage-export').querySelector('details').open = true; true`);
await setup("上一轮那三条明细都在", until(
  `document.querySelectorAll('#pipe-stage-export .pipe-failures p').length === 3`));
listed = await rows();
check("翻历史时一颗「不再抓取」都没有", listed.every(r => !r.drop),
      JSON.stringify(listed.map(r => r.drop)));
check("链接照旧能点(它只是读)", listed[0].href === LINK, listed[0].href);

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
