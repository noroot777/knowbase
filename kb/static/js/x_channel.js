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
  $('source-x-check').hidden = !same;
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
  if (xChanBusy) return;
  xChanBusy = true; $('source-x-login-btn').disabled = true;
  try {
    const r = await pipePost(`/api/intake/${xChanKind}/x-channel/login`);
    toast(r.detail, { type: 'info', ms: 12000 });
  } catch (e) { toast('打不开登录窗口', { detail: e.message }); }
  finally { xChanBusy = false; $('source-x-login-btn').disabled = false; }
}

async function checkXChannel() {
  if (xChanBusy) return;
  xChanBusy = true; $('source-x-check').disabled = true;
  $('source-x-state').textContent = '正在检查…';
  try {
    // 这一步会真起一个 Chrome 或真跑一次 feedgrab doctor,比普通请求慢得多。
    const r = await pipeJson(`/api/intake/${xChanKind}/x-channel/check`, {}, 120000);
    if (xChan) { xChan.ready = r.ready; xChan.why = r.detail; }
    $('source-x-state').textContent = r.detail;
    toast(r.detail, { type: r.ready ? 'ok' : 'err' });
  } catch (e) {
    $('source-x-state').textContent = e.message;
    toast('检查没跑完', { detail: e.message });
  } finally { xChanBusy = false; $('source-x-check').disabled = false; }
}
