async page => {
  const checks = [], errors = [];
  const base = new URL(page.url()).origin;
  await page.reload();
  page.on('pageerror', error => errors.push(error.message));
  const check = (name, passed) => {
    if (!passed) throw new Error(name);
    checks.push(name);
  };
  const keys = async () => (await (await page.request.get(`${base}/api/keys`)).json()).keys;
  const toggle = page.getByRole('switch', {name: 'Responses → Chat Completions', exact: true});
  const modal = page.locator('#keys-modal');
  const save = modal.getByRole('button', {name: '保存', exact: true});
  const open = async () => {
    await page.locator('.workspace-settings > summary').click();
    await page.locator('#keys-btn').click();
    await page.waitForFunction(() => !document.getElementById('openai-protocol-conversion').disabled);
  };
  const persist = async enabled => {
    await toggle.setChecked(enabled);
    const saved = page.waitForResponse(response => response.url().endsWith('/api/keys')
      && response.request().method() === 'POST');
    await save.click();
    check('保存协议开关成功', (await saved).ok());
    await page.waitForFunction(value => document.getElementById('openai-protocol-conversion').checked === value,
      enabled);
    check(`后端保存 ${enabled}`, (await keys()).openai.protocol_conversion_enabled === enabled);
    check('Key 不回填到输入框', await page.locator('#key-openai').inputValue() === '');
  };

  await page.setViewportSize({width: 1360, height: 1000});
  await open();
  check('旧配置默认开启转换', await toggle.isChecked());
  check('转换说明可见', await page.locator('#openai-conversion-help').isVisible());
  check('Anthropic 不展示转换字段', !(await keys()).anthropic.hasOwnProperty('protocol_conversion_enabled'));
  await persist(false);
  check('关闭后显示原生 Responses', await page.locator('#openai-conversion-state').innerText() === '使用原生 Responses');
  await page.screenshot({path: 'output/playwright/openai-native-responses.png'});
  await modal.getByRole('button', {name: '关闭', exact: true}).click();
  await page.reload();
  await open();
  check('刷新页面保留关闭状态', !(await toggle.isChecked()));

  await toggle.setChecked(true);
  const modelsLoaded = page.waitForResponse(response => response.url().includes('/api/models/remote'));
  await modal.getByRole('button', {name: '拉取可用模型', exact: true}).last().click();
  check('拉取模型成功', (await modelsLoaded).ok());
  await page.locator('#models-openai-box').getByText('test-model').waitFor();
  check('拉取模型时同步保存转换开关', (await keys()).openai.protocol_conversion_enabled === true);
  await persist(true);
  await modal.getByRole('button', {name: '关闭', exact: true}).click();
  await page.reload();
  await open();
  check('刷新页面保留开启状态', await toggle.isChecked());
  await page.screenshot({path: 'output/playwright/openai-chat-conversion.png'});

  await page.request.post(`${base}/api/keys`, {data: {openai_key: ' ', openai_base_url: ' '}});
  await modal.getByRole('button', {name: '关闭', exact: true}).click();
  await page.reload();
  await open();
  check('未配置凭证也能单独修改协议', await toggle.isEnabled());
  await persist(false);
  check('保存协议不会生成 Key', !(await keys()).openai.set);

  await page.setViewportSize({width: 390, height: 844});
  await toggle.scrollIntoViewIfNeeded();
  check('移动端开关可见', await toggle.isVisible());
  check('移动端面板无横向溢出', await page.locator('.key-conversion').evaluate(element =>
    element.scrollWidth <= element.clientWidth && element.getBoundingClientRect().right <= window.innerWidth));
  await toggle.press('Space');
  check('空格键可操作协议开关', await toggle.isChecked());
  await page.screenshot({path: 'output/playwright/openai-conversion-mobile.png'});
  await page.setViewportSize({width: 1360, height: 1000});
  check('浏览器无脚本错误', errors.length === 0);
  return {checks, errors, total: checks.length};
}
