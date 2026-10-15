// 「☆ 收藏此段」**绑得对不对**:位置绑在哪一处、版本绑在哪一版。
//
// 这一份和前两份(`snip_probe.mjs` 四处正文收得下点得回去、`snip_probe_edges.mjs`
// 收下来之后还作不作数)管的是同一条链上最后两种"当场看着成功"的半成品:
//
//   1 **位置证据只听了一半。** 前文那一边说"只可能是第一处或第二处",后文那一边
//     唯一指向第三处 —— 把"有两处"和"没有"压成同一个值的话,前文那个集合就丢了,
//     于是高亮落在一处前文已经明确排除掉的地方(审查实测:日报三处偏移
//     809/912/1015,收藏的是 912、高亮落在 1015;独立阅读页 223/326/429,收藏的
//     是 326、高亮落在 429)。**两份定位实现各判一次**。
//   2 **核对和落库之间正文换了一版。** 核对读的是服务端那一篇、写入是另一个请求,
//     中间那一下重译写回/正文补抓照样会把那一篇换掉 —— 没有共同的版本号的话,
//     收藏照旧落成 origin=doc 返回 200,立刻回看是新正文、原句不在、0 个高亮
//     (审查实测)。**三个绑文档的入口各判一次**:订阅阅读器(有退路 → 退回
//     feed)、独立阅读页和图鉴抽屉(没有退路 → 如实拒,并说清怎么办)。
//
// 判据一律落在后果上:**库里那条收藏是什么**(有没有、origin 是哪一类、绑没绑
// path)、**高亮落在第几处**(不是"文字对不对" —— 三处的文字都对)、**屏幕上那
// 句提示说的是哪一种退法**。
//
// 「盘上那一篇换了一版」靠沙箱独有的两条路由摆成确定时序(`tests/serve_snippets.py`
// 的 `install_test_routes`):`touch` 真改文件,`swap-after-read` 把 touch 挪到
// 「那一次 `/api/doc` 的响应已经生成好、POST 还没发出」之间 —— 一个响应都不伪造。
//
// 外壳(起浏览器、连 CDP、页内帮手、独立阅读页那一头)共用 ./snip_probe_lib.mjs。
//
//   HEADLESS_SHELL=<bin> node tests/snip_probe_bind.mjs <baseURL> <manifest.json>
import {
  appToastClear, base, boot, check, clickJump, DOC_HELPERS, docSnip,
  docToastClear, evalJs, favDocHref, finish, fx, highlightText, js, openFavList,
  send, sleep, snip, until,
} from "./snip_probe_lib.mjs";

const docUrl = rel => base + "/doc/" + rel.split("/").map(encodeURIComponent).join("/");

/** 沙箱那几条路由(真改盘上那一篇 / 真删一句 / 把那一改挪到核对之后)。
 *  回 HTTP 状态。`text` 只有 `drop` 要。*/
const sandbox = (route, path, text = "") => evalJs(
  `fetch('/__snip__/' + ${js(route)} + '?path=' + encodeURIComponent(${js(path)})
     + (${js(text)} ? '&text=' + encodeURIComponent(${js(text)}) : ''),
     { method: 'POST' }).then(r => r.status)`);

/** 库里引文等于 q 的那一条的前后文(判「锚点是从哪儿采的」用)。*/
const favRowOf = q => evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  const f = d.favorites.find(x => x.quote === ${js(q)});
  return f ? JSON.stringify({ pre: f.prefix, suf: f.suffix }) : 'null';
})()`);

/** 手搭一条 origin=daily 的收藏(锚点由探针给,不经过采集那一步)。
 *  正文改稿之后锚点就是这个形状,而改稿本身不是这条判据要验的东西。*/
const putDaily = (q, p, s, title, u = 0) => evalJs(
  `fetch('/api/favorites', { method: 'POST',
     headers: { 'Content-Type': 'application/json' },
     body: JSON.stringify({ quote: ${js(q)}, prefix: ${js(p)}, suffix: ${js(s)},
       origin: 'daily', origin_ref: ${js(fx.dailyDate)}, quote_unique: ${Number(u)},
       origin_title: ${js(title)} }) }).then(r => r.status)`);

/** 同上,但绑那一篇导出稿(origin=doc + 真 path):面板给这一类拼的是一条带
 *  `#kbq=` 的阅读页链接,和 daily 那一类的回跳按钮是**两条不同的代码路径**。*/
const putDoc = (q, p, s, title, u = 0) => evalJs(`(async () => {
  // 绑文档的收藏必须带盘上此刻那一版(后端 400 挡着),现问一次
  const d = await fetch('/api/doc?path=' + encodeURIComponent(${js(fx.docPath)}))
    .then(r => r.json());
  const r = await fetch('/api/favorites', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ quote: ${js(q)}, prefix: ${js(p)}, suffix: ${js(s)},
      origin: 'doc', path: ${js(fx.docPath)}, doc_version: String(d.version || ''),
      quote_unique: ${Number(u)}, origin_title: ${js(title)} }) });
  return r.status;
})()`);

/** 库里引文等于 q、来源是 origin 的那一条记下的「采集那会儿有几处」。
 *  回 `1` / `0` / `null`(没记下)/ `'none'`(没有这条)。*/
