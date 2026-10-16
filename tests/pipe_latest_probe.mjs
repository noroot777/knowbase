// 「哪一行是此刻屏幕上那一轮」:**位置不是证据**。
//
// `pipe_history_probe.mjs` 验的是翻历史那一套行为,`pipe_layout_probe.mjs` 验几何,
// 这一份只验**选择语义**的一条:清单首行和当前状态来自两条独立的请求、两份独立可失败
// 的落盘,**允许不同步**。留档失败那一轮根本没写进历史文件,于是清单首条是上午那个红,
// 而状态端点报的是后来那一轮 —— 按「排第一」认最新的那一版,点上午那个红会退出只读、
// 屏幕换成后来那一轮的结果,上午那一轮的失败明细再也打不开(第 1 轮审查实点)。
//
// 两道守卫各钉一份红:后端那次证明(`is_current`,过 `same_event` 只认可信身份),和
// 前端拿手上这份快照的版本再对一次(后端那次证明只对它生成回包那一刻成立,而清单可能
// 是个迟到的旧回包)。
//
// **还有一局是「这一行已经在 DOM 里了」**:渲染那一刻的证明只在那一刻成立。后台又跑完
// 一轮、状态端点报的已经是它,而这一列那次历史查询恰好失败(503)—— 整列一个字没重渲
// 染,上午那个红还挂着上一次渲染留下的「最新」和退出动作,点它屏幕不换、那一轮的 503
// 读不到(第 2 轮审查在页面上实点出来的)。两道:列表按`在跑没跑|屏幕上那一轮的版本`
// 重渲染,以及点击那一刻再核验一次(冗余带)。
//
// **判据落在后果上,不落在 onclick 那串文本上** —— 动作换个函数名不该让这一份变红,
// 而「点上午那个红到底打开了哪一轮」才是要钉的事。
//
// 自带只读 mock 后端,CDP 直连 playwright 缓存里的 chrome-headless-shell(不装
// playwright、不连网、始终无头、临时 profile)。
//
//   HEADLESS_SHELL=<bin> node tests/pipe_latest_probe.mjs
import { createServer } from "node:http";
import { readFileSync, writeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize } from "node:path";
import { attachCdp, sleep } from "./pipe_history_cdp.mjs";
import { MORNING, EVENING, LATER, MIME, FIXTURES } from "./pipe_history_fixture.mjs";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "kb", "static");
const versionOf = r => String((r && (r.run_id || r.finished_at)) || "") || "none";

// 清单里有哪几轮 / 状态端点报的是哪一轮 —— **两者可以不是同一轮**,那正是要摆的局
let runs = [EVENING, MORNING];
let current = EVENING;
// true = 后端把首行标成了「就是此刻那份结果」而其实不是(迟到的旧回包、或者将来某处
// 把这个字段算错)。摆它是为了钉前端那第二道,不是说后端现在会这样。
let forceCurrent = false;
// >0 = 接下来这几次历史查询返回 503(每次扣 1)。摆的是「后台又跑完一轮,而这一列那次
// 补读恰好失败」—— 清单里那几行只能继续留在 DOM 里。
let failRuns = 0;

