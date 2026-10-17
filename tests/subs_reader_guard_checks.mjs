// 「读正文时让出位置」这一层**不许顺手吃掉什么** —— 四组定向检查,接在
// `tests/subs_layout_probe.mjs` 上。收起本身的那几档在
// `tests/subs_reader_checks.mjs`,这一份专钉它的副作用。
//
// ① **退出阅读的每一条路都要把工具栏放回来。**「只看列表」和窄屏「返回列表」
//    只切 class、不经过布局函数,工具栏会停在 `height:0 + inert` 上 —— 再切回
//    正文视图它还是那样,整条谁都点不着。不变量落在元素自己身上(scrollaway.js
//    给每条轴挂了 ResizeObserver,阅读区被 display:none 时尺寸归零就复位),
//    所以这里**不验「某个按钮记得调了复位」,验「走完这条路之后那条工具栏能用」**
//    —— 判据是点它里面的「订阅管理」真能把视图切过去。
// ② **要人处理的告警不许被收走。** 收起的那块里装着「读取失败 + 重新读取」,
//    `role="alert"` 落进 `inert` 子树就等于这条消息没送到。反过来钉一边:同一块
//    浮层里的进度(`role="status"`)不该把工具栏钉死在展开态。
// ③ **手势门认的是「手还按着」,不是「最近若干毫秒有动静」。** 按住原生滚动条
//    慢慢往下拖,这一分钟里手一直在上面;按时间窗算的话一秒之后就判成「程序在
//    滚」,工具栏再也收不起来。另一边:展开方向任何时候都放行,不受这道门影响。
// ④ **后台重绘不许把收起态弹回来。** 同步完一轮会重绘这一页,一刀切复位的话
//    就是「读着读着工具栏自己跳出来」。
//
// 判据一律钉在后果上(高度、点不点得中、视图切没切过去),不验类名。

const SETTLE = 380;
const settle = (ms = SETTLE) => new Promise(resolve => setTimeout(resolve, ms));

/**
 * 正文滚动轴中间那个点,滚轮和按下都打这儿。
 *
 * **现算,不许写死**:这一组里有一段把视口压到 720 宽验窄屏,写死的 1150 落在
 * 视口外,滚轮一下都发不进去 —— 而「前提:确实收起来了」之后那几条照样会跑完,
 * 变成一串不敏感的绿。
 */
const at = evaluate => evaluate(`(() => {
  const box = $('source-reader-scroll').getBoundingClientRect();
  return JSON.stringify({ x: Math.round(box.left + box.width / 2), y: Math.round(box.top + box.height / 2) });
})()`).then(JSON.parse);

