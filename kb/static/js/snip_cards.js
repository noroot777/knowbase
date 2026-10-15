// 列表里的「收藏此段」:订阅列表卡片的那一截正文、搜索结果卡片的命中片段。
//
// **这两处摊的都是真原文**,不是模型写的摘要 —— 前者是订阅正文开头那 380 字
// (短社交观点往往整条都在里面),后者是命中那一篇正文里的一段(`best_chunk`)。
// 需求是「只要能展示原文的地方都能收」,所以它们也得接上。
//
// **为什么单开一个文件**:这两个容器分别住在 `sources.js` / `core.js` 里,而
// `attachSnip` 在 `snip.js` 里 —— core.js 比 snip.js 先加载,在它顶层注册会撞
// ReferenceError。注册集中在这儿,加载顺序只要求它排在 snip.js 和
// source_reader.js 之后(见 `kb/static/index.html`)。
//
// 三条共同的边界:
//   - **只开正文那一段。** 标题、作者、日期、来源 badge、实体按钮、以及搜索卡上
//     那块 LLM 摘要(`.summary`)都不在范围内 —— 它们不是原文,收了点回去一定找
//     不到。判据是「选区得落在那一段的容器里」,不是「落在卡片里」。
//   - **装饰字符不算原文。** 两处在正文被截断时都补了一个「…」,它在那一篇里压根
//     不存在,所以各自挂成独立的 `.snip-more`、由 `skip` 跳过。
//   - **容器只是那一篇的一截**,所以一律 `partial: true`:唯一性不按这一截断言
//     (切片里只有一处证明不了整篇里只有一处,见 `snip.js` 的 `snipSpec`)。绑了
//     文档的那一类随后会按盘上那一篇重算一遍,那一份才是回跳要用的语料。
//
// 出处的算法不在这儿重写:订阅那边直接用阅读器那一份(`sourceItemSnipSource`),
// 搜索那边按命中那一篇的 path 走 origin=doc 那一档。事故形态和回归在
// docs/decisions/fav-snippets.md。

/** 卡片里那一段正文的容器 —— 选区不在它**里面**就不受理。 */
function snipCardBody(range, cardSel, bodySel) {
  const card = snipHostOf(range, cardSel);
  const body = card && card.querySelector(bodySel);
  if (!body || !body.contains(range.commonAncestorContainer)) return null;
  return { card, body };
}

const SNIP_CARD_SKIP = '.snip-more';

attachSnip($('source-list'), range => {
  const host = snipCardBody(range, '.source-entry[data-source-key]', '.source-excerpt');
  if (!host) return null;
  const item = sourceRows.find(row => row.key === host.card.dataset.sourceKey);
  // 列表摊的是**正文那一份**(译文优先,同阅读器的默认视图),不是「原文」视图
  const src = sourceItemSnipSource(item, { original: false });
  return src && { ...src, root: host.body, skip: SNIP_CARD_SKIP, partial: true };
});

attachSnip($('results'), range => {
  const host = snipCardBody(range, '.hit[data-hit]', '.snippet');
  if (!host) return null;
  const hit = searchHits[Number(host.card.dataset.hit)];
  // 搜索结果都是库里的文档,没有 path 的命中不该存在 —— 真碰上就不给收,
  // 而不是存一条没有落点的收藏
  if (!hit || !hit.path) return null;
  // **卡片上这一截不带版本号,而且它和那一页上的文本本来就不是同一份。** 检索
  // 语料是入库时切的块(`kb/chunker.py` 为了让检索词对得上把 `*` 剥掉了,却留着
  // `~~` 之类),卡片又只摊命中的那一截。所以这一类**不自带 `doc_version`**,
  // 交给 `snipVerified` 现读那一篇:核过了就按盘上那一份重算唯一性和定位、
  // 带着**读到的那一版**落库;核不过(删除线那类)也带着读到的那一版照旧绑这一
  // 篇 —— 点回去打开的至少是对的那一篇文章,而收藏那一刻会当场说一句「点回去
  // 可能定位不到这一处」(见 `snip.js` 的 `snipVerified` / `STAY_NOTE`)。
  // **不给 `fallback`**:退到 `origin=feed` 是假话(这是库里的文章,不是订阅
  // 条目),而 `origin=doc` 配空 path 后端按 400 拒 —— 读不出那一篇的那两种
  // (已经不在索引里 / 这一刻读不出来)由 `saveSnip` 如实拒掉并说明原因。
  return {
    origin: 'doc', path: hit.path, root: host.body, skip: SNIP_CARD_SKIP, partial: true,
    origin_title: hit.title || hit.path, origin_url: hit.url || '',
    docText: () => snipDocText(hit.path),
  };
});
