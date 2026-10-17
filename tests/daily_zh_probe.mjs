// 日报正文里的本地译文入口(kb/static/js/daily_zh.js)在真页面上的回归。
//
// 验五件事:
//   ① 出处是译文的那一条后面多一个入口,**不是译文的那一条一个字都不多**;
//   ①b 同一篇被重复收录时(url 一份存页面那个编码形式、一份存原样),字面对得上
//      的那一行没译文**不许**把等价的那一份挡在候选外面;
//   ② 补上去的字不进正文的文本(`textContent` / 选区都读不到它)—— 日报正文
//      同时是「收藏此段」的采集面,插一个真字符会让已收下的片段整体错位;
//   ③ 点开从**左边**滑出阅读器,读的是 vault 里那一篇中文;而别处打开的阅读器
//      照旧从右边出来(反过来钉一边:换边不许粘在阅读器上);
//   ④ 回包落地时屏幕上已经不是那一份正文了就不插 —— 两种形态各一条。
//
// 需要一个跑着沙箱数据的 kb-web:
//   python3 tests/daily_sandbox.py /tmp/kb-daily-zh-sandbox 8801
//   python3 tests/daily_zh_sandbox.py /tmp/kb-daily-zh-sandbox
//   KB_DATA_DIR=... KB_VAULT_ROOT=... uv run uvicorn kb.server:app --port 8802
// CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、不连网)。
//
//   HEADLESS_SHELL=<bin> node tests/daily_zh_probe.mjs http://127.0.0.1:8802 2026-10-16 https://example.org/zh-of-en 暗号
import { writeSync } from "node:fs";
import { launchShell } from "./probe_shell.mjs";

const base = process.argv[2];
const REPORT = process.argv[3];     // 摆好那份历史日报的日期
const ZH_URL = process.argv[4];     // 译文那一篇的原文地址
const ZH_MARK = process.argv[5];    // 译文正文里的暗号
const HREFS = JSON.parse(process.argv[6] || "{}");   // 后端算的「库里 url → 页面 href」
const DUP = JSON.parse(process.argv[7] || "{}");    // 重复收录那一对:{href, zhPath}
const ZH_PATH = "source/rss/zh-of-en.md";
const PLAIN_URL = "https://example.org/a";   // 库里有、但不是译文的那一条

const { proc, port } = await launchShell(["--window-size=1440,1000"]);
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
const until = async (expr, ms = 20000) => { const dl = Date.now() + ms; while (Date.now() < dl) { if (await evalJs(expr)) return true; await sleep(120); } return false; };
await send("Runtime.enable"); await send("Page.enable");

const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok: !!ok, detail: String(detail) });

await send("Page.navigate", { url: base + "/#daily" });
await until(`typeof dailyLoaded !== 'undefined' && dailyLoaded`, 30000);
await evalJs(`loadDailyPanel()`);
await until(`!!document.querySelector('[data-report="${REPORT}"]')`);
await evalJs(`openDailyReport('${REPORT}')`);
await until(`document.getElementById('daily-reader-title').textContent === '日报 · ${REPORT}'`);
// **先确认正文本身读到了。** 这个入口是额外的一条路,它那一侧出任何问题都不该
// 把一份**已经读到了**的日报换成「读取失败」四个字(`openDailyReport` 的 catch
// 就在那儿等着)—— 代价完全不对等,而且那块提示连「收藏此段」都收不了。
check("日报正文读到了(额外入口不许把它盖成「读取失败」)",
      /素材清单/.test(await evalJs(`document.getElementById('daily-reader-body').textContent`)),
      (await evalJs(`document.getElementById('daily-reader-body').textContent`)).slice(0, 60));
// 入口是异步补上去的(要问一次后端),等它落地。
await until(`!!document.querySelector('#daily-reader-body a.daily-zh')`);

// ── 1. 有译文的那一条有入口,没译文的那一条没有 ──────────────────────────────
const shot = JSON.parse(await evalJs(`(() => {
  const body = document.getElementById('daily-reader-body');
  const refs = [...body.querySelectorAll('a.daily-ref')];
  const at = t => refs.find(a => a.textContent.trim() === t);
  const zhOf = a => { const n = a && a.nextElementSibling;
                      return n && n.classList.contains('daily-zh') ? n : null; };
  const four = at('[4]'), one = at('[1]');
  const z = zhOf(four);
  return JSON.stringify({
    fourHref: four ? four.getAttribute('href') : '',
    oneHref: one ? one.getAttribute('href') : '',
    hasZh: !!z, oneHasZh: !!zhOf(one),
    total: body.querySelectorAll('a.daily-zh').length,
    href: z ? z.getAttribute('href') : '',
    title: z ? z.title : '',
    label: z ? z.getAttribute('aria-label') : '',
    kids: z ? z.childNodes.length : -1,
    text: z ? z.textContent : '?',
    display: z ? getComputedStyle(z).display : '',
    after: z ? getComputedStyle(z, '::after').content : '',
  }); })()`));