const uniqOf = (q, origin) => evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  const f = d.favorites.find(x => x.quote === ${js(q)} && x.origin === ${js(origin)});
  return f ? JSON.stringify(f.quote_unique) : 'none';
})()`);

/** 库里引文等于 q 的那几条,整成一段 JSON 文本(只留判据要看的那两个字段)。
 *  **自带 fetch,不走外壳装在页面里那套帮手**:独立阅读页是另一个页面上下文,
 *  那套帮手在那儿压根不存在(上一版就撞在这儿 —— 第 3 节一上来就 Uncaught)。*/
const favsOf = q => evalJs(`(async () => {
  const d = await fetch('/api/favorites?status=inbox').then(r => r.json());
  return JSON.stringify(d.favorites.filter(f => f.quote === ${js(q)})
    .map(f => ({ o: f.origin, p: f.path || null })));   // 接口把 NULL 给成空串
})()`);

await boot();

// ── 1. 两侧候选不相交:不许只听还说得上话的那一边(app 那一侧) ──────────────
await evalJs(`switchTab('daily'); true`);
const dailyReady = await until(
  `dailyCurrent === ${js(fx.dailyDate)}
   && $('daily-reader-body').textContent.includes(${js(fx.disjQ)})`, 40000);
check("日报面板铺出了不相交那三处", dailyReady,
  await evalJs(`$('daily-reader-body').textContent.slice(0, 60)`));
// 同一个 q、同一个 p,两条只差后文:一条的后文指第三处(和前文的候选不相交),
// 一条的后文指第一处(落在前文的候选里)。**两条都得判对** —— 只拒不放行的话,
// "一有歧义就全拒"也能过上面那条,而那是把能定位的收藏也废掉。
await putDaily(fx.disjQ, fx.disjP, fx.disjSFar, "日报 · 两侧不相交");
await putDaily(fx.disjQ, fx.disjP, fx.disjSNear, "日报 · 两侧有交集");
await openFavList();
const clickedFar = await clickJump("daily", fx.disjQ, fx.disjSFar);
check("收藏面板里能点到不相交那条的回跳", clickedFar);
const farToast = await until(`window.__toastText().includes('没找到这段收藏')`, 20000);
check("两侧候选不相交时如实说没找到", farToast,
  await evalJs(`window.__toastText()`));
check("两侧候选不相交时一处也不许高亮",
  (await highlightText()) === "(没有高亮)", await highlightText());
// 反过来钉一边:**前文那一边照旧有两处候选**,可后文指的正是其中一处 —— 交集
// 唯一,这一条必须照旧落回第一处(偏移按压平坐标系比,文字三处都一样)
await openFavList();
const clickedNear = await clickJump("daily", fx.disjQ, fx.disjSNear);
check("收藏面板里能点到有交集那条的回跳", clickedNear);
const wantFirst = await evalJs(
  `window.__textOffset('#daily-reader-body', ${js(fx.disjQ)}, 1)`);
let gotNear = -1;
for (let i = 0; i < 120 && gotNear !== wantFirst; i++) {
  gotNear = await evalJs(`window.__hiOffset('#daily-reader-body')`);
  if (gotNear !== wantFirst) await sleep(150);
}
check("前文有两处候选、后文指着其中一处时照旧落回那一处",
  gotNear === wantFirst && wantFirst >= 0, `高亮偏移 ${gotNear},期望 ${wantFirst}`);

// ── 1b. 弱档:两侧都失配时,日报这一头一律不许采信 ───────────────────────────
// 两侧上下文都失配 = 这段话周围的正文已经不是采集时那样了。日报正文**采集和回跳
// 走的是同一个渲染器**,所以没有「两种渲染摊出来的文本不一样」这个借口 —— 一侧
// 对不上就只能是正文自己变过。那时「现在只剩一处」证明不了「这一处就是当时那一
// 处」:当时有两处的话,原处完全可能正是被改掉的那一处;**当时只有一处也一样**
// —— 原句被改稿搬走、别处新写了一句一模一样的,裸引文照样唯一命中(审查用生产
// 函数实测:收藏偏移 153,删掉原处之后回查到 45)。
// 所以这一头**两条都该是「没找到」,`u` 不是放行条件**。能证明「要搜的这一份就是
// 采集时那一份」的只有两种,都不在这一头:绑了文档且版本号相等(第 2b/2d/7 节)、
// 问答限定到那一条消息(`snip_probe_chat.mjs`)。
// 后文给的是**正文里压根没有的一句话**,前文留空:这是「两侧都用不上」的确定形状,
// 不用去改盘上那个文件(改文件那条路另有判据,见第 3、7 节)。
const GONE_S1 = "甲:这一句在正文里压根不存在";
const GONE_S2 = "乙:这一句在正文里也压根不存在";
await putDaily(fx.onceSolo, "", GONE_S1, "日报 · 采集时唯一", 1);
await putDaily(fx.onceSolo, "", GONE_S2, "日报 · 采集时有两处", 0);
await openFavList();
// **先把上一节留下的提示和高亮清掉。** 上一节那条收藏是定位得到的,它的高亮还亮
// 着;而「没找到」那句提示在第 1 节也出现过一次 —— 不清的话下面两条判据读到的
// 是上一节的结果,这一节等于什么都没验(实测:1b 两条在未清状态下一条假绿一条
// 假红)。
await appToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); true`);
check("收藏面板里能点到「采集时唯一」那条", await clickJump("daily", fx.onceSolo, GONE_S1));
const soloToast = await until(`window.__toastText().includes('没找到这段收藏')`, 20000);
check("采集那会儿只有一处的,在日报这一头也如实说没找到", soloToast,
  await evalJs(`window.__toastText()`));