export async function checkScrollAwayGuards({ evaluate, wait, check, click, screenshot, send, viewport }) {
  const shape = () => evaluate(`(() => {
    const head = $('source-head'), scroll = $('source-reader-scroll');
    return { head: Math.round(head.getBoundingClientRect().height),
      reader: Math.round(scroll.getBoundingClientRect().height),
      top: Math.round(scroll.scrollTop), inert: head.hasAttribute('inert'),
      inline: head.getAttribute('style') || '',
      error: (() => {
        // 「看得见」要按命中测试算:收起态是 height:0 + overflow:hidden +
        // opacity:0 + pointer-events:none,而 getClientRects() 对这几样一概照旧
        // 非空 —— 拿它当判据的话,报错被收走也是一条绿。
        const btn = $('source-error').querySelector('button');
        const box = btn.getBoundingClientRect();
        if (!box.width || !box.height) return false;
        const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
        return !!hit && btn.contains(hit);
      })() };
  })()`);
  const wheel = async (delta, times) => {
    const spot = await at(evaluate);
    for (let step = 0; step < times; step++) {
      await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: spot.x, y: spot.y, deltaX: 0, deltaY: delta });
      await settle(90);
    }
    await settle();
  };
  const scrollTo = async position => {
    await evaluate(`$('source-reader-scroll').scrollTop = ${position}; true`);
    await settle();
  };
  /** 收起到位,顺带确认这一轮的前提成立(没收起的话后面几条全是假绿)。 */
  const collapse = async label => {
    await scrollTo(0);
    await wheel(120, 6);
    const away = await shape();
    const room = await evaluate(`$('source-reader-scroll').scrollHeight - $('source-reader-scroll').clientHeight`);
    check(`${label}:前提 —— 工具栏确实收起来了`, away.head === 0, JSON.stringify({ ...away, room }));
    return away;
  };

  await evaluate(`openSubsPanel('social'); $('wechat-welcome').hidden = true; true`);
  await wait(`sourceKind === 'social'`);
  await evaluate(`switchSourceView('items');
    $('source-workbench').classList.remove('is-list-only', 'is-focused');
    if ($('source-q').value || $('source-filter').value) {
      $('source-q').value = ''; $('source-filter').value = ''; sourceFilterChanged();
    }
    true`);
  await wait(`sourceView === 'items' && !sourceBusy && sourceRows.length >= 30 && sourceReaderItem`);
  await evaluate(`clearTimeout(sourcePoll); $('source-reader-scroll').scrollTop = 0; true`);
  await settle();
  const open = await shape();
  check('前提:工具栏初始是整条摊开的', open.head >= 50 && !open.inert, JSON.stringify(open));

  // ── ① 退出阅读的两条路 ──────────────────────────────────────────────────
  await collapse('只看列表');
  await click('#source-list-expand');
  await wait(`$('source-workbench').classList.contains('is-list-only')`);
  await settle();
  const listOnly = await shape();
  check('切到只看列表,收起态当场撤掉', listOnly.head === open.head && !listOnly.inert
    && !/height/.test(listOnly.inline), JSON.stringify({ open, listOnly }));
  await click('#source-list-expand');
  await wait(`!$('source-workbench').classList.contains('is-list-only')`);
  await settle();
  // 后果判据:那条工具栏里的按钮真点得中(光看高度不够,`inert` 是另一回事)。
  await click('#source-view-manage');
  await settle();
  check('回到正文视图后工具栏里的按钮点得中',
    await evaluate(`sourceView === 'manage'`), JSON.stringify(await shape()));
  await evaluate(`switchSourceView('items'); true`);
  await wait(`sourceView === 'items'`);
  await settle();

  // 窄屏那一条:列表和正文单栏切换,返回列表同样只切 class。
  await viewport(720, 900);
  await settle();
  await evaluate(`$('source-workbench').classList.add('has-mobile-reader'); true`);
  await settle();
  await collapse('窄屏返回列表');
  await evaluate(`backToSourceList(); true`);
  await settle();
  const backToList = await shape();
  check('窄屏返回列表,收起态当场撤掉',
    !backToList.inert && !/height/.test(backToList.inline), JSON.stringify(backToList));
  await viewport(1440, 1000);
  await settle();
  await scrollTo(0);

  // ── ② 告警的出口 ────────────────────────────────────────────────────────
  await collapse('读取失败');
  await evaluate(`sourceError('本地服务暂不可用'); true`);
  await settle();
  const failed = await shape();
  check('读取失败时工具栏自己放出来', failed.head >= 50 && !failed.inert, JSON.stringify(failed));
  check('失败原因和那颗「重新读取」点得中', failed.error, JSON.stringify(failed));
  await screenshot('scroll-away-error-visible');
  // 报错还摆着的时候不许再收 —— 收了等于刚送到的消息又被收走。
  await wheel(120, 6);
  const stillOpen = await shape();
  check('报错没处理完,往下滚也不收', stillOpen.head >= 50 && stillOpen.error, JSON.stringify(stillOpen));
  // 后果判据:点下去真把这一路重读了一遍(读成功会把报错清掉)。
  await click('#source-error button');
  await settle(1500);
  check('点下去真把来源重读了一遍', await evaluate(`$('source-error').hidden`), JSON.stringify(await shape()));
  await evaluate(`sourceError(''); clearTimeout(sourcePoll); true`);
  await wait(`!sourceBusy && sourceRows.length >= 30 && sourceReaderItem`);
  await settle();
  const cleared = await collapse('报错清掉之后');
  check('报错清掉之后又能收起来', cleared.head === 0, JSON.stringify(cleared));

  // 反过来钉一边:同一块浮层里的**进度**是 role=status,不该把工具栏钉死。
  await scrollTo(0);
  await evaluate(`$('source-progress').hidden = false;
    $('source-progress-text').textContent = '正在同步…'; updateSourceNotices(); true`);
  await settle();
  await wheel(120, 6);
  const running = await shape();
  check('同步进度(status)不拦收起', running.head === 0, JSON.stringify(running));
  await evaluate(`$('source-progress').hidden = true; updateSourceNotices(); true`);
  await settle();

  // ── ③ 手势门:认「手还按着」 ────────────────────────────────────────────
  await scrollTo(0);
  const hold = await at(evaluate);
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...hold, button: 'left', clickCount: 1 });
  await settle(1000);                                   // 比 AWAY_GESTURE_MS 长一截
  await scrollTo(200);                                  // 拖动把轴带下去的那一下
  const dragged = await shape();
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...hold, button: 'left', clickCount: 1 });
  check('按住滚动条慢慢拖,照样算他在读', dragged.head === 0, JSON.stringify(dragged));

  // 反过来钉一边:松手之后过了窗口,程序改 scrollTop 不算。
  await scrollTo(0);
  await settle(900);
  await scrollTo(200);
  const afterRelease = await shape();
  check('松手过了窗口,程序滚不收', afterRelease.head === open.head, JSON.stringify(afterRelease));

  // 另一边:**展开方向不过这道门** —— 换文章、程序往回滚都该把它放出来。
  await scrollTo(0);
  await wheel(120, 6);
  const beforeUp = await shape();
  check('前提:先收起来了', beforeUp.head === 0 && beforeUp.top > 150, JSON.stringify(beforeUp));
  await settle(900);                                    // 让手势窗口过期
  await scrollTo(Math.max(70, beforeUp.top - 100));     // 仍在 AWAY_ENTER 之上
  const up = await shape();
  check('往回滚不受手势门影响,照样放出来', up.head === open.head && up.top > 64, JSON.stringify({ beforeUp, up }));

  // ── ④ 手动收录那条持久提示也要送到人跟前 ──────────────────────────────
  // 投递栏跟正文一起收起,而「这一篇没进去、你还得再做点什么」是终态提示 ——
  // 落在 inert 子树里就永远走不出去(短暂的 toast 过几秒也没了)。走真实汇合点
  // `updateGrabControls()`,不自己改 DOM。
  await evaluate(`openSubsPanel('inbox'); true`);
  await wait(`sourceKind === 'inbox' && !$('grab-bar').hidden`);
  await evaluate(`switchSourceView('items');
    $('source-workbench').classList.remove('is-list-only', 'is-focused'); true`);
  await wait(`sourceView === 'items' && sourceRows.length > 0 && sourceReaderItem`);
  await evaluate(`clearTimeout(sourcePoll); $('source-reader-scroll').scrollTop = 0; true`);
  await settle();
  const grabOpen = await evaluate(`Math.round($('grab-bar').getBoundingClientRect().height)`);
  await collapse('手动收录');
  const barAway = await evaluate(`Math.round($('grab-bar').getBoundingClientRect().height)`);
  check('手动收录:前提 —— 投递栏也收起来了', barAway === 0, JSON.stringify({ grabOpen, barAway }));
  await evaluate(`grabBusy = false; grabStage = 'failed';
    grabNote = '抓取失败，这条没存下来；再投一次。'; updateGrabControls(); true`);
  await settle();
  const grabFailed = await evaluate(`(() => {
    const bar = $('grab-bar'), hint = $('grab-hint');
    const box = hint.getBoundingClientRect();
    const hit = box.width && box.height
      && document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return JSON.stringify({ bar: Math.round(bar.getBoundingClientRect().height),
      inert: bar.hasAttribute('inert'), said: hint.textContent,
      visible: !!hit && hint.contains(hit) });
  })()`);
  check('收录失败的持久提示一出现,投递栏自己放出来',
    JSON.parse(grabFailed).bar >= 60 && !JSON.parse(grabFailed).inert, grabFailed);
  check('那句失败原因真看得见', JSON.parse(grabFailed).visible
    && /没存下来/.test(JSON.parse(grabFailed).said), grabFailed);
  // 反过来钉一边:成功态不该把投递栏永久钉在展开态。
  await evaluate(`grabStage = 'done'; grabNote = '已写入知识库，全文检索里搜得到了。';
    updateGrabControls(); true`);
  await settle();
  await scrollTo(0);
  await wheel(120, 6);
  const grabDone = await evaluate(`Math.round($('grab-bar').getBoundingClientRect().height)`);
  check('收录成功之后照旧收得起来', grabDone === 0, String(grabDone));
  await evaluate(`grabStage = ''; grabNote = ''; updateGrabControls(); true`);
  await evaluate(`openSubsPanel('social'); true`);
  await wait(`sourceKind === 'social'`);
  await evaluate(`switchSourceView('items');
    $('source-workbench').classList.remove('is-list-only', 'is-focused'); true`);
  await wait(`sourceView === 'items' && sourceRows.length >= 30 && sourceReaderItem`);
  await evaluate(`clearTimeout(sourcePoll); true`);
  await scrollTo(0);

  // ── ⑤ 后台重绘不弹回来 ──────────────────────────────────────────────────
  await collapse('后台重绘');
  await evaluate(`applySubsReadingLayout(); true`);
  await settle();
  const redrawn = await shape();
  check('后台同步完一轮重绘,收起态原样留着', redrawn.head === 0 && redrawn.inert, JSON.stringify(redrawn));
  await scrollTo(0);
  await settle();
}
