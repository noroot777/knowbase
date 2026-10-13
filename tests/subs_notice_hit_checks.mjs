async function checkNoticeCenterClicks({ label, noticesId, inputId, evaluate, check, click, screenshot, send }) {
  const originalValue = await evaluate(`$(${JSON.stringify(inputId)}).value`);
  const geometry = () => evaluate(`(() => {
    const heading = $(${JSON.stringify(noticesId)}).closest('.source-heading').getBoundingClientRect();
    const content = $('${inputId === 'source-q' ? 'source-workbench' : 'subs-wechat-manage'}').getBoundingClientRect();
    return { heading:heading.height, top:content.top, height:content.height };
  })()`);
  const before = await geometry();
  try {
    await screenshot(`notice-center-${inputId}-before`);
    await click('#' + inputId);
    check(`${label}输入框中心首点击收起提示并直接聚焦`, await evaluate(`!$(${JSON.stringify(noticesId)}).open && document.activeElement === $(${JSON.stringify(inputId)})`));
    check(`${label}首点击后输入框中心可以真实命中`, await evaluate(`(() => {
      const input = $(${JSON.stringify(inputId)}), box = input.getBoundingClientRect();
      return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === input;
    })()`));
    await send('Input.insertText', { text:'科技' });
    check(`${label}首点击后无需再次点击即可真实输入`, await evaluate(`document.activeElement === $(${JSON.stringify(inputId)}) && $(${JSON.stringify(inputId)}).value === ${JSON.stringify(originalValue + '科技')}`));
    const after = await geometry();
    check(`${label}收起自动提示不挤压工作区或改变页头`, Object.keys(before).every(key => Math.abs(before[key] - after[key]) < 1), JSON.stringify({ before, after }));
    await screenshot(`notice-center-${inputId}-typed`);
  } finally {
    await evaluate(`${inputId === 'source-q' ? 'clearTimeout(sourceSearchTimer);' : ''}
      $(${JSON.stringify(inputId)}).value = ${JSON.stringify(originalValue)}; true`);
  }
}

export async function checkFirstWechatNotice({ evaluate, wait, check, click, screenshot, send }) {
  await wait(`!$('subs-wechat').hidden && !!wechatSubsChannel() && $('src-status').textContent !== '检查中…'`);
  await checkNoticeCenterClicks({ label:'公众号首访', noticesId:'subs-wechat-notices', inputId:'tgt-search',
    evaluate, check, click, screenshot, send });
}

export async function checkAutomaticSourceNotice({ evaluate, wait, check, click, screenshot, send }) {
  const originalKind = await evaluate('sourceKind');
  const fail = message => evaluate(`(async () => {
    window.qaNoticeHitMessage = ${JSON.stringify(message)};
    await loadSourceModule(false); clearTimeout(sourcePoll);
  })()`);
  await evaluate(`openSubsPanel('rss'); true`);
  await wait(`sourceKind === 'rss' && sourceData && sourceRows.length > 0`);
  await evaluate(`window.qaNoticeHitJson = window.pipeJson;
    window.pipeJson = (path, ...rest) => path === '/api/intake/rss'
      ? Promise.reject(new Error(window.qaNoticeHitMessage)) : window.qaNoticeHitJson(path, ...rest); true`);
  try {
    await fail('RSS 读取失败：暂时无法连接内容来源，请稍后重新读取。');
    await checkNoticeCenterClicks({ label:'RSS 读取失败', noticesId:'source-notices', inputId:'source-q',
      evaluate, check, click, screenshot, send });
    await fail('RSS 筛选读取失败：来源服务暂时不可用。');
    await click('#source-filter');
    check('RSS 来源筛选中心首点击同样可以收起自动提示', await evaluate(`!$('source-notices').open`));
    check('RSS 来源筛选中心不再是无响应区域', await evaluate(`(() => {
      const select = $('source-filter'), box = select.getBoundingClientRect();
      return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === select;
    })()`));
    await click('#source-filter');
    check('RSS 收起后来源筛选中心可以真实聚焦', await evaluate(`document.activeElement === $('source-filter')`));
    await send('Input.dispatchKeyEvent', { type:'keyDown', key:'Escape', code:'Escape', windowsVirtualKeyCode:27 });
    await send('Input.dispatchKeyEvent', { type:'keyUp', key:'Escape', code:'Escape', windowsVirtualKeyCode:27 });
    if (!await evaluate(`$('source-notices').open`)) await click('#source-notices > summary');
    check('RSS 用户手动查看时重试按钮仍可真实命中', await evaluate(`(() => {
      const button = document.querySelector('#source-error button'), box = button.getBoundingClientRect();
      return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === button;
    })()`));
  } finally {
    await evaluate(`window.pipeJson = window.qaNoticeHitJson;
      delete window.qaNoticeHitJson; delete window.qaNoticeHitMessage;
      sourceError(); openSubsPanel(${JSON.stringify(originalKind)}); true`);
    await wait(`sourceKind === ${JSON.stringify(originalKind)} && sourceData && sourceRows.length > 0`);
    await evaluate(`clearTimeout(sourcePoll); true`);
  }
}
