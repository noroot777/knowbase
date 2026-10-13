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

export async function checkWechatNotices({ evaluate, wait, check, click, screenshot }) {
  await wait(`!$('subs-wechat').hidden && !!wechatSubsChannel() && $('src-status').textContent !== '检查中…'`);
  await evaluate(`(async () => {
    invalidatePipeStatus();
    window.qaWechatNoticeState = { json:window.pipeJson, snapshot:pipeSnapshot, online:false, running:false };
    window.pipeJson = (path, ...rest) => {
      const state = window.qaWechatNoticeState;
      if (path.startsWith('/api/pipeline/status')) {
        if (path.includes('source=false')) return Promise.resolve({
          running:state.running, planned_stages:['crawl'], current_stage:'crawl',
          crawl_filter:{ channel:wechatSubsChannel(), accounts:[] },
          stages:state.running ? { crawl:{ status:'running', detail:'正在同步公众号' } } : {}, log:[] });
        return Promise.resolve({ source:{ online:state.online, channel_label:'公众号',
          message:state.online ? '' : '验证采集器连接失败', hint:'' } });
      }
      return state.json(path, ...rest);
    };
    $('subs-wechat-notices').open = false;
    await refreshPipe(); invalidatePipeStatus();
    await refreshPipeSource();
  })()`);
  const syncState = running => evaluate(`(async () => {
    window.qaWechatNoticeState.running = ${running};
    await refreshPipe(); invalidatePipeStatus();
    if (!${running}) await refreshPipeSource();
  })()`);
  const searchAccessible = () => evaluate(`(() => {
    const input = $('tgt-search'), box = input.getBoundingClientRect();
    return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === input;
  })()`);
  try {
    check('公众号连接失败保留详情入口且默认收起', await evaluate(`!$('subs-wechat-notices').hidden && !$('subs-wechat-notices').open && $('src-detail').classList.contains('error')`));
    await click('#subs-wechat-notices > summary');
    check('公众号连接失败原因可以手动展开', await evaluate(`$('subs-wechat-notices').open && $('src-detail').getClientRects().length > 0`));
    await evaluate(`(async () => { window.qaWechatNoticeState.online = true; await refreshPipeSource(); })()`);
    check('公众号连接恢复且无说明时隐藏并复位详情', await evaluate(`$('src-status').textContent === '已连接' && $('subs-wechat-notices').hidden && !$('subs-wechat-notices').open`));
    await syncState(true);
    check('公众号恢复后后台同步不继承旧展开态', await evaluate(`!$('subs-wechat-notices').hidden && !$('subs-wechat-notices').open && $('subs-wechat-progress-text').textContent === '正在同步公众号'`));
    const accessible = await searchAccessible();
    check('公众号恢复后同步不会遮挡添加搜索框', accessible);
    await screenshot('wechat-notices-recovered-1440');
    if (accessible) await click('#tgt-search');
    check('公众号同步时添加搜索框仍可鼠标聚焦', accessible && await evaluate(`document.activeElement === $('tgt-search')`));
    await evaluate(`$('subs-wechat-notices').open = false; true`);
    await click('#subs-wechat-notices > summary');
    check('公众号同步详情仍可由用户主动展开', await evaluate(`$('subs-wechat-notices').open && $('subs-wechat-progress').getClientRects().length > 0`));
    await syncState(false);
    check('公众号同步结束后隐藏提示也会清除展开态', await evaluate(`$('subs-wechat-notices').hidden && !$('subs-wechat-notices').open`));
    await syncState(true);
    check('公众号下一轮后台同步保持收起且搜索仍可用', await evaluate(`!$('subs-wechat-notices').hidden && !$('subs-wechat-notices').open`) && await searchAccessible());
    await screenshot('wechat-notices-next-sync-1440');
  } finally {
    await evaluate(`(async () => {
      invalidatePipeStatus();
      window.pipeJson = window.qaWechatNoticeState.json;
      pipeSnapshot = window.qaWechatNoticeState.snapshot;
      delete window.qaWechatNoticeState;
      $('subs-wechat-notices').open = false;
      renderPipeState(pipeSnapshot || { running:false, stages:{}, log:[] });
      await refreshPipeSource(); queuePipeRefresh();
    })()`);
  }
}
