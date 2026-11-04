// 候选排名弹窗那一组点检,从 `youtube_probe.mjs` 拆出来(那份顶着 700 行上限)。
// 两段各在主探针的固定位置调用:`dialog` 在准入名次还是默认 10 的时候跑,`limit` 在
// 主探针把准入名次存成 2 之后跑。CDP 那几个动作由主探针传进来,这里不另起连接。

// 名次从卡片文字里读回来。这段刻意不用正则 —— 它住在模板字面量里,`\d` 这类转义会先被字面量吃掉。
const rankOf = `node => Number((node.textContent.split('第 ')[1] || '').split(' 名')[0])`;

export async function dialog({ evaluate, wait, click, fill, send, check, screenshot }) {
  // 候选榜收在「候选排名」那颗按钮后面:订阅清单里一条都不许夹着它,点开才取、才显示。
  check('订阅清单里不夹着候选榜', await evaluate(`
    !$('source-list').querySelector('.youtube-rank-item') && !$('youtube-rank-modal') &&
    document.querySelectorAll('.source-subscription').length === 52`));
  await click('#youtube-rank-open');
  await wait(`!!$('youtube-rank-modal') && document.querySelectorAll('.youtube-rank-item').length === 4`);
  check('候选榜显示且非 AI 视频被过滤', await evaluate(`
    document.querySelectorAll('.youtube-rank-item').length === 4 &&
    $('youtube-ranking').textContent.includes('轻量过滤')`));
  // **只有一张榜**:没有排序下拉，每条只报一个名次和一个分数，不再出现「热度/价值」。
  // 这段刻意不用正则 —— 它住在模板字面量里，`\d`、`\/` 这类转义会先被字面量吃掉。
  check('一张榜一个名次，没有排序下拉也没有两榜字样', await evaluate(`(() => {
    const text = $('youtube-ranking').textContent;
    const cards = [...document.querySelectorAll('[data-group^="d"] .youtube-rank-item')];
    const ranks = cards.map(${rankOf}).sort((a, b) => a - b);
    const first = cards.find(node => (${rankOf})(node) === 1);
    return !document.querySelector('#youtube-rank-order') &&
      !text.includes('热度') && !text.includes('价值') &&
      first.textContent.includes('第 1 名 / ') && first.textContent.includes(' 分 · ') &&
      ranks.join() === '1,2,3';
  })()`), await evaluate(`document.querySelector('.youtube-rank-item')?.textContent`));
  check('候选榜页头一行报总数和每轮准入名次', await evaluate(`
    $('youtube-rank-count').textContent === '4 条 · 每轮准入前 10 名'`),
    await evaluate(`$('youtube-rank-count').textContent`));
  // 排上名的按发布日期分组(上海时区、新的在上、组内按名次)、默认展开;没排上名的另起一格、
  // 默认收起。期望的那一天**另用一种算法**从接口回包算(+8 小时取 UTC 日期),不拿页面自己的
  // 函数对页面自己 —— 那样时区写错了也恒绿。
  check('排上名的按发布日期分组，过滤的另起一格收起', await evaluate(`fetch('/api/intake/youtube/ranking')
    .then(response => response.json()).then(value => {
      const shanghai = published => new Date(new Date(published).getTime() + 8 * 3600000).toISOString().slice(0, 10);
      const expected = Object.fromEntries(value.items.filter(item => item.score_rank)
        .map(item => [item.url, shanghai(item.published)]));
      const groups = [...document.querySelectorAll('.youtube-rank-group')];
      const dated = groups.filter(node => node.dataset.group.startsWith('d'));
      const keys = dated.map(node => node.dataset.group.slice(1));
      const placed = dated.every(node => [...node.querySelectorAll('.youtube-rank-item')].every(card =>
        expected[card.querySelector('a').getAttribute('href')] === node.dataset.group.slice(1)));
      const ordered = dated.every(node => {
        const ranks = [...node.querySelectorAll('.youtube-rank-item')].map(${rankOf});
        return ranks.join() === [...ranks].sort((a, b) => a - b).join();
      });
      const skipped = groups.at(-1);
      return dated.length > 0 && placed && ordered && dated.every(node => node.open) &&
        keys.join() === [...keys].sort().reverse().join() &&
        Object.keys(expected).length === 3 &&
        skipped.dataset.group === 'skipped' && !skipped.open &&
        skipped.querySelector('summary').textContent.includes('不处理') &&
        skipped.querySelector('summary').textContent.includes('1 条') &&
        skipped.textContent.includes('轻量过滤') && !groups.some(node => node.textContent.includes('第 1 期'));
    })`), await evaluate(`[...document.querySelectorAll('.youtube-rank-group summary')].map(node => node.textContent).join(' | ')`));
  // 「当前准入」由后端按真实准入条件判(`admitted`),页面只照着标:比的是**哪几条**,不只是有没有。
  const admittedMatches = `fetch('/api/intake/youtube/ranking').then(response => response.json()).then(value => {
    const expected = value.items.filter(item => item.admitted).map(item => item.url).sort().join();
    const marked = [...document.querySelectorAll('.youtube-rank-item')].filter(node => node.querySelector('.youtube-rank-next'))
      .map(node => node.querySelector('a').getAttribute('href')).sort().join();
    return expected !== '' && expected === marked && !document.querySelector('[data-group="skipped"] .youtube-rank-next');
  })`;
  check('当前准入照后端判定就地标出', await evaluate(admittedMatches));
  await screenshot('ranking-desktop');
  // 搜索时未排名那几格的组头说匹配了几条,不拿全组总数冒充。
  await fill('#youtube-rank-q', 'travel');
  await wait(`!!document.querySelector('[data-group="skipped"]')`);
  check('搜索时未排名那格的组头报匹配条数', await evaluate(`
    document.querySelector('[data-group="skipped"] summary').textContent.includes('匹配 1 条 · 共 1 条') &&
    !document.querySelector('[data-group^="d"]')`));
  await fill('#youtube-rank-q', '不会有这个标题zz');
  await wait(`$('youtube-ranking').textContent.includes('没有匹配的候选')`);
  check('候选榜搜索筛掉不匹配的条目', await evaluate(`document.querySelectorAll('.youtube-rank-item').length === 0`));
  await fill('#youtube-rank-q', '');
  await send('Input.dispatchKeyEvent', {type:'keyDown', key:'Backspace', code:'Backspace', windowsVirtualKeyCode:8});
  await send('Input.dispatchKeyEvent', {type:'keyUp', key:'Backspace', code:'Backspace', windowsVirtualKeyCode:8});
  await wait(`document.querySelectorAll('.youtube-rank-item').length === 4`);
  check('清空搜索后候选榜原样回来', true);
  await send('Input.dispatchKeyEvent', {type:'keyDown', key:'Escape', code:'Escape', windowsVirtualKeyCode:27});
  await send('Input.dispatchKeyEvent', {type:'keyUp', key:'Escape', code:'Escape', windowsVirtualKeyCode:27});
  await wait(`!$('youtube-rank-modal')`);
  check('Esc 关掉候选榜并把焦点还给入口按钮', await evaluate(`document.activeElement === $('youtube-rank-open')`));
  // **「还在读」不许被说成「是空的」。** 把榜单那一包压慢,读取途中往搜索框敲字:
  // 页面得照旧说正在读取,不能叫人去点同步;回包落地后按刚敲的那个词筛。
  await evaluate(`(() => {
    const real = window.pipeJson;
    window.pipeJson = (path, ...rest) => String(path).includes('/youtube/ranking')
      ? new Promise(resolve => setTimeout(resolve, 1500)).then(() => real(path, ...rest)) : real(path, ...rest);
    window.__restorePipeJson = () => { window.pipeJson = real; };
    return true;
  })()`);
  await click('#youtube-rank-open');
  await wait(`$('youtube-ranking')?.textContent.includes('正在读取')`);
  await fill('#youtube-rank-q', 'agent');
  check('读取途中搜索不误报空榜', await evaluate(`
    $('youtube-ranking').textContent.includes('正在读取') && !$('youtube-ranking').textContent.includes('还没有候选')`),
    await evaluate(`$('youtube-ranking').textContent`));
  await wait(`document.querySelectorAll('.youtube-rank-item').length > 0`);
  check('回包落地后按读取途中敲的词筛选', await evaluate(`
    [...document.querySelectorAll('.youtube-rank-item')].every(node => node.textContent.toLowerCase().includes('agent')) &&
    document.querySelectorAll('.youtube-rank-item').length < 4`));
  await evaluate(`window.__restorePipeJson(); closeYouTubeRanking(); true`);
}