check("那一条一处也不许高亮", (await highlightText()) === "(没有高亮)",
  await highlightText());
await appToastClear();
await openFavList();
check("收藏面板里能点到「采集时有两处」那条", await clickJump("daily", fx.onceSolo, GONE_S2));
const onceToast = await until(`window.__toastText().includes('没找到这段收藏')`, 20000);
check("采集那会儿有两处的,两侧都失配之后如实说没找到", onceToast,
  await evalJs(`window.__toastText()`));
check("并且一处也不许高亮", (await highlightText()) === "(没有高亮)",
  await highlightText());
// ── 1c. 一侧有证据、却在这份文本里一处也对不上:另一侧只剩一处也不许采信 ─────
// 上面 1b 那两条是**两侧都用不上**。这一条是中间那一格,也是最容易蒙过去的一格:
// 前文那一边有证据(锚点里真记着一句话),可这份文本里一处也对不上 —— 那就说明
// **这份语料已经不是采集时那一份了**(那句话被改稿删掉或改写了)。这时候"后文那
// 边只剩一处"证明不了那一处就是当时那一处:同样的话正文里有两处,后文指着的是
// **第二处**,而前文描述的那一处已经不在了。采信的后果是屏幕上亮起一句用户压根
// 没收过的话 —— 比一句「没找到」坏得多(高亮本身在撒谎)。
// **一侧的三种状态不许压成两种**:`null`(天生没有这一侧)照旧听另一侧,
// `[]`(有这一侧、一处也对不上)才是这一格。下面紧跟着的那一条就钉 `null` 那一格。
const GONE_P = "丙:这一句原来在它前面,后来被改稿删掉了";
await appToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); true`);
await putDaily(fx.onceQ, GONE_P, fx.onceTail2, "日报 · 前文一处也对不上", 0);
await openFavList();
check("收藏面板里能点到「前文一处也对不上」那条",
  await clickJump("daily", fx.onceQ, fx.onceTail2));
const lostPToast = await until(`window.__toastText().includes('没找到这段收藏')`, 20000);
check("前文有证据却一处也对不上时,如实说没找到", lostPToast,
  await evalJs(`window.__toastText()`));
const secondAt = await evalJs(
  `window.__textOffset('#daily-reader-body', ${js(fx.onceQ)}, 2)`);
await sleep(600);
const lostPHi = await evalJs(`window.__hiOffset('#daily-reader-body')`);
check("而且一处也不许高亮(采信后文的话,亮的会是第二处 —— 用户没收过那一句)",
  lostPHi === -1, `高亮偏移 ${lostPHi},第二处在 ${secondAt}`);
// 反过来钉一边:**天生没有前文**的锚点(引文顶在正文开头、md 和渲染两套文本差了
// 一个标题那类)照旧只听后文 —— 这一档不许跟着一起拒,不然定得到的收藏也废了
await appToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); true`);
await putDaily(fx.onceQ, "", fx.onceTail1, "日报 · 天生没有前文", 0);
await openFavList();
check("收藏面板里能点到「天生没有前文」那条",
  await clickJump("daily", fx.onceQ, fx.onceTail1));
const firstAt = await evalJs(
  `window.__textOffset('#daily-reader-body', ${js(fx.onceQ)}, 1)`);
let lostNullHi = -1;
for (let i = 0; i < 120 && lostNullHi !== firstAt; i++) {
  lostNullHi = await evalJs(`window.__hiOffset('#daily-reader-body')`);
  if (lostNullHi !== firstAt) await sleep(150);
}
check("天生没有前文的那一条照旧按后文落回它指的那一处",
  lostNullHi === firstAt && firstAt >= 0,
  `高亮偏移 ${lostNullHi},期望 ${firstAt}(第一处)`);

// ── 1d. 裸锚点(两侧天生都没有):日报这一头照旧不许采信 ─────────────────────
// 上面 1c 那两条管的是「一侧丢了」。两侧**天生**都没有的锚点(整条气泡/整段正文
// 就是引文、手搭的裸锚点)压根走不到那道带子,直接落到裸引文那一档 —— 所以那一档
// 自己也要看「证明得出这份语料就是采集时那一份」没有。日报永远证明不出来,于是
// 「采集那会儿只有一处」+「现在也只有一处」在这一头照旧不是证据。
// 引文挑的是正文里另一句没人收过的话(`onceTail1`),这样这一条收藏在面板上认得出。
await appToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); true`);
await putDaily(fx.onceTail1, "", "", "日报 · 两侧天生都没有", 1);
await openFavList();
check("收藏面板里能点到那条裸锚点", await clickJump("daily", fx.onceTail1));
const bareToast = await until(`window.__toastText().includes('没找到这段收藏')`, 20000);
check("裸锚点在日报这一头如实说没找到", bareToast,
  await evalJs(`window.__toastText()`));
await sleep(600);
const bareHi = await evalJs(`window.__hiOffset('#daily-reader-body')`);
check("裸锚点那一条一处也不许高亮(这句话正文里确实只有一处,可那不是证据)",
  bareHi === -1, `高亮偏移 ${bareHi}`);

// 采集那一头也要钉住:**这个数是真从正文里数出来的**,不是存了个常量
const dupTake = await snip("#daily-reader-body", fx.onceQ, 2);
check("日报里那句重复两次的话收得下", dupTake.shown, `picked=${dupTake.picked}`);
check("库里那条记的是「采集那会儿不只一处」",
  (await uniqOf(fx.onceQ, "daily")) === "0", await uniqOf(fx.onceQ, "daily"));

// ── 2. 同一件事,独立阅读页自己判一次 ───────────────────────────────────────
// `/doc/…` 自带一份内联的定位实现(`kb/web/pages.py` 的 `_DOC_SNIP_JS`,那一页
// 脱离 kb-web 也要能用)—— **两份实现各要有自己的红**。
await send("Page.navigate", { url: docUrl(fx.docPath) });
await until(`!!document.getElementById('content')
  && document.getElementById('content').textContent.includes(${js(fx.disjQ)})`, 25000);
await evalJs(DOC_HELPERS);
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.disjQ)}, p: ${js(fx.disjP)},
    s: ${js(fx.disjSFar)} })); true`);
