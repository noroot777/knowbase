// 「我的订阅」阅读区的两条定向检查,都接在 `tests/subs_layout_probe.mjs` 上:
//
// ① **读正文时让出位置** —— 正文往下滚,它上面那条工具栏(以及手动收录那一类的
//    投递栏)收起来,滚回顶上再原样放回来。实现和它吃过的亏在
//    `kb/static/js/scrollaway.js`。
// ② **投递框的焦点只有一条边** —— 全站口径是「border 变色 + 贴着内沿的 1px 内
//    描边连成一条 2px 实线」(base.css),外面再套一圈 outline 就是两道选中痕迹。
//    `redesign_probe.mjs` 第 8 节钉的是检索页和图鉴页那几个框,够不着订阅页里
//    这一个 —— 它正是漏在外面、长出「双眼皮」的那一处。
//
// 判据一律钉在**后果**上:阅读区实际多拿到多少高度、放回来之后是不是真的回到
// 原样、该拦的那两档拦没拦住 —— 不验「类名加上没有」。

/** 收起/展开跑完一轮过渡要等多久(base.css 那条是 .2s)。 */
const SETTLE = 380;

const settle = () => new Promise(resolve => setTimeout(resolve, SETTLE));

/** 把这一类来源摆回「最新内容 + 没有筛选」:视图和搜索词都是按来源记住的。 */
async function resetToReading(evaluate, wait) {
  await evaluate(`switchSourceView('items');
    $('source-workbench').classList.remove('is-list-only', 'is-focused');
    if ($('source-q').value || $('source-filter').value) {
      $('source-q').value = ''; $('source-filter').value = ''; sourceFilterChanged();
    }
    true`);
  await wait(`sourceView === 'items' && !$('source-q').value && !sourceBusy`);
}

