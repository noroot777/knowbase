let modelSettingsRevision = -1;
let modelSettingsEpoch = 0;
const modelSettingsReads = { ask: 0, fav: 0 };

function modelOperationId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

function recordModelSettings(data) {
  if (Number.isSafeInteger(data.revision)) {
    modelSettingsRevision = Math.max(modelSettingsRevision, data.revision);
  }
}

function invalidateModelSettings() {
  modelSettingsEpoch++;
  modelSettingsReads.ask++;
  modelSettingsReads.fav++;
  favToolsInited = false;
}

function beginModelRead(target) {
  return { sequence: ++modelSettingsReads[target], epoch: modelSettingsEpoch };
}

function ownsModelRead(target, read, ...responses) {
  return read.sequence === modelSettingsReads[target] && read.epoch === modelSettingsEpoch
    && responses.every(data => !Number.isSafeInteger(data.revision) || data.revision >= modelSettingsRevision);
}

function refreshModelConsumers(data = {}, preserveAsk = false) {
  if (Number.isSafeInteger(data.revision) && data.revision < modelSettingsRevision) return;
  recordModelSettings(data);
  for (const [key, target] of [['ask', 'ask-model'], ['fav', 'fav-model']]) {
    const stage = data.stages?.find(entry => entry.key === key);
    const value = stage ? (stage.provider && stage.model ? stage.provider + ':' + stage.model : '')
      : key === 'ask' ? data.default : data.model;
    if (value === undefined) continue;
    const select = $(target);
    if (value && ![...select.options].some(option => option.value === value)) {
      select.insertAdjacentHTML('afterbegin', `<option value="${esc(value)}">${esc(modelLabel(value))}</option>`);
    }
    select.value = value;
  }
  loadModels(preserveAsk);
  initFavTools(true);
}

document.addEventListener('change', event => {
  if (event.target.id === 'ask-model') modelSettingsReads.ask++;
});
