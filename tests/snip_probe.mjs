// 「☆ 收藏此段」在**四处正文**上都收得下、点得回去。
//
// 这条曾经只在 `/doc/…` 阅读页上成立。日报正文、问答回答、订阅阅读器、图鉴条目
// 各渲染一份正文,却都没有这个按钮;而前三者在 documents 表里没有行(日报写在
// `daily/`,不在任何 `SOURCES.root` 底下;回答压根不是文件),所以这件事不是前端
// 加个按钮 —— `favorites.path` 原来是 `NOT NULL REFERENCES documents(path)`,
// 没有 path 的片段一条都存不下去(见 docs/decisions/fav-snippets.md)。
//
// 判据一律落在**后果**上:库里那条收藏是什么(origin / origin_ref / path / quote)、
// 点回去之后屏幕上是哪一份正文、那段话有没有被高亮。不落在「按钮出现了没有」
// 这一步 —— 按钮浮出来而存不进去,是这条链最容易出现的那种半成品。
//
// 每条正断言都配一条反向的:选不满 4 字不许弹按钮、空态那三颗建议按钮和页头
// 标题(不是正文)选中不许弹、已经有文档的那一类**照旧**走阅读页链接。
//
// 外壳(起浏览器、连 CDP、页内帮手)在 ./snip_probe_lib.mjs,和
// `snip_probe_edges.mjs` 共用那一份。
//
//   HEADLESS_SHELL=<bin> node tests/snip_probe.mjs <baseURL> <manifest.json>
import {
  base, boot, check, clickJump, evalJs, finish, fx, highlightText, js,
  openFavList, send, sleep, snip, snipHighlight, until,
} from "./snip_probe_lib.mjs";

await boot();

// ── 1. 每日日报:有 vault 文件、**没有 documents 行** ──
await evalJs(`switchTab('daily'); true`);
// **先把"正文铺好了"单独记一条。** 冷启动时日报面板要等 settings/window/reports
// 三个接口,慢机器上这一步就是最容易超时的那个;不单独记的话它一超时,后面十条
// 判据跟着全红,报告里看不出真因是"正文压根没出来"(实测遇到过一次)。
const dailyReady = await until(
  `dailyCurrent === ${js(fx.dailyDate)} && $('daily-reader-body').textContent.includes(${js(fx.snipDaily)})`,
  40000);
check("日报面板铺出那一份正文", dailyReady,
  await evalJs(`dailyCurrent + ' | ' + $('daily-reader-body').textContent.slice(0, 60)`));
const d1 = await snip("#daily-reader-body", fx.snipDaily);
check("日报正文选中后浮出收藏按钮", d1.shown, `picked=${d1.picked}`);
const daily = await evalJs(`window.__favBy('daily')`);
check("日报片段落库:origin=daily + 日期 + 原文", daily.length === 1
  && daily[0].origin_ref === fx.dailyDate && daily[0].quote === fx.snipDaily
  && daily[0].path === "",
  JSON.stringify(daily.map(f => ({ ref: f.origin_ref, q: f.quote, path: f.path }))));
check("日报片段的标题是那一天的日报", daily.length === 1
  && daily[0].title === "日报 · " + fx.dailyDate, daily[0]?.title);

// 前后文锚点:**同一句话在这份日报里出现两次**,选的是第二处,而且**连着
// 段落边界那两个空白一起选** —— 审查复现的正是这种选法:引文首尾的空白不挪进
// 前后文的话,后端只 strip 引文、前后文照旧,整段锚点拼不回原文,定位退回只找
// 引文,于是静默落在第一处(见下面第 5 节那条偏移判据)。
const d1b = await snip("#daily-reader-body", fx.snipDup, 2, true);
check("重复句的第二处连着首尾空白也能收藏", d1b.shown, `picked=${d1b.picked}`);
check("选区真的带上了首尾空白", /^\s[\s\S]*\s$/.test(String(d1b.picked)),
  JSON.stringify(d1b.picked));
