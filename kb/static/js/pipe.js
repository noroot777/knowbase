// ── 数据管道 ──────────────────────────────────────────────────────────────
const STAGE_META = {
  crawl:   { name: '抓取',  desc: '来源 → 采集器本地' },
  export:  { name: '导出',  desc: '采集器 → obs md' },
  index:   { name: '索引',  desc: 'obs → FTS + 向量' },
  extract: { name: '提取',  desc: 'entity/topic/摘要' },
  projects:{ name: '条目化', desc: '工具/项目拆条目' },
  wiki:    { name: 'Wiki',  desc: '双链/实体页/日摘要' },
};
const STATUS_ICON = { pending: '⋯', running: '<span class="spin"></span>',
                      ok: '✓', skipped: '⊘', error: '✗', '': '—' };
let pipeTimer = null;

// ── 全流程同步弹窗(时间段 + 目标多选) ──────────────────────────────────────
let pipeAcctList = [];                                 // 来源的全部可选目标
let pipeSel = new Set();                               // 选中的目标
let pipeRange = { label: '不限时间', from: '', to: '' };
// 采集器给的区间补抓注意事项(如"微信只保留近 30 天可翻页历史")。
// 由 refreshPipe 从 /api/pipeline/status 带回,面板本身不预设任何来源知识。
let pipeRangeHint = '';

