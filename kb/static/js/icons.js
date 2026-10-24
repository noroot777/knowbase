// Local outline icons: no font download or third-party request.
const ICON_PATHS = {
  rss: '<circle cx="5" cy="19" r="1"/><path d="M4 11a9 9 0 0 1 9 9M4 4a16 16 0 0 1 16 16"/>',
  chat: '<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5a9.5 9.5 0 0 1 19 0Z"/><path d="M7 9h9M7 13h6"/>',
  library: '<path d="M4 5h5v14H4zM10 3h5v16h-5zM17 6l4-1 3 13-4 1z"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  spark: '<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3ZM20 2v4M18 4h4"/>',
  bookmark: '<path d="M6 4h12v17l-6-4-6 4V4Z"/>',
  sync: '<path d="M20 10a8 8 0 0 0-14-5L3 8M3 3v5h5M4 14a8 8 0 0 0 14 5l3-3M21 21v-5h-5"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  pencil: '<path d="M16 3a2.8 2.8 0 0 1 4 4L7 20l-5 2 2-5L16 3ZM14 5l4 4"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  network: '<circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="5" r="2.5"/><circle cx="13" cy="19" r="2.5"/><path d="m7.5 6 9-1M6 8l6 9M18 7l-4 10"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 17l10 5 10-5"/>',
  orbit: '<circle cx="12" cy="12" r="3"/><ellipse cx="12" cy="12" rx="10" ry="5" transform="rotate(-40 12 12)"/>',
  // 齿轮:8 齿等分,齿顶 R=9.3 / 齿根 r=7,齿顶半角 10°、齿根半角 16°(齿根略宽
  // 的梯形齿)。原来那条是手画的折线,起点和终点对不上、齿也不等分,看着就像
  // 缺了一块——齿轮是旋转对称的形状,少一齿或齿距不匀一眼就能看出来,所以这条
  // 路径是按极坐标算出来的,别手改数字。
  settings: '<path d="M10.4 2.8A9.3 9.3 0 0 1 13.6 2.8L13.9 5.3A7 7 0 0 1 15.4 5.9L17.3 4.4A9.3 9.3 0 0 1 19.6 6.7L18.1 8.6A7 7 0 0 1 18.7 10.1L21.2 10.4A9.3 9.3 0 0 1 21.2 13.6L18.7 13.9A7 7 0 0 1 18.1 15.4L19.6 17.3A9.3 9.3 0 0 1 17.3 19.6L15.4 18.1A7 7 0 0 1 13.9 18.7L13.6 21.2A9.3 9.3 0 0 1 10.4 21.2L10.1 18.7A7 7 0 0 1 8.6 18.1L6.7 19.6A9.3 9.3 0 0 1 4.4 17.3L5.9 15.4A7 7 0 0 1 5.3 13.9L2.8 13.6A9.3 9.3 0 0 1 2.8 10.4L5.3 10.1A7 7 0 0 1 5.9 8.6L4.4 6.7A9.3 9.3 0 0 1 6.7 4.4L8.6 5.9A7 7 0 0 1 10.1 5.3Z"/><circle cx="12" cy="12" r="3"/>',
  chevron: '<path d="m10 6.5 5.5 5.5-5.5 5.5"/>',
  arrow: '<path d="M4.5 12h15m-6.2-6.2L19.5 12l-6.2 6.2"/>',
  keyboard: '<rect x="2" y="5" width="20" height="14" rx="3"/><path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M6 12h.01M10 12h.01M14 12h.01M18 12h.01M7 15h10"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  list: '<path d="M9 5h12M9 12h12M9 19h12M3 5h1M3 12h1M3 19h1"/>',
  code: '<path d="m8 7-5 5 5 5m8-10 5 5-5 5M14 4l-4 16"/>',
  box: '<path d="m12 3 9 5v9l-9 5-9-5V8l9-5ZM3 8l9 5 9-5M12 13v9M7.5 5.5l9 5"/>',
  paper: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 8h3M9 12h6M9 16h6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6.8V12l3.6 2.1"/>',
};
function icon(name, cls = '') {
  return `<svg class="icon ${cls}" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name] || ICON_PATHS.box}</svg>`;
}

/* ── 行内箭头 ──────────────────────────────────────────────────────────────
   一份几何、一个尺寸单位(1em,跟着所在文字缩放)。原来各处直接写字符
   ↗ ↑ ↓ → ▾ ▴:这些字在 PingFang SC 里本身就细长歪斜,缺字时还会回退到系统
   emoji 字体,于是同一行里的箭头粗细、长度、基线各不相同,跟这套 1.4px 描边的
   图标更不是一家。要加新方向就往这张表里加,别再在页面里写字符。          */
const ARROW_PATHS = {
  left: 'M9.5 6h-7M5.3 3.3 2.6 6l2.7 2.7',
  ext: 'M2.9 9.1 9.1 2.9M4.9 2.9h4.2v4.2',    // 外链:右上
  right: 'M2.5 6h7M6.7 3.3 9.4 6 6.7 8.7',    // 站内前往
  up: 'M6 9.4V2.6M3.3 5.3 6 2.6l2.7 2.7',     // 回到顶部
  down: 'M6 2.6v6.8M3.3 6.7 6 9.4l2.7-2.7',   // 跳到最新
  caretDown: 'm2.9 4.7 3.1 3.1 3.1-3.1',      // 展开/下拉
  caretUp: 'm2.9 7.3 3.1-3.1 3.1 3.1',        // 收起
};
function arrow(name) {
  const cls = name === 'ext' ? 'ia ia-ext' : 'ia';
  return `<svg class="${cls}" width="1em" height="1em" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ARROW_PATHS[name] || ARROW_PATHS.right}"/></svg>`;
}

function renderIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(el => { el.outerHTML = icon(el.dataset.icon); });
  root.querySelectorAll('[data-arrow]').forEach(el => { el.outerHTML = arrow(el.dataset.arrow); });
}
renderIcons();
