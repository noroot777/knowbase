// Workspace navigation, addressable filters and accessible overlay lifecycle.
const WORKSPACE_TITLES = { gallery: '工具图鉴', search: '全文检索', ask: '知识问答', fav: '我的收藏', daily: '每日日报', subs: '我的订阅', pipe: '同步中心', care: '问题与待办' };
let activeTab = 'gallery';
let restoringLocation = false;
// 地址栏里那个 #tab 在 initWorkspace 收尾时才被读走(restoreWorkspaceLocation),
// 而 loadGallery() 是和 initWorkspace 并排起跑的:它的接口先回来时
// applyGalleryFilter() 会顺手写一次地址栏,把 #daily / #pipe 覆盖成 #gallery,
// 深链当场失效、落到图鉴上。两条路都是网络往返,谁先回来纯看运气(无头浏览器里
// 实测四次红一次)。首次定位完成之前一律不写地址栏:那时候地址栏才是权威。
let workspaceLocated = false;
// **首次定位之前人就点了导航**(刷新后立刻点侧栏那一下):那一下比地址栏里这一份
// **新**,所以定位不许把它顶掉 —— 而它偏偏写不进地址栏(上面那条:定位完成前谁都
// 不许写),于是定位读到的还是旧地址,`switchTab` 当场把人点的那一下换走。
// 后果不是「慢」而是**卡住**:来源面板的装载有一道 `sourceActive()`
// (`sources.js`,判据含 `activeTab === 'subs'`),切走之后它直接 return,
// 面板的数据请求**一次都没发出去** —— 投递栏已经显示了、按钮还停在「同步全部」,
// 页面一个报错都没有。实测:满载下点一次「存链接」就复现(无头探针里第 1 次就红),
// 空载约 40 次红 1 次。事故形态和回归见 `docs/decisions/link-grab.md`。
let workspaceNavigated = false;
const tabScroll = new Map();
const compactNavigation = matchMedia('(max-width:900px)');

function saveWorkspaceLocation(push = false) {
  if (restoringLocation || !workspaceLocated) return;
  const params = new URLSearchParams();
  if (activeTab === 'gallery') {
    for (const [key, value] of Object.entries(gState)) {
      if (value && !(key === 'sort' && value === 'recent')) params.set(key, value);
    }
  }
  const hash = '#' + activeTab + (params.size ? '?' + params : '');
  if (location.hash !== hash) history[push ? 'pushState' : 'replaceState'](null, '', location.pathname + location.search + hash);
}

function updateWorkspaceTab(tab, previousScroll = window.scrollY) {
  const changed = tab !== activeTab;
  // 人自己点的那一下才算(定位自己走到这儿不算,否则它永远是"人点过的")
  if (!workspaceLocated && !restoringLocation) workspaceNavigated = true;
  tabScroll.set(activeTab, previousScroll);
  activeTab = tab;
  $('page-crumb').textContent = WORKSPACE_TITLES[tab];
  document.title = WORKSPACE_TITLES[tab] + ' · Knowbase';
  for (const key of Object.keys(WORKSPACE_TITLES)) {
    if (key === tab) $('tab-' + key).setAttribute('aria-current', 'page');
    else $('tab-' + key).removeAttribute('aria-current');
  }
  toggleNavigation(false);
  saveWorkspaceLocation(changed);
  if (changed) window.scrollTo({ top: tabScroll.get(tab) || 0, behavior: 'instant' });
}

function restoreWorkspaceLocation() {
  const [tabName, query = ''] = location.hash.slice(1).split('?');
  // 老深链 #wechat/#social/#rss 仍然认:switchTab 会把它落到「我的订阅」的对应视图
  const known = Object.hasOwn(WORKSPACE_TITLES, tabName)
    || (typeof subsCatalog !== 'undefined' && subsCatalog.some(m => m.id === tabName));
  // **人已经点过的那一下赢**(见 `workspaceNavigated`):只认他选的那个标签页,
  // 地址栏里的图鉴筛选照旧读 —— 他点的正是图鉴时,深链上那几个筛选不该丢。
  const chosen = workspaceNavigated && !workspaceLocated;
  const tab = chosen ? activeTab : (known ? tabName : 'gallery');
  restoringLocation = true;
  workspaceLocated = true;   // 地址已经读完,之后各面板再存位置就不会盖掉它
  if (tab === 'gallery') {
    clearTimeout(gQTimer);
    const params = new URLSearchParams(query);
    for (const key of ['q', 'kind', 'topic']) gState[key] = params.get(key) || '';
    gState.src = ['gzh', 'video'].includes(params.get('src')) ? params.get('src') : '';
    gState.sort = ['recent', 'n', 'first'].includes(params.get('sort')) ? params.get('sort') : 'recent';
    syncGalleryControls();
    applyGalleryFilter();
  }
  // 人点的那一下已经把标签页切好了,不必再切一遍(再切会让来源面板重装一轮)
  if (!chosen) switchTab(tab);
  restoringLocation = false;
  // 定位期间写地址栏被挡着(`saveWorkspaceLocation` 开头那道),这里把末态落进去。
  // 正常那条路上地址栏本来就等于末态,那一句自带「不一样才写」,是个空操作。
  saveWorkspaceLocation();
}

