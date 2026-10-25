let youtubeRankGeneration = 0;
let youtubeStatusGeneration = 0;
const youtubePoolLabels = {creator_core:'核心创作者', interview_pool:'AI 访谈池',
  creator_watchlist:'观察名单（不自动排名）', quarantine:'隔离名单（不自动排名）'};
const youtubeNumberFields = {max_videos:['每轮最多处理视频数',1,20], max_minutes:['每轮累计时长（分钟）',1,240],
  max_video_minutes:['单条时长上限（分钟）',1,180], window_days:['候选回看天数',1,30],
  heat_limit:['热度榜准入名次',1,50], value_limit:['价值榜准入名次',1,50],
  run_seconds:['每轮处理时间上限（秒）',60,3600], asr_threads:['本地语音识别线程数',1,8]};

function youtubeEditor(source, kind) {
  let fields = $('youtube-channel-fields');
  if (!fields) {
    fields = document.createElement('fieldset'); fields.id = 'youtube-channel-fields';
    fields.innerHTML = `<legend>YouTube 排名规则</legend><label for="youtube-pool">频道分组</label>
      <select id="youtube-pool">${Object.entries(youtubePoolLabels).map(([key,label]) => `<option value="${key}">${label}</option>`).join('')}</select>
      <label for="youtube-weight">排名权重（0.1–3）</label><input id="youtube-weight" type="number" min="0.1" max="3" step="0.05">
      <p class="hint">分组决定是否进入候选排名，权重同时作用于热度和价值分。启用订阅不等于批准下载全部视频。</p>`;
    $('source-edit-error').before(fields);
  }
  fields.hidden = kind !== 'youtube'; fields.disabled = kind !== 'youtube';
  let policy = {};
  try { policy = JSON.parse(source?.config || '{}'); } catch {}
  $('youtube-pool').value = policy.pool || 'creator_core';
  $('youtube-weight').value = policy.score_multiplier ?? 1;
  for (const id of ['source-edit-feed', 'source-edit-fulltext']) {
    const input = $(id), label = input.closest('label') || document.querySelector(`label[for="${id}"]`);
    input.hidden = kind === 'youtube'; input.disabled = kind === 'youtube';
    if (label) label.hidden = kind === 'youtube';
    const note = (id === 'source-edit-feed' ? input : label)?.nextElementSibling;
    if (note?.classList.contains('hint')) note.hidden = kind === 'youtube';
  }
}

function youtubeChannelPayload() {
  return {pool:$('youtube-pool').value, score_multiplier:Number($('youtube-weight').value)};
}

function fillYouTubeSettings(settings, sent) {
  const generation = ++youtubeStatusGeneration;
  let fields = $('youtube-settings-fields');
  if (!fields) {
    fields = document.createElement('div'); fields.id = 'youtube-settings-fields';
    fields.innerHTML = `<h3>排名与处理预算</h3><p class="hint">只对热度榜或价值榜入选的视频花算力。先复用字幕，需要时仅下载音频；英文译成中文后才入库。画面理解关闭。</p>
      ${Object.entries(youtubeNumberFields).map(([key,[label,min,max]]) => `<label for="youtube-${key}">${label}</label><input id="youtube-${key}" type="number" min="${min}" max="${max}" step="1">`).join('')}
      <label><input id="youtube-ai_only" type="checkbox"> 仅纳入标题或简介明确与 AI 相关的视频</label>
      <label><input id="youtube-asr_enabled" type="checkbox"> 字幕不可用时允许本地语音转录</label>
      <label for="youtube-whisper_model">Whisper 模型文件路径（可选）</label>
      <input id="youtube-whisper_model" maxlength="2048" placeholder="留空尝试已有本地模型">
      <p class="hint">英文专用模型不能用于其他语言；本地转录不消耗模型 token，但会占用 CPU 和内存。缓存字幕和译文可在重试时复用。</p>
      <p id="youtube-tools" class="hint" role="status"></p>`;
    $('source-settings-error').before(fields);
  }
  fields.hidden = sourceKind !== 'youtube';
  fields.querySelectorAll('input').forEach(input => { input.disabled = sourceKind !== 'youtube'; });
  if (sourceKind !== 'youtube') return;
  if (sent && JSON.stringify(youtubeSettingsPayload()) !== sent) return;
  const values = settings.youtube || {};
  for (const key of Object.keys(youtubeNumberFields)) $(`youtube-${key}`).value = values[key] ?? '';
  for (const key of ['ai_only', 'asr_enabled']) $(`youtube-${key}`).checked = !!values[key];
  $('youtube-whisper_model').value = values.whisper_model || '';
  $('youtube-tools').textContent = '正在检查工具和模型配置…';
  pipeJson('/api/intake/youtube/status').then(status => {
    if (sourceKind !== 'youtube' || generation !== youtubeStatusGeneration) return;
    const tools = status.tools;
    const missing = [['yt_dlp','yt-dlp'],['ffmpeg','FFmpeg'],['whisper','whisper-cli'],['model','Whisper 模型']]
      .filter(([key]) => !tools[key]).map(([,label]) => label);
    $('youtube-tools').textContent = (status.models_ready ? '中文翻译与内容笔记模型可用。' : status.model_hint + '。')
      + (missing.length ? `缺少 ${missing.join('、')}。字幕不可用时不会伪造转录。` : '下载与本地转录工具已就绪。');
  }).catch(error => { if (sourceKind === 'youtube' && generation === youtubeStatusGeneration) $('youtube-tools').textContent = '工具状态读取失败：' + error.message; });
}

