// Run with Playwright CLI in an isolated headless session against tests/serve_sources.py.
// No production data or remote social/RSS feeds are used.
async page => {
  const checks = [], errors = [], held = [];
  const check = (name, ok) => { if (!ok) throw new Error(name); checks.push(name); };
  const onError = e => errors.push(e.message);
  page.on('pageerror', onError);
  let mode = 'pass', hits = 0;
  const item = {key:'test', title:'Fresh R&D a < b', author:'测试作者',
    body:'R&D，a < b，Rust & Go\n<img src=x onerror="window.__feedXSS=1">',
    url:'https://example.org/post', published:'2026-09-28 09:30', content_kind:'content'};
  const data = value => ({items:[value], total:1});
  const reply = (route, value, status=200) => route.fulfill({status, contentType:'application/json', body:JSON.stringify(value)});
  const handler = async route => {
    if (!route.request().url().includes('/rss/items?')) return route.continue();
    hits++;
    if (mode === 'fail') { mode = 'pass'; return reply(route, {detail:'测试：内容服务暂时不可用'},503); }
    if (mode === 'hold') { held.push(route); return; }
    if (mode === 'fresh') return reply(route, data(item));
    return route.continue();
  };
  await page.route('**/api/intake/*/items?*', handler);
  try {
    await page.setViewportSize({width:1440,height:1050});
    // 三类来源并进了「我的订阅」页:先定好要看的视图,再走真实点击进入
    await page.evaluate(() => localStorage.setItem('kb-subs-kind', 'social'));
    await page.locator('#tab-subs').click();
    await page.locator('#subs-switch-social').waitFor();
    await page.waitForFunction(() => sourceData?.unconfigured === 51);
    check('默认 51 个账号明确待配置，不可启动空跑', await page.locator('#source-sync').isDisabled()
      && (await page.locator('#source-configuration').textContent()).includes('51 个账号待配置'));
    await page.locator('#source-view-manage').click();
    check('管理列表逐条显示待配置', await page.locator('.source-state').filter({hasText:'待配置'}).count() === 51);
    await page.screenshot({path:'output/playwright/review-social-configuration.png'});
    await page.locator('#source-bridge-field summary').click();
    await page.locator('#source-bridges').fill('https://example.org/{handle}/rss');
    await page.locator('#source-settings-save').click();
    await page.waitForFunction(() => sourceData?.ready === 51);
    check('保存桥接后无需刷新即可同步', await page.locator('#source-sync').isEnabled()
      && await page.locator('#source-configuration').isHidden());
    await page.locator('#source-bridges').fill('');
    await page.locator('#source-settings-save').click();
    await page.waitForFunction(() => sourceData?.unconfigured === 51);

    // No metadata changes and no manual reload between the failure and recovery.
    mode = 'fail'; hits = 0;
    await page.locator('#subs-switch-rss').click();
    await page.locator('[data-retry-items]').waitFor();
    check('内容失败显示错误与重新读取', (await page.locator('#source-list').textContent()).includes('内容服务暂时不可用'));
    await page.screenshot({path:'output/playwright/review-items-retry.png'});
    await page.waitForFunction(() => !sourceItemsFailed && !document.querySelector('[data-retry-items]'), null, {timeout:20000});
    check('概览不变时 15 秒轮询自动恢复内容', hits >= 2
      && !(await page.locator('#source-list').textContent()).includes('正在读取内容'));

    mode = 'fail';
    await page.locator('#source-view-items').click();
    await page.locator('[data-retry-items]').waitFor();
    await page.locator('[data-retry-items]').click();
    await page.waitForFunction(() => !sourceItemsFailed);
    check('手动重新读取可恢复', await page.locator('[data-retry-items]').count() === 0);

    // Responses from an old view, module or filter must not replace the current UI.
    mode = 'hold';
    await page.locator('#source-view-items').click();
    while (!held.length) await page.waitForTimeout(20);
    await page.locator('#source-view-manage').click();
    await reply(held.shift(), data({...item,title:'过期内容'}));
    await page.waitForTimeout(80);
    check('旧内容响应不覆盖订阅管理', await page.locator('.source-subscription').count() === 9);

    await page.locator('#source-view-items').click();
    while (!held.length) await page.waitForTimeout(20);
    await page.locator('#subs-switch-social').click();
    await page.waitForFunction(() => sourceKind === 'social' && sourceData?.sources.length === 51);
    await reply(held.shift(), {detail:'过期 RSS 错误'},503);
    await page.waitForTimeout(80);
    check('切模块后旧错误不污染新模块', !(await page.locator('#subspanel').textContent()).includes('过期 RSS 错误'));

    await page.locator('#subs-switch-rss').click();
    while (!held.length) await page.waitForTimeout(20);
    mode = 'fresh';
    await page.locator('#source-q').fill('Fresh');
    await page.locator('.source-entry-title').waitFor();
    await reply(held.shift(), data({...item,title:'过期关键词结果'}));
    await page.waitForTimeout(80);
    check('筛选改变后丢弃旧响应', (await page.locator('.source-entry-title').textContent()) === item.title);
    check('正文卡片按原文显示符号且不执行标签', await page.locator('#source-list img').count() === 0
      && (await page.locator('.source-excerpt').textContent()).includes('Rust & Go'));
    await page.locator('.source-entry-title').click();
    check('阅读弹层用纯文本保留原文', (await page.locator('#source-reader-body').textContent()) === item.body
      && await page.locator('#source-reader-body img').count() === 0);
    check('Markdown 渲染层继续过滤执行属性', await page.evaluate(() => {
      const node = document.createElement('div');
      node.innerHTML = mdHtml('<img src=x onerror="window.__feedXSS=1"><script>window.__feedXSS=1</script>');
      return !node.querySelector('script, [onerror]') && !window.__feedXSS;
    }));
    await page.screenshot({path:'output/playwright/review-plaintext-reader.png'});
    await page.keyboard.press('Escape');
    mode = 'pass';
    await page.locator('#source-q').fill('');
    await page.locator('#source-add').click();
    await page.locator('#source-edit-name').fill('审查回归 RSS');
    await page.locator('#source-edit-url').fill('https://example.org/review-regression.xml');
    await page.locator('#source-edit-save').click();
    const added = page.locator('.source-subscription').filter({hasText:'审查回归 RSS'});
    await added.waitFor();
    await added.locator('[data-source-action="sync"]').click();
    await page.waitForFunction(async () => {
      const state = await (await fetch('/api/pipeline/status?source=false')).json();
      return !state.running && state.stages?.index?.status === 'ok';
    });
    await page.locator('#source-view-items').click();
    await page.getByRole('button', {name:'验证样例：Agent 知识工作流'}).waitFor();
    check('单源同步贯通采集、Markdown 和索引', (await page.locator('#source-list').textContent()).includes('notebook'));
    const found = await page.evaluate(async () =>
      (await fetch('/api/search?q=notebook&source=rss&vector=false')).json());
    check('新增内容可在 RSS 来源下全文检索', JSON.stringify(found).includes('source/rss/')
      && JSON.stringify(found).includes('notebook'));
    check('无未捕获页面异常', errors.length === 0);
    await page.evaluate(result => { window.__sourcesReviewResult = result; }, {checks, errors});
    return {checks, errors};
  } finally {
    for (const route of held) await route.abort();
    await page.unroute('**/api/intake/*/items?*', handler);
    page.off('pageerror', onError);
  }
}
