async function checkNoticeCenterClicks({ label, noticesId, inputId, screenshotPrefix = 'notice-center', evaluate, check, click, screenshot, send }) {
  const originalValue = await evaluate(`$(${JSON.stringify(inputId)}).value`);
  const geometry = () => evaluate(`(() => {
    const heading = $(${JSON.stringify(noticesId)}).closest('.source-heading').getBoundingClientRect();
    const content = $('${inputId === 'source-q' ? 'source-workbench' : 'subs-wechat-manage'}').getBoundingClientRect();
    return { heading:heading.height, top:content.top, height:content.height };
  })()`);
  const before = await geometry();
  try {
    await screenshot(`${screenshotPrefix}-${inputId}-before`);
    await click('#' + inputId);
    check(`${label}输入框中心首点击收起提示并直接聚焦`, await evaluate(`!$(${JSON.stringify(noticesId)}).open && document.activeElement === $(${JSON.stringify(inputId)})`));
    check(`${label}首点击后输入框中心可以真实命中`, await evaluate(`(() => {
      const input = $(${JSON.stringify(inputId)}), box = input.getBoundingClientRect();
      return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === input;
    })()`));
    await send('Input.insertText', { text:'科技' });
    check(`${label}首点击后无需再次点击即可真实输入`, await evaluate(`document.activeElement === $(${JSON.stringify(inputId)}) && $(${JSON.stringify(inputId)}).value === ${JSON.stringify(originalValue + '科技')}`));
    const after = await geometry();
    check(`${label}收起提示不挤压工作区或改变页头`, Object.keys(before).every(key => Math.abs(before[key] - after[key]) < 1), JSON.stringify({ before, after }));
    await screenshot(`${screenshotPrefix}-${inputId}-typed`);
  } finally {
    if (await evaluate(`$(${JSON.stringify(noticesId)}).open`)) await click('#' + noticesId + ' > summary');
    await evaluate(`${inputId === 'source-q' ? 'clearTimeout(sourceSearchTimer);' : ''}
      $(${JSON.stringify(inputId)}).value = ${JSON.stringify(originalValue)}; true`);
  }
}

async function checkNoticeTextSelection({ label, noticesId, textId, evaluate, check, send, afterSelection, reverse = false, tiny = false }) {
  const points = await evaluate(`(() => {
    const text = $(${JSON.stringify(textId)}).firstChild, range = document.createRange();
    range.setStart(text, 0); range.setEnd(text, Math.min(text.length, ${tiny ? 1 : 6}));
    const box = range.getBoundingClientRect();
    return { left:${tiny ? 'box.left + box.width / 2 - 1' : 'box.left + 1'},
      right:${tiny ? 'box.left + box.width / 2 + 1' : 'box.right - 1'}, y:box.top + box.height / 2 };
  })()`);
  const start = reverse ? points.right : points.left, end = reverse ? points.left : points.right;
  try {
    await send('Input.dispatchMouseEvent', { type:'mouseMoved', x:start, y:points.y });
    await send('Input.dispatchMouseEvent', { type:'mousePressed', x:start, y:points.y, button:'left', buttons:1, clickCount:1 });
    for (let step = 1; step <= 5; step++) await send('Input.dispatchMouseEvent', {
      type:'mouseMoved', x:start + (end - start) * step / 5, y:points.y, button:'left', buttons:1 });
    await send('Input.dispatchMouseEvent', { type:'mouseReleased', x:end, y:points.y, button:'left', clickCount:1 });
    check(`${label}仍可真实拖选说明文字`, await evaluate(`window.getSelection().toString().length > 0`));
    check(`${label}拖选文字不会收起详情`, await evaluate(`$(${JSON.stringify(noticesId)}).open`));
    if (afterSelection) await afterSelection();
  } finally {
    await evaluate(`window.getSelection().removeAllRanges(); true`);
  }
}

