export async function checkSubscriptionNotices({ evaluate, check, click, screenshot }) {
  const visibility = await evaluate(`['source-progress', 'source-issues', 'source-configuration']
    .map(id => ({ id, hidden:$(id).hidden }))`);
  const progressText = await evaluate(`$('source-progress-text').textContent`);
  const failOverview = message => evaluate(`(async () => {
    const originalJson = window.pipeJson;
    try {
      window.pipeJson = (path, ...rest) => path === '/api/intake/' + sourceKind
        ? Promise.reject(new Error(${JSON.stringify(message)})) : originalJson(path, ...rest);
      await loadSourceModule(false);
    } finally {
      window.pipeJson = originalJson;
      clearTimeout(sourcePoll);
    }
  })()`);
  const searchAccessible = () => evaluate(`(() => {
    const input = $('source-q'), box = input.getBoundingClientRect();
    return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === input;
  })()`);
  try {
    await evaluate(`sourceError();
      for (const id of ['source-progress', 'source-issues', 'source-configuration']) $(id).hidden = true;
      updateSourceNotices(); true`);
    check('所有来源状态清空时详情隐藏且收起', await evaluate(`$('source-notices').hidden && !$('source-notices').open`));
    await failOverview('验证读取失败：本地服务暂不可用');
    check('首次轮询失败自动展开错误原因', await evaluate(`!$('source-notices').hidden && $('source-notices').open && $('source-error-text').textContent.includes('本地服务暂不可用')`));
    await click('#source-notices > summary');
    await failOverview('验证读取失败：本地服务暂不可用');
    check('相同错误再次轮询尊重用户收起状态', await evaluate(`!$('source-notices').hidden && !$('source-notices').open && !$('source-error').hidden`));
    check('相同错误重复轮询不遮挡搜索框', await searchAccessible());
    await click('#source-q');
    check('错误详情收起后搜索仍可鼠标聚焦', await evaluate(`document.activeElement === $('source-q')`));
    await screenshot('source-notices-repeated-error-1440');
    await failOverview('验证读取失败：连接超时');
    check('错误文案变化时重新展开新的原因', await evaluate(`$('source-notices').open && $('source-error-text').textContent.includes('连接超时')`));
    await failOverview('验证读取失败：连接超时');
    check('相同错误重复轮询不关闭已展开的详情', await evaluate(`$('source-notices').open`));
    await evaluate(`sourceError(); true`);
    check('故障恢复且没有其他状态时清除残留展开态', await evaluate(`$('source-notices').hidden && !$('source-notices').open`));
    await evaluate(`$('source-configuration').hidden = false; updateSourceNotices(); true`);
    check('故障恢复后待配置提示重新出现仍默认收起', await evaluate(`!$('source-notices').hidden && !$('source-notices').open`));
    check('恢复后的待配置提示不遮挡搜索框', await searchAccessible());
    await evaluate(`$('source-notices').open = false; true`);
    await click('#source-notices > summary');
    check('待配置详情仍能由用户手动展开', await evaluate(`$('source-notices').open && $('source-configuration').getClientRects().length > 0`));
    await evaluate(`$('source-configuration').hidden = true; updateSourceNotices(); true`);
    check('手动展开后状态全部清空也会复位详情', await evaluate(`$('source-notices').hidden && !$('source-notices').open`));
    await evaluate(`$('source-progress').hidden = false;
      $('source-progress-text').textContent = '验证同步中'; updateSourceNotices(); true`);
    check('后续同步提示不会继承旧的展开态', await evaluate(`!$('source-notices').hidden && !$('source-notices').open && $('source-notices-summary').textContent === '验证同步中'`));
    check('故障恢复后开始同步仍可操作搜索', await searchAccessible());
    await screenshot('source-notices-recovered-1440');
    await evaluate(`$('source-progress').hidden = true; updateSourceNotices(); true`);
    await failOverview('验证读取失败：连接超时');
    check('同类错误在恢复后再次发生仍会提醒用户', await evaluate(`$('source-notices').open && !$('source-error').hidden`));
  } finally {
    await evaluate(`sourceError();
      for (const state of ${JSON.stringify(visibility)}) $(state.id).hidden = state.hidden;
      $('source-progress-text').textContent = ${JSON.stringify(progressText)};
      $('source-notices').open = false; updateSourceNotices(); true`);
  }
}