check("出处是译文的那一条,编号后面多了入口",
      shot.hasZh && shot.fourHref === ZH_URL, JSON.stringify(shot));
check("入口指向 vault 里那一篇", shot.href === "/doc/" + ZH_PATH.split("/").map(encodeURIComponent).join("/"),
      shot.href);
check("库里有、但不是译文的那一条一个字都没多(反过来钉一边)",
      !shot.oneHasZh && shot.oneHref === PLAIN_URL, JSON.stringify(shot));
// 两条:`[4]` 那一篇,加下面 ①b 那一组的 `[5]`。整篇只有这两处出处是译文。
check("入口只长在该长的地方", shot.total === 2, shot.total);
check("提示里带着原文标题", /原文标题：A runtime release, in English/.test(shot.title), shot.title);
check("读屏软件读得到它是什么", shot.label === "读本地中文译文", shot.label);

// ── 1b. 同一篇被重复收录:字面命中那一行没译文,不许挡住等价的那一份 ─────────
// 沙箱里摆了两行:url 一份存页面那个编码形式(没译过)、一份存原样中文(译过)。
// 原来的实现拿「库里有一行 url 跟你逐字相同」当「这个地址的候选收齐了」,于是
// 中文那一份压根没进候选 —— 面板上这个编号没有「译」,重开日报还是没有
// (2026-10-17 审查 F2 续)。**这一组是端到端的那一半**:后端判据在
// tests/test_daily_trans.py,这里钉的是「页面上那个 href 确实是编码形式」加
// 「照它问回来的是译过的那一篇」。
const dup = JSON.parse(await evalJs(`(() => {
  const body = document.getElementById('daily-reader-body');
  const five = [...body.querySelectorAll('a.daily-ref')].find(a => a.textContent.trim() === '[5]');
  const n = five && five.nextElementSibling;
  const z = n && n.classList.contains('daily-zh') ? n : null;
  return JSON.stringify({
    refHref: five ? five.getAttribute('href') : '',
    hasZh: !!z,
    path: z ? decodeURIComponent(z.dataset.path || '') : '',
    url: z ? z.dataset.url : '',
  }); })()`));
check("页面上那个 href 是编码形式(marked 把中文路径编掉了,和库里没译那一行逐字相同)",
      dup.refHref === DUP.href, JSON.stringify({ got: dup.refHref, want: DUP.href }));
check("字面对得上的那一行没译文,渲染等价的那一份照样找得到", dup.hasZh, JSON.stringify(dup));
check("找到的是译过的那一篇,不是字面命中那一篇", dup.path === DUP.zhPath,
      JSON.stringify({ got: dup.path, want: DUP.zhPath }));
check("抽屉顶上那个「原文」还指着你点的那个地址(不许换成库里另一份)",
      dup.url === DUP.href, dup.url);

// ── 2. 那个字不进正文的文本 ─────────────────────────────────────────────────
// 日报正文同时是「收藏此段」的采集面:片段的回跳锚点按正文的文字坐标系算
// (snip_text.js),往里插一个真字符,**已经收下的片段**就整体对不上。所以这个
// 入口里不许有文本节点,字只能由 `::after` 画 —— 这一组钉的就是这件事。
check("入口里没有文本节点", shot.kids === 0 && shot.text === "", JSON.stringify(shot));
check("字确实画出来了(不是压根没显示)", /译/.test(shot.after), shot.after);
check("样式是行内的(块级会在坐标系里凭空多一个换行)",
      shot.display === "inline" || shot.display.startsWith("inline-"), shot.display);
const textShot = JSON.parse(await evalJs(`(() => {
  const body = document.getElementById('daily-reader-body');
  const r = document.createRange(); r.selectNodeContents(body);
  const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
  const picked = sel.toString(); sel.removeAllRanges();
  return JSON.stringify({ dom: body.textContent, sel: picked }); })()`));