async function checkNoticeSelectionClicks(options) {
  await checkNoticeTextSelection({ ...options, afterSelection:async () => {
    const { label, noticesId, textId, inputId, evaluate, check } = options;
    check(`${label}下一次点击前保留选区且输入框中心仍被说明遮挡`, await evaluate(`(() => {
      const notices = $(${JSON.stringify(noticesId)}), box = $(${JSON.stringify(inputId)}).getBoundingClientRect();
      const selection = window.getSelection(), target = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
      return !selection.isCollapsed && notices.contains(selection.anchorNode) && notices.contains(selection.focusNode)
        && notices.querySelector('.source-notices-body').contains(target);
    })()`));
    const click = options.jitter ? async selector => {
      if (selector !== '#' + inputId) return options.click(selector);
      const point = await evaluate(`(() => {
        const box = $(${JSON.stringify(inputId)}).getBoundingClientRect();
        return { x:box.left + box.width / 2, y:box.top + box.height / 2 };
      })()`);
      check(`${label}微移点击的起点不命中说明文字`, await evaluate(`(() => {
        const range = document.createRange(); range.selectNodeContents($(${JSON.stringify(textId)}));
        return [...range.getClientRects()].every(box => ${point.x - 1} < box.left || ${point.x - 1} > box.right
          || ${point.y} < box.top || ${point.y} > box.bottom);
      })()`));
      await options.send('Input.dispatchMouseEvent', { type:'mouseMoved', x:point.x - 1, y:point.y });
      await options.send('Input.dispatchMouseEvent', { type:'mousePressed', x:point.x - 1, y:point.y, button:'left', buttons:1, clickCount:1 });
      await options.send('Input.dispatchMouseEvent', { type:'mouseMoved', x:point.x + 1, y:point.y, button:'left', buttons:1 });
      await options.send('Input.dispatchMouseEvent', { type:'mouseReleased', x:point.x + 1, y:point.y, button:'left', clickCount:1 });
    } : options.click;
    await checkNoticeCenterClicks({ ...options, click, label:options.label + '拖选后' });
  } });
}

