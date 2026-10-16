export async function checkSubscriptionUsability({ evaluate, wait, check, click, viewport, screenshot, send }) {
  const key = async (value, code, virtualKey) => {
    await send('Input.dispatchKeyEvent', { type:'keyDown', key:value, code, windowsVirtualKeyCode:virtualKey });
    await send('Input.dispatchKeyEvent', { type:'keyUp', key:value, code, windowsVirtualKeyCode:virtualKey });
  };
  const heading = () => evaluate(`(() => {
    const page = document.querySelector('.subs-page-heading').getBoundingClientRect();
    const toolbar = document.querySelector('.source-toolbar').getBoundingClientRect();
    return [page.top, page.height, toolbar.top, toolbar.height, $('source-workbench').getBoundingClientRect().top,
      $('source-list-pane').getBoundingClientRect().top];
  })()`);
  const search = async query => {
    await evaluate(`$('source-q').value = ${JSON.stringify(query)}; sourceFilterChanged(); true`);
  };
  await viewport(1440, 1000);
  await evaluate(`openSubsPanel('social'); $('wechat-welcome').hidden = true; true`);
  await wait(`sourceKind === 'social' && sourceRows.length >= 30 && sourceData`);
  await evaluate(`clearTimeout(sourcePoll); true`);
  const initialHeading = await heading();
  await evaluate(`window.qaSubsHeader = document.querySelector('.subs-page-heading');
    window.qaSourceToolbar = document.querySelector('.source-toolbar'); true`);
  await click('#source-view-manage');
  await wait(`sourceView === 'manage' && document.querySelector('.source-subscription')`);
  check('阅读与管理的页头、工具栏及内容起点完全一致', JSON.stringify(initialHeading) === JSON.stringify(await heading()));
  check('切换视图复用页头和工具栏而不是重建', await evaluate(`window.qaSubsHeader === document.querySelector('.subs-page-heading') && window.qaSourceToolbar === document.querySelector('.source-toolbar')`));
  check('管理列表与设置各自滚动且不撑高整页', await evaluate(`$('source-list-pane').scrollHeight > $('source-list-pane').clientHeight && getComputedStyle($('source-settings')).overflowY === 'auto' && document.documentElement.scrollHeight <= innerHeight + 1`));
  await screenshot('manage-1440');
  await click('#source-view-items');
  await wait(`sourceView === 'items' && sourceRows.length >= 30`);
  await search('译文');
  await wait(`sourceTotal > 0 && sourceTotal < 90 && sourceRows.length === sourceTotal`);
  await evaluate(`$('source-filter').value = String(sourceData.sources.find(source => source.name === '长内容布局验证').id); sourceFilterChanged(); true`);
  await wait(`sourceReaderItem && sourceReaderContext === sourceReadingContext()`);
  await evaluate(`readSourceItem(sourceRows[2].key, false); $('source-list-pane').scrollTop = 220;
    $('source-reader-scroll').scrollTop = 180; window.qaReading = {
      key:sourceReaderItem.key, q:$('source-q').value, id:$('source-filter').value,
      list:$('source-list-pane').scrollTop, reader:$('source-reader-scroll').scrollTop }; true`);
  await click('#source-view-manage');
  check('管理搜索不继承内容关键词或来源筛选', await evaluate(`!$('source-q').value && !$('source-filter').value && $('source-filter').hidden && $('source-list-expand').hidden`));
  await search('长内容布局验证');
  await wait(`document.querySelectorAll('.source-subscription').length === 1`);
  check('管理筛选显示匹配数量与总数', await evaluate(`$('source-items-count').textContent.startsWith('1 / ') && $('source-q').getAttribute('aria-label').includes('订阅名称')`));
  await click('#source-view-items');
  await wait(`sourceReaderItem?.key === window.qaReading.key && $('source-q').value === window.qaReading.q && document.querySelector('.source-entry.is-selected')?.dataset.sourceKey === window.qaReading.key`);
  check('返回阅读保留来源筛选、选中文章及两栏滚动位置', await evaluate(`$('source-filter').value === window.qaReading.id && Math.abs($('source-list-pane').scrollTop - window.qaReading.list) < 1 && Math.abs($('source-reader-scroll').scrollTop - window.qaReading.reader) < 1`));
  await click('#source-view-manage');
  check('再次进入管理保留独立搜索条件', await evaluate(`$('source-q').value === '长内容布局验证' && document.querySelectorAll('.source-subscription').length === 1`));
  await click('#source-filter-reset');
  await wait(`document.querySelectorAll('.source-subscription').length === sourceData.sources.length`);
  check('清除管理筛选后恢复所有订阅并返回搜索焦点', await evaluate(`$('source-filter-reset').hidden && document.activeElement === $('source-q')`));
  await click('#source-view-items');
  await wait(`sourceReaderItem && $('source-q').value === '译文'`);
  await click('#source-filter-reset');
  await wait(`sourceTotal === 90 && sourceRows.length >= 30`);
  check('清除内容筛选同时重置关键词和来源', await evaluate(`!$('source-q').value && !$('source-filter').value && $('source-filter-reset').hidden`));
  await evaluate(`$('source-more').scrollIntoView({block:'nearest'}); true`);
  await click('#source-more');
  await wait(`sourceRows.length === 60`);
  await evaluate(`readSourceItem(sourceRows[38].key, false); window.qaLoadedKey = sourceReaderItem.key; true`);
  await click('#source-view-manage');
  await click('#source-view-items');
  await wait(`sourceRows.length === 60 && sourceReaderItem?.key === window.qaLoadedKey && document.querySelector('.source-entry.is-selected')?.dataset.sourceKey === window.qaLoadedKey`);
  check('视图切换保留已加载的分页窗口及后续文章', await evaluate(`sourceRows.length === 60 && document.querySelector('[data-source-key="' + window.qaLoadedKey + '"]')?.getAttribute('aria-current') === 'true'`));
  await search('没有匹配的验证关键词');
  await wait(`sourceTotal === 0 && !!document.querySelector('[data-clear-source-filters]')`);
  check('筛选空态解释原因并提供恢复入口', await evaluate(`$('source-list').textContent.includes('没有匹配的内容') && !$('source-filter-reset').hidden`));
  await click('[data-clear-source-filters]');
  await wait(`sourceTotal === 90 && sourceReaderItem`);
  check('空态按钮可恢复全部内容', await evaluate(`!$('source-q').value && sourceRows.length >= 30`));
  await evaluate(`$('source-list-pane').scrollTop = 0; document.querySelector('[data-source-key]').focus(); true`);
  await key('ArrowDown', 'ArrowDown', 40);
  check('方向键切换文章预览并保留列表焦点', await evaluate(`sourceReaderItem.key === sourceRows[1].key && document.activeElement.dataset.sourceKey === sourceRows[1].key`));
  await key('Home', 'Home', 36);
  check('Home 可回到第一条可阅读内容', await evaluate(`sourceReaderItem.key === sourceRows[0].key`));
  await click('#source-notices > summary');
  await evaluate(`$('source-notices').querySelector('summary').focus(); true`);
  await key('Escape', 'Escape', 27);
  check('Escape 关闭状态详情并保留焦点', await evaluate(`!$('source-notices').open && document.activeElement === $('source-notices').querySelector('summary')`));
  await click('#source-notices > summary');
  await click('#source-view-items');
  check('点击详情之外可以收起状态浮层', await evaluate(`!$('source-notices').open`));
  await evaluate(`$('subs-switch-social').focus(); true`);
  await key('ArrowRight', 'ArrowRight', 39);
  await wait(`sourceKind === 'rss' && sourceRows.length >= 30`);
  check('来源方向键切换后焦点落在新标签', await evaluate(`document.activeElement === $('subs-switch-rss') && $('subs-switch-rss').getAttribute('tabindex') === '0' && document.querySelectorAll('#subs-switch [tabindex="0"]').length === 1`));
  const feedHeading = await evaluate(`JSON.stringify([...document.querySelector('.subs-page-heading').getClientRects()].map(box => [box.top,box.height,box.width]))`);
  await evaluate(`openSubsPanel('wechat'); true`);
  await wait(`!$('subs-wechat').hidden && $('src-status').textContent !== '检查中…'`);
  check('公众号与其他来源共用相同尺寸的页头', feedHeading === await evaluate(`JSON.stringify([...document.querySelector('.subs-page-heading').getClientRects()].map(box => [box.top,box.height,box.width]))`));
  check('公众号连接说明默认收起但仍有明确入口', await evaluate(`!$('subs-wechat-notices').hidden && !$('subs-wechat-notices').open && !$('src-detail').hidden`));
  const wechatHeight = await evaluate(`$('tgt-list').getBoundingClientRect().height`);
  await click('#subs-wechat-notices > summary');
  check('展开公众号连接详情不会挤压订阅列表', wechatHeight === await evaluate(`$('tgt-list').getBoundingClientRect().height`));
  await screenshot('wechat-1440');
  await evaluate(`openSubsPanel('social'); true`);
  await wait(`sourceKind === 'social' && sourceReaderItem && sourceRows.length >= 30`);
  for (const width of [320, 375, 760, 761, 820, 1024, 1440]) {
    await viewport(width, 900);
    const geometry = await evaluate(`({width:innerWidth,document:document.documentElement.scrollWidth,
      list:$('source-list-pane').getBoundingClientRect().height, page:document.documentElement.scrollHeight})`);
    check(`${width}px 阅读页无横向溢出并保留足够列表高度`, geometry.document <= geometry.width && geometry.list >= 280, JSON.stringify(geometry));
    const before = await heading();
    await click('#source-view-manage');
    check(`${width}px 阅读与管理切换不改变工具栏`, JSON.stringify(before) === JSON.stringify(await heading()));
    if (width <= 760) {
      await evaluate(`$('source-settings-save').scrollIntoView({block:'center'}); true`);
      check(`${width}px 手机管理仍能访问保存设置`, await evaluate(`$('source-settings-save').getBoundingClientRect().bottom <= innerHeight && !$('source-settings-fields').disabled && document.documentElement.scrollWidth <= innerWidth`));
    }
    await evaluate(`window.scrollTo(0,0); true`);
    await click('#source-view-items');
    await wait(`sourceView === 'items' && sourceReaderItem`);
    await evaluate(`window.scrollTo(0,0); true`);
  }
  await viewport(375, 844);
  await evaluate(`$('subs-switch-social').focus(); true`);
  await key('End', 'End', 35);
  // End 落在**最后一类来源**上,而那是哪一类由 `SOURCE_TYPES` 说了算 ——
  // 写死 'web' 的那一版在新增一类来源时当场超时(而它要验的「切一次只滚标签栏」
  // 跟是哪一类毫无关系)。
  const lastKind = await evaluate(`subsCatalog[subsCatalog.length - 1].id`);
  await wait(`sourceKind === ${JSON.stringify(lastKind)} && sourceReaderItem`);
  check('手机来源切换仅滚动标签栏且选中标签可见', await evaluate(`$('subs-switch').scrollLeft > 0 && $('subs-switch-${lastKind}').getBoundingClientRect().right <= innerWidth && document.documentElement.scrollWidth <= innerWidth && window.scrollY === 0`));
  await screenshot('reading-mobile-375');
  await click('#source-view-manage');
  await screenshot('manage-mobile-375');
  await evaluate(`openSubsPanel('wechat'); true`);
  await wait(`!$('subs-wechat').hidden`);
  for (const width of [320, 375]) {
    await viewport(width, 844);
    check(`${width}px 公众号管理不溢出且搜索添加仍可见`, await evaluate(`document.documentElement.scrollWidth <= innerWidth && $('tgt-search').getBoundingClientRect().width >= 160 && $('tgt-search-btn').getBoundingClientRect().right <= innerWidth`));
  }
  await screenshot('wechat-mobile-375');
  await viewport(1440, 600);
  await evaluate(`openSubsPanel('social'); true`);
  await wait(`sourceKind === 'social' && sourceData`);
  if (await evaluate(`sourceView === 'manage'`)) await click('#source-view-items');
  await wait(`sourceView === 'items' && sourceReaderItem`);
  await evaluate(`window.scrollTo(0,0); true`);
  const compact = await evaluate(`(() => {
    const pane = $('source-list-pane').getBoundingClientRect();
    return {height:pane.height,top:pane.top,page:document.documentElement.scrollHeight,
      rows:[...document.querySelectorAll('.source-entry')].filter(node => {
        const row=node.getBoundingClientRect(); return row.top >= pane.top && row.bottom <= pane.bottom;
      }).length}; })()`);
  check('矮桌面窗口把可读高度留给列表而不是页头', compact.height >= 390 && compact.rows >= 3 && compact.page <= 600, JSON.stringify(compact));
  await screenshot('reading-desktop-1440-600');
  await viewport(1440, 1000);
}
