async page => {
  const checks = [], errors = [], itemRequests = [];
  const check = (name, passed) => {
    if (!passed) throw new Error(name);
    checks.push(name);
  };
  const onError = error => errors.push(error.message);
  page.on('pageerror', onError);
  const base = new URL(page.url()).origin;
  await page.setViewportSize({ width: 1440, height: 1000 });
  const overview = await (await page.request.get(base + '/api/intake/rss')).json();
  const sourceId = overview.sources[0].id, otherSourceId = overview.sources[1].id;
  const makeItem = index => ({
    key: 'boundary-' + index, source_id: index === 2 ? otherSourceId : sourceId,
    title: '阅读边界文章 ' + String(index).padStart(2, '0'), title_zh: '', body_zh: '',
    body: Array.from({ length: 35 }, (_entry, paragraph) =>
      `这是第 ${index} 篇文章的第 ${paragraph + 1} 段。阅读过程中，新内容不应该自动替换这篇文章。`).join('\n\n'),
    author: '隔离回归样例', url: 'https://example.org/boundary/' + index,
    published: '2026-10-09 09:30', content_kind: 'content',
  });
  let items = Array.from({ length: 30 }, (_entry, index) => makeItem(index + 1));
  items[29] = { ...items[29], title: 'Boundary article 30', title_zh: '阅读边界文章 30',
    body_zh: items[29].body, body: 'Continue reading the selected article without interruption.\n\n'.repeat(35) };
  const lastItem = items[29];
  const socialItem = { ...makeItem(1), key: 'boundary-post', title: '观点没有重复标题',
    body: '观点没有重复标题', content_kind: 'post' };
  const overviewHandler = route => route.fulfill({ contentType: 'application/json',
    body: JSON.stringify({ ...overview, total: items.length, sources: overview.sources.map(source => ({
      ...source, item_count: items.filter(item => item.source_id === source.id).length,
    })) }),
  });
  const itemHandler = route => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get('limit') || 30), offset = Number(url.searchParams.get('offset') || 0);
    const query = url.searchParams.get('q') || '', filter = url.searchParams.get('source_id');
    const candidates = url.pathname.includes('/social/') ? [socialItem] : items;
    const filtered = candidates.filter(item => (!filter || String(item.source_id) === filter)
      && [item.title, item.title_zh, item.body, item.body_zh].join(' ').includes(query));
    itemRequests.push({ limit, offset, query, filter, total: filtered.length });
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
      items: filtered.slice(offset, offset + limit), total: filtered.length, translating: 0, fetching: 0,
    }) });
  };
  const snapshot = () => page.evaluate(() => ({
    key: sourceReaderItem?.key, rows: sourceRows.length, offset: sourceOffset, total: sourceTotal,
    classes: document.getElementById('source-workbench').className,
    scroll: document.getElementById('source-reader-scroll').scrollTop, original: sourceReaderOriginal,
    position: document.getElementById('source-reader-position').textContent,
    currentInList: sourceRows.some(item => item.key === sourceReaderItem?.key),
  }));
  await page.route('**/api/intake/rss', overviewHandler);
  await page.route('**/api/intake/*/items?*', itemHandler);
  try {
    await page.goto(base + '/?boundary-regression=' + Date.now() + '#rss');
    await page.waitForFunction(() => sourceReaderItem?.key === 'boundary-1'
      && sourceRows.length === 30 && !!document.querySelector('.source-entry.is-selected'));
    check('初次自动选文尚未打开手机阅读模式', !await page.locator('#source-workbench').evaluate(node => node.classList.contains('has-mobile-reader')));
    await page.locator('#source-reader-expand').click();
    check('专注阅读正常显示正文并收起列表', await page.locator('#source-reader').isVisible()
      && !await page.locator('.source-stream').isVisible());
    await page.setViewportSize({ width: 390, height: 844 });
    const resizeResult = { state: await snapshot(), readerVisible: await page.locator('#source-reader').isVisible(),
      listVisible: await page.locator('.source-stream').isVisible() };
    check('自动选文后专注再缩窄仍能阅读正文', resizeResult.readerVisible && !resizeResult.listVisible);
    await page.locator('#source-view-items').click();
    await page.waitForFunction(() => sourceRows.length === 30 && !!document.querySelector('.source-entry.is-selected'));
    check('窄屏重新读取不会进入双列空白', await page.locator('#source-reader').isVisible());
    await page.locator('#source-reader').scrollIntoViewIfNeeded();
    await page.screenshot({ path: 'output/playwright/boundary-focus-mobile-fixed.png' });
    await page.locator('.source-reader-back').click();
    check('跨断点专注阅读可以返回列表', await page.locator('.source-stream').isVisible()
      && !await page.locator('#source-reader').isVisible());
    await page.setViewportSize({ width: 1440, height: 1000 });
    check('返回宽屏恢复双栏而不是保留空白', await page.locator('.source-stream').isVisible()
      && await page.locator('#source-reader').isVisible());
    await page.locator('#source-reader-expand').click();
    await page.locator('#source-reader-expand').click();
    await page.setViewportSize({ width: 390, height: 844 });
    check('退出专注后缩窄仍至少保留阅读区', await page.locator('#source-reader').isVisible());
    await page.locator('.source-reader-back').click();
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('[data-source-key="boundary-30"] .source-entry-title').click();
    await page.locator('#source-reader-original').click();
    await page.locator('#source-reader-scroll').evaluate(node => { node.scrollTop = 220; });
    await page.evaluate(() => window.scrollTo(0, 0));
    const beforeRefresh = await snapshot(), requestsBefore = itemRequests.length;
    check('正在读窗口末篇原文且位于正文中段', beforeRefresh.key === lastItem.key
      && beforeRefresh.original && beforeRefresh.scroll === 220);
    await page.screenshot({ path: 'output/playwright/boundary-refresh-before.png' });
    const startedAt = Date.now();
    items = [makeItem(31), ...items];
    await page.waitForFunction(() => sourceTotal === 31 && sourceRows[0]?.key === 'boundary-31', null, { timeout: 22000 });
    const afterRefresh = await snapshot();
    const refreshResult = { before: beforeRefresh, after: afterRefresh, elapsedMs: Date.now() - startedAt,
      requests: itemRequests.slice(requestsBefore) };
    check('真实自动轮询已更新列表窗口', afterRefresh.total === 31 && afterRefresh.rows === 30
      && afterRefresh.offset === 30 && !afterRefresh.currentInList);
    check('新内容挤出末篇时仍保留当前文章', afterRefresh.key === lastItem.key);
    check('后台更新保留正文滚动位置', afterRefresh.scroll === beforeRefresh.scroll);
    check('后台更新保留原文语言选择和全文', afterRefresh.original
      && await page.locator('#source-reader-body').textContent() === lastItem.body);
    check('列表之外的当前文章不显示虚假的零号位置', afterRefresh.position === '继续阅读');
    check('不能把列表首篇误当成当前文章的下一篇', await page.locator('#source-reader-prev').isDisabled()
      && await page.locator('#source-reader-next').isDisabled());
    await page.screenshot({ path: 'output/playwright/boundary-refresh-after-fixed.png' });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('.source-reader-back').click();
    check('文章移出列表后返回仍获得可见焦点', await page.locator('#source-list-pane').evaluate(node => node === document.activeElement));
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('#source-more').click();
    await page.waitForFunction(() => sourceRows.length === 31 && !!document.querySelector('[data-source-key="boundary-30"].is-selected'));
    const afterMore = await snapshot();
    check('加载更多从真实偏移继续且不重复条目', afterMore.offset === 31
      && await page.evaluate(() => new Set(sourceRows.map(item => item.key)).size === 31));
    check('文章重新进入列表时保留语言和阅读位置', afterMore.key === lastItem.key
      && afterMore.original && afterMore.scroll === beforeRefresh.scroll);
    check('文章重新进入列表后恢复准确位置和导航', afterMore.position === '31 / 31'
      && !await page.locator('#source-reader-prev').isDisabled() && await page.locator('#source-reader-next').isDisabled());
    await page.locator('#source-reader-prev').click();
    check('恢复导航后上一篇仍按列表顺序切换', await page.evaluate(() => sourceReaderItem.key === 'boundary-29'));
    await page.locator('#source-reader-next').click();
    check('恢复导航后可以回到原文章', await page.evaluate(() => sourceReaderItem.key === 'boundary-30'));
    items = items.filter(item => item.key !== lastItem.key);
    await page.locator('#source-view-items').click();
    await page.waitForFunction(() => sourceRows.length === 30 && sourceTotal === 30);
    check('完整结果排除当前文章时不保留旧正文', await page.evaluate(() => sourceReaderItem?.key === 'boundary-31'));
    check('完整结果重新选中有效文章并恢复导航', await page.locator('.source-entry.is-selected').count() === 1
      && await page.locator('#source-reader-position').textContent() === '1 / 30'
      && !await page.locator('#source-reader-next').isDisabled());
    check('完整结果不误导用户继续加载已不存在的文章', await page.locator('#source-more').isHidden()
      && await page.locator('#source-reader-scroll').evaluate(node => node.scrollTop === 0));
    await page.locator('#source-view-manage').click();
    await page.locator('#source-q').fill('阅读边界文章 02');
    await page.locator('#source-view-items').click();
    await page.waitForFunction(() => sourceRows.length === 1 && sourceReaderItem?.key === 'boundary-2');
    check('管理模式改变关键词后不保留不匹配的旧文章', await page.locator('#source-reader-title').textContent() === '阅读边界文章 02');
    await page.locator('#source-q').fill('');
    await page.waitForFunction(() => sourceRows.length === 30);
    await page.locator('#source-filter').selectOption(String(sourceId));
    const filteredCount = items.filter(item => item.source_id === sourceId).length;
    await page.waitForFunction(count => sourceRows.length === count && sourceTotal === count
      && sourceReaderItem?.source_id === Number(document.getElementById('source-filter').value), filteredCount);
    check('改变订阅筛选后不泄漏其他订阅的旧正文', await page.evaluate(() => sourceReaderItem.key !== 'boundary-2'));
    await page.locator('#source-filter').selectOption('');
    await page.locator('#source-q').fill('没有任何匹配内容');
    await page.waitForFunction(() => sourceTotal === 0 && sourceReaderItem === null);
    check('空结果仍清除保留正文和原文链接', await page.locator('#source-reader-body').textContent() === ''
      && await page.locator('#source-reader-link').getAttribute('href') === null);
    await page.locator('#source-q').fill('');
    await page.waitForFunction(() => !!sourceReaderItem);
    await page.locator('#subs-switch-social').click();
    await page.waitForFunction(() => sourceKind === 'social' && sourceReaderItem?.key === 'boundary-post');
    check('切换来源不能继续显示上一来源的缓存文章', await page.locator('#source-reader-body').textContent() === socialItem.body);
    check('整轮没有页面脚本异常', errors.length === 0);
    return { result: 'PASS', checks: checks.length, details: checks, resizeResult, refreshResult, errors };
  } catch (error) {
    error.message += '\n最后通过：' + checks.at(-1) + '\n当前状态：' + JSON.stringify(await snapshot());
    throw error;
  } finally {
    await page.unroute('**/api/intake/rss', overviewHandler);
    await page.unroute('**/api/intake/*/items?*', itemHandler);
    page.off('pageerror', onError);
  }
}
