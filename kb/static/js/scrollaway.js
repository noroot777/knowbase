// ── 读正文时让出位置:往下滚,正文上面那条工具栏收起来 ────────────────────────
//
// **为什么只有某些面板需要这一层。** 整页滚动的面板(每日日报、全文检索、同步
// 中心)页头本来就跟着整页滚走,一个字都不用改;会卡住的是**高度锁在可用视口上**
// 的那一类 —— 「我的订阅」的阅读区就是(`body.subs-reading`,来由见
// docs/decisions/subs-layout.md):页头每多一行,正文就少一行,而它上面顶着的是
// 来源工具栏(60px)加手动收录的投递栏(95px)。滚不动它,也挤不走它。
//
// 口径五条,都是实测/构造上吃过亏的地方:
//
// ① **只认正文那一条滚动轴。** 内容列表自己滚的时候不收 —— 那不是在读。
// ② **收起只认用户自己滚的那一下。** 程序改 `scrollTop` 照样发 scroll 事件,而
//    这一页到处都在改它:换一篇文章写回 0、从「订阅管理」切回阅读时恢复上次停
//    在哪儿。后一种会被当成「他正在往下读」,工具栏当场收起 —— 可用户刚回到这
//    一页、手都没动过,紧跟着要点的那颗按钮恰好就在收起的那块里,**点不中**
//    (2026-10-17 `subs_usability_checks` 恢复 180px 阅读位置那一步实测:
//    `click('#source-view-manage')` 落空,视图没切过去,后面整串检查连环超时)。
//    所以收起要有一次真手势垫底(滚轮/触摸/键盘/拖滚动条);**放出来不设这道门**
//    —— 那个方向永远安全,换文章时正该靠它自动展开。
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
//
// 高度为什么由 JS 写进 inline:`height:auto` 做不了过渡,得给它一个具体的起点和
// 终点。收起态留着 inline 的 `height:0`,展开动画跑完就把 inline 那几条撤干净,
// 元素回到自然布局 —— 之后窗口折行、提示文字变长变短都不用谁来同步。
// 配套样式(过渡曲线,以及收起时要跟着收掉的 padding / 边线 / min-height)在
// `kb/static/css/base.css` 的 `.scroll-away`。

/** 绑过的滚动轴。`resetScrollAway()` 要能一把全复位,所以存着。 */
const SCROLL_AWAY = [];
/** 滚过这么多才允许收起:正文开头附近来回挪几下不该让工具栏跟着闪。 */
const AWAY_ENTER = 64;
/** 同方向累计这么多像素才改判(口径②)。 */
const AWAY_STEP = 20;
/** 收起之后至少要剩这么多可滚距离,否则这篇太短、不值得收(口径③)。 */
const AWAY_ROOM = 160;
/** 离底不足「收起的高度 + 这一点余量」时不收 —— 收了 scrollTop 会被钳(口径④)。 */
const AWAY_FLOOR = 24;
/** 一次真手势之后多久内的滚动还算他自己滚的(口径②;惯性滚动要够用)。 */
const AWAY_GESTURE_MS = 700;
/** 清理 inline 样式的兜底时限,比 base.css 里那条 .2s 过渡长一截。 */
const AWAY_MS = 280;

/**
 * 把一条滚动轴和它上面那几块「可以收起来」的东西绑在一起。
 *
 * @param {string} scrollerId 正文滚动容器的 id
 * @param {string[]} targetIds 要收起的元素 id,按它们在页面上从上到下的顺序
 * @returns {object|null} 绑定状态;元素不在页面上时返回 null(不抛)
 */
function bindScrollAway(scrollerId, targetIds) {
  const scroller = $(scrollerId);
  const targets = (targetIds || []).map(id => $(id)).filter(Boolean)
    .map(el => ({ el, away: false, natural: 0, timer: 0 }));
  if (!scroller || !targets.length) return null;
  const state = { scroller, targets, last: 0, carry: 0, gestureAt: 0 };
  // 口径②的那道门。四种都要:滚轮、触摸、键盘(空格 / PageDown / 方向键)、
  // 拖滚动条(pointerdown 落在这个元素上)。认手势而不是认「delta 大不大」——
  // 程序一次跳 180px,用户快速甩一下也能跳这么多,按幅度分不开。
  const mark = () => { state.gestureAt = Date.now(); };
  for (const type of ['wheel', 'touchmove', 'keydown', 'pointerdown'])
    scroller.addEventListener(type, mark, { passive: true });
  scroller.addEventListener('scroll', () => scrollAwayTick(state), { passive: true });
  SCROLL_AWAY.push(state);
  return state;
}

/** 这几块加起来有多高(收起的按收起前量到的那一份算)。 */
function scrollAwayHeight(state) {
  return state.targets.reduce(
    (sum, t) => sum + (t.away ? t.natural : t.el.getBoundingClientRect().height), 0);
}

/** 一次滚动:按口径②③④决定收起还是放出来。 */
function scrollAwayTick(state) {
  const el = state.scroller;
  const top = Math.max(0, el.scrollTop);      // Safari 回弹会给负值
  const delta = top - state.last;
  state.last = top;
  // 回到顶部一律放出来 —— 换一篇文章(`readSourceItem` 把 scrollTop 写回 0)
  // 也走这一条,不用在那边另外记着调我们。**这个方向不过手势那道门。**
  if (top <= AWAY_ENTER) { state.carry = 0; setScrollAway(state, false); return; }
  // 口径②:没有真手势垫底的,就是程序自己改的(恢复阅读位置那一类),只对账、
  // 不改判 —— 收起它要的那块地方正好压着用户接下来要点的按钮。
  if (Date.now() - state.gestureAt > AWAY_GESTURE_MS) { state.carry = 0; return; }
  if (!delta) return;
  if ((delta > 0) !== (state.carry > 0)) state.carry = 0;   // 方向反了,重新攒
  state.carry += delta;
  if (state.carry >= AWAY_STEP) setScrollAway(state, true);
  else if (state.carry <= -AWAY_STEP) setScrollAway(state, false);
}

/** 收起 / 放出这一组。已经在那个状态就什么都不做。 */
function setScrollAway(state, away) {
  if (state.targets.every(t => t.away === away)) return;
  if (away) {
    const el = state.scroller, height = scrollAwayHeight(state);
    const room = el.scrollHeight - el.clientHeight;
    if (room - height < AWAY_ROOM) return;                     // 整条轴太短
    if (room - Math.max(0, el.scrollTop) < height + AWAY_FLOOR) return;  // 已经贴底
  }
  for (const t of state.targets) setScrollAwayTarget(t, away);
  state.carry = 0;
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

/**
 * 全部复位成展开态,**不走动画**。
 *
 * 换来源、切「订阅管理」、切走这个 tab 都调它:那几下本来就在重排,而且阅读区
 * 这时可能是隐藏的 —— 隐藏元素量不出高度,动画只会留下一个半截的 inline 高度。
 */
function resetScrollAway() {
  for (const state of SCROLL_AWAY) {
    state.carry = 0;
    state.last = Math.max(0, state.scroller.scrollTop);
    for (const t of state.targets) {
      clearTimeout(t.timer);
      t.away = false;
      t.el.classList.remove('is-away');
      t.el.removeAttribute('inert');
      t.el.style.height = '';
      t.el.style.overflow = '';
    }
  }
}

// 窗口尺寸一变,工具栏折不折行、折几行都可能换一个答案,量过的那份高度跟着作废。
// 复位比重新量省事,也更稳:用户接着往下滚一下就又收起来了。
window.addEventListener('resize', resetScrollAway);