const docFar = await until(
  `(document.getElementById('kbtoast').textContent || '').includes('没找到这段收藏')`,
  20000);
check("阅读页上两侧候选不相交时一处也不许高亮",
  (await highlightText()) === "(没有高亮)", await highlightText());
check("阅读页上两侧候选不相交时也如实说没找到", docFar,
  await evalJs(`document.getElementById('kbtoast').textContent`));
// 反过来钉一边(同第 1 节末尾):交集唯一的那一条照旧落回第一处
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.disjQ)}, p: ${js(fx.disjP)},
    s: ${js(fx.disjSNear)} })); true`);
let docNth = -1;
for (let i = 0; i < 120 && docNth !== 1; i++) {
  docNth = await evalJs(`window.__docHiNth(${js(fx.disjQ)})`);
  if (docNth !== 1) await sleep(150);
}
check("阅读页上交集唯一的那一条照旧落回第一处", docNth === 1,
  `高亮落在第 ${docNth} 处`);

// ── 2b. 裸引文那一档,独立阅读页自己判一次 ───────────────────────────────────
// 这一页自带一份内联的定位和采集(`kb/web/pages.py` 的 `seek`/`contextOf`,脱离
// kb-web 也要能用)—— **两份实现各要有自己的红**。
// 这一头能走到裸引文那一档,是因为它**证明得出「这一篇就是采集时那一篇」**:
// 锚点里的 `v` 是收藏那一刻核对过的正文版本,和这一页此刻的 `DOC_VERSION` 相等。
// 三条只差一个数,把别的变量全钉住:`u=1 + 版本相等` → 落回那一处;
// `u=0 + 版本相等` → 没找到;**`u=1 + 版本不等` → 也没找到**(那一篇改过了,
// 「现在只剩一处」就不再是证据 —— 同第 1b 节那条实测)。
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.onceSolo)}, p: '',
    s: ${js(GONE_S1)}, u: 1, v: DOC_VERSION })); true`);
let docSoloHi = "";
for (let i = 0; i < 120 && docSoloHi !== fx.onceSolo; i++) {
  docSoloHi = await highlightText();
  if (docSoloHi !== fx.onceSolo) await sleep(150);
}
check("阅读页上采集时唯一的那条,两侧失配之后照旧按引文高亮得出来",
  docSoloHi === fx.onceSolo, docSoloHi);
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.onceSolo)}, p: '',
    s: ${js(GONE_S2)}, u: 0, v: DOC_VERSION })); true`);
const docOnce = await until(
  `(document.getElementById('kbtoast').textContent || '').includes('没找到这段收藏')`,
  20000);
check("阅读页上采集时有两处的那条,两侧失配之后如实说没找到", docOnce,
  await evalJs(`document.getElementById('kbtoast').textContent`));
check("阅读页上那一条也一处都不许高亮", (await highlightText()) === "(没有高亮)",
  await highlightText());
// 第三条:`u=1`,可锚点记的是另一版正文 —— 一个字都不许高亮
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.onceSolo)}, p: '',
    s: ${js(GONE_S1)}, u: 1, v: 'v-这不是这一篇此刻的版本' })); true`);
const docStaleV = await until(
  `(document.getElementById('kbtoast').textContent || '').includes('没找到这段收藏')`,
  20000);
check("采集时唯一、但锚点记的是另一版正文 → 阅读页如实说没找到", docStaleV,
  await evalJs(`document.getElementById('kbtoast').textContent`));
check("版本不等那一条也一处都不许高亮", (await highlightText()) === "(没有高亮)",
  await highlightText());
// 第四条:**两侧都天生没有**的那种锚点(手搭的裸锚点、以及引文就是整份正文的
// 那一类)—— 它直接落到裸引文那一档,上面那道「一侧失配」的带子拦不到它,所以
// 这一档自己也要看版本。`snip_probe.mjs` 第 8 节钉的是它版本相等时**定得到**。
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.onceSolo)}, p: '', s: '',
    u: 1, v: 'v-这也不是这一篇此刻的版本' })); true`);
const bareStaleV = await until(
  `(document.getElementById('kbtoast').textContent || '').includes('没找到这段收藏')`,
  20000);
check("两侧都没有的裸锚点,版本不等时也如实说没找到", bareStaleV,
  await evalJs(`document.getElementById('kbtoast').textContent`));
check("它也一处都不许高亮", (await highlightText()) === "(没有高亮)",
  await highlightText());
// ── 2c. 「一侧有证据却一处也对不上」,独立阅读页自己判一次 ────────────────────
// 同第 1c 节那一格(两份定位实现各要有自己的红)。这一头的 `same` 是现算的
// (`!!spec.v && spec.v === DOC_VERSION`),这两条都不带 `v` → 证明不了,所以
// 前文一处也对不上时后文那一处不许采信。
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.onceQ)}, p: ${js(GONE_P)},
    s: ${js(fx.onceTail2)}, u: 0 })); true`);
