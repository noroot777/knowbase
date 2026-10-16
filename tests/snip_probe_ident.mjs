// 「这一处是不是他选的那一处」—— **原处身份**,这条链上最后一类"当场看着成功"。
//
// 前几份管的是收得下(`snip_probe.mjs`)、收下来的是不是屏幕上那份文字
// (`snip_probe_shown.mjs`)、收下来之后还作不作数(`snip_probe_edges.mjs`、
// `snip_probe_chat.mjs`)、列表里那几截原文(`snip_probe_cards.mjs`)、绑在哪一处
// 哪一版(`snip_probe_bind.mjs`)。这一份管的是**位置身份不许由"现在正好对得上"
// 推出来** —— 一档一格,外加独立阅读页那一份实现自己再判一次:
//
//   1 **核对那一步不许凭裸引文命中就算过。** 图鉴抽屉开着的那几分钟里,盘上那一篇
//     把选中那一处连它的引子一起换掉、另一处一字不差的同句还在 —— 按裸引文命中
//     它就等于"核过了":版本号跟着换成新那一版、落库返回 200,点回去高亮的是用户
//     从没选过的那一处(审查实测:旧页面版本 ff4fb2dd、核对版本 c54d6364,保存行
//     的 doc_version 被换成后者,回看落在第一节)。
//  1b **屏幕上一处、盘上也一处,而盘上那一处是改稿新写在别处的。** 靠数量分不出
//     来,拦它的是「面板那一版和盘上这一版不等」这个正面证据 —— 否则版本号被偷换
//     成新那一版、落库返回 200,点回去高亮在他从没选过的那一节(审查实测:旧页面
//     版本 eadb7fb8、核对读到 15f02774,保存行的 doc_version 被换成后者)。
//  1c **「只听另一侧」那一档同样不许靠它定身份。** 两节里各写一遍、前文一字不差,
//     旧抽屉上选的是乙节;盘上把乙节连后文一起换掉 —— 前文那一侧于是唯一命中甲
//     节,后文那一侧一处也对不上。核对那一步要是照旧只听另一侧,库里那条就指着他
//     从没选过的甲节(审查实测:旧页面 351ade05、盘上 2c4ef0c0,返回 200 / id=1)。
//  1d **订阅阅读器那一头:原来它压根报不出「我摊的是哪一版」。** 于是「两侧全失配」
//     被按「两种渲染不一样」默认成同一份语料,裸引文那一档照旧放行 —— 盘上把原处
//     那一段换掉、另一节新写一句同句,收藏返回 200 / origin=doc,点回去高亮的是
//     改稿新写的那一节(审查实测:阅读器那一份 7f1aca69、盘上 8dc78146)。这一格
//     **先钉住列表那一行真报出了版本号、而且和 `/api/doc` 是同一个数** —— 不然
//     那条拒绝只是「压根说不出版本」,把这个字段整个删掉也照旧全绿;改稿之前那一句
//     绑的就是那一篇,这条反向判据排在改稿**之前**(之后再也回不去了)。
// 1d-bis **改稿发生在列表铺出来之前。** 这一头摊的是 feeds.db 里那一行,所以那个数
//     只能按**这一行内容**算(`exported.item_body_md`)。响应时读盘算的话,这一行旧正文
//     会带上**新文件**的版本号,核对必然相等、弱档重新放行,点回去高亮改稿新写的
//     那一节(审查实测:阅读器那一份 97aaf058、盘上 8b7eb4cb,返回 200 / id=2)。
//  1e **搜索卡那一头:索引比盘上旧的时候如实说不出版本号。** `best_chunk` 是建索引
//     那一刻切下来的,改稿之后没重建索引,卡片上躺的就是一截旧正文 —— 响应时读盘
//     算的那个数于是必然"相等",弱档重新放行(审查实测:建索引时 9b885a9a、盘上
//     8ae932ab,返回 200 / id=1,回看高亮改稿新写的那一节)。判据是**建索引时那份
//     文件的指纹和此刻盘上这一份逐字节相等**才报,否则空串;而「说不出」一律按
//     「证明不了」办,收藏照旧绑那一篇文章、但如实记 `anchor_exact=0`。
//  1f **采集那一刻就分不出两处这件事,核对这一步不许把它重算掉。** 图鉴抽屉上两处
//     的前后各两百二十字一字不差(锚点三档 30/80/200 都分不出),采集如实说"定不
//     住";页面不刷新、盘上把**选中那一处**整段换掉,剩下那一处于是成了整段锚点的
//     唯一命中 —— 核对要是收不到那件事,就按它判"核过了":`anchor_exact` 重算成 1、
//     版本号换成新那一版、落库返回 200,屏幕上只有一句普通的"片段已收藏",点回去
//     高亮的是他从没选过的那一处(审查实测:屏幕那一份 18930e6c、盘上 81010813,
//     收藏行 u=1/e=1,回看高亮第一处)。第 2 格管**落库之后**那一头(回跳哪一档都
//     不给用),这一格管**落库之前**。
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
// 独立阅读页那一份实现自己再判一次(第 4 格)搬去了
// `tests/snip_probe_ident_doc.mjs` —— 700 行上限,**紧跟在这一份后面跑、顺序一点
// 没动**(它要用这一份留下的那条收藏所在的那一篇导出稿)。
//
// 判据一律落在后果上:**库里那条收藏是什么**(有没有、`quote_unique`/`anchor_exact`
// 记的是几)、**高亮落在第几处**(不是"文字对不对" —— 两处的文字一模一样)、
// **屏幕上那句提示说的是哪一种**。第 2、3 格的语料由 `setDailyBody()` 现摆 ——
// 那是日报正文的**唯一写入口**(产品代码里那一个),出处跟着正文一起落下来。
//
//   HEADLESS_SHELL=<bin> node tests/snip_probe_ident.mjs <baseURL> <manifest.json>
import {
  appToastClear, boot, check, clickJump, dropFavs, evalJs, finish, fx,
  highlightText, js, openFavList, rowsOf, sleep, snip, snipHighlight, until,
} from "./snip_probe_lib.mjs";

