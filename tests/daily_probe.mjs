// 日报面板的浏览器回归:侧栏入口、列表与正文渲染、定时设置保存、生成按钮、
// 来源面板显示译文而不是英文原文。
// CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、不连网、
// 始终无头、临时 profile),与 tests/redesign_probe.mjs 同一套连法。
//
//   HEADLESS_SHELL=<bin> node tests/daily_probe.mjs http://127.0.0.1:8799
import { spawn } from "node:child_process";

const BIN = process.env.HEADLESS_SHELL;
const base = process.argv[2];

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

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));

let id = 0;
const pend = new Map();
const pageErrors = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    pageErrors.push(m.params.args.map(a => a.value ?? a.description ?? "").join(" "));
});
const send = (method, params = {}) => {
  const mid = ++id;
  return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
};
const evalJs = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
// 界面上的状态要等接口回来才落地,拍固定时长会在机器慢的时候假红。
const waitFor = async (expression, ms = 8000) => {
  for (let i = 0; i * 200 < ms; i++) {
    if (await evalJs(expression)) return true;
    await sleep(200);
  }
  return false;
};

await send("Runtime.enable");
await send("Page.enable");

const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));

await send("Page.navigate", { url: base + "/#daily" });
// initWorkspace 要先把来源模块拉回来才 restoreWorkspaceLocation,首帧之后还有
// 一串接口要落地;等 dailyLoaded 而不是拍一个固定时长。
for (let i = 0; i < 60 && !(await evalJs(`typeof dailyLoaded !== 'undefined' && dailyLoaded`)); i++) await sleep(250);

check("侧栏有「每日日报」入口", await evalJs(`!!document.getElementById('tab-daily')`));
check("面板打开后标题是每日日报", await evalJs(`document.getElementById('page-crumb').textContent`) === "每日日报");
check("面板可见", await evalJs(`getComputedStyle(document.getElementById('dailypanel')).display !== 'none'`));

check("日报列表出现了 2026-09-28",
      await evalJs(`[...document.querySelectorAll('[data-report]')].some(b => b.dataset.report === '2026-09-28')`));
check("已生成份数写在页头", (await evalJs(`document.getElementById('daily-count').textContent`)) !== "—");
const bodyHtml = await evalJs(`document.getElementById('daily-reader-body').innerHTML`);
check("正文渲染成 markdown", bodyHtml.includes("<h2") && bodyHtml.includes("今日要点"), bodyHtml.slice(0, 80));
check("正文没有重复一遍大标题", !bodyHtml.includes("<h1"), bodyHtml.slice(0, 120));
check("正文元信息带素材数与模型",
      /条素材/.test(await evalJs(`document.getElementById('daily-reader-meta').textContent`)));

check("同步范围列出了各通道",
      (await evalJs(`document.querySelectorAll('#daily-channels input').length`)) >= 3);
check("日期默认是今天",
      (await evalJs(`document.getElementById('daily-date').value`)) === new Date().toLocaleDateString("sv"));

// 定时设置:改一个字段 → 保存按钮亮 → 保存后回显下一次时间
await evalJs(`(() => { const t = document.getElementById('daily-time');
  t.value = '06:45'; t.dispatchEvent(new Event('input')); })()`);
check("改了设置后保存按钮可用", !(await evalJs(`document.getElementById('daily-sched-save').disabled`)));
await evalJs(`(() => { document.getElementById('daily-on').checked = true;
  document.getElementById('daily-on').dispatchEvent(new Event('change')); saveDailySettings(); })()`);
await sleep(1200);
const schedMsg = await evalJs(`document.getElementById('daily-sched-msg').textContent`);
check("保存后回显了下一次生成时间", /每天 06:45/.test(schedMsg), schedMsg);

// 空日期:不该悄悄出一份空日报,而是把原因写在横幅上,并且这句原因要留得住
// (紧接着会刷一次日报列表,刷成功顺手清横幅的话原因一闪就没了)。
await evalJs(`(() => { document.getElementById('daily-date').value = '2026-09-27'; runDaily(false); })()`);
await waitFor(`!document.getElementById('daily-error').hidden`);
await evalJs(`loadDailyReports()`);
const banner = await evalJs(`document.getElementById('daily-error-text').textContent`);
check("没有内容的日期给出原因而不是空日报", /没有已入库的内容/.test(banner), banner);
check("这一天没有被写成日报",
      !(await evalJs(`[...document.querySelectorAll('[data-report]')].some(b => b.dataset.report === '2026-09-27')`)));

// 重新生成已有的那天:先弹确认框,确认后列表里的生成时间会变
const stamp = `(async () => (await (await fetch('/api/daily/reports/2026-09-28')).json()).generated)()`;
const before = await evalJs(stamp);
await evalJs(`(() => { document.getElementById('daily-date').value = '2026-09-28'; runDaily(false); })()`);
const asked = await waitFor(`!!document.querySelector('.confirm-mask [data-act=ok]')`, 3000);
check("覆盖已有日报前先确认", asked,
      asked ? "" : await evalJs(`JSON.stringify({dates: dailyReports.map(r => r.date), busy: dailyBusy, state: dailyState && dailyState.phase})`));
if (asked) await evalJs(`document.querySelector('.confirm-mask [data-act=ok]').click()`);
await waitFor(`${stamp}.then(v => v !== ${JSON.stringify(before)})`);
const after = await evalJs(stamp);
check("确认后这一天被重新生成", !!after && after !== before, `${before} → ${after}`);
await evalJs(`refreshDailyStatus()`);
check("重新生成后不再显示上一次的失败",
      await evalJs(`document.getElementById('daily-error').hidden`));

// 来源面板:英文条目显示译文,弹窗能切回原文
await evalJs(`switchTab('rss')`);
await sleep(2500);
const excerpt = await evalJs(`document.querySelector('.source-excerpt')?.textContent || ''`);
check("订阅内容显示的是中文译文", /[一-鿿]/.test(excerpt) && !/^The runtime schedules/.test(excerpt), excerpt.slice(0, 60));
check("条目标注了已译为中文",
      /已译为中文/.test(await evalJs(`document.querySelector('.source-entry-footer span')?.textContent || ''`)));
await evalJs(`document.querySelector('[data-read-item]').click()`);
await sleep(500);
check("阅读弹窗给了看原文的入口", !(await evalJs(`document.getElementById('source-reader-original').hidden`)));
await evalJs(`toggleSourceOriginal()`);
check("切回原文后正文是英文",
      /The runtime schedules/.test(await evalJs(`document.getElementById('source-reader-body').textContent`)));

check("页面没有脚本报错", pageErrors.length === 0, pageErrors.join(" | "));

console.log(oks.map(o => "  ok  " + o).join("\n"));
if (fails.length) console.log(fails.map(f => "FAIL  " + f).join("\n"));
console.log(`\n${oks.length} ok, ${fails.length} failed`);
ws.close(); proc.kill();
process.exit(fails.length ? 1 : 0);
