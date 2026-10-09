async page => {
  const checks = [], errors = [];
  const check = (name, passed) => {
    if (!passed) throw new Error(name);
    checks.push(name);
  };
  const onError = error => errors.push(error.message);
  page.on('pageerror', onError);
  const base = new URL(page.url()).origin;
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(base + '/?reading-qa=' + Date.now() + '#rss');
  await page.waitForFunction(() => sourceData?.sources.length > 1 && sourceKind === 'rss');
  const sourceIds = await page.evaluate(() => sourceData.sources.slice(0, 2).map(source => source.id));
  const sourceCount = await page.evaluate(() => sourceData.sources.length);
  const rssOverview = await page.evaluate(() => sourceData);
  const chinese = [
    '阅读不是收集更多链接，而是为下一次行动积累更好的判断。',
    '一、为长期思考留出空间',
    '信息每天都在更新，但真正值得留下的观点往往需要反复阅读。把零散的发现整理到同一个地方，让它们在需要时重新出现。',
    '与其不停切换窗口，不如先选一篇文章，在安静的阅读区里把它读完。列表负责发现，正文负责理解，两件事各有自己的空间。',
    '二、让知识进入工作流',
    '当一个观点能够解释你正在遇到的问题时，它就从信息变成了知识。记下问题，找到相关材料，再把结论带回真实的工作。',
    '好的工具不应该替你决定所有事情，而应该减少那些重复的操作。搜索、筛选和选中状态都留在原地，阅读就不必一次次从头开始。',
    '三、留下可以复用的线索',
    '每次阅读后，留下一句自己的理解：它改变了什么判断？还能用在哪里？这比把整篇文章收藏后再也不打开更有价值。',
    '把长期目标拆成小而可重复的动作。保持耐心，也保留好奇心，让每一次阅读都比上一次多走一步。',
  ].join('\n\n');
  const first = {
    key: 'reading-1', title: 'From reading to action: a quieter knowledge workflow',
    title_zh: '把知识变成行动：让阅读真正进入工作流', body: 'A quieter reading workflow.\n\n' + 'Read, reflect, and put knowledge into practice.\n\n'.repeat(30),
    body_zh: chinese + '\n\n' + chinese, author: '长期主义笔记',
    published: '2026-10-09 09:30', content_kind: 'content', url: 'https://example.org/reading-1',
    source_id: sourceIds[0],
  };
  const rss = [first, ...Array.from({ length: 33 }, (_, index) => ({
    key: 'reading-' + (index + 2), title: ['寻找同行者：比快速出发更重要的事', '建立个人知识库之前，先想清楚你要回答什么', '三个能让阅读更专注的小习惯'][index % 3],
    body: '让信息有序地进入日常工作。\n\n保留清晰的上下文，减少来回切换。\n\n' + chinese,
    title_zh: '', body_zh: '', author: ['思考的尺度', '工具与方法', '每周阅读'][index % 3],
    published: '2026-10-08 14:30', content_kind: index === 2 ? 'summary' : 'content',
    url: 'https://example.org/reading-' + (index + 2), source_id: sourceIds[index % 2],
  }))];
  rss[4] = { ...rss[4], title: 'Just a queued English article', body: 'Never show this English draft.', translating: true };
  rss[5] = { ...rss[5], title: '链接条目正在准备', body: 'Never show this jump-link shell.', fetching: true };
  rss[6] = { ...rss[6], body: '已入库的链接壳', refetching: true };
  rss[7] = { ...rss[7], body: '已有英文正文，等待译文覆盖', retranslating: true };
  rss[8] = { ...rss[8], body_zh: '正文译好了，标题还在等待补译。', partial: true };
  rss[9] = { ...rss[9], title: '符号与安全', body: 'R&D，a < b，Rust & Go\n<img src=x onerror="window.__readerXSS=1">' };
  const social = [{ ...rss[1], key: 'post-1', title: '这是一条没有标题的观点。', body: '这是一条没有标题的观点。\n\n读完一篇文章，再开始下一篇。', content_kind: 'post', author: '方法笔记' }];
  rssOverview.total = rss.length;
  rssOverview.sources = rssOverview.sources.map(source => ({ ...source,
    item_count: rss.filter(item => item.source_id === source.id).length,
  }));
  const overviewHandler = route => route.fulfill({ contentType: 'application/json', body: JSON.stringify(rssOverview) });
  let mode = 'normal';
  const handler = async route => {
    const url = new URL(route.request().url());
    const items = url.pathname.includes('/social/') ? social : rss;
    if (mode === 'error') return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '隔离测试：内容暂时不可用' }) });
    const query = url.searchParams.get('q') || '', selected = url.searchParams.get('source_id');
    const filtered = items.filter(item => (!selected || String(item.source_id) === selected)
      && (item.title + item.title_zh + item.body + item.body_zh).includes(query));
    const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 30);
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
      items: filtered.slice(offset, offset + limit), total: filtered.length, translating: 1, fetching: 1,
    }) });
  };
  await page.route('**/api/intake/*/items?*', handler);
  await page.route('**/api/intake/rss', overviewHandler);
  try {
    await page.evaluate(() => loadSourceModule(false));
    await page.locator('#source-view-items').click();
    await page.waitForFunction(() => sourceReaderItem?.key === 'reading-1' && !!document.querySelector('.source-entry.is-selected'));
    check('自动选中第一篇可阅读内容', await page.locator('.source-entry.is-selected').count() === 1);
    check('默认显示中文译文和原文标题', await page.locator('#source-reader-body').textContent() === first.body_zh
      && await page.locator('#source-reader-subtitle').textContent() === first.title);
    check('阅读区常驻且不锁住页面', !await page.locator('#source-reader').evaluate(node => node.classList.contains('modal-mask'))
      && !await page.locator('#main-content').evaluate(node => node.inert));
    check('阅读模式不挤占同步设置', !await page.locator('#source-settings').isVisible());
    const layout = await page.evaluate(() => {
      const list = document.querySelector('.source-stream').getBoundingClientRect();
      const reader = document.getElementById('source-reader').getBoundingClientRect();
      return reader.left >= list.right - 1 && reader.width > list.width * 1.5 && Math.abs(reader.height - list.height) < 2;
    });
    check('桌面上列表窄、正文宽且高度一致', layout);
    check('总数和已加载数量分别正确', await page.locator('#source-items-count').textContent() === '34 条'
      && await page.locator('.source-entry').count() === 30);
    await page.screenshot({ path: 'output/playwright/subscription-reader-desktop.png' });
    await page.locator('#source-reader-original').click();
    check('切换原文不丢失正文', await page.locator('#source-reader-body').textContent() === first.body);
    await page.locator('#source-reader-translated').click();
    check('可切回中文译文', await page.locator('#source-reader-body').textContent() === first.body_zh);
    const independent = await page.evaluate(() => {
      const list = document.getElementById('source-list-pane'), reader = document.getElementById('source-reader-scroll');
      reader.scrollTop = 180;
      list.scrollTop = 230;
      return list.scrollTop === 230 && reader.scrollTop === 180;
    });
    check('列表和正文可以独立滚动', independent);
    await page.locator('[data-source-key="reading-2"]').click();
    check('点击卡片切换正文并重置正文滚动', await page.evaluate(() => sourceReaderItem.key === 'reading-2'
      && document.getElementById('source-reader-scroll').scrollTop === 0));
    await page.locator('[data-source-key="reading-3"]').focus();
    await page.keyboard.press('Enter');
    check('键盘可以选文', await page.evaluate(() => sourceReaderItem.key === 'reading-3'));
    await page.locator('#source-reader-next').click();
    check('下一篇导航切换文章', await page.evaluate(() => sourceReaderItem.key === 'reading-4'));
    check('摘要明确提示不是全文', (await page.locator('#source-reader-note').textContent()).includes('订阅摘要'));
    await page.locator('#source-reader-next').click();
    check('导航跳过未入库的待翻译和待补抓内容', await page.evaluate(() => sourceReaderItem.key === 'reading-7'));
    check('已有链接壳仍能读并明确标注补抓', (await page.locator('#source-reader-note').textContent()).includes('壳已入库'));
    await page.locator('#source-reader-next').click();
    check('已有外文仍能读且明确等待覆盖', (await page.locator('#source-reader-note').textContent()).includes('正在等中文译文覆盖'));
    await page.locator('#source-reader-next').click();
    check('半译文保留真实状态', (await page.locator('#source-reader-note').textContent()).includes('标题还是原文'));
    await page.locator('#source-reader-next').click();
    check('HTML 标签按纯文本安全展示', await page.locator('#source-reader-body').textContent() === rss[9].body
      && await page.locator('#source-reader-body img').count() === 0 && !await page.evaluate(() => window.__readerXSS));
    await page.locator('#source-more').click();
    await page.waitForFunction(() => sourceRows.length === 34);
    check('加载更多保留选中文章', await page.evaluate(() => sourceReaderItem.key === 'reading-10'));
    await page.locator('#source-reader-expand').click();
    check('专注阅读收起列表', !await page.locator('.source-stream').isVisible());
    await page.evaluate(() => renderSourceView(false));
    check('后台刷新保留专注模式和选中内容', !await page.locator('.source-stream').isVisible()
      && await page.evaluate(() => sourceReaderItem.key === 'reading-10'));
    await page.locator('#source-reader-expand').click();
    check('可恢复列表', await page.locator('.source-stream').isVisible());
    await page.locator('#source-q').fill('不存在的文章关键词');
    await page.waitForFunction(() => sourceRows.length === 0);
    check('筛选为空时清掉旧正文和原文地址', await page.locator('#source-reader-body').textContent() === ''
      && await page.locator('#source-reader-link').getAttribute('href') === null);
    await page.locator('#source-q').fill('符号与安全');
    await page.waitForFunction(() => sourceRows.length === 1);
    check('筛选后自动阅读匹配内容', await page.evaluate(() => sourceReaderItem.key === 'reading-10'));
    await page.locator('#source-q').fill('');
    await page.waitForFunction(() => sourceRows.length === 30);
    await page.locator('#source-filter').selectOption(String(sourceIds[1]));
    await page.waitForFunction(() => sourceRows.length < 30 && sourceRows.every(item => item.source_id === sourceData.sources[1].id));
    check('按订阅筛选同步更新阅读区', await page.evaluate(() => sourceReaderItem.source_id === sourceData.sources[1].id));
    await page.locator('#source-filter').selectOption('');
    await page.waitForFunction(() => sourceRows.length === 30);
    mode = 'error';
    await page.locator('#source-view-items').click();
    await page.locator('[data-retry-items]').waitFor();
    check('读取失败不残留正文', await page.locator('#source-reader-body').textContent() === '');
    mode = 'normal';
    await page.locator('[data-retry-items]').click();
    await page.waitForFunction(() => !!sourceReaderItem);
    check('读取失败可手动恢复', await page.locator('[data-retry-items]').count() === 0);
    await page.locator('#source-view-manage').click();
    check('管理模式保留订阅操作和同步设置', await page.locator('#source-settings').isVisible()
      && !await page.locator('#source-reader').isVisible() && await page.locator('.source-subscription').count() === sourceCount);
    await page.locator('#subs-switch-social').click();
    await page.locator('#source-view-items').click();
    await page.waitForFunction(() => sourceReaderItem?.key === 'post-1');
    check('切来源清掉上个来源正文', await page.locator('#source-reader-body').textContent() === social[0].body);
    check('大V观点不重复显示伪标题', !await page.locator('#source-reader-title').isVisible()
      && await page.locator('.source-entry h3').count() === 0);
    await page.setViewportSize({ width: 390, height: 844 });
    check('手机默认显示内容列表', await page.locator('.source-stream').isVisible()
      && !await page.locator('#source-reader').isVisible());
    await page.locator('.source-entry').click();
    check('手机选文打开正文', await page.locator('#source-reader').isVisible()
      && !await page.locator('.source-stream').isVisible());
    await page.locator('.source-reader-back').click();
    check('手机可返回列表并保留选中状态', await page.locator('.source-stream').isVisible()
      && await page.locator('.source-entry.is-selected').count() === 1);
    await page.locator('#subs-switch-rss').click();
    await page.locator('#source-view-items').click();
    await page.waitForFunction(() => sourceKind === 'rss' && sourceRows.length === 30);
    await page.locator('[data-source-key="reading-1"]').click();
    check('手机无横向溢出', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    check('手机阅读区充分利用视口', await page.locator('#source-reader').evaluate(node => node.getBoundingClientRect().height >= innerHeight - 100));
    await page.screenshot({ path: 'output/playwright/subscription-reader-mobile.png' });
    await page.evaluate(() => renderSourceView(false));
    check('手机后台刷新不关闭正在阅读的正文', await page.locator('#source-reader').isVisible());
    await page.locator('.source-reader-back').click();
    await page.setViewportSize({ width: 1024, height: 900 });
    check('平板保持双栏且不横向溢出', await page.locator('.source-stream').isVisible()
      && await page.locator('#source-reader').isVisible() && await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: 'output/playwright/subscription-reader-tablet.png' });
    await page.setViewportSize({ width: 1440, height: 1000 });
    check('整轮没有页面脚本错误', errors.length === 0);
    return { result: 'PASS', checks: checks.length, details: checks, screenshots: [
      'output/playwright/subscription-reader-desktop.png', 'output/playwright/subscription-reader-mobile.png', 'output/playwright/subscription-reader-tablet.png',
    ] };
  } catch (error) {
    error.message += '\n最后通过：' + checks.at(-1) + '\n当前状态：' + JSON.stringify(await page.evaluate(() => ({
      kind: sourceKind, view: sourceView, rows: sourceRows.length, key: sourceReaderItem?.key,
      query: document.getElementById('source-q').value, filter: document.getElementById('source-filter').value,
    })));
    throw error;
  } finally {
    await page.unroute('**/api/intake/*/items?*', handler);
    await page.unroute('**/api/intake/rss', overviewHandler);
    page.off('pageerror', onError);
  }
}