function toggleNavigation(open) {
  const show = compactNavigation.matches && (open ?? !document.body.classList.contains('nav-open'));
  document.body.classList.toggle('nav-open', show);
  $('nav-mask').hidden = !show;
  document.querySelector('.mobile-menu').setAttribute('aria-expanded', String(show));
  if (show) {
    $('sidebar').setAttribute('role', 'dialog');
    $('sidebar').setAttribute('aria-modal', 'true');
  } else {
    $('sidebar').removeAttribute('role');
    $('sidebar').removeAttribute('aria-modal');
  }
  syncOverlays();
}

/* ── 导轨上的名字 ──────────────────────────────────────────────────────────
   宽屏导轨只剩图标,名字在悬停或键盘聚焦时推到栏外。文字本身就是按钮里那段
   .nav-text(屏幕阅读器读的也是它),这里只负责定位:#sidebar 开着
   overflow-y: auto(矮窗口得能滚到底),而「某一个轴不是 visible 就两个轴一起
   裁」——纯 CSS 的绝对定位浮层会被它连横向一起切掉,所以浮层挂在 body 上、
   位置现算。窄屏抽屉里名字本来就显示着,不走这条路。 */
const NAV_ITEM = '.tabs button,.explore-links a,.nav-dropdown summary';
// 逗号分隔的选择器列表后面直接缀 :hover 只会命中最后一项,得逐项缀
const NAV_HOVER = NAV_ITEM.split(',').map(sel => sel + ':hover').join(',');
const navItemOf = node => (node instanceof Element ? node.closest(NAV_ITEM) : null);
/* 悬停和键盘聚焦是两条独立的来路,各记各的、谁在就显示谁。合成一个「当前锚点」
   的话两者会互相拆台:点一下按钮(焦点留在它上面)再把鼠标挪开,鼠标那次隐藏
   因为锚点已经被 focus 改写而被当成过期请求丢掉,浮层就一直挂在那儿不走。 */
let navTipHover = null, navTipFocus = null;

function syncNavTip() {
  const tip = $('nav-tip');
  const item = navTipHover || navTipFocus;
  // details 展开时设置浮层就在同一个位置上,那时不必再说一遍名字
  const text = item && !compactNavigation.matches && !item.closest('details[open]')
    ? item.querySelector('.nav-text')?.textContent.trim() : '';
  if (!text) { tip.hidden = true; return; }
  tip.textContent = text;
  tip.hidden = false;
  const box = item.getBoundingClientRect();
  tip.style.left = Math.round(box.right + 8) + 'px';
  tip.style.top = Math.round(box.top + (box.height - tip.offsetHeight) / 2) + 'px';
}

/* 两种来路都从 DOM 现问一次,再重算位置。用在三个时机上:
   ①侧栏自己滚动——键盘 Tab 到露在外面的入口时浏览器会自动滚它,锚点没变、
     只是位置变了,原来在这里把两个来源一起清掉,于是焦点还在那个入口上、
     却再也不会有第二次 focusin 把名字带回来(矮窗口下连着 Tab 必现);
   ②窗口改尺寸;③监听刚装上、以及设置菜单开合——那时根本没有事件可等,
     只能照当下的 :hover / :focus-visible 补一次。 */
function resyncNavTip() {
  const rail = $('sidebar');
  navTipHover = rail.querySelector(NAV_HOVER);
  const focused = navItemOf(document.activeElement);
  navTipFocus = focused?.matches(':focus-visible') ? focused : null;
  syncNavTip();
}