const dup = (await evalJs(`window.__favBy('daily')`)).filter(f => f.quote === fx.snipDup);
// 锚点记的必须是**第二处**那段前文。prefix 空(或记成第一处的)时这条就红 ——
// 而它决定的正是下面那次回跳落在哪一处。
check("锚点记下的是第二处的前文", dup.length === 1
  && dup[0].prefix.includes(fx.dupLeadSecond),
  JSON.stringify(dup.map(f => ({ p: f.prefix, s: f.suffix }))));

// ── 1b. 换日那一刻:出处跟着**屏幕上这份正文**走,不跟着"正在请求谁" ──
// 只按住"读某一份日报"那一次请求(别的接口照旧),这期间屏幕上还是今天那一份。
await evalJs(`window.__delayDaily(6000); true`);
await evalJs(`openDailyReport(${js(fx.dailyDateOld)}); true`);   // 故意不等它回来
const midway = await evalJs(`$('daily-reader-body').dataset.snipRef + ' | ' + dailyCurrent`);
const slow = await snip("#daily-reader-body", fx.snipDailySlow);
check("换日加载中,旧正文照旧收得下", slow.shown, `picked=${slow.picked} / ${midway}`);
const slowFav = (await evalJs(`window.__favBy('daily')`))
  .filter(f => f.quote === fx.snipDailySlow);
// 判据:出处是**屏幕上那份正文**那一天(今天),不是正在请求的那一天。
check("加载中收下的片段记的是屏幕上那一份的日期", slowFav.length === 1
  && slowFav[0].origin_ref === fx.dailyDate,
  JSON.stringify(slowFav.map(f => f.origin_ref)) + ` / 请求的是 ${fx.dailyDateOld} / ${midway}`);
// 等换日真落地,再确认正文容器上的出处换成了新那一天
const swapped = await until(
  `$('daily-reader-body').dataset.snipRef === ${js(fx.dailyDateOld)}`, 20000);
check("换日落地后出处跟着换成新那一份", swapped,
  await evalJs(`$('daily-reader-body').dataset.snipRef`));

// 反向:读不到那一天时阅读区写的是「读取失败」——**那几个字不是日报正文**,
// 一个字都不该收得下(审查实测:它被存成了一条日报片段)。
await evalJs(`window.__restoreDaily(); openDailyReport('1999-01-02'); true`);
const failed = await until(`$('daily-reader-body').textContent.includes('读取失败')`, 20000);
const failSel = await evalJs(`window.__snipSelect('#daily-reader-body', '可以重新生成')`);
const failSel2 = failSel === 'ok' ? failSel
  : await evalJs(`window.__snipSelect('#daily-reader-body', '这一天还没有生成日报')`);
await sleep(300);
check("读取失败的提示文案不算正文", failed && !(await evalJs(`window.__snipBtnShown()`)),
  `failed=${failed} picked=${failSel2} / ` + await evalJs(`$('daily-reader-body').textContent.slice(0, 40)`));
// 回到今天那一份:后面几节都从这儿接着走
await evalJs(`openDailyReport(${js(fx.dailyDate)}); true`);
await until(`$('daily-reader-body').dataset.snipRef === ${js(fx.dailyDate)}`, 20000);

// ── 2. 知识问答:连文件都没有 ──
await evalJs(`switchTab('ask'); openConv(${fx.convId}, true); true`);
const chatReady = await until(`$('chat-msgs').textContent.includes(${js(fx.snipChat)})`, 30000);
check("问答面板铺出那一轮回答", chatReady,
  await evalJs(`$('chat-msgs').textContent.slice(0, 60)`));
