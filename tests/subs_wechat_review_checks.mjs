async function dismissProbeToasts({ evaluate, wait }) {
  await evaluate(`document.querySelectorAll('.toast-x').forEach(button => button.click()); true`);
  await wait(`!document.querySelector('.toast-x')`);
}

export async function checkWechatCollectorNotices({ evaluate, wait, check, click, screenshot, send }) {
  const refreshSource = source => evaluate(`(async () => {
    window.qaWechatReviewState.source = ${JSON.stringify(source)};
    await refreshPipeSource();
  })()`);
  const setRunning = running => evaluate(`(async () => {
    window.qaWechatReviewState.running = ${running};
    await refreshPipe(); invalidatePipeStatus();
  })()`);
  const closeNotices = async () => {
    if (await evaluate(`$('subs-wechat-notices').open`)) await click('#subs-wechat-notices > summary');
  };
  const openNotices = async () => {
    if (!await evaluate(`$('subs-wechat-notices').open`)) await click('#subs-wechat-notices > summary');
  };
  const connected = { online:true, channel_label:'公众号', message:'本机库只读到 3 个会话', hint:'请检查本机登录状态后继续同步。' };
  const offline = { online:false, channel_label:'公众号', message:'采集器连接失败', hint:'请重新检查连接。' };
  const silent = { online:true, channel_label:'公众号', message:'', hint:'' };
  await evaluate(`invalidatePipeStatus();
    window.qaWechatReviewState = { json:window.pipeJson, snapshot:pipeSnapshot, running:false, source:{} };
    window.pipeJson = (path, ...rest) => {
      const state = window.qaWechatReviewState;
      if (path.startsWith('/api/pipeline/status')) {
        if (path.includes('source=false')) return Promise.resolve({
          running:state.running, planned_stages:['crawl'], current_stage:'crawl',
          crawl_filter:{ channel:wechatSubsChannel(), accounts:[] },
          stages:state.running ? { crawl:{ status:'running', detail:'正在同步公众号' } } : {}, log:[] });
        return Promise.resolve({ source:state.source });
      }
      return state.json(path, ...rest);
    }; true`);
  try {
    await setRunning(false);
    await refreshSource(silent);
    await dismissProbeToasts({ evaluate, wait });
    await refreshSource(connected);
    check('公众号接通后采集器说明首次自动可见且保留完整提示', await evaluate(`$('src-status').textContent === '已连接' && $('subs-wechat-notices').open && !$('subs-wechat-notices').classList.contains('has-error') && $('src-detail').getClientRects().length > 0 && $('src-detail').textContent === ${JSON.stringify(connected.message + ' · ' + connected.hint)}`));
    check('公众号收起摘要不再只写连接详情而是显示采集器原话', await evaluate(`$('subs-wechat-notices-summary').textContent.includes('本机库只读到 3 个会话')`));
    await screenshot('review-wechat-connected-1440');
    await closeNotices();
    await refreshSource(connected);
    check('公众号手动收起后相同采集器说明不会反复展开', await evaluate(`!$('subs-wechat-notices').open && $('subs-wechat-notices-summary').textContent.includes('本机库只读到 3 个会话')`));
    await refreshSource({ ...connected, hint:'发现未匹配会话，请检查后重试。' });
    check('公众号采集器提示发生变化时重新提醒一次', await evaluate(`$('subs-wechat-notices').open && $('src-detail').textContent.includes('未匹配会话')`));
    await setRunning(true);
    await evaluate(`$('subs-wechat-progress').querySelector('button').focus(); true`);
    await send('Input.dispatchKeyEvent', { type:'keyDown', key:'Escape', code:'Escape', windowsVirtualKeyCode:27 });
    await send('Input.dispatchKeyEvent', { type:'keyUp', key:'Escape', code:'Escape', windowsVirtualKeyCode:27 });
    check('公众号新说明可用 Escape 收起并保留入口焦点', await evaluate(`!$('subs-wechat-notices').open && document.activeElement === $('subs-wechat-notices').querySelector('summary')`));
    await refreshSource(offline);
    check('公众号新的连接失败自动展示原因', await evaluate(`$('subs-wechat-notices').open && $('subs-wechat-notices').classList.contains('has-error') && $('src-detail').getClientRects().length > 0`));
    await closeNotices();
    await refreshSource(offline);
    check('公众号相同连接失败尊重用户收起状态', await evaluate(`!$('subs-wechat-notices').open`));
    await setRunning(true);
    check('公众号同步期间连接失败仍优先显示原始原因', await evaluate(`$('subs-wechat-notices-summary').textContent.includes('采集器连接失败') && $('subs-wechat-notices').classList.contains('has-error') && !$('subs-wechat-progress').hidden`));
    await refreshSource({ ...offline, message:'采集器暂时无法读取本机会话' });
    check('公众号同步中变化的连接错误仍会自动展开', await evaluate(`$('subs-wechat-notices').open`));
    await refreshSource(silent);
    check('公众号连接恢复但同步仍在进行时收起自动详情', await evaluate(`$('src-status').textContent === '已连接' && !$('subs-wechat-notices').hidden && !$('subs-wechat-notices').open && $('subs-wechat-notices-summary').textContent.includes('正在同步公众号')`));
    await openNotices();
    await refreshSource(silent);
    check('公众号手动查看同步详情不被正常连接轮询收起', await evaluate(`$('subs-wechat-notices').open`));
    await refreshSource(connected);
    await refreshSource(silent);
    check('公众号手动查看期间提示恢复仍保留同步详情', await evaluate(`$('subs-wechat-notices').open`));
    const search = await evaluate(`(() => {
      const input = $('tgt-search'), box = input.getBoundingClientRect();
      const point = { x:box.right - 6, y:box.top + box.height / 2 };
      return { point, accessible:document.elementFromPoint(point.x, point.y) === input };
    })()`);
    await send('Input.dispatchMouseEvent', { type:'mousePressed', ...search.point, button:'left', clickCount:1 });
    await send('Input.dispatchMouseEvent', { type:'mouseReleased', ...search.point, button:'left', clickCount:1 });
    check('公众号外部点击一次即可收起详情并聚焦添加搜索', search.accessible && await evaluate(`!$('subs-wechat-notices').open && document.activeElement === $('tgt-search')`));
    await setRunning(false);
    check('公众号没有采集器说明及同步时隐藏并复位入口', await evaluate(`$('subs-wechat-notices').hidden && !$('subs-wechat-notices').open`));
    await setRunning(true);
    check('公众号下一轮普通同步不继承采集器提示的展开态', await evaluate(`!$('subs-wechat-notices').hidden && !$('subs-wechat-notices').open`));
  } finally {
    await evaluate(`(async () => {
      invalidatePipeStatus();
      window.pipeJson = window.qaWechatReviewState.json;
      pipeSnapshot = window.qaWechatReviewState.snapshot;
      delete window.qaWechatReviewState;
      renderPipeState(pipeSnapshot || { running:false, stages:{}, log:[] });
      await refreshPipeSource(); queuePipeRefresh();
    })()`);
    await closeNotices();
  }
}