/** 沙箱那几条路由(真改盘上那一篇)。回 HTTP 状态。`into` 只有 `drop` 要:给了
 *  就把那句话换成它(而不是换成那句缺省的「已经被改稿换掉了」)。*/
const sandbox = (route, path, text = "", into = "") => evalJs(
  `fetch('/__snip__/' + ${js(route)} + '?path=' + encodeURIComponent(${js(path)})
     + (${js(text)} ? '&text=' + encodeURIComponent(${js(text)}) : '')
     + (${js(into)} ? '&into=' + encodeURIComponent(${js(into)}) : ''),
     { method: 'POST' }).then(r => r.status)`);

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

// ── 1b. 屏幕上一处、盘上也一处 —— 而盘上那一处是改稿新写在别处的 ──────────────
// 这是审查实测的那条形状(两份语料各只有一处,靠数量分不出来):抽屉还开着(版本
// 号是旧的),盘上那一篇把这句话所在那一节换掉、另一节新写了一句一模一样的,前后文
// 都不一样。**裸引文那一档在这儿正好命中它** —— 采信就是把一处用户从没选过的文字
// 当成他选的那一处:版本号跟着换成新那一版(后端那道 409 于是永远判不出"面板旧
// 了")、落库返回 200、点回去高亮在别处。拦它的是「面板那一版和盘上这一版不等」
// 这个**正面证据**,不是数量。
// **反面那一边在 bind 第 4c 格**:同样是版本不等,可盘上只是末尾补了一段、原句连
// 前后文都还在 —— 那一条照旧存得下、绑新那一版、提示里不许出现「刷新」。
await boot();
await evalJs(`switchTab('gallery'); true`);
await until(`typeof openToolDrawer === 'function'`, 15000);
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
if (!await until(`$('td-entries').textContent.includes(${js(fx.toolMove)})`, 25000))
  throw new Error("图鉴抽屉没铺出「原处」那一句");
const moveSeen = () => times(`$('td-entries').textContent`, fx.toolMove);
check("抽屉上这句话只有一处(版本号就是这一刻这一份)", (await moveSeen()) === 1,
  `屏幕上 ${await moveSeen()} 处`);
