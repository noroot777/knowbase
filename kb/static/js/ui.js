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
function toast(text, { type = 'err', detail = '', ms } = {}) {
  const host = toastHost();
  while (host.children.length >= TOAST_MAX) host.firstElementChild.remove();

  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.innerHTML =
    `<span class="toast-ic">${TOAST_ICON[type] || ''}</span>` +
    `<div class="toast-body"><b>${esc(text)}</b>` +
    `${detail ? `<span>${esc(detail)}</span>` : ''}</div>` +
    `<button class="toast-x" aria-label="关闭">✕</button>`;
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

/** 确认框(取代原生 confirm)。返回 Promise<boolean>,Esc / 点遮罩 = 取消 */
function confirmBox({ title, body = '', ok = '确定', cancel = '取消', danger = false }) {
  return new Promise(resolve => {
    const prevFocus = document.activeElement;
    const mask = document.createElement('div');
    mask.className = 'modal-mask confirm-mask';
    mask.innerHTML =
      `<div class="modal confirm" role="dialog" aria-modal="true">` +
      `<h2>${esc(title)}</h2>` +
      `${body ? `<div class="hint confirm-body">${esc(body)}</div>` : ''}` +
      `<div class="confirm-acts">` +
      `<button class="ghost" data-act="cancel">${esc(cancel)}</button>` +
      `<button class="${danger ? 'danger' : ''}" data-act="ok">${esc(ok)}</button>` +
      `</div></div>`;

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

/** 从失败的 Response 里榨出最可读的一句错误。
    FastAPI 的 {detail} > 校验错误数组 > 纯文本正文(未捕获异常时 Starlette 返回纯文本
    "Internal Server Error",直接 r.json() 会炸,旧代码就是这样把原因吞成"启动失败"的) */
async function apiFail(r, fallback = '请求失败') {
  let txt = '';
  try { txt = (await r.text()).trim(); } catch {}
  if (txt) {
    let j = null;
    try { j = JSON.parse(txt); } catch {}
    if (j) {
      const d = j.detail ?? j.message ?? j.error;
      if (typeof d === 'string' && d) return d;
      if (Array.isArray(d) && d.length) return d.map(x => x.msg || JSON.stringify(x)).join('; ');
    } else if (txt.length <= 200 && txt[0] !== '<') {
      return `${txt}(HTTP ${r.status})`;
    }
  }
  return `${fallback}(HTTP ${r.status})`;
}

/** 惯用简写:响应不 ok 时弹 toast 并返回 true(调用方据此 return) */
async function toastIfFail(r, text) {
  if (r.ok) return false;
  toast(text, { detail: await apiFail(r) });
  return true;
}
