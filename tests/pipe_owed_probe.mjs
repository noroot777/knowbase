// 「重试失败项」那颗按钮**露不露脸,按今天还欠不欠判**,不按最近一轮红不红。
//
// 一天里会跑好几轮,而面板上那份当前状态只留最后一轮:09:12 抓 RSS 失败、10:20 一轮
// 公众号全绿收尾之后,最近那一轮一个红都没有 —— 按旧判据按钮当场藏掉,于是 09:12 那条
// **连点的机会都没有**,后端那一侧补得再对也到不了(审查 R1 的同一条路)。后端口径是
// `pipeline_retry.today_failed`(当日账本),回包字段 `today_failed`。
//
// 四档,两正两反:
// ① 这一轮全绿、今天还欠着 → 按钮照旧露脸,**而且那句话说清它为什么在**(按钮冒出来
//    却没有任何一句话解释,和「这个按钮坏了」长得一模一样);
// ② **反过来钉一边**:今天什么都不欠 → 按钮照旧藏着,那句话也不许提「今天早些时候」;
// ③ 这一轮自己报了红、今天欠的也是它 → **不许数两遍**(上面那句话已经念过它了);
// ④ 这一轮自己报了红、今天另外还欠着别的 → 两句话各说各的,按钮当然在。
//
// 自带只读 mock 后端,CDP 直连缓存里的 chrome-headless-shell(不装 playwright、不连网、
// 始终无头、临时 profile)。摆盘共用 `pipe_history_fixture.mjs` 那几轮。
//
//   HEADLESS_SHELL=<bin> node tests/pipe_owed_probe.mjs
import { createServer } from "node:http";
import { readFileSync, writeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { attachCdp, sleep } from "./pipe_history_cdp.mjs";
import { MORNING, EVENING, MIME, FIXTURES } from "./pipe_history_fixture.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
let current = EVENING;          // 屏幕上这一轮
let owed = [];                  // 今天还欠的那几步(回包的 today_failed)

const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const json = data => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(200, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  if (path === "/api/pipeline/status") {
    return json({ running: false, stop_requested: false, stopped: false,
                  stop_supported: true, last_run: current, log: [],
                  today_failed: owed,
                  result_version: current.run_id });
  }
  if (path === "/api/pipeline/runs") {
    return json({ runs: [{ ...current, version: current.run_id, key: current.run_id,
                           invalid: false, is_current: true }],
                  error: "", version: current.run_id, keep_days: 3 });
  }
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

const { proc, ws, send, evalJs, until, pageErrors } = await attachCdp();
const oks = [], fails = [];
const check = (name, pass, info = "") => (pass ? oks : fails).push(pass ? name : `${name} — ${info}`);
const reload = async () => {
  await evalJs(`refreshPipe()`);
  await sleep(250);
};
const now = () => evalJs(`({ hidden: $('pipe-retry').hidden,
                             hint: $('pipe-state-hint').textContent })`);

try {
await send("Page.navigate", { url: `${base}/#pipe` });
// 等的是**DOM 节点自己**,不是 `$(...)` —— 那个助手要等应用 JS 跑起来才有,早一刻
// 问它只会把「还没加载完」报成一句 Uncaught。
if (!await until(
      `document.querySelectorAll('#pipe-history-list .pipe-history-row').length === 1`,
      8000)) {
  fails.push("摆局没成:面板没加载出来");
  throw new Error("stop");
}

// ② 先钉反面:这一轮全绿、今天什么都不欠
let seen = await now();
check("今天什么都不欠时按钮照旧藏着", seen.hidden, JSON.stringify(seen));
check("那句话也不提「今天早些时候」", !/今天早些时候/.test(seen.hint), seen.hint);

// ① 这一轮全绿、今天早些时候那条抓取还欠着
owed = ["crawl"];
await reload();
seen = await now();
check("这一轮全绿、今天还欠着时按钮照旧露脸", !seen.hidden, JSON.stringify(seen));
check("那句话说清了它为什么在",
      /今天早些时候/.test(seen.hint) && /1 个步骤/.test(seen.hint), seen.hint);

// ③ 这一轮自己报了红、今天欠的就是它 —— 不许数两遍
current = MORNING;
owed = ["export"];
await reload();
seen = await now();
check("这一轮自己报的那条不再数一遍",
      /上次有 1 个步骤失败/.test(seen.hint) && !/今天早些时候/.test(seen.hint), seen.hint);
check("按钮当然在", !seen.hidden, JSON.stringify(seen));

// ④ 这一轮红的是导出,今天另外还欠着抓取
owed = ["export", "crawl"];
await reload();
seen = await now();
check("这一轮之外那几步照旧要说出来",
      /上次有 1 个步骤失败/.test(seen.hint), seen.hint);

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));

} catch (e) {
  if (e.message !== "stop") fails.push(`探针中断 — ${e.message}`);
}

const out = [...oks.map(n => `  ✓ ${n}`), ...fails.map(n => `  ✗ ${n}`)].join("\n");
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