// 两次改稿:先把原处那一句连引子换掉,再把另一节那段无关的话换成"也写了这一句"
check("沙箱把原处那一句换掉了",
  (await sandbox("drop", fx.toolPath, fx.toolMoveLead + fx.toolMove)) === 200);
check("沙箱在另一节新写了一句一模一样的",
  (await sandbox("drop", fx.toolPath, fx.toolMoveSpare,
     fx.toolMoveSpareLead + fx.toolMove + "。")) === 200);
const diskText = () => evalJs(`fetch('/api/doc?path='
  + encodeURIComponent(${js(fx.toolPath)})).then(r => r.json())
  .then(d => String(d.content || ''))`);
const disk = await diskText();
check("盘上这句话也只有一处 —— 可那一处是新写的那一节(原处的引子没了)",
  disk.split(fx.toolMove).length - 1 === 1
  && disk.includes(fx.toolMoveSpareLead + fx.toolMove)
  && !disk.includes(fx.toolMoveLead + fx.toolMove),
  `盘上 ${disk.split(fx.toolMove).length - 1} 处 / 新引子在=${
    disk.includes(fx.toolMoveSpareLead + fx.toolMove)} / 旧引子在=${
    disk.includes(fx.toolMoveLead + fx.toolMove)}`);
check("屏幕上那一处没跟着变(抽屉就是那一刻读到的那一份)",
  (await moveSeen()) === 1
  && await evalJs(`$('td-entries').textContent.includes(
       ${js(fx.toolMoveLead + fx.toolMove)})`) === true, "屏幕上仍是原处那一段");
await appToastClear();
const moveTake = await snip("#td-entries", fx.toolMove, 1);
check("旧抽屉上那一句照旧选得中、按钮照旧点得下", moveTake.shown,
  `picked=${moveTake.picked}`);
const moveToast = await toast();
check("如实说面板上这份正文旧了、让刷新再收藏(没把版本号偷换成新那一版)",
  moveToast.includes("刷新"), moveToast);
check("一行都没落库(没有一条指着他从没选过的那一节)",
  (await rowsOf(fx.toolMove)) === "[]", await rowsOf(fx.toolMove));
await evalJs(`closeToolDrawer(); true`);

// ── 1c. 两节里各写一遍、前文一字不差:只听还对得上的那一侧就冒认了第一处 ─────
// 上面两格拦的是**裸引文**那一档(两侧全失配),这一格拦的是**只听另一侧**那一档
// —— 审查实测的另一条形状。旧抽屉上选的是乙节那一处,盘上那一篇随后把乙节连后文
// 一起换掉:前文那一侧(两节一字不差)于是**唯一命中甲节**,后文那一侧一处也对不
// 上。「一侧对不上」在核对这一步本来是常态(两头的渲染不一样),可常态证明不了
// 「这两份是同一份语料」—— 版本号不等时照旧只听另一侧,换来的就是一条指着甲节的
// 收藏(审查实测:旧页面 351ade05、盘上 2c4ef0c0,返回 200 / id=1,回看高亮第一节)。
// **反面那一边在 bind 第 4c 格**:同样是版本号不等,可盘上只是末尾补了一段、两侧
// 上下文都还对得上 —— 那一条照旧收得下、绑新那一版,提示里不许出现「刷新」。
await boot();
await evalJs(`switchTab('gallery'); true`);
await until(`typeof openToolDrawer === 'function'`, 15000);
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
if (!await until(`$('td-entries').textContent.split(${js(fx.sideQ)}).length - 1 === 2`,
  25000)) throw new Error("图鉴抽屉没铺出两节各一处那一句");
const sideSeen = () => times(`$('td-entries').textContent`, fx.sideQ);
const sideLeadSeen = () => times(`$('td-entries').textContent`, fx.sideLead + fx.sideQ);
check("抽屉上这句话摆着两处、连前文也一字不差(版本号就是这一刻这一份)",
  (await sideSeen()) === 2 && (await sideLeadSeen()) === 2,
  `屏幕上 ${await sideSeen()} 处 / 带同一段前文的 ${await sideLeadSeen()} 处`);
check("沙箱把乙节那一处连后文一起换掉了",
  (await sandbox("drop", fx.toolPath, fx.sideQ + fx.sideTailB)) === 200);
