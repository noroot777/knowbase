let sourceReaderItem = null, sourceReaderOriginal = false, sourceReaderContext = '';
const sourceReaderKeys = new Map();

function sourceItemReadable(item) {
  return !item.translating && !item.fetching;
}

function sourceItemKind(item) {
  return item.fetching || item.refetching ? '链接条目' : item.content_kind === 'post' ? '社交观点'
    : item.content_kind === 'summary' ? '订阅摘要' : '订阅正文';
}

function sourceItemStatus(item) {
  const text = sourceText(item);
  if (text.fetching) return '正在补抓原文正文';
  if (text.refetching) return '只有链接的壳已入库，正在补抓原文正文覆盖';
  if (text.translating) return '外文原文，正在等翻译';
  if (text.translated) return '已译为中文';
  if (text.retranslating) return '外文原文已入库，正在等中文译文覆盖';
  if (text.partial) return '正文已译为中文，标题还是原文（下次同步补译）';
  return '';
}

function renderSourceEntry(item) {
  const text = sourceText(item), readable = sourceItemReadable(item);
  const post = item.content_kind === 'post', key = esc(item.key);
  const author = item.author || '未知作者', status = sourceItemStatus(item);
  const title = text.translating ? '外文内容，正在等翻译' : text.title;
  // 这一截摊的是**真正的正文**(短观点往往整条都在这儿),所以它能收藏
  // (`kb/static/js/snip_cards.js`)。那两档等待中的文案不是正文,`sourceItemReadable`
  // 把它们挡在收藏之外。
  const pending = text.translating || text.fetching;
  const excerpt = text.translating
    ? '译成中文之后才写进知识库；这一条现在只有原文，点“原文”可以直接看。'
    : text.fetching ? '这个来源只给了一条链接，正在去原文把正文抓回来；抓到之后才翻译、写进知识库。'
    : text.body.slice(0, 380);
  // **省略号不是原文里的字。** 单独挂一个 span,收藏那头按 `.snip-more` 跳过它 ——
  // 混在同一个文本节点里的话,选到结尾那一下会把它一起存进引文,点回去定位不到。
  const more = !pending && text.body.length > 380 ? '<span class="snip-more">…</span>' : '';
  const heading = readable
    ? post ? '' : `<h3><button class="source-entry-title" data-read-item="${key}">${esc(title)}</button></h3>`
    : `<h3 class="source-entry-name">${esc(title)}</h3>`;
  return `<article class="source-entry${readable ? '' : ' is-pending'}" ${readable
    ? `data-source-key="${key}" tabindex="0" aria-label="阅读${esc(post ? author + '的观点' : title)}" aria-current="false"` : ''}>
    <div class="source-entry-meta"><span><b class="source-entry-avatar" aria-hidden="true">${esc(author.slice(0, 1))}</b>${esc(author)}</span><time>${esc(item.published ? sourceTime(item.published) : '发布时间未知')}</time></div>
    ${heading}${!post && text.translated && item.title !== title ? `<p class="source-entry-original">${esc(item.title)}</p>` : ''}
    <p class="source-excerpt${post && readable ? ' is-post' : ''}">${esc(excerpt)}${more}</p>
    <div class="source-entry-footer"><span>${sourceItemKind(item)}${status ? ' · ' + status : ''}</span><div>${!post && !item.x_post_meta && sourceModules.get(item.kind)?.body_from_link ? `<a href="/browser-capture?item=${encodeURIComponent(item.key)}" target="_blank" rel="noopener noreferrer">从浏览器补抓</a>` : ''}${readable ? `<button class="text-button" data-read-item="${key}">展开阅读</button>` : ''}<a href="${esc(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer">原文${arrow('ext')}</a></div></div></article>`;
}

function updateSourceReadingView() {
  const manage = sourceView === 'manage';
  $('source-workbench').classList.toggle('is-managing', manage);
  $('source-reader').hidden = manage;
  $('source-settings').hidden = !manage;
  if (manage) $('source-workbench').classList.remove('is-focused', 'has-mobile-reader', 'is-list-only');
  updateSourceFilterControls();
  updateSourceListButton();
  applySubsReadingLayout();
  updateSourceExpandButton();
}

