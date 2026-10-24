let modelSettingsRevision = -1;
let modelSettingsEpoch = 0;
const modelSettingsReads = { ask: 0, fav: 0 };
const modelSettingsApplied = { ask: -1, fav: -1 };
const modelSettingsRequested = { ask: -1, fav: -1 };
const modelSettingsPending = { ask: null, fav: null };
let modelAskSelection = null;

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
  const read = { sequence: ++modelSettingsReads[target], epoch: modelSettingsEpoch };
  modelSettingsRequested[target] = modelSettingsRevision;
  modelSettingsPending[target] = read;
  return read;
}

function ownsModelRead(target, read, ...responses) {
  return read.sequence === modelSettingsReads[target] && read.epoch === modelSettingsEpoch
    && responses.every(data => !Number.isSafeInteger(data.revision) || data.revision >= modelSettingsRevision);
}

function appliedModelSettings(target, data) {
  recordModelSettings(data);
  if (Number.isSafeInteger(data.revision)) modelSettingsApplied[target] = data.revision;
}

function finishModelRead(target, read) {
  if (modelSettingsPending[target] === read) modelSettingsPending[target] = null;
  reconcileModelConsumers();
}

function reconcileModelConsumers() {
  for (const target of ['ask', 'fav']) {
    if (modelSettingsApplied[target] >= modelSettingsRevision
        || modelSettingsRequested[target] >= modelSettingsRevision
        || modelSettingsPending[target]) continue;
    if (target === 'ask') loadModels();
    else initFavTools(true);
  }
}

function refreshModelConsumers(data = {}, preserveAsk = false) {
  if (Number.isSafeInteger(data.revision) && data.revision < modelSettingsRevision) return;
  if (!preserveAsk) modelAskSelection = null;
  recordModelSettings(data);
  for (const [key, target] of [['ask', 'ask-model'], ['fav', 'fav-model']]) {
    const stage = data.stages?.find(entry => entry.key === key);
    let value = stage ? (stage.provider && stage.model ? stage.provider + ':' + stage.model : '')
      : key === 'ask' ? data.default : data.model;
    if (value === undefined) continue;
    if (key === 'ask' && modelAskSelection !== null) value = modelAskSelection;
    const select = $(target);
    if (value && ![...select.options].some(option => option.value === value)) {
      select.insertAdjacentHTML('afterbegin', `<option value="${esc(value)}">${esc(modelLabel(value))}</option>`);
    }
    select.value = value;
    appliedModelSettings(key, data);
  }
  loadModels();
  initFavTools(true);
}

document.addEventListener('change', event => {
  if (event.target.id === 'ask-model') modelAskSelection = event.target.value;
});