const sideDisk = await diskText();
check("盘上还剩甲节那一处(所以下面那条拒绝不是因为这段话不在了)",
  sideDisk.split(fx.sideQ).length - 1 === 1 && sideDisk.includes(fx.sideQ + fx.sideTailA),
  `盘上 ${sideDisk.split(fx.sideQ).length - 1} 处`);
check("屏幕上那两处没跟着变(抽屉就是那一刻读到的那一份)", (await sideSeen()) === 2,
  `屏幕上 ${await sideSeen()} 处`);
await appToastClear();
const sideTake = await snip("#td-entries", fx.sideQ, 2);
check("旧抽屉上乙节那一处照旧选得中、按钮照旧点得下", sideTake.shown,
  `picked=${sideTake.picked}`);
const sideToast = await toast();
check("如实说面板上这份正文旧了、让刷新再收藏(没拿前文那一侧的唯一命中当原处)",
  sideToast.includes("刷新"), sideToast);
check("一行都没落库(没有一条指着甲节那一处)", (await rowsOf(fx.sideQ)) === "[]",
  await rowsOf(fx.sideQ));
// 反过来钉一边:**刷新之后(版本号跟上了)剩下那一处照旧收得下** —— 拦住的是
// 「旧页面 + 他选的那一处没了」,不是「一侧对不上就一律收不下」
await boot();
await evalJs(`switchTab('gallery'); true`);
await until(`typeof openToolDrawer === 'function'`, 15000);
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
await until(`$('td-entries').textContent.includes(${js(fx.sideQ)})`, 25000);
check("刷新之后屏幕上跟盘上一样只剩甲节那一处", (await sideSeen()) === 1,
  `屏幕上 ${await sideSeen()} 处`);
await appToastClear();
const sideAgain = await snip("#td-entries", fx.sideQ, 1);
check("剩下那一处照旧收得下", sideAgain.shown, `picked=${sideAgain.picked}`);
const sideRow = JSON.parse(await rowsOf(fx.sideQ));
check("而且绑的是那一篇、按新那一份算出了定得住、后文记的是甲节那一段",
  sideRow.length === 1 && sideRow[0].o === "doc" && sideRow[0].p === fx.toolPath
  && sideRow[0].u === 1 && sideRow[0].e === 1
  && sideRow[0].suf.startsWith(fx.sideTailA.slice(0, 8)), JSON.stringify(sideRow));
await evalJs(`closeToolDrawer(); true`);

// ── 1d. 订阅阅读器:同一件事,而这一头原来压根报不出「我摊的是哪一版」 ─────────
// 审查实测的第三条形状。阅读器摊的是条目正文、盘上那一篇是它导出的那一份,两头的
// 渲染不一样,所以「两侧上下文都对不上」在这儿是常态 —— 原来核对那一步据此默认
// 「这两份是同一份」,裸引文那一档于是照旧放行:盘上那一篇把原处那一段换掉、另一
// 节新写了一句一模一样的,收藏返回 200 / origin=doc,点回去高亮的是改稿新写的那
// 一节(审查实测:阅读器那一份 7f1aca69、盘上 8dc78146)。
// 拦它的是**列表那一行报出来的 `doc_version`**(`/api/intake/{kind}/items`)——
// 所以**先钉住它真有这个数、而且和 `/api/doc` 报的是同一个**:缺了这两条,下面
// 那条拒绝就只是「压根说不出版本」,把这个字段整个删掉也照旧全绿。
// **那个数只能按这一行内容自己算**(`exported.item_body_md` → `body_version_of`),
// 不许响应时去读盘:读盘算出来的是「刚读到的文件」,而这一头摊的是 feeds.db 里
// 那一行 —— 第 1d-bis 格钉的就是这一条(改稿发生在列表铺出来**之前**)。
await boot();
await evalJs(`switchTab('subs'); true`);
if (!await until(`!!document.getElementById('subs-switch-rss')`, 25000))
  throw new Error("订阅面板没列出 rss 那一档");
await evalJs(`document.getElementById('subs-switch-rss').click(); true`);
if (!await until(`typeof sourceRows !== 'undefined'
  && sourceRows.some(r => r.key === ${js(fx.movedKey)})`, 30000))
  throw new Error("订阅列表没列出改稿那一条");