function resetSourceReader(message = '') {
  sourceReaderItem = null;
  sourceReaderOriginal = false;
  sourceReaderContext = '';
  $('source-reader-content').hidden = true;
  $('source-reader-empty').hidden = false;
  $('source-reader-empty').querySelector('p').textContent = message || '从左侧选一篇';
  $('source-reader-title').textContent = '';
  $('source-reader-body').textContent = '';
  $('source-reader-outline').hidden = true;
  $('source-reader-outline').querySelector('nav').replaceChildren();
  $('source-reader-meta').textContent = '';
  $('source-reader-link').removeAttribute('href');
  $('source-items-count').textContent = '';
  $('source-workbench').classList.remove('has-mobile-reader', 'is-focused');
  updateSourceExpandButton();
  highlightSourceEntry();
}

function sourceReadingContext() {
  const selectedId = $('source-filter').value;
  const source = sourceData?.sources.find(item => String(item.id) === selectedId);
  return JSON.stringify([sourceKind, $('source-q').value.trim(), selectedId,
    source?.url || '', source?.feed_url || '', source?.mode || '']);
}

function syncSourceReader() {
  const key = sourceReaderKeys.get(sourceKind);
  const retained = sourceReaderItem && sourceReaderItem.key === key && sourceItemReadable(sourceReaderItem)
    && sourceReaderContext === sourceReadingContext() && sourceOffset < sourceTotal
    && !sourceRows.some(item => item.key === key) ? sourceReaderItem : null;
  const selected = sourceRows.find(item => item.key === key && sourceItemReadable(item))
    || retained || sourceRows.find(sourceItemReadable);
  if (!selected) {
    resetSourceReader(sourceRows.length ? '内容正在准备中' : '没有可阅读的内容');
    $('source-items-count').textContent = `${sourceTotal} 条`;
    return;
  }
  if (sourceReaderItem?.key === selected.key) {
    const scroll = $('source-reader-scroll').scrollTop;
    sourceReaderItem = selected;
    sourceReaderContext = sourceReadingContext();
    renderSourceReader();
    $('source-reader-scroll').scrollTop = scroll;
    highlightSourceEntry();
  } else {
    readSourceItem(selected.key, false);
  }
}

function highlightSourceEntry() {
  $('source-list').querySelectorAll('[data-source-key]').forEach(entry => {
    const selected = entry.dataset.sourceKey === sourceReaderItem?.key;
    entry.classList.toggle('is-selected', selected);
    entry.setAttribute('aria-current', String(selected));
  });
}

function readSourceItem(key, focus = true) {
  const item = sourceRows.find(row => row.key === key);
  if (!item || !sourceItemReadable(item) || sourceView !== 'items') return;
  sourceReaderItem = item;
  sourceReaderOriginal = false;
  sourceReaderContext = sourceReadingContext();
  sourceReaderKeys.set(sourceKind, key);
  if (focus) {
    $('source-workbench').classList.remove('is-list-only');
    updateSourceListButton();
  }
  $('source-reader').style.display = '';
  $('source-reader-content').hidden = false;
  $('source-reader-empty').hidden = true;
  renderSourceReader();
  $('source-reader-scroll').scrollTop = 0;
  highlightSourceEntry();
  if (focus) {
    $('source-workbench').classList.add('has-mobile-reader');
    $('source-reader').focus({ preventScroll: true });
    if (window.matchMedia('(max-width:760px)').matches) $('source-reader').scrollIntoView({ block: 'start' });
  }
}

function renderSourceReader() {
  const item = sourceReaderItem;
  if (!item) return;
  const text = sourceText(item), original = sourceReaderOriginal;
  const post = item.content_kind === 'post', author = item.author || '未知作者';
  const readable = sourceRows.filter(sourceItemReadable);
  const position = readable.findIndex(row => row.key === item.key);
  $('source-reader-avatar').textContent = author.slice(0, 1);
  $('source-reader-title').hidden = post;
  $('source-reader-title').textContent = post ? '' : (original ? item.title : text.title);
  $('source-reader-meta').textContent = `${author} · ${item.published ? sourceTime(item.published) : '发布时间未知'}${text.translated ? (original ? ' · 原文' : ' · 自动翻译') : ''}`;
  $('source-reader-subtitle').hidden = post || original || !text.translated || item.title === text.title;
  $('source-reader-subtitle').textContent = item.title;
  renderSourceBody(original ? item.body : text.body, { plain:post });
  $('source-reader-kind').textContent = sourceItemKind(item);
  $('source-reader-link').href = safeUrl(item.url);
  $('source-reader-language').hidden = !text.translated;
  $('source-reader-original').hidden = !text.translated;
  $('source-reader-original').classList.toggle('on', original);
  $('source-reader-original').setAttribute('aria-pressed', String(original));
  $('source-reader-translated').classList.toggle('on', !original);
  $('source-reader-translated').setAttribute('aria-pressed', String(!original));
  $('source-reader-position').textContent = position < 0 ? '继续阅读' : `${position + 1} / ${readable.length}`;
  $('source-reader-position').title = position < 0 ? '当前文章暂不在已加载列表中，加载更多可重新定位。' : '当前已加载的可阅读内容';
  $('source-reader-prev').disabled = position <= 0;
  $('source-reader-next').disabled = position < 0 || position >= readable.length - 1;
  const note = [item.content_kind === 'summary' ? '当前收录的是订阅摘要，阅读全文请打开原文。' : '',
    text.refetching || text.retranslating || text.partial ? sourceItemStatus(item) + '。' : ''].filter(Boolean).join('');
  $('source-reader-note').hidden = !note;
  $('source-reader-note').textContent = note;
}