export async function limit({ evaluate, wait, click, fill, send, check }) {
  // 准入名次改成 2 之后,第 3 名不再标当前准入;反过来钉一边:前两名照旧标着。
  await click('#youtube-rank-open');
  await wait(`document.querySelectorAll('.youtube-rank-group').length > 0`);
  check('改了准入名次就按新名次标当前准入', await evaluate(`(() => {
    const cards = [...document.querySelectorAll('[data-group^="d"] .youtube-rank-item')];
    const marked = cards.filter(node => node.querySelector('.youtube-rank-next')).map(${rankOf}).sort((a, b) => a - b);
    return $('youtube-rank-count').textContent.endsWith('每轮准入前 2 名') && marked.join() === '1,2' && cards.length === 3;
  })()`), await evaluate(`[...document.querySelectorAll('.youtube-rank-group summary')].map(node => node.textContent).join(' | ')`));
  // 人收起的那一组,搜索时临时全展开,清空搜索后还得是收起的。收起的是第 1 名所在那天 ——
  // 下面搜的 agent 只命中它,搜索时那一组得还在。
  const day = await evaluate(`[...document.querySelectorAll('[data-group^="d"]')].find(node =>
    [...node.querySelectorAll('.youtube-rank-item')].some(card => (${rankOf})(card) === 1)).dataset.group`);
  await click(`[data-group="${day}"] > summary`);
  await wait(`!document.querySelector('[data-group="${day}"]').open`);
  await fill('#youtube-rank-q', 'agent');
  await wait(`document.querySelector('[data-group="${day}"]')?.open`);
  await fill('#youtube-rank-q', '');
  await send('Input.dispatchKeyEvent', {type:'keyDown', key:'Backspace', code:'Backspace', windowsVirtualKeyCode:8});
  await send('Input.dispatchKeyEvent', {type:'keyUp', key:'Backspace', code:'Backspace', windowsVirtualKeyCode:8});
  await wait(`document.querySelectorAll('.youtube-rank-item').length === 4`);
  check('搜索之后人收起的那一组仍是收起的', await evaluate(`
    !document.querySelector('[data-group="${day}"]').open &&
    [...document.querySelectorAll('[data-group^="d"]')].every(node => node.dataset.group === '${day}' || node.open)`));
  await evaluate(`closeYouTubeRanking(); true`);
}