const rowVer = await evalJs(`String((sourceRows.find(
  r => r.key === ${js(fx.movedKey)}) || {}).doc_version || '')`);
const docVer = () => evalJs(`fetch('/api/doc?path='
  + encodeURIComponent(${js(fx.movedPath)})).then(r => r.json())
  .then(d => String(d.version || ''))`);
check("订阅列表那一行报出了「我摊的这一截对应盘上那一版」",
  rowVer.length > 0 && rowVer === await docVer(),
  `列表 ${rowVer} / 接口 ${await docVer()}`);
await evalJs(`readSourceItem(${js(fx.movedKey)}); true`);
if (!await until(`$('source-reader-body').textContent.includes(${js(fx.movedQ)})`, 20000))
  throw new Error("订阅阅读器没摊开改稿那一条");
// **反过来钉一边,而且要排在改稿之前**:没改稿的时候这一句绑的就是那一篇导出稿。
// 放到后面做不到 —— 文件一旦被改,这一行内容和盘上那一份就真的不是一回事了,
// 刷新也回不去(那正是下面 1d-bis 的结论)。
await appToastClear();
const movedOk = await snip("#source-reader-body", fx.movedQ);
check("改稿之前那一句收得下", movedOk.shown, `picked=${movedOk.picked}`);
const movedOkRow = JSON.parse(await rowsOf(fx.movedQ));
check("而且绑的就是那一篇导出稿(拦住的是改稿,不是「订阅正文从此绑不了文档」)",
  movedOkRow.length === 1 && movedOkRow[0].o === "doc"
  && movedOkRow[0].p === fx.movedPath, JSON.stringify(movedOkRow));
await dropFavs(fx.movedQ);
// 两次改稿:原处那一段换掉,另一节换成「也写了这一句」
check("沙箱把原处那一段换掉了",
  (await sandbox("drop", fx.movedPath, fx.movedLead + fx.movedQ)) === 200);
check("沙箱在另一节新写了一句一模一样的",
  (await sandbox("drop", fx.movedPath, fx.movedSpare,
     fx.movedSpareLead + fx.movedQ + "。")) === 200);
const movedDisk = await evalJs(`fetch('/api/doc?path='
  + encodeURIComponent(${js(fx.movedPath)})).then(r => r.json())
  .then(d => String(d.content || ''))`);
check("盘上这句话也只有一处 —— 可那一处是新写在另一节的(原处的引子没了)",
  movedDisk.split(fx.movedQ).length - 1 === 1
  && movedDisk.includes(fx.movedSpareLead + fx.movedQ)
  && !movedDisk.includes(fx.movedLead + fx.movedQ),
  `盘上 ${movedDisk.split(fx.movedQ).length - 1} 处 / 新引子在=${
    movedDisk.includes(fx.movedSpareLead + fx.movedQ)}`);
check("屏幕上那一份没跟着变(阅读器就是那一刻读到的那一份)",
  await evalJs(`$('source-reader-body').textContent.includes(
    ${js(fx.movedLead + fx.movedQ)})`) === true, "屏幕上仍是原处那一段");
await appToastClear();
const movedTake = await snip("#source-reader-body", fx.movedQ);
check("旧阅读器上那一句照旧选得中、按钮照旧点得下", movedTake.shown,
  `picked=${movedTake.picked}`);
const movedRow = JSON.parse(await rowsOf(fx.movedQ));
check("它退回了站外原文那一档,一条都没指着改稿新写的那一节",
  movedRow.length === 1 && movedRow[0].o === "feed" && movedRow[0].p === null,
  JSON.stringify(movedRow));
const movedToast = await toast();
check("而且如实说了退的是哪一种(核对不上,不是一句光秃秃的成功)",
  movedToast.includes("核对不上") && movedToast.includes("站外原文"), movedToast);