const docLostP = await until(
  `(document.getElementById('kbtoast').textContent || '').includes('没找到这段收藏')`,
  20000);
check("阅读页上前文一处也对不上时,也如实说没找到", docLostP,
  await evalJs(`document.getElementById('kbtoast').textContent`));
check("阅读页上那一条也一处都不许高亮(采信的话亮的是第二处)",
  (await highlightText()) === "(没有高亮)",
  `${await highlightText()} / 第 ${await evalJs(`window.__docHiNth(${js(fx.onceQ)})`)} 处`);
// 反过来钉一边:天生没有前文的那一条,在这一页上也照旧只听后文
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); location.hash = '#kbq=' +
  encodeURIComponent(JSON.stringify({ q: ${js(fx.onceQ)}, p: '',
    s: ${js(fx.onceTail2)}, u: 0 })); true`);
let docNullNth = -1;
for (let i = 0; i < 120 && docNullNth !== 2; i++) {
  docNullNth = await evalJs(`window.__docHiNth(${js(fx.onceQ)})`);
  if (docNullNth !== 2) await sleep(150);
}
check("阅读页上天生没有前文的那一条照旧按后文落回第二处", docNullNth === 2,
  `高亮落在第 ${docNullNth} 处`);

// 这一页的**采集**也得真数一遍(它和 app 那头各算一次)
const docDup = await docSnip(fx.onceQ);
check("阅读页上那句重复两次的话收得下", docDup.shown && docDup.toast.includes("已收藏"),
  `picked=${docDup.picked} toast=${docDup.toast}`);
check("阅读页收下的那条记的是「采集那会儿不只一处」",
  (await uniqOf(fx.onceQ, "doc")) === "0", await uniqOf(fx.onceQ, "doc"));
const docSolo = await docSnip(fx.onceSolo);
check("阅读页上那句整篇只有一处的话也收得下",
  docSolo.shown && docSolo.toast.includes("已收藏"),
  `picked=${docSolo.picked} toast=${docSolo.toast}`);
check("阅读页收下的那条记的是「采集那会儿只有一处」",
  (await uniqOf(fx.onceSolo, "doc")) === "1", await uniqOf(fx.onceSolo, "doc"));

// ── 2d. 那个数还得真走到阅读页去:链接由收藏面板现拼 ─────────────────────────
// 上面两节把锚点直接写进地址栏,验的是**阅读页收到 `u` 之后怎么判**;可日常那条
// 路上锚点是**收藏面板拼出来的**(`fav.js` 的 `favDocAHtml`)—— 它少带一个 `u`,
// 阅读页那一头就只知道"不知道",裸引文那一档一律不采信,于是**采集那会儿确实
// 只有一处的收藏也点不回去了**(屏幕上是一句「没找到这段收藏」,而库里那条好好
// 记着 1)。和 daily 那一类的回跳按钮(`favJumpHtml` 的 data-jump)是两条代码
// 路径,**各要有自己的红**。同第 1b 节:两条只差 `u` 一个数。
const putSolo = await putDoc(fx.onceSolo, "", GONE_S1, "一篇 · 采集时唯一", 1);
const putDup = await putDoc(fx.onceSolo, "", GONE_S2, "一篇 · 采集时有两处", 0);
check("这两条绑文档的收藏手搭进去了",
  Number(putSolo) === 200 && Number(putDup) === 200,
  `solo=${putSolo} dup=${putDup}`);
await boot();
await openFavList();
const hrefSolo = await favDocHref(fx.onceSolo, GONE_S1);
const hrefDup = await favDocHref(fx.onceSolo, GONE_S2);
check("面板给这两条各拼出了一条阅读页链接", !!hrefSolo && !!hrefDup,
  `solo=${hrefSolo} dup=${hrefDup}`);
await send("Page.navigate", { url: base + hrefSolo });
await until(`!!document.getElementById('content')
  && document.getElementById('content').textContent.includes(${js(fx.onceSolo)})`, 25000);
await evalJs(DOC_HELPERS);
let panelSolo = "";
for (let i = 0; i < 120 && panelSolo !== fx.onceSolo; i++) {
  panelSolo = await highlightText();
  if (panelSolo !== fx.onceSolo) await sleep(150);
}
check("点面板上「采集时唯一」那条,阅读页按引文落回那一处", panelSolo === fx.onceSolo,
  panelSolo);
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); true`);
await send("Page.navigate", { url: base + hrefDup });
const panelDup = await until(
  `(document.getElementById('kbtoast').textContent || '').includes('没找到这段收藏')`,
  20000);
check("点面板上「采集时有两处」那条,阅读页如实说没找到", panelDup,
  await evalJs(`document.getElementById('kbtoast').textContent`));
check("那一条也一处都不许高亮", (await highlightText()) === "(没有高亮)",
  await highlightText());