function watchNavTips() {
  const rail = $('sidebar');
  // 事件代理:来源那几个 tab 是接口回来之后才插进去的。在同一个入口内部挪动
  // (图标 ↔ 按钮)也会冒一对 over/out,比一次 relatedTarget 滤掉,否则浮层会闪。
  rail.addEventListener('pointerover', e => {
    const item = navItemOf(e.target);
    if (item !== navItemOf(e.relatedTarget)) { navTipHover = item; syncNavTip(); }
  });
  rail.addEventListener('pointerout', e => {
    const item = navItemOf(e.target);
    if (item && item !== navItemOf(e.relatedTarget)) { navTipHover = null; syncNavTip(); }
  });
  rail.addEventListener('pointerleave', () => { navTipHover = null; syncNavTip(); });
  // 只认键盘聚焦:鼠标点完焦点也留在按钮上,那时名字已经由悬停给出了
  rail.addEventListener('focusin', e => {
    const item = navItemOf(e.target);
    navTipFocus = item?.matches(':focus-visible') ? item : null;
    syncNavTip();
  });
  rail.addEventListener('focusout', () => { navTipFocus = null; syncNavTip(); });
  rail.addEventListener('scroll', resyncNavTip);
  window.addEventListener('resize', resyncNavTip);
  // 监听是在任何网络等待之前装的,但光标和焦点此刻可能已经停在某个入口上了
  resyncNavTip();
}

function focusWorkspaceSearch() {
  if (!['gallery', 'search', 'fav'].includes(activeTab)) switchTab('search');
  toggleNavigation(false);
  if (activeTab === 'fav' && favActive && matchMedia('(max-width: 760px)').matches) closeFavDetail();
  const input = $(({ gallery: 'g-q', search: 'q', fav: 'fav-search' })[activeTab]);
  input.focus(); input.select();
}

function searchExample(query) { $('q').value = query; doSearch(); }
function toggleChatHistory() {
  const show = $('askpanel').classList.toggle('history-open');
  $('chat-history-toggle').setAttribute('aria-expanded', String(show));
}
function openShortcuts() { $('shortcuts-modal').style.display = ''; }
function closeShortcuts() { $('shortcuts-modal').style.display = 'none'; }

const overlayClosers = {
  'tool-drawer': closeToolDrawer, reader: closeReader, 'pipe-modal': closePipeModal,
  'stages-modal': closeStages, 'keys-modal': closeKeys,
  'mcp-modal': closeMcp, 'shortcuts-modal': closeShortcuts,
  'source-editor': closeSourceEditor, 'source-reader': () => { $('source-reader').style.display = 'none'; },
  'wechat-modal': closeWechatSetup,
  sidebar: () => toggleNavigation(false),
};
let overlayTop = null;
const focusReturns = new WeakMap();
const focusableSelector = 'a[href],button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),summary,iframe,[tabindex="0"]';
function visibleFocusables(el) {
  return [...el.querySelectorAll(focusableSelector)].filter(x => x.getClientRects().length && !x.closest('[inert]'));
}
function focusOverlay(el) {
  const target = visibleFocusables(el)[0] || el;
  if (target === el) el.tabIndex = -1;
  target.focus({ preventScroll: true });
}

function syncOverlays() {
  const dialogs = [...document.querySelectorAll('.modal-mask,#tool-drawer,#reader,#cap-drawer')];
  const visible = dialogs.filter(el => el.style.display !== 'none' && el.isConnected);
  if (document.body.classList.contains('nav-open') && compactNavigation.matches) visible.push($('sidebar'));
  visible.sort((a, b) => Number(getComputedStyle(a).zIndex) - Number(getComputedStyle(b).zIndex));
  const next = visible.at(-1) || null;
  const prev = overlayTop;
  if (next && next !== prev && !focusReturns.has(next)) focusReturns.set(next, document.activeElement);
  overlayTop = next;
  // Sidebar and mobile header are siblings; never inert an active dialog ancestor.
  for (const el of [$('main-content'), $('sidebar'), $('topbar'), ...dialogs]) el.inert = !!next && el !== next;
  if (!next && compactNavigation.matches) $('sidebar').inert = true;
  document.body.classList.toggle('overlay-open', !!next);
  if (next === prev) return;
  if (prev && !visible.includes(prev)) {
    const target = focusReturns.get(prev);
    focusReturns.delete(prev);
    if (target?.isConnected && !target.closest('[inert]') && target.getClientRects().length) target.focus({ preventScroll: true });
  }
  if (next && !next.contains(document.activeElement)) focusOverlay(next);
}