// ── 1d-bis. 改稿发生在列表铺出来**之前**:报出来的那个数不许跟着盘上走 ────────
// 上面那一格里改稿发生在加载之后,所以「列表那一行报的数」和「盘上此刻那一版」
// 本来就不等。**真正的坑是倒过来的那个时序**:盘上那一篇先改,之后才打开订阅、
// 铺列表、摊正文 —— 响应那一刻去读盘的话,这一行旧正文会带上**新文件**的版本号,
// 核对那一步两个数必然相等,弱档重新放行,收藏返回 200 / origin=doc,点回去高亮
// 的是改稿新写的那一节(审查实测:阅读器那一份 97aaf058、盘上 8b7eb4cb)。
// 这一头摊的是 feeds.db 里那一行,所以那个数只能按**这一行内容**算
// (`exported.item_body_md`):盘上那一篇已经被改过,两个数于是不等。
await dropFavs(fx.movedQ);
await boot();
await evalJs(`switchTab('subs'); true`);
await until(`!!document.getElementById('subs-switch-rss')`, 25000);
await evalJs(`document.getElementById('subs-switch-rss').click(); true`);
await until(`typeof sourceRows !== 'undefined'
  && sourceRows.some(r => r.key === ${js(fx.movedKey)})`, 30000);
const staleVer = await evalJs(`String((sourceRows.find(
  r => r.key === ${js(fx.movedKey)}) || {}).doc_version || '')`);
const diskVer = await docVer();
check("列表那一行报的还是「我摊的这份正文」那个数 —— 和盘上此刻那一份不等",
  staleVer.length > 0 && diskVer.length > 0 && staleVer !== diskVer,
  `列表 ${staleVer} / 盘上 ${diskVer}`);
await evalJs(`readSourceItem(${js(fx.movedKey)}); true`);
await until(`$('source-reader-body').textContent.includes(${js(fx.movedQ)})`, 20000);
check("摊出来的还是原处那一段(改稿之后才加载,数据库里那一行没跟着变)",
  await evalJs(`$('source-reader-body').textContent.includes(
    ${js(fx.movedLead + fx.movedQ)})`) === true, "屏幕上仍是原处那一段");
await appToastClear();
const staleTake = await snip("#source-reader-body", fx.movedQ);
check("改稿之后才加载的那一页上,那一句照旧选得中、按钮照旧点得下", staleTake.shown,
  `picked=${staleTake.picked}`);
const staleRow = JSON.parse(await rowsOf(fx.movedQ));
check("它照旧退回站外原文 —— 旧正文没有被标成新那一版、也没指着改稿新写的那一节",
  staleRow.length === 1 && staleRow[0].o === "feed" && staleRow[0].p === null,
  JSON.stringify(staleRow));
await dropFavs(fx.movedQ);

// ── 1e. 搜索卡:索引比盘上旧时,如实说不出版本号(「说不出」不等于「同一份」)──
// 搜索卡摊的那一截(`best_chunk`)是**建索引那一刻**从那一篇切下来的,存在
// `chunks.text` 里。盘上那一篇后来被改稿、还没重建索引的那几分钟(或者几天),
// 卡片上躺的就是一截**旧正文** —— 而响应那一刻去读盘算版本号的话,这一截旧文字
// 会带上新文件的那个数:核对必然相等、弱档重新放行,收藏返回 200,点回去高亮的是
// 改稿新写的那一节(审查实测:建索引时 9b885a9a、盘上 8ae932ab)。
// 口径在 `kb/web/common.py` 的 `indexed_doc_version`:**只有建索引时那份文件的
// 指纹和此刻盘上这一份逐字节相等才报**,否则空串 =「说不出」。而「说不出」一律
// 按「证明不了」办(`snip.js` 的 `sameCorpus`),不按「同一份」办。
// 语料借上面那一篇改稿过的订阅条目:1d 已经把它在盘上改掉了、索引没重建,
// 正好就是这个时序。
const hitsOfPath = (q, path) => evalJs(`(async () => {
  const d = await fetch('/api/search?q=' + encodeURIComponent(${js(q)})
    + '&top_k=20').then(r => r.json());
  const h = (d.hits || []).find(x => x.path === ${js(path)});
  return JSON.stringify({ n: (d.hits || []).length, got: !!h,
    v: h ? String(h.doc_version || '') : null });
})()`);
const movedHit = JSON.parse(await hitsOfPath("改稿换到别处", fx.movedPath));
if (!movedHit.got)
  throw new Error("搜索没命中那一篇改稿过的订阅稿:" + JSON.stringify(movedHit));
