// 「这一处是不是他选的那一处」—— **原处身份**,这条链上最后一类"当场看着成功"。
//
// 前几份管的是收得下(`snip_probe.mjs`)、收下来的是不是屏幕上那份文字
// (`snip_probe_shown.mjs`)、收下来之后还作不作数(`snip_probe_edges.mjs`、
// `snip_probe_chat.mjs`)、列表里那几截原文(`snip_probe_cards.mjs`)、绑在哪一处
// 哪一版(`snip_probe_bind.mjs`)。这一份管的是**位置身份不许由"现在正好对得上"
// 推出来**,三格 + 独立阅读页那一份实现自己再判一次:
//
//   1 **核对那一步不许凭裸引文命中就算过。** 图鉴抽屉开着的那几分钟里,盘上那一篇
//     把选中那一处连它的引子一起换掉、另一处一字不差的同句还在 —— 按裸引文命中
//     它就等于"核过了":版本号跟着换成新那一版、落库返回 200,点回去高亮的是用户
//     从没选过的那一处(审查实测:旧页面版本 ff4fb2dd、核对版本 c54d6364,保存行
//     的 doc_version 被换成后者,回看落在第一节)。
//   2 **采集那一刻就定不住的锚点,往后哪一档都不许定位。** 前后各两百字也一字不差
//     的两处,采集那一刻谁也说不出用户选的是第几处(屏幕上当场说了一句"可能定位
//     不到");后来原处被整节删掉,剩下的那一处成了整段锚点的唯一命中 —— 采信它
//     就是高亮一处从没被收藏过的文字(审查用生产函数实测:采集偏移 740,删掉第二
//     节之后回查到 244)。所以这件事要**跟着收藏落库**(`favorites.anchor_exact`)。
//   3 **反过来,「整份正文就是这段引文」不许被这套收紧判死。** 日报整段正文收下来、
//     一个字都没改,点回去却是一句"没找到"(审查实测):采集那头按"这份 text 就是
//     此刻这一份"算出"定得到",回跳那头两侧上下文天生都是空的、又拿不出同一份语料
//     的证明,于是所有完整正文选区一律失效。
//
// 判据一律落在后果上:**库里那条收藏是什么**(有没有、`quote_unique`/`anchor_exact`
// 记的是几)、**高亮落在第几处**(不是"文字对不对" —— 两处的文字一模一样)、
// **屏幕上那句提示说的是哪一种**。第 2、3 格的语料由 `setDailyBody()` 现摆 ——
// 那是日报正文的**唯一写入口**(产品代码里那一个),出处跟着正文一起落下来。
//
//   HEADLESS_SHELL=<bin> node tests/snip_probe_ident.mjs <baseURL> <manifest.json>
import {
  appToastClear, base, boot, check, clickJump, DOC_HELPERS, docSnip, evalJs,
  favDocHref, finish, fx, highlightText, js, openFavList, send, sleep, snip,
  snipHighlight, until,
} from "./snip_probe_lib.mjs";

/** 沙箱那几条路由(真改盘上那一篇)。回 HTTP 状态。*/
const sandbox = (route, path, text = "") => evalJs(
  `fetch('/__snip__/' + ${js(route)} + '?path=' + encodeURIComponent(${js(path)})
     + (${js(text)} ? '&text=' + encodeURIComponent(${js(text)}) : ''),
     { method: 'POST' }).then(r => r.status)`);

