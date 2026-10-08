// 同一篇文章的两个阅读入口必须读到同样的内容:订阅面板的「展开阅读」和知识库的
// 文章阅读页(/doc/…)。
//
// 这条曾经不成立 —— 订阅正文按契约已经是纯文本(标签都剥掉了),可阅读页把 md
// 交给 marked 时又把正文里剩下的字面标签当成原始 HTML:`Use List<T>` 在阅读页上
// 显示成 `Use List`(`<T>` 被当未知标签吃掉),整段只有 `<script>alert(1)</script>`
// 的那篇打开后正文一片空白,而同一篇在订阅面板上是完整的(textContent 显示)。
// 2026-10-08 第 3 轮审查实测两种样本。
//
// 顺手钉住反向:阅读页把原始 HTML 转义成文字之后,**正常的 markdown 照旧渲染** ——
// 标题、列表、外链、行内代码、围栏代码、表格一个都不许退化成纯文字。
//
// CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、不连网、
// 始终无头、临时 profile),口径同 tests/chat_height_probe.mjs。
//
//   HEADLESS_SHELL=<bin> node tests/reading_probe.mjs <baseURL> <manifest.json>
import { spawn } from "node:child_process";
import { readFileSync, writeSync } from "node:fs";

const BIN = process.env.HEADLESS_SHELL;
const base = process.argv[2];
const docs = JSON.parse(readFileSync(process.argv[3], "utf8"));

const proc = spawn(BIN, ["--headless", "--remote-debugging-port=0", "--disable-gpu",
                         "--hide-scrollbars", "--window-size=1280,900", "about:blank"],
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
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown")
    pageErrors.push(m.params.exceptionDetails.exception?.description
                    || m.params.exceptionDetails.text || "");
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
const until = async (expr, ms = 15000) => {
  const dl = Date.now() + ms;
  while (Date.now() < dl) { if (await evalJs(expr)) return true; await sleep(120); }
  return false;
};
await send("Runtime.enable");
await send("Page.enable");

const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok: !!ok, detail: String(detail) });
// 「可读内容」:逐行 trim、丢掉空行。两个入口的空白规则本来就不同(一个
// white-space 照显、一个按 HTML 折叠),比的是有没有少字,不是比排版。
const lines = s => String(s).split("\n").map(x => x.trim()).filter(Boolean);
const same = (a, b) => JSON.stringify(lines(a)) === JSON.stringify(lines(b));
const docUrl = rel => base + "/doc/" + rel.split("/").map(encodeURIComponent).join("/");

// ── 1. 订阅面板的展开阅读(textContent,本来就是完整的) ──
await send("Page.navigate", { url: base + "/#subs" });
await until(`typeof switchTab === 'function' && typeof subsCatalog !== 'undefined'`, 25000);
await evalJs(`switchTab('subs'); document.getElementById('subs-switch-rss').click(); true`);
await until(`typeof sourceRows !== 'undefined' && sourceRows.length >= 3`, 20000);

const readerText = {};
for (const [key, title] of [["generics", "泛型示例"], ["script", "脚本字面示例"],
                            ["scriptComment", "脚本开头示例"]]) {
  const opened = await evalJs(`(() => {
    const art = [...document.querySelectorAll('#source-list article')]
      .find(a => a.textContent.includes(${JSON.stringify(title)}));
    if (!art) return false;
    art.querySelector('[data-read-item]').click();
    return true;
  })()`);
  await until(`document.getElementById('source-reader').style.display === ''`);
  readerText[key] = await evalJs(`document.getElementById('source-reader-body').textContent`);
  check(`订阅面板展开阅读打开了「${title}」`, opened && readerText[key]);
  await evalJs(`document.getElementById('source-reader').style.display = 'none'; true`);
}
check("订阅面板:泛型示例一个字没少",
      same(readerText.generics, "新 API 使用 List<T>。\n第二段保持独立。"),
      JSON.stringify(readerText.generics));
