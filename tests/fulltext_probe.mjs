// 正文补抓(kb/feeds/fulltext.py)那一档在真页面上的回归:开关在编辑弹窗里存得住、
// 保存时真的发出去、订阅行上标得出来、待补抓的条目换掉正文而不是标题、也不给
// 「展开阅读」,以及**别人家的条目不受影响**。
//
// 需要一个跑着沙箱数据的 kb-web:
//   python3 tests/llm_stub.py --port 8803 &
//   python3 tests/daily_sandbox.py /tmp/kb-fulltext-sandbox 8803
//   python3 tests/fulltext_sandbox.py /tmp/kb-fulltext-sandbox
//   KB_DATA_DIR=... KB_VAULT_ROOT=... uv run uvicorn kb.server:app --port 8802
// CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、不连网)。
//
//   HEADLESS_SHELL=<bin> node tests/fulltext_probe.mjs http://127.0.0.1:8802 62 63
import { spawn } from "node:child_process";
import { writeSync } from "node:fs";

const BIN = process.env.HEADLESS_SHELL;
const base = process.argv[2];
const AGG = process.argv[3], PLAIN = process.argv[4];   // 聚合源 / 普通源的订阅 id

const proc = spawn(BIN, ["--headless", "--remote-debugging-port=0", "--disable-gpu",
                         "--hide-scrollbars", "--window-size=1440,1000", "about:blank"],
                   { stdio: ["ignore", "pipe", "pipe"] });
const port = await new Promise((res, rej) => {
  let buf = "";
  proc.stderr.on("data", d => { buf += d; const m = buf.match(/ws:\/\/127\.0\.0\.1:(\d+)\//); if (m) res(+m[1]); });
  proc.on("exit", c => rej(new Error("chrome exit " + c)));
  setTimeout(() => rej(new Error("no cdp port")), 20000);
});
const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let id = 0; const pend = new Map(); const pageErrors = []; const dialogs = [];
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    pageErrors.push(m.params.args.map(a => a.value ?? a.description ?? "").join(" "));
  // 原生弹窗是硬性禁止的(全局视觉语言那一条),出现一次就记下来。
  if (m.method === "Page.javascriptDialogOpening") dialogs.push(m.params.message);
});
const send = (method, params = {}) => { const mid = ++id; return new Promise(res => { pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); }); };
const evalJs = async expression => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + " :: " + expression);
  return r.result?.result?.value;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (expr, ms = 15000) => { const dl = Date.now() + ms; while (Date.now() < dl) { if (await evalJs(expr)) return true; await sleep(120); } return false; };
await send("Runtime.enable"); await send("Page.enable");

const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok: !!ok, detail: String(detail) });

await send("Page.navigate", { url: base + "/" });
await until(`typeof switchTab === 'function' && typeof sourceModules !== 'undefined' && sourceModules.size >= 3`, 25000);
await evalJs(`switchTab('subs')`);
await until(`typeof openSubsPanel === 'function'`);
// 进 RSS 视图的订阅管理
await evalJs(`openSubsPanel('rss')`);
await until(`sourceKind === 'rss' && !!sourceData`);
await evalJs(`sourceView = 'manage'; renderSourceView(true)`);
await until(`!!document.querySelector('[data-source-row="${AGG}"], .source-subscription')`);

// ── 1. 订阅行上标得出来,而且只标开了的那一条 ────────────────────────────────
const rowText = await evalJs(`(() => {
  const rows = [...document.querySelectorAll('.source-subscription')];
  const find = n => rows.find(r => r.querySelector('h3')?.textContent === n);
  const agg = find('Link Aggregator'), plain = find('Full Text Blog');
  return JSON.stringify({agg: agg?.querySelector('.source-row-meta')?.textContent || '',
                         plain: plain?.querySelector('.source-row-meta')?.textContent || ''});
})()`);
const rows = JSON.parse(rowText);
check("开了补抓的订阅行上写着「补抓原文正文」", rows.agg.includes("补抓原文正文"), rows.agg);
check("没开的那条订阅行上不写", rows.plain && !rows.plain.includes("补抓原文正文"), rows.plain);

// ── 2. 编辑弹窗:开关的勾选态跟后端一致 ──────────────────────────────────────
await evalJs(`openSourceEditor(${AGG})`);
await until(`document.getElementById('source-editor').style.display !== 'none'`);
check("弹窗里有「去原文链接补抓正文」这个开关",
      await evalJs(`!!document.getElementById('source-edit-fulltext')`));
check("开了的订阅打开弹窗是勾上的",
      await evalJs(`document.getElementById('source-edit-fulltext').checked === true`));
check("开关旁边写清楚了它是给谁用的、以及打开之后存量条目会怎样", await evalJs(`(() => {
  const t = document.getElementById('source-edit-fulltext').closest('label')?.nextElementSibling?.textContent || '';
  return t.includes('聚合源') && t.includes('已收录的旧条目');
})()`));
await evalJs(`closeSourceEditor()`);
await evalJs(`openSourceEditor(${PLAIN})`);
await until(`document.getElementById('source-editor').style.display !== 'none'`);
check("没开的订阅打开弹窗是没勾的",
      await evalJs(`document.getElementById('source-edit-fulltext').checked === false`));