check("索引比盘上旧的那一条,如实报不出版本号(不拿「刚读到的文件」冒充)",
  movedHit.v === "", JSON.stringify(movedHit));
// 反过来钉一边:**没动过的那一篇照旧报得出,而且就是 `/api/doc` 报的那个数** ——
// 不然「一律回空串」也能让上面那条绿。挑的是**没有任何探针改过的**那一篇
// (`stalePath`:那一条的译文换过、可盘上那份导出稿一个字没动);`docPath` 不行,
// bind 那一份已经在它身上验过「读取与提交之间换版」,此刻它的索引本来就旧了。
const freshHit = JSON.parse(await hitsOfPath("English", fx.stalePath));
if (!freshHit.got)
  throw new Error("搜索没命中那一篇没动过的导出稿:" + JSON.stringify(freshHit));
const freshDisk = await evalJs(`fetch('/api/doc?path='
  + encodeURIComponent(${js(fx.stalePath)})).then(r => r.json())
  .then(d => String(d.version || '')).catch(() => '')`);
check("没动过的那一条照旧报得出,而且和 `/api/doc` 是同一个数",
  freshHit.v && freshHit.v === freshDisk, `${freshHit.v} / ${freshDisk}`);
// 后果落在收藏上:卡片上那一截旧正文照旧收得下(它绑得住那一篇文章),可**不许
// 声称定得住** —— 盘上那一句此刻只剩改稿新写在另一节的那一处,前文那一侧一处也
// 对不上,于是如实记 `anchor_exact=0`,屏幕上当场说一句「点回去可能定位不到」。
await dropFavs(fx.movedQ);
await boot();
await evalJs(`switchTab('search'); $('q').value = '改稿换到别处'; doSearch(); true`);
if (!await until(`[...document.querySelectorAll('.hit[data-hit] .snippet')]
  .some(n => (n.textContent || '').includes(${js(fx.movedQ)}))`, 30000))
  throw new Error("搜索卡上没摊出那一截旧正文");
await appToastClear();
const cardTake = await snip("#results", fx.movedQ);
check("搜索卡上那一截旧正文照旧选得中、按钮照旧点得下", cardTake.shown,
  `picked=${cardTake.picked}`);
const cardRow = JSON.parse(await rowsOf(fx.movedQ));
check("它绑那一篇文章、但如实记下「当时定不住」(e=0),没拿另一节那一处当原处",
  cardRow.length === 1 && cardRow[0].o === "doc" && cardRow[0].p === fx.movedPath
  && cardRow[0].e === 0, JSON.stringify(cardRow));
const cardToast = await toast();
check("而且当场说了一句「点回去可能定位不到这一处」",
  cardToast.includes("定位不到"), cardToast);
await dropFavs(fx.movedQ);

// ── 1f. 采集就分不出两处:核对这一步不许把它重算成「定得住」 ───────────────────
// 上面几格拦的是「核对那一步按别处那一处的命中算过了」,这一格拦的是**采集那一刻
// 已经算出来的事实在半路丢了**:采集阶段那个字段叫 `locatable`、库里那一列叫
// `anchor_exact`,转换只认后者的话,「这条锚点当时就定不住」压根进不了核对那一步,
// 定位函数本来有的那道拒绝于是没机会生效。
// **先在同一份语料上收一次**:那一下产品自己就说得出"这条锚点定不住"(e=0 + 一句
// 「定位不到」)—— 没有这一条,下面那条拒绝也可能只是因为"这段话在盘上没有了"。
await boot();
await evalJs(`switchTab('gallery'); true`);
await until(`typeof openToolDrawer === 'function'`, 15000);
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
if (!await until(`$('td-entries').textContent.split(${js(fx.dupQ)}).length - 1 === 2`,
  25000)) throw new Error("图鉴抽屉没铺出前后两百字都一样的那两处");