export async function checkReaderScrollAway({ evaluate, wait, check, screenshot, send }) {
  // 真滚轮,不是 scrollTop 赋值:这件事是在**读**的时候发生的,得证明真实手势
  // 能走到(口径同 `tests/x_channel_probe.mjs` 那条「必须是真实鼠标事件」)。
  const wheel = async (x, y, delta, times) => {
    for (let step = 0; step < times; step++) {
      await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x, y, deltaX: 0, deltaY: delta });
      await new Promise(resolve => setTimeout(resolve, 90));
    }
    await settle();
  };
  const scrollTo = async position => {
    await evaluate(`$('source-reader-scroll').scrollTop = ${position}; true`);
    await settle();
  };
  const shape = () => evaluate(`(() => {
    const head = $('source-head'), bar = $('grab-bar'), scroll = $('source-reader-scroll');
    return { head: Math.round(head.getBoundingClientRect().height),
      bar: bar.hidden ? null : Math.round(bar.getBoundingClientRect().height),
      reader: Math.round(scroll.getBoundingClientRect().height),
      top: Math.round(scroll.scrollTop), room: scroll.scrollHeight - scroll.clientHeight,
      inert: head.hasAttribute('inert'), inline: head.getAttribute('style') || '' };
  })()`);
  // 把可滚距离调到指定值:正文换成一行字,再用正文那个盒子自己的高度去凑。
  // 两道拦截判据各要一个特定的 room,靠真实文章凑不出来。
  // **不能拿「把它压成 0 再量 scrollHeight」当底** —— 内容比视口矮时
  // `scrollHeight` 会被 `clientHeight` 兜底,量到的是视口高度而不是内容高度,
  // 照它算出来的 room 全错(2026-10-17 实测:要 600 拿到 20,那一档的判据因此
  // 变成了一条不敏感的绿)。改成先撑到一个一定滚得动的高度,量出此刻的 room,
  // 再按差额回调 —— room 随这个盒子的高度线性变化。
  const setRoom = async want => {
    await evaluate(`(() => {
      const body = $('source-reader-body');
      if (body.dataset.qaText === undefined) body.dataset.qaText = body.textContent;
      body.textContent = '短';
      body.style.height = '2000px';
      return true;
    })()`);
    return evaluate(`(() => {
      const scroll = $('source-reader-scroll'), body = $('source-reader-body');
      const room = scroll.scrollHeight - scroll.clientHeight;
      body.style.height = (2000 - (room - ${want})) + 'px';
      scroll.scrollTop = 0;
      return scroll.scrollHeight - scroll.clientHeight;
    })()`);
  };
  const restoreBody = () => evaluate(`(() => {
    const body = $('source-reader-body');
    body.style.height = '';
    if (body.dataset.qaText !== undefined) { body.textContent = body.dataset.qaText; delete body.dataset.qaText; }
    $('source-reader-scroll').scrollTop = 0;
    return true;
  })()`);

  await evaluate(`openSubsPanel('social'); $('wechat-welcome').hidden = true; true`);
  await wait(`sourceKind === 'social'`);
  // 视图和筛选都是**按来源记住的**,而这组检查排在一长串别的检查后面 —— 不自己
  // 摆正的话,接手到的可能是上一组留下的「订阅管理 + 一个搜索词」,列表里一条
  // 可读内容都没有(2026-10-17 实测:切到手动收录时 view 还是 manage,
  // `sourceRows` 为 0,等 12 秒全落空)。
  await resetToReading(evaluate, wait);
  await wait(`sourceRows.length >= 30 && sourceReaderItem`);
  await evaluate(`clearTimeout(sourcePoll); $('source-reader-scroll').scrollTop = 0; true`);
  await settle();
  const atTop = await shape();
  check('正文在顶上时工具栏整条都在', atTop.head >= 50 && !atTop.inert, JSON.stringify(atTop));

  await wheel(1150, 600, 120, 6);
  const away = await shape();
  check('正文往下滚,上面那条工具栏收起来', away.head === 0, JSON.stringify(away));
  check('收起让出来的高度全给了正文', away.reader - atTop.reader >= atTop.head - 1,
        JSON.stringify({ atTop, away }));
  check('收起的工具栏不许还能用 Tab 走进去', away.inert, JSON.stringify(away));
  await screenshot('scroll-away-collapsed');

  await wheel(1150, 600, -120, 3);
  const back = await shape();
  check('往回滚一点,工具栏原样回来',
        back.head === atTop.head && back.reader === atTop.reader, JSON.stringify({ atTop, back }));
  check('放回来之后不留 inline 高度,也不再 inert',
        !/height/.test(back.inline) && !back.inert, JSON.stringify(back));

  // 反向钉一边之一:滚**内容列表**不是在读正文,工具栏不许跟着收。
  await scrollTo(0);
  await wheel(400, 600, 120, 8);
  const listed = await evaluate(`JSON.stringify({ list: Math.round($('source-list-pane').scrollTop),
    head: Math.round($('source-head').getBoundingClientRect().height) })`);
  check('滚内容列表不收工具栏', JSON.parse(listed).list > 0 && JSON.parse(listed).head === atTop.head, listed);

  // 反向钉一边之二:**程序改 scrollTop 不算他在读。** 从「订阅管理」切回阅读会
  // 恢复上次停在哪儿,那一下照样发 scroll 事件;认了它,用户手都没动工具栏就没
  // 了,而紧跟着要点的「订阅管理」正好在那块里(2026-10-17 实测点不中,后面
  // 一串检查连环超时)。
  await scrollTo(0);
  await scrollTo(260);
  const restored = await shape();
  check('程序恢复阅读位置不收工具栏', restored.head === atTop.head && !restored.inert,
        JSON.stringify(restored));
  await scrollTo(0);

  // 反向钉一边之三:收起会让可滚距离缩短,短到收完滚不回来就不许收 —— 收了
  // 浏览器会把 scrollTop 钳回去,那一下是一次向上的滚动,于是展开、又能往下滚,
  // 来回抖。两道判据各钉一次,再钉一次「不该拦的照收」。
  // 位置先用程序摆好(上面刚钉过:摆位置本身不会收),再补一次小幅真手势去触发
  // 判断 —— 这几档的射程全在那两个数上,靠滚轮凑不出精确的 room 和 top。
  const nudge = async position => { await scrollTo(position); await wheel(1150, 600, 30, 1); };
  const shortRoom = await setRoom(200);
  // 夹具自己也要钉一句:room 没摆到位的话,下面那条判据会变成一条不敏感的绿。
  check('短正文那一档的可滚距离摆到了 200 上下', Math.abs(shortRoom - 200) <= 4, String(shortRoom));
  await nudge(70);
  const short = await shape();
  check('正文短到收完就滚不回来时不收', short.head === atTop.head,
        JSON.stringify({ shortRoom, short }));

  const longRoom = await setRoom(600);
  check('长正文那一档的可滚距离摆到了 600 上下', Math.abs(longRoom - 600) <= 4, String(longRoom));
  await nudge(540);
  const bottom = await shape();
  check('已经贴到底了不收', bottom.head === atTop.head, JSON.stringify({ longRoom, bottom }));
  await scrollTo(0);
  await nudge(170);
  const middle = await shape();
  check('同一篇在中段照收不误', middle.head === 0, JSON.stringify({ longRoom, middle }));
  await scrollTo(0);
  await restoreBody();
  await settle();

  // 手动收录那一类头上还顶着一条投递栏,两块要一起收 —— 它正是用户截图里
  // 红框圈住的那 155px。
  await evaluate(`openSubsPanel('inbox'); true`);
  await wait(`sourceKind === 'inbox'`);
  await wait(`!$('grab-bar').hidden`);
  await resetToReading(evaluate, wait);
  await wait(`sourceRows.length > 0 && sourceReaderItem`);
  await evaluate(`clearTimeout(sourcePoll); $('source-reader-scroll').scrollTop = 0; true`);
  await settle();
  const inboxTop = await shape();
  check('手动收录的投递栏在工具栏下面占着一行', inboxTop.bar >= 60, JSON.stringify(inboxTop));
  await wheel(1150, 600, 120, 6);
  const inboxAway = await shape();
  check('工具栏和投递栏一起收起来', inboxAway.head === 0 && inboxAway.bar === 0, JSON.stringify(inboxAway));
  check('两块让出来的高度都给了正文',
        inboxAway.reader - inboxTop.reader >= inboxTop.head + inboxTop.bar - 2,
        JSON.stringify({ inboxTop, inboxAway }));
  await screenshot('scroll-away-inbox-collapsed');

  // 切去「订阅管理」时阅读区不在页面上,收起态必须当场复位 —— 不复位的话
  // 那块工具栏会带着一个半截的 inline 高度留在管理视图上。
  await evaluate(`switchSourceView('manage'); true`);
  await wait(`sourceView === 'manage'`);
  const managed = await shape();
  check('切到订阅管理,工具栏当场复位',
        managed.head === inboxTop.head && !managed.inert && !/height/.test(managed.inline),
        JSON.stringify({ inboxTop, managed }));
  await evaluate(`switchSourceView('items'); true`);
  await wait(`sourceView === 'items'`);
  await evaluate(`$('source-reader-scroll').scrollTop = 0; true`);
  await settle();
}