const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  const json = data => {
    const body = Buffer.from(JSON.stringify(data));
    res.writeHead(200, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
  };
  if (path === "/api/pipeline/status") {
    return json({ running: false, stop_requested: false, stopped: false, stop_supported: true,
                  last_run: current, log: [], result_version: versionOf(current) });
  }
  if (path === "/api/pipeline/runs") {
    if (failRuns > 0) {
      failRuns--;
      const body = Buffer.from(JSON.stringify({ detail: "history unavailable" }));
      res.writeHead(503, { "Content-Type": "application/json", "Content-Length": body.length });
      return res.end(body);
    }
    // 真后端:`is_current = same_event(行, last_result())` —— 只认可信身份
    const rows = runs.map((r, i) => ({
      ...r, version: versionOf(r), key: versionOf(r), invalid: false,
      is_current: forceCurrent ? i === 0 : !!(r.run_id && r.run_id === current.run_id),
    }));
    return json({ runs: rows, error: "", version: versionOf(runs[0]), keep_days: 3 });
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

const { proc, ws, send, evalJs, until, clickAt, pageErrors } = await attachCdp();
const oks = [], fails = [];
const check = (name, pass, info = "") => (pass ? oks : fails).push(pass ? name : `${name} — ${info}`);
const setup = async (name, promise) => {
  if (await promise) return;
  fails.push(`摆局没成:${name}`);
  throw new Error("stop");
};
// 两条请求都重新发一次,然后等页面把两份都认下来
const reload = async () => {
  await evalJs(`(async () => { await refreshPipe(); await loadPipeHistory(); })()`);
  await sleep(250);
};

try {
await send("Page.navigate", { url: `${base}/#pipe` });
await setup("面板加载出两轮留档",
            until(`document.querySelectorAll('#pipe-history-list .pipe-history-row').length === 2`,
                  8000));

// ── 1. 正常那一档:首行确实是此刻那一轮,它就该标「最新」、点它回到最新 ──────────
const normal = await evalJs(`(() => {
  const rows = document.querySelectorAll('#pipe-history-list .pipe-history-row');
  return { first: rows[0].className, second: rows[1].className };
})()`);
check("证明得了的那一行标「最新」", /\blatest\b/.test(normal.first), JSON.stringify(normal));
check("别的行不标", !/\blatest\b/.test(normal.second), JSON.stringify(normal));
// 判据落在后果上:先翻到上午那一轮,再点首行 —— 屏幕要回到最新(不是又进一次只读)
await setup("先翻到上午那一轮", clickAt("#pipe-history-list .pipe-history-row:nth-child(2)"));
await setup("进了只读态", until(`!$('pipe-viewing').hidden`));
await setup("点首行", clickAt("#pipe-history-list .pipe-history-row:nth-child(1)"));
await sleep(250);
const back = await evalJs(`({ readonly: !$('pipe-viewing').hidden,
                              stage: $('pipe-stage-export').dataset.status })`);
check("点它回到最新(退出只读、屏幕是最新那一轮)",
      !back.readonly && back.stage === 'ok', JSON.stringify(back));

// ── 2. 留档失败:清单里只有上午那个红,状态端点报的已经是后来那一轮 ────────────────
//
// **这一局是本探针的来由。** 点首行必须打开**它自己那一轮**(上午那个红),不许退出
// 只读换成下午那份成功结果。
runs = [MORNING];
current = LATER;
await reload();
await setup("清单变成只有上午那一轮",
            until(`document.querySelectorAll('#pipe-history-list .pipe-history-row').length === 1`));
check("证明不了就不标「最新」",
      !/\blatest\b/.test(await evalJs(
        `document.querySelector('#pipe-history-list .pipe-history-row').className`)),
      await evalJs(`document.querySelector('#pipe-history-list .pipe-history-row').className`));
await setup("点首行", clickAt("#pipe-history-list .pipe-history-row"));
await sleep(300);
const opened = await evalJs(`({
  readonly: !$('pipe-viewing').hidden,
  stage: $('pipe-stage-export').dataset.status,
  detail: $('pipe-stage-export').querySelector('.pipe-stage-detail').textContent,
  label: $('pipe-state-label').textContent })`);
check("点上午那个红,打开的是它自己那一轮(只读态)",
      opened.readonly && opened.stage === 'error', JSON.stringify(opened));
check("打开的是上午那一轮自己的失败原因", /翻译失败/.test(opened.detail), opened.detail);
check("那句话说的是「这一轮的结果」", /这一轮/.test(opened.label), opened.label);

// ── 3. 反过来钉前端那第二道:后端标错了也不许按位置认 ─────────────────────────
forceCurrent = true;
await evalJs(`exitPipeHistory(); true`);
await reload();
check("后端标了最新但版本对不上,照旧不标",
      !/\blatest\b/.test(await evalJs(
        `document.querySelector('#pipe-history-list .pipe-history-row').className`)),
      await evalJs(`document.querySelector('#pipe-history-list .pipe-history-row').className`));
forceCurrent = false;

// ── 4. 这一行已经在 DOM 里了:状态往前走,而这一列那次补读失败 ─────────────────
//
// 轮询只会把**状态**更新上来(历史那次 503),所以「上一次渲染留下的动作」这条路
// 是真实可达的:整列不重渲染的话,上午那个红就一直挂着「最新」。
runs = [EVENING, MORNING];
current = EVENING;
await reload();
await setup("回到正常那一局",
            until(`document.querySelectorAll('#pipe-history-list .pipe-history-row').length === 2`));
await setup("首行标着最新",
            until(`/\\blatest\\b/.test(
              document.querySelector('#pipe-history-list .pipe-history-row').className)`));
current = LATER;                      // 后台又跑完一轮
failRuns = 9;                         // 这一列接下来几次补读全失败
// 调的是**轮询自己那个入口**(生产里就是 6 秒那一跳,这里不等它),没有改任何页面
// 变量、也没有动 DOM:状态端点报新那一轮,历史那次请求 503
await evalJs(`refreshPipe()`);
await sleep(400);
const moved = await evalJs(`({
  detail: $('pipe-stage-export').querySelector('.pipe-stage-detail').textContent,
  rows: document.querySelectorAll('#pipe-history-list .pipe-history-row').length,
  first: document.querySelector('#pipe-history-list .pipe-history-row').className })`);
check("屏幕已经是新那一轮,而清单还是旧那两行(补读失败)",
      moved.rows === 2 && /第三轮/.test(moved.detail), JSON.stringify(moved));
check("旧那一行不再挂着「最新」", !/\blatest\b/.test(moved.first), JSON.stringify(moved));
await setup("点那一行", clickAt("#pipe-history-list .pipe-history-row"));
await sleep(300);
const stale = await evalJs(`({ readonly: !$('pipe-viewing').hidden,
                               when: $('pipe-viewing').textContent,
                               detail: $('pipe-stage-export').querySelector('.pipe-stage-detail').textContent })`);
check("点它打开的是它自己那一轮,不是屏幕上那一轮",
      stale.readonly && /21:04/.test(stale.when), JSON.stringify(stale));
check("打开的内容是那一轮自己的", /95 条/.test(stale.detail), stale.detail);

// 冗余带那一道单独钉:**不经过重渲染**直接调 `pickPipeRun`(页面上到不了这一步 ——
// 状态一更新就会重渲染),所以这里直接摆页面变量。缺了它的话「列表没能重渲染」那一档
// 就只剩一道守卫。
await evalJs(`exitPipeHistory(); true`);
await evalJs(`(() => { const row = pipeHistory[0];
  pipeSnapshot = { running: false, last_run: ${JSON.stringify(LATER)},
                   result_version: '${LATER.run_id}' };
  pickPipeRun(row.key); return true; })()`);
await sleep(200);
const guarded = await evalJs(`({ readonly: !$('pipe-viewing').hidden,
                                 when: $('pipe-viewing').textContent })`);
check("点击那一刻再核验:旧行照旧打开它自己那一轮",
      guarded.readonly && /21:04/.test(guarded.when), JSON.stringify(guarded));

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