function youtubeSettingsPayload() {
  const result = {};
  for (const key of Object.keys(youtubeNumberFields)) result[key] = Number($(`youtube-${key}`).value);
  for (const key of ['ai_only', 'asr_enabled']) result[key] = $(`youtube-${key}`).checked;
  result.whisper_model = $('youtube-whisper_model').value.trim();
  return result;
}

async function renderYouTubeRanking() {
  const generation = ++youtubeRankGeneration;
  if (sourceKind !== 'youtube' || sourceView !== 'manage' || !sourceActive()) return;
  const selected = $('source-filter').value, query = $('source-q').value.trim().toLowerCase();
  try {
    const result = await pipeJson('/api/intake/youtube/ranking' + (selected ? '?source_id=' + encodeURIComponent(selected) : ''));
    if (generation !== youtubeRankGeneration || sourceKind !== 'youtube' || sourceView !== 'manage' || !sourceActive()) return;
    $('youtube-ranking')?.remove();
    const panel = document.createElement('section'); panel.id = 'youtube-ranking'; panel.className = 'youtube-ranking';
    const items = result.items.filter(item => (item.title + ' ' + item.channel).toLowerCase().includes(query));
    const states = {candidate:'候选待处理', deferred:'留待下轮', filtered:'轻量过滤', excluded:'不自动处理',
      processing:'正在处理', ready:'中文笔记已完成', failed:'处理失败，待重试'};
    panel.innerHTML = `<h3>候选排名 · ${result.total} 条</h3><p class="hint">同一视频跨榜只处理一次；未入选的视频不下载、不转录。下方仅显示前 100 条候选。</p>
      <label for="youtube-rank-order">排序</label><select id="youtube-rank-order"><option value="heat_rank">热度榜</option><option value="value_rank">价值榜</option></select>
      <div id="youtube-rank-items"></div><p class="hint">${result.total ? '频道管理' : '尚未检查频道。点击同步全部，先获取轻量候选；不会直接处理全部视频。'}</p>`;
    $('source-list').prepend(panel);
    const render = () => {
      const order = $('youtube-rank-order').value;
      $('youtube-rank-items').innerHTML = [...items].sort((left,right) => (left[order] || 100000) - (right[order] || 100000))
        .map(item => `<article class="youtube-rank-item"><a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a>
          <p class="hint">${esc(item.channel)} · 热度 #${item.heat_rank || '—'} / ${item.heat} · 价值 #${item.value_rank || '—'} / ${item.value} · ${item.views} 次观看</p>
          <p class="hint">${esc(states[item.state] || item.state)}${item.reason ? '：' + esc(item.reason) : ''}</p></article>`).join('') || '<p class="hint">没有匹配的候选。</p>';
    };
    $('youtube-rank-order').addEventListener('change', render); render();
  } catch (error) {
    if (generation !== youtubeRankGeneration || sourceKind !== 'youtube' || sourceView !== 'manage') return;
    $('youtube-ranking')?.remove();
    const panel = document.createElement('section'); panel.id = 'youtube-ranking'; panel.className = 'youtube-ranking';
    const message = document.createElement('p'); message.setAttribute('role','alert'); message.textContent = '候选排名读取失败：' + error.message;
    const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'ghost'; retry.textContent = '重试排名'; retry.onclick = renderYouTubeRanking;
    panel.append(message, retry); $('source-list').prepend(panel);
  }
}
