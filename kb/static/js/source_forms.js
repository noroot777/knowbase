let sourceEditId = null, sourceEditKind = '', sourceSaving = false;
function openSourceEditor(id = null) {
  const s = sourceData?.sources.find(s => s.id === id);
  sourceEditId = id; sourceEditKind = sourceKind;
  $('source-editor-title').textContent = id ? '编辑订阅' : '添加订阅';
  $('source-edit-name').value = s?.name || '';
  $('source-edit-url').value = s?.url || '';
  $('source-edit-group').value = s?.group_name || '';
  $('source-edit-feed').value = s?.feed_url || '';
  $('source-edit-enabled').checked = s ? !!s.enabled : true;
  $('source-edit-fulltext').checked = !!s?.fulltext;
  $('source-edit-extract').checked = !!s?.extract;
  $('source-edit-hint').textContent = sourceModules.get(sourceKind).url_hint;
  $('source-edit-error').textContent = '';
  $('source-edit-save').disabled = false;
  $('source-editor').style.display = '';
  $('source-edit-name').focus();
}
function closeSourceEditor() { if (!sourceSaving) $('source-editor').style.display = 'none'; }
async function saveSourceEditor(e) {
  e.preventDefault();
  if (sourceSaving) return;
  sourceSaving = true; $('source-edit-save').disabled = true;
  $('source-edit-error').textContent = '';
  const body = { name:$('source-edit-name').value.trim(), url:$('source-edit-url').value.trim(),
    group_name:$('source-edit-group').value.trim(), feed_url:$('source-edit-feed').value.trim(),
    enabled:$('source-edit-enabled').checked, fulltext:$('source-edit-fulltext').checked,
    extract:$('source-edit-extract').checked };
  try {
    await pipeJson(`/api/intake/${sourceEditKind}/sources${sourceEditId ? '/'+sourceEditId : ''}`, {
      method:sourceEditId?'PATCH':'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
    });
    $('source-editor').style.display = 'none';
    toast(sourceEditId ? '订阅已更新' : '订阅已添加', {type:'ok'});
    await switchSourceView('manage'); await loadSourceModule(true);
  } catch(e) { $('source-edit-error').textContent = e.message; }
  finally { sourceSaving=false; $('source-edit-save').disabled = false; }
}
async function actOnSource(action, id) {
  const s = sourceData?.sources.find(s => s.id === id);
  if (!s || sourceBusy) return;
  if (action === 'edit') return openSourceEditor(id);
  if (action === 'sync') return s.enabled ? syncSource([id]) : null;
  const kind = sourceKind;
  if (action === 'delete' && !await confirmBox({title:'删除这条订阅？',
    body:`删除“${s.name}”后将停止同步，已收录的内容仍保留在知识库。默认订阅也不会自动恢复。`,ok:'删除订阅',danger:true})) return;
  sourceBusy = true; updateSourceRunStatus();
  try {
    await pipeJson(`/api/intake/${kind}/sources/${id}`, { method:action==='delete'?'DELETE':'PATCH',
      ...(action==='delete'?{}:{headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}) });
    toast(action==='delete'?'订阅已删除':s.enabled?'已停用':'已启用', {type:'ok'});
    await loadSourceModule(false);
    if (sourceView === 'manage') await renderSourceView();
  } catch(e) { toast('操作未完成', {detail:e.message}); }
  finally { sourceBusy=false; updateSourceRunStatus(); }
}
function fillSourceSettings(s) {
  $('source-settings-error').textContent = '';
  $('source-settings-fields').disabled = false;
  $('source-auto').checked = s.enabled;
  $('source-interval').value = s.interval_minutes;
  $('source-bridges').value = (s.bridges || []).join('\n');
  // 桥接只是三条 X 通道之一,显示哪一块由 x_channel.js 按当前通道决定。
  fillXChannelFields(s);
  loadXChannel(sourceKind);
  $('source-settings-status').textContent = s.enabled
    ? `已启用 · 下次计划 ${s.next_run ? sourceTime(s.next_run*1000) : '即将开始'}`
    : '自动同步未开启。手动同步随时可用。';
}
async function saveSourceSettings(e) {
  e.preventDefault();
  const kind = sourceKind;
  $('source-settings-save').disabled = true;
  $('source-settings-error').textContent = '';
  const body = { enabled:$('source-auto').checked, interval_minutes:Number($('source-interval').value) };
  if (sourceModules.get(kind).adapter === 'social') {
    body.bridges = $('source-bridges').value.split('\n').map(v=>v.trim()).filter(Boolean);
    Object.assign(body, xChannelPayload(kind));
  }
  try {
    const settings = await pipeJson(`/api/intake/${kind}/settings`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if (kind === sourceKind) {
      fillSourceSettings(settings);
      await loadSourceModule(false);
    }
    toast('同步设置已保存', {type:'ok'});
  } catch(e) { $('source-settings-error').textContent = e.message; }
  finally { $('source-settings-save').disabled = false; }
}
