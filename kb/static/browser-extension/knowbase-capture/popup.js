(() => {
  'use strict';
  const element = id => document.getElementById(id);
  let server = '';
  let key = '';
  let intents = [];
  let preview = null;
  let busy = false;

  function status(message, error = false) {
    element('status').textContent = message;
    element('status').classList.toggle('error', error);
  }

  function setBusy(value) {
    busy = value;
    const available = !!element('intent').value;
    for (const name of ['read', 'selection']) element(name).disabled = value || !available;
    for (const name of ['connect', 'refresh', 'intent']) element(name).disabled = value || (name === 'intent' && !intents.length);
    element('save').disabled = value || !preview || !element('complete').checked;
  }

  function clearPreview() {
    preview = null;
    element('preview').hidden = true;
    element('body').value = '';
    element('complete').checked = false;
    element('save').disabled = true;
  }

  function localServer(value) {
    const url = new URL(value);
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname)
        || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
      throw new Error('只允许 http://127.0.0.1:端口 或 http://localhost:端口 的本机服务');
    }
    return url.origin;
  }

  async function api(path, options = {}) {
    if (!server || !key) throw new Error('请先从知识库补抓页复制配对密钥并连接');
    const response = await fetch(`${server}/api/browser-capture${path}`, {
      credentials: 'omit', redirect: 'error', cache: 'no-store',
      headers: {'Content-Type': 'application/json', Authorization: `Bearer ${key}`}, ...options
    });
    return apiResponse(response, '浏览器采集请求失败');
  }

  // 只为了好读:百分号编码过的中文地址在这里显示成人能对着地址栏核对的样子。
  // **判据不在这里** —— 网址是不是同一页一律由服务端 /precheck 判(见
  // docs/decisions/browser-capture.md「准入判据只有一份」)。
  function plainUrl(value) {
    try { return decodeURI(value); } catch { return value; }
  }

  async function loadIntents() {
    clearPreview();
    const result = await api('/pending');
    intents = result.intents;
    element('intent').replaceChildren();
    if (!intents.length) {
      const empty = document.createElement('option');
      empty.value = '';
      empty.textContent = '请先在知识库发起采集请求';
      element('intent').append(empty);
    }
    for (const intent of intents) {
      const option = document.createElement('option');
      option.value = intent.id;
      const created = new Date(intent.created_at * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'});
      option.textContent = `${intent.title} · ${created} 发起 · ${intent.id.slice(-8)}`;
      element('intent').append(option);
    }
    updateExpected();
    status(intents.length ? '已连接。先核对目标条目，再主动读取当前文章。' : '没有待采集请求。请先在知识库文章上点击「从浏览器补抓」并发起请求。');
  }

  function updateExpected() {
    clearPreview();
    const intent = intents.find(item => item.id === element('intent').value);
    element('expected').textContent = intent ? `指定文章：${plainUrl(intent.page_url)}` : '';
    setBusy(busy);
  }

  async function read(mode) {
    if (busy) return;
    clearPreview();
    setBusy(true);
    try {
      const intent = intents.find(item => item.id === element('intent').value);
      if (!intent) throw new Error('请先在上面选中本次要补回的待办');
      const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
      if (!tab?.id || !/^https?:\/\//.test(tab.url || '')) throw new Error('请在要采集的文章标签上打开扩展，而不是扩展管理页或知识库页面');
      // 读之前先问服务端:这个请求还在不在、这一页是不是它指定的那一篇、条目还是
      // 绑定的那一版吗。不通过就一个字都不读 —— 否则人读完整页才被拒(2026-10-26
      // 审查实测的三种:网址带中文、复读提示页、读的那几分钟里条目被同步改过)。
      await api('/precheck', {method: 'POST', body: JSON.stringify({intent_id: intent.id, page_url: tab.url, method: mode})});
      await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['extract.js']});
      const results = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: method => {
        try { return readKnowbaseArticle(method); }
        catch (error) { return {error: error.message}; }
      }, args: [mode]});
      const captured = results[0]?.result;
      if (captured?.error) throw new Error(captured.error);
      if (!captured?.body) throw new Error('页面没有返回正文，未上传任何内容');
      // 再问一次,这次带上正文:读的过程中跳转了吗、这份正文过得了入库那道门槛吗。
      // 预览之后人还要逐段核对,所以判据必须在渲染预览之前就拿到,不能留到提交。
      const verified = await api('/precheck', {method: 'POST', body: JSON.stringify(
        {intent_id: intent.id, page_url: captured.page_url, body: captured.body, method: mode})});
      preview = {...captured, intent_id: intent.id};
      element('page-title').textContent = captured.title;
      element('page-url').textContent = plainUrl(captured.page_url);
      element('count').textContent = `${verified.characters} 字符 · ${mode === 'selection' ? '你所选的文字，请确认不是选段' : '自动识别的文章区域'} · 尚未上传`;
      element('body').value = captured.body;
      element('preview').hidden = false;
      status('先核对全文。只有勾选确认并点击使用，才会向本机知识库提交正文。');
    } catch (error) { status(error.message, true); }
    finally { setBusy(false); }
  }

  element('read').addEventListener('click', () => read('article'));
  element('selection').addEventListener('click', () => read('selection'));
  element('intent').addEventListener('change', updateExpected);
  element('complete').addEventListener('change', () => setBusy(busy));
  element('connect').addEventListener('click', async () => {
    if (busy) return;
    setBusy(true);
    try {
      server = localServer(element('server').value.trim());
      key = element('key').value.trim();
      if (!/^[A-Za-z0-9_-]{40,80}$/.test(key)) throw new Error('请完整复制本机知识库显示的配对密钥');
      await loadIntents();
      await chrome.storage.local.set({server, key});
      element('connection').open = false;
    } catch (error) { status(error.message, true); }
    finally { setBusy(false); }
  });
  element('refresh').addEventListener('click', async () => {
    if (busy) return;
    setBusy(true);
    try { await loadIntents(); } catch (error) { status(error.message, true); }
    finally { setBusy(false); }
  });
  element('save').addEventListener('click', async () => {
    if (busy || !preview || !element('complete').checked) return;
    setBusy(true);
    try {
      const result = await api('/upload', {method: 'POST', body: JSON.stringify({...preview, confirmed_complete: true})});
      clearPreview();
      try { await loadIntents(); } catch { status('正文已保存；待办刷新失败，可稍后重新打开扩展。'); return; }
      status(result.detail);
    } catch (error) {
      const retry = !error.status || error.status >= 500
        ? '。若连接中断，可再次提交同一预览；服务端不会重复替换。' : '';
      status(error.message + retry, true);
    }
    finally { setBusy(false); }
  });

  async function initialize() {
    setBusy(true);
    try {
      const stored = await chrome.storage.local.get(['server', 'key']);
      element('server').value = stored.server || 'http://127.0.0.1:8787';
      element('key').value = stored.key || '';
      server = localServer(element('server').value);
      key = stored.key || '';
      if (!key) { element('connection').open = true; status('首次使用：连接本机服务并填写配对密钥。'); return; }
      await loadIntents();
    } catch (error) { element('connection').open = true; status(error.message, true); }
    finally { setBusy(false); }
  }
  initialize();
})();