function setSourceOriginal(original) {
  if (!sourceReaderItem || !sourceText(sourceReaderItem).translated) return;
  sourceReaderOriginal = original;
  renderSourceReader();
  $('source-reader-scroll').scrollTop = 0;
}

function toggleSourceOriginal() {
  setSourceOriginal(!sourceReaderOriginal);
}

function stepSourceReader(direction) {
  const readable = sourceRows.filter(sourceItemReadable);
  const position = readable.findIndex(item => item.key === sourceReaderItem?.key);
  if (position < 0) return;
  const next = readable[position + direction];
  if (!next) return;
  readSourceItem(next.key);
  const entry = [...$('source-list').querySelectorAll('[data-source-key]')].find(node => node.dataset.sourceKey === next.key);
  entry?.scrollIntoView({ block: 'nearest' });
}

function updateSourceExpandButton() {
  const focused = $('source-workbench').classList.contains('is-focused');
  $('source-reader-expand').setAttribute('aria-pressed', String(focused));
  $('source-reader-expand').setAttribute('aria-label', focused ? '显示内容列表' : '专注阅读');
  $('source-reader-expand').querySelector('span').textContent = focused ? '显示列表' : '专注阅读';
}

function expandSourceReader() {
  $('source-workbench').classList.remove('is-list-only');
  updateSourceListButton();
  const focused = $('source-workbench').classList.toggle('is-focused');
  if (focused) $('source-workbench').classList.add('has-mobile-reader');
  updateSourceExpandButton();
}

function updateSourceListButton() {
  const listOnly = $('source-workbench').classList.contains('is-list-only');
  $('source-list-expand').setAttribute('aria-pressed', String(listOnly));
  $('source-list-expand').textContent = listOnly ? '显示正文' : '只看列表';
}

function expandSourceList() {
  $('source-workbench').classList.remove('is-focused', 'has-mobile-reader');
  $('source-workbench').classList.toggle('is-list-only');
  updateSourceListButton();
  updateSourceExpandButton();
}

function backToSourceList() {
  $('source-workbench').classList.remove('has-mobile-reader', 'is-focused');
  updateSourceExpandButton();
  const entry = [...$('source-list').querySelectorAll('[data-source-key]')].find(node => node.dataset.sourceKey === sourceReaderItem?.key);
  (entry || $('source-list-pane')).focus({ preventScroll: true });
  if (window.matchMedia('(max-width:760px)').matches) document.querySelector('.source-stream').scrollIntoView({ block: 'start' });
}

