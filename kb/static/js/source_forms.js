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
  // 「自动同步 / 同步间隔」两个控件删掉之后(那一档整条拆了,见
  // `kb.pipeline._scheduler_tick`),这张表单上只剩 X 的取法那一块 —— 别的来源
  // 打开会看到一个空表单配一颗「保存设置」,点下去提交一个空对象。整张表单
  // 跟着 X 那一块一起藏掉,面板剩下的说明文字照旧显示。
  $('source-settings-form').hidden = sourceModules.get(sourceKind)?.adapter !== 'social';
  // X 那一块(取法、可执行文件路径、登录/导入/检查)整个归 x_channel.js 管。
  fillXChannelFields(s);
  loadXChannel(sourceKind);
  // 「按来源类型每 N 分钟自动同步」那一档拆掉了(见 kb.pipeline._scheduler_tick),
  // 这一类来源改成跟着日报那一轮走。**所以这一行必须去问日报到底开没开、这一类
  // 在不在它的范围里** —— 无条件写「每天同步」在用户关掉日报定时、或者把 RSS 从
  // 范围里去掉之后就是一句假话,而他会一直等一个不会来的自动同步。
  renderSourceScheduleNote();
  // 现读一次:两条定时在别处改过之后,这一行不该还停在上次打开时的说法。
  loadSchedules();
}
// 定时配置一变就重画(谁保存成功都会推一次,见 `schedules.js`)。**这一行自己
// 不缓存配置**:缓存过的那一版里,页头保存成功只更新页头,这一行长期说反话。
watchSchedules(() => renderSourceScheduleNote());
// 共享层是**页内**的:另一份已打开的页面(或另一台机器)在那边保存了,这边收
// 不到通知,而这一页上别的几句说明都会在自己被打开时重读,只有这一行是停在屏幕
// 上不动的。所以**回到这一页就重读一次**,别把「我没收到通知」当成「服务器那份
// 没变」。常规来源轮询不带这一读(定时一天也改不了一次,15 秒一问不值当)。
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && sourceActive() && schedules()) loadSchedules();
});
// 「这一类来源什么时候会自动同步」。**要问遍所有还在生效的定时**,现在只剩日报
// 这一条(页头那颗「定时同步」连同它的接口一起删了,见
// `docs/decisions/sync-schedule.md`)。**再加一条定时就要在这里加一问** —— 漏问的
// 后果是这里写「目前不会自动同步」而那条定时照样到点跑,两句话当场打架;所以这个
// 函数照旧返回**一个列表**,不是一个真假。
//
// 配置本身由 `schedules.js` 那一层持有(含包形差异、三态和代次),这里只读它。
// 这一类来源会被哪几条定时带上。返回 [{时刻}],空数组 = 没有一条会带上它。
function sourceScheduleHits(kind) {
  const { daily } = schedules() || {};
  const hits = [];
  // 日报那一轮:定时开着、勾了「生成前先同步」、而且这一类在它的同步范围里
  if (daily?.enabled && daily.sync !== false && (daily.channels || []).includes(kind))
    hits.push({ time: daily.time, what: '日报' });
  return hits;
}
function renderSourceScheduleNote() {
  const box = $('source-settings-status');
  if (!box) return;
  const manual = '手动同步随时可用。';
  const config = schedules();
  if (!config) { box.textContent = manual; return; }
  const hits = sourceScheduleHits(sourceKind);
  if (hits.length) {
    // 已知有一条带上它 —— 这句话不受另一条读不到的影响(再多一条定时只会多一个
    // 同步时刻,不会把这个时刻取消掉)。
    box.textContent = hits.map(h => `每天 ${h.time} 跟着${h.what}那一轮同步`).join('；')
      + `。${manual}`;
    return;
  }
  const { daily, unknown } = config;
  if (unknown.length) {
    // 已知的那几条都没带上它,可还有读不到的 —— 这时只能说"不知道"。这里不顺带
    // 念日报那条的原因:那几句话都以「目前不会自动同步」收尾,而此刻正是不确定。
    box.textContent = `读不到${unknown.join('、')}的设置，没法确定这一类来源会不会`
      + `自动同步。${manual}`;
    return;
  }
  // 每条都读到了、一条都没带上它。**把原因说出来**,否则用户不知道该去哪儿改。
  box.textContent = (!daily.enabled ? '日报定时未启用，这一类来源目前不会自动同步。'
    : daily.sync === false ? `日报定时每天 ${daily.time} 生成，但没开「生成前先同步」。`
    : '这一类来源不在日报的同步范围里，目前不会自动同步。') + manual;
}

function saveSourceSettings(e) {
  e.preventDefault();
  return saveSourceSettingsNow();
}
// 把这张表单存下去。**返回「存成了没有」**:X 那三颗按钮点下去会先借这一步把
// 草稿存掉(见 `x_channel.js` 的 `xChanEnsureSaved`),存不成就不该接着做 ——
// 后端读的是存下来的那一份,没存成就是做到另一档头上。
// 失败的原因照旧落在表单那行红字上(`#source-settings-error`),不额外弹 toast:
// 那是字段级的错,就地红字是这个项目的口径(见 CLAUDE.md「前端反馈」)。
async function saveSourceSettingsNow() {
  const kind = sourceKind;
  $('source-settings-save').disabled = true;
  $('source-settings-error').textContent = '';
  const body = {};
  if (sourceModules.get(kind).adapter === 'social') Object.assign(body, xChannelPayload(kind));
  try {
    const settings = await pipeJson(`/api/intake/${kind}/settings`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if (kind === sourceKind) {
      fillSourceSettings(settings);
      await loadSourceModule(false);
    }
    toast('同步设置已保存', {type:'ok'});
    return true;
  } catch(e) { $('source-settings-error').textContent = e.message; return false; }
  finally { $('source-settings-save').disabled = false; }
}
