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
//   ③ **每个终态都要落在提示上**:成功、重复、失败、占不到管道、以及「这一篇
//      到底写进去了没有」。提示只写进行中、不收敛的话,面板会一直停在「正在
//      写入知识库」——而那时它早跑完了。收敛分两步:**身份认后端发的那一轮
//      id**(grabConverge),**结论问这一篇自己**(grabCheckItem 去问
//      `/api/grab/state`)。那一轮 export/index 双 ok 只是旁证 —— 阶段全绿而
//      这一篇不在库里是实测形态;谎报已完成的话用户搜不到,再投一次只会得到
//      「已经收过了」。

let grabBusy = false;
// 从侧栏那个「存链接」入口进来时才抢焦点:单纯切到「我的订阅」不该把光标
// 从页面上别的地方拽走(一次性标记,面板装好就用掉)。
let grabWantsFocus = false;
// 这次收录的身份:动作号(发起一次就 ++)和导航号(打开来源模块就 ++)。
let grabAction = 0, grabNav = 0;
// 这次收录走到哪一步、面板上该写哪句话。stage: '' 空闲 / writing 正在写入 /
// done 写完了 / pending 要用户再点一次(没占到管道、那一轮红了、或者结果核不上)
// / checking 正在核对这一篇 / failed 没收下。grabRun 是后端告诉我们「我给你起的
// 是哪一轮」(/api/grab 的 run),grabRunAt 是**起跑之前**面板上那一轮 —— 快照还
// 停在它上面时只是没轮询到新的,不是核不上。grabUrl / grabResume 是这次投的那条
// 地址和它的归属来源该点哪颗按钮(后端给的 resume)。
let grabStage = '', grabNote = '', grabRun = '', grabRunAt = '';
let grabUrl = '', grabResume = '', grabWhy = '';

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
 *  **身份认后端发的那个 run id,结论问这一篇自己。**
 *  ① 身份:`/api/grab` 回的 `run` 就是 `start_pipeline` 发的 id(生产者分配、
 *     原子),快照里的 `run_id` 跟它相等才说明看到的是同一轮。不认 `running`
 *     的跳变(这条路一次模型都不调,整轮可能几百毫秒,面板一次都没见过
 *     `running=true`;口径同 sources.js 的 `sourceRunFinished`),也不许按
 *     「run_id 变了就算跑完」猜 —— 别人插在中间的一轮会被认成自己的。
 *  ② 结论:**那一轮的阶段结果只是旁证,不是结论。** 整轮 export/index 双 ok
 *     只证明那一轮跑完了,证明不了**这一篇**进了知识库:实测 vault 里那一篇被
 *     删掉而两个 hash 还相等时,export 一个字没写、index 正确地把旧索引收掉,
 *     双 ok 而那一篇搜不到。所以收尾去问一次 `/api/grab/state`(只读),按**这
 *     一篇**的事实落终态;阶段的红只用来给失败那句话补上原因。
 *  ③ 核不上(快照已经是别的一轮、后端没给 run)也走同一问:这一篇到底在不在
 *     知识库里是一个随时问得出的事实,比追一轮已经过去的运行可靠。 */
function grabConverge() {
  if (grabStage !== 'writing') return;
  const snap = pipeSnapshot;
  if (!snap) return;
  const id = snap.run_id || '';
  if (id === grabRun) {
    if (snap.running) return;
    const stages = snap.stages || {};
    grabWhy = ['export', 'index'].map(s => stages[s] || {})
      .filter(x => x.status !== 'ok').map(x => x.detail).filter(Boolean)[0] || '';
  } else if (id === grabRunAt) {
    return;                       // 快照还是起跑之前那一张:等下一次轮询
  } else {
    grabWhy = '';                 // 已经是别的一轮:它的阶段结果不是我们那一轮的
  }
  grabStage = 'checking';
  grabNote = '';
  grabCheckItem();
}

/** 问一次「这一篇此刻在知识库里吗」,按答复落终态。
 *
 *  过期判据和投递那一次同一套(动作号 + 导航号):核对期间用户又投了一条、或者
 *  切去了别的来源,这次答复就不往面板上写(口径同 `submitGrab`)。 */
