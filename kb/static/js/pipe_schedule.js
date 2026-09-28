// Edit a complete schedule, then save once; do not write on every field change.
let schedLoaded = false, schedDirty = false, schedSaving = false, schedLoading = false;
function scheduleMessage(s) {
  if (!s.enabled) return '定时同步未启用';
  const next = s.next_run ? ` · 下次 ${new Date(s.next_run).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}` : '';
  return `已保存：每天 ${s.time} 自动同步${next}`;
}
async function loadSched() {
  if (schedLoading || schedSaving || schedDirty) return;
  schedLoading = true; $('sched-reload').hidden = true;
  $('sched-msg').textContent = '正在读取定时设置…';
  try {
    await loadPipeChannels();
    const s = await pipeJson('/api/pipeline/schedule');
    $('sched-on').checked = !!s.enabled; $('sched-time').value = s.time || '07:30';
    $('sched-channel').value = s.channel || pipeChannel;
    schedLoaded = true; $('sched-fields').disabled = false;
    $('sched-msg').textContent = scheduleMessage(s);
  } catch (e) { $('sched-msg').textContent = '读取失败：' + e.message; $('sched-reload').hidden = false; }
  finally { schedLoading = false; }
}
function markSchedDirty() {
  if (!schedLoaded || schedSaving) return;
  schedDirty = true; $('sched-save').disabled = false; $('sched-msg').textContent = '有未保存的修改';
}
async function saveSched() {
  if (!schedLoaded || schedSaving || !schedDirty) return;
  if (!$('sched-time').reportValidity()) return;
  if (!$('sched-channel').value) { $('sched-msg').textContent = '请选择同步通道'; return; }
  const body = { enabled: $('sched-on').checked, time: $('sched-time').value, channel: $('sched-channel').value };
  schedSaving = true; $('sched-fields').disabled = true; $('sched-save').disabled = true;
  $('sched-save').textContent = '正在保存…'; $('sched-msg').textContent = '';
  try {
    const saved = await pipePost('/api/pipeline/schedule', body); schedDirty = false;
    $('sched-msg').textContent = scheduleMessage(saved);
  } catch (e) { $('sched-msg').textContent = '未保存：' + e.message + '。修改已保留，可重试。'; }
  finally { schedSaving = false; $('sched-fields').disabled = false; $('sched-save').disabled = !schedDirty; $('sched-save').textContent = '保存设置'; }
}