export async function checkFirstWechatNotice({ evaluate, wait, check, click, screenshot, send }) {
  await wait(`!$('subs-wechat').hidden && !!wechatSubsChannel() && $('src-status').textContent !== '检查中…'`);
  await checkNoticeCenterClicks({ label:'公众号首访', noticesId:'subs-wechat-notices', inputId:'tgt-search',
    evaluate, check, click, screenshot, send });
  await click('#subs-wechat-notices > summary');
  check('公众号手动重新展开也显示收起说明', await evaluate(`$('subs-wechat-notices').open && !wechatNoticesAutoOpened
    && getComputedStyle(document.querySelector('#subs-wechat-notices .source-notice-dismiss-hint')).display !== 'none'`));
  await checkNoticeSelectionClicks({ label:'公众号手动详情', noticesId:'subs-wechat-notices', textId:'src-detail', inputId:'tgt-search', screenshotPrefix:'notice-manual-wechat',
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
  await evaluate(`window.qaNoticeHitJson = window.pipeJson; window.qaNoticeHitCalls = 0;
    window.pipeJson = (path, ...rest) => {
      if (path !== '/api/intake/rss') return window.qaNoticeHitJson(path, ...rest);
      window.qaNoticeHitCalls++; return Promise.reject(new Error(window.qaNoticeHitMessage));
    }; true`);
  try {
    await fail('RSS 读取失败：暂时无法连接内容来源，请稍后重新读取。');
    await checkNoticeCenterClicks({ label:'RSS 读取失败', noticesId:'source-notices', inputId:'source-q',
      evaluate, check, click, screenshot, send });
    await click('#source-notices > summary');
    check('RSS 错误手动重新展开也显示收起说明', await evaluate(`$('source-notices').open && !sourceNoticesAutoOpened
      && getComputedStyle(document.querySelector('#source-notices .source-notice-dismiss-hint')).display !== 'none'`));
    await checkNoticeSelectionClicks({ label:'RSS 手动错误详情', noticesId:'source-notices', textId:'source-error-text', inputId:'source-q', screenshotPrefix:'notice-manual-rss',
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
    await click('#source-notices > summary');
    await click('#source-filter');
    check('RSS 手动错误详情的筛选中心首点击收起并聚焦', await evaluate(`!$('source-notices').open && document.activeElement === $('source-filter')`));
    if (await evaluate(`$('source-notices').open`)) await click('#source-notices > summary');
    if (!await evaluate(`$('source-notices').open`)) await click('#source-notices > summary');
    check('RSS 用户手动查看时重试按钮仍可真实命中', await evaluate(`(() => {
      const button = document.querySelector('#source-error button'), box = button.getBoundingClientRect();
      return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === button;
    })()`));
    const retryCalls = await evaluate('window.qaNoticeHitCalls');
    await checkNoticeTextSelection({ label:'RSS 重试按钮前', noticesId:'source-notices', textId:'source-error-text', evaluate, check, send,
      afterSelection:async () => {
        await click('#source-error button');
        await wait(`window.qaNoticeHitCalls === ${retryCalls + 1} && !$('source-error').hidden`);
        check('RSS 拖选后重试首点击仅调用一次真实读取', await evaluate(`window.qaNoticeHitCalls === ${retryCalls + 1} && !$('source-notices').open`));
      } });
    await click('#source-notices > summary');
    await checkNoticeTextSelection({ label:'RSS 键盘重试前', noticesId:'source-notices', textId:'source-error-text', evaluate, check, send,
      afterSelection:async () => {
        await evaluate(`$('source-error').querySelector('button').focus(); true`);
        await send('Input.dispatchKeyEvent', { type:'keyDown', key:'Enter', code:'Enter', windowsVirtualKeyCode:13, text:'\r' });
        await send('Input.dispatchKeyEvent', { type:'keyUp', key:'Enter', code:'Enter', windowsVirtualKeyCode:13 });
        await wait(`window.qaNoticeHitCalls === ${retryCalls + 2} && !$('source-error').hidden`);
        check('RSS 拖选后键盘重试仅触发一次且收起提示', await evaluate(`window.qaNoticeHitCalls === ${retryCalls + 2} && !$('source-notices').open`));
      } });
  } finally {
    await evaluate(`window.pipeJson = window.qaNoticeHitJson;
      delete window.qaNoticeHitJson; delete window.qaNoticeHitMessage; delete window.qaNoticeHitCalls;
      sourceError(); openSubsPanel(${JSON.stringify(originalKind)}); true`);
    await wait(`sourceKind === ${JSON.stringify(originalKind)} && sourceData && sourceRows.length > 0`);
    await evaluate(`clearTimeout(sourcePoll); true`);
  }
}

export async function checkManualSourceNotices({ evaluate, check, click, screenshot, send }) {
  const states = await evaluate(`['source-progress', 'source-issues', 'source-configuration']
    .map(id => ({ id, hidden:$(id).hidden, html:$(id).innerHTML }))`);
  try {
    for (const [state, label] of [['configuration', '待配置'], ['progress', '正在同步'], ['issues', '来源连接需处理']]) {
      await evaluate(`closeSourceNotices(); sourceError(); $('source-configuration').hidden = false;
        $('source-progress').hidden = ${state !== 'progress'};
        $('source-progress-text').textContent = '正在同步来源：第 3/12 个账号';
        $('source-issues').hidden = ${state !== 'issues'};
        $('source-issues').textContent = '1 个来源上次连接失败，查看订阅管理'; updateSourceNotices(); true`);
      await click('#source-notices > summary');
      check(`${label}从摘要真实点击进入手动详情`, await evaluate(`$('source-notices').open && !sourceNoticesAutoOpened`));
      check(`${label}手动详情也显示收起说明`, await evaluate(`getComputedStyle(document.querySelector('#source-notices .source-notice-dismiss-hint')).display !== 'none'`));
      const centerCheck = state === 'configuration' ? checkNoticeSelectionClicks : checkNoticeCenterClicks;
      await centerCheck({ label:label + '手动详情', noticesId:'source-notices', textId:'source-configuration', inputId:'source-q', screenshotPrefix:'notice-manual-' + state,
        evaluate, check, click, screenshot, send });
      await click('#source-notices > summary');
      await click('#source-filter');
      check(`${label}手动详情的筛选中心首点击收起并聚焦`, await evaluate(`!$('source-notices').open && document.activeElement === $('source-filter')`));
    }
    await click('#source-notices > summary');
    await click('#source-issues');
    check('手动连接问题详情的管理按钮首点击直接跳转', await evaluate(`sourceView === 'manage' && !$('source-notices').open`));
    await evaluate(`switchSourceView('items')`);
  } finally {
    await evaluate(`closeSourceNotices();
      for (const state of ${JSON.stringify(states)}) { $(state.id).hidden = state.hidden; $(state.id).innerHTML = state.html; }
      updateSourceNotices(); true`);
  }
}

export async function checkNoticeGestureSequences({ evaluate, wait, check, click, screenshot, send, viewport }) {
  const original = await evaluate(`({ kind:subsKind, width:innerWidth, height:innerHeight })`);
  try {
    for (const kind of ['rss', 'wechat']) {
      await evaluate(`openSubsPanel(${JSON.stringify(kind)}); true`);
      await wait(kind === 'rss' ? `sourceKind === 'rss' && sourceData && sourceRows.length > 0`
        : `!$('subs-wechat').hidden && !$('src-detail').hidden && $('src-status').textContent !== '检查中…'`);
      const noticesId = kind === 'rss' ? 'source-notices' : 'subs-wechat-notices';
      const textId = kind === 'rss' ? 'source-error-text' : 'src-detail';
      const inputId = kind === 'rss' ? 'source-q' : 'tgt-search';
      for (const width of [1440, 375]) {
        await viewport(width, 1000);
        for (const automatic of [true, false]) {
          for (let attempt = 1; attempt <= 3; attempt++) {
            const label = `${kind} ${width}px ${automatic ? '自动' : '手动'}说明第 ${attempt} 次${attempt === 3 ? '（2px 拖选）' : ''}`;
            await evaluate(kind === 'rss'
              ? `closeSourceNotices(); sourceError(${JSON.stringify(label + '：来源暂时不可用，请稍后重新读取。')}); true`
              : `closeWechatNotices(); updateWechatNotices(true); true`);
            if (!automatic) {
              await click('#' + noticesId + ' > summary');
              await click('#' + noticesId + ' > summary');
            }
            check(`${label}进入预期的展开模式`, await evaluate(`$(${JSON.stringify(noticesId)}).open
              && ${kind === 'rss' ? 'sourceNoticesAutoOpened' : 'wechatNoticesAutoOpened'} === ${automatic}`));
            await checkNoticeSelectionClicks({ label, noticesId, textId, inputId, reverse:attempt === 2, tiny:attempt === 3,
              jitter:kind === 'rss' && width === 1440 && attempt === 1,
              screenshotPrefix:`notice-gesture-${kind}-${width}-${automatic ? 'auto' : 'manual'}-${attempt}`,
              evaluate, check, click, screenshot, send });
          }
        }
      }
    }
  } finally {
    await viewport(original.width, original.height);
    await evaluate(`sourceError(); closeSourceNotices(); closeWechatNotices(); openSubsPanel(${JSON.stringify(original.kind)}); true`);
    await wait(`subsKind === ${JSON.stringify(original.kind)} && sourceKind === ${JSON.stringify(original.kind)} && sourceData && sourceRows.length > 0`);
    await evaluate(`clearTimeout(sourcePoll); true`);
  }
}
