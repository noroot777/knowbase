// ── 手动收录:粘一条链接,内容抓进知识库 ──────────────────────────────────────
// 后端口径在 kb/feeds/grab.py(硬约束 docs/decisions/link-grab.md)。这一层只管
// 三件事:禁掉重复提交、把慢回包的归属对一次、把结果说成人话。
//
// 这颗按钮是**慢请求**(抓一篇网页正文、长文还要再问一次接口,上限 40 秒)。
// 所以三条规矩:
//   ① 在途期间按钮禁用、输入框里的地址不清 —— 失败时用户不用重新粘一次。
//   ② 回包落地前先对一次归属。**归属是这次动作自己的身份**(grabAction +
//      grabNav + sourceKind),**不是列表的 `sourceVersion`** —— 那个数每次自动
//      刷新都 ++(闲时 15 秒、管道跑时 4 秒),借它等于「慢回包一律当过期丢掉」:
//      18 秒那一次实测正常成功,而输入框没清、提示停在「正在抓这一篇的正文…」
//      (2026-10-16 审查实测)。只有**换了来源模块**和**又发起一次**能让旧动作
//      过期;轮询不能。过期之后 toast 照发(那是用户自己的动作,他该知道结果),
//      但一个字都不往面板上写(口径同 frontend-feedback.md 的代次那条)。
//   ③ **每个终态都要落在提示上**:成功、重复、失败、占不到管道、以及「写入
//      那一轮跑完了」。提示只写进行中、不收敛的话,面板会一直停在
//      「正在写入知识库」——而那时它早写完了。

let grabBusy = false;
// 从侧栏那个「存链接」入口进来时才抢焦点:单纯切到「我的订阅」不该把光标
// 从页面上别的地方拽走(一次性标记,面板装好就用掉)。
let grabWantsFocus = false;
// 这次收录的身份:动作号(发起一次就 ++)和导航号(打开来源模块就 ++)。
let grabAction = 0, grabNav = 0;
// 这次收录走到哪一步、面板上该写哪句话。stage: '' 空闲 / writing 正在写入 /
// done 写完了 / pending 存下了但没占到管道 / failed 没收下。
let grabStage = '', grabNote = '', grabRunAt = '';

const GRAB_HINT = 'X 的单条推文走公开接口（长文连正文一起取），'
  + '其他站点按文章正文提取；抓不到正文的不会入库。';

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
    // 打开来源模块 = 一次导航:在途的那次收录从此只发 toast、不再写这块面板,
    // 上一次的终态提示也跟着作废(它说的是上一个面板的事)。
    // **输入框里没提交的那一串留着**:切去别的来源看一眼再回来是常见动作,
    // 替用户把草稿清掉等于让他重新粘一次。只有本次收录成功才清它。
    grabNav++;
    grabStage = ''; grabNote = '';
  }
  updateGrabControls();
  if (manual && grabWantsFocus) { grabWantsFocus = false; focusGrabInput(); }
}

/** 这一轮同步是不是带着手动收录。判据和来源工具栏那颗按钮同一个,不另写一份。 */
function grabRunIsOurs(snap) {
  return !!snap && typeof pipeRunScopes === 'function'
    && pipeRunScopes(snap).some(scope => scope.channel === 'inbox');
}

/** 「正在写入知识库」要自己收敛 —— 那一轮跑完了,提示不能还停在进行中。
 *
 *  **认 `run_id` 变了,不认 running 的跳变**:这条路一次模型都不调,整轮可能
 *  几百毫秒,面板一次都没见过 `running=true`(口径同 sources.js 的
 *  `sourceRunFinished`)。管道同时只跑一轮,所以「我们起跑之后第一轮跑完的」
 *  就是我们那一轮。*/
function grabConverge() {
  if (grabStage !== 'writing') return;
  const snap = pipeSnapshot;
  if (!snap || snap.running) return;
  const id = snap.run_id || '';
  if (!id || id === grabRunAt) return;
  grabStage = 'done';
  grabNote = '已写入知识库，全文检索里搜得到了。';
}