const dupSeen = () => times(`$('td-entries').textContent`, fx.dupQ);
// 摆盘前提:**两处的邻域到两百字还是一字不差**(锚点最长那一档就是 200)
const dupShape = await evalJs(`(() => {
  const t = $('td-entries').textContent.replace(/\s+/g, ' ');
  const a = t.indexOf(${js(fx.dupQ)}), b = t.indexOf(${js(fx.dupQ)}, a + 1);
  const q = ${js(fx.dupQ)}.length;
  return [30, 80, 200].every(n => t.slice(a - n, a) === t.slice(b - n, b)
    && t.slice(a + q, a + q + n) === t.slice(b + q, b + q + n));
})()`);
check("抽屉上这句话摆着两处、连前后两百字也一字不差(采集怎么加长都分不出先后)",
  (await dupSeen()) === 2 && dupShape === true,
  `屏幕上 ${await dupSeen()} 处 / 邻域一样=${dupShape}`);
await appToastClear();
const dupSame = await snip("#td-entries", fx.dupQ, 2);
check("语料一个字没改时第二处照旧收得下", dupSame.shown, `picked=${dupSame.picked}`);
const dupSameToast = await toast();
check("而且当场就说了一句「点回去可能定位不到这一处」",
  dupSameToast.includes("定位不到"), dupSameToast);
const dupSameRow = JSON.parse(await rowsOf(fx.dupQ));
check("库里那条如实记着「当时就定不住」(e=0)—— 这是产品自己给的采集期事实",
  dupSameRow.length === 1 && dupSameRow[0].o === "doc" && dupSameRow[0].e === 0
  && dupSameRow[0].u === 0, JSON.stringify(dupSameRow));
await dropFavs(fx.dupQ);   // 下面那条判据要让「库里这一条是什么」只剩一个答案
// 盘上把**选中那一处**(第二处)整段换掉 —— 两处的邻域一字不差,所以改稿那一串要
// 跨过它右边那两百多字、含着只有它才有的那句收尾
check("沙箱把第二处连它右边那两百多字一起换掉了",
  (await sandbox("drop", fx.toolPath, fx.dupQ + fx.dupPad + fx.dupTail)) === 200);
const dupDisk = await diskText();
check("盘上还剩第一处(所以下面那条拒绝不是因为这段话不在了)",
  dupDisk.split(fx.dupQ).length - 1 === 1, `盘上 ${dupDisk.split(fx.dupQ).length - 1} 处`);
check("屏幕上那两处没跟着变(抽屉就是那一刻读到的那一份)", (await dupSeen()) === 2,
  `屏幕上 ${await dupSeen()} 处`);
await appToastClear();
const dupTake2 = await snip("#td-entries", fx.dupQ, 2);
check("旧抽屉上第二处照旧选得中、按钮照旧点得下", dupTake2.shown,
  `picked=${dupTake2.picked}`);
const dupToast2 = await toast();
check("如实说面板上这份正文旧了、让刷新再收藏(没把「定不住」重算成「定得住」)",
  dupToast2.includes("刷新"), dupToast2);
check("一行都没落库(没有一条指着他从没选过的第一处)",
  (await rowsOf(fx.dupQ)) === "[]", await rowsOf(fx.dupQ));
// 反过来钉一边:**刷新之后剩下那一处照旧收得下、而且这一回真定得住** —— 拦住的是
// 「旧页面 + 采集时就分不出 + 选的那一处没了」,不是「这一句一律收不下」
await boot();
await evalJs(`switchTab('gallery'); true`);
await until(`typeof openToolDrawer === 'function'`, 15000);
await evalJs(`openToolDrawer(${js(fx.toolNorm)}); true`);
await until(`$('td-entries').textContent.includes(${js(fx.dupQ)})`, 25000);
check("刷新之后屏幕上跟盘上一样只剩第一处", (await dupSeen()) === 1,
  `屏幕上 ${await dupSeen()} 处`);
await appToastClear();
const dupAgain = await snip("#td-entries", fx.dupQ, 1);
check("剩下那一处照旧收得下", dupAgain.shown, `picked=${dupAgain.picked}`);
const dupAgainRow = JSON.parse(await rowsOf(fx.dupQ));
check("绑的是那一篇、按新那一份算出了唯一且定得住",
  dupAgainRow.length === 1 && dupAgainRow[0].o === "doc"
  && dupAgainRow[0].p === fx.toolPath && dupAgainRow[0].u === 1
  && dupAgainRow[0].e === 1, JSON.stringify(dupAgainRow));
await dropFavs(fx.dupQ);
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

finish();
