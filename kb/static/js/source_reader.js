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
  const excerpt = text.translating
    ? '译成中文之后才写进知识库；这一条现在只有原文，点“原文”可以直接看。'
    : text.fetching ? '这个来源只给了一条链接，正在去原文把正文抓回来；抓到之后才翻译、写进知识库。'
    : text.body.slice(0, 380) + (text.body.length > 380 ? '…' : '');
  const heading = readable
    ? post ? '' : `<h3><button class="source-entry-title" data-read-item="${key}">${esc(title)}</button></h3>`
    : `<h3 class="source-entry-name">${esc(title)}</h3>`;
  return `<article class="source-entry${readable ? '' : ' is-pending'}" ${readable
    ? `data-source-key="${key}" tabindex="0" aria-label="阅读${esc(post ? author + '的观点' : title)}" aria-current="false"` : ''}>
    <div class="source-entry-meta"><span><b class="source-entry-avatar" aria-hidden="true">${esc(author.slice(0, 1))}</b>${esc(author)}</span><time>${esc(item.published ? sourceTime(item.published) : '发布时间未知')}</time></div>
    ${heading}${!post && text.translated && item.title !== title ? `<p class="source-entry-original">${esc(item.title)}</p>` : ''}
    <p class="source-excerpt${post && readable ? ' is-post' : ''}">${esc(excerpt)}</p>
    <div class="source-entry-footer"><span>${sourceItemKind(item)}${status ? ' · ' + status : ''}</span><div>${readable ? `<button class="text-button" data-read-item="${key}">展开阅读</button>` : ''}<a href="${esc(safeUrl(item.url))}" target="_blank" rel="noopener noreferrer">原文${arrow('ext')}</a></div></div></article>`;
}

function updateSourceReadingView() {
  const manage = sourceView === 'manage';
  $('source-workbench').classList.toggle('is-managing', manage);
  $('source-reader').hidden = manage;
  $('source-settings').hidden = !manage;
  if (manage) $('source-workbench').classList.remove('is-focused', 'has-mobile-reader');
  updateSourceExpandButton();
}

function resetSourceReader(message = '') {
  sourceReaderItem = null;
  sourceReaderOriginal = false;
  sourceReaderContext = '';
  $('source-reader-content').hidden = true;
  $('source-reader-empty').hidden = false;
  $('source-reader-empty').querySelector('h3').textContent = message || '在这里安心阅读';
  $('source-reader-empty').querySelector('p').textContent = message ? '选择其他内容，或稍后再试。' : '从左侧选择一篇内容，正文会在这里展开。';
  $('source-reader-title').textContent = '';
  $('source-reader-body').textContent = '';
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
  $('source-reader-body').textContent = original ? item.body : text.body;
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
  const focused = $('source-workbench').classList.toggle('is-focused');
  if (focused) $('source-workbench').classList.add('has-mobile-reader');
  updateSourceExpandButton();
}

function backToSourceList() {
  $('source-workbench').classList.remove('has-mobile-reader', 'is-focused');
  updateSourceExpandButton();
  const entry = [...$('source-list').querySelectorAll('[data-source-key]')].find(node => node.dataset.sourceKey === sourceReaderItem?.key);
  (entry || $('source-list-pane')).focus({ preventScroll: true });
  if (window.matchMedia('(max-width:760px)').matches) document.querySelector('.source-stream').scrollIntoView({ block: 'start' });
}
