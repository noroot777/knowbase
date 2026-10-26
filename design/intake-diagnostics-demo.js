(() => {
  'use strict';

  const byId = id => document.getElementById(id);
  const escapeHtml = value => String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${window.diagnosticIcons[name] || ''}</svg>`;
  const clock = () => new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  const cloneSeed = () => structuredClone(window.diagnosticSeed);
  const tabNames = ['attention', 'queued', 'running', 'resolved', 'archived'];
  const explanations = {
    attention: '需要你决定，不会自行恢复', queued: '已排队，不代表正在抓取',
    running: '仅显示此页正在模拟的工作', resolved: '必要环节全部完成后才进入这里',
    archived: '暂缓与不再收录，不冒充已解决'
  };
  let issues = cloneSeed();
  let currentTab = 'attention';
  let activeId = issues[0].id;
  let selected = new Set();
  let operationLog = [];
  let busy = false;
  let revision = 0;
  let undoState = null;
  let toastTimer;
  let confirmAction = null;

  function tabFor(issue) {
    return ['snoozed', 'excluded'].includes(issue.status) ? 'archived' : issue.status;
  }

  function visibleIssues() {
    const query = byId('search').value.trim().toLocaleLowerCase();
    const source = byId('source-filter').value;
    const stage = byId('stage-filter').value;
    return issues.filter(issue => tabFor(issue) === currentTab &&
      (source === 'all' || issue.source === source) &&
      (stage === 'all' || issue.stage === stage) &&
      (!query || [issue.title, issue.source, issue.subscription, issue.reason, issue.evidence]
        .some(value => value.toLocaleLowerCase().includes(query))));
  }

  function statusText(issue) {
    if (issue.status === 'attention') return issue.type === 'held' ? '待人工确认' : issue.type === 'source' ? '来源受限' : issue.attempts >= 3 ? '重试已停止' : '待决定重试';
    return ({ queued: '待执行', running: '演示执行中', resolved: '已完成（演示）',
      snoozed: '已暂缓', excluded: '不再收录' })[issue.status];
  }

  function addHistory(issue, message) {
    issue.history.unshift([clock(), message]);
  }

  function showToast(message, actionLabel = '', action = null) {
    clearTimeout(toastTimer);
    const toast = byId('toast');
    toast.replaceChildren(document.createTextNode(message));
    if (actionLabel && action) {
      const button = document.createElement('button');
      button.textContent = actionLabel;
      button.addEventListener('click', () => { toast.hidden = true; action(); }, { once: true });
      toast.append(button);
    }
    toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, 6500);
  }

  function record(action, targets, detail) {
    operationLog.unshift({ time: clock(), action, titles: targets.map(issue => issue.title), detail });
  }

  function transaction(action, targets, change, message) {
    if (busy) return showToast('演示正在执行，请等本轮结束后再修改待办。');
    const snapshot = structuredClone(issues);
    targets.forEach(change);
    revision += 1;
    undoState = { issues: snapshot, revision, action };
    record(action, targets, message);
    selected.clear();
    render();
    const expectedRevision = revision;
    showToast(message, '撤销', () => {
      if (!undoState || busy || expectedRevision !== revision) return showToast('状态已经变化，不能撤销这一步。');
      issues = undoState.issues;
      undoState = null;
      revision += 1;
      record('撤销处理', targets, `已撤销「${action}」，恢复先前状态`);
      render();
      showToast('已恢复操作前的状态；操作记录仍保留。');
    });
  }

  function setTab(nextTab) {
    currentTab = nextTab;
    selected.clear();
    activeId = '';
    render();
  }

  function render() {
    const visible = visibleIssues();
    if (!visible.some(issue => issue.id === activeId)) activeId = visible[0]?.id || '';
    const visibleIds = new Set(visible.map(issue => issue.id));
    selected = new Set([...selected].filter(id => visibleIds.has(id)));
    for (const tab of tabNames) {
      byId(`count-${tab}`).textContent = issues.filter(issue => tabFor(issue) === tab).length;
      const button = byId(`tab-${tab}`);
      button.setAttribute('aria-selected', String(tab === currentTab));
      button.tabIndex = tab === currentTab ? 0 : -1;
    }
    byId('workbench').setAttribute('aria-labelledby', `tab-${currentTab}`);
    byId('nav-count').textContent = issues.filter(issue => issue.status === 'attention').length;
    byId('tab-explanation').textContent = explanations[currentTab];
    byId('visible-count').textContent = `${visible.length} 条`;
    byId('log-count').textContent = operationLog.length;
    const queued = issues.filter(issue => issue.status === 'queued');
    byId('run-label').textContent = busy ? '演示执行中…' : `执行待办${queued.length ? ` · ${queued.length}` : ''}`;
    byId('run-queue').disabled = busy || !queued.length;
    byId('reset-demo').disabled = busy;
    byId('health-dot').classList.toggle('is-running', busy);
    byId('health-title').textContent = busy ? '正在模拟执行，不会向真实站点发起请求' : '管道空闲，没有后台抓取在运行';
    byId('health-hint').textContent = busy ? '仅本次明确选择的样例会执行；其他排队项保持原状。' : '待执行只代表已排队。点击「执行待办」才会开始本页模拟。';
    renderList(visible);
    renderSelection(visible);
    renderDetail(issues.find(issue => issue.id === activeId));
  }

  function renderList(visible) {
    byId('issue-list').innerHTML = visible.length ? visible.map(issue => {
      const tone = issue.status === 'attention' && issue.type === 'held' ? 'held' : issue.status;
      const summary = issue.status === 'queued' ? (issue.manualBody ? '正文已补入，等待后续入库' : '未开始 · 等待调度')
        : issue.status === 'running' ? '本页正在模拟处理' : issue.status === 'resolved' ? '已完成必要环节（模拟）'
        : issue.status === 'excluded' ? '此条不再自动执行，可恢复' : issue.status === 'snoozed' ? '保留记录，等待手动恢复'
        : issue.type === 'held' ? '已挂起，不会自动重试' : `已尝试 ${issue.attempts} 次 · ${issue.stage}`;
      const disabled = busy || issue.status === 'resolved';
      return `<article class="issue-row${issue.id === activeId ? ' is-selected' : ''}">
        <label class="row-check"><input type="checkbox" data-select="${issue.id}" aria-label="选择 ${escapeHtml(issue.title)}" ${selected.has(issue.id) ? 'checked' : ''} ${disabled ? 'disabled' : ''}></label>
        <button class="issue-select" data-item="${issue.id}" aria-pressed="${issue.id === activeId}">
          <span class="row-meta"><span class="source-badge">${escapeHtml(issue.source === 'RSS' ? 'RSS' : issue.source.slice(0, 1))}</span><span>${escapeHtml(issue.subscription)}</span><time>${issue.date}</time></span>
          <h3>${escapeHtml(issue.title)}</h3><span class="row-summary"><span class="status ${tone}">${statusText(issue)}</span><span class="row-separator">/</span><span>${escapeHtml(summary)}</span></span>
        </button></article>`;
    }).join('') : `<div class="empty-state">${icon(currentTab === 'resolved' ? 'check' : 'inbox')}<h3>${currentTab === 'running' ? '现在没有任务在执行' : '这里暂时没有内容'}</h3><p>${currentTab === 'running' ? '排队的条目不会被误报成正在运行。' : '可以切换状态，或清除来源与搜索条件。'}</p><button data-action="clear-filters">清除筛选</button>${currentTab === 'running' ? '<button data-action="view-resolved">查看已完成 →</button>' : ''}</div>`;
  }

  function renderSelection(visible) {
    const selectable = visible.filter(issue => !busy && issue.status !== 'resolved');
    byId('select-all').checked = selectable.length > 0 && selectable.every(issue => selected.has(issue.id));
    byId('select-all').indeterminate = selected.size > 0 && selected.size < selectable.length;
    byId('select-all').disabled = !selectable.length;
    byId('selection-label').textContent = selected.size ? `已选 ${selected.size} 条` : '选择本页';
    byId('batch-actions').hidden = !selected.size;
    byId('list-order').hidden = !!selected.size;
    const retry = document.querySelector('[data-batch="retry"], [data-batch="restore"]');
    retry.dataset.batch = currentTab === 'archived' ? 'restore' : 'retry';
    retry.textContent = currentTab === 'archived' ? '恢复处理' : '重新排队';
    retry.hidden = currentTab === 'queued';
    retry.disabled = busy || (currentTab !== 'archived' && currentTab !== 'attention');
    document.querySelector('[data-batch="run"]').hidden = currentTab !== 'queued';
    document.querySelector('[data-batch="snooze"]').hidden = currentTab === 'archived';
    const selectedSourceIssue = visible.some(issue => selected.has(issue.id) && issue.stage === '来源连接');
    document.querySelector('[data-batch="exclude"]').disabled = busy || currentTab === 'archived' || selectedSourceIssue;
    document.querySelector('[data-batch="exclude"]').title = selectedSourceIssue ? '来源连接问题只能重试或暂缓，不能按单篇内容排除' : '';
  }

  function pipeline(issue) {
    const stages = issue.stage === '来源连接' ? ['访问来源', '读取时间线', '检查覆盖', '结束检查'] : ['发现链接', '补齐正文', '翻译处理', '写入知识库'];
    const position = issue.manualBody ? 2 : ({ '来源连接': 0, '正文抓取': 1, '翻译': 2, '入库处理': 3 })[issue.stage];
    return stages.map((name, index) => {
      const complete = issue.status === 'resolved' || index < position;
      const current = index === position;
      const tone = complete ? 'done' : !current ? '' : issue.status === 'attention' ? 'blocked'
        : issue.status === 'running' ? 'active' : 'waiting';
      const symbol = complete ? '✓' : current && issue.status === 'attention' ? '!' : index + 1;
      return `<div class="pipeline-step ${tone}"><span class="step-point">${symbol}</span><span>${name}</span></div>`;
    }).join('');
  }

  function renderDetail(issue) {
    if (!issue) {
      byId('detail-pane').innerHTML = `<div class="empty-state">${icon('file')}<h3>选择一条，看看下一步</h3><p>失败原因、处理建议和记录会显示在这里。</p></div>`;
      return;
    }
    const archived = ['snoozed', 'excluded'].includes(issue.status);
    const tone = issue.status === 'resolved' ? 'success' : issue.status === 'attention' ? '' : 'neutral';
    const reason = issue.status === 'queued' ? '已经排队，但还没有开始执行' : issue.status === 'running' ? '本条正在演示执行中'
      : issue.status === 'resolved' ? issue.stage === '来源连接' ? '来源复查已完成（本页模拟）' : '必要处理已完成（本页模拟）' : archived ? issue.status === 'excluded' ? '已决定不再收录此条' : '已暂缓，保留问题记录' : issue.reason;
    const explanation = issue.status === 'queued' ? (issue.manualBody ? '手动补入的正文已保留在本页内存中。接下来执行后续处理，不再重复抓取正文。' : '不会在后台持续补抓。可以执行这一条，或与其他待办一起运行。')
      : issue.status === 'running' ? '这是模拟进度，没有真实网络请求。执行结果出来后，才会更新最终状态。'
      : issue.status === 'resolved' ? issue.stage === '来源连接' ? '仅代表这条来源检查问题已恢复，不代表发现了新文章或已完成文章入库。没有访问真实账号。' : '演示流程已完成。此原型没有向真实知识库写入文档。'
      : archived ? '这不是问题已解决，也不会计入成功数。恢复处理会回到搁置前的状态，不会立刻发起抓取。' : issue.explanation;
    let actions = '';
    if (issue.status === 'attention') {
      const preferManual = issue.stage === '正文抓取' && (issue.preferred === 'manual' || issue.type === 'held');
      const retryButton = `<button class="button ${preferManual ? 'secondary' : 'primary'}" data-action="retry">${icon('sync')}重新排队</button>`;
      const manualButton = issue.stage === '正文抓取' ? `<button class="button ${preferManual ? 'primary' : 'secondary'}" data-action="manual">${icon('edit')}手动补正文</button>` : '';
      actions = preferManual ? manualButton + retryButton : retryButton + manualButton;
    } else if (issue.status === 'queued') {
      actions = `<button class="button primary" data-action="run">${icon('play')}执行此条（演示）</button>`;
      if (issue.stage === '正文抓取') actions += '<button class="button secondary" data-action="manual">手动补正文</button>';
    } else if (archived) actions = `<button class="button primary" data-action="restore">${icon('sync')}恢复处理</button>`;
    else if (issue.status === 'running') actions = '<button class="button primary" disabled>等待演示结果…</button>';
    else actions = '<button class="button secondary" data-action="logs">查看处理记录</button>';
    const advice = issue.status === 'attention' ? issue.recommendation : issue.status === 'resolved' ? '这条已完成，不需要重新排队。完整操作记录会保留下来。'
      : archived ? '你随时可以改变决定。恢复操作不会清空历史，也不会把尚未完成的内容当成成功。'
      : issue.status === 'running' ? '你可以继续查看其他条目；本轮演示只处理事先确认的范围。' : '执行前会再次展示范围。演示结果可选择成功或失败，便于查看两种处理分支。';
    byId('detail-pane').innerHTML = `<button class="detail-back" data-action="back-list">← 返回问题列表</button><div class="detail-eyebrow"><span>问题详情</span><span>· ${escapeHtml(issue.stage)}</span><span>${issue.id}</span></div>
      <h2 class="detail-title">${escapeHtml(issue.title)}</h2><div class="detail-source"><span>${escapeHtml(issue.source)}</span><span>·</span><span>${escapeHtml(issue.subscription)}</span>${issue.url ? `<a href="${escapeHtml(issue.url)}" target="_blank" rel="noopener noreferrer">打开原文 ${icon('external')}</a>` : '<span>· 样例未提供原文链接</span>'}</div>
      <div class="pipeline" aria-label="收录进度">${pipeline(issue)}</div>
      <div class="reason-box ${tone}"><strong>${escapeHtml(reason)}</strong><p>${escapeHtml(explanation)}</p></div>
      <dl class="detail-facts"><div><dt>已尝试次数</dt><dd>${issue.attempts} 次 · 记录不随重排抹去</dd></div><div><dt>当前是否执行</dt><dd>${issue.status === 'running' ? '是，仅本页模拟' : '否'}</dd></div></dl>
      <section class="recommended"><h3 class="section-caption">${archived ? '改变这个决定' : '建议的下一步'}</h3><p>${escapeHtml(advice)}</p><div class="detail-actions">${actions}</div>${!archived && !['resolved', 'running'].includes(issue.status) ? `<div class="minor-actions">${issue.stage === '正文抓取' ? '<button data-action="link">换个链接再试</button>' : '<span></span>'}<button data-action="snooze">暂缓处理</button>${issue.stage !== '来源连接' ? '<button data-action="exclude" class="danger-text">不再收录</button>' : ''}</div>` : ''}</section>
      <section class="item-history"><h3 class="section-caption">这条内容的处理记录</h3>${issue.history.slice(0, 3).map(([time, message]) => `<div class="history-entry"><time>${escapeHtml(time)}</time><p>${escapeHtml(message)}</p></div>`).join('')}<details><summary class="dialog-note">查看原始原因${issue.manualBody ? '与补入正文' : ''}</summary><p class="dialog-note">${escapeHtml(issue.evidence)}</p>${issue.manualBody ? `<p class="dialog-note">${escapeHtml(issue.manualBody.slice(0, 300))}</p>` : ''}</details></section>`;
    byId('detail-pane').querySelectorAll('[data-action]').forEach(button => { button.disabled = (busy && button.dataset.action !== 'back-list') || button.disabled; });
  }

  function openDialog(title, body, label, onConfirm, danger = false) {
    byId('dialog-title').textContent = title;
    byId('dialog-body').innerHTML = body;
    byId('dialog-error').textContent = '';
    byId('dialog-confirm').textContent = label;
    byId('dialog-confirm').className = `button ${danger ? 'danger' : 'primary'}`;
    byId('dialog-confirm').hidden = !onConfirm;
    byId('dialog-cancel').textContent = onConfirm ? '取消' : '关闭';
    confirmAction = onConfirm;
    byId('action-dialog').showModal();
    (byId('action-dialog').querySelector('textarea, input, select') || byId('dialog-cancel')).focus();
  }

  function scopeMarkup(targets) {
    return `<div class="dialog-scope"><b>仅处理 ${targets.length} 条演示样例</b><ul>${targets.slice(0, 4).map(issue => `<li>${escapeHtml(issue.title)}</li>`).join('')}${targets.length > 4 ? `<li>另 ${targets.length - 4} 条已选内容</li>` : ''}</ul></div>`;
  }

  function requestAction(action, targets) {
    if (busy) return showToast('本轮演示尚未结束，暂时不能修改状态。');
    const eligible = targets.filter(issue => action === 'retry' ? issue.status === 'attention'
      : action === 'restore' ? ['snoozed', 'excluded'].includes(issue.status)
      : ['attention', 'queued'].includes(issue.status) && (action !== 'exclude' || issue.stage !== '来源连接'));
    if (!eligible.length) return showToast('当前选择中没有适用于此操作的条目。');
    const descriptions = {
      retry: ['重新排队这些内容？', '重新开放一轮处理机会，保留历史尝试次数。不会立刻抓取，也不计为已解决。', '确认重新排队'],
      snooze: ['暂缓处理这些内容？', '从待办中移出，保留问题、原文链接与处理记录。不会自动恢复；你可以在「已搁置」中手动恢复。', '确认暂缓'],
      exclude: ['不再收录这些内容？', '仅排除此处列出的内容，不停用整个订阅，也不删除已经入库的文档。可在「已搁置」中恢复。', '确认不再收录'],
      restore: ['恢复处理这些内容？', '回到搁置前的状态，保留之前的失败记录。不会直接执行，也不会清空重试历史。', '确认恢复']
    };
    const [title, description, label] = descriptions[action];
    openDialog(title, `<p>${description}</p>${scopeMarkup(eligible)}<p class="dialog-note">仅修改本页演示数据，不影响真实内容或订阅。</p>`, label, () => {
      transaction(label, eligible, issue => {
        if (action === 'retry') { issue.status = 'queued'; addHistory(issue, '人工重新排队，尚未开始执行'); }
        if (action === 'snooze' || action === 'exclude') {
          issue.beforeArchive = issue.status;
          issue.status = action === 'snooze' ? 'snoozed' : 'excluded';
          addHistory(issue, action === 'snooze' ? '已暂缓，等待手动恢复' : '人工选择不再收录此条（可恢复）');
        }
        if (action === 'restore') { issue.status = issue.beforeArchive || 'attention'; addHistory(issue, '已恢复搁置前状态，未自动执行'); }
      }, action === 'retry' ? `${eligible.length} 条已排队，尚未执行。可切换「待执行」查看。` : `${eligible.length} 条已${action === 'restore' ? '恢复原状态' : action === 'snooze' ? '暂缓' : '标记为不再收录'}（演示）。`);
    }, action === 'exclude');
  }

  function manualBody(issue) {
    openDialog('补入这篇文章的正文', `<p>只替换本条的正文，之后仍需执行后续入库。不改变订阅，也不把待入库当成已完成。</p>${scopeMarkup([issue])}<label class="dialog-field">文章正文<textarea id="manual-body" placeholder="粘贴你已经核对过的文章正文，至少 40 个字符。">${escapeHtml(issue.manualBody || '')}</textarea><small>按纯文本保存，不执行粘贴内容中的 HTML 或脚本。</small></label>`, '保存正文并排队', () => {
      const body = byId('manual-body').value.trim();
      if (body.length < 40) { byId('dialog-error').textContent = '请补入至少 40 个字符的正文；标题或一句提示不能代替正文。'; return false; }
      transaction('手动补入正文', [issue], target => {
        target.manualBody = body;
        target.stage = '入库处理';
        target.status = 'queued';
        addHistory(target, '正文已手动补入，等待后续入库（演示）');
      }, '正文已补入，后续入库已排队，尚未完成（演示）。');
    });
  }

  function changeLink(issue) {
    openDialog('换一个原文链接', `<p>适合原链接指向摘要页、失效地址或跳转页的情况。保存后重新排队，不会当场发起请求。</p><label class="dialog-field">可直接阅读正文的网页链接<input id="replacement-url" type="url" placeholder="https://…" value="${escapeHtml(issue.url)}"><small>这里只演示 HTTP / HTTPS 网页链接，不处理 PDF 或需要授权的内容。</small></label>`, '保存链接并排队', () => {
      let url;
      try { url = new URL(byId('replacement-url').value.trim()); } catch { byId('dialog-error').textContent = '请输入完整的 http:// 或 https:// 网页链接。'; return false; }
      if (!['http:', 'https:'].includes(url.protocol) || /\.pdf$/i.test(url.pathname)) {
        byId('dialog-error').textContent = '请使用 HTTP / HTTPS 正文网页，而不是脚本或 PDF 地址。'; return false;
      }
      transaction('替换原文链接', [issue], target => {
        target.url = url.href;
        target.status = 'queued';
        addHistory(target, '原文链接已更换，等待重新抓取（演示）');
      }, '新链接已保存并排队，还没有抓取（演示）。');
    });
  }

  function requestRun(targets) {
    if (busy) return;
    const queued = targets.filter(issue => issue.status === 'queued');
    if (!queued.length) return showToast('没有可执行的待办。请先重新排队。');
    openDialog('执行这批待办（演示）', `<p>只执行下面明确列出的范围，不附带其他来源。所有结果都是模拟，不会联网或写入真实知识库。</p>${scopeMarkup(queued)}<label class="dialog-field">这次要查看的演示分支<select id="simulation-result"><option value="success">模拟成功 → 完成对应的必要处理</option><option value="failure">模拟失败 → 返回需处理，保留记录</option></select></label>`, '开始模拟执行', () => {
      runSimulation(queued, byId('simulation-result').value);
    });
  }

  async function runSimulation(targets, outcome) {
    busy = true;
    revision += 1;
    undoState = null;
    byId('toast').hidden = true;
    selected.clear();
    byId('source-filter').value = 'all';
    byId('stage-filter').value = 'all';
    byId('search').value = '';
    currentTab = 'running';
    record('开始模拟执行', targets, `仅运行这 ${targets.length} 条；预设分支：${outcome === 'success' ? '成功' : '失败'}`);
    for (const issue of targets) {
      issue.status = 'running';
      activeId = issue.id;
      addHistory(issue, '本页开始模拟执行，没有真实网络请求');
      render();
      await new Promise(resolve => setTimeout(resolve, 1400));
      issue.attempts += 1;
      if (outcome === 'success') {
        issue.status = 'resolved';
        issue.reason = '本页模拟成功，必要环节已完成';
        addHistory(issue, issue.stage === '来源连接' ? '模拟结果：来源复查完成，不代表新文章已入库' : '模拟结果：必要环节与入库已完成');
      } else {
        issue.status = 'attention';
        issue.type = 'failed';
        issue.reason = '本次模拟执行失败，等待你决定';
        issue.explanation = '演示失败分支：没有取得可确认的成功结果，因此回到需处理，不自动再次执行。';
        issue.evidence = '本页预设的模拟失败，不是生产环境的失败证据';
        issue.recommendation = '核对原因后可以重新排队、补正文或暂缓。已发生的尝试继续保留。';
        addHistory(issue, '模拟结果：执行失败，回到需处理');
      }
    }
    busy = false;
    currentTab = outcome === 'success' ? 'resolved' : 'attention';
    activeId = targets[0].id;
    record('模拟执行结束', targets, outcome === 'success' ? '模拟成功；没有真实写库' : '模拟失败；没有把失败计为已完成');
    render();
    showToast(outcome === 'success' ? `${targets.length} 条模拟处理完成，没有写入生产库。` : `${targets.length} 条模拟失败，已回到「需处理」。`);
  }

  function showLog() {
    const active = issues.find(issue => issue.id === activeId);
    const body = operationLog.length ? `<div class="log-list">${operationLog.map(entry => `<div class="log-row"><time>本页 ${entry.time}</time><strong>${escapeHtml(entry.action)} · ${entry.titles.length} 条</strong><p>${escapeHtml(entry.titles.join('、'))}</p><p>${escapeHtml(entry.detail)}</p></div>`).join('')}</div>`
      : `<p>本页尚未进行任何处理。列表中的既有记录为演示样例。</p>${active ? `<div class="dialog-scope"><b>${escapeHtml(active.title)}</b>${active.history.map(([time, message]) => `<p>${escapeHtml(time)} · ${escapeHtml(message)}</p>`).join('')}</div>` : ''}`;
    openDialog('处理记录', `${body}<p class="dialog-note">记录仅在本页内存中保留，刷新或重置后恢复初始样例。</p>`, '', null);
  }

  document.querySelectorAll('[data-icon]').forEach(element => { element.outerHTML = icon(element.dataset.icon); });
  document.querySelectorAll('[data-tab]').forEach(button => {
    button.addEventListener('click', () => setTab(button.dataset.tab));
    button.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const position = tabNames.indexOf(currentTab);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabNames.length - 1 :
        (position + (event.key === 'ArrowRight' ? 1 : -1) + tabNames.length) % tabNames.length;
      setTab(tabNames[next]);
      byId(`tab-${tabNames[next]}`).focus();
    });
  });
  ['search', 'source-filter', 'stage-filter'].forEach(id => byId(id).addEventListener(id === 'search' ? 'input' : 'change', () => { selected.clear(); render(); }));
  byId('issue-list').addEventListener('click', event => {
    const button = event.target.closest('[data-item]');
    if (button) {
      activeId = button.dataset.item;
      render();
      if (matchMedia('(max-width: 740px)').matches) byId('detail-pane').scrollIntoView({
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start'
      });
    }
  });
  byId('issue-list').addEventListener('change', event => {
    const checkbox = event.target.closest('[data-select]');
    if (!checkbox) return;
    if (checkbox.checked) selected.add(checkbox.dataset.select); else selected.delete(checkbox.dataset.select);
    renderSelection(visibleIssues());
  });
  byId('select-all').addEventListener('change', event => {
    selected = new Set(event.target.checked ? visibleIssues().filter(issue => issue.status !== 'resolved' && !busy).map(issue => issue.id) : []);
    render();
  });
  byId('clear-selection').addEventListener('click', () => { selected.clear(); render(); });
  document.querySelectorAll('[data-batch]').forEach(button => button.addEventListener('click', () => {
    const targets = issues.filter(issue => selected.has(issue.id));
    if (button.dataset.batch === 'run') requestRun(targets); else requestAction(button.dataset.batch, targets);
  }));
  byId('run-queue').addEventListener('click', () => requestRun(issues.filter(issue => issue.status === 'queued')));
  byId('show-log').addEventListener('click', showLog);
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    if (action === 'back-list') {
      byId('workbench').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
      return;
    }
    if (action === 'clear-filters') {
      byId('search').value = ''; byId('source-filter').value = 'all'; byId('stage-filter').value = 'all'; render(); return;
    }
    if (action === 'view-resolved') return setTab('resolved');
    const issue = issues.find(item => item.id === activeId);
    if (!issue) return;
    if (['retry', 'restore', 'snooze', 'exclude'].includes(action)) requestAction(action, [issue]);
    else if (action === 'manual') manualBody(issue);
    else if (action === 'link') changeLink(issue);
    else if (action === 'run') requestRun([issue]);
    else if (action === 'logs') showLog();
  });
  byId('dialog-close').addEventListener('click', () => byId('action-dialog').close());
  byId('dialog-cancel').addEventListener('click', () => byId('action-dialog').close());
  byId('dialog-confirm').addEventListener('click', () => { if (confirmAction && confirmAction() !== false) byId('action-dialog').close(); });
  byId('action-dialog').addEventListener('close', () => { confirmAction = null; });
  byId('reset-demo').addEventListener('click', () => {
    openDialog('重置这个演示？', '<p>清空本页操作记录，恢复最初的 12 条样例。不会触碰真实数据。</p>', '重置演示', () => {
      issues = cloneSeed(); currentTab = 'attention'; activeId = issues[0].id; selected.clear(); operationLog = [];
      revision += 1; undoState = null;
      byId('search').value = ''; byId('source-filter').value = 'all'; byId('stage-filter').value = 'all';
      render(); showToast('已恢复初始演示数据。');
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key === '/' && !byId('action-dialog').open && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
      event.preventDefault(); byId('search').focus();
    }
  });
  render();
})();
