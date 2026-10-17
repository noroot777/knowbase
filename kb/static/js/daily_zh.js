// ── 日报正文里的本地译文入口 ──────────────────────────────────────────────────
// 出处编号点开是**站外原文**,而其中一部分是外文。那些内容在写进知识库之前就已经
// 译过一遍,vault 里躺着的正是中文那一份(口径见 docs/decisions/translate.md)——
// 也就是说"点开发现是英文"在本地本来有答案,只是日报正文里没有入口。这一片就做
// 这一件事:问一次后端「这些地址里哪几条本地有译文」,有的就在编号后面补一个
// 「译」,点开从**左边**滑出阅读器读那一篇。
//
// 判据整个在后端(`kb/daily_trans.py`:vault md 的 frontmatter 里
// `translated_from` 非空),这边不猜 —— 回包里没有的那些一个字都不加,照旧只有
// 原文链接。阅读器是 core.js 那一个(`a[data-doc]` 的点击分派),`data-side=left`
// 只换它从哪一边出来。
//
// **补上去那个字是 CSS 画的(`.daily-zh::after`),不是文本节点。** 日报正文同时是
// 「收藏此段」的采集面,而片段回跳的锚点按正文的**文字坐标系**算
// (`snip_text.js`):往正文里插一个真字符,已经收下的片段就整体错位、回跳落空。
// 伪元素的 content 既不进 `textContent` 也不进 `Selection.toString()`(拿
// chrome-headless-shell 实测确认过),于是坐标系逐字不变。同理样式只许用行内
// display(`inline` / `inline-*`)—— 块级会被 `snipBreaksOf` 当成一处显示分隔,
// 坐标系里凭空多一个换行,一样错位。
//
// 从 daily.js 分出来是因为它那边到了 CLAUDE.md 那条 700 行上限;入口由
// `openDailyReport` 在正文落下来之后调,不 await —— 日报本身已经在屏幕上了。

/** 一个译文入口元素。**里面不放文本节点**(理由见文件抬头)。 */
function dailyZhLink(url, d, vault) {
  const a = document.createElement('a');
  a.className = 'daily-zh';
  a.href = docLink(d);
  // 点击分派认这个(core.js):走站内阅读器,中键/Cmd 点照旧能新标签打开。
  a.dataset.doc = '1';
  a.dataset.path = encodeURIComponent(d.path);
  a.dataset.title = d.title || '';
  a.dataset.url = safeUrl(url);        // 阅读器头上那个「原文」入口
  a.dataset.obs = obsLink({ vault, path: d.path });
  a.dataset.side = 'left';
  // 字是伪元素画的,所以读屏软件在这里什么也读不到 —— 名字得自己给。
  a.setAttribute('aria-label', '读本地中文译文');
  a.title = '本地中文译文'
    + (d.original_title ? `（原文标题：${d.original_title}）` : '');
  return a;
}

/** 这一份日报正文里,出处本地有译文的那几处,在编号后面补上入口。 */
async function markDailyTranslations(el, date) {
  const refs = [...el.querySelectorAll('a.daily-ref[href]')]
    .filter(a => /^https?:/i.test(a.getAttribute('href') || ''));
  if (!refs.length) return;
  let data;
  try {
    data = await pipePost('/api/daily/translations',
                          { urls: [...new Set(refs.map(a => a.getAttribute('href')))] });
  } catch {
    return;    // 静默:正文已经读到了,为一个额外入口弹一句报错是在添乱
  }
  // **回包到了,屏幕上还是发请求时那一份吗。** 翻到别的日报 ⇒ 正文容器记着的日期
  // (`setDailyBody` 写的 `data-snip-ref`)换了;同一天重新渲染过 ⇒ 日期没换,但
  // 手上这批节点已经离开文档树。
  //
  // **这两道都是「早一道」,真正兜住屏幕的是「只往手上那批节点旁边插」** —— 正文
  // 是整块 innerHTML 重建的,换过之后这些引用指向的是一棵没人看的 DOM,插进去也
  // 落不到屏幕上。如实记着:单独去掉任何一道,回归都红 0 条。留着是因为它们挡的
  // 是**另一种实现**:回包落地后重新 `querySelectorAll` 一遍(看上去同样合理)时,
  // 新正文里同一个编号会被当场插上入口 —— 两份日报连着引用同一篇译文是常事。
  // 那种实现下两道各红一条(反向验证记在 docs/decisions/daily.md)。
  if (el.dataset.snipRef !== date) return;
  const docs = data.docs || {};
  for (const a of refs) {
    const d = docs[a.getAttribute('href')];
    // 已经补过就别再补一个(同一份正文上重复调用)。
    if (!d || !a.isConnected || a.nextElementSibling?.classList.contains('daily-zh')) continue;
    a.after(dailyZhLink(a.getAttribute('href'), d, data.vault));
  }
}