/** 库里引文等于 q 的那几条(只留判据要看的几个字段),整成一段 JSON 文本。*/
const rowsOf = q => evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  return JSON.stringify(d.favorites.filter(f => f.quote === ${js(q)}).map(f => ({
    o: f.origin, p: f.path || null, u: f.quote_unique, e: f.anchor_exact,
    pre: f.prefix, suf: f.suffix })));
})()`);

/** 现摆一份日报正文(产品代码里那个唯一写入口),出处记成夹具那一天。*/
const stage = html => evalJs(
  `(() => { setDailyBody(${js(html)}, ${js(fx.dailyDate)}); return true; })()`);

/** 走**真回跳**:库里那条 → `snipSpecOf` → `consumeSnipJump`(日报面板消费待定位
 *  的那一句就是它)。回 'ok' / 'none'。 */
const jump = q => evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  const f = d.favorites.find(x => x.quote === ${js(q)});
  if (!f) return 'none';
  dailyPendingSnip = { ref: ${js(fx.dailyDate)}, spec: snipSpecOf(f) };
  dailyPendingSnip = consumeSnipJump(dailyPendingSnip, ${js(fx.dailyDate)},
    $('daily-reader-body'));
  return 'ok';
})()`);

const hi = () => evalJs(`window.__hiOffset('#daily-reader-body')`);
const toast = () => evalJs(`window.__toastText()`);

await boot();

// ── 1. 图鉴抽屉:原处被改稿换掉,别处还剩一句一字不差的同句 ──────────────────
// 这一节是审查那条复现的形状:抽屉开着(版本号是旧的),盘上那一篇把选中那一处连
// 引子一起换掉、另一处同样的文字还在。**核对那一步的位置证据只能来自锚点的两侧**
// —— 两侧都对不上时,"引文本身在这儿只有一处"恰恰说明用户选的那一处的邻域没了。
await evalJs(`switchTab('gallery'); true`);
if (!await until(`typeof openToolDrawer === 'function'`, 15000))
  throw new Error("图鉴面板的脚本没加载起来");
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
/** 某一段文字在某处出现了几次(`expr` 里可以 await)。*/
const times = (expr, q) => evalJs(
  `(async () => String(${expr}).split(${js(q)}).length - 1)()`);
const onScreen = () => times(`$('td-entries').textContent`, fx.toolTwin);
const drawerReady = await until(
  `$('td-entries').textContent.split(${js(fx.toolTwin)}).length - 1 === 2`, 25000);
check("图鉴抽屉上这句话摆着两处(版本号就是这一刻这一份)", drawerReady,
  `屏幕上 ${await onScreen()} 处`);
// 盘上把**甲节那一处连它的引子一起**换掉 —— 屏幕上那两处不动,盘上只剩乙节那一处
check("沙箱把甲节那一处从盘上删掉了",
  (await sandbox("drop", fx.toolPath, fx.toolTwinLead + fx.toolTwin)) === 200);
// **先钉住"盘上照旧找得到这段话"** —— 不然下面那条拒绝可能只是因为盘上那一篇里
// 压根没有它了(那是 bind 第 4d 节那一格,两回事)。
const onDisk = await times(`await fetch('/api/doc?path='
  + encodeURIComponent(${js(fx.toolPath)})).then(r => r.json())
  .then(d => d.content || '')`, fx.toolTwin);
check("盘上还剩乙节那一处(所以下面那条拒绝不是因为这段话不在了)", onDisk === 1,
  `盘上 ${onDisk} 处`);
check("屏幕上那两处没跟着变(抽屉就是那一刻读到的那一份)",
  (await onScreen()) === 2, `屏幕上 ${await onScreen()} 处`);
await appToastClear();
const twinTake = await snip("#td-entries", fx.toolTwin, 1);
check("旧抽屉上甲节那一处照旧选得中、按钮照旧点得下", twinTake.shown,
  `picked=${twinTake.picked}`);
const twinToast = await toast();
// 后端那道 409 是靠「面板那一版 ≠ 盘上这一版」判出来的 —— 核对把版本号换成新那一
// 版的话这一句就永远说不出口,收藏照旧落库
check("如实说面板上这份正文旧了、让刷新再收藏(没把版本号偷换成新那一版)",
  twinToast.includes("刷新"), twinToast);