// ── 3. 独立阅读页:页面上这份正文已经旧了,不许把收藏绑上去 ──────────────────
// 这一页把「我摊的是哪一版正文」烤在 HTML 里(`DOC_VERSION`),所以它天然就是
// 「手里拿着旧版本」那个局面 —— 不用任何时序栅栏。先钉另一边:**新开的页面照旧
// 收得下**(不然"一律拒"也能过下面那条)。
await send("Page.navigate", { url: docUrl(fx.toolPath) });
await until(`!!document.getElementById('content')
  && document.getElementById('content').textContent.includes(${js(fx.toolVFresh)})`,
  25000);
const freshDoc = await docSnip(fx.toolVFresh);
check("新开的阅读页照旧收得下", freshDoc.shown && freshDoc.toast.includes("已收藏"),
  `picked=${freshDoc.picked} toast=${freshDoc.toast}`);
check("它真绑上了那一篇",
  (await favsOf(fx.toolVFresh)) === JSON.stringify([{ o: "doc", p: fx.toolPath }]),
  await favsOf(fx.toolVFresh));
// 现在真把盘上那一篇换一版(页面不刷新,手里那个 DOC_VERSION 就旧了)
check("沙箱真改掉了盘上那一篇", (await sandbox("touch", fx.toolPath)) === 200);
const staleDoc = await docSnip(fx.toolVStale);
check("正文换版之后阅读页如实拒掉,并说清刷新再收",
  staleDoc.shown && staleDoc.toast.includes("刷新"),
  `picked=${staleDoc.picked} toast=${staleDoc.toast}`);
check("被拒的那一条一行都没落库", (await favsOf(fx.toolVStale)) === "[]",
  await favsOf(fx.toolVStale));

// ── 4. 图鉴抽屉:版本号来自条目接口,**而能不能回看要按阅读页那一头的文本算** ──
// 抽屉可以开着好几分钟,正文在这期间被换掉不会通知这一页;它也**没有退路**
// (回跳只有阅读页这一条)。所以这一节分四件事各钉一遍:
//   4a 同一个文件版本,两头的文本却不是同一份(正文里带原始 HTML)→ 照存、
//      但当场说清"点回去可能定位不到" —— 这是审查实测那一条:抽屉里选到的整句
//      在阅读页那一头中间插着四个尖括号字,核对本来就该判定位不到。
//   4b 可收的范围和采锚点的范围**都只在正文上**:摘要行(账号 / 日期 / 文章标题)
//      选中不许弹按钮,抽屉里那三颗链接按钮和小节标题不许进锚点。
//   4c 盘上那一篇**补了一段、原句还在** → 照存、绑新那一版(版本号不等不等于
//      回看不了)。
//   4d 盘上那一篇把那句话**删掉了** → 这一下才真的回看不了:如实拒、0 行。
await boot();
await evalJs(`switchTab('gallery'); true`);
if (!await until(`typeof openToolDrawer === 'function'`, 15000))
  throw new Error("图鉴面板的脚本没加载起来");
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
const drawerReady = await until(
  `$('td-entries').textContent.includes(${js(fx.toolVStale)})`, 25000);
check("图鉴抽屉铺出了那一条记录的正文", drawerReady,
  await evalJs(`$('td-entries').textContent.slice(0, 60)`));
check("抽屉里那一条记录带着它的正文版本号",
  await evalJs(`!!document.querySelector('.td-entry').dataset.snipVersion`),
  await evalJs(`JSON.stringify(document.querySelector('.td-entry').dataset)`));
// 4a 原始 HTML:屏幕上是连着的一句,阅读页那一头不是
check("抽屉里那一句显示成了粗体(标签本身看不见)",
  await evalJs(`!!document.querySelector('.td-entry .body strong')
    && $('td-entries').textContent.includes(${js(fx.toolHtmlSeen)})`),
  await evalJs(`$('td-entries').textContent.slice(0, 200)`));
await appToastClear();
// 这一句落在三个 text node 上(<strong> 把它切成了三段),所以用跨元素那一档选法
const htmlTool = await snip("#td-entries", fx.toolHtmlSeen, 1, "span");
check("跨过原始 HTML 的那一句照旧收得下", htmlTool.shown, `picked=${htmlTool.picked}`);
const htmlToast = await evalJs(`window.__toastText()`);
check("并且当场说清了点回去可能定位不到这一处",
  htmlToast.includes("定位不到"), htmlToast);
check("它绑的还是那一篇(文件是对的,只是两头的文本不一样)",
  (await favsOf(fx.toolHtmlSeen)) === JSON.stringify([{ o: "doc", p: fx.toolPath }]),
  await favsOf(fx.toolHtmlSeen));
// 反过来钉一边:核对那一步**不许动全局那套渲染**。核对要按阅读页的规则算
// (原始 HTML 当字面文字),而 `marked.use()` 是全局的 —— 拿它换规则的话,图鉴、
// 日报、问答、日报素材账本就都跟着变了(那几处要的正是"标签当标签")。核过一次
// 之后原地重渲染这一条,那一句照旧得是粗体。
await evalJs(`renderToolEntries(toolMeta); true`);
check("核对过之后再渲染一次,那一句照旧是粗体(没把全局那套渲染改掉)",
  await evalJs(`!!document.querySelector('.td-entry .body strong')`),
  await evalJs(`document.querySelector('.td-entry .body').innerHTML.slice(0, 200)`));

