// 「读正文时让出位置」装在**另外三处阅读区**上的检查:文章阅读器(正文在同源
// iframe 里)、工具详情抽屉、收藏详情。接在 `tests/subs_layout_probe.mjs` 上 ——
// 这四处在同一个单页应用里,切 tab 就到。订阅页自己那几档在
// `subs_reader_checks.mjs` 和 `subs_reader_guard_checks.mjs`。
//
// 这三处跟订阅页的分别在于:**那条头里装着退出这一处的唯一入口**(关闭 / 返回)。
// 所以收起的同时右上角要顶上一颗常驻的 `.away-exit`,判据是**点它真能退出去**,
// 不是「它的 display 不是 none」—— 收起态那块是 `inert`,只看样式分不出来。
//
// 正文内容由探针自己撑高:这一层要钉的是「轴绑没绑上、头收不收、退不退得出去」,
// 跟里面装的是哪一篇无关,而夹具库里没有图鉴和收藏数据。文章阅读器那一处
// iframe 装的是一份静态文本(同源即可)—— 跨文档绑定成不成立只看这个。

const SETTLE = 380;
const settle = (ms = SETTLE) => new Promise(resolve => setTimeout(resolve, ms));

/** 在某个元素中间滚几下。坐标要现算:这几处的宽度都跟视口绑着。 */
async function wheelOver(send, evaluate, selector, delta, times) {
  const at = JSON.parse(await evaluate(`(() => {
    const box = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
    return JSON.stringify({ x: Math.round(box.left + box.width / 2), y: Math.round(box.top + box.height / 2) });
  })()`));
  for (let step = 0; step < times; step++) {
    await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: at.x, y: at.y, deltaX: 0, deltaY: delta });
    await settle(90);
  }
  await settle();
  return at;
}

/** 点一个元素的中心(这几颗按钮都是动态出现的,给不出稳定的 click 选择器)。 */
async function clickCenter(send, evaluate, selector) {
  const at = JSON.parse(await evaluate(`(() => {
    const node = document.querySelector(${JSON.stringify(selector)});
    if (!node) return 'null';
    const box = node.getBoundingClientRect();
    return JSON.stringify({ x: Math.round(box.left + box.width / 2), y: Math.round(box.top + box.height / 2) });
  })()`));
  if (!at) return false;
  for (const type of ['mousePressed', 'mouseReleased'])
    await send('Input.dispatchMouseEvent', { ...at, type, button: 'left', clickCount: 1 });
  await settle();
  return true;
}