check("一行都没落库", (await rowsOf(fx.toolTwin)) === "[]", await rowsOf(fx.toolTwin));
// 反过来钉一边:**刷新之后剩下那一处照旧收得下**(拦住的是"旧页面 + 选的那一处
// 没了",不是"这一句一律收不下")
await boot();
await evalJs(`switchTab('gallery'); true`);
await until(`typeof openToolDrawer === 'function'`, 15000);
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
await until(`$('td-entries').textContent.includes(${js(fx.toolTwin)})`, 25000);
check("刷新之后屏幕上跟盘上一样只剩乙节那一处", (await onScreen()) === 1,
  `屏幕上 ${await onScreen()} 处`);
await appToastClear();
const twinAgain = await snip("#td-entries", fx.toolTwin, 1);
check("剩下那一处照旧收得下", twinAgain.shown, `picked=${twinAgain.picked}`);
const againRow = JSON.parse(await rowsOf(fx.toolTwin));
// 锚点从新那一份上采,两侧都对得上 —— 所以这一条收藏在那一头定得住(`e` 为 1)
check("而且绑的是那一篇、按新那一份算出了定得住",
  againRow.length === 1 && againRow[0].o === "doc" && againRow[0].p === fx.toolPath
  && againRow[0].u === 1 && againRow[0].e === 1
  && againRow[0].pre.endsWith("又说一遍:"), JSON.stringify(againRow));
await evalJs(`closeToolDrawer(); true`);

// ── 2. 采集那一刻整段锚点就定不住:这件事要跟着收藏落库 ──────────────────────
// 两节一字不差、连前后各两百多字也一字不差(锚点三档 30/80/200 都分不出两处)。
// 采集那一刻屏幕上就说了一句「可能定位不到这一处」—— 那句话要跟着落库,不然
// 原处被删掉之后剩下的那一处成了唯一命中,回跳静默高亮一处从没被收藏过的文字。
await evalJs(`switchTab('daily'); true`);
const dailyReady = await until(
  `dailyCurrent === ${js(fx.dailyDate)}
   && $('daily-reader-body').textContent.length > 100`, 40000);
check("日报面板把那一天的正文读出来了(随后才摆语料)", dailyReady,
  await evalJs(`$('daily-reader-body').textContent.slice(0, 60)`));
const PAD = "甲乙丙丁戊己庚辛壬癸".repeat(22);      // 220 字:比最长那一档还长
const DUP_Q = "这一句两处都有、连前后两百字也一字不差";
const SECTION = PAD + DUP_Q + PAD;
await stage(`<p>${SECTION}</p><p>${SECTION}</p>`);
await appToastClear();
const dupTake = await snip("#daily-reader-body", DUP_Q, 2);
check("两处一字不差的语料里,第二处照旧收得下", dupTake.shown,
  `picked=${dupTake.picked}`);
const dupToast = await toast();
check("收藏那一刻当场说了一句「可能定位不到这一处」",
  dupToast.includes("定位不到"), dupToast);
check("库里那条把「当时就定不住」记了下来(`anchor_exact` 是 0,不是空也不是 1)",
  (await rowsOf(DUP_Q)) === JSON.stringify([{ o: "daily", p: null, u: 0, e: 0,
    pre: PAD.slice(-30), suf: PAD.slice(0, 30) }]),
  await rowsOf(DUP_Q));
// 原处被整节删掉:整段锚点于是在剩下那一节上**唯一命中**
await stage(`<p>${SECTION}</p>`);
await appToastClear();
check("回跳走到了日报正文上", (await jump(DUP_Q)) === "ok");
const dupBack = await toast();
check("如实说没找到,不许拿剩下那一处顶上", dupBack.includes("没找到这段收藏"),
  dupBack);