// ── 订阅正文的「收藏此段」 ────────────────────────────────────────────────────
// 这一处有两条路,**按「点回去那一页里有没有这段话」分**:
//
// ① 盘上那篇 md 里找得到这段话 → 绑那篇文档(origin=doc),回跳走阅读页的锚点
//    定位(最可靠的那一种)。
// ② 原文视图、还没导出、或者盘上那篇跟屏幕上这份对不上 → origin=feed。
//    **原文在 vault 里压根不存在**(导出写进 md 的是译文),绑文档的话这条收藏
//    点回去永远是「没找到这段收藏」—— 那是把一句准话换成一句假话。feed 这一类
//    回跳走站外原文。
//
// **走哪条不由客户端手里的字段推断,由存之前那一道核对定**:`docText` 去读
// `/api/doc` 那一篇(阅读页渲染的就是这个文件),在里面找这条锚点,找不到就落
// 到②,退的理由如实写进提示。口径在 kb/static/js/snip.js 的 `snipVerified`。
// **核过了还要把「核的是哪一版」带上**(那道核对把 `doc_version` 挂到来源上,
// `postSnip` 原样回传):核对和落库是两个请求,中间那一下重译写回、正文补抓照样
// 会把那一篇换成另一版 —— 后端那时按 409 拒,这一头照旧退到②(见 `RETRY_NOTE`)。
//
// **这一头还要报出「我摊的这一份对应盘上那一版是什么指纹」**(`doc_version`,列表
// 那一行带来的)。核对那一步拿它和刚读到的那一版比:相等才算「屏幕上这一份就是盘
// 上这一份」,一侧上下文失配才可以只听另一侧、才可以退到裸引文。**报不出这个数不
// 等于同一份** —— 原来这一头什么都不报、核对那边按「两头渲染本来就不一样」默认同
// 份,于是审查实测里盘上那一篇把原处那一段换掉、另一节新写了一句一模一样的,裸引
// 文那一档正好命中它:收藏成功,点回去高亮的是改稿新写的那一节。
// **那个数是按这一行内容算的,不是响应时读盘算的**(`/api/intake/{kind}/items` →
// `kb/feeds/exported.item_body_md` → `scanner.body_version_of`):这一头摊的是 feeds.db
// 里那一行,读盘算出来的数证明的是「刚读到的文件」—— 盘上先改稿、之后才铺这张列
// 表的话,这一行旧正文会带着新文件那个数,核对必然相等(审查实测:阅读器那一份
// 97aaf058、盘上 8b7eb4cb,收藏返回 200 / origin=doc,点回去高亮改稿新写的那一节)。
// 口径在 `snip.js` 的 `sameCorpus`。
//
// 原来这里按 `exported_hash === content_hash` 推断「盘上那篇就是这份正文」,
// **那个判据立不住**:它是**原文**的哈希(`kb/feeds/sync.py` 导出成功那一句写的
// 就是 `exported_hash=content_hash`),同一篇原文重译一遍、重导出一遍,它一个
// 字节都不变 —— 于是「两个哈希相等」和「盘上那篇是哪一份译文」没有关系(审查
// 实测:保留一页旧阅读器不刷新,后台重译重导出,两个哈希仍相等、旧判据照旧为真,
// 收藏存成 doc 返回 200,点回去正文是另一份译文、0 个高亮、报「没找到」)。
// 推断换成核对,顺带把这几条都收进同一道闸:
//   - `path` 非空只说明上一次导出留下了一篇,不说明它是这份正文(译文写回只清
//     `exported_hash`、正文补抓只换 `content_hash`,两条都保留 `path`);
//   - md 落盘了、索引那一步还没跑(`/api/doc` 对它判 404),那几分钟里退②;
//   - 短观点和列表仍摊纯文本,文章正文则复用阅读页的 Markdown 渲染规则。
//     核对不能因此省掉:同一段标记在纯文本和阅读页里仍可能长得不一样,
//     `snipRenderedText` 和 `renderSourceBody` 共用 `snipDocRenderer` 保持一致。

/** 一条订阅内容 → 它的收藏出处。**阅读器和列表卡片共用这一份** —— 两处摊的是
 *  同一条内容的同一份正文(列表只摊开头那一截),出处的算法不该各写一遍。
 *  `original` = 摊的是「原文」视图那一份(译文之前的那一份,vault 里没有对应文件)。*/
function sourceItemSnipSource(item, { original = false } = {}) {
  if (!item || !sourceItemReadable(item)) return null;
  const text = sourceText(item);
  const title = (original ? item.title : text.title) || item.title || '订阅内容';
  const viaFeed = {
    origin: 'feed', origin_ref: sourceKind + ':' + item.key,
    origin_title: title, origin_url: item.url || '',
  };
  // 原文视图那一份在 vault 里压根没有对应文件(导出写的是译文),不必去核对
  if (original || !item.path) return viaFeed;
  return {
    origin: 'doc', path: item.path, origin_title: title,
    // 列表那一行报出来的「盘上那一篇此刻是哪一版」(读不出来时是空串 = 说不出,
    // 那一头按「证明不了」办,不按「同一份」办)。见上面那段和 `sameCorpus`。
    doc_version: item.doc_version || '',
    origin_url: item.url || '', fallback: viaFeed,
    docText: () => snipDocText(item.path),
  };
}

attachSnip($('source-reader-body'), () =>
  sourceItemSnipSource(sourceReaderItem, { original: sourceReaderOriginal }));

// 读正文时让出位置:往下滚就把来源工具栏和手动收录的投递栏收起来,滚回顶上再
// 放出来。这一页的阅读区高度锁在可用视口上(`body.subs-reading`),上面那两块
// 一共顶着 155px —— 收不收起来,正文就差那么多行。机制和它吃过的亏在
// `kb/static/js/scrollaway.js`;复位挂在 `applySubsReadingLayout()` 上。
bindScrollAway('source-reader-scroll', ['source-head', 'grab-bar']);