check("订阅面板:脚本字面示例一个字没少",
      same(readerText.script, "<script>alert(1)</script>"),
      JSON.stringify(readerText.script));
check("订阅面板:脚本开头示例一个字没少",
      same(readerText.scriptComment, "示例开头写 <!-- <script>。\n这里是普通的第二段。"),
      JSON.stringify(readerText.scriptComment));

// ── 2. 同一篇的知识库阅读页 ──
const docBody = async () => evalJs(`(() => {
  const c = document.getElementById('content');
  const h1 = c.querySelector('h1');
  const head = h1 ? h1.textContent : '';
  return c.textContent.replace(head, '');
})()`);

await send("Page.navigate", { url: docUrl(docs.generics) });
await until(`!!document.getElementById('content') && document.getElementById('content').innerHTML.length > 0`);
const genericsDoc = await docBody();
check("阅读页:泛型示例里的 <T> 还在",
      lines(genericsDoc).join("\n").includes("List<T>"), JSON.stringify(genericsDoc));
check("阅读页与订阅面板读到的是同一份内容(泛型)",
      same(genericsDoc, readerText.generics),
      `${JSON.stringify(lines(genericsDoc))} vs ${JSON.stringify(lines(readerText.generics))}`);
check("阅读页:段落结构没塌(两个 <p>)",
      await evalJs(`document.querySelectorAll('#content p').length`) === 2,
      await evalJs(`document.getElementById('content').innerHTML.slice(0, 200)`));

await send("Page.navigate", { url: docUrl(docs.script) });
await until(`!!document.getElementById('content') && document.getElementById('content').innerHTML.length > 0`);
const scriptDoc = await docBody();
check("阅读页:整段只有脚本示例的那篇正文没有消失",
      lines(scriptDoc).join("\n").includes("<script>alert(1)</script>"),
      JSON.stringify(scriptDoc));
check("阅读页与订阅面板读到的是同一份内容(脚本示例)",
      same(scriptDoc, readerText.script),
      `${JSON.stringify(lines(scriptDoc))} vs ${JSON.stringify(lines(readerText.script))}`);
// 示例是待显示的文字,不是要执行的脚本:正文里不许出现可执行的 script 节点,
// 原始 HTML 也不许进到 innerHTML 里(DOMPurify 照旧过一遍,这里是更严那一侧)。
check("阅读页:示例没有变成可执行的 script 节点",
      await evalJs(`document.querySelectorAll('#content script').length`) === 0,
      String(await evalJs(`document.querySelectorAll('#content script').length`)));
check("阅读页:正文 innerHTML 里是转义后的文字,没有裸标签",
      await evalJs(`!/<script/i.test(document.getElementById('content').innerHTML)`),
      await evalJs(`document.getElementById('content').innerHTML.slice(0, 200)`));

// ── 2b. 正文里写着「脚本开头」的那篇:整段内联脚本曾经压根没执行 ──
// `<!--` 之后再出现 `<script` 会把页面的内联 <script> 推进 HTML 的「双重转义」状态,
// 那之后页面末尾的 `</script>` 只退回一层、不结束脚本元素,于是 MD 没定义、字面
// renderer 没注册、正文渲染和纯文本降级全都没跑 —— 阅读页只剩标题,后面那个普通
// 段落一起消失(第 4 轮审查实测)。
await send("Page.navigate", { url: docUrl(docs.scriptComment) });
await until(`!!document.getElementById('content') && document.getElementById('content').innerHTML.length > 0`);
const commentDoc = await docBody();
check("阅读页:脚本开头示例那篇正文没有消失",
      lines(commentDoc).join("\n").includes("<!-- <script>"), JSON.stringify(commentDoc));
check("阅读页:它后面那个普通段落也还在",
      lines(commentDoc).join("\n").includes("这里是普通的第二段。"), JSON.stringify(commentDoc));
