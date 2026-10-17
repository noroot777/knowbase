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

/** 在某个点上滚几下。 */
async function wheelAt(send, at, delta, times) {
  for (let step = 0; step < times; step++) {
    await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: at.x, y: at.y, deltaX: 0, deltaY: delta });
    await settle(90);
  }
  await settle();
  return at;
}

/**
 * 在某个元素跟视口的**交集**中间滚几下。
 *
 * 不能直接拿元素自己的中心:窄屏那一支里 `.fv-detail` 比视口高出好几屏,它的
 * 中心落在视口外,滚轮一下都发不进去(那条检查会变成一条不敏感的绿)。
 */
async function wheelOver(send, evaluate, selector, delta, times) {
  const at = JSON.parse(await evaluate(`(() => {
    const box = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
    const top = Math.max(0, box.top), bottom = Math.min(innerHeight, box.bottom);
    const left = Math.max(0, box.left), right = Math.min(innerWidth, box.right);
    return JSON.stringify({ x: Math.round((left + right) / 2), y: Math.round((top + bottom) / 2) });
  })()`));
  return wheelAt(send, at, delta, times);
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

export async function checkDrawerScrollAway({ evaluate, wait, check, screenshot, send, viewport }) {
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

  // ── 收藏详情 ────────────────────────────────────────────────────────────
  // 这一处跟另外三处不一样:**要收的那条头在它自己那条轴里面**。所以正文必须用
  // **真实的可锚定文本**撑开 —— 浏览器的滚动锚定只在有文本节点要保位置时才补偿
  // `scrollTop`,拿一个空白的高盒子占位,那一档就是一条不敏感的绿(上一轮正是
  // 这么漏掉的:连滚六次最终状态碰巧对了,而单次滚动停手时它一直在抖)。
  const favFixture = `(() => {
    const box = document.querySelector('.fv-detail-content');
    box.innerHTML = Array.from({ length: 60 }, (unused, i) =>
      '<p style="margin:0 0 16px;line-height:1.9">第 ' + (i + 1) + ' 段：'
      + '收藏详情的正文得是真文字，浏览器才会在布局变了之后去保住视口里的这一段。'.repeat(3)
      + '</p>').join('');
    document.querySelector('.fv-detail').scrollTop = 0;
    scrollTo(0, 0);
    return true;
  })()`;
  const favShape = () => shape('.fv-detail > .fv-aside-heading', '.fv-detail', '.fv-inspector .away-exit');

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
  await evaluate(favFixture);
  await settle();
  const favTop = await favShape();
  check('收藏详情:头在上面占着一行', favTop.head >= 30 && !favTop.exit, JSON.stringify(favTop));

  // **一次滚轮,然后停手** —— 之后一个输入都不再注入,只等。这一档钉的是
  // 「收起这件事自己不会把自己撤销」:头的高度变化会让浏览器补偿 `scrollTop`,
  // 补偿方向正好跟收起相反,认了它就来回震荡(收→补→展→再补),停手两秒之后
  // 那条头还占着原来的位置。两次采样隔开一秒多,抖的话不可能两次都正好是 0。
  await wheelOver(send, evaluate, '.fv-detail', 120, 1);
  await settle(700);
  const favSettling = await favShape();
  await settle(1300);
  const favSettled = await favShape();
  check('收藏详情:一次滚轮停手之后稳稳收起,不来回抖',
    favSettling.head === 0 && favSettled.head === 0, JSON.stringify({ favSettling, favSettled }));
  check('收藏详情:收起之后那条头不许还能用 Tab 走进去', favSettled.inert, JSON.stringify(favSettled));
  // 收起把位置本身压低了(头在轴里面,浏览器为了保住视口里那段文字补掉了一截):
  // **「回到顶部」那一支要是不把那一截加回去,轻轻再往下滚一下就会命中它、头当场
  // 弹回来。** 这里只往下挪到那条线上,方向上才攒 +1、不够触发收起那一支,所以
  // 这一下只会走位置判据 —— 正好单独钉住它。
  const favEdge = JSON.parse(await evaluate(`(() => {
    const box = document.querySelector('.fv-detail');
    const from = Math.round(box.scrollTop);
    box.scrollTop = from < 64 ? 64 : from;
    return JSON.stringify({ from, to: Math.round(box.scrollTop) });
  })()`));
  await settle();
  const favEdged = await favShape();
  // 前半句同时钉住夹具前提:收起之后位置确实被压到了那条线以下,不然这条不敏感。
  check('收藏详情:收起之后位置落在「回到顶部」那条线上,头也不许弹回来',
    favEdge.from < 64 && favEdged.head === 0, JSON.stringify({ favEdge, favEdged }));
  // **来回三趟。** 判位置那把尺子要是带状态(譬如一边收起一边往里攒一个估出来的
  // 「补偿量」),每趟都会多攒一截:攒到超过那条线之后,滚回开头也放不出来了。
  // 每趟都把两头都钉上 —— 只钉「收得起来」的话,放不出来那一半就漏了。
  for (let round = 1; round <= 3; round++) {
    await wheelOver(send, evaluate, '.fv-detail', -120, 8);
    await settle(600);
    const back = await favShape();
    check(`收藏详情:第 ${round} 趟滚回开头,那条头原样回来`,
      back.head >= 30 && !back.inert, JSON.stringify(back));
    await wheelOver(send, evaluate, '.fv-detail', 120, 1);
    await settle(600);
    const again = await favShape();
    check(`收藏详情:第 ${round} 趟再往下滚,照旧收得起来`,
      again.head === 0 && again.inert, JSON.stringify(again));
  }

  check('收藏详情:常驻返回按钮顶上来了', favSettled.exit, JSON.stringify(favSettled));
  await screenshot('scroll-away-favorite-collapsed');
  await clickCenter(send, evaluate, '.fv-inspector .away-exit');
  check('收藏详情:收起状态下那颗返回按钮真能退回收藏架',
    await evaluate(`favActive === null`), '');

  // ── 收藏详情 · 窄屏 ─────────────────────────────────────────────────────
  // 760px 以下这块**不自己滚**(`max-height:none; overflow:visible`),用户滚的是
  // 整页。照桌面那条轴绑的话 scrollTop 和可滚距离都是 0,一下都收不起来,而那条
  // sticky 的头会一直占着屏幕上方一行 —— 轴得跟着断点换。
  await viewport(390, 844);
  await settle();
  await evaluate(`openFavDetail(901); true`);
  await wait(`favActive === 901 && document.querySelector('.fv-detail')`);
  await evaluate(favFixture);
  await settle();
  const narrowTop = await favShape();
  const narrowAxis = JSON.parse(await evaluate(`JSON.stringify({
    inner: Math.round(document.querySelector('.fv-detail').scrollHeight
      - document.querySelector('.fv-detail').clientHeight),
    page: Math.round(document.documentElement.scrollHeight - innerHeight) })`));
  // 夹具自己钉一句:这一档的前提就是「内部不滚、整页滚」,前提不成立下面是假绿。
  check('收藏详情窄屏:前提 —— 内部不滚、整页才滚',
    narrowAxis.inner === 0 && narrowAxis.page > 400, JSON.stringify(narrowAxis));
  check('收藏详情窄屏:头一开始占着一行', narrowTop.head >= 30 && !narrowTop.exit, JSON.stringify(narrowTop));
  await wheelAt(send, { x: 195, y: 500 }, 120, 10);
  const narrowAway = await favShape();
  check('收藏详情窄屏:滚整页也收得起来', narrowAway.head === 0 && narrowAway.inert,
    JSON.stringify({ narrowAway, scrollY: await evaluate(`Math.round(scrollY)`) }));
  check('收藏详情窄屏:常驻返回按钮跟着顶上来', narrowAway.exit, JSON.stringify(narrowAway));
  await screenshot('scroll-away-favorite-narrow');
  await clickCenter(send, evaluate, '.fv-detail-exit');
  check('收藏详情窄屏:那颗按钮真能退回收藏架', await evaluate(`favActive === null`), '');

  await viewport(1440, 1000);
  await settle();
  await evaluate(`favItems = []; renderFavCollection(); renderFavInspector();
    scrollTo(0, 0); switchTab('subs'); true`);
  await settle();
}