export async function checkDrawerScrollAway({ evaluate, wait, check, screenshot, send }) {
  const shape = (head, scroll, exit) => evaluate(`(() => {
    const h = document.querySelector(${JSON.stringify(head)});
    const s = document.querySelector(${JSON.stringify(scroll)});
    const e = document.querySelector(${JSON.stringify(exit)});
    return { head: Math.round(h.getBoundingClientRect().height),
      body: Math.round(s.getBoundingClientRect().height),
      inert: h.hasAttribute('inert'),
      exit: !!e && e.getClientRects().length > 0 };
  })()`);

  // ── 工具详情抽屉 ────────────────────────────────────────────────────────
  await evaluate(`switchTab('gallery'); true`);
  await settle();
  // 真实入口:抽屉的显示和绑定都在 openToolDrawer 的头几行,工具数据取不回来
  // 不影响这一层(夹具库里没有图鉴数据,取回来的一定是那张错误卡)。
  await evaluate(`openToolDrawer('qa-fixture-tool'); true`);
  await wait(`$('tool-drawer').style.display !== 'none'`);
  await evaluate(`$('td-entries').innerHTML = '<div style="height:2400px"></div>';
    $('td-scroll').scrollTop = 0; true`);
  await settle();
  const drawerTop = await shape('.td-head', '#td-scroll', '#td-exit');
  check('抽屉:头在上面占着一行,常驻关闭按钮还没出来',
    drawerTop.head >= 40 && !drawerTop.exit, JSON.stringify(drawerTop));
  await wheelOver(send, evaluate, '#td-scroll', 120, 6);
  const drawerAway = await shape('.td-head', '#td-scroll', '#td-exit');
  check('抽屉:往下滚,头收起来', drawerAway.head === 0 && drawerAway.inert, JSON.stringify(drawerAway));
  check('抽屉:让出来的高度给了内容',
    drawerAway.body - drawerTop.body >= drawerTop.head - 1, JSON.stringify({ drawerTop, drawerAway }));
  check('抽屉:常驻关闭按钮顶上来了', drawerAway.exit, JSON.stringify(drawerAway));
  await screenshot('scroll-away-drawer-collapsed');
  // 后果判据:收起之后**真的还退得出去**。
  await clickCenter(send, evaluate, '#td-exit');
  check('抽屉:收起状态下那颗关闭按钮真能关掉抽屉',
    await evaluate(`$('tool-drawer').style.display === 'none'`), '');
  // 关掉等于这条轴不在页面上了,收起态必须当场撤掉 —— 下次打开不能还是收着的。
  await evaluate(`openToolDrawer('qa-fixture-tool'); true`);
  await wait(`$('tool-drawer').style.display !== 'none'`);
  await settle();
  const reopened = await shape('.td-head', '#td-scroll', '#td-exit');
  check('抽屉:再打开时头是摊开的', reopened.head >= 40 && !reopened.inert && !reopened.exit,
    JSON.stringify(reopened));
  await evaluate(`closeToolDrawer(); true`);
  await settle();

  // ── 文章阅读器(正文在同源 iframe 里) ──────────────────────────────────
  await evaluate(`(() => {
    const a = document.createElement('a');
    a.setAttribute('href', '/static/css/base.css');
    a.dataset.title = '跨文档收起验证';
    openReader(a);
    return true;
  })()`);
  await wait(`$('reader').style.display !== 'none' && $('reader-frame').contentDocument?.readyState === 'complete'`);
  await evaluate(`(() => {
    const doc = $('reader-frame').contentDocument;
    doc.body.innerHTML = '<div style="height:3000px"></div>';
    doc.scrollingElement.scrollTop = 0;
    return true;
  })()`);
  await settle();
  const readerTop = await shape('#reader-head', '#reader-frame', '#reader-exit');
  check('阅读器:头在上面占着一行', readerTop.head >= 40 && !readerTop.exit, JSON.stringify(readerTop));
  await wheelOver(send, evaluate, '#reader-frame', 120, 6);
  const readerAway = await shape('#reader-head', '#reader-frame', '#reader-exit');
  check('阅读器:iframe 里往下滚,外面那条头收起来',
    readerAway.head === 0 && readerAway.inert, JSON.stringify(readerAway));
  check('阅读器:让出来的高度给了正文',
    readerAway.body - readerTop.body >= readerTop.head - 1, JSON.stringify({ readerTop, readerAway }));
  check('阅读器:常驻关闭按钮顶上来了', readerAway.exit, JSON.stringify(readerAway));
  await screenshot('scroll-away-reader-collapsed');
  await clickCenter(send, evaluate, '#reader-exit');
  check('阅读器:收起状态下那颗关闭按钮真能关掉阅读器',
    await evaluate(`$('reader').style.display === 'none'`), '');

  // 不关、直接换下一篇:重绑走的是**旧 window 已经 detach** 那条路(先 release
  // 上一份再绑新的)。这一档单独钉一次 —— 头一次打开永远绕过它。
  await evaluate(`(() => {
    const a = document.createElement('a');
    a.setAttribute('href', '/static/css/base.css');
    a.dataset.title = '第一篇';
    openReader(a);
    return true;
  })()`);
  await wait(`$('reader').style.display !== 'none' && $('reader-frame').contentDocument?.readyState === 'complete'`);
  await evaluate(`(() => {
    const a = document.createElement('a');
    a.setAttribute('href', '/static/js/scrollaway.js');
    a.dataset.title = '第二篇';
    openReader(a);
    return true;
  })()`);
  await wait(`$('reader-frame').contentWindow?.location.pathname === '/static/js/scrollaway.js'
    && $('reader-frame').contentDocument?.readyState === 'complete'`);
  await evaluate(`(() => {
    const doc = $('reader-frame').contentDocument;
    doc.body.innerHTML = '<div style="height:3000px"></div>';
    doc.scrollingElement.scrollTop = 0;
    return true;
  })()`);
  await settle();
  await wheelOver(send, evaluate, '#reader-frame', 120, 6);
  const second = await shape('#reader-head', '#reader-frame', '#reader-exit');
  check('阅读器:换下一篇之后照样收得起来', second.head === 0 && second.exit, JSON.stringify(second));
  await evaluate(`closeReader(); true`);
  await settle();
  check('阅读器:换过篇之后关闭不出错', await evaluate(`$('reader').style.display === 'none'`), '');

  // ── 收藏详情(每次重绘都是新 DOM,绑定要跟着走) ────────────────────────
  await evaluate(`switchTab('fav'); true`);
  await settle();
  await evaluate(`(() => {
    favItems = [{ id: 901, title: '收起验证样例', path: '', url: 'https://example.org/qa',
      quote: '', note: '', summary: '用来验证收藏详情那条头会不会跟着收起。',
      status: 'inbox', origin: 'doc', action: null,
      created_at: new Date().toISOString().slice(0, 19) }];
    openFavDetail(901);
    return true;
  })()`);
  await wait(`favActive === 901 && document.querySelector('.fv-detail')`);
  await evaluate(`document.querySelector('.fv-detail-content').style.height = '2400px';
    document.querySelector('.fv-detail').scrollTop = 0; true`);
  await settle();
  const favTop = await shape('.fv-detail > .fv-aside-heading', '.fv-detail', '.fv-inspector .away-exit');
  check('收藏详情:头在上面占着一行', favTop.head >= 30 && !favTop.exit, JSON.stringify(favTop));
  await wheelOver(send, evaluate, '.fv-detail', 120, 6);
  const favAwayShape = await shape('.fv-detail > .fv-aside-heading', '.fv-detail', '.fv-inspector .away-exit');
  check('收藏详情:往下滚,头收起来', favAwayShape.head === 0 && favAwayShape.inert, JSON.stringify(favAwayShape));
  check('收藏详情:常驻返回按钮顶上来了', favAwayShape.exit, JSON.stringify(favAwayShape));
  await screenshot('scroll-away-favorite-collapsed');
  await clickCenter(send, evaluate, '.fv-inspector .away-exit');
  check('收藏详情:收起状态下那颗返回按钮真能退回收藏架',
    await evaluate(`favActive === null`), '');
  await evaluate(`favItems = []; renderFavCollection(); renderFavInspector(); switchTab('subs'); true`);
  await settle();
}