check("新建订阅默认不勾(对给全文的 feed 再抓一次是拿提取结果换作者给的全文)",
      await (async () => { await evalJs(`closeSourceEditor(); openSourceEditor(null)`);
        return evalJs(`document.getElementById('source-edit-fulltext').checked === false`); })());

// ── 3. 保存时真的把它发出去(看真实请求 body,不是看变量)────────────────────
await evalJs(`closeSourceEditor()`);
const sent = await evalJs(`(async () => {
  const seen = [];
  const real = window.fetch;
  window.fetch = (u, o) => { if (String(u).includes('/sources/')) seen.push({url:String(u), method:o?.method, body:o?.body}); return real(u, o); };
  openSourceEditor(${PLAIN});
  document.getElementById('source-edit-fulltext').checked = true;
  await saveSourceEditor(new Event('submit'));
  window.fetch = real;
  return JSON.stringify(seen);
})()`);
const calls = JSON.parse(sent);
const patch = calls.find(c => c.method === "PATCH");
check("保存时发的是 PATCH 且 body 里带着 fulltext:true",
      patch && JSON.parse(patch.body).fulltext === true, sent.slice(0, 220));
// 存回去,别把沙箱改脏(后面还要看那条普通订阅的条目)
await evalJs(`(async () => { openSourceEditor(${PLAIN});
  document.getElementById('source-edit-fulltext').checked = false;
  await saveSourceEditor(new Event('submit')); })()`);
await sleep(600);
check("取消勾选之后后端真的关掉了(开关能来回切)",
      (await (await fetch(`${base}/api/intake/rss`)).json()).sources
        .find(s => String(s.id) === PLAIN)?.fulltext == 0);

// ── 4. 条目列表:待补抓那一条换掉的是正文,不是标题 ───────────────────────────
await evalJs(`sourceView = 'items'; renderSourceView(true)`);
await until(`!$('source-filter').hidden && [...$('source-filter').options].some(o=>o.value==='${AGG}')`);
await evalJs(`$('source-filter').value = '${AGG}'; renderSourceView(true)`);
await until(`sourceRows.length === 5`);
await until(`document.querySelectorAll('#source-list .source-entry').length >= 5`);
const cards = JSON.parse(await evalJs(`JSON.stringify([...document.querySelectorAll('#source-list .source-entry')].map(a => ({
  name: a.querySelector('.source-entry-name')?.textContent || '',
  titleBtn: a.querySelector('.source-entry-title')?.textContent || '',
  body: a.querySelector('.source-excerpt')?.textContent || '',
  footer: a.querySelector('.source-entry-footer span')?.textContent || '',
  read: !!a.querySelector('[data-read-item]'),
  origin: a.querySelector('.source-entry-footer a')?.getAttribute('href') || '',
})))`));
const waiting = cards.find(c => c.footer.includes("正在补抓原文正文"));
check("待补抓的条目在列表上标出来了", !!waiting, JSON.stringify(cards.map(c => c.footer)));
check("它的标题照显示(是内容的标题,不是占位文字)",
      waiting && waiting.name === "Yes, and", waiting && waiting.name);
check("它的正文换成了状态文案,不照显示那个跳转壳",
      waiting && waiting.body.includes("正在去原文把正文抓回来") && !waiting.body.includes("HN Points"),
      waiting && waiting.body.slice(0, 80));
check("它不给「展开阅读」(库里这一份马上要被整篇正文换掉)", waiting && waiting.read === false);
check("原文链接还在,点得到", waiting && waiting.origin === "https://news.example.org/post-1", waiting && waiting.origin);
check("footer 左边写的是「链接条目」而不是「订阅正文」",
      waiting && waiting.footer.startsWith("链接条目"), waiting && waiting.footer);

// ── 4b. 已经导出过的那一档:库里此刻就是这份壳,如实显示、照旧能展开 ────────
// 打开这个开关最常见的时机就是"已经同步过、几百条壳摆在眼前",那一批全是
// 这个形态。对它们说"抓到之后才写进知识库"和文件、索引的事实正相反,而那句
// 文案配上"不给展开阅读"等于把一批本来读得到的条目一起藏起来。
const shipped = cards.find(c => c.footer.includes("只有链接的壳已入库"));
check("已经入库的壳单独一档,不跟着说「抓到才写进知识库」", !!shipped,
      JSON.stringify(cards.map(c => c.footer)));
check("它照旧能「展开阅读」(库里真的有这一篇)", shipped && shipped.read === true);
check("它如实显示库里那份壳,不换成状态文案",
      shipped && shipped.body.includes("HN Points") && !shipped.body.includes("正在去原文"),
      shipped && shipped.body.slice(0, 60));
check("它的标题是可点的那种(走普通分支)",
      shipped && shipped.titleBtn.includes("already made it into the vault"),
      shipped && (shipped.titleBtn || shipped.name));

