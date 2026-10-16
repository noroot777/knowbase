// 「这一处是不是他选的那一处」第 4 格:**独立阅读页那一份实现自己再判一次**。
//
// 共享层那几格在 `tests/snip_probe_ident.mjs` 里(核对那一步、采集期事实、整份
// 正文那三类),**这一份紧跟在它后面跑**:同一个沙箱、同一个库,顺序有意义。
// 拆成两份只因为 700 行上限 —— 判据的分界正好落在"两份实现各要有自己的红"上:
// `/doc/…` 自带一份内联的采集和定位(`kb/web/pages.py` 的 `_DOC_SNIP_JS` / `seek`,
// 那一页脱离 kb-web 也要能打开),共享层全绿证明不了它。
//
//   HEADLESS_SHELL=<bin> node tests/snip_probe_ident_doc.mjs <baseURL> <manifest.json>
import {
  base, boot, check, DOC_HELPERS, docSnip, dropFavs, evalJs, favDocHref, finish,
  fx, highlightText, js, openFavList, rowsOf, send, sleep, until,
} from "./snip_probe_lib.mjs";

await boot();

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