async function grabCheckItem(because) {
  const action = grabAction, nav = grabNav, url = grabUrl;
  let data = null;
  try {
    const r = await fetch('/api/grab/state?url=' + encodeURIComponent(url));
    if (r.ok) data = await r.json();
  } catch (e) { data = null; }
  if (action !== grabAction || nav !== grabNav) return;
  const resume = '；' + (grabResume || '稍后在「手动收录」里点一次「写入知识库」')
    .replace(/^，/, '') + '。';
  const lead = because ? because + '：' : '';
  if (data && data.known && !data.content_ok) {
    // **「写进去了」不等于「写进去的是一篇文章」。** 这一档照旧是可恢复的:
    // 再投一次会重抓、确认得了就把库里那一份换掉(`grab_api._recover`)。
    grabStage = 'pending';
    grabNote = lead + '这一篇收下的内容看不出是正文；再投一次这条链接，'
      + '会重抓并在确认得了时把它换掉。';
  } else if (data && data.done) {
    grabStage = 'done';
    grabNote = lead + '已写入知识库，全文检索里搜得到了。';
  } else if (data && because && !data.known) {
    // 断在半路那一档:库里压根没有这一行 —— 这时才说得出「没存下来」。
    grabStage = 'failed';
    grabNote = because + '，这条没存下来；再投一次。';
  } else if (data) {
    grabStage = 'pending';
    grabNote = lead + (because ? '这一篇还没进知识库（' : '那一轮跑完了，这一篇还没进知识库（')
      + (grabWhy || data.owing || '原因未知') + '）' + resume;
  } else {
    grabStage = 'pending';
    grabNote = lead + '没核到这一篇的结果' + resume;
  }
  updateGrabControls();
}

/** 提示栏此刻该写哪句话。**所有写 `#grab-hint` 的路都过这里**,不另写一份。 */
function grabHintText() {
  if (grabBusy) return '正在抓这一篇的正文，长文会多等一会儿…';
  if (grabStage === 'checking') return '正在核对这一篇写进知识库了没有…';
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
  // 这一轮的身份要等后端发(不自己猜);起跑之前那一轮的 id 先记下来,收敛时用它
  // 分清「还没轮询到」和「核不上」。
  grabRun = '';
  grabRunAt = (pipeSnapshot && pipeSnapshot.run_id) || '';
  grabUrl = url; grabResume = ''; grabWhy = '';
  updateGrabControls();
  let r;
  try {
    r = await fetch('/api/grab', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
  } catch (e) {
    // **回包没送达 ≠ 后端没存下来。** 保存排在起管道之前,断线可能发生在它之后 ——
    // 说「这条没存下来」是在断言一件我们不知道的事,而用户据此会去别处找原因。
    // 出路是现成的:去问一次这一篇自己的状态(`/api/grab/state`,只读),问得到
    // 就照事实说,问不到就如实说「结果未知,可以再投一次」(重投是安全的:
    // `DO NOTHING` 挡着,内容不会变成两份)。
    grabBusy = false;
    if (mine()) { grabStage = 'checking'; grabNote = ''; }
    updateGrabControls();              // 过期也要恢复控件:不然按钮一直是禁用的
    toast('没连上服务', { detail: String(e && e.message || e) });
    if (mine()) grabCheckItem('连接断在半路，结果不确定');
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
  grabUrl = data.url || url;          // 后端规范化之后那一条才是库里的身份
  grabResume = data.resume || '';     // 归属来源该点哪颗按钮(后端算,前端不另写一份)
  if (data.pipeline === 'started') {
    grabStage = 'writing';
    grabRun = data.run || '';         // 那一轮的身份(没给就直接去问这一篇的事实)
    grabNote = (data.duplicate ? '这条之前就收过了；' : '已存入；')
      + '正在写入知识库，完成后全文检索就能搜到。';
  } else if (data.pipeline === 'busy') {
    grabStage = 'pending';
    grabNote = (data.detail || '内容已存下；其他同步正在运行') + '。';
  } else if (data.content_ok === false) {
    // 库里那一份看不出是正文,而这次重抓也没换成(撞上关卡 / 还是确认不了 /
    // 这期间别人已经动过它)。**这不是终态**:再投一次还会重抓。
    grabStage = 'pending';
    grabNote = (data.detail || '库里那一份看不出是正文') + '。';
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
