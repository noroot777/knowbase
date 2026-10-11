// Edit a complete schedule, then save once; do not write on every field change.
let schedLoaded = false, schedDirty = false, schedSaving = false, schedLoading = false;
function openSched() {
  $('sched-modal').style.display = '';
  loadSched();
}
function closeSched() { $('sched-modal').style.display = 'none'; }
function renderSchedSummary(schedule) {
  $('sched-summary').textContent = schedule.enabled ? `定时同步 · 每天 ${schedule.time}` : '定时同步 · 未启用';
}
function scheduleMessage(s) {
  if (!s.enabled) return '定时同步未启用';
  const next = s.next_run ? ` · 下次 ${new Date(s.next_run).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}` : '';
  return `已保存：每天 ${s.time} 自动同步${next}`;
}
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
    renderSchedSummary(s);
  } catch (e) { $('sched-msg').textContent = '读取失败：' + e.message; $('sched-reload').hidden = false; }
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
    renderSchedSummary(saved);
  } catch (e) { $('sched-msg').textContent = '未保存：' + e.message + '。修改已保留，可重试。'; }
  finally { schedSaving = false; $('sched-fields').disabled = false; $('sched-save').disabled = !schedDirty; $('sched-save').textContent = '保存设置'; }
}