check("正文的 textContent 里只有编号,没有那个字",
      /\[4\]/.test(textShot.dom) && !/\[4\]译/.test(textShot.dom),
      textShot.dom.slice(textShot.dom.indexOf("[4]") - 20, textShot.dom.indexOf("[4]") + 10));
check("屏幕选区里也没有(「收藏此段」收的就是它)",
      /\[4\]/.test(textShot.sel) && !/\[4\]译/.test(textShot.sel),
      textShot.sel.slice(textShot.sel.indexOf("[4]") - 20, textShot.sel.indexOf("[4]") + 10));

// ── 3. 点开:从左边滑出,读的是那一篇中文 ────────────────────────────────────
// 点击这一句**不许抛**:入口没出现时整个探针会在这里崩掉,后面十几条一条都
// 跑不了、JSON 也发不出去 —— 坏掉一件事要能看见是哪几条坏了。
await evalJs(`(() => { const z = document.querySelector('#daily-reader-body a.daily-zh');
  if (z) z.click(); return !!z; })()`);
await until(`document.getElementById('reader').style.display !== 'none'`);
// 量的是**计算样式**而不是 `getBoundingClientRect()`:滑入那 0.18s 里元素正被
// `transform: translateX(-40px)` 推着,照 rect 量会量到动画中途的 -40(第一次跑
// 就是这么假红的)。靠边是 left/right 定的,transform 不改它们。
const opened = JSON.parse(await evalJs(`(() => {
  const r = document.getElementById('reader'), cs = getComputedStyle(r);
  return JSON.stringify({
    left: cs.left, right: cs.right, cls: r.className,
    frame: document.getElementById('reader-frame').getAttribute('src') || '',
    title: document.getElementById('reader-title').textContent,
    src: document.getElementById('reader-src').getAttribute('href') || '',
    srcShown: document.getElementById('reader-src').style.display !== 'none',
    obs: document.getElementById('reader-obs').getAttribute('href') || '',
  }); })()`));
check("抽屉是从左边出来的", opened.left === "0px" && /from-left/.test(opened.cls),
      JSON.stringify(opened));
check("抽屉里装的是 vault 里那一篇", /\/doc\/source\/rss\/zh-of-en\.md$/.test(opened.frame),
      opened.frame);
check("抽屉头上写着这一篇的标题", opened.title === "一个从英文译过来的运行时发布", opened.title);
check("原文入口还在,指向站外那个地址", opened.srcShown && opened.src === ZH_URL,
      JSON.stringify(opened));
check("Obsidian 入口也拼出来了(不是一个空的 #)", /^obsidian:\/\/open\?vault=/.test(opened.obs),
      opened.obs);
// 抽屉里那一篇确实是中文 —— iframe 跨 target 不好读,直接取同一个地址比内容。
const docHtml = await evalJs(`(async () => (await (await fetch(${JSON.stringify("/doc/" + ZH_PATH)})).text()))()`);
check("那一篇的正文是中文译文", docHtml.includes(ZH_MARK), docHtml.slice(0, 120));

// 别处打开的阅读器照旧从右边出来:换边是调用方说了算的一次性修饰,不许粘住。
await evalJs(`closeReader()`);
await evalJs(`(() => { const a = document.createElement('a');
  a.href = ${JSON.stringify("/doc/" + ZH_PATH)}; a.dataset.doc = '1';
  a.dataset.title = '随便一篇'; openReader(a); return true; })()`);
const plainOpen = JSON.parse(await evalJs(`(() => { const r = document.getElementById('reader');
  const cs = getComputedStyle(r);
  return JSON.stringify({ left: cs.left, right: cs.right, cls: r.className }); })()`));
check("别处打开的阅读器还是从右边出来", plainOpen.right === "0px" && plainOpen.left !== "0px"
      && !/from-left/.test(plainOpen.cls), JSON.stringify(plainOpen));
await evalJs(`closeReader()`);