export async function checkWechatTargetSizing({ evaluate, wait, check, viewport, screenshot }) {
  const populate = count => evaluate(`tgtCands = []; renderTgtCands();
    tgtList = Array.from({ length:${count} }, (_, index) => ({
      name:'布局验证公众号 ' + String(index + 1).padStart(2, '0'), enabled:true, ready:true,
      state:'可采集', detail:'已确认本机会话'
    }));
    renderTargets(); $('tgt-list').scrollTop = 0; true`);
  const measure = () => evaluate(`(() => {
    const list = $('tgt-list'), box = list.getBoundingClientRect();
    const entries = [...list.children], first = entries[0].getBoundingClientRect(), last = entries.at(-1).getBoundingClientRect();
    const hint = document.querySelector('#subs-wechat-manage > .hint').getBoundingClientRect();
    return { height:box.height, content:last.bottom - first.top, scrollHeight:list.scrollHeight,
      clientHeight:list.clientHeight, hintGap:hint.top - box.bottom, hintBottom:hint.bottom,
      viewport:innerHeight, page:document.documentElement.scrollHeight,
      horizontal:document.documentElement.scrollWidth > innerWidth };
  })()`);
  await evaluate(`window.qaWechatTargetSizing = { list:tgtList, cands:tgtCands, label:tgtLabel }; true`);
  try {
    await dismissProbeToasts({ evaluate, wait });
    await viewport(1440, 1000);
    for (const count of [0, 3, 6]) {
      await populate(count);
      const dimensions = await measure();
      check(`${count} 个公众号的名单边框按内容收缩而非填满空白`, Math.abs(dimensions.height - dimensions.content - 2) < 3, JSON.stringify(dimensions));
      check(`${count} 个公众号的说明紧接名单且工作区不整页滚动`, dimensions.hintGap >= 0 && dimensions.hintGap <= 14 && dimensions.page <= dimensions.viewport + 1, JSON.stringify(dimensions));
      if (count) await screenshot(`review-wechat-${count}-targets-1440`);
    }
    await populate(30);
    const desktop = await measure();
    check('30 个公众号超出高度时名单内部滚动且说明仍可见', desktop.scrollHeight > desktop.clientHeight + 100 && desktop.hintBottom <= desktop.viewport && desktop.page <= desktop.viewport + 1, JSON.stringify(desktop));
    await evaluate(`$('tgt-list').scrollTop = $('tgt-list').scrollHeight; true`);
    check('长公众号名单可以滚动到最后一条操作按钮', await evaluate(`(() => {
      const button = document.querySelector('#tgt-list .tgt-row:last-child button'), box = button.getBoundingClientRect();
      return $('tgt-list').scrollTop > 0 && document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === button;
    })()`));
    await screenshot('review-wechat-30-targets-1440');
    await viewport(375, 760);
    await populate(3);
    const mobileShort = await measure();
    check('手机少量公众号同样按内容收缩且无横向溢出', Math.abs(mobileShort.height - mobileShort.content - 2) < 3 && !mobileShort.horizontal, JSON.stringify(mobileShort));
    await screenshot('review-wechat-3-targets-375');
    await populate(30);
    const mobileLong = await measure();
    check('手机长公众号名单内部滚动且页尾说明保留', mobileLong.scrollHeight > mobileLong.clientHeight && mobileLong.hintBottom <= mobileLong.viewport && mobileLong.page <= mobileLong.viewport + 1 && !mobileLong.horizontal, JSON.stringify(mobileLong));
    await screenshot('review-wechat-30-targets-375');
  } finally {
    await viewport(1440, 1000);
    await evaluate(`tgtList = window.qaWechatTargetSizing.list; tgtCands = window.qaWechatTargetSizing.cands;
      tgtLabel = window.qaWechatTargetSizing.label; delete window.qaWechatTargetSizing;
      renderTgtCands(); renderTargets(); true`);
  }
}