check("一处也不许高亮", (await hi()) === -1, String(await hi()));
// **反过来钉一边:拦住它的正是落库那个事实,不是"这份文本里对不上"。** 同一条
// 锚点、同一份语料,只把 `e` 换成 1(模拟老收藏没记下这件事)就定得到 —— 少了
// 这一条,"一有歧义就全拒"也能过上面那三条,而那是把能定位的收藏也废掉。
const dupWhy = await evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  const f = d.favorites.find(x => x.quote === ${js(DUP_Q)});
  const spec = snipSpecOf(f);
  const idx = snipTextIndex($('daily-reader-body'));
  return JSON.stringify({ asSaved: !!snipSeek(idx.text, spec),
                          asLegacy: !!snipSeek(idx.text, { ...spec, e: 1 }) });
})()`);
check("整段锚点在这份文本里本来是唯一命中的(拦它的是落库那个事实)",
  dupWhy === JSON.stringify({ asSaved: false, asLegacy: true }), dupWhy);

// ── 3. 整份正文选中:两侧天生都是空的,而「语料整份就是这段引文」自己就是证明 ──
// 这一格是上面那套收紧的边界:**不许把"完整正文选区"一起判死**。日报正文一个字
// 没改、立刻点回去却说"没找到"是审查实测那一条 —— 采集那头算出"定得到"、回跳那头
// 拿不出证明,两头判据矛盾,而所有完整正文选区都长这个形状。
const WHOLE_Q = "这一份日报只有这一句完整的正文,整段收下来之后应当点得回来";
await stage(`<p>${WHOLE_Q}</p>`);
await appToastClear();
const wholeTake = await snip("#daily-reader-body", WHOLE_Q);
check("整份正文选中照旧收得下", wholeTake.shown, `picked=${wholeTake.picked}`);
const wholeToast = await toast();
check("而且没有多说一句「可能定位不到」(采集那头就认它定得到)",
  !wholeToast.includes("定位不到"), wholeToast);
check("库里那条两侧上下文都是空的、定得住那一位记的是 1",
  (await rowsOf(WHOLE_Q)) === JSON.stringify([{ o: "daily", p: null, u: 1, e: 1,
    pre: "", suf: "" }]),
  await rowsOf(WHOLE_Q));
await appToastClear();
check("回跳走到了日报正文上", (await jump(WHOLE_Q)) === "ok");
check("正文一个字没改,点回去就定得到", (await hi()) === 0, String(await hi()));
check("而且没有弹那句「没找到这段收藏」", !(await toast()).includes("没找到"),
  await toast());
// 反过来钉一边:**这一档不是「放开所有裸引文」**。语料比引文多了一段(日报重算过
// 一轮)时,两侧天生都没有的锚点一律不采信 —— 哪怕那段话照旧在里面。
await stage(`<p>${WHOLE_Q}</p><p>后来这一份日报又补了一段话,于是语料不再只有那一句。</p>`);
await appToastClear();
const stillThere = await evalJs(
  `$('daily-reader-body').textContent.includes(${js(WHOLE_Q)})`);
check("改过的那一份里这段话照旧在(所以下面那条拒绝不是因为它不在了)",
  stillThere === true, String(stillThere));
check("回跳走到了改过的那份正文上", (await jump(WHOLE_Q)) === "ok");
const grownBack = await toast();
check("语料比引文多了一段,就不再采信裸引文那一档", grownBack.includes("没找到"),
  grownBack);
check("这一下一处也不许高亮", (await hi()) === -1, String(await hi()));

// ── 3b. 「当时就定不住」这件事要经得起那一跳的传递 ──────────────────────────
// 第 2 格是拿生产函数直接问阶梯,这一格走**用户真点的那一下**:收藏卡上那颗按钮的
// 载荷(`fav.js` 的 `favJumpHtml`)→ `jumpFavOrigin` → 日报面板。库里记着、这条路
// 上漏掉的话,第 2 格那三条照旧是绿的,而他真点的那一下又去冒认别处了。
// 锚点借 bind 第 1 节那一组(两侧都还在、交集唯一,它那一头钉着落回第一处),
// **两条只差 `e` 这一个数**:0 → 一个字都不许高亮;1 → 照旧落回那一处。
await boot();                   // 上一格现摆过日报正文,这一格要的是盘上那一份
/** 把库里引文等于 q 的收藏全删掉。回跳按钮按「引文 + 后文」找,前面几份探针留下
 *  的同引文收藏不清掉就分不清点中的是哪一条。*/
const dropFavs = q => evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  for (const f of d.favorites.filter(x => x.quote === ${js(q)}))
    await fetch('/api/favorites/' + f.id, { method: 'DELETE' });
  return true;
})()`);
/** 手搭一条 origin=daily 的收藏:锚点由探针给,`e` 是这一格唯一的变量。*/
const putDaily = e => evalJs(
  `fetch('/api/favorites', { method: 'POST',
     headers: { 'Content-Type': 'application/json' },
     body: JSON.stringify({ quote: ${js(fx.disjQ)}, prefix: ${js(fx.disjP)},
       suffix: ${js(fx.disjSNear)}, origin: 'daily',
       origin_ref: ${js(fx.dailyDate)}, quote_unique: 1, anchor_exact: ${Number(e)},
       origin_title: '日报 · 定不定得住' }) }).then(r => r.status)`);
