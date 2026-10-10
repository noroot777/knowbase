async page => {
  const checks = [], errors = [], states = [];
  const check = (name, passed) => {
    if (!passed) throw new Error(name);
    checks.push(name);
  };
  const onError = error => errors.push(error.message);
  const base = new URL(page.url()).origin;
  const snapshot = () => page.evaluate(() => ({
    key: sourceReaderItem?.key, title: sourceReaderItem?.title, context: sourceReaderContext,
    rows: sourceRows.map(item => ({ key: item.key, title: item.title })), total: sourceTotal,
    offset: sourceOffset, scroll: document.getElementById('source-reader-scroll').scrollTop,
    position: document.getElementById('source-reader-position').textContent,
    selectedCards: document.querySelectorAll('.source-entry.is-selected').length,
    moreHidden: document.getElementById('source-more').hidden,
  }));
  const editSubscription = async (sourceId, name, url, feedUrl) => {
    await page.locator('#source-view-manage').click();
    await page.locator(`[data-source-action="edit"][data-source-id="${sourceId}"]`).click();
    await page.locator('#source-edit-name').fill(name);
    if (url !== undefined) await page.locator('#source-edit-url').fill(url);
    if (feedUrl !== undefined) await page.locator('#source-edit-feed').fill(feedUrl);
    await page.locator('#source-edit-save').click();
    await page.waitForFunction(({ sourceId, name, url, feedUrl }) => {
      const source = sourceData?.sources.find(item => item.id === sourceId);
      return !sourceSaving && source?.name === name && (url === undefined || source.url === url)
        && (feedUrl === undefined || source.feed_url === feedUrl);
    }, { sourceId, name, url, feedUrl });
  };
  const syncSubscription = async (sourceId, total) => {
    await page.locator(`[data-source-action="sync"][data-source-id="${sourceId}"]`).click();
    await page.waitForFunction(({ sourceId, total }) => !pipeSnapshot?.running
      && sourceData?.sources.find(item => item.id === sourceId)?.item_count === total,
      { sourceId, total }, { timeout: 45000 });
    await page.locator('#source-view-items').click();
    await page.waitForFunction(total => sourceTotal === total && sourceRows.length === Math.min(total, 30), total);
  };
  const serverItems = async (sourceId, offset = 0) => {
    const response = await page.request.get(`${base}/api/intake/rss/items?limit=100&offset=${offset}&source_id=${sourceId}`);
    if (!response.ok()) throw new Error('读取真实条目接口失败：' + response.status());
    return response.json();
  };
  page.on('pageerror', onError);
  try {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(base + '/?membership-regression=' + Date.now() + '#rss');
    await page.waitForFunction(() => sourceKind === 'rss' && sourceData?.sources.some(source => source.name === '边界旧订阅'));
    const sourceIds = await page.evaluate(() => ({
      complete: sourceData.sources.find(source => source.name === '边界旧订阅').id,
      paged: sourceData.sources.find(source => source.name === '分页旧订阅').id,
    }));
    await page.locator('#source-filter').selectOption(String(sourceIds.complete));
    await page.waitForFunction(() => sourceTotal === 1 && sourceReaderItem?.title === '旧网址的旧文章');
    const beforeComplete = await snapshot();
    check('完整结果场景先选中旧订阅文章', beforeComplete.selectedCards === 1 && beforeComplete.position === '1 / 1');
    await page.screenshot({ path: 'output/playwright/membership-before-edit.png' });
    await editSubscription(sourceIds.complete, '边界新订阅', 'https://example.org/review-new-feed');
    check('真实修改网址已清除旧订阅关联', await page.evaluate(sourceId =>
      sourceData.sources.find(source => source.id === sourceId).item_count === 0, sourceIds.complete));
    await syncSubscription(sourceIds.complete, 1);
    await page.waitForFunction(() => sourceReaderItem?.title === '新网址的新文章');
    const afterComplete = await snapshot(), completeItems = await serverItems(sourceIds.complete);
    states.push({ scenario: 'complete-url-change', before: beforeComplete, after: afterComplete });
    check('更换网址后正文属于真实新订阅结果', completeItems.total === 1
      && completeItems.items[0].key === afterComplete.key && afterComplete.key !== beforeComplete.key);
    check('更换网址使阅读上下文失效', afterComplete.context !== beforeComplete.context);
    check('完整新结果恢复选中卡片与正常位置', afterComplete.selectedCards === 1
      && afterComplete.position === '1 / 1' && afterComplete.moreHidden && afterComplete.scroll === 0);
    await page.screenshot({ path: 'output/playwright/membership-complete-url-fixed.png' });
    await page.locator('#source-view-items').click();
    await page.waitForFunction(() => sourceReaderItem?.title === '新网址的新文章' && !!document.querySelector('.source-entry.is-selected'));
    check('再次读取不恢复旧网址正文', (await snapshot()).key === afterComplete.key);

    await page.locator('#source-filter').selectOption(String(sourceIds.paged));
    await page.waitForFunction(() => sourceTotal === 1 && sourceReaderItem?.title === '分页旧网址的旧文章');
    const beforePaged = await snapshot();
    await editSubscription(sourceIds.paged, '分页新订阅', 'https://example.org/review-paged-new-feed');
    await syncSubscription(sourceIds.paged, 35);
    const afterPaged = await snapshot(), pagedItems = await serverItems(sourceIds.paged);
    states.push({ scenario: 'partial-url-change', before: beforePaged, after: afterPaged });
    check('新网址的结果尚未加载完整', afterPaged.offset === 30 && afterPaged.total === 35 && !afterPaged.moreHidden);
    check('分页未完成也不能保留旧网址正文', afterPaged.key !== beforePaged.key
      && afterPaged.key === pagedItems.items[0].key && !pagedItems.items.some(item => item.key === beforePaged.key));
    check('分页新结果更新上下文并恢复导航', afterPaged.context !== beforePaged.context
      && afterPaged.selectedCards === 1 && afterPaged.position === '1 / 30'
      && !await page.locator('#source-reader-next').isDisabled());
    await page.screenshot({ path: 'output/playwright/membership-partial-url-fixed.png' });

    const lastKey = afterPaged.rows.at(-1).key;
    await page.locator(`[data-source-key="${lastKey}"] .source-entry-title`).click();
    await page.locator('#source-reader-scroll').evaluate(node => { node.scrollTop = 220; });
    const beforeArrival = await snapshot();
    check('真实接口分页场景读至末篇正文中段', beforeArrival.key === lastKey && beforeArrival.scroll === 220);
    const startedAt = Date.now();
    const syncResponse = await page.request.post(base + '/api/intake/rss/sync', { data: { source_ids: [sourceIds.paged] } });
    check('真实后台单源同步成功启动', syncResponse.ok());
    await page.waitForFunction(() => sourceTotal === 36 && sourceRows.length === 30, null, { timeout: 45000 });
    const afterArrival = await snapshot(), tailItems = await serverItems(sourceIds.paged, 30);
    states.push({ scenario: 'real-poll-retention', before: beforeArrival, after: afterArrival,
      elapsedMs: Date.now() - startedAt });
    check('新内容确实把当前文章挤出分页窗口', !afterArrival.rows.some(item => item.key === lastKey)
      && tailItems.items.some(item => item.key === lastKey));
    check('仍属于真实筛选结果的文章保留正文与阅读位置', afterArrival.key === lastKey
      && afterArrival.context === beforeArrival.context && afterArrival.scroll === 220);
    check('真实分页外阅读不显示虚假导航位置', afterArrival.position === '继续阅读'
      && await page.locator('#source-reader-prev').isDisabled() && await page.locator('#source-reader-next').isDisabled());

    await editSubscription(sourceIds.paged, '分页仅改名称');
    await page.locator('#source-view-items').click();
    await page.waitForFunction(() => sourceView === 'items' && sourceRows.length === 30
      && sourceOffset === 30 && sourceTotal === 36 && !document.getElementById('source-more').hidden);
    const afterRename = await snapshot();
    check('仅改显示名称不丢失有效的分页外阅读', afterRename.key === lastKey
      && afterRename.context === beforeArrival.context && afterRename.scroll === 220
      && afterRename.position === '继续阅读');
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: 'output/playwright/membership-valid-retention.png' });

    await editSubscription(sourceIds.paged, '分页替代 RSS', undefined, 'https://example.org/review-paged-other-feed');
    await syncSubscription(sourceIds.paged, 35);
    const afterFeed = await snapshot(), feedItems = await serverItems(sourceIds.paged);
    states.push({ scenario: 'partial-feed-url-change', before: afterRename, after: afterFeed });
    check('网址不变时替代 RSS 地址仍使上下文失效', afterFeed.context !== afterRename.context);
    check('替代 RSS 的分页结果不沿用旧地址正文', afterFeed.offset === 30 && afterFeed.total === 35
      && afterFeed.key === feedItems.items[0].key && !feedItems.items.some(item => item.key === lastKey));
    check('替代 RSS 恢复准确选中状态及阅读起点', afterFeed.selectedCards === 1
      && afterFeed.position === '1 / 30' && afterFeed.scroll === 0);
    await page.screenshot({ path: 'output/playwright/membership-partial-feed-fixed.png' });
    check('整轮真实接口回归无页面脚本异常', errors.length === 0);
    return { result: 'PASS', checks: checks.length, details: checks, states, errors };
  } catch (error) {
    error.message += '\n最后通过：' + checks.at(-1) + '\n当前状态：' + JSON.stringify(await snapshot());
    throw error;
  } finally {
    page.off('pageerror', onError);
  }
}