const d2 = await snip("#chat-msgs", fx.snipChat);
check("回答选中后浮出收藏按钮", d2.shown, `picked=${d2.picked}`);
const chat = await evalJs(`window.__favBy('chat')`);
check("问答片段落库:origin=chat + 会话 id + 原文", chat.length === 1
  && chat[0].origin_ref === String(fx.convId) && chat[0].quote === fx.snipChat
  && chat[0].path === "",
  JSON.stringify(chat.map(f => ({ ref: f.origin_ref, q: f.quote, path: f.path }))));
check("问答片段用那一轮的提问指代", chat.length === 1
  && chat[0].title.startsWith("问答 · "), chat[0]?.title);

// 收藏**提问本身**:标题该取它自己那一问,不该往前取到上一轮那个问题。
const d2b = await snip("#chat-msgs", fx.chatQuestion2);
check("第二轮的提问也收得下", d2b.shown, `picked=${d2b.picked}`);
const asked = (await evalJs(`window.__favBy('chat')`))
  .filter(f => f.quote === fx.chatQuestion2);
check("收藏提问本身时标题取它自己那一问", asked.length === 1
  && asked[0].title === "问答 · " + fx.chatQuestion2,
  JSON.stringify(asked.map(f => f.title)));

// 反向:空态那三颗建议按钮不是正文,选中不许弹按钮(它们和回答同在 #chat-msgs 里)
await evalJs(`newChat(); true`);
await until(`!!document.querySelector('.chat-suggest')`, 8000);
const sug = await evalJs(`window.__snipSelect('#chat-msgs', '最近一周公众号都提过哪些开源项目')`);
await sleep(300);
check("问答空态的建议按钮不算正文", sug === "ok" && !(await evalJs(`window.__snipBtnShown()`)),
  `picked=${sug}`);

// ── 3. 我的订阅:译文视图 + 已导出 → 绑那篇文档 ──
// 订阅面板默认落在公众号那一档(`subsKind` 初值 wechat),rss 那一档要自己点。
// 等的是**那颗来源切换按钮自己**而不是 `typeof subsCatalog`:后者是模块级变量,
// 脚本一加载就有了,早于来源目录拿回来(口径同 tests/reading_probe.mjs)。
await evalJs(`switchTab('subs'); true`);
if (!await until(`!!document.getElementById('subs-switch-rss')`, 25000))
  throw new Error("订阅面板没列出 rss 那一档");
await evalJs(`document.getElementById('subs-switch-rss').click(); true`);
await until(`typeof sourceRows !== 'undefined' && sourceRows.some(r => r.key === ${js(fx.itemKey)})`, 30000);
await evalJs(`readSourceItem(${js(fx.itemKey)}); true`);
const feedReady = await until(`$('source-reader-body').textContent.includes(${js(fx.snipFeed)})`, 20000);
check("订阅阅读器铺出那一篇正文", feedReady,
  await evalJs(`$('source-reader-body').textContent.slice(0, 60)`));
const d3 = await snip("#source-reader-body", fx.snipFeed);
check("订阅正文选中后浮出收藏按钮", d3.shown, `picked=${d3.picked}`);
const docs = await evalJs(`window.__favBy('doc')`);
check("订阅片段落库:origin=doc + 真 path + 原文", docs.length === 1
  && docs[0].path === fx.docPath && docs[0].quote === fx.snipFeed,
  JSON.stringify(docs.map(f => ({ path: f.path, q: f.quote }))));

// **有文档路径 ≠ 那段话在阅读页那一头找得到。** 这个阅读器按 textContent 原样
// 摊开正文(星号看得见),而 `/doc/…` 把同一份 md 按 markdown 渲染(星号没了)。
// 存之前那道核对(`snip.js` 的 `snipVerified`)要把这一段退回 origin=feed ——
// 回跳打开站外原文,而不是存一条点回去永远「没找到」的收藏。
const dmk = await snip("#source-reader-body", fx.snipMarked);
check("带 markdown 标记的那段也收得下", dmk.shown, `picked=${dmk.picked}`);
const marked = (await evalJs(`window.__favs()`)).filter(f => f.quote === fx.snipMarked);
check("它退回 origin=feed、不冒认能定位回文档", marked.length === 1
  && marked[0].origin === "feed" && marked[0].path === ""
  && marked[0].url === "https://example.org/skill-pipeline",
  JSON.stringify(marked.map(f => ({ o: f.origin, p: f.path, u: f.url }))));

