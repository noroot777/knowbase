export async function checkSourceNoticeRecovery({ evaluate, wait, check, click, screenshot, send }) {
  const originalKind = await evaluate('sourceKind');
  const recover = () => evaluate(`(async () => {
    try { await loadSourceModule(false); } finally { clearTimeout(sourcePoll); }
  })()`);
  const failOverview = message => evaluate(`(async () => {
    const originalJson = window.pipeJson;
    try {
      window.pipeJson = (path, ...rest) => path === '/api/intake/' + sourceKind
        ? Promise.reject(new Error(${JSON.stringify(message)})) : originalJson(path, ...rest);
      await loadSourceModule(false);
    } finally { window.pipeJson = originalJson; clearTimeout(sourcePoll); }
  })()`);
  const openNotices = async () => {
    if (!await evaluate(`$('source-notices').open`)) await click('#source-notices > summary');
  };
  const searchAccessible = () => evaluate(`(() => {
    const input = $('source-q'), box = input.getBoundingClientRect();
    return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === input;
  })()`);
  const pressEscape = async () => {
    await send('Input.dispatchKeyEvent', { type:'keyDown', key:'Escape', code:'Escape', windowsVirtualKeyCode:27 });
    await send('Input.dispatchKeyEvent', { type:'keyUp', key:'Escape', code:'Escape', windowsVirtualKeyCode:27 });
  };
  try {
    if (await evaluate(`$('source-notices').open`)) await click('#source-notices > summary');
    await evaluate(`readSourceItem(sourceRows[4].key, false); $('source-list-pane').scrollTop = 0; true`);
    await failOverview('迁移验证：内容读取失败');
    check('迁移后真实读取失败仍自动展开原因', await evaluate(`$('source-notices').open && !$('source-error').hidden`));
    await recover();
    check('恢复时仍有待配置提示也会收起自动浮层', await evaluate(`$('source-error').hidden && !$('source-notices').open && !$('source-notices').hidden && !$('source-configuration').hidden`));
    check('恢复后搜索框和第一条顶部均未被遮挡', await searchAccessible() && await evaluate(`(() => {
      const entry = document.querySelector('.source-entry'), box = entry.getBoundingClientRect();
      return document.elementFromPoint(box.left + box.width / 2, box.top + box.height * .15)
        ?.closest('[data-source-key]') === entry;
    })()`));
    await click('.source-entry', .15);
    check('恢复后真实点击第一条顶部可以阅读', await evaluate(`sourceReaderItem.key === sourceRows[0].key`));
    await screenshot('notice-import-recovery-1440');
    await evaluate(`$('source-progress').hidden = false; $('source-progress-text').textContent = '同步进行中';
      $('source-issues').hidden = false; sourceError('组合状态下的读取失败'); sourceError(); true`);
    check('其他同步与连接问题仍存在时只收起自动详情', await evaluate(`!$('source-notices').hidden && !$('source-notices').open && !$('source-progress').hidden && !$('source-issues').hidden`));
    check('组合状态恢复后搜索框仍可命中', await searchAccessible());
    await recover();
    await openNotices();
    await recover();
    check('用户手动查看的详情不会被成功轮询收起', await evaluate(`$('source-notices').open`));
    await click('#source-configuration');
    check('手动详情正文可点击收起继续操作', await evaluate(`!$('source-notices').open`));
    await openNotices();
    await failOverview('手动查看期间出现的读取失败');
    await recover();
    check('手动查看期间的故障恢复仍保留详情', await evaluate(`$('source-notices').open`));
    await click('.source-entry:nth-of-type(3)');
    check('外部点击一次即可关闭详情并阅读对应条目', await evaluate(`!$('source-notices').open && sourceReaderItem.key === sourceRows[2].key`));
    await failOverview('持续的读取失败');
    await click('.source-entry:nth-of-type(3)');
    await failOverview('持续的读取失败');
    check('关闭自动详情后相同错误重试仍尊重收起选择', await evaluate(`!$('source-notices').open`));
    await recover();
    await failOverview('新的读取失败');
    await evaluate(`$('source-error').querySelector('button').focus(); true`);
    await pressEscape();
    check('Escape 收起详情并把内部焦点返回入口', await evaluate(`!$('source-notices').open && document.activeElement === $('source-notices').querySelector('summary')`));
    await recover();
    await openNotices();
    await evaluate(`openSchedModal(); true`);
    await wait(`overlayTop?.id === 'sched-modal' && !schedLoading`);
    await pressEscape();
    await wait(`$('sched-modal').style.display === 'none'`);
    check('同步弹窗的 Escape 优先级不被迁移逻辑破坏', await evaluate(`$('source-notices').open`));
    await click('#source-notices > summary');
    await click('#source-reader-expand');
    await failOverview('专注阅读期间的读取失败');
    check('专注阅读保留可见的状态入口及自动故障原因', await evaluate(`$('source-workbench').classList.contains('is-focused') && $('source-notices').getClientRects().length > 0 && $('source-notices').open && $('source-error').getClientRects().length > 0`));
    await screenshot('notice-import-focused-1440');
    await click('#source-error button');
    await wait(`$('source-error').hidden && !$('source-notices').open`);
    await evaluate(`clearTimeout(sourcePoll); true`);
    check('专注阅读真实点击重试恢复后保留状态入口和正文', await evaluate(`$('source-workbench').classList.contains('is-focused') && $('source-notices').getClientRects().length > 0 && $('source-reader').getClientRects().length > 0`));
    await openNotices();
    await pressEscape();
    check('专注阅读详情同样可用 Escape 收起', await evaluate(`!$('source-notices').open && $('source-notices').getClientRects().length > 0`));
    await click('#source-reader-expand');
    check('提示恢复逻辑不影响返回双栏阅读', await evaluate(`!$('source-workbench').classList.contains('is-focused') && $('source-list-pane').getClientRects().length > 0`));
    await evaluate(`sourceError('相同的来源连接错误');
      window.qaSourceRecoveryJson = window.pipeJson;
      window.pipeJson = (path, ...rest) => path === '/api/intake/rss'
        ? Promise.reject(new Error('相同的来源连接错误')) : window.qaSourceRecoveryJson(path, ...rest); true`);
    await click('#subs-switch-rss');
    await wait(`sourceKind === 'rss' && !$('source-error').hidden && $('source-error-text').textContent === '相同的来源连接错误'`);
    check('新来源首次失败即使文案相同也会独立提醒', await evaluate(`$('source-notices').open`));
  } finally {
    await evaluate(`if (window.qaSourceRecoveryJson) {
      window.pipeJson = window.qaSourceRecoveryJson; delete window.qaSourceRecoveryJson;
    } true`);
    if (await evaluate(`sourceKind !== ${JSON.stringify(originalKind)}`)) {
      await evaluate(`openSubsPanel(${JSON.stringify(originalKind)}); true`);
      await wait(`sourceKind === ${JSON.stringify(originalKind)} && sourceData && sourceRows.length >= 30`);
    }
    await recover();
    if (await evaluate(`$('source-notices').open`)) await click('#source-notices > summary');
  }
}
