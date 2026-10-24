async page => {
  const checks = [], errors = [];
  const base = new URL(page.url()).origin;
  const modal = page.locator('#stages-modal');
  const check = (name, passed) => {
    if (!passed) throw new Error(name);
    checks.push(name);
  };
  page.on('pageerror', error => errors.push(error.message));
  const settings = async () => (await page.request.get(`${base}/api/stages`)).json();
  const group = key => modal.locator('.stage-preset-item').filter({
    has: page.locator(`.stage-preset[data-preset="${key}"]`),
  });
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
  const rename = async (key, name, keyboard = false) => {
    await group(key).getByRole('button', {name: '修改组名', exact: true}).click();
    const input = group(key).getByRole('textbox', {name: '组名', exact: true});
    check('编辑时输入框自动聚焦', await input.evaluate(element => element === document.activeElement));
    await input.fill(name);
    const response = page.waitForResponse(result => result.url().endsWith('/api/stages/preset/name'));
    if (keyboard) await input.press('Enter');
    else await group(key).getByRole('button', {name: '保存组名', exact: true}).click();
    check('改名保存成功', (await response).ok());
    await settled();
    check('卡片显示新组名', await group(key).getByRole('button', {name: `应用${name.trim()}`, exact: true}).count() === 1);
  };

  await page.setViewportSize({width: 1360, height: 1000});
  await page.reload();
  await open();
  const before = JSON.stringify((await settings()).stages.map(stage => stage.configured));
  await rename('claude', 'Claude 研究专用', true);
  await rename('gpt', '  GPT 日常工作  ');
  check('改名不改变模型分配', JSON.stringify((await settings()).stages.map(stage => stage.configured)) === before);
  check('两个组名独立保存', (await settings()).presets.every(preset =>
    preset.label === (preset.key === 'claude' ? 'Claude 研究专用' : 'GPT 日常工作')));
  await modal.getByRole('button', {name: '关闭', exact: true}).click();
  await page.reload();
  await open();
  check('刷新后保留组名', await group('gpt').getByRole('button', {name: '应用GPT 日常工作', exact: true}).count() === 1);

  await group('claude').getByRole('button', {name: '修改组名', exact: true}).click();
  await group('claude').getByRole('textbox', {name: '组名', exact: true}).fill('不保存这个名字');
  await group('claude').getByRole('button', {name: '取消', exact: true}).click();
  check('取消不改变组名', (await settings()).presets.find(preset => preset.key === 'claude').label === 'Claude 研究专用');
  await group('claude').getByRole('button', {name: '修改组名', exact: true}).click();
  const invalid = group('claude').getByRole('textbox', {name: '组名', exact: true});
  await invalid.fill('   ');
  const failed = page.waitForResponse(response => response.url().endsWith('/api/stages/preset/name'));
  await group('claude').getByRole('button', {name: '保存组名', exact: true}).click();
  check('空白组名拒绝保存', (await failed).status() === 400);
  await page.getByText('组名需为 1–40 个字符', {exact: true}).waitFor();
  await settled();
  check('保存失败保留输入供修改', await invalid.inputValue() === '   ' && await invalid.isEnabled());
  await page.locator('#toast-host').getByRole('button', {name: '关闭', exact: true}).click();
  await page.getByText('组名需为 1–40 个字符', {exact: true}).waitFor({state: 'detached'});
  await invalid.fill('Claude 研究专用');
  const retry = page.waitForResponse(response => response.url().endsWith('/api/stages/preset/name'));
  await invalid.press('Enter');
  check('修改后可重试保存', (await retry).ok());
  await settled();

  await page.request.post(`${base}/api/keys`, {data: {openai_key: 'sk-fake-browser-qa'}});
  await modal.getByRole('button', {name: '关闭', exact: true}).click();
  await page.reload();
  await open();
  const applied = page.waitForResponse(response => response.url().endsWith('/api/stages/preset'));
  await group('gpt').getByRole('button', {name: '应用GPT 日常工作', exact: true}).click();
  const appliedResponse = await applied;
  check('改名后仍可切换原模型组', appliedResponse.ok() && (await appliedResponse.json()).applied_preset === 'GPT 日常工作');
  await settled();
  check('成功提示使用自定义名称', (await page.locator('#stages-msg').textContent()).includes('GPT 日常工作'));
  await rename('claude', '方案 "A" <轻量>');
  check('特殊字符按文字显示', await group('claude').locator('img, script, 轻量').count() === 0);
  await rename('claude', 'Claude 研究专用');
  await page.screenshot({path: 'output/playwright/model-preset-names-desktop.png'});

  await page.setViewportSize({width: 375, height: 667});
  await group('gpt').getByRole('button', {name: '修改组名', exact: true}).click();
  check('手机上编辑入口可用', await group('gpt').getByRole('textbox', {name: '组名', exact: true}).isVisible());
  check('手机编辑表单不横向溢出', await modal.locator('.modal').evaluate(element => element.scrollWidth <= element.clientWidth));
  await page.locator('#stages-content').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({path: 'output/playwright/model-preset-names-mobile.png'});
  await group('gpt').getByRole('button', {name: '取消', exact: true}).click();
  await page.request.post(`${base}/api/keys`, {data: {openai_key: ' '}});
  await modal.getByRole('button', {name: '关闭', exact: true}).click();
  await page.reload();
  await open();
  check('缺 key 的组仍能改名', await group('gpt').locator('.stage-preset').isDisabled()
    && await group('gpt').getByRole('button', {name: '修改组名', exact: true}).isEnabled());
  await rename('gpt', 'GPT 待配置');
  check('浏览器无脚本错误', errors.length === 0);
  return {total: checks.length, checks, errors};
}