// 反向:选不满 4 字不许弹(误触、双击一个词)
await evalJs(`window.__snipSelect('#source-reader-body', '开头'); true`);
await sleep(300);
check("选不满四个字不弹按钮", !(await evalJs(`window.__snipBtnShown()`)));

// 反向的另一头:**用键盘选的那一段照旧要弹按钮**。`__snipSelect` 自己派的是
// mouseup,那条路验不到键盘;这里只派 keyup(松开 Shift 的样子)。少了这条路,
// 只用键盘选字的人点不出这个功能,而屏幕上什么异常都没有(审查报告的 N2)。
const kbPicked = await evalJs(`(() => {
  const host = document.getElementById('source-reader-body');
  const w = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    const i = n.nodeValue.indexOf('开头一段交代背景');
    if (i < 0) continue;
    const r = document.createRange();
    r.setStart(n, i); r.setEnd(n, i + '开头一段交代背景'.length);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    document.dispatchEvent(new KeyboardEvent('keyup', { key: 'Shift', bubbles: true }));
    return 'ok';
  }
  return 'no-text';
})()`);
await sleep(300);
check("用键盘选的那一段也弹得出按钮(只派 keyup、不派 mouseup)",
  kbPicked === "ok" && !!(await evalJs(`window.__snipBtnShown()`)), `picked=${kbPicked}`);
// 选区清掉、按钮收回去:下面两条反向判据都是"不许弹"
await evalJs(`getSelection().removeAllRanges();
  document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true })); true`);
await sleep(300);

// 反向:页头标题不是注册过的正文容器
const head = await evalJs(`window.__snipSelect('.source-reader-heading', document.getElementById('source-reader-title').textContent.slice(0, 6))`);
await sleep(300);
check("没注册的区域选中不弹按钮", !(await evalJs(`window.__snipBtnShown()`)), `picked=${head}`);

// ── 3b. 图鉴详情抽屉:条目正文是一篇真文档,按 origin=doc 绑那条记录的 path ──
// 抽屉里并排着这个工具的**全部**记录,所以出处挂在每个 `.td-entry` 自己的 data
// 属性上、按选区往上找宿主 —— 这一条同时钉住那一步(注册在容器上、出处按条算)。
await evalJs(`switchTab('gallery'); true`);
if (!await until(`typeof openToolDrawer === 'function'`, 15000))
  throw new Error("图鉴面板的脚本没加载起来");
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
const toolReady = await until(
  `$('td-entries').textContent.includes(${js(fx.snipTool)})`, 25000);
check("图鉴抽屉铺出那一条记录的正文", toolReady,
  await evalJs(`$('td-entries').textContent.slice(0, 60)`));
const d4 = await snip("#td-entries", fx.snipTool);
check("图鉴条目正文选中后浮出收藏按钮", d4.shown, `picked=${d4.picked}`);
const tools = await evalJs(`window.__favBy('doc')`);
check("图鉴片段落库:origin=doc + 那条记录的 path + 原文",
  tools.some(f => f.path === fx.toolPath && f.quote === fx.snipTool),
  JSON.stringify(tools.map(f => ({ path: f.path, q: f.quote }))));

// 反向:小节标题("记录时间线 · N 条")和条目正文同住 #td-entries,选中不许弹
const sec = await evalJs(`window.__snipSelect('#td-entries', '记录时间线')`);
await sleep(300);
check("图鉴小节标题不算正文", sec === "ok" && !(await evalJs(`window.__snipBtnShown()`)),
  `picked=${sec}`);
