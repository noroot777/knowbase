// ── 读正文时让出位置:往下滚,正文上面那条工具栏收起来 ────────────────────────
//
// **哪些地方需要这一层。** 整页滚动的面板(每日日报、全文检索、同步中心、工具
// 图鉴列表)页头本来就跟着整页滚走,一个字都不用改;会卡住的是**正文自己占一条
// 滚动轴、而轴上面那条头固定不动**的那一类 —— 「我的订阅」的阅读区(来源工具栏
// 60px + 手动收录的投递栏 95px)、文章阅读器、工具详情抽屉、收藏详情。滚不动它,
// 也挤不走它。
//
// 口径,都是实测/构造上吃过亏的地方:
//
// ① **只认正文那一条滚动轴。** 内容列表自己滚的时候不收 —— 那不是在读。
// ② **收起只认用户自己滚的那一下。** 程序改 `scrollTop` 照样发 scroll 事件,而
//    这些页面到处都在改它:换一篇文章写回 0、从「订阅管理」切回阅读时恢复上次
//    停在哪儿。后一种会被当成「他正在往下读」,工具栏当场收起 —— 可用户刚回到
//    这一页、手都没动过,紧跟着要点的那颗按钮恰好就在收起的那块里,**点不中**
//    (2026-10-17 `subs_usability_checks` 恢复 180px 阅读位置那一步实测:
//    `click('#source-view-manage')` 落空,后面整串检查连环超时)。
//    判据是**手还按着没松**(pointerdown…pointerup/cancel 的整个区间),不是
//    「最近若干毫秒内有过动静」:按住原生滚动条慢慢往下拖一分钟,这一分钟里手一直
//    在上面,而按时间窗算的话一秒之后就判成「程序在滚」,工具栏再也收不起来
//    (实测:按住 1000ms 再拖 120px,正文 500→1744px 而工具栏纹丝不动;同样距离
//    快速拖就正常收起)。时间窗只留着接松手之后的惯性。
//    **放出来不设这道门** —— 那个方向永远安全,换文章时正该靠它自动展开;所以
//    手势判据只挂在收起那一支上,不许写成提前 return。
// ③ **方向要累计着看。** 触控板和惯性滚动一帧里正负都有,单看一次 delta 会在
//    边界上反复横跳(收起→展开→收起),看起来像页面在抖。
// ④ **收起之后必须还滚得回来。** 收起让滚动容器变高、可滚距离跟着变短:短到
//    比收起的那一块还少时,浏览器会把 `scrollTop` 钳回去,那一下是**一次向上的
//    滚动事件**,于是展开、容器变矮、又能往下滚……来回抖。所以收之前问两句:
//    整条轴够不够长、此刻离底还有没有收起那么多。
// ⑤ **收起态要留着 `overflow:hidden`。** 来源工具栏里挂着一块绝对定位的状态
//    浮层(`.source-notices-body`,`top:100%`),不裁的话它会从一条高度为 0 的
//    工具栏底下冒出来。反过来,**展开之后那条 inline `overflow` 必须撤掉** ——
//    常驻 hidden 会把展开态的浮层也裁掉,那才是真丢信息。
// ⑥ **收起不许把「要人处理的告警」和「退出这一处的唯一入口」一起收走。**
//    前者由 `scrollAwayBlocked()` 挡在收起之前(订阅页那条工具栏里装着「读取
//    失败 + 重新读取」);后者由调用方传 `host`,收起时它身上会打
//    `has-scroll-away`,`.away-exit` 那颗常驻按钮靠它显形。
// ⑧ **判位置要用一把不受自身收起状态影响的尺子。** 收起轴**内**的目标会让轴上
//    的内容整体上移,浏览器为了让视口里那段文字别乱跑会补偿 `scrollTop`
//    (scroll anchoring)—— 补偿之后 `scrollTop` 就不再代表「正文读到哪儿了」,
//    而补偿方向正好跟收起相反,认了它就是收起→补偿→展开→再补偿的来回震荡
//    (实测:收藏详情里单次 120px 滚轮之后 1049ms 内方向反转 9 次,那条头在
//    29.7~57px 之间抖,停手之后一点都没收起来)。
//    尺子是 `scrollAwayPosition()`:**`scrollTop` 加上此刻被自己收掉的轴内高度**。
//    这个数在收起前后恒定(120 → 补偿到 63,加回 57 还是 120),方向累计和「回到
//    顶部」两条判据从此有共同的参照。**目标在轴外时这一项恒为 0**,另外三处的
//    判据跟原来一字不差。
//    **别去猜「这一截位移是不是补偿」。** 按方向 + 时间窗 + 幅度上限估一个
//    「自身补偿量」看着也能把震荡压住,但那几样都证明不了因果:平息期里用户真的
//    反向滚了、换一篇把 `scrollTop` 写回 0,统统满足同一组条件,于是被当成自身
//    补偿攒进去 —— 实测快速滚回开头或紧接着点下一篇,整条工具栏就再也回不来了,
//    而那个估出来的量还会一轮轮累积(收藏往返三次:57 → 104 → 161,可它那条头
//    一共才 57px)。
//    动画**跑的过程中**那几帧仍然对不齐(高度是渐变的,而收掉的高度按收起前量到
//    的整份算),所以还要一小段**平息期**:期间只跟着记位置、不改判,平息期一完
//    按真实位置补判一次。
// ⑦ **阅读区不在页面上了就得瞬时复位**,否则那条工具栏停在 `height:0 + inert`
//    上,切回来谁都点不着它。这条不许押在「某个按钮记得调一下」——
//    每条轴自己挂一个 `ResizeObserver` 盯着,元素被 `display:none` 时尺寸归零,
//    它当场复位(`syncScrollAway()` 是同一条判据的同步版)。
//
// 高度为什么由 JS 写进 inline:`height:auto` 做不了过渡,得给它一个具体的起点和
// 终点。收起态留着 inline 的 `height:0`,展开动画跑完就把 inline 那几条撤干净,
// 元素回到自然布局 —— 之后窗口折行、提示文字变长变短都不用谁来同步。
// 配套样式(过渡曲线、收起时要跟着收掉的 padding / 边线 / min-height、以及
// `.away-exit`)在 `kb/static/css/base.css`。各处的接入点见
// `docs/decisions/scroll-away.md`。