// ── 4. 回包落地时屏幕上已经换了正文 ────────────────────────────────────────
// 这个入口是异步补的,而日报可以在这期间被翻走。两种形态各一条:换了一天
// (正文容器记着的日期变了)、同一天重新渲染过(手上那批节点已经离开文档树)。
// 两条都验「屏幕上那一份不受影响」,不是验内部标志。
const hold = async (afterSend) => {
  await evalJs(`(() => {
    window.__realPipePost = pipePost;
    window.__release = null;
    pipePost = () => new Promise(res => { window.__release = res; });
    return true; })()`);
  await evalJs(`(() => { markDailyTranslations(document.getElementById('daily-reader-body'),
                                               '${REPORT}'); return true; })()`);
  await until(`!!window.__release`);
  await evalJs(afterSend);
  await evalJs(`(() => { window.__release({ docs: { ${JSON.stringify(ZH_URL)}:
      { path: ${JSON.stringify(ZH_PATH)}, title: '译文', original_title: '' } }, vault: 'v' });
    return true; })()`);
  await sleep(250);
  const n = await evalJs(`document.querySelectorAll('#daily-reader-body a.daily-zh').length`);
  await evalJs(`(() => { pipePost = window.__realPipePost; return true; })()`);
  return n;
};
// 换上去那一份**也引用同一篇译文**(连着两天的日报引用同一篇是常事)——
// 不这么摆的话,一个"回包落地后重新查一遍 DOM"的实现在这条用例下照样全绿:
// 它在新正文里压根找不到编号,于是什么也没插。
const movedAway = await hold(`(() => { setDailyBody(
  '<p>别的日报 <a class="daily-ref" href=${JSON.stringify(ZH_URL)}>[4]</a></p>', '1999-01-01');
  return true; })()`);
check("翻到别的日报之后,那次回包不再往屏幕上插入口", movedAway === 0, movedAway);
// 回到那一份:**同一天**重新渲染一次,手上那批节点就离开了文档树 —— 旧回包同样
// 不许落到屏幕上那份新正文里(日期这一道挡不住它,它要的就是这一天)。
// 换正文要**同步**换掉(`setDailyBody` 而不是 `openDailyReport`):后者要先等一次
// 网络往返,回包会赶在正文换掉之前落地,这条用例就变成在验别的事情了。
await evalJs(`openDailyReport('${REPORT}')`);
await until(`document.querySelectorAll('#daily-reader-body a.daily-zh').length === 2`);
const rerendered = await hold(`(() => { setDailyBody(
  '<p>同一天、重新渲染过 <a class="daily-ref" href=${JSON.stringify(ZH_URL)}>[4]</a></p>',
  '${REPORT}'); return true; })()`);
check("同一天重新渲染过,旧回包也不落到新正文上", rerendered === 0, rerendered);
// **而且这只是让出一次机会,不是永久丢掉**:重新打开那一份,入口照旧补得上
// (上面两条的代价只有这么大 —— 否则"保守地不插"就成了"再也看不见")。
await evalJs(`openDailyReport('${REPORT}')`);
check("重新打开之后入口照旧补得上",
      await until(`document.querySelectorAll('#daily-reader-body a.daily-zh').length === 2`));

// 重复调用不该补出第二个。
await evalJs(`(() => { markDailyTranslations(document.getElementById('daily-reader-body'),
                                             '${REPORT}'); return true; })()`);
await sleep(600);
check("同一份正文上再调一次也不多长一个",
      (await evalJs(`document.querySelectorAll('#daily-reader-body a.daily-zh').length`)) === 2);

// ── 5. 「库里的 url 渲染到页面上长什么样」两边逐位对齐 ──────────────────────
// 后端为了把 href 对回库里那一篇,照着 marked 的 `cleanUrl` 又写了一遍
// (`daily_trans._as_href`)。**两个实现逐位对齐不许按规范推** —— 窄了就是带
// 保留字符的出处永远拿不到入口,宽了就是把两篇不同的文章当成同一个地址(F2 那个
// 形态:点「译」打开另一篇)。所以拿页面上真的 `encodeURI` 跟它逐个比。
const mismatch = JSON.parse(await evalJs(`(() => {
  const want = ${JSON.stringify(HREFS)};
  const bad = {};
  for (const [url, backend] of Object.entries(want)) {
    const front = encodeURI(url).replace(/%25/g, '%');   // marked 的 cleanUrl
    if (front !== backend) bad[url] = { front, backend };
  }
  return JSON.stringify(bad); })()`));
check("后端算的 href 和页面上 marked 那条规则逐位相同",
      Object.keys(HREFS).length >= 7 && !Object.keys(mismatch).length,
      JSON.stringify(mismatch));

check("页面没有脚本报错", pageErrors.length === 0, pageErrors.join(" | "));

// stdout 要一次写完再退出(理由同 fulltext_probe.mjs)。
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
ws.close(); proc.kill();
process.exitCode = bad.length ? 1 : 0;
