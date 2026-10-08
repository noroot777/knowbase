// 「我的订阅」/「同步中心」拆分后的浏览器回归:三类来源并列的页内切换、连接卡与
// 订阅管理就地在订阅页、定时同步跟着搬家、老深链(#social 等)落到对应视图、
// 同步中心跑完后把失败步骤统一列进总览问题块(运行中不弹,最后统一处理)。
//
// 需要一个跑着沙箱数据的 kb-web(和 tests/daily_probe.mjs 同一套):
//   python3 tests/llm_stub.py --port 8801 &
//   python3 tests/daily_sandbox.py /tmp/kb-subs-sandbox 8801
//   KB_DATA_DIR=... KB_VAULT_ROOT=... uv run uvicorn kb.server:app --port 8799
// CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、不连网)。
//
//   HEADLESS_SHELL=<bin> node tests/subs_probe.mjs http://127.0.0.1:8799
import { spawn } from "node:child_process";
const BIN = process.env.HEADLESS_SHELL;
const base = process.argv[2];
const proc = spawn(BIN, ["--headless", "--remote-debugging-port=0", "--disable-gpu",
                         "--hide-scrollbars", "--window-size=1440,1000", "about:blank"],
                   { stdio: ["ignore", "pipe", "pipe"] });
const port = await new Promise((res, rej) => {
  let buf = "";
  proc.stderr.on("data", d => { buf += d; const m = buf.match(/ws:\/\/127\.0\.0\.1:(\d+)\//); if (m) res(+m[1]); });
  proc.on("exit", c => rej(new Error("exit " + c)));
  setTimeout(() => rej(new Error("no port")), 20000);
});
const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let id = 0; const pend = new Map(); const pageErrors = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    pageErrors.push(m.params.args.map(a => a.value ?? a.description ?? "").join(" "));
});
const send = (method, params = {}) => { const mid = ++id; return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); }); };
const evalJs = async expression => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (expr, ms = 10000) => { const dl = Date.now() + ms; while (Date.now() < dl) { if (await evalJs(expr)) return true; await sleep(120); } return false; };
await send("Runtime.enable"); await send("Page.enable");
const fails = [], oks = [];
const check = (name, cond, extra = "") => (cond ? oks : fails).push(name + (extra ? ` — ${extra}` : ""));

await send("Page.navigate", { url: base + "/" });
await until(`typeof openSubsPanel === 'function' && typeof subsCatalog !== 'undefined' && subsCatalog.length === 3`, 20000);

// 1. 订阅页:三类来源并列,默认落公众号视图
await evalJs(`switchTab('subs')`);
await until(`document.querySelectorAll('#subs-switch button').length === 3`);
check("切换器列出三类并列来源",
      await evalJs(`[...document.querySelectorAll('#subs-switch button span')].map(e=>e.textContent).join()`) === "公众号,大V观点,RSS",
      await evalJs(`[...document.querySelectorAll('#subs-switch button span')].map(e=>e.textContent).join()`));
check("默认打开公众号视图", await evalJs(`!document.getElementById('subs-wechat').hidden && document.getElementById('subs-feeds').hidden`));
check("地址栏是 #subs", await evalJs(`location.hash`) === "#subs", await evalJs(`location.hash`));
check("连接卡在订阅页上", await evalJs(`!!document.querySelector('#subs-wechat .pipe-source')`));
check("定时同步落在订阅页底部", await evalJs(`!!document.querySelector('#subspanel .pipe-schedule')
      && document.querySelector('#subspanel .pipe-schedule').getClientRects().length > 0`));
await until(`document.getElementById('tgt-list').textContent.length > 0`, 12000);
check("订阅列表就地渲染(沙箱无采集器,如实报原因)",
      (await evalJs(`document.getElementById('tgt-list').textContent`)).length > 0,
      (await evalJs(`document.getElementById('tgt-list').textContent`)).slice(0, 50));