// 抽屉关掉再往下走:遮罩盖着的话后面那几次点击都落不到收藏面板上
await evalJs(`closeToolDrawer(); true`);

// ── 4. 收藏面板:四处来源都列得出,各走各的那条回跳 ──
await evalJs(`switchTab('fav'); toggleFavMode(); true`);   // 切到完整列表
await until(`$('fav-list').querySelectorAll('.fav-group').length >= 4`, 15000);
// **阅读页链接连着它的 path 一起看。** 只问"有没有一条带 #kbq= 的 /doc 链接"
// 是过不了反向的:分流那一步去掉之后,日报和问答那两条片段也会拼出
// `/doc/#kbq=…` —— 判据照旧为真,而那串地址指向的是 iframe 里一张 404。
const shape = await evalJs(`(() => {
  const jumpOrigins = [...$('fav-list').querySelectorAll('.fav-jump')].map(b => {
    try { return JSON.parse(decodeURIComponent(b.dataset.jump)).origin; } catch { return '?'; }
  });
  const docLinks = [...$('fav-list').querySelectorAll('a[data-doc]')].map(a => ({
    href: a.getAttribute('href'), path: decodeURIComponent(a.dataset.path || ''),
  }));
  const badges = [...$('fav-list').querySelectorAll('.fav-origin')].map(b => b.textContent);
  return { jumpOrigins, docLinks, badges,
           cards: $('fav-list').querySelectorAll('.fav-group').length };
})()`);
// 五张卡:两篇文档各一张(图鉴那条记录、订阅那篇导出稿)、日报、问答,外加
// **退回 feed 那一条**(带 markdown 标记那段,它和同一篇的 doc 片段不同组:
// 分组按 `path || origin+ref`)。四类来源一个不少 —— 这才是这条的判据。
check("四处来源都列得出,各走各的那条回跳", shape.cards === 5
  && ["daily", "chat", "feed"].every(o => shape.jumpOrigins.includes(o))
  && shape.docLinks.length > 0, JSON.stringify(shape));
check("有文档的那一类照旧走阅读页链接", shape.docLinks.some(
  l => l.path === fx.docPath && l.href.includes("#kbq=")), JSON.stringify(shape.docLinks));
// 反向:**一条指向空路径的阅读页链接都不许有**。那是分流漏掉的样子 ——
// 点下去 iframe 里是 `/doc/` 的 404,而卡片看起来和能读的那条一模一样。
check("没有指向空路径的阅读页链接", shape.docLinks.every(l => !!l.path),
  JSON.stringify(shape.docLinks));
check("没文档的那两类给的是回跳按钮",
  shape.jumpOrigins.includes("daily") && shape.jumpOrigins.includes("chat"),
  JSON.stringify(shape.jumpOrigins));
// 角标是每组一个,**只给非文章那几类**:五组里有两组是文档,它们不带角标。
check("来源角标只标非文章那几类", shape.badges.length === 3
  && ["每日日报", "知识问答", "订阅内容"].every(b => shape.badges.includes(b)),
  JSON.stringify(shape.badges));

// ── 4b. 「回原文」那一步也要认协议 ──
// `url` 是内容自带的(RSS 的 link),后端只管长度。同一份数据渲染成 `<a>` 时各处
// 都过 `safeUrl`,只有订阅那条回跳曾经把它直接交给 `window.open`(审查报告的
// N5)—— `javascript:` 真交出去就是在当前源上跑一段脚本。判据落在**交给
// window.open 的到底是什么**:把它换成记账的版本,不真导航。
await evalJs(`window.__opened = [];
  window.open = (u) => { window.__opened.push(String(u)); return null; }; true`);