export async function checkGrabInputFocus({ evaluate, wait, check, click }) {
  await evaluate(`openSubsPanel('inbox'); true`);
  await wait(`sourceKind === 'inbox' && !$('grab-bar').hidden`);
  // 真实鼠标点进去,不用 `.focus()`::focus-visible 分键盘/指针两种模态,
  // 而这一条判的是「有没有多画一圈」,两种模态下都不该多。
  await click('#grab-url');
  await new Promise(resolve => setTimeout(resolve, 260));
  const ring = await evaluate(`(() => {
    const brand = '23, 96, 79', style = getComputedStyle($('grab-url'));
    return { outline: style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0,
      inset: style.boxShadow.includes(brand) && style.boxShadow.includes('inset'),
      border: parseFloat(style.borderTopWidth) > 0 && style.borderTopColor.includes(brand),
      radius: style.borderTopLeftRadius, padding: style.paddingLeft,
      focused: document.activeElement === $('grab-url') };
  })()`);
  check('投递框真的拿到了焦点', ring.focused, JSON.stringify(ring));
  check('投递框聚焦不套外圈 outline(不许再长双眼皮)', !ring.outline, JSON.stringify(ring));
  check('投递框的焦点是 border + 内描边那一条', ring.border && ring.inset, JSON.stringify(ring));
  // 反过来钉一边:几何也要跟全站那条输入框基线一致,不是自己另写一套。
  check('投递框的圆角和内边距走全站基线', ring.radius === '10px' && ring.padding === '13px',
        JSON.stringify(ring));
  await evaluate(`$('grab-url').blur(); true`);
}