/** 绑过的滚动轴。复位和对账要能一把全扫,所以存着。 */
const SCROLL_AWAY = [];
/** 滚过这么多才允许收起:正文开头附近来回挪几下不该让工具栏跟着闪。 */
const AWAY_ENTER = 64;
/** 同方向累计这么多像素才改判(口径③)。 */
const AWAY_STEP = 20;
/** 收起之后至少要剩这么多可滚距离,否则这篇太短、不值得收(口径④)。 */
const AWAY_ROOM = 160;
/** 离底不足「收起的高度 + 这一点余量」时不收 —— 收了 scrollTop 会被钳(口径④)。 */
const AWAY_FLOOR = 24;
/** 松手之后多久内的滚动还算他自己滚的(口径②,接惯性用;按着的时候不看它)。 */
const AWAY_GESTURE_MS = 700;
/** 清理 inline 样式的兜底时限,也是自己改完高度之后那段平息期(口径⑧)。
 *  比 base.css 里那条 .2s 过渡长一截 —— 高度是渐变的,中途那几帧对不齐。 */
const AWAY_MS = 280;

/**
 * 把元素和 iframe 的 window 抹平成一条「问得出位置」的轴。
 *
 * 跨文档那一种(文章阅读器)手势发生在 iframe 自己的 document 上,而松手可能落在
 * 外面 —— 两份 window 的 pointerup 都得听。
 */
function scrollAwayAxis(node) {
  const win = typeof node.scrollY === 'number';
  return {
    node,
    gestures: win ? node.document : node,
    outer: win ? node : window,
    // 两个读数都兜一层:**换一篇文章时旧的那份 window 已经 detach 了**,而解绑
    // 正好发生在新文档的 load 里(先 release 上一份再绑新的),这条路每换一篇就
    // 走一次。Chrome 实测读出来是 0、不抛,但 detach 之后属性访问规范上没给保证
    // (`room` 还要两级解引用到 `document.documentElement`);真抛起来
    // `closeReader()` 就断在这儿,阅读器关都关不掉 —— 代价不对称,兜着。
    get top() {
      try { return Math.max(0, win ? node.scrollY : node.scrollTop); }   // Safari 回弹给负值
      catch { return 0; }
    },
    get room() {
      try {
        return win ? node.document.documentElement.scrollHeight - node.innerHeight
          : node.scrollHeight - node.clientHeight;
      } catch { return 0; }
    },
    /** 这个元素在不在这条轴滚的那堆内容里面(口径⑧的那把尺子按它分档)。 */
    holds(el) {
      try {
        const root = win ? node.document.documentElement : node;
        return !!root && !!el && root.contains(el);
      } catch { return false; }
    },
  };
}

