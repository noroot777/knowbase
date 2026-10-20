// 「这一轮作用在哪几个来源上」在**真页面**上的红 —— 同步中心那行「上次来源」和
// 历史清单那一列,共用后端那一份结论和前端那一份派生。
//
// 抓取和导出早就是两份范围了(`crawl_filter` / `export_filter`:抓取收窄到失败的那
// 几条、导出照旧走整条来源),而「整轮来源」这个问题原来只读抓取那一份 —— 于是
// **「只补导出」那一轮一个来源名都报不出来**(它的抓取范围是 `{force: true}`):
// 同步中心写「选择来源后开始一次同步」、历史那行写「未记录来源」,而那一轮真真切切
// 地在往 RSS 里写文章(第 1 轮自由审查 R1 实页实测)。
//
// 给人翻的那份历史记录**故意不存导出范围**(那一份不许喂给重试派生),所以后端收尾
// 时把这一轮的来源集合直接存成**结论**(`pipeline_persist.run_record` 的 `scopes`);
// 没有这一格的老记录照旧按抓取范围念 —— 两边都钉着。
//
// 从 `pipe_history_probe.mjs` 里分出来的,**分开只为行数**(那边已经顶着 700 行上限,
// CLAUDE.md 那条):摆盘共用 `pipe_history_fixture.mjs`,判据在这里。
//
// 自带只读 mock 后端,CDP 直连缓存里的 chrome-headless-shell(不装 playwright、不连网、
// 始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/pipe_scope_probe.mjs
import { createServer } from "node:http";
import { readFileSync, writeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { attachCdp, sleep } from "./pipe_history_cdp.mjs";
import { MORNING, EVENING, MIME, FIXTURES } from "./pipe_history_fixture.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");

// 「只补导出」那一轮:抓取范围里一个来源名都没有,来源只写在那份结论里。
const EXPORT_ONLY = {
  run_id: "run-export-only", run_kind: "retry", stopped: false,
  started_at: "2026-10-20 11:00:00", finished_at: "2026-10-20 11:02:00",
  planned_stages: ["export", "index", "extract"],
  crawl_filter: { force: true },
  scopes: [{ channel: "rss", accounts: [], retry: "" }],
  stages: { ...EVENING.stages, crawl: { status: "skipped", detail: "本次无需重试" } },
  log: ["[10-20 11:02:00] 管道结束"],
};
// 同一轮,但来自升级前那份落盘(没有那格结论):照抓取范围念。
const LEGACY = {
  ...EXPORT_ONLY, run_id: "run-legacy", scopes: undefined,
  planned_stages: ["crawl", "export"], crawl_filter: { channel: "rss" },
};

let current = EXPORT_ONLY;
let runs = [EXPORT_ONLY, MORNING];

const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const json = data => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(200, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  if (path === "/api/pipeline/status")
    return json({ running: false, stop_requested: false, stopped: false,
                  stop_supported: true, last_run: current, log: [],
                  today_failed: [], result_version: current.run_id });
  if (path === "/api/pipeline/runs")
    return json({ runs: runs.map(r => ({ ...r, version: r.run_id, key: r.run_id,
                                         invalid: false, is_current: r === current })),
                  error: "", version: runs[0].run_id, keep_days: 3 });
  if (FIXTURES[path]) return json(FIXTURES[path]);
  if (path.startsWith("/api/")) return json({});
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

const { proc, ws, evalJs, until, pageErrors, send } = await attachCdp();
const oks = [], fails = [];
const check = (name, pass, info = "") => (pass ? oks : fails).push(pass ? name : `${name} — ${info}`);
const rows = () => evalJs(
  `[...document.querySelectorAll('.pipe-history-row')].map(e => e.textContent.replace(/\\s+/g, ' ').trim())`);
const whereLine = () => evalJs(`$('pipe-running-channel').textContent`);
const show = async what => {
  current = what;
  runs = [what, MORNING];
  await evalJs(`pipeHistory = []; pipeViewing = null; renderPipeHistory(); true`);
  await evalJs(`refreshPipe(); loadPipeHistory(); true`);
  return until(`pipeHistory[0]?.run_id === '${what.run_id}'`, 8000);
};

try {
await send("Page.navigate", { url: `${base}/#pipe` });
if (!await until(`typeof refreshPipe === 'function' && typeof pipeRunScopes === 'function'`, 20000)) {
  fails.push("摆局没成:面板没加载出来");
  throw new Error("stop");
}

// ① 只补导出那一轮:两处都要说得出来源
if (!await show(EXPORT_ONLY)) fails.push("摆局没成:列表没换成只补导出那一轮");
await sleep(250);
check("只补导出那一轮,同步中心那行说得出来源",
      /订阅源/.test(await whereLine()) && !/选择来源后开始一次同步/.test(await whereLine()),
      await whereLine());
check("它的历史行也写得出来源,不许写「未记录来源」",
      /订阅源/.test((await rows())[0] || "") && !/未记录来源/.test((await rows())[0] || ""),
      (await rows())[0]);

// ② **反过来钉一边**:没有那格结论的老记录照旧按抓取范围念
if (!await show(LEGACY)) fails.push("摆局没成:列表没换成那条老记录");
await sleep(250);
check("老记录照旧按抓取范围说来源", /订阅源/.test(await whereLine()), await whereLine());
check("老记录的历史行也照旧写来源", /订阅源/.test((await rows())[0] || ""),
      (await rows())[0]);

// ③ **反过来钉另一边**:纯全库那一轮不许凭空附加来源,而要自己有话说
if (!await show({ ...EXPORT_ONLY, run_id: "run-library", run_kind: "sync",
                  scopes: [], planned_stages: ["index", "extract"],
                  crawl_filter: { channel: "wechat_local" } }))
  fails.push("摆局没成:列表没换成全库那一轮");
await sleep(250);
check("全库那一轮写「全库」,不许认成某个来源",
      /全库/.test(await whereLine()) && !/公众号|wechat/.test(await whereLine()),
      await whereLine());

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
ws.close();
proc.kill();
server.close();
process.exitCode = fails.length ? 1 : 0;