const EVIL_Q = "这条订阅内容的链接是一段脚本不是地址";
const evilPost = await evalJs(`(async () => {
  const r = await fetch('/api/favorites', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ quote: ${js(EVIL_Q)}, origin: 'feed', origin_ref: 'rss:1',
      origin_title: '一条链接写成脚本的订阅内容',
      origin_url: 'javascript:window.__pwned = 1' }) });
  return r.status + ' ' + (await r.text()).slice(0, 200);
})()`);
// 面板重铺是异步的,而列表里本来就有别的回跳按钮 —— 等**这一条**真出现再点,
// 不然点在旧 DOM 上,判据会表现成"点不到"
await openFavList();
const evilReady = await until(`[...$('fav-list').querySelectorAll('.fav-jump')].some(b => {
  try { return JSON.parse(decodeURIComponent(b.dataset.jump)).q === ${js(EVIL_Q)}; }
  catch { return false; } })`, 15000);
const evilClicked = evilReady && await evalJs(`(() => {
  for (const b of $('fav-list').querySelectorAll('.fav-jump')) {
    let j; try { j = JSON.parse(decodeURIComponent(b.dataset.jump)); } catch { continue; }
    if (j.q === ${js(EVIL_Q)}) { b.click(); return true; }
  }
  return false;
})()`);
await sleep(400);
check("收藏面板里能点到那条脚本链接的回跳", evilClicked,
  `ready=${evilReady} post=${evilPost}`);
check("javascript: 链接一次也没交给 window.open",
  (await evalJs(`JSON.stringify(window.__opened)`)) === "[]",
  await evalJs(`JSON.stringify(window.__opened)`));
check("认不出协议时走的是「没有站外链接」那条退路,如实说了话",
  (await evalJs(`window.__toastText()`)).includes("没有站外链接"),
  await evalJs(`window.__toastText()`));
// 反过来钉一边:**认得出的链接照旧开出去**(不然"一律不 open"也能过上面那条)
await openFavList();
const okClicked = await evalJs(`(() => {
  for (const b of $('fav-list').querySelectorAll('.fav-jump')) {
    let j; try { j = JSON.parse(decodeURIComponent(b.dataset.jump)); } catch { continue; }
    if (j.origin === 'feed' && String(j.url || '').startsWith('http')) { b.click(); return true; }
  }
  return false;
})()`);
await sleep(400);
check("https 的那条照旧开去站外原文", okClicked
  && (await evalJs(`JSON.stringify(window.__opened)`)).includes("https://example.org/"),
  await evalJs(`JSON.stringify(window.__opened)`));

// ── 5. 回跳:点日报那条 → 回到那一份正文 + 那段话被高亮 ──
// **点不到就记红、接着往下跑**,不许抛 —— 定点注入一处之后这颗按钮常常压根不
// 存在(落库那一步断了、或者分流那一步没分),而探针在这儿崩掉就只剩一条栈,
// 说不出"哪几条判据红了"。反向验证要的正是后者。
const clickedDaily = await clickJump("daily", fx.snipDaily);
check("收藏面板里能点到日报那条的回跳", clickedDaily);
// **等的是高亮,不是 `dailyCurrent`** —— 后者在 `openDailyReport` 第一行就被
// 设好了(它要先把列表里那一行标亮),正文和定位还在等接口回来。拿它当判据
// 就是每次都抢在定位之前读,读到的是上一次那段话。
const hi = await snipHighlight(fx.snipDaily);
check("日报片段点回去落在那一天的日报上",
  await evalJs(`activeTab === 'daily' && dailyCurrent === ${js(fx.dailyDate)}`),
  await evalJs(`activeTab + ' / ' + dailyCurrent`));
// 判据是"这个 Highlight 里那个 Range 的文字正是收藏的那一段",不是"注册过一个
// 名字" —— 后者在定位到错误位置时照样为真。
check("那段话被高亮出来", hi === fx.snipDaily, hi);

// 反向:回跳按钮不许把地址栏的 tab 搞丢(workspace 那条深链口径)
check("回跳之后地址栏仍是日报", (await evalJs(`location.hash`)).startsWith("#daily"),
  await evalJs(`location.hash`));