check("阅读页与订阅面板读到的是同一份内容(脚本开头)",
      same(commentDoc, readerText.scriptComment),
      `${JSON.stringify(lines(commentDoc))} vs ${JSON.stringify(lines(readerText.scriptComment))}`);
// 整段脚本真的跑完了:MD 定义了、片段收藏那一段也挂上了(它排在渲染之后,
// 内联脚本被吞掉时第一个消失的就是它)。
check("阅读页:内联脚本跑到底了(MD 有定义、片段收藏挂上了)",
      await evalJs(`typeof MD === 'string' && !!document.getElementById('snipbtn')`),
      JSON.stringify(await evalJs(`[typeof MD, !!document.getElementById('snipbtn')]`)));

// ── 2c. 同一个开头放进正常的 markdown 代码围栏里,照样不许吞掉全文 ──
await send("Page.navigate", { url: docUrl(docs.fenced) });
await until(`!!document.getElementById('content') && document.getElementById('content').innerHTML.length > 0`);
const fenced = await evalJs(`(() => {
  const c = document.getElementById('content');
  const pre = c.querySelector('pre code');
  return { fence: pre ? pre.textContent.trim() : '', text: c.textContent,
           scripts: c.querySelectorAll('script').length };
})()`);
check("阅读页:围栏里的脚本开头照旧按代码显示",
      fenced.fence === "<!--\n<script>", JSON.stringify(fenced.fence));
check("阅读页:围栏后面那个普通段落还在",
      fenced.text.includes("这是代码块之后的普通段落。"), JSON.stringify(fenced.text));
check("阅读页:围栏里的示例没有变成可执行的 script 节点",
      fenced.scripts === 0, String(fenced.scripts));

// ── 3. 反向:正常 markdown 照旧渲染 ──
await send("Page.navigate", { url: docUrl(docs.markdown) });
await until(`!!document.getElementById('content') && document.getElementById('content').innerHTML.length > 0`);
const md = await evalJs(`(() => {
  const c = document.getElementById('content');
  const pre = c.querySelector('pre code');
  return {
    h1: c.querySelectorAll('h1').length,
    li: c.querySelectorAll('ul li').length,
    link: c.querySelector('a[href="https://example.org/link"]')?.textContent || '',
    code: c.querySelector('p code')?.textContent || '',
    fence: pre ? pre.textContent.trim() : '',
    fenceScripts: pre ? pre.querySelectorAll('script').length : -1,
    table: c.querySelectorAll('table td').length,
  };
})()`);
check("正常 markdown:标题照旧是 <h1>", md.h1 === 1, String(md.h1));
check("正常 markdown:列表照旧是两项 <li>", md.li === 2, String(md.li));
check("正常 markdown:外链照旧是 <a>", md.link === "外链", md.link);
check("正常 markdown:行内代码照旧是 <code>", md.code === "行内代码", md.code);
check("正常 markdown:围栏里的脚本示例照旧按代码显示",
      md.fence === "<script>alert('在围栏里')</script>" && md.fenceScripts === 0,
      `${md.fence} / script 节点 ${md.fenceScripts}`);
check("正常 markdown:表格照旧是 <table>", md.table === 2, String(md.table));

check("整轮没有 JS 错误", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));

ws.close();
proc.kill();
const bad = results.filter(r => !r.ok);
// 收尾一律 writeSync、不许 process.exit:node 对管道上的 stdout/stderr 是异步写的,
// exit() 会把还没刷出去的那部分直接丢掉(原委见 tests/chat_height_probe.mjs)。
for (const r of results)
  writeSync(2, `${r.ok ? "  ok" : "FAIL"}  ${r.name}${r.detail ? " — " + r.detail : ""}\n`);
writeSync(2, `${results.length - bad.length} ok, ${bad.length} failed\n`);
writeSync(1, JSON.stringify(results) + "\n");
process.exitCode = bad.length ? 1 : 0;