/**
 * 把一条滚动轴和它上面那几块「可以收起来」的东西绑在一起。
 *
 * @param {string|Element|Window} scroller 正文滚动容器;iframe 传它的 contentWindow
 * @param {(string|Element)[]} targets 要收起的元素,按页面上从上到下的顺序
 * @param {object} [opts]
 * @param {Element} [opts.visible] 「这块阅读区还在不在页面上」看谁(口径⑦)。
 *   跨文档时滚动轴在另一个 document 里、本页量不着,得指 iframe 元素。默认是轴自己。
 * @param {Element} [opts.host] 收起时在谁身上打 `has-scroll-away`(口径⑥)。
 * @returns {object|null} 绑定句柄(带 `release()`);元素不在页面上时返回 null(不抛)
 */
function bindScrollAway(scroller, targets, opts = {}) {
  const node = typeof scroller === 'string' ? $(scroller) : scroller;
  const picked = (targets || []).map(t => typeof t === 'string' ? $(t) : t).filter(Boolean);
  if (!node || !picked.length) return null;
  const axis = scrollAwayAxis(node);
  // `inside` 是**收起它会不会挪动轴上内容的坐标**(口径⑧)。这一页的结构在绑定
  // 期间不变,所以量一次就够;收藏详情每次重绘都重新绑,跟着一起重算。
  const blocks = picked.map(el => ({ el, inside: axis.holds(el), away: false, natural: 0, timer: 0 }));
  const state = {
    axis, targets: blocks, last: 0, carry: 0, gestureAt: 0, holding: false, off: [],
    settleUntil: 0, settleTimer: 0,
    visible: opts.visible || (axis.gestures === node ? node : null),
    host: opts.host || null,
  };
  const listen = (target, type, fn) => {
    target.addEventListener(type, fn, { passive: true });
    state.off.push(() => target.removeEventListener(type, fn));
  };
  // 口径②那道门。四类手势都要:滚轮、触摸、键盘(空格 / PageDown / 方向键)、
  // 拖滚动条。认手势而不是认「delta 大不大」—— 程序一次跳 180px,用户快速甩一下
  // 也能跳这么多,按幅度分不开。
  const mark = () => { state.gestureAt = Date.now(); };
  for (const type of ['wheel', 'touchmove', 'keydown']) listen(axis.gestures, type, mark);
  listen(axis.gestures, 'pointerdown', () => { state.holding = true; state.gestureAt = Date.now(); });
  // 松手未必落在轴上(拖着滚动条甩出去、拖出 iframe),所以挂顶层,两份都挂。
  for (const top of new Set([axis.outer, window]))
    for (const type of ['pointerup', 'pointercancel'])
      listen(top, type, () => { state.holding = false; state.gestureAt = Date.now(); });
  listen(axis.node, 'scroll', () => scrollAwayTick(state));
  // 口径⑦:不变量落在元素自己身上,不靠哪个调用点记得。`display:none` 之后
  // 尺寸归零会回调一次,这一下就把收起态撤干净。
  if (state.visible && typeof ResizeObserver === 'function') {
    const watch = new ResizeObserver(() => {
      if (!scrollAwayVisible(state)) resetScrollAwayState(state);
    });
    watch.observe(state.visible);
    state.off.push(() => watch.disconnect());
  }
  state.release = () => {
    resetScrollAwayState(state);
    for (const off of state.off) off();
    state.off = [];
    const at = SCROLL_AWAY.indexOf(state);
    if (at >= 0) SCROLL_AWAY.splice(at, 1);
  };
  SCROLL_AWAY.push(state);
  return state;
}

/** 这块阅读区此刻在不在页面上(口径⑦)。量不着参照物的一律当「在」,不乱复位。 */
function scrollAwayVisible(state) {
  const el = state.visible;
  return !el || (el.isConnected && el.getClientRects().length > 0);
}

