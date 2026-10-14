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
  const creating = !sourceEditId, kind = sourceEditKind;
  const inManager = () => sourceKind === kind && sourceView === 'manage' && sourceActive();
  const body = { name:$('source-edit-name').value.trim(), url:$('source-edit-url').value.trim(),
    group_name:$('source-edit-group').value.trim(), feed_url:$('source-edit-feed').value.trim(),
    enabled:$('source-edit-enabled').checked, fulltext:$('source-edit-fulltext').checked,
    extract:$('source-edit-extract').checked };
  try {
    const saved = await pipeJson(`/api/intake/${kind}/sources${sourceEditId ? '/'+sourceEditId : ''}`, {
      method:sourceEditId?'PATCH':'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
    });
    $('source-editor').style.display = 'none';
    toast(creating ? '订阅已添加' : '订阅已更新', {type:'ok'});
    if (sourceKind !== kind || !sourceActive()) return;
    await switchSourceView('manage');
    if (!inManager()) return;
    await loadSourceModule(false);
    if (!creating || !inManager()) return;
    if (!sourceData?.sources.some(source => source.id === saved.id)) await loadSourceModule(false);
    if (inManager() && sourceData?.sources.some(source => source.id === saved.id)) {
      clearTimeout(sourceSearchTimer);
      const rowSelector = `[data-source-action="edit"][data-source-id="${saved.id}"]`;
      if (!$('source-list').querySelector(rowSelector)) {
        $('source-q').value = '';
        await renderSourceView(true);
      }
      if (inManager()) $('source-list').querySelector(rowSelector)?.closest('.source-subscription').scrollIntoView({block:'nearest'});
    }
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
  $('source-bridges').value = (s.bridges || []).join('\n');
  // 「自动同步 / 同步间隔」两个控件删掉之后(那一档整条拆了,见
  // `kb.pipeline._scheduler_tick`),这张表单上只剩 X 的取法那一块 —— 别的来源
  // 打开会看到一个空表单配一颗「保存设置」,点下去提交一个空对象。整张表单
  // 跟着 X 那一块一起藏掉,面板剩下的说明文字照旧显示。
  $('source-settings-form').hidden = sourceModules.get(sourceKind)?.adapter !== 'social';
  // 桥接只是三条 X 通道之一,显示哪一块由 x_channel.js 按当前通道决定。
  fillXChannelFields(s);
  loadXChannel(sourceKind);
  // 「按来源类型每 N 分钟自动同步」那一档拆掉了(见 kb.pipeline._scheduler_tick),
  // 这一类来源改成跟着日报那一轮走。**所以这一行必须去问日报到底开没开、这一类
  // 在不在它的范围里** —— 无条件写「每天同步」在用户关掉日报定时、或者把 RSS 从
  // 范围里去掉之后就是一句假话,而他会一直等一个不会来的自动同步。
  renderSourceScheduleNote();
  // 现读一次:日报那边改了设置之后,这一行不该还停在上次打开时的说法。
  loadSourceScheduleNote();
}
// 日报定时的现状。读失败/还没读到时不猜,只说手动同步那一半(已知为真)。
let sourceDailySettings = null;
async function loadSourceScheduleNote() {
  try { sourceDailySettings = await pipeJson('/api/daily/settings'); }
  catch { sourceDailySettings = null; }
  renderSourceScheduleNote();
}
function renderSourceScheduleNote() {
  const box = $('source-settings-status');
  if (!box) return;
  const s = sourceDailySettings;
  const manual = '手动同步随时可用。';
  if (!s) { box.textContent = manual; return; }
  const inScope = (s.channels || []).includes(sourceKind);
  box.textContent = !s.enabled
    ? `日报定时未启用，这一类来源目前不会自动同步。${manual}`
    : !s.sync
    ? `日报定时每天 ${s.time} 生成，但没开「生成前先同步」。${manual}`
    : !inScope
    ? `这一类来源不在日报的同步范围里，目前不会自动同步。${manual}`
    : `每天 ${s.time} 跟着日报那一轮同步。${manual}`;
}

async function saveSourceSettings(e) {
  e.preventDefault();
  const kind = sourceKind;
  $('source-settings-save').disabled = true;
  $('source-settings-error').textContent = '';
  const body = {};
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
