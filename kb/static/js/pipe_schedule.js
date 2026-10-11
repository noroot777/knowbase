// Edit a complete schedule, then save once; do not write on every field change.
let schedLoaded = false, schedDirty = false, schedSaving = false, schedLoading = false;
function scheduleMessage(s) {
  if (!s.enabled) return '定时同步未启用';
  const next = s.next_run ? ` · 下次 ${new Date(s.next_run).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}` : '';
  return `已保存：每天 ${s.time} 自动同步${next}`;
}
// 设置搬进弹窗之后,页头那颗按钮就是它在页面上唯一的露头 —— 当前状态写在按钮
// 上,不打开也看得见「几点跑/有没有开」。读不到时保持裸标签(别写死一个未启用,
// 那是在报一个没查到的结论)。
function renderSchedEntry(s) {
  const label = $('subs-sched-label');
  if (!label) return;
  label.textContent = s ? (s.enabled ? `定时同步 · 每天 ${s.time}` : '定时同步 · 未启用') : '定时同步';
  $('subs-sched-open').classList.toggle('is-on', !!s?.enabled);
}
function openSchedModal() {
  $('sched-modal').style.display = '';
  loadSched();
}
function closeSchedModal() { $('sched-modal').style.display = 'none'; }
// 定时同步也可以一次跑几个来源，所以这里是一组勾选框而不是一个下拉。
function renderSchedChannels(selected) {
  $('sched-channels').innerHTML = pipeChannels.map(c => `<label class="pipe-channel">
    <input type="checkbox" data-sched-channel="${esc(c.id)}" onchange="markSchedDirty()"${selected.includes(c.id) ? ' checked' : ''}>
    <span><b>${esc(c.label)}</b></span></label>`).join('');
}
function schedChannels() {
  return [...$('sched-channels').querySelectorAll('input:checked')].map(i => i.dataset.schedChannel);
}
async function loadSched() {
  if (schedLoading || schedSaving || schedDirty) return;
  // 这一趟是不是"从没读成功过"那一次:读失败时要不要把字段留在禁用态只看它。
  const first = !schedLoaded;
  schedLoading = true; $('sched-reload').hidden = true;
  $('sched-fields').disabled = true;
  $('sched-save').disabled = true;
  $('sched-msg').textContent = '正在读取定时设置…';
  try {
    await loadPipeChannels();
    const s = await pipeJson('/api/pipeline/schedule');
    $('sched-on').checked = !!s.enabled; $('sched-time').value = s.time || '07:30';
    renderSchedChannels(s.channels?.length ? s.channels : [s.channel || pipeChannel]);
    schedLoaded = true; $('sched-fields').disabled = false;
    $('sched-msg').textContent = scheduleMessage(s);
    renderSchedEntry(s);
  } catch (e) {
    $('sched-msg').textContent = '读取失败：' + e.message; $('sched-reload').hidden = false;
    // 重读失败时屏幕上那份仍是上一次读到的真实设置,照旧可编辑可保存;
    // 从没读成功过才留在禁用态 —— 那时根本没有值可编辑。
    $('sched-fields').disabled = first;
  }
  finally { schedLoading = false; }
}
function markSchedDirty() {
  if (!schedLoaded || schedSaving || schedLoading) return;
  schedDirty = true; $('sched-save').disabled = false; $('sched-msg').textContent = '有未保存的修改';
}
async function saveSched() {
  if (!schedLoaded || schedSaving || !schedDirty) return;
  if (!$('sched-time').reportValidity()) return;
  const channels = schedChannels();
  if (!channels.length) { $('sched-msg').textContent = '请至少选择一个同步来源'; return; }
  const body = { enabled: $('sched-on').checked, time: $('sched-time').value, channels };
  schedSaving = true; $('sched-fields').disabled = true; $('sched-save').disabled = true;
  $('sched-save').textContent = '正在保存…'; $('sched-msg').textContent = '';
  try {
    const saved = await pipePost('/api/pipeline/schedule', body); schedDirty = false;
    $('sched-msg').textContent = scheduleMessage(saved);
    renderSchedEntry(saved);
  } catch (e) { $('sched-msg').textContent = '未保存：' + e.message + '。修改已保留，可重试。'; }
  finally { schedSaving = false; $('sched-fields').disabled = false; $('sched-save').disabled = !schedDirty; $('sched-save').textContent = '保存设置'; }
}