/**
 * 这几块里有没有**此刻真的摆在页面上、要人处理**的告警(口径⑥)。
 *
 * 判据是 `role="alert"` 而不是「有没有提示」:同一条浮层里还装着进度和待配置
 * (`role="status"`),那两类不该把工具栏钉死在展开态。
 *
 * **「在不在页面上」要自己量,不能只问它的 `hidden`。** 手动收录那条投递栏是
 * 跟着来源整条 `display:none` 的,里面那句上一轮的失败提示自己并没有 hidden、
 * 角色也还挂着 —— 只看 `hidden` 的话,收录失败过一次之后,大V观点 / RSS /
 * 简报 / 网页专栏读长文时工具栏全都收不起来(四处实测)。量盒子同时**包含**
 * 了原来那道 `hidden` 判据(`hidden` 就是 `display:none`),而**被收起的那一块照旧
 * 量得出盒子**(`height:0 + overflow:hidden` 不改布局盒),所以「收起之后新来的
 * 告警要放出来」一字没变。
 */
function scrollAwayBlocked(state) {
  return state.targets.some(t => [...t.el.querySelectorAll('[role="alert"]')]
    .some(node => node.getClientRects().length > 0 && node.textContent.trim()));
}

/** 这几块加起来有多高(收起的按收起前量到的那一份算)。 */
function scrollAwayHeight(state) {
  return state.targets.reduce(
    (sum, t) => sum + (t.away ? t.natural : t.el.getBoundingClientRect().height), 0);
}

/** 轴上的内容此刻被我们自己收掉了多少(只有轴内的目标算数,口径⑧)。 */
function scrollAwayCollapsed(state) {
  return state.targets.reduce((sum, t) => sum + (t.away && t.inside ? t.natural : 0), 0);
}

/**
 * 判位置用的坐标(口径⑧):`scrollTop` **加上此刻被自己收掉的轴内高度**。
 *
 * 轴内的目标一收,浏览器会补偿 `scrollTop` 保住视口里那段文字;加回这一份,
 * 这个数在收起前后恒定,方向累计和「回到顶部」才有共同的尺子。目标在轴外时
 * (四处接入里的另外三处)这一项恒为 0,判据跟原来一字不差。
 */
function scrollAwayPosition(state) {
  return state.axis.top + scrollAwayCollapsed(state);
}

/** 一次滚动:按口径②③④⑧决定收起还是放出来。 */
function scrollAwayTick(state) {
  const position = scrollAwayPosition(state);
  const delta = position - state.last;
  state.last = position;
  // 口径⑧:动画跑的那几帧对不齐(高度渐变,而收掉的高度按整份算),只记不改判。
  // 排在「回到顶部」前面:那一支也会被吞掉,由 `scrollAwaySettled()` 补判。
  if (state.settleUntil && Date.now() < state.settleUntil) { state.carry = 0; return; }
  // 回到顶部一律放出来 —— 换一篇文章(`readSourceItem` 把 scrollTop 写回 0)
  // 也走这一条,不用在那边另外记着调我们。**这个方向不过手势那道门。**
  if (position <= AWAY_ENTER) { state.carry = 0; setScrollAway(state, false); return; }
  if (!delta) return;
  if ((delta > 0) !== (state.carry > 0)) state.carry = 0;   // 方向反了,重新攒
  state.carry += delta;
  if (state.carry >= AWAY_STEP) {
    // 口径②:手按着的全程都算他在滚;松开之后再给一段惯性。够不上的是程序自己
    // 改的(恢复阅读位置那一类),只对账、不改判 —— 收起它要的那块地方正好压着
    // 用户接下来要点的按钮。**判据只写在这一支上**,展开那一支永远放行。
    if (state.holding || Date.now() - state.gestureAt <= AWAY_GESTURE_MS) setScrollAway(state, true);
    else state.carry = 0;
  } else if (state.carry <= -AWAY_STEP) setScrollAway(state, false);
}