document.addEventListener('keydown', e => {
  if (e.isComposing) return;
  const menu = document.querySelector('.nav-dropdown[open]');
  if (e.key === 'Escape' && menu && (!overlayTop || overlayTop.id === 'sidebar')) {
    e.preventDefault(); menu.open = false;
    menu.querySelector('summary').focus(); return;
  }
  if (overlayTop) {
    if (e.key === 'Escape' && !overlayTop.classList.contains('confirm-mask')) {
      e.preventDefault(); e.stopImmediatePropagation();
      if (overlayTop.id === 'pipe-modal' && $('pipe-range-pop').style.display !== 'none') { closePipeRange(); $('pipe-range-btn').focus(); }
      else overlayClosers[overlayTop.id]?.();
    } else if (e.key === 'Tab') {
      const items = visibleFocusables(overlayTop);
      if (!items.length) { e.preventDefault(); return; }
      if (e.shiftKey && (document.activeElement === items[0] || !items.includes(document.activeElement))) {
        e.preventDefault(); items.at(-1).focus();
      } else if (!e.shiftKey && document.activeElement === items.at(-1)) {
        e.preventDefault(); items[0].focus();
      }
    }
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); focusWorkspaceSearch(); }
  else if (e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey && !e.target.closest('input,textarea,select,[contenteditable]')) {
    e.preventDefault(); focusWorkspaceSearch();
  }
}, true);

document.addEventListener('click', e => {
  document.querySelectorAll('.nav-dropdown[open]').forEach(menu => {
    if (!menu.contains(e.target)) menu.open = false;
  });
});
document.querySelectorAll('.nav-dropdown').forEach(menu => {
  menu.addEventListener('toggle', () => {
    if (menu.open) document.querySelectorAll('.nav-dropdown[open]').forEach(other => {
      if (other !== menu) other.open = false;
    });
    // 展开之后设置浮层就铺在名字浮层的位置上,把收录统计压掉一截;
    // syncNavTip 自己认 details[open],但开合不发 pointer/focus 事件,得主动补一次
    resyncNavTip();
  });
});

document.addEventListener('focusin', e => {
  if (overlayTop && !overlayTop.contains(e.target)) focusOverlay(overlayTop);
});

async function initWorkspace() {
  // 导轨上没有常驻标签,名字只剩这一条路——它必须在任何网络等待之前就位。
  // 排在 initSourceModules() 后面的话,来源目录慢(pipeJson 默认超时 15 秒)或者
  // 干脆超时的那段时间里,工具列表已经能翻了,而侧栏每一个入口都说不出自己叫
  // 什么;请求回来也救不了已经停在那儿的那次悬停(没有新的 pointerover)。
  watchNavTips();
  // 来源目录要先就位:restoreWorkspaceLocation 认老深链(#social 等)靠的是它。
  // 导轨入口是静态的,目录读不到也不影响侧栏,失败显示在「我的订阅」页内。
  await initSourceModules();
  $('reader-frame').addEventListener('load', () => {
    try {
      $('reader-frame').contentDocument?.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !e.isComposing) { e.preventDefault(); closeReader(); }
      });
    } catch { /* An external document cannot share keyboard events. */ }
  });
  document.querySelectorAll('.modal-mask > .modal').forEach((el, index) => {
    el.setAttribute('role', 'dialog'); el.setAttribute('aria-modal', 'true');
    const heading = el.querySelector('h2');
    if (heading) { heading.id ||= 'dialog-title-' + index; el.setAttribute('aria-labelledby', heading.id); }
  });
  const observer = new MutationObserver(syncOverlays);
  document.querySelectorAll('.modal-mask,#tool-drawer,#reader').forEach(el => observer.observe(el, { attributes: true, attributeFilter: ['style'] }));
  observer.observe(document.body, { childList: true });
  window.addEventListener('hashchange', restoreWorkspaceLocation);
  compactNavigation.addEventListener('change', () => toggleNavigation(false));
  try { setGalleryView(localStorage.getItem('kb-gallery-view') === 'list' ? 'list' : 'grid'); } catch {}
  restoreWorkspaceLocation();
  syncOverlays();
  refreshPipe(); // Discover work started by the scheduler or another tab, even on the gallery.
  refreshDailyStatus(); // 同理:定时日报可能正在跑,侧栏那颗点要立刻亮起来。
}
