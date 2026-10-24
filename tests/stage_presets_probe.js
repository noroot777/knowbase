async page => {
  const checks = [], errors = [];
  const base = new URL(page.url()).origin;
  page.on('pageerror', error => errors.push(error.message));
  const check = (name, passed) => {
    if (!passed) throw new Error(name);
    checks.push(name);
  };
  const modal = page.locator('#stages-modal');
  const claude = modal.getByRole('button', {name: '应用Claude 默认', exact: true});
  const gpt = modal.getByRole('button', {name: '应用GPT 默认', exact: true});
  const settings = async () => (await page.request.get(`${base}/api/stages`)).json();
  const open = async () => {
    const navigation = page.getByRole('button', {name: '打开导航', exact: true});
    if (await navigation.isVisible()) await navigation.click();
    await page.getByLabel('工作区设置', {exact: true}).click();
    await page.getByRole('button', {name: '模型配置', exact: true}).click();
    await modal.locator('.stage-preset').last().waitFor();
  };
  const settled = async () => {
    await page.waitForFunction(() => document.getElementById('stages-content').getAttribute('aria-busy') === 'false');
  };
  const persist = async (button, keyboard = false) => {
    const saved = page.waitForResponse(response => response.url().endsWith('/api/stages/preset')
      && response.request().method() === 'POST');
    if (keyboard) {
      await button.focus();
      await button.press('Enter');
    } else {
      await button.click();
    }
    const response = await saved;
    check('整组保存返回成功', response.ok());
    await settled();
    return response.json();
  };

  await page.setViewportSize({width: 1360, height: 1000});
  await page.reload();
  await open();
  check('Claude 组初始激活', await claude.getAttribute('aria-pressed') === 'true');
  const before = (await settings()).stages.find(stage => stage.key === 'agent').configured;
  const gptBody = await persist(gpt);
  check('GPT 组切换后激活', await gpt.getAttribute('aria-pressed') === 'true');
  check('Claude 组切换后取消激活', await claude.getAttribute('aria-pressed') === 'false');
  check('研究阶段完整保留', JSON.stringify(gptBody.stages.find(stage => stage.key === 'agent').configured)
    === JSON.stringify(before));
  check('其余全部阶段切到 GPT', gptBody.stages.filter(stage => stage.key !== 'agent')
    .every(stage => stage.provider === 'openai' && stage.model.startsWith('gpt-')));
  check('轻量阶段用 Spark', gptBody.stages.find(stage => stage.key === 'gate').model === 'gpt-5.3-codex-spark');
  check('主力阶段用 GPT', gptBody.stages.find(stage => stage.key === 'ask').model === 'gpt-5.5');
  check('GPT 组推荐与当前方案一致', gptBody.stages.filter(stage => stage.key !== 'agent')
    .every(stage => stage.recommend === `${stage.provider}:${stage.model}`));
  await page.waitForFunction(() => document.getElementById('ask-model').value === 'openai:gpt-5.5');
  check('问答下拉同步切换', await page.locator('#ask-model').inputValue() === 'openai:gpt-5.5');
  await page.waitForFunction(() => document.getElementById('fav-model').value === 'openai:gpt-5.5');
  check('收藏下拉同步切换', await page.locator('#fav-model').inputValue() === 'openai:gpt-5.5');
  check('说明保留研究阶段', (await page.locator('#stages-msg').textContent()).includes('研究阶段保持原配置'));
  await page.screenshot({path: 'output/playwright/model-presets-gpt.png'});

  await modal.getByRole('button', {name: '关闭', exact: true}).click();
  await page.reload();
  await open();
  check('刷新页面仍激活 GPT', await gpt.getAttribute('aria-pressed') === 'true');
  const changed = page.waitForResponse(response => response.url().endsWith('/api/stages')
    && response.request().method() === 'POST');
  await modal.locator('.stage-row select').first().selectOption('anthropic:claude-haiku-4-5');
  check('逐项微调返回成功', (await changed).ok());
  await settled();
  check('微调后不误标整组激活', await modal.locator('.stage-preset[aria-pressed="true"]').count() === 0);
  await persist(gpt, true);
  check('键盘可恢复整组方案', await gpt.getAttribute('aria-pressed') === 'true');

  let releaseSave, startedSave;
  const started = new Promise(resolve => { startedSave = resolve; });
  const hold = new Promise(resolve => { releaseSave = resolve; });
  await page.route('**/api/stages/preset', async route => {
    startedSave();
    await hold;
    await route.continue();
  });
  const delayed = page.waitForResponse(response => response.url().endsWith('/api/stages/preset'));
  await claude.click();
  await started;
  check('保存期间禁用重复操作', await gpt.isDisabled()
    && await modal.locator('.stage-row select').first().isDisabled());
  check('保存期间仍可关闭弹窗', await modal.getByRole('button', {name: '关闭', exact: true}).isEnabled());
  releaseSave();
  check('延迟保存成功', (await delayed).ok());
  await settled();
  await page.unroute('**/api/stages/preset');
  check('完成后解除禁用', await gpt.isEnabled());
  check('可一键切回 Claude', await claude.getAttribute('aria-pressed') === 'true');

  const unchanged = JSON.stringify((await settings()).stages.map(stage => stage.configured));
  await page.route('**/api/stages/preset', route => route.fulfill({
    status: 503, contentType: 'application/json', body: JSON.stringify({detail: '模拟保存失败'}),
  }));
  await gpt.click();
  await page.getByText('模拟保存失败', {exact: true}).waitFor();
  await settled();
  check('保存失败不误标 GPT 已应用', await gpt.getAttribute('aria-pressed') === 'false');
  check('保存失败不改变任何阶段', JSON.stringify((await settings()).stages.map(stage => stage.configured)) === unchanged);
  check('失败后允许重试', await gpt.isEnabled());
  await page.locator('#toast-host').getByRole('button', {name: '关闭', exact: true}).click();
  await page.getByText('模拟保存失败', {exact: true}).waitFor({state: 'detached'});
  await page.unroute('**/api/stages/preset');
  await persist(gpt);

  for (const viewport of [{width: 390, height: 844}, {width: 375, height: 667}]) {
    await page.setViewportSize(viewport);
    await page.locator('#stages-content').evaluate(element => { element.scrollTop = 0; });
    check(`手机 ${viewport.width} 预设可见`, await claude.isVisible() && await gpt.isVisible());
    check(`手机 ${viewport.width} 无横向溢出`, await modal.locator('.modal').evaluate(element =>
      element.scrollWidth <= element.clientWidth && element.getBoundingClientRect().right <= window.innerWidth));
    check(`手机 ${viewport.width} 关闭按钮在视口内`, await modal.getByRole('button', {name: '关闭', exact: true})
      .evaluate(element => element.getBoundingClientRect().bottom <= window.innerHeight));
    await modal.locator('.stage-row select').last().scrollIntoViewIfNeeded();
    check(`手机 ${viewport.width} 最后阶段可滚动到达`, await modal.locator('.stage-row select').last().isVisible());
  }
  await page.locator('#stages-content').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({path: 'output/playwright/model-presets-mobile.png'});

  await page.request.post(`${base}/api/keys`, {data: {openai_key: ' '}});
  await modal.getByRole('button', {name: '关闭', exact: true}).click();
  await page.reload();
  await open();
  check('缺少 key 时 GPT 方案不可点击', await gpt.isDisabled());
  check('禁用原因提供配置指引', (await gpt.textContent()).includes('API Key'));
  check('另一组仍可用', await claude.isEnabled());
  check('浏览器无脚本错误', errors.length === 0);
  return {total: checks.length, checks, errors};
}
