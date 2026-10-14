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
// 「这一类来源什么时候会自动同步」。**要问遍所有还在生效的定时**,不是只问日报:
// 页头那颗「定时同步」(pipeline_schedule)按仓库主人的要求暂留着,它照样会到点跑
// 全流程。只看日报的话,B 开着而 A 关着时这里会写「目前不会自动同步」,而页面上方
// 明明写着「定时同步 · 每天 07:30」—— 两句话当场打架。
//
// 两个接口的**包形不一样**,各按各的读:`/api/daily/settings` 外面套了一层
// `{settings, channels, default_channels}`(`channels` 是可选来源目录,不是待同步
// 名单),`/api/pipeline/schedule` 是扁的。把外层当设置对象读,`enabled` 永远取不到,
// 于是开着的定时被一律说成「未启用」。
let sourceSchedules = null;
async function loadSourceScheduleNote() {
  const ask = async (url, pick) => { try { return pick(await pipeJson(url)); } catch { return null; } };
  const [daily, pipe] = await Promise.all([
    ask('/api/daily/settings', d => d.settings),
    ask('/api/pipeline/schedule', d => d),
  ]);
  // **三态,不是两态**:读到 = 知道它开着还是关着,读不到 = **不知道**,
  // 而不知道不等于"没开"。少读一条就敢说「目前不会自动同步」,而没读到的那条
  // 可能正开着 —— 页面上方还写着「定时同步 · 每天 07:30」,两句话当场打架
  // (反过来也一样:日报那条读不到时同样不许替它说"没开")。
  // 删掉页头那颗定时同步时,记得把下面这条读取一起删掉,否则这行字会永远
  // 卡在「读不到页头的定时同步」上 —— 接口 404 和断网在这里长得一样
  // (`pipeJson` 只抛一句话,不带状态码)。
  sourceSchedules = { daily, pipe, unknown: [...(daily ? [] : ['日报']),
                                             ...(pipe ? [] : ['页头的定时同步'])] };
  renderSourceScheduleNote();
}
// 这一类来源会被哪几条定时带上。返回 [{时刻}],空数组 = 没有一条会带上它。
function sourceScheduleHits(kind) {
  const { daily, pipe } = sourceSchedules || {};
  const hits = [];
  // 日报那一轮:定时开着、勾了「生成前先同步」、而且这一类在它的同步范围里
  if (daily?.enabled && daily.sync !== false && (daily.channels || []).includes(kind))
    hits.push({ time: daily.time, what: '日报' });
  if (pipe?.enabled && (pipe.channels || [pipe.channel]).includes(kind))
    hits.push({ time: pipe.time, what: '页头的定时同步' });
  return hits;
}
function renderSourceScheduleNote() {
  const box = $('source-settings-status');
  if (!box) return;
  const manual = '手动同步随时可用。';
  if (!sourceSchedules) { box.textContent = manual; return; }
  const hits = sourceScheduleHits(sourceKind);
  if (hits.length) {
    // 已知有一条带上它 —— 这句话不受另一条读不到的影响(再多一条定时只会多一个
    // 同步时刻,不会把这个时刻取消掉)。
    box.textContent = hits.map(h => `每天 ${h.time} 跟着${h.what}那一轮同步`).join('；')
      + `。${manual}`;
    return;
  }
  const { daily, unknown } = sourceSchedules;
  if (unknown.length) {
    // 已知的那几条都没带上它,可还有读不到的 —— 这时只能说"不知道"。这里不顺带
    // 念日报那条的原因:那几句话都以「目前不会自动同步」收尾,而此刻正是不确定。
    box.textContent = `读不到${unknown.join('、')}的设置，没法确定这一类来源会不会`
      + `自动同步。${manual}`;
    return;
  }
  // 两条都读到了、一条都没带上它。**把原因说出来**,否则用户不知道该去哪儿改。
  box.textContent = (!daily.enabled ? '日报定时未启用，这一类来源目前不会自动同步。'
    : daily.sync === false ? `日报定时每天 ${daily.time} 生成，但没开「生成前先同步」。`
    : '这一类来源不在日报的同步范围里，目前不会自动同步。') + manual;
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