// 4b 可收的范围、采锚点的范围,都只在正文上
await appToastClear();
// 反过来钉一边:这条记录的 `<details>` 里**正文之外还住着别的字** —— 下面那三
// 颗链接按钮(「原文 / Obsidian / 在阅读器打开」)。它们在目标文档里压根不存在,
// 选中不许弹按钮,存下来就是一条点回去必定落空的收藏。
// **判据落在那三颗按钮上,不落在摘要行上**:摘要行(账号 / 日期 / 文章标题)住在
// `<summary>` 里,那一段文字连浏览器都不交出来(程序设好 Range 之后
// `getSelection().toString()` 是空串,于是字数那道判据先把按钮按下去了),
// 拿它当判据的话,去掉收窄那一句照旧全绿 —— 验的是浏览器而不是我们这段代码。
const linkTool = await snip("#td-entries", "在阅读器打开");
check("抽屉里那颗链接按钮的文案确实选中了(不然下面那条判据说不清)",
  String(linkTool.picked).startsWith("ok"), `picked=${linkTool.picked}`);
check("正文之外那几颗按钮的文案选中不弹按钮", !linkTool.shown,
  `picked=${linkTool.picked}`);
check("它们一行都没落库", (await favsOf("在阅读器打开")) === "[]",
  await favsOf("在阅读器打开"));
const tailTool = await snip("#td-entries", fx.toolTail);
check("正文最后一句照旧收得下", tailTool.shown, `picked=${tailTool.picked}`);
const tailRow = await favRowOf(fx.toolTail);
check("库里有这一条(先钉住,不然下面那条判据没有行可看、永远成立)",
  tailRow !== "null", tailRow);
// 正则里**那三颗按钮的文案要一个不少**:后文只取三十来个字,漏了打头那一颗
// (「原文」)的话,窗口压根够不到后面两颗 —— 这条判据就跟采集范围无关了
// (实测注入"采集范围不收窄"之后 suffix 是 `。 …(第 1 次) 原文`,照旧全绿)。
check("前后文里没有抽屉那几颗按钮的文案",
  !/原文|Obsidian|在阅读器打开|就这个工具提问/.test(tailRow), tailRow);
// 4c 补了一段(原句还在):该放行
check("沙箱给盘上那一篇补了一段", (await sandbox("touch", fx.toolPath)) === 200);
await appToastClear();
const keptTool = await snip("#td-entries", fx.toolVKept);
check("补过一段之后那一句照旧收得下", keptTool.shown, `picked=${keptTool.picked}`);
const keptToast = await evalJs(`window.__toastText()`);
check("没有拿「版本号不等」当理由拒掉它", !keptToast.includes("刷新"), keptToast);
check("它绑的是那一篇",
  (await favsOf(fx.toolVKept)) === JSON.stringify([{ o: "doc", p: fx.toolPath }]),
  await favsOf(fx.toolVKept));
// 4d 那句话被改稿删掉了:这才是真的回看不了
check("沙箱把那一句从盘上那一篇里删掉了",
  (await sandbox("drop", fx.toolPath, fx.toolVStale)) === 200);
await appToastClear();
const staleTool = await snip("#td-entries", fx.toolVStale);
check("改稿把那句话删掉之后,图鉴这一条如实拒掉", staleTool.shown,
  `picked=${staleTool.picked}`);
const toolToast = await evalJs(`window.__toastText()`);
check("并且说清了刷新再收藏", toolToast.includes("刷新"), toolToast);
check("图鉴那一条也一行都没落库", (await favsOf(fx.toolVStale)) === "[]",
  await favsOf(fx.toolVStale));
// 反过来钉一边:**刷新之后又收得下**(版本号是真的在跟着那一篇走,不是"绑了
// 文档就一律拒")。**刷整页而不是关掉重开抽屉** —— 同一个工具重开直接用现成
// 视图(`openToolDrawer` 的 `!changed && toolMeta` 那一句),版本号跟着那份视图
// 一起还是旧的。提示里那句话也只好说"刷新"(说"重新打开这条记录"就不灵了)。
await boot();
await evalJs(`switchTab('gallery'); true`);
await until(`typeof openToolDrawer === 'function'`, 15000);
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
await until(`$('td-entries').textContent.includes(${js(fx.toolVAgain)})`, 25000);
const againTool = await snip("#td-entries", fx.toolVAgain);
check("刷新之后照旧收得下", againTool.shown, `picked=${againTool.picked}`);
check("它绑的还是那一篇",
  (await favsOf(fx.toolVAgain)) === JSON.stringify([{ o: "doc", p: fx.toolPath }]),
  await favsOf(fx.toolVAgain));
await evalJs(`closeToolDrawer(); true`);

// ── 5. 订阅阅读器:它是三个入口里唯一**有退路**的那个 ───────────────────────
// 核对那一次 `/api/doc` 读到的是服务端那一篇,而写入是另一个请求 —— 下面两节各
// 摆一种"中间出了事":读不出来(5b)、正文换了一版(6)。两种都该退回站外原文,
// 并**如实说退的是哪一种**。
await evalJs(`switchTab('subs'); true`);
if (!await until(`!!document.getElementById('subs-switch-rss')`, 25000))
  throw new Error("订阅面板没列出 rss 那一档");
await evalJs(`document.getElementById('subs-switch-rss').click(); true`);
await until(`typeof sourceRows !== 'undefined'
  && sourceRows.some(r => r.key === ${js(fx.itemKey)})`, 30000);
await evalJs(`readSourceItem(${js(fx.itemKey)}); true`);
const raceReady = await until(
  `$('source-reader-body').textContent.includes(${js(fx.snipRace)})`, 20000);
