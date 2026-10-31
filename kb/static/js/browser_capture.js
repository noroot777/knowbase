/** 从浏览器采集 · 抽屉里那套状态机(外壳和 DOM 在 `capture_drawer.js`)。
 *
 *  原先这是独立页面 `/browser-capture?item=<key>` 的脚本,条目 key 从地址栏取、
 *  脚本一加载就自己跑起来。搬进抽屉之后这两件事都得改成由外壳驱动:key 是
 *  `startCapture(key)` 的参数,而**关抽屉必须能把轮询停住** —— 独立页关掉标签
 *  就什么都没了,抽屉底下那一页还活着。
 */
(() => {
  'use strict';
  const element = id => document.getElementById(id);
  let itemKey = null;
  let snapshot = null;
  let timer = null;
  let processing = false;
  let drafting = false;
  let continued = '';
  let loadVersion = 0;
  //: **每次打开抽屉是新的一代。** `loadVersion` 只作废在途的那一次 `refresh`,
  //: 作废不了「发起 / 取消 / 改址 / 继续处理」这些**写动作**的后半段:它们
  //: `await` 完回来还要读 `itemKey`、写 `actionError`、调 `render()`、再 `refresh()`
  //: 排下一轮轮询 —— 而那时人可能已经关掉抽屉、甚至打开了另一篇。
  //: 实测两种形态:①发起后立刻关掉,4 秒后冒出 `GET /target/null`,还把本该停掉
  //: 的轮询重新排了起来;②A 的网址填错、发起后立刻关掉并打开 B,A 那个迟到的 409
  //: 把 B 顶上改成红字「请使用完整文章网址」,而 B 的网址是对的。
  let generation = 0;
  const current = gen => gen === generation;
  let pendingSignature = '';
  let loadError = '';
  let actionError = '';
  let urlAuto = null;

  async function api(path, options = {}) {
    const response = await fetch(`/api/browser-capture${path}`, {
      cache: 'no-store', headers: {'Content-Type': 'application/json'}, ...options
    });
    return apiResponse(response, '补抓请求失败');
  }

  function message(text, error = false) {
    element('cap-message').textContent = text;
    element('cap-message').classList.toggle('error', error);
  }

  function dateTime(timestamp) {
    return new Date(timestamp * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'});
  }

  // 只为了好读:百分号编码过的中文地址显示成人能对着地址栏核对的样子。回填给服务端
  // 时它会被重新规范化成同一份,所以这只是显示 —— **判据不在前端**(网址是不是同一
  // 页由 kb/browser_capture.page_url 一处判,见 docs/decisions/browser-capture.md)。
  function plainUrl(value) {
    try { return decodeURI(value); } catch { return value; }
  }

  // 自动填入只认「框里还是上一次自动填进去的那一份」。`force` 用于已有请求:那时
  // 框是禁用的,必须显示服务端记下的那一份。
  //
  // 事故形态(实测):本块每 2 秒重画一次,原来的判据是「框是空的就当作还没填过」
  // —— 人清空、切出去复制浏览器里真正的地址、切回来一粘贴,2.6 秒前就被填回的原
  // 网址还在,贴的那一截接在它后面,拼成一个谁也打不开的地址,之后扩展只会一直说
  // 「当前标签网址与指定文章不同」。草稿态(点过「重新采集正文」、框解禁)是同一个
  // 形态的第二处,所以这道闸不能只挡首次渲染。
  function autofillUrl(value, force = false) {
    const field = element('cap-page-url');
    if (!force && urlAuto !== null && field.value !== urlAuto) return;
    if (field.value !== value) field.value = value;
    urlAuto = value;
  }

  function renderPending(waiting) {
    const signature = JSON.stringify(waiting);
    if (signature !== pendingSignature) {
      pendingSignature = signature;
      element('cap-pending-list').replaceChildren();
      for (const request of waiting) {
        const row = document.createElement('li');
        const note = document.createElement('p');
        note.className = 'hint';
        note.textContent = `${dateTime(request.created_at)} 发起 · ${dateTime(request.expires_at)} 到期 · ${request.id.slice(-8)}`;
        const url = document.createElement('p');
        url.className = 'url';
        url.textContent = plainUrl(request.page_url);
        const cancel = document.createElement('button');
        cancel.className = 'secondary';
        cancel.type = 'button';
        cancel.dataset.cancelIntent = request.id;
        cancel.textContent = '取消此请求';
        row.append(note, url, cancel);
        // 浏览器停在了另一个地址(站点按语言跳转是最常见的那一种):服务端在拒绝
        // 那一刻就记下了真实的最终网址,这里把它摆出来并给一颗改用它的按钮。
        // 上面那一栏在有待办时是禁用的,所以**出路只能开在这里** —— 否则扩展那
        // 句「可一键改用它重发」指向的操作在界面上做不到。改不改仍然是人点的。
        if (request.seen_url && request.seen_url !== request.page_url) {
          const block = document.createElement('div');
          block.className = 'mismatch';
          const lead = document.createElement('p');
          lead.className = 'hint';
          lead.textContent = '浏览器停在另一个地址，本次没有读取正文。';
          const seen = document.createElement('p');
          seen.className = 'url';
          seen.textContent = plainUrl(request.seen_url);
          const use = document.createElement('button');
          use.type = 'button';
          use.dataset.useUrl = request.seen_url;
          use.textContent = '改用这个地址';
          block.append(lead, seen, use);
          row.append(block);
        }
        element('cap-pending-list').append(row);
      }
    }
    element('cap-pending').hidden = !waiting.length;
    element('cap-pending-list').querySelectorAll('button').forEach(button => { button.disabled = processing; });
  }

  function render() {
    if (!snapshot) return;
    if (loadError) {
      for (const name of ['cap-create', 'cap-continue', 'cap-restart', 'cap-page-url', 'cap-draft-cancel'])
        element(name).disabled = true;
      element('cap-pending-list').querySelectorAll('button').forEach(button => { button.disabled = true; });
      element('cap-saved').hidden = true;
      message(loadError, true);
      return;
    }
    const saved = snapshot.saved;
    const pending = snapshot.pending[0];
    const locked = !!pending || (!!saved && !drafting);
    element('cap-title').textContent = snapshot.title;
    element('cap-original-url').textContent = snapshot.url;
    element('cap-kind').textContent = snapshot.kind;
    if (pending) autofillUrl(plainUrl(pending.page_url), true);
    else if (!element('cap-page-url').value) autofillUrl(snapshot.url);
    element('cap-page-url').disabled = processing || locked;
    element('cap-create').disabled = processing || locked;
    element('cap-create').textContent = pending ? '等待浏览器采集' : saved && !drafting ? '正文已补入' : '发起浏览器采集';
    element('cap-draft-cancel').hidden = !drafting || !saved || !!pending;
    element('cap-draft-cancel').disabled = processing;
    element('cap-saved').hidden = !saved;
    element('cap-continue').disabled = processing || !!pending || !saved || continued === saved.id;
    element('cap-restart').hidden = drafting || !!pending;
    element('cap-restart').disabled = processing;
    element('cap-intent-note').textContent = pending ? `请求 ${pending.id.slice(-8)} · ${dateTime(pending.expires_at)} 到期`
      : saved ? `回执 ${saved.id.slice(-8)} · 已持久保存` : '';
    renderPending(snapshot.pending);
    if (saved) element('cap-receipt').textContent = `本次补入 ${saved.receipt.characters} 字符 · ${new Date(saved.receipt.captured_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})}`;
    if (pending) message('等你在那个文章标签上点扩展采集；同一篇的其他标签共用这个请求。');
    else if (drafting && saved) message('已保存的正文和回执仍然保留，核对网址再发起。');
    else if (saved) message('正文已补回原条目。翻译与入库尚待执行。');
    else if (snapshot.saved_error) message(snapshot.saved_error, true);
    // 「然后点发起」不写:那颗按钮就在这一栏上。留「核对」是因为这一栏是**预填**
    // 的(原网址),而站点常按语言跳转 —— 对不上才有下面那一块 `.mismatch`。
    else message('核对文章网址。');
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
        element('cap-run-note').textContent = '';
        actionError = '';
      }
      loadError = '';
      snapshot = state;
      render();
    } catch (error) {
      if (version !== loadVersion) return;
      loadError = error.message;
      element('cap-create').disabled = true;
      element('cap-continue').disabled = true;
      element('cap-restart').disabled = true;
      element('cap-saved').hidden = true;
      message(error.message, true);
    } finally {
      if (version === loadVersion) timer = setTimeout(refresh, 2000);
    }
  }

  //: **每次打开都要重绑。** 抽屉是建了就 append、关了就 remove 的(见
  //: `capture_drawer.js` 里为什么),所以这一轮拿到的是一套全新的元素 —— 原来那个
  //: 「只绑一次」的标志会让第二次打开起每一颗按钮都没有监听器:界面完整、点下去
  //: 什么都不发生。元素是新的,所以也不存在重复绑定。
  function bind() {
    element('cap-create').addEventListener('click', async () => {
      if (processing) return;
      const target = element('cap-page-url').value.trim();
      // 勾了「同时打开」就**先开,再发起**。`window.open` 一旦离开这次点击的同步
      // 调用栈就不再算用户手势,会被弹窗拦截挡掉 —— `diag_actions.js` 那颗入口当初
      // 不弹确认框,为的正是这件事。发起失败时多开一个标签不要紧:勾这个框的意思
      // 本来就是「我这就要去那一页」,而扩展也得在那一页上才点得动。
      if (element('cap-also-open').checked && target) window.open(target, '_blank', 'noopener');
      const gen = generation;
      processing = true;
      actionError = '';
      render();
      try {
        await api('/intents', {method: 'POST', body: JSON.stringify({key: itemKey, page_url: target})});
        if (current(gen)) await refresh();
      } catch (error) { if (current(gen)) actionError = error.message; }
      finally { if (current(gen)) { processing = false; render(); } }
    });

    element('cap-pending-list').addEventListener('click', async event => {
      const cancel = event.target.closest('[data-cancel-intent]');
      const reuse = event.target.closest('[data-use-url]');
      if (processing || (!cancel && !reuse)) return;
      const gen = generation;
      processing = true;
      actionError = '';
      render();
      try {
        // 改用另一个地址走的是同一个 `create`:服务端自己把这篇文章旧的那个待办
        // 顶掉,所以不必先取消再发起(中间那一下谁都接不住)。
        if (reuse) await api('/intents', {method: 'POST', body: JSON.stringify({key: itemKey, page_url: reuse.dataset.useUrl})});
        else await api(`/intents/${encodeURIComponent(cancel.dataset.cancelIntent)}`, {method: 'DELETE'});
        if (current(gen)) await refresh();
      } catch (error) { if (current(gen)) actionError = error.message; }
      finally { if (current(gen)) { processing = false; render(); } }
    });

    element('cap-restart').addEventListener('click', async () => {
      if (processing || !snapshot?.saved) return;
      const gen = generation;
      processing = true;
      const confirmed = await confirmBox({title: '重新采集正文？', body: '已保存的正文和回执不会被撤销，只有再采一次并通过服务端核验才会替换。', ok: '重新采集'});
      // 确认框开着的时候抽屉可能已经被关掉、或者换了一条:那一下的「是」不属于
      // 现在屏幕上这一篇。
      if (!current(gen)) return;
      processing = false;
      if (confirmed) drafting = true;
      render();
    });

    element('cap-draft-cancel').addEventListener('click', () => { drafting = false; render(); });

    element('cap-continue').addEventListener('click', async () => {
      if (processing || !snapshot?.saved) return;
      const identity = snapshot.saved.id;
      const gen = generation;
      processing = true;
      // 话必须跟 `browser_capture_api.CONTINUE_STAGES` 对得上:`extract` 不在那份清单里,
      // 所以这里不许再承诺会买一轮条目化提取 —— 上面那两行警告同理。
      const confirmed = await confirmBox({title: '继续导出与索引？', body: '跑的是导出与索引两段：同类来源其他待办会一起导出，可能补抓别的文章并调用翻译模型，索引会检查全库。条目化提取不在这一步。', ok: '开始处理'});
      if (!current(gen)) return;
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
        if (!current(gen)) return;
        continued = identity;
        element('cap-run-note').textContent = `${result.detail} · 批次 ${result.run}`;
        element('cap-run-note').classList.remove('error');
      } catch (error) {
        if (!current(gen)) return;
        await refresh();
        if (!current(gen)) return;
        element('cap-run-note').textContent = `${error.message}。请核对当前条目后再操作。`;
        element('cap-run-note').classList.add('error');
      } finally { if (current(gen)) { processing = false; render(); } }
    });
  }

  //: 外壳调这两个。**每次打开都从干净状态起**:同一个抽屉会被不同条目反复用,
  //: 上一条的 snapshot、草稿态、自动填入基准留着就会在新条目上显示旧数据 ——
  //: 而那一栏网址一旦带着上一篇的值,发起的就是指着另一篇的待办。
  function startCapture(key) {
    generation += 1;
    loadVersion += 1;
    itemKey = key;
    snapshot = null;
    processing = false;
    drafting = false;
    continued = '';
    pendingSignature = '';
    loadError = '';
    actionError = '';
    urlAuto = null;
    element('cap-page-url').value = '';
    element('cap-also-open').checked = false;
    element('cap-run-note').textContent = '';
    element('cap-saved').hidden = true;
    element('cap-pending').hidden = true;
    bind();
    if (!key) { message('请从订阅文章或待办上的「从浏览器采集」进入。', true); return; }
    message('正在读取目标条目…');
    refresh();
  }

  //: 关抽屉。`++loadVersion` 让在途的那次 `refresh` 回来时自己作废 —— 只清
  //: `timer` 不够:请求已经发出去的那一次会在 `finally` 里再排下一轮。
  //: `++generation` 管的是另一半:写动作的后半段(见上面那段)。
  function stopCapture() {
    generation += 1;
    clearTimeout(timer);
    timer = null;
    loadVersion += 1;
    itemKey = null;
  }

  window.startCapture = startCapture;
  window.stopCapture = stopCapture;
})();