// 本地日期 YYYY-MM-DD。不能用 toISOString() —— 那给的是 UTC 日期,
// 东八区 00:00-07:59 会算成前一天,整套预设范围跟着错开一天(当天文章漏抓)。
function localDay(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function isoDay(offset) {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return localDay(d);
}
function pipePresets() {
  const today = isoDay(0);
  const m3 = new Date(); m3.setMonth(m3.getMonth() - 3);
  return [
    { label: '不限时间', from: '', to: '' },
    { label: '今天', from: today, to: today },
    { label: '近 7 天', from: isoDay(-7), to: today },
    { label: '近 30 天', from: isoDay(-30), to: today },
    { label: '近 3 个月', from: localDay(m3), to: today },
    { label: '今年', from: today.slice(0, 4) + '-01-01', to: today },
  ];
}

async function openPipeModal() {
  // 目标列表每次打开都拉一次(可能刚在采集器那边加了新目标)
  const d = await fetch('/api/pipeline/accounts').then(r => r.json()).catch(() => null);
  pipeAcctList = (d && d.accounts) || [];
  pipeSel = new Set(pipeAcctList);                     // 默认全选
  pipeRange = { label: '不限时间', from: '', to: '' };
  $('pipe-acct-search').value = '';
  $('pipe-modal').style.display = '';
  renderPipeRange();
  renderPipeAcctList();
}
function closePipeModal() {
  $('pipe-modal').style.display = 'none';
  $('pipe-range-pop').style.display = 'none';
}

function togglePipeRangePop(e) {
  e.stopPropagation();
  const pop = $('pipe-range-pop');
  const show = pop.style.display === 'none';
  if (show) renderPipePresets();
  pop.style.display = show ? '' : 'none';
}
function renderPipePresets() {
  $('pipe-range-presets').innerHTML = pipePresets().map((p, i) => `
    <div onclick="applyPipePreset(${i})"
         style="display:flex;align-items:center;gap:8px;padding:8px 14px;cursor:pointer;font-size:13.5px"
         onmouseover="this.style.background='var(--tag-bg)'" onmouseout="this.style.background=''">
      <span style="width:14px;color:var(--accent)">${p.label === pipeRange.label ? '✓' : ''}</span>${p.label}
    </div>`).join('');
  $('pipe-custom-from').value = pipeRange.from;
  $('pipe-custom-to').value = pipeRange.to;
  $('pipe-custom-err').textContent = '';
}
function applyPipePreset(i) {
  pipeRange = { ...pipePresets()[i] };
  $('pipe-range-pop').style.display = 'none';
  renderPipeRange();
}
function applyPipeCustomRange() {
  const from = $('pipe-custom-from').value, to = $('pipe-custom-to').value;
  const err = m => { $('pipe-custom-err').textContent = m; };
  if (!from || !to) { err('起止日期都要填'); return; }
  if (from > to) { err('起始日期不能晚于结束日期'); return; }
  err('');
  pipeRange = { label: `${from} ~ ${to}`, from, to };
  $('pipe-range-pop').style.display = 'none';
  renderPipeRange();
}

function renderPipeRange() {
  $('pipe-range-label').textContent = pipeRange.label;
  const ranged = !!pipeRange.from;
  $('pipe-range-hint').textContent = ranged
    ? pipeRangeHint
    : '不限时间 = 增量同步各目标的新内容(当天已抓过的自动跳过),固定同步全部目标。';
  // 增量模式后端不支持按目标过滤,目标区置灰
  const blk = $('pipe-acct-block');
  blk.style.opacity = ranged ? '' : '.45';
  blk.style.pointerEvents = ranged ? '' : 'none';
  renderPipeEst();
}

function renderPipeAcctList() {
  const q = $('pipe-acct-search').value.trim().toLowerCase();
  const items = q ? pipeAcctList.filter(n => n.toLowerCase().includes(q)) : pipeAcctList;
  $('pipe-acct-count').textContent = `${pipeSel.size}/${pipeAcctList.length}`;
  // 目标名来自采集器的外部数据,不拼进 inline onclick 的 JS 字符串:
  // 只转义单引号挡不住双引号/尖括号破坏属性。值走 data-acct + 事件委托。
  $('pipe-acct-list').innerHTML = items.length ? items.map(n => `
    <div data-acct="${esc(n)}"
         style="display:flex;align-items:center;gap:9px;padding:9px 13px;cursor:pointer;
                border-bottom:1px solid var(--line);font-size:13.5px"
         onmouseover="this.style.background='var(--tag-bg)'" onmouseout="this.style.background=''">
      <span style="color:${pipeSel.has(n) ? 'var(--accent)' : 'var(--muted)'}">${pipeSel.has(n) ? '☑' : '☐'}</span>
      <span>${esc(n)}</span>
    </div>`).join('')
    : `<div style="padding:22px;text-align:center;font-size:12.5px;color:var(--muted)">${
        pipeAcctList.length ? '没有匹配的目标' : '采集器不在线或还没有可选目标'}</div>`;
  renderPipeEst();
}
$('pipe-acct-list').addEventListener('click', e => {
  const row = e.target.closest('[data-acct]');
  if (row) togglePipeAcct(row.dataset.acct);
});
function togglePipeAcct(n) {
  pipeSel.has(n) ? pipeSel.delete(n) : pipeSel.add(n);
  renderPipeAcctList();
}
function pipeSelectAll() { pipeSel = new Set(pipeAcctList); renderPipeAcctList(); }
function pipeSelectNone() { pipeSel = new Set(); renderPipeAcctList(); }

function renderPipeEst() {
  const ranged = !!pipeRange.from;
  const n = pipeSel.size;
  if (ranged) {
    const s = n * 12;
    const t = s < 60 ? `~${s}s` : `~${Math.round(s / 60)} 分钟`;
    $('pipe-est').innerHTML =
      `抓取预计耗时 <b>${t}</b>(${n} 个号 × 平均 12s,含反爬间隔:页间 2.5-4s、号间 4-7s)。<br>` +
      `提取阶段与抓取并行、claude 3 路并发,管道总时长 ≈ 抓取和提取里较长的那个。`;
    $('pipe-start').disabled = n === 0;
    $('pipe-start').textContent = `开始同步 (${n} 个)`;
  } else {
    $('pipe-est').innerHTML =
      `增量抓各号新文章,耗时视更新量而定。<br>` +
      `提取阶段与抓取并行、claude 3 路并发,管道总时长 ≈ 抓取和提取里较长的那个。`;
    $('pipe-start').disabled = false;
    $('pipe-start').textContent = '开始同步';
  }
}

async function refreshPipe(withPending = false) {
  const s = await fetch('/api/pipeline/status' + (withPending ? '?pending=1' : ''))
    .then(r => r.json()).catch(() => null);
  if (!s) return;

  // 来源采集器状态。message/hint 都是采集器给的成品文案,这里只负责显示
  // ——换来源(RSS、播客…)不用改这段。
  const src = s.source || { online: false, message: '' };
  $('src-label').textContent = src.label ? `来源 · ${src.label}` : '来源采集器';
  $('src-status').textContent = src.online ? '在线' : '不在线(抓取将跳过)';
  $('src-status').style.color = src.online ? 'var(--ok)' : '#c33';
  $('src-detail').textContent =
    [src.message, src.hint].filter(Boolean).join(' · ');
  const console_ = $('src-console');
  console_.style.display = src.console_url ? '' : 'none';
  if (src.console_url) console_.href = src.console_url;
  pipeRangeHint = src.range_hint || '';

  // 阶段卡片(空闲时叠加 last_run 和待处理数)
  const stages = s.running ? s.stages : (s.last_run ? s.last_run.stages : s.stages);
  $('pipe-stages').innerHTML = Object.entries(STAGE_META).map(([k, m]) => {
    const st = stages[k] || { status: '', detail: '' };
    const pendN = s.pending && s.pending[k] != null ? s.pending[k] : null;
    const pend = pendN != null ? `待处理 ${pendN}` : '';
    // 卡片正文 = 上次运行结果 + 当前积压。两者都有就并排显示:只认 detail 的话,
    // 「本次无需重试」这类重试链留下的通用文案会把真正待办的积压数盖掉
    // (重试跳过 projects 时,169 篇待条目化在卡片上就消失了)。
    const body = [st.detail, pendN ? pend : ''].filter(Boolean).join(' · ') || pend;
    // 失败明细(后端 _stage 的 failures):卡片内可展开,不用翻日志
    const fails = st.failures || [];
    const failHtml = fails.length ? `
      <details style="margin-top:5px">
        <summary style="cursor:pointer;font-size:11.5px;color:#c33">失败明细 ${fails.length}</summary>
        <div style="font-size:11.5px;margin-top:3px;max-height:130px;overflow:auto">
          ${fails.map(f => `<div style="margin-bottom:4px">· ${esc(f.title)}<br>
            <span style="color:var(--muted)">${esc(f.reason)}</span></div>`).join('')}
        </div>
      </details>` : '';
    return `<div class="hit" style="margin:0;padding:12px 14px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <b style="font-size:14px">${m.name}</b>
        <span style="font-size:14px">${STATUS_ICON[st.status] ?? st.status}</span>
      </div>
      <div style="font-size:11.5px;color:var(--muted);margin:2px 0 6px">${m.desc}</div>
      <div style="font-size:12px;color:#4A565F;min-height:18px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden"
           title="${esc(body)}">${esc(body)}</div>
      ${failHtml}
    </div>`;
  }).join('');

  // 日志 + 按钮态
  $('pipe-log').textContent = (s.log && s.log.length) ? s.log.join('\n')
    : (s.last_run ? `上次运行完成于 ${s.last_run.finished_at}` : '(暂无日志)');
  const logBox = $('pipe-log').parentElement;
  logBox.scrollTop = logBox.scrollHeight;
  $('pipe-run').disabled = s.running;
  $('pipe-run').textContent = s.running ? '同步中…' : '全流程同步';
  // 任一阶段有失败(或提取有积压)且空闲时,亮出全局重试按钮
  const hasFail = Object.values(stages).some(st =>
    st && (st.status === 'error' || /失败 [1-9]/.test(st.detail || '') ||
           /\d+ 个账号失败/.test(st.detail || '')));
  const hasPend = s.pending && s.pending.extract > 0;
  $('pipe-retry').style.display = (!s.running && (hasFail || hasPend)) ? '' : 'none';

  // 运行中自动轮询
  if (s.running && !pipeTimer) {
    pipeTimer = setInterval(() => refreshPipe(false), 1500);
  } else if (!s.running && pipeTimer) {
    clearInterval(pipeTimer);
    pipeTimer = null;
    refreshPipe(true);   // 结束后刷一次待处理数
    loadMeta();          // 文档/实体数可能变了
  }
}

async function retryFailed() {
  const btn = $('pipe-retry');
  btn.disabled = true;
  try {
    const r = await fetch('/api/pipeline/retry-failed', { method: 'POST' });
    if (await toastIfFail(r, '重试没能启动')) return;
    const d = await r.json().catch(() => ({}));
    toast('已开始重试失败项', { type: 'ok', detail: d.plan ? '阶段:' + d.plan : '' });
    refreshPipe(false);
  } catch (e) {
    toast('重试没能启动', { detail: e.message });
  } finally {
    btn.disabled = false;
  }
}

// ── 每日定时同步 ──────────────────────────────────────────────────────────
async function loadSched() {
  try {
    const s = await fetch('/api/pipeline/schedule').then(r => r.json());
    $('sched-on').checked = !!s.enabled;
    if (s.time) $('sched-time').value = s.time;
  } catch {}
}

async function saveSched() {
  const body = { enabled: $('sched-on').checked, time: $('sched-time').value || '07:30' };
  try {
    const r = await fetch('/api/pipeline/schedule', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
    $('sched-msg').textContent = body.enabled ? `✓ 每天 ${body.time} 自动同步` : '已关闭';
  } catch (e) {
    $('sched-msg').textContent = '保存失败: ' + e.message;
  }
  setTimeout(() => { $('sched-msg').textContent = ''; }, 5000);
}

async function startPipeFromModal() {
  const body = {};
  if (pipeRange.from) {
    body.since_date = pipeRange.from;
    body.until_date = pipeRange.to;
    body.accounts = Array.from(pipeSel);
  }
  const btn = $('pipe-start');
  btn.disabled = true;
  try {
    const r = await fetch('/api/pipeline/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (await toastIfFail(r, '同步没能启动')) return;
    closePipeModal();
    refreshPipe(false);
  } catch (e) {
    toast('同步没能启动', { detail: e.message });
  } finally {
    btn.disabled = false;
  }
}