// 回跳到重复句那条:**落点必须是第二处**。只比文字的话两处都对得上,
// 而用户看到的是另一段上下文里那句一模一样的话。
await evalJs(`switchTab('fav'); true`);
await until(`$('fav-list').querySelectorAll('.fav-jump').length >= 1`, 15000);
const clickedDup = await clickJump("daily", fx.snipDup);
check("收藏面板里能点到重复句那条的回跳", clickedDup);
const want2 = await evalJs(`window.__textOffset('#daily-reader-body', ${js(fx.snipDup)}, 2)`);
let gotOff = -1;
for (let i = 0; i < 100 && gotOff !== want2; i++) {
  await sleep(150);
  gotOff = await evalJs(`window.__hiOffset('#daily-reader-body')`);
}
check("重复句回跳落在第二处,不是第一处", want2 > 0 && gotOff === want2,
  `期望偏移 ${want2},实际 ${gotOff}`);

// ── 6. 问答那条回跳 ──
await evalJs(`switchTab('fav'); true`);
await until(`$('fav-list').querySelectorAll('.fav-jump').length >= 1`, 15000);
const clickedChat = await clickJump("chat", fx.snipChat);
check("收藏面板里能点到问答那条的回跳", clickedChat);
const hiChat = await snipHighlight(fx.snipChat);
check("问答片段点回去落在那个对话上",
  await evalJs(`activeTab === 'ask' && chatConvId === ${fx.convId}`),
  await evalJs(`activeTab + ' / ' + chatConvId`));
check("回答里那段话被高亮出来", hiChat === fx.snipChat, hiChat);

// ── 7. 原日报已经不在了:说清楚,不装作跳回去了 ──
// 退回"打开第一份"的话,屏幕上是另一天的日报、没有任何提示,而待定位还挂着:
// 用户以为跳到了,其实在读别的东西。
const GONE_QUOTE = "出自一份已经删掉的日报";
await evalJs(`fetch('/api/favorites', { method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ quote: ${js(GONE_QUOTE)}, origin: 'daily',
    origin_ref: '1999-01-01', origin_title: '日报 · 1999-01-01' }) }).then(r => r.ok)`);
await evalJs(`switchTab('fav'); loadFavPanel(); true`);
await until(`[...$('fav-list').querySelectorAll('.fav-jump')].some(b =>
  (b.dataset.jump || '').includes('1999-01-01'))`, 15000);
const clickedGone = await clickJump("daily", GONE_QUOTE);
check("收藏面板里能点到那条回跳", clickedGone);
const toldGone = await until(`window.__toastText().includes('1999-01-01')`, 20000);
check("原日报不在了会当场说清楚", toldGone, await evalJs(`window.__toastText()`));
check("这次回跳被丢掉,不留在那儿等着",
  await evalJs(`dailyPendingSnip === null`), await evalJs(`JSON.stringify(dailyPendingSnip)`));

// ── 7b. 歧义锚点:前后文对不上时**不许静默挑第一处** ──
// 没有前后文、引文又在正文里出现两次(这里用重复句的后半截)。老口径会直接
// `indexOf(quote)` 落在第一处:用户看到一个高亮,而它在另一段上下文里 ——
// 一句"没找到"比一个落错地方的高亮诚实。
const AMBI = fx.snipDup.slice(-7);
await evalJs(`fetch('/api/favorites', { method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ quote: ${js(AMBI)}, origin: 'daily',
    origin_ref: ${js(fx.dailyDate)}, origin_title: '日报 · 歧义' }) }).then(r => r.ok)`);
await evalJs(`switchTab('fav'); loadFavPanel(); true`);
await until(`[...$('fav-list').querySelectorAll('.fav-jump')].some(b =>
  (b.dataset.jump || '').includes(encodeURIComponent(${js(AMBI)})))`, 15000);
