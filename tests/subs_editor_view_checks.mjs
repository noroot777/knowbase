export async function checkSubscriptionEditorViews({ evaluate, wait, check, click, screenshot, send }) {
  const originalKind = await evaluate('sourceKind');
  const savedViews = await evaluate(`['rss:items', 'rss:manage'].map(key => ({ key, state:sourceViewState.get(key) || null }))`);
  const savedFilter = await evaluate(`sourceFilters.get('rss') || null`);
  const createdIds = [];
  const itemQuery = '知识', managerQuery = '管理保留回归';
  const type = async (id, text) => {
    await click('#' + id);
    await send('Input.insertText', { text });
  };
  const clearFilters = async () => {
    if (await evaluate(`!$('source-filter-reset').hidden`)) await click('#source-filter-reset');
    await wait(`!$('source-q').value && !$('source-filter').value`);
  };
  const contentSearch = async () => {
    await click('#source-view-items');
    await clearFilters();
    await type('source-q', itemQuery);
    await wait(`sourceView === 'items' && $('source-q').value === ${JSON.stringify(itemQuery)} && sourceRows.length >= 30 && sourceReaderItem`);
    await evaluate(`clearTimeout(sourcePoll); $('source-list-pane').scrollTop = 145; true`);
    await click('.source-entry:nth-of-type(3)');
    await wait(`sourceReaderItem?.key === sourceRows[2].key`);
    return evaluate(`({ key:sourceReaderItem.key, scroll:$('source-list-pane').scrollTop })`);
  };
  const create = async (name, url) => {
    await click('#source-add');
    await wait(`$('source-editor').style.display !== 'none' && document.activeElement === $('source-edit-name')`);
    await send('Input.insertText', { text:name });
    await type('source-edit-url', url);
    await evaluate(`$('source-edit-save').scrollIntoView({ block:'nearest' }); true`);
    await click('#source-edit-save');
    await wait(`!sourceSaving && $('source-editor').style.display === 'none' && sourceView === 'manage'
      && sourceData.sources.some(source => source.url === ${JSON.stringify(url)})`);
    const id = await evaluate(`sourceData.sources.find(source => source.url === ${JSON.stringify(url)}).id`);
    createdIds.push(id);
    await evaluate(`clearTimeout(sourcePoll); true`);
    return id;
  };
  const hasRow = id => evaluate(`!!document.querySelector('[data-source-action="edit"][data-source-id="${id}"]')`);
  const showRow = async (id, label) => {
    const name = `${label}新增订阅滚到可视区后编辑按钮可真实命中`;
    if (!await hasRow(id)) { check(name, false, '新增订阅不在管理列表中'); return; }
    await evaluate(`document.querySelector('[data-source-action="edit"][data-source-id="${id}"]')
      .closest('.source-subscription').scrollIntoView({ block:'nearest' }); true`);
    check(name, await evaluate(`(() => {
      const button = document.querySelector('[data-source-action="edit"][data-source-id="${id}"]'), box = button.getBoundingClientRect();
      return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === button;
    })()`));
  };
  try {
    await click('#subs-switch-rss');
    await wait(`sourceKind === 'rss' && sourceData && sourceRows.length > 0`);
    await click('#source-view-manage');
    await clearFilters();
    await click('#source-view-items');
    const initialCount = await evaluate('sourceData.sources.length');
    const firstReading = await contentSearch();
    await screenshot('editor-content-search-before-add');
    const firstId = await create('添加回归新订阅', 'https://example.org/editor-view-empty.xml');
    check('内容搜索后添加确实创建一次订阅', await evaluate(`sourceData.sources.length === ${initialCount + 1}`));
    check('内容搜索后添加使用管理视图自己的空关键词', await evaluate(`sourceView === 'manage' && !$('source-q').value && $('source-filter').hidden`));
    check('内容搜索后添加立即呈现新订阅而非筛空提示', await hasRow(firstId)
      && await evaluate(`!$('source-list').textContent.includes('没有匹配的订阅') && document.querySelectorAll('.source-subscription').length === ${initialCount + 1}`));
    await showRow(firstId, '默认管理筛选');
    await screenshot('editor-add-from-content-visible');
    await click('#source-view-items');
    await wait(`sourceView === 'items' && sourceReaderItem && document.querySelectorAll('.source-entry').length > 0`);
    check('添加返回内容后恢复原关键词', await evaluate(`$('source-q').value === ${JSON.stringify(itemQuery)}`));
    const restoredReading = await evaluate(`({ key:sourceReaderItem.key, scroll:$('source-list-pane').scrollTop })`);
    check('添加返回内容后保留阅读与列表位置', restoredReading.key === firstReading.key
      && Math.abs(restoredReading.scroll - firstReading.scroll) < 1, JSON.stringify({ firstReading, restoredReading }));

    await click('#source-view-manage');
    await clearFilters();
    await type('source-q', managerQuery);
    await wait(`sourceView === 'manage' && $('source-list').textContent.includes('没有匹配的订阅')`);
    const secondReading = await contentSearch();
    const secondName = managerQuery + '新增订阅';
    const secondId = await create(secondName, 'https://example.org/editor-view-saved.xml');
    check('内容搜索后添加恢复已保存的管理关键词', await evaluate(`$('source-q').value === ${JSON.stringify(managerQuery)}`));
    check('恢复管理筛选后新订阅可见且只匹配它', await hasRow(secondId)
      && await evaluate(`document.querySelectorAll('.source-subscription').length === 1 && $('source-list').textContent.includes(${JSON.stringify(secondName)})`));
    check('第二次添加不会重复创建已有订阅', await evaluate(`sourceData.sources.length === ${initialCount + 2}`));
    await showRow(secondId, '已保存管理筛选');
    await screenshot('editor-add-restores-manager-search');

    if (!await hasRow(secondId)) {
      await clearFilters();
      await type('source-q', managerQuery);
      await wait(`!!document.querySelector('[data-source-action="edit"][data-source-id="${secondId}"]')`);
    }
    await click(`[data-source-action="edit"][data-source-id="${secondId}"]`);
    await wait(`$('source-editor').style.display !== 'none' && sourceEditId === ${secondId}`);
    await send('Input.dispatchKeyEvent', { type:'keyDown', key:'End', code:'End', windowsVirtualKeyCode:35 });
    await send('Input.dispatchKeyEvent', { type:'keyUp', key:'End', code:'End', windowsVirtualKeyCode:35 });
    await send('Input.insertText', { text:'（更新）' });
    await evaluate(`$('source-edit-save').scrollIntoView({ block:'nearest' }); true`);
    await click('#source-edit-save');
    await wait(`!sourceSaving && $('source-editor').style.display === 'none'
      && sourceData.sources.some(source => source.id === ${secondId} && source.name === ${JSON.stringify(secondName + '（更新）')})`);
    check('管理内编辑保存保留用户自己的筛选', await evaluate(`sourceView === 'manage' && $('source-q').value === ${JSON.stringify(managerQuery)}`) && await hasRow(secondId));
    check('编辑更新既有订阅而不是新建重复条目', await evaluate(`sourceData.sources.length === ${initialCount + 2}`));
    await click('#source-view-items');
    await wait(`sourceView === 'items' && sourceReaderItem && document.querySelectorAll('.source-entry').length > 0`);
    check('管理编辑后返回内容仍恢复原搜索与阅读', await evaluate(`$('source-q').value === ${JSON.stringify(itemQuery)}
      && sourceReaderItem.key === ${JSON.stringify(secondReading.key)}`));
    await contentSearch();
    await click('#source-add');
    await wait(`$('source-editor').style.display !== 'none' && document.activeElement === $('source-edit-name')`);
    await evaluate(`document.querySelector('#source-editor .source-form-actions button[type="button"]').scrollIntoView({ block:'nearest' }); true`);
    await wait(`(() => {
      const button = document.querySelector('#source-editor .source-form-actions button[type="button"]'), box = button.getBoundingClientRect();
      return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === button;
    })()`);
    const cancelTarget = await evaluate(`(() => {
      const button = document.querySelector('#source-editor .source-form-actions button[type="button"]'), box = button.getBoundingClientRect();
      return { hit:document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)?.outerHTML, top:box.top, bottom:box.bottom, viewport:innerHeight };
    })()`);
    await click('#source-editor .source-form-actions button[type="button"]');
    const cancelled = await evaluate(`({ display:$('source-editor').style.display, view:sourceView, q:$('source-q').value, count:sourceData.sources.length, saving:sourceSaving })`);
    check('取消添加不切换视图或丢掉内容搜索', cancelled.display === 'none' && cancelled.view === 'items'
      && cancelled.q === itemQuery && cancelled.count === initialCount + 2, JSON.stringify({ cancelTarget, cancelled }));
  } finally {
    await evaluate(`(async () => {
      closeSourceEditor(); clearTimeout(sourceSearchTimer); clearTimeout(sourcePoll);
      for (const id of ${JSON.stringify(createdIds)}) await pipeJson('/api/intake/rss/sources/' + id, { method:'DELETE' });
      if (sourceKind === 'rss') await loadSourceModule(true);
      clearTimeout(sourcePoll);
    })()`);
    await evaluate(`openSubsPanel(${JSON.stringify(originalKind)}); true`);
    await wait(`sourceKind === ${JSON.stringify(originalKind)} && sourceData && sourceRows.length > 0`);
    await evaluate(`for (const saved of ${JSON.stringify(savedViews)}) {
      if (saved.state) sourceViewState.set(saved.key, saved.state); else sourceViewState.delete(saved.key);
    }
    const savedFilter = ${JSON.stringify(savedFilter)};
    if (savedFilter) sourceFilters.set('rss', savedFilter); else sourceFilters.delete('rss');
    clearTimeout(sourcePoll); true`);
  }
}