/** 提示栏此刻该写哪句话。**所有写 `#grab-hint` 的路都过这里**,不另写一份。 */
function grabHintText() {
  if (grabBusy) return '正在抓这一篇的正文，长文会多等一会儿…';
  if (grabNote) return grabNote;         // 本次动作的终态(成功/重复/失败/顺延)
  const snap = pipeSnapshot;
  if (snap && snap.running) {
    // **「在跑的是不是这一类」要分开说。** 刚存完一条的那几秒正好是 inbox 自己
    // 那一轮在跑,照「其他同步正在运行」写就是在说一件不存在的事 —— 用户刚按下
    // 的那一下反倒被说成别人的任务(截图实测过)。
    // **别许一个不存在的自动排队**:别人的同步在跑时后端会拒绝起跑这一轮
    // (如实回「稍后点一次」),没有任何机制替用户把它排进去。
    return grabRunIsOurs(snap)
      ? '正在把收下的内容写进知识库，完成后全文检索就能搜到。'
      : '其他同步正在运行；现在照旧能存，等它跑完再点一次「写入知识库」。';
  }
  return GRAB_HINT;
}

function updateGrabControls() {
  // 这一类来源没有投递栏时一个字都不写(它是隐藏的,写了也只是埋一句过期的话)。
  if ($('grab-bar').hidden) return;
  $('grab-go').disabled = grabBusy;
  $('grab-go').textContent = grabBusy ? '正在抓取…' : '存入知识库';
  $('grab-url').disabled = grabBusy;
  grabConverge();
  $('grab-hint').textContent = grabHintText();
  // 管道在跑时照旧能存:抓取和入库不碰 kb.db / vault,写进知识库那一步由后端
  // 按「占不到就如实说」处理(grab_api 里那一段)。所以这里**不拦**。
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
  // 归属:发起这一刻的动作号 + 导航号 + 这块面板在说谁。回包落地前逐一对上。
  const action = ++grabAction, nav = grabNav;
  const kind = typeof sourceKind !== 'undefined' ? sourceKind : '';
  const mine = () => action === grabAction && nav === grabNav
    && kind === (typeof sourceKind !== 'undefined' ? sourceKind : '');
  grabBusy = true;
  grabStage = ''; grabNote = '';
  // 收敛「正在写入」时要认得出「这一轮不是我起跑之前那一轮」。
  grabRunAt = (pipeSnapshot && pipeSnapshot.run_id) || '';
  updateGrabControls();
  let r;
  try {
    r = await fetch('/api/grab', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
  } catch (e) {
    grabBusy = false;
    if (mine()) { grabStage = 'failed'; grabNote = '没连上服务，这条没存下来。'; }
    updateGrabControls();              // 过期也要恢复控件:不然按钮一直是禁用的
    toast('没连上服务', { detail: String(e && e.message || e) });
    return;
  }
  grabBusy = false;
  if (!r.ok) {
    // 400 的消息就是「为什么没收下 + 下一步」(后端一档一句话),原样交给用户。
    const why = await apiFail(r, '抓取失败');
    if (mine()) { grabStage = 'failed'; grabNote = '这条没收下：' + why; }
    updateGrabControls();
    toast('这条没收下', { detail: why });
    if (mine()) $('grab-url').focus();
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
  if (!mine()) { updateGrabControls(); return; }   // 过期的回包:不碰面板
  if (!data.duplicate) $('grab-url').value = '';
  // 终态按后端给的事实分档:started=那一轮起跑了(等 grabConverge 收尾)、
  // busy=存下了但没占到管道(要用户再点一次)、没有 pipeline 这个字段=它早就
  // 走完整条链了(grab_api 的幂等那一档)。
  if (data.pipeline === 'started') {
    grabStage = 'writing';
    grabNote = (data.duplicate ? '这条之前就收过了；' : '已存入；')
      + '正在写入知识库，完成后全文检索就能搜到。';
  } else if (data.pipeline === 'busy') {
    grabStage = 'pending';
    grabNote = (data.detail || '内容已存下；其他同步正在运行') + '。';
  } else {
    grabStage = 'done';
    grabNote = data.detail || '这条已经在知识库里了。';
  }
  updateGrabControls();
  // 列表里马上就该看见它(published 空的条目按收录时刻排在最前,见
  // registry.items 的 COALESCE)。同步状态由 pipe 那条轮询自己刷。
  if (typeof loadSourceModule === 'function') loadSourceModule(false);
  if (typeof refreshPipe === 'function') refreshPipe();
  $('grab-url').focus();
}