// ── 4c. 补抓成功、还在等译文的**旧**条目:「读得到」这个事实不许丢 ──────────
// 第 1 轮审查那张截图:它的标题连同「展开阅读」一起消失、换成一句"译成中文
// 之后才写进知识库",而 vault 里那份 md 好好地躺着。根因是补抓时清掉了
// `exported_hash` —— 那是「入没入过库」的事实来源(见 fulltext._save)。
const waitingZh = cards.find(c => c.origin === "https://news.example.org/post-fetched-shipped");
check("补抓成功还在等译文的旧条目仍然在列表上", !!waitingZh,
      JSON.stringify(cards.map(c => c.origin)));
check("它的标题照显示(不是「外文内容，正在等翻译」那句占位文字)",
      waitingZh && waitingZh.titleBtn.includes("still waiting for its translation"),
      waitingZh && (waitingZh.titleBtn || waitingZh.name));
check("它照旧能「展开阅读」(vault 里那份 md 还在)", waitingZh && waitingZh.read === true,
      JSON.stringify(waitingZh || {}));
check("footer 说的是「等中文译文覆盖」,不是「译成中文之后才写进知识库」",
      waitingZh && waitingZh.footer.includes("外文原文已入库，正在等中文译文覆盖"),
      waitingZh && waitingZh.footer);
check("它的正文如实显示抓回来的那一篇,不换成状态文案",
      waitingZh && waitingZh.body.includes("did come back from the original link")
        && !waitingZh.body.includes("译成中文"), waitingZh && waitingZh.body.slice(0, 70));
check("它也不该再挂着「正在补抓」(正文已经抓到了)",
      waitingZh && !waitingZh.footer.includes("正在补抓"), waitingZh && waitingZh.footer);

const done = cards.find(c => c.titleBtn.includes("正文确实抓回来了"));
check("补抓完成并译好的那条恢复成普通形态(标题可点、能展开阅读)",
      done && done.read === true && done.footer.includes("已译为中文"), JSON.stringify(done || {}));
check("补抓完成的那条正文是整篇,不是状态文案",
      done && done.body.includes("顺着原文链接抓回来"), done && done.body.slice(0, 60));
const exhausted = cards.find(c => c.name.includes("正在等翻译") || c.titleBtn.includes("could not be read"));
check("试满重试上限的那条离开了补抓这一档(不许永久挂着「正在补抓」)",
      exhausted && !exhausted.footer.includes("正在补抓"), JSON.stringify(exhausted || {}));

// ── 5. 顶上那句计数:两档各说各的 ───────────────────────────────────────────
const note = await evalJs(`document.querySelector('#source-list .source-note')?.textContent || ''`);
check("顶上写着还有几条在补抓", note.includes("正在补抓原文正文"), note);
// 四条里只有一条是"还没入库 + 待补抓",已经入库那条不该被算进这句话 ——
// 它已经读得到了,算进"抓到才写进知识库"就是又说了一遍那句不准的话。
check("这句计数只数还没入库那一档", /^1 条只拿到链接/.test(note), note);
check("补抓和等翻译是两句话、都说(流水线上先后两步,数字不是一回事)",
      note.includes("正在补抓原文正文") && note.includes("还在等翻译"), note);

// ── 6. 别人家的条目不受影响 ─────────────────────────────────────────────────
await evalJs(`$('source-filter').value = '${PLAIN}'; renderSourceView(true)`);
await until(`sourceRows.length === 1`);
const plainCards = await evalJs(`JSON.stringify([...document.querySelectorAll('#source-list .source-entry')].map(a =>
  (a.querySelector('.source-entry-footer span')?.textContent || '')))`);
check("没开补抓的订阅,条目一条都不标",
      !plainCards.includes("补抓"), plainCards);
check("它也不出那句计数提示",
      !(await evalJs(`document.querySelector('#source-list .source-note')?.textContent || ''`)).includes("补抓"));

// ── 7. 整轮不许有原生弹窗和 JS 错误 ─────────────────────────────────────────
check("没有原生 alert/confirm/prompt", dialogs.length === 0, dialogs.join(" | "));
check("没有 JS 错误", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));

ws.close(); proc.kill();
// 收尾一律 writeSync、不用 process.exit:管道上的写是异步的,exit() 会把没刷出去
// 的那部分丢掉;而 writeSync 自己要顶住短写和 EAGAIN(fd 是非阻塞的)。
function out(fd, text) {
  let buf = Buffer.from(text, "utf8");
  while (buf.length) {
    try { buf = buf.subarray(writeSync(fd, buf)); }
    catch (e) { if (e.code !== "EAGAIN") throw e;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2); }
  }
}
const bad = results.filter(r => !r.ok);
for (const r of results)
  out(2, `${r.ok ? "  ok" : "FAIL"}  ${r.name}${r.detail ? " — " + r.detail : ""}\n`);
out(2, `${results.length - bad.length} ok, ${bad.length} failed\n`);
out(1, JSON.stringify(results) + "\n");
process.exitCode = bad.length ? 1 : 0;