const clickedAmbi = await clickJump("daily", AMBI);
check("收藏面板里能点到歧义那条的回跳", clickedAmbi);
const ambiToast = await until(`window.__toastText().includes('没找到这段收藏')`, 20000);
check("歧义锚点如实说没找到", ambiToast, await evalJs(`window.__toastText()`));
check("歧义锚点不许高亮到第一处", (await highlightText()) === "(没有高亮)",
  await highlightText());

// ── 8. 独立阅读页:这边存的锚点,那边点回去要落得上 ──
// `/doc/…` 是脱离 kb-web 也能直接打开的独立产物,自带一份内联的采集+定位
// (`kb/web/pages.py` 的 `_DOC_SNIP_JS`)。**两份实现共用同一套锚点坐标系**,
// 差一个空格就对不上 —— 所以这一节真跳过去看高亮,而不是只看链接长什么样。
const docHref = shape.docLinks.find(
  l => l.path === fx.docPath && l.href.includes("#kbq="));
check("订阅那条片段给出了带锚点的阅读页链接", !!docHref, JSON.stringify(shape.docLinks));
if (docHref) {
  await send("Page.navigate", { url: base + docHref.href });
  await until(`!!document.getElementById('content')
    && document.getElementById('content').textContent.includes(${js(fx.snipFeed)})`, 25000);
  const hiDoc = await snipHighlight(fx.snipFeed, 10000);
  check("阅读页上那段话被高亮出来", hiDoc === fx.snipFeed, hiDoc);
  // **跨段落的引文**:订阅阅读器那边两段之间是空行,阅读页这边渲染成两个 <p>
  // (串起来只剩一个换行)—— 不把空白压平的话,这条锚点在这一页永远找不到。
  const across = "剩下的都是工程问题。 请执行 " + fx.snipMarkedPlain + " 并核对结果。";
  // **手搭的裸锚点要带 `u: 1` 和 `v`(这一篇此刻的版本号)**:这一页只在「采集
  // 那一刻这段话就只有一处」**且「要搜的这一篇就是采集时那一篇」**时才采信裸引文
  // 那一档(`kb/web/pages.py` 的 `seek`),两个数缺哪个都按「说不清」办。真实链路
  // 上它们由 `fav.js` 从 `favorites.quote_unique` / `doc_version` 带过来;这一段话
  // 在那一篇里确实只有一处,而这一页此刻就是收藏时那一版(这一节还没动过盘上那
  // 篇 md)。反过来那两边(`u: 0` 不许采信、版本不等不许采信)在
  // snip_probe_bind.mjs 里。
  await evalJs(`location.hash = '#kbq=' + encodeURIComponent(JSON.stringify(
    { q: ${js(across)}, p: '', s: '', u: 1, v: DOC_VERSION })); true`);
  const hiAcross = await snipHighlight(across, 10000);
  check("跨段落的引文在阅读页上也定位得到", hiAcross === across, hiAcross);
  // **这一页的定位也要求每一档唯一**(同 7b,那一条验的是 app 那一侧)。这一篇
  // 里有一整段一字不差出现两次,连它前后各三十个字都一样 —— 拿那一档的前后文
  // 手搭一条锚点,每一侧都不止一处候选,这一页必须如实说没找到、**不许高亮到第一处**。
  // 两份实现各要有自己的红(口径同 CLAUDE.md「视觉语言」那三处产物)。
  const ambiP = fx.ambiLead.slice(-30), ambiS = fx.ambiTail.slice(0, 30);
  await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
    encodeURIComponent(JSON.stringify({ q: ${js(fx.ambiQuote)},
      p: ${js(ambiP)}, s: ${js(ambiS)} })); true`);
  await sleep(1200);
  check("阅读页上歧义锚点不许高亮到第一处",
    (await highlightText()) === "(没有高亮)", await highlightText());
}

finish();
