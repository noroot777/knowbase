// ── 手动收录:粘一条链接,内容抓进知识库 ──────────────────────────────────────
// 后端口径在 kb/feeds/grab.py(硬约束 docs/decisions/link-grab.md)。这一层只管
// 三件事:禁掉重复提交、把慢回包的归属对一次、把结果说成人话。
//
// 这颗按钮是**慢请求**(抓一篇网页正文、长文还要再问一次接口,上限 40 秒)。
// 所以两条规矩:
//   ① 在途期间按钮禁用、输入框里的地址不清 —— 失败时用户不用重新粘一次。
//   ② 回包落地前先对一次归属(发起那一刻的 sourceKind + sourceVersion)。
//      切到别的来源、或者这块面板重读过事实之后,那份回包说的是**过去那个面板**
//      的事:toast 照发(那是用户自己的动作,他该知道结果),但**一个字都不往
//      面板上写**,也不去刷别人的列表(口径同 frontend-feedback.md 的代次那条)。

let grabBusy = false;
// 从侧栏那个「存链接」入口进来时才抢焦点:单纯切到「我的订阅」不该把光标
// 从页面上别的地方拽走(一次性标记,面板装好就用掉)。
let grabWantsFocus = false;

/** 侧栏「存链接」:落到「我的订阅 · 手动收录」上,并把光标放进输入框。 */
function grabFromNav() {
  grabWantsFocus = true;
  // **不走 `switchTab('inbox')` 那条深链。** 那一步要靠 `subsCatalog` 认出这个
  // id(core.js 开头那一句),而目录是异步装的 —— 页面刚打开就点这颗按钮时它还
  // 是空的,于是 'inbox' 被当成未知 tab 直接 `return`,点下去什么也不发生
  // (2026-10-16 界面探针实测:等满 15 秒都没切过去,而手动慢慢点总是好的 ——
  // 正是那种只在"刷新后立刻点"时出现的形态)。
  // 这里直接写意图:下一次进「我的订阅」要打开哪一类。`openSubsPanel` 自己会
  // 补读目录,所以不依赖它此刻装没装完。
  subsPendingKind = 'inbox';
  switchTab('subs');
}

/** 这一类来源要不要显示投递栏:判据是 spec.pollable===false(手动收录)。 */
function applyGrabBar(spec) {
  const manual = spec && spec.pollable === false;
  $('grab-bar').hidden = !manual;
  // 「添加订阅」在这一类里没有意义:收件桶是按站点自动建的,不是人去填的。
  $('source-add').hidden = !!manual;
  if (manual) {
    $('grab-hint').textContent = 'X 的单条推文走公开接口（长文连正文一起取），'
      + '其他站点按文章正文提取；抓不到正文的不会入库。';
    $('grab-url').value = '';
  }
  updateGrabControls();
  if (manual && grabWantsFocus) { grabWantsFocus = false; focusGrabInput(); }
}

function updateGrabControls() {
  const running = pipeSnapshot?.running;
  $('grab-go').disabled = grabBusy;
  $('grab-go').textContent = grabBusy ? '正在抓取…' : '存入知识库';
  $('grab-url').disabled = grabBusy;
  // 管道在跑时照旧能存:抓取和入库不碰 kb.db / vault,写进知识库那一步由后端
  // 按「占不到就如实说」处理(grab_api 里那一段)。所以这里**不拦**。
  //
  // **「在跑的是不是这一类」要分开说。** 刚存完一条的那几秒正好是 inbox 自己
  // 那一轮在跑,照「其他同步正在运行」写就是在说一件不存在的事 —— 用户刚按下
  // 的那一下反倒被说成别人的任务(截图实测过)。判据和来源工具栏那颗按钮同一个
  // (`pipeRunScopes`),不另写一份。
  if (!grabBusy && running && !$('grab-bar').hidden) {
    const ours = typeof pipeRunScopes === 'function'
      && pipeRunScopes(pipeSnapshot).some(scope => scope.channel === 'inbox');
    $('grab-hint').textContent = ours
      ? '正在把收下的内容写进知识库，完成后全文检索就能搜到。'
      : '其他同步正在运行；现在存下来的内容会在它结束后写进知识库。';
  }
}

function grabKeydown(event) {
  if (event.key === 'Enter') { event.preventDefault(); submitGrab(); }
}

/** 聚焦投递栏。侧栏那个「存链接」入口落到这一页之后调它。 */
function focusGrabInput() {
  const box = $('grab-url');
  if (box && !$('grab-bar').hidden) box.focus({ preventScroll: true });
}

async function submitGrab() {
  if (grabBusy) return;
  const box = $('grab-url'), url = box.value.trim();
  if (!url) { toast('先粘一条链接', { type: 'info' }); box.focus(); return; }
  // 归属:发起这一刻这块面板在说谁。回包落地前要和它逐一对上。
  const kind = typeof sourceKind !== 'undefined' ? sourceKind : '';
  const age = typeof sourceVersion !== 'undefined' ? sourceVersion : 0;
  grabBusy = true;
  updateGrabControls();
  $('grab-hint').textContent = '正在抓这一篇的正文，长文会多等一会儿…';
  let r;
  try {
    r = await fetch('/api/grab', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
  } catch (e) {
    grabBusy = false;
    updateGrabControls();
    toast('没连上服务', { detail: String(e && e.message || e) });
    return;
  }
  grabBusy = false;
  const mine = kind === sourceKind && age === sourceVersion;
  if (!r.ok) {
    // 400 的消息就是「为什么没收下 + 下一步」(后端一档一句话),原样交给用户。
    toast('这条没收下', { detail: await apiFail(r, '抓取失败') });
    if (mine) { updateGrabControls(); $('grab-url').focus(); }
    return;
  }
  let data = {};
  try { data = await r.json(); } catch {}
  const title = (data.title || url).slice(0, 60);
  if (data.duplicate) {
    toast('已经收过了', { type: 'info', detail: data.detail || title });
  } else {
    toast('已存入：' + title, { type: 'ok', detail: data.detail || '' });
  }
  if (!mine) return;                 // 过期的回包:不碰面板(见顶上那段)
  if (!data.duplicate) $('grab-url').value = '';
  updateGrabControls();
  $('grab-hint').textContent = data.pipeline === 'busy'
    ? '内容已存下；其他同步正在运行，稍后点一次「写入知识库」。'
    : '正在写入知识库，完成后全文检索就能搜到。';
  // 列表里马上就该看见它(published 空的条目按收录时刻排在最前,见
  // registry.items 的 COALESCE)。同步状态由 pipe 那条轮询自己刷。
  if (typeof loadSourceModule === 'function') loadSourceModule(false);
  if (typeof refreshPipe === 'function') refreshPipe();
  $('grab-url').focus();
}
