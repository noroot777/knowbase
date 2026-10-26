(() => {
  'use strict';
  const element = id => document.getElementById(id);
  const itemKey = new URLSearchParams(location.search).get('item');
  const storageKey = `knowbase-capture:${itemKey}`;
  let intent = null;
  let timer = null;
  let processing = false;

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

  function remember(identity) {
    try {
      if (identity) sessionStorage.setItem(storageKey, identity);
      else sessionStorage.removeItem(storageKey);
    } catch {}
  }

  function display(state) {
    intent = state;
    const pending = state.status === 'pending';
    const saved = state.status === 'saved';
    if (!pending) clearTimeout(timer);
    element('create').disabled = pending || saved;
    element('create').textContent = saved ? '正文已补入' : pending ? '等待浏览器采集' : '发起浏览器采集';
    element('page-url').disabled = pending || saved;
    element('cancel').hidden = !pending;
    element('saved').hidden = !saved;
    element('step-capture').classList.toggle('on', pending || saved);
    element('step-process').classList.toggle('on', saved);
    element('intent-note').textContent = saved ? `回执 ${state.id.slice(-8)} · 已持久保存`
      : `请求 ${state.id.slice(-8)} · ${new Date(state.expires_at * 1000).toLocaleTimeString('zh-CN', {timeZone: 'Asia/Shanghai'})} 到期`;
    if (saved) {
      message('正文已补回原条目。翻译与入库尚待执行。');
      element('receipt').textContent = `本次补入 ${state.receipt.characters} 字符 · ${new Date(state.receipt.captured_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})}`;
      clearTimeout(timer);
    } else if (pending) message('等待你在文章标签中主动采集；本页没有读取任何浏览器标签。');
    else {
      message(state.status === 'stale' ? '条目已变化，未接受旧请求。请重新发起采集。' : '请求已取消，可以重新指定文章。', true);
      remember('');
    }
  }

  async function poll() {
    if (!intent || intent.status !== 'pending') return;
    const identity = intent.id;
    try {
      const state = await api(`/intents/${encodeURIComponent(identity)}`);
      if (intent.id !== identity || intent.status !== 'pending') return;
      display(state);
    } catch (error) {
      if (intent.id !== identity || intent.status !== 'pending') return;
      message(error.message, true);
      if (error.status === 404 || Date.now() >= intent.expires_at * 1000) {
        display({...intent, status: 'expired'});
        message('请求已到期或服务已重启，请重新发起采集。', true);
      }
    }
    if (intent.status === 'pending') timer = setTimeout(poll, 2000);
  }

  element('create').addEventListener('click', async () => {
    if (processing) return;
    processing = true;
    element('create').disabled = true;
    try {
      const state = await api('/intents', {method: 'POST', body: JSON.stringify({key: itemKey, page_url: element('page-url').value.trim()})});
      display(state);
      remember(state.id);
      timer = setTimeout(poll, 2000);
    } catch (error) { message(error.message, true); element('create').disabled = false; }
    finally { processing = false; }
  });

  element('cancel').addEventListener('click', async () => {
    if (processing || !intent) return;
    processing = true;
    try { display(await api(`/intents/${encodeURIComponent(intent.id)}`, {method: 'DELETE'})); }
    catch (error) {
      if (error.status === 404) display({...intent, status: 'expired'});
      message(error.message, true);
    }
    finally { processing = false; }
  });

  element('restart').addEventListener('click', async () => {
    if (processing || !intent) return;
    processing = true;
    const confirmed = await confirmBox({title: '重新采集正文？', body: '重新采集不会撤销已保存的正文；只有再次预览确认后才会替换。', ok: '重新采集'});
    processing = false;
    if (!confirmed) return;
    clearTimeout(timer);
    display({...intent, status: 'cancelled'});
    element('run-note').textContent = '';
    element('continue').disabled = false;
    message('已保存的正文保持不变。请核对网址，再发起新的采集请求。');
  });

  element('pair').addEventListener('click', async () => {
    try {
      const pairing = await api('/pairing');
      element('pairing-key').value = pairing.key;
      element('pairing').hidden = false;
    } catch (error) { message(error.message, true); }
  });

  element('copy-key').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(element('pairing-key').value); message('密钥已复制，只粘贴到你安装的 Knowbase 扩展中。'); }
    catch { element('pairing-key').type = 'text'; element('pairing-key').select(); message('无法自动复制，请复制选中的配对密钥。'); }
  });

  element('continue').addEventListener('click', async () => {
    if (processing || !intent) return;
    processing = true;
    const confirmed = await confirmBox({title: '继续该来源后续处理？', body: '继续会处理该来源其他待办，可能补抓其他文章、调用翻译及提取模型，索引会检查全库。', ok: '开始处理'});
    if (!confirmed) { processing = false; return; }
    element('continue').disabled = true;
    try {
      const result = await api(`/intents/${encodeURIComponent(intent.id)}/continue`, {method: 'POST', body: '{}'});
      element('run-note').textContent = `${result.detail} · 批次 ${result.run}`;
      element('run-note').classList.remove('error');
    } catch (error) {
      element('run-note').textContent = `${error.message}。正文已保留，可稍后重试。`;
      element('run-note').classList.add('error');
      element('continue').disabled = false;
    } finally { processing = false; }
  });

  async function initialize() {
    element('service-url').textContent = location.origin;
    if (!itemKey) return message('请从订阅文章上的「从浏览器补抓」进入；不能把未绑定的正文写进任意条目。', true);
    try {
      const target = await api(`/target/${encodeURIComponent(itemKey)}`);
      element('title').textContent = target.title;
      element('original-url').textContent = target.url;
      element('kind').textContent = target.kind;
      element('page-url').value = target.url;
      element('page-url').disabled = false;
      element('create').disabled = false;
      message('核对文章网址，然后发起一次采集请求。');
      let previous = '';
      try { previous = sessionStorage.getItem(storageKey); } catch {}
      if (previous) {
        try {
          const state = await api(`/intents/${encodeURIComponent(previous)}`);
          if (state.key !== itemKey) throw new Error('请求与条目不匹配');
          element('page-url').value = state.page_url;
          display(state);
          if (state.status === 'pending') timer = setTimeout(poll, 2000);
        } catch { remember(''); message('先前请求已过期或服务已重启，请重新发起。', true); }
      }
    } catch (error) { message(error.message, true); }
  }
  initialize();
})();