await dropFavs(fx.disjQ);
check("前面几份留下的同引文收藏清掉了", (await rowsOf(fx.disjQ)) === "[]",
  await rowsOf(fx.disjQ));
check("手搭的那条「当时就定不住」落库了", (await putDaily(0)) === 200);
check("收藏面板把它列出来了", await openFavList());
await appToastClear();
check("点得到它那颗回跳按钮", await clickJump("daily", fx.disjQ, fx.disjSNear));
const jumpedE0 = await until(
  `window.__toastText().includes('没找到这段收藏')`, 20000);
check("真点那一下也如实说没找到", jumpedE0, await evalJs(`window.__toastText()`));
check("真点那一下一个字都不许高亮", (await highlightText()) === "(没有高亮)",
  await highlightText());
// 反过来钉一边:同一组锚点、只把那个数换成 1,这一跳照旧落回那一处
await dropFavs(fx.disjQ);
check("换成「当时定得住」那条也落库了", (await putDaily(1)) === 200);
check("收藏面板把换过的那条列出来了", await openFavList());
await appToastClear();
check("点得到换过那条的回跳按钮", await clickJump("daily", fx.disjQ, fx.disjSNear));
const jumpedE1 = await snipHighlight(fx.disjQ);
check("只把那个数换成 1,这一跳就又落回那一处了", jumpedE1 === fx.disjQ, jumpedE1);

// ── 4. 同一件事,独立阅读页自己判一次 ───────────────────────────────────────
// `/doc/…` 自带一份内联的定位(`kb/web/pages.py` 的 `seek`,那一页脱离 kb-web 也
// 要能打开)—— **两份实现各要有自己的红**。这一格也走真路:面板给绑了文档的收藏
// 拼的是一条带 `#kbq=` 的阅读页链接(`fav.js` 的 `favDocAHtml`),探针点它。
// 三条只差一个数、别的变量全钉住:没记过这件事(老收藏)→ 照旧按引文落回那一处;
// `e=0` → 一个字都不许高亮;再换成 `e=1` → 又落回去。
// 这一头走得到裸引文那一档,是因为它**证明得出「这一篇就是采集时那一篇」**
// (链接里的 `v` 等于这一页此刻的 `DOC_VERSION`),所以拦住中间那条的只能是
// 采集那会儿记下的那个事实。
const GONE_S = "甲:这一句在正文里压根不存在";
/** 手搭一条绑那一篇导出稿的收藏(后文给一句正文里压根没有的话 —— 两侧对不上,
 *  于是只剩裸引文那一档),`extra` 是要加的那个数。*/