check("订阅阅读器摊开了竞态那一句", raceReady,
  await evalJs(`$('source-reader-body').textContent.slice(0, 60)`));
// ── 5b. 「这一刻读不出来」不等于「这一篇不在索引里」 ─────────────────────────
// 两种都退回站外原文,可**说给用户的话不一样**:后者是个已经证明了的结论(等索引
// 跑完就好了),前者只是一次失败。把前者说成后者,用户据此做的判断就是错的。
check("沙箱把盘上那一篇变成读不出来的",
  (await sandbox("unreadable", fx.docPath)) === 200);
const unread = await snip("#source-reader-body", fx.snipUnreadable);
check("读不出盘上那一篇时照旧收得下", unread.shown, `picked=${unread.picked}`);
check("它退成了 origin=feed",
  (await favsOf(fx.snipUnreadable)) === JSON.stringify([{ o: "feed", p: null }]),
  await favsOf(fx.snipUnreadable));
const unreadToast = await evalJs(`window.__toastText()`);
check("说的是「读不出来」,不是「还没进检索索引」",
  unreadToast.includes("读不出") && !unreadToast.includes("检索索引"), unreadToast);
check("沙箱把那一篇换回来了", (await sandbox("unreadable", fx.docPath)) === 200);

// ── 6. 订阅阅读器:核对完到落库这一下,盘上那一篇被换了一版 ──────────────────
check("沙箱把「换一版」挂在了那一次核对之后",
  (await sandbox("swap-after-read", fx.docPath)) === 200);
const race = await snip("#source-reader-body", fx.snipRace);
check("竞态那一句照旧收得下(不是一句失败)", race.shown, `picked=${race.picked}`);
check("它退成了 origin=feed、没绑那一篇",
  (await favsOf(fx.snipRace)) === JSON.stringify([{ o: "feed", p: null }]),
  await favsOf(fx.snipRace));
const raceToast = await evalJs(`window.__toastText()`);
check("并且如实说了是「刚换了一版正文」才退的",
  raceToast.includes("换了一版"), raceToast);

// ── 7. 整条链上的版本门闸:收藏落库 → 面板拼链接 → 阅读页定位 ────────────────
// 第 2b 节把版本号直接写进地址栏,验的是**阅读页收到 `v` 之后怎么判**;这一节走
// 日常那条路:版本号由后端**随收藏落库**(`favorites.doc_version`)、面板从库里
// 读出来拼进 `#kbq=`(`fav.js` 的 `favDocAHtml`)、阅读页再和此刻这一篇比。
// **中间少一环,后果都是同一种静默**:落库那一步丢了它,面板拼不出 `v`,于是
// 所有绑文档的收藏在改稿之后都退成「说不清」(漏报,顶多一句如实的没找到);
// 反过来阅读页忘了比,改稿之后那一处同句照旧被当成原处高亮(误报,静默跳错地方)。
// 两条判据只差一件事:**中间有没有人真改过盘上那一篇**。
const GONE_S3 = "丙:这一句在正文里同样压根不存在";
await boot();
const putV = await putDoc(fx.onceSolo, "", GONE_S3, "一篇 · 版本门闸", 1);
check("这条绑文档的收藏手搭进去了(带着盘上此刻那一版)", Number(putV) === 200, putV);
await openFavList();
const hrefV = await favDocHref(fx.onceSolo, GONE_S3);
check("面板给它拼出了带版本号的阅读页链接",
  !!hrefV && decodeURIComponent(hrefV).includes('"v"'), hrefV);
await send("Page.navigate", { url: base + hrefV });
await until(`!!document.getElementById('content')
  && document.getElementById('content').textContent.includes(${js(fx.onceSolo)})`, 25000);
await evalJs(DOC_HELPERS);
let hiV = "";
for (let i = 0; i < 120 && hiV !== fx.onceSolo; i++) {
  hiV = await highlightText();
  if (hiV !== fx.onceSolo) await sleep(150);
}
check("没人动过那一篇时,这条收藏照旧按引文落回那一处", hiV === fx.onceSolo, hiV);
// 现在真改盘上那一篇(末尾续一段,那句话本身还在)
check("沙箱真改掉了盘上那一篇", (await sandbox("touch", fx.docPath)) === 200);
await docToastClear();
await evalJs(`CSS.highlights.delete('kb-quote'); true`);
// **重载,不是再 navigate 一次同一个地址** —— 后者连 hash 都没变,浏览器按同文档
// 导航处理,页面不重载、`locateQuote` 也不再跑一遍,于是这一节什么都没验(实测:
// 既没有提示也没有高亮,两条判据一条假红一条假绿)。重载之后这一页烤进来的
// `DOC_VERSION` 才是改完那一版。
await send("Page.reload", { ignoreCache: true });
await until(`!!document.getElementById('content')
  && document.getElementById('content').textContent.includes(${js(fx.onceSolo)})`, 25000);
await evalJs(DOC_HELPERS);
const staleV = await until(
  `(document.getElementById('kbtoast').textContent || '').includes('没找到这段收藏')`,
  25000);
check("那一篇被改过之后,同一条链接如实说没找到", staleV,
  await evalJs(`document.getElementById('kbtoast').textContent`));
check("并且一处也不许高亮(那段话还在,位置说不清)",
  (await highlightText()) === "(没有高亮)", await highlightText());

finish();