// 2. 页内切到 RSS 视图
await evalJs(`document.getElementById('subs-switch-rss').click()`);
await until(`!document.getElementById('subs-feeds').hidden`);
check("RSS 视图接管页面、公众号视图收起", await evalJs(`document.getElementById('subs-wechat').hidden`));
check("来源名降为二级标题", await evalJs(`document.getElementById('source-title').tagName`) === "H2");
await until(`sourceData && sourceData.sources.length >= 1`, 12000);
check("feeds 数据照常读进来", await evalJs(`sourceKind`) === "rss");
check("刷新记住所选视图", await evalJs(`localStorage.getItem('kb-subs-kind')`) === "rss");

// 3. 老深链 #social 落到订阅页对应视图
await send("Page.navigate", { url: base + "/#social" });
await until(`typeof activeTab !== 'undefined' && activeTab === 'subs'`, 20000);
await until(`subsKind === 'social' && !document.getElementById('subs-feeds').hidden`);
check("老深链 #social 落到订阅页的大V观点视图", await evalJs(`subsKind === 'social'`));

// 4. 同步中心:跑完失败的那轮在总览里统一列出 + 重试按钮
await evalJs(`switchTab('pipe')`);
await sleep(300);
check("同步中心没有连接卡和定时设置", await evalJs(`!document.querySelector('#pipepanel .pipe-source')
      && !document.querySelector('#pipepanel .pipe-schedule')`));
// 沙箱里有未提取的存量积压:问题块如实显示待提取(和旧版重试按钮同一口径)
check("空闲时问题块只报真实积压", await evalJs(
  `document.getElementById('pipe-issues').hidden
   || document.getElementById('pipe-issues-title').textContent.includes('待提取')`),
  await evalJs(`document.getElementById('pipe-issues-title').textContent`));
// 喂一份「跑完、导出失败」的快照
await evalJs(`renderPipeState({ running:false, stages:{}, log:[], last_run: {
  run_kind:'sync', planned_stages:['crawl','export','index'], finished_at:'2026-10-08 09:00:00',
  started_at:'2026-10-08 08:00:00', crawl_filter:{channel:'wechat_local',accounts:[]},
  stages:{ crawl:{status:'ok',detail:'完成'},
           export:{status:'error',detail:'4 条译文暂缺:模型服务 503'},
           index:{status:'ok',detail:'完成'} }, log:[] } }); true`);
check("失败步骤统一列在总览问题块里",
      await evalJs(`!document.getElementById('pipe-issues').hidden
        && document.getElementById('pipe-issues-title').textContent === '1 个步骤需处理'
        && document.getElementById('pipe-issues-list').textContent.includes('导出文章')
        && document.getElementById('pipe-issues-list').textContent.includes('503')`),
      await evalJs(`document.getElementById('pipe-issues-title').textContent`));
check("问题块里有重试按钮", await evalJs(`!document.getElementById('pipe-retry').hidden`));
// 运行中不弹问题块(失败不阻止本轮继续,收尾统一说)
await evalJs(`renderPipeState({ running:true, current_stage:'index', stop_supported:true,
  planned_stages:['crawl','export','index'], started_at:'2026-10-08 08:00:00',
  crawl_filter:{channel:'wechat_local',accounts:[]},
  stages:{ crawl:{status:'ok',detail:'完成'},
           export:{status:'error',detail:'失败 4'},
           index:{status:'running',detail:'建立索引'} }, log:[] }); true`);
check("运行中问题块不出现(最后统一处理)", await evalJs(`document.getElementById('pipe-issues').hidden`));

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));
const out = [...oks.map(n => `  ok ${n}`), ...fails.map(n => `  FAIL ${n}`)].join("\n");
process.stdout.write(`${out}\n${fails.length ? "FAIL" : "PASS"} ${oks.length}/${oks.length + fails.length}\n`);
ws.close(); proc.kill();
process.exitCode = fails.length ? 1 : 0;