const putDoc = extra => evalJs(`(async () => {
  const d = await fetch('/api/doc?path=' + encodeURIComponent(${js(fx.docPath)}))
    .then(r => r.json());
  const r = await fetch('/api/favorites', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ quote: ${js(fx.onceSolo)}, prefix: '',
      suffix: ${js(GONE_S)}, origin: 'doc', path: ${js(fx.docPath)},
      doc_version: String(d.version || ''), quote_unique: 1,
      origin_title: '阅读页 · 定不定得住', ...${extra} }) });
  return r.status;
})()`);
/** 回到外壳 → 只留下这一条 → 点面板上它那条阅读页链接。回那条链接。*/
const viaPanel = async extra => {
  await boot();
  await dropFavs(fx.onceSolo);
  if ((await putDoc(extra)) !== 200) return "";
  if (!await openFavList()) return "";
  const href = await favDocHref(fx.onceSolo, GONE_S);
  if (!href) return "";
  await send("Page.navigate", { url: base + href });
  if (!await until(`!!document.getElementById('content')
    && document.getElementById('content').textContent.includes(${js(fx.onceSolo)})`,
    25000)) return "";
  await evalJs(DOC_HELPERS);
  return href;
};
/** 等高亮落到那一句上(定位是异步的),回它最后落在哪一段文字上。*/
const settle = async () => {
  let got = "";
  for (let i = 0; i < 120 && got !== fx.onceSolo; i++) {
    got = await highlightText();
    if (got !== fx.onceSolo) await sleep(150);
  }
  return got;
};
const hrefLegacy = await viaPanel("{}");
check("面板给没记过这件事的老收藏照旧拼出链接、且链接里不带那个数",
  !!hrefLegacy && !decodeURIComponent(hrefLegacy).includes('"e"'), hrefLegacy);
const legacyHi = await settle();
check("点它,阅读页照旧按引文落回那一处", legacyHi === fx.onceSolo, legacyHi);
const hrefE0 = await viaPanel("{ anchor_exact: 0 }");
check("面板把「当时就定不住」这件事写进了那条链接",
  decodeURIComponent(hrefE0).includes('"e":0'), hrefE0);
// 这一页是刚导航过来的,`#kbtoast` 本来就是空的 —— **不要先清一次**:那一句提示
// 在定位那一下就弹出来了,清掉之后再等它一辈子也不会再弹。
const docRefused = await until(
  `(document.getElementById('kbtoast').textContent || '').includes('没找到这段收藏')`,
  20000);
check("点它,阅读页如实说没找到", docRefused,
  await evalJs(`document.getElementById('kbtoast').textContent`));
check("而且一个字都不许高亮", (await highlightText()) === "(没有高亮)",
  await highlightText());
const hrefE1 = await viaPanel("{ anchor_exact: 1 }");
check("面板给「当时定得住」那条拼出的链接里记的是 1",
  decodeURIComponent(hrefE1).includes('"e":1'), hrefE1);
const exactHi = await settle();
check("只把那个数换成 1,阅读页就又落回那一处了(拦它的是那个数)",
  exactHi === fx.onceSolo, exactHi);
// **这一页自己采集的那一条也要把这件事写下来。** 阅读页有自己一份内联的采集
// (`_DOC_SNIP_JS`),落库时漏掉这个数的话库里记的是"不知道" —— 那一头按原样放行
// (老收藏不能一次性废掉),于是这一页收下来的收藏从此全不受这条约束管。
await dropFavs(fx.onceSolo);
const docTake = await docSnip(fx.onceSolo);
check("阅读页上那一句照旧收得下", docTake.shown, `picked=${docTake.picked}`);
const docRow = await rowsOf(fx.onceSolo);
check("而且它把「这条锚点定得住」一起写进了库里(不是空)",
  docRow === JSON.stringify([{ o: "doc", p: fx.docPath, u: 1, e: 1,
    pre: JSON.parse(docRow)[0] ? JSON.parse(docRow)[0].pre : "",
    suf: JSON.parse(docRow)[0] ? JSON.parse(docRow)[0].suf : "" }]), docRow);

finish();
