// ── 通用反馈层:轻提示 toast / 确认框 / 接口错误文案 ────────────────────────
// 全站不用浏览器原生 alert/confirm:样式不受控(系统弹窗跟瓷白×墨蓝黑的调子不搭)、
// 阻塞主线程、丢上下文。这里的三个函数是唯一出口,新代码一律走这儿。
// z 层级(样式在 base.css):顶栏 20 < 抽屉 30 < 阅读器 40 < 弹窗 50 < 确认框 80 < toast 90

const TOAST_ICON = { err: '✗', ok: '✓', info: '·' };
const TOAST_MS = { err: 6000, ok: 3200, info: 4500 };
const TOAST_MAX = 4;

function toastHost() {
  let h = document.getElementById('toast-host');
  if (!h) {
    h = document.createElement('div');
    h.id = 'toast-host';
    h.setAttribute('role', 'status');
    h.setAttribute('aria-live', 'polite');
    document.body.appendChild(h);
  }
  return h;
}

/** 轻提示。text=一句话结论,detail=次要说明(小字,可长)。type: err|ok|info */
function toast(text, { type = 'err', detail = '', ms, key = '' } = {}) {
  const host = toastHost();
  if (key) dismissToastGroup(key);
  while (host.children.length >= TOAST_MAX) host.firstElementChild.remove();

  const el = document.createElement('div');
  el.className = 'toast ' + type;
  if (key) el.dataset.toastKey = key;
  el.innerHTML =
    `<span class="toast-ic">${TOAST_ICON[type] || ''}</span>` +
    '<div class="toast-body"><b></b></div>' +
    `<button class="toast-x" aria-label="关闭">✕</button>`;
  el.querySelector('b').textContent = text;
  if (detail) {
    const description = document.createElement('span');
    description.textContent = detail;
    el.querySelector('.toast-body').appendChild(description);
  }
  host.appendChild(el);

  let timer = null;
  const close = () => {
    if (!el.isConnected) return;
    clearTimeout(timer);
    el.classList.add('out');
    setTimeout(() => el.remove(), 160);
  };
  el.querySelector('.toast-x').onclick = close;
  timer = setTimeout(close, ms ?? TOAST_MS[type] ?? 4500);
  return close;
}

function dismissToastGroup(key) {
  document.querySelectorAll('[data-toast-key]').forEach(el => {
    if (el.dataset.toastKey === key) el.remove();
  });
}

/** 确认框(取代原生 confirm)。返回 Promise<boolean>,Esc / 点遮罩 = 取消 */
function confirmBox({ title, body = '', ok = '确定', cancel = '取消', danger = false }) {
  return new Promise(resolve => {
    const prevFocus = document.activeElement;
    const mask = document.createElement('div');
    mask.className = 'modal-mask confirm-mask';
    mask.innerHTML =
      `<div class="modal confirm" role="dialog" aria-modal="true">` +
      `<h2></h2>` +
      `${body ? '<div class="hint confirm-body"></div>' : ''}` +
      `<div class="confirm-acts">` +
      `<button class="ghost" data-act="cancel"></button>` +
      `<button class="${danger ? 'danger' : ''}" data-act="ok"></button>` +
      `</div></div>`;
    mask.querySelector('h2').textContent = title;
    if (body) mask.querySelector('.confirm-body').textContent = body;
    mask.querySelector('[data-act=cancel]').textContent = cancel;
    mask.querySelector('[data-act=ok]').textContent = ok;

    const done = v => {
      // 捕获阶段监听:Esc 先给确认框,不会穿透到 core.js 那条「阅读器→抽屉→弹窗」的关闭链
      document.removeEventListener('keydown', onKey, true);
      mask.remove();
      if (prevFocus && prevFocus.focus) prevFocus.focus();
      resolve(v);
    };
    const onKey = e => {
      if (e.key === 'Escape') { e.stopPropagation(); done(false); }
      else if (e.key === 'Enter') { e.stopPropagation(); done(true); }
    };

    mask.onclick = e => { if (e.target === mask) done(false); };
    mask.querySelector('[data-act=cancel]').onclick = () => done(false);
    mask.querySelector('[data-act=ok]').onclick = () => done(true);
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(mask);
    mask.querySelector('[data-act=ok]').focus();
  });
}

/** 惯用简写:响应不 ok 时弹 toast 并返回 true(调用方据此 return) */
async function toastIfFail(r, text) {
  if (r.ok) return false;
  toast(text, { detail: await apiFail(r) });
  return true;
}