/** 收起 / 放出这一组。已经在那个状态就什么都不做。 */
function setScrollAway(state, away) {
  if (state.targets.every(t => t.away === away)) return;
  const height = scrollAwayHeight(state);     // 这一次涉及多少高度(改之前量)
  if (away) {
    if (scrollAwayBlocked(state)) return;                      // 里面有要人处理的告警
    const axis = state.axis, room = axis.room;
    if (room - height < AWAY_ROOM) return;                     // 整条轴太短
    if (room - axis.top < height + AWAY_FLOOR) return;         // 已经贴底
  }
  for (const t of state.targets) setScrollAwayTarget(t, away);
  if (state.host) state.host.classList.toggle('has-scroll-away', away);
  state.carry = 0;
  // 口径⑧:高度正在渐变,中途那几帧的坐标对不齐,先不改判。
  state.settleUntil = Date.now() + AWAY_MS;
  clearTimeout(state.settleTimer);
  state.settleTimer = setTimeout(() => scrollAwaySettled(state), AWAY_MS);
}

/**
 * 平息期一完,按**此刻的真实坐标**补判一次(口径⑧)。
 *
 * 期间的事件都被吞了,所以这一下必须补:用户要是在这期间滚回了顶上、或者换了
 * 一篇(`scrollTop` 被写回 0),他已经停在那儿 —— 不会再有下一次 scroll 事件来把
 * 这条头放出来。
 */
function scrollAwaySettled(state) {
  state.settleUntil = 0;
  state.last = scrollAwayPosition(state);
  state.carry = 0;
  if (state.last <= AWAY_ENTER) setScrollAway(state, false);
}

/** 单块的收起/展开动画。收起态留下 inline 的 height/overflow,展开跑完撤干净。 */
function setScrollAwayTarget(t, away) {
  if (t.away === away) return;
  const el = t.el;
  if (away) t.natural = el.getBoundingClientRect().height;
  t.away = away;
  el.style.overflow = 'hidden';
  el.style.height = (away ? t.natural : 0) + 'px';
  void el.offsetHeight;             // 让起点先落地,否则两次赋值被合并成一跳
  el.classList.toggle('is-away', away);
  el.style.height = (away ? 0 : t.natural) + 'px';
  // 收起的东西不许还能用 Tab 走进去(里面有按钮和输入框),`inert` 连点击一起管。
  if (away) el.setAttribute('inert', ''); else el.removeAttribute('inert');
  clearTimeout(t.timer);
  t.timer = setTimeout(() => clearScrollAwayStyle(t), AWAY_MS);
}

/** 展开跑完把 inline 撤掉;收起态要留着 —— height:0 和 overflow 正是收起本身。 */
function clearScrollAwayStyle(t) {
  if (t.away) return;
  t.el.style.height = '';
  t.el.style.overflow = '';
}

/** 一条轴复位成展开态,**不走动画**(隐藏元素量不出高度,动画只会留个半截高度)。 */
function resetScrollAwayState(state) {
  state.carry = 0;
  state.holding = false;
  state.gestureAt = 0;
  clearTimeout(state.settleTimer);
  state.settleUntil = 0;
  if (state.host) state.host.classList.remove('has-scroll-away');
  for (const t of state.targets) {
    clearTimeout(t.timer);
    t.away = false;
    t.el.classList.remove('is-away');
    t.el.removeAttribute('inert');
    t.el.style.height = '';
    t.el.style.overflow = '';
  }
  state.last = scrollAwayPosition(state);   // 放在后面:这时 collapsed 才归 0
}

/**
 * 按「这块阅读区现在什么处境」把收起态对一遍 —— 布局、可见性、提示内容变过之后调它。
 *
 * 跟 `resetScrollAway()` 的分别是**它看处境、不一刀切**:阅读区还在、也没有告警
 * 要处理时,收起态原样留着。后台同步完一轮会重绘这一页,那时候一刀切复位就是
 * 「读着读着工具栏自己弹回来」。
 */
function syncScrollAway() {
  for (const state of [...SCROLL_AWAY]) {
    if (!scrollAwayVisible(state)) resetScrollAwayState(state);      // 口径⑦
    else if (scrollAwayBlocked(state)) setScrollAway(state, false);  // 口径⑥
  }
}

/** 全部一刀切复位成展开态。只有「量过的高度整批作废」那一类场合用得上。 */
function resetScrollAway() {
  for (const state of SCROLL_AWAY) resetScrollAwayState(state);
}

// 窗口尺寸一变,工具栏折不折行、折几行都可能换一个答案,量过的那份高度跟着作废。
// 复位比重新量省事,也更稳:用户接着往下滚一下就又收起来了。
window.addEventListener('resize', resetScrollAway);
