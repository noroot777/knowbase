(() => {
  'use strict';
  const element = id => document.getElementById(id);
  const itemKey = new URLSearchParams(location.search).get('item');
  let snapshot = null;
  let timer = null;
  let processing = false;
  let drafting = false;
  let continued = '';
  let loadVersion = 0;
  let pendingSignature = '';
  let loadError = '';
  let actionError = '';

  async function api(path, options = {}) {
    const response = await fetch(`/api/browser-capture${path}`, {
      cache: 'no-store', headers: {'Content-Type': 'application/json'}, ...options
    });
    return apiResponse(response, '补抓请求失败');
  }

  function message(text, error = false) {
    element('message').textContent = text;
    element('message').classList.toggle('error', error);
  }

  function dateTime(timestamp) {
    return new Date(timestamp * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'});
  }

  function renderPending(waiting) {
    const signature = JSON.stringify(waiting);
    if (signature !== pendingSignature) {
      pendingSignature = signature;
      element('pending-list').replaceChildren();
      for (const request of waiting) {
        const row = document.createElement('li');
        const note = document.createElement('p');
        note.className = 'hint';
        note.textContent = `${dateTime(request.created_at)} 发起 · ${dateTime(request.expires_at)} 到期 · ${request.id.slice(-8)}`;
        const url = document.createElement('p');
        url.className = 'url';
        url.textContent = request.page_url;
        const cancel = document.createElement('button');
        cancel.className = 'secondary';
        cancel.dataset.cancelIntent = request.id;
        cancel.textContent = '取消此请求';
        row.append(note, url, cancel);
        element('pending-list').append(row);
      }
    }
    element('pending').hidden = !waiting.length;
    element('pending-list').querySelectorAll('button').forEach(button => { button.disabled = processing; });
  }

  function render() {
    if (!snapshot) return;
    if (loadError) {
      for (const name of ['create', 'continue', 'restart', 'page-url', 'draft-cancel']) element(name).disabled = true;
      element('pending-list').querySelectorAll('button').forEach(button => { button.disabled = true; });
      element('saved').hidden = true;
      message(loadError, true);
      return;
    }
    const saved = snapshot.saved;
    const pending = snapshot.pending[0];
    const locked = !!pending || (!!saved && !drafting);
    element('title').textContent = snapshot.title;
    element('original-url').textContent = snapshot.url;
    element('kind').textContent = snapshot.kind;
    if (pending) element('page-url').value = pending.page_url;
    else if (!element('page-url').value) element('page-url').value = snapshot.url;
    element('page-url').disabled = processing || locked;
    element('create').disabled = processing || locked;
    element('create').textContent = pending ? '等待浏览器采集' : saved && !drafting ? '正文已补入' : '发起浏览器采集';
    element('draft-cancel').hidden = !drafting || !saved || !!pending;
    element('draft-cancel').disabled = processing;
    element('saved').hidden = !saved;
    element('continue').disabled = processing || !!pending || !saved || continued === saved.id;
    element('restart').hidden = drafting || !!pending;
    element('restart').disabled = processing;
    element('step-capture').classList.toggle('on', !!pending || !!saved);
    element('step-process').classList.toggle('on', !!saved);
    element('intent-note').textContent = pending ? `请求 ${pending.id.slice(-8)} · ${dateTime(pending.expires_at)} 到期`
      : saved ? `回执 ${saved.id.slice(-8)} · 已持久保存` : '';
    renderPending(snapshot.pending);
    if (saved) element('receipt').textContent = `本次补入 ${saved.receipt.characters} 字符 · ${new Date(saved.receipt.captured_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})}`;
    if (pending) message('等待你在文章标签中主动采集；同一篇其他标签会共用此请求，本页没有读取浏览器标签。');
    else if (drafting && saved) message('已保存的正文和回执仍然保留。请核对网址再发起，也可以退出重新采集。');
    else if (saved) message('正文已补回原条目。翻译与入库尚待执行。');
    else if (snapshot.saved_error) message(snapshot.saved_error, true);
    else message('核对文章网址，然后发起一次采集请求。');
    if (actionError) message(actionError, true);
  }

  async function refresh() {
    clearTimeout(timer);
    const version = ++loadVersion;
    try {
      const state = await api(`/target/${encodeURIComponent(itemKey)}`);
      if (version !== loadVersion) return;
      if (state.saved?.id !== snapshot?.saved?.id) {
        drafting = false;
        continued = '';
        element('run-note').textContent = '';
        actionError = '';
      }
      loadError = '';
      snapshot = state;
      render();
    } catch (error) {
      if (version !== loadVersion) return;
      loadError = error.message;
      element('create').disabled = true;
      element('continue').disabled = true;
      element('restart').disabled = true;
      element('saved').hidden = true;
      message(error.message, true);
    } finally {
      if (version === loadVersion) timer = setTimeout(refresh, 2000);
    }
  }

  element('create').addEventListener('click', async () => {
    if (processing) return;
    processing = true;
    actionError = '';
    render();
    try {
      await api('/intents', {method: 'POST', body: JSON.stringify({key: itemKey, page_url: element('page-url').value.trim()})});
      await refresh();
    } catch (error) { actionError = error.message; }
    finally { processing = false; render(); }
  });

  element('pending-list').addEventListener('click', async event => {
    const cancel = event.target.closest('[data-cancel-intent]');
    if (processing || !cancel) return;
    processing = true;
    actionError = '';
    render();
    try {
      await api(`/intents/${encodeURIComponent(cancel.dataset.cancelIntent)}`, {method: 'DELETE'});
      await refresh();
    } catch (error) { actionError = error.message; }
    finally { processing = false; render(); }
  });

  element('restart').addEventListener('click', async () => {
    if (processing || !snapshot?.saved) return;
    processing = true;
    const confirmed = await confirmBox({title: '重新采集正文？', body: '重新采集不会撤销已保存的正文和回执；只有再次预览确认后才会替换。', ok: '重新采集'});
    processing = false;
    if (confirmed) drafting = true;
    render();
  });

  element('draft-cancel').addEventListener('click', () => { drafting = false; render(); });

  element('pair').addEventListener('click', async () => {
    try {
      const pairing = await api('/pairing');
      element('pairing-key').value = pairing.key;
      element('pairing').hidden = false;
    } catch (error) { toast(error.message, {key: 'capture-pairing'}); }
  });

  element('copy-key').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(element('pairing-key').value);
      toast('密钥已复制，只粘贴到你安装的 Knowbase 扩展中。', {type: 'ok', key: 'capture-copy'});
    } catch {
      element('pairing-key').type = 'text';
      element('pairing-key').select();
      toast('无法自动复制，请复制选中的配对密钥。', {type: 'info', detail: '按 Cmd-C（Windows 为 Ctrl-C）复制已选中的文字。', key: 'capture-copy'});
    }
  });

  element('continue').addEventListener('click', async () => {
    if (processing || !snapshot?.saved) return;
    const identity = snapshot.saved.id;
    processing = true;
    const confirmed = await confirmBox({title: '继续该来源后续处理？', body: '继续会处理该来源其他待办，可能补抓其他文章、调用翻译及提取模型，索引会检查全库。', ok: '开始处理'});
    if (!confirmed) { processing = false; render(); return; }
    if (snapshot.saved?.id !== identity) {
      processing = false;
      render();
      toast('保存回执已经变化，请核对当前内容后再操作。', {key: 'capture-continue'});
      return;
    }
    render();
    try {
      const result = await api(`/intents/${encodeURIComponent(identity)}/continue`, {method: 'POST', body: '{}'});
      continued = identity;
      element('run-note').textContent = `${result.detail} · 批次 ${result.run}`;
      element('run-note').classList.remove('error');
    } catch (error) {
      await refresh();
      element('run-note').textContent = `${error.message}。请核对当前条目后再操作。`;
      element('run-note').classList.add('error');
    } finally { processing = false; render(); }
  });

  element('service-url').textContent = location.origin;
  if (!itemKey) message('请从订阅文章上的「从浏览器补抓」进入；不能把未绑定的正文写进任意条目。', true);
  else refresh();
})();
