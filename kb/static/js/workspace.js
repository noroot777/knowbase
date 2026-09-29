// Workspace navigation, addressable filters and accessible overlay lifecycle.
const WORKSPACE_TITLES = { gallery: '工具图鉴', search: '全文检索', ask: '知识问答', fav: '我的收藏', pipe: '同步中心' };
let activeTab = 'gallery';
let restoringLocation = false;
// 首次定位完成前谁都不许写地址栏:图鉴的数据常常比 initWorkspace 里那次
// restoreWorkspaceLocation 先回来,applyGalleryFilter 一存位置就把深链接
// (#pipe 这种)按默认的 activeTab 覆写成 #gallery,用户直接打开同步中心会被弹回图鉴。
let workspaceLocated = false;
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
  const tab = Object.hasOwn(WORKSPACE_TITLES, tabName) ? tabName : 'gallery';
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
  switchTab(tab);
  restoringLocation = false;
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

function focusWorkspaceSearch() {
  if (activeTab !== 'gallery' && activeTab !== 'search') switchTab('search');
  toggleNavigation(false);
  const input = $(activeTab === 'gallery' ? 'g-q' : 'q');
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
  'tgt-modal': closeTargets, 'stages-modal': closeStages, 'keys-modal': closeKeys,
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
  const dialogs = [...document.querySelectorAll('.modal-mask,#tool-drawer,#reader')];
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
  });
});

document.addEventListener('focusin', e => {
  if (overlayTop && !overlayTop.contains(e.target)) focusOverlay(overlayTop);
});

async function initWorkspace() {
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
}
