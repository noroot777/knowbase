(() => {
  'use strict';
  const element = id => document.getElementById(id);
  let server = '';
  let key = '';
  let intents = [];
  let preview = null;
  let busy = false;
  let here = null;
  //: 预览区此刻服务于哪一件事:补回某个待办(false)还是收录一篇新文章(true)。
  //: 两件事共用这一块预览和那道「我已核对」—— 人要核对的东西一模一样。
  let collecting = false;

  function status(message, error = false) {
    element('status').textContent = message;
    element('status').classList.toggle('error', error);
  }

  function setBusy(value) {
    busy = value;
    const available = !!element('intent').value;
    for (const name of ['read', 'selection']) element(name).disabled = value || !available;
    for (const name of ['collect', 'collect-page', 'collect-selection'])
      element(name).disabled = value || !here;
    for (const name of ['connect', 'refresh', 'intent']) element(name).disabled = value || (name === 'intent' && !intents.length);
    element('save').disabled = value || !preview || !element('complete').checked;
  }

  function clearPreview() {
    preview = null;
    collecting = false;
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

  async function call(path, options = {}) {
    if (!server || !key) throw new Error('请先从知识库补抓页复制配对密钥并连接');
    const response = await fetch(`${server}${path}`, {
      credentials: 'omit', redirect: 'error', cache: 'no-store',
      headers: {'Content-Type': 'application/json', Authorization: `Bearer ${key}`}, ...options
    });
    return apiResponse(response, '浏览器采集请求失败');
  }
  // 两族端点、同一条连接和同一份配对密钥。`/api/grab` 那几个服务端不验密钥
  // (它只收一条地址,内容由服务端自己去抓),`/from-page` 验 —— 一律带上,
  // 省得在这里按端点记一张「谁要密钥」的表。
  const api = (path, options) => call('/api/browser-capture' + path, options);
  const grabApi = (path, options) => call('/api/grab' + path, options);

  // 只为了好读:百分号编码过的中文地址在这里显示成人能对着地址栏核对的样子。
  // **判据不在这里** —— 网址是不是同一页一律由服务端 /precheck 判(见
  // docs/decisions/browser-capture.md「准入判据只有一份」)。
  function plainUrl(value) {
    try { return decodeURI(value); } catch { return value; }
  }

  async function currentTab() {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!tab?.id || !/^https?:\/\//.test(tab.url || '')) {
      throw new Error('请在要采集的文章标签上打开扩展，而不是扩展管理页或知识库页面');
    }
    return tab;
  }

  // 这一页在知识库里吗 —— 人点开扩展最先想知道的事。**判据在服务端**
  // (`/api/grab/state` 只读,一个字节都不写),这里只展示结论。
  async function loadHere() {
    element('here-actions').hidden = true;
    element('collect-page').hidden = true;
    element('collect-selection').hidden = true;
    element('here-title').hidden = true;
    try {
      here = await currentTab();
    } catch (error) {
      here = null;
      element('here-state').textContent = '这一页不能采集';
      element('here-url').textContent = error.message;
      return;
    }
    element('here-url').textContent = plainUrl(here.url);
    const state = await grabApi(`/state?url=${encodeURIComponent(here.url)}`);
    if (state.known) {
      // 「还没写完」时服务端会连着给出人该去哪一页点哪一颗(`resume`)。
      // 自己另编一句就等于把那份口径抄进扩展,它改了这里不会跟着改。
      element('here-state').textContent = state.done
        ? '这一页已在知识库里。' : `这一页已收下，还没写完${state.resume || ''}。`;
      element('here-title').textContent = state.title || '';
      element('here-title').hidden = !state.title;
      return;
    }
    element('here-state').textContent = '这一页还不在知识库里。';
    element('here-actions').hidden = false;
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

  // 在那个标签上读一次正文。**补正文和收录共用**:同一个提取器、同一句错误话术。
  async function extractFrom(tab, mode) {
    await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['extract.js']});
    const results = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: method => {
      try { return readKnowbaseArticle(method); }
      catch (error) { return {error: error.message}; }
    }, args: [mode]});
    const captured = results[0]?.result;
    if (captured?.error) throw new Error(captured.error);
    if (!captured?.body) throw new Error('页面没有返回正文，未上传任何内容');
    return captured;
  }

  async function read(mode) {
    if (busy) return;
    clearPreview();
    setBusy(true);
    try {
      const intent = intents.find(item => item.id === element('intent').value);
      if (!intent) throw new Error('请先在上面选中本次要补回的待办');
      const tab = await currentTab();
      // 读之前先问服务端:这个请求还在不在、这一页是不是它指定的那一篇、条目还是
      // 绑定的那一版吗。不通过就一个字都不读 —— 否则人读完整页才被拒(2026-10-26
      // 审查实测的三种:网址带中文、复读提示页、读的那几分钟里条目被同步改过)。
      await api('/precheck', {method: 'POST', body: JSON.stringify({intent_id: intent.id, page_url: tab.url, method: mode})});
      const captured = await extractFrom(tab, mode);
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

  // 收录这一页:先让服务端自己去抓(它走完整的八道准入,还能取到标题、作者、
  // 日期)。抓不到才轮到下面那一颗 —— **不在这里判是为什么抓不到**:无论登录墙、
  // 关卡还是脚本渲染,人的下一步都一样,而他正看着这一页。
  element('collect').addEventListener('click', async () => {
    if (busy || !here) return;
    setBusy(true);
    try {
      const result = await grabApi('', {method: 'POST', body: JSON.stringify({url: here.url})});
      // 同下面提交那一处:收下之后刷新失败不改变已经收下这件事,更不该把
      // 下面那两颗兜底按钮露出来 —— 那会让人把已入库的这一页再读一遍正文。
      try { await loadHere(); } catch { /* 照旧报「已收下」 */ }
      status(result.detail || '已收下');
    } catch (error) {
      status(error.message, true);
      // 两颗一起露出,因为**服务端抓不到**和**它自动识别不出正文**是两回事:
      // 前者给「改用这一页的正文」就够了,后者(整页是脚本渲染的壳、正文在
      // iframe 里、页面没有能认出的文章容器)只有人自己选中那一段才取得到。
      // 只给前一颗的话,`extract.js` 回的「请选好全文再读一次」就指向一颗
      // 不存在的按钮 —— 人照着做完,无处可点。
      element('collect-page').hidden = false;
      element('collect-selection').hidden = false;
    }
    finally { setBusy(false); }
  });

  element('collect-page').addEventListener('click', () => collect('article'));
  element('collect-selection').addEventListener('click', () => collect('selection'));

  // 用这一页的正文收录。和补正文那条路同一套:先问服务端(不通过就一个字都不读
  // 第二遍)、预览上的字数用服务端那一份、人勾完确认才提交。
  async function collect(mode) {
    if (busy || !here) return;
    clearPreview();
    setBusy(true);
    try {
      const captured = await extractFrom(here, mode);
      const payload = {url: captured.page_url, title: captured.title, body: captured.body};
      const verified = await grabApi('/from-page/precheck', {method: 'POST', body: JSON.stringify(payload)});
      collecting = true;
      preview = payload;
      element('page-title').textContent = verified.title;
      element('page-url').textContent = plainUrl(captured.page_url);
      // 不写「尚未上传」:这一刻正文**已经**传给本机服务核对过了(上面那次预检)。
      // 待定的是「收不收进知识库」,说成没上传会让人以为取消就什么都没发生。
      element('count').textContent = `${verified.characters} 字符 · ${mode === 'selection' ? '你所选的文字' : '自动识别的文章区域'} · 尚未收录`;
      element('body').value = captured.body;
      element('preview').hidden = false;
      status('全文已传给本机服务核对，先看一遍。只有勾选确认并点击使用，才会收录进知识库。');
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
      await refreshAll();
      await chrome.storage.local.set({server, key});
      element('connection').open = false;
    } catch (error) { status(error.message, true); }
    finally { setBusy(false); }
  });
  element('refresh').addEventListener('click', async () => {
    if (busy) return;
    setBusy(true);
    try { await refreshAll(); } catch (error) { status(error.message, true); }
    finally { setBusy(false); }
  });

  // 「这一页」那一问失败不该挡住待办那一块:两件事互不依赖,一边连不上时
  // 另一边照旧可用(反过来也一样)。
  async function refreshAll() {
    // 两边各自接住,顺序不构成依赖:`loadIntents` 抛出时(密钥不对、服务没起)
    // 「这一页」照旧要问 —— 写成 `await loadIntents()` 打头就等于让它一失败
    // 这一整块永远停在「正在确认这一页…」,而注释还写着互不依赖。
    // `loadIntents` 那个错要原样抛出去:connect 那颗按钮靠它判密钥对不对。
    let failure = null;
    try { await loadIntents(); } catch (error) { failure = error; }
    try { await loadHere(); } catch (error) {
      element('here-state').textContent = '没能确认这一页：' + error.message;
    }
    if (failure) throw failure;
  }
  element('save').addEventListener('click', async () => {
    if (busy || !preview || !element('complete').checked) return;
    setBusy(true);
    try {
      if (collecting) {
        const result = await grabApi('/from-page', {method: 'POST', body: JSON.stringify(preview)});
        clearPreview();
        // 已经收下了。这一问失败只是「状态没刷新」—— 落进下面那个 catch 会
        // 告诉人「可再次提交同一预览」,而预览刚被 clearPreview 清掉,他照着
        // 做不到;更要紧的是那句话会让他以为**没收下**。
        try { await loadHere(); } catch { /* 下面照旧报「已收下」 */ }
        status(result.detail || '已收下');
        return;
      }
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
      await refreshAll();
    } catch (error) { element('connection').open = true; status(error.message, true); }
    finally { setBusy(false); }
  }
  initialize();
})();
