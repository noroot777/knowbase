// X 的采集通道:桥接 / feedgrab(cookie) / 我的浏览器登录态。
//
// 通道清单、人话名字、就绪判断全部由后端给(`/api/intake/<kind>/x-channel`)。
// **前端不许把通道 id 和它们各自要填什么写死成一张表**:这里只按后端给的
// id 决定显示哪一块,新增/退役一条通道是后端那一份注册表的事。
// (退役名的翻译在后端做 —— 这份选择存在 feed_settings 里,`get_settings`
//  读出来就已经 resolve 过了,浏览器手里拿不到退役名。)
let xChan = null, xChanKind = '', xChanBusy = false;

function xChannelValue() {
  return $('source-x-channel').value;
}

// 按当前选择显示该填什么。各通道要的东西不一样,全塞在一起只会让人以为
// 选了浏览器还得去填桥接地址。
function xChannelPicked() {
  if (!xChan) return;
  const now = xChannelValue();
  const show = (id, on) => { $(id).hidden = !on; };
  show('source-bridge-field', now === 'bridge');
  show('source-x-feedgrab', now === 'feedgrab');
  show('source-x-browser', now === 'browser');
  const meta = (xChan.channels || []).find(c => c.id === now);
  $('source-x-hint').textContent = meta?.hint || '';
  // 这几行只在"选择没变"时才是当前通道的事实;改了选择就是一句过期的话。
  const same = now === xChan.current;
  $('source-x-state').textContent = same
    ? (xChan.ready ? '当前通道已就绪。' : (xChan.why || '当前通道还没有配好。'))
    : '改完记得点「保存设置」,保存之后才会生效。';
  $('source-x-login').textContent = xChan.login_command || '';
  // 「下一步怎么办」由后端给(`readiness` 的第三项),前端只负责显示。有些通道
  // 要做的事没法塞进一个输入框(比如去浏览器地址栏开一次远程调试开关),它就
  // 从这里出来 —— 这样**新增一条通道不用改这个文件**,口径同上面那句"不许把
  // 通道和它们各自要填什么写死成一张表"。和状态行一样,只在选择没变时才是事实。
  // 去重按**内容**判,不是按通道写死一张表:feedgrab 那档的「下一步」就是上面
  // 那条可复制的命令本身(它的第三项必须带着它 —— 待配置横幅那边没有可复制块,
  // 见 `x_channels.readiness`),并排再写一遍只是噪音。
  const next = same ? (xChan.hint || '') : '';
  $('source-x-next').textContent = next === $('source-x-login').textContent ? '' : next;
  $('source-x-check').hidden = !same;
  // 「打开登录窗口」同理不能在改了选择还没保存时点:后端读的是**存下来**的那一份
  // 通道,于是它只会回一句"当前保存的取法不是浏览器登录态" —— 用户对着一个明明
  // 选着「我的浏览器登录态」的下拉收到这句拒绝,看不出差在哪一步(2026-10-09 实测)。
  // **这里禁用而不是藏掉**(「检查连接」是藏的):那颗按钮产出的是一个**结论**,
  // 贴在新选择旁边就是谎话,没有结论可给就不该出现;这颗是一个**动作**,而且上面
  // 那段说明("在一个独立 profile 里登录一次")指的就是它 —— 藏掉它说明就悬空了,
  // 灰着反倒说得清"现在点不了"。
  const loginBtn = $('source-x-login-btn');
  loginBtn.disabled = !same;
  loginBtn.title = same ? '' : '先点「保存设置」,保存之后才能在这里登录';
}

// 「页面上选的这条通道,后端已经存下来了吗」。禁用登录按钮和恢复它都认这一份。
function xChannelSaved() {
  return !!xChan && xChannelValue() === xChan.current;
}

async function loadXChannel(kind) {
  const panel = $('source-xchan');
  panel.hidden = sourceModules.get(kind)?.adapter !== 'social';
  if (panel.hidden) return;
  const want = kind;
  let data;
  try {
    data = await pipeJson(`/api/intake/${kind}/x-channel`);
  } catch (e) {
    // 读不出来时别把这一块留成空壳:桥接那个输入框还在(它是存量唯一的
    // 取法),顺手给一条重读入口,而不是让人对着一个没有选项的下拉。
    if (want !== sourceKind) return;
    xChan = null;
    $('source-x-channel').innerHTML = '';
    $('source-x-hint').textContent = '';
    $('source-bridge-field').hidden = false;
    $('source-x-feedgrab').hidden = $('source-x-browser').hidden = true;
    $('source-x-check').hidden = true;
    $('source-x-next').textContent = '';
    $('source-x-state').textContent = `读不出可选的取法（${e.message}）。`;
    return;
  }
  if (want !== sourceKind) return;                 // 这期间切走了,别覆盖别人的面板
  xChan = data; xChanKind = kind;
  const sel = $('source-x-channel');
  sel.innerHTML = '';
  for (const c of data.channels || []) {
    const o = document.createElement('option');
    o.value = c.id; o.textContent = c.label; sel.appendChild(o);
  }
  sel.value = data.current;
  xChannelPicked();
}

// 保存设置时附带的那几个字段。社交来源之外返回空对象。
function xChannelPayload(kind) {
  if (!xChan || xChanKind !== kind) return {};
  return { x_channel: xChannelValue(),
           x_feedgrab_bin: $('source-x-bin').value.trim(),
           x_chrome_path: $('source-x-chrome').value.trim() };
}

function fillXChannelFields(settings) {
  if (!('x_channel' in (settings || {}))) return;
  $('source-x-bin').value = settings.x_feedgrab_bin || '';
  $('source-x-chrome').value = settings.x_chrome_path || '';
  if (xChan) { xChan.current = settings.x_channel; $('source-x-channel').value = settings.x_channel; }
  xChannelPicked();
}

async function openXLoginWindow() {
  // 未保存时这颗按钮本来就是禁用的;这一句是给别的入口(键盘、脚本)兜底 ——
  // 发上去只会换回一句"当前保存的取法不是……",白跑一趟还留个看不懂的提示。
  if (xChanBusy || !xChannelSaved()) return;
  xChanBusy = true; $('source-x-login-btn').disabled = true;
  try {
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/login`);
    toast(r.detail, { type: 'info', ms: 12000 });
  } catch (e) { toast('打不开登录窗口', { detail: e.message }); }
  // 恢复成"此刻该不该能点",不是无条件放开:这期间下拉可能已经被改走了。
  finally { xChanBusy = false; $('source-x-login-btn').disabled = !xChannelSaved(); }
}

async function checkXChannel() {
  if (xChanBusy) return;
  xChanBusy = true; $('source-x-check').disabled = true;
  $('source-x-state').textContent = '正在检查…';
  try {
    // 这一步会真起一个 Chrome 或真跑一次 feedgrab doctor,比普通请求慢得多。
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/check`, {}, 120000);
    if (xChan) { xChan.ready = r.ready; xChan.why = r.detail; }
    $('source-x-state').textContent = r.detail;
    toast(r.detail, { type: r.ready ? 'ok' : 'err' });
  } catch (e) {
    $('source-x-state').textContent = e.message;
    toast('检查没跑完', { detail: e.message });
  } finally { xChanBusy = false; $('source-x-check').disabled = false; }
}
