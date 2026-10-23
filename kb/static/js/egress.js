// ── 出口与解析:DoH 端点 / 单次上限 / 出口代理 ────────────────────────────────
// 后端口径在 kb/feeds/egress.py,接口是 /api/egress(读、写、测)。
// **设置优先、环境变量兜底**,**保存即生效**(不用重启),所以这个面板上
// 「看得见的就是生效的」—— 那也是它存在的全部理由。前因后果见
// docs/decisions/dns-egress.md。
//
// **标记在这儿现建,不写进 index.html**:那份已经 673 行、顶着 700 行上限
// (全局约定),而这一块的界面只有这一处用。先例是 ui.js 的 confirmBox。
// 建了就 append、关了就 remove —— workspace.js 的 MutationObserver 只盯**页面
// 初始那批** .modal-mask 的 style 变化,后来挂上去的那个改 display 它看不见;
// 而 body 的 childList 它盯着,所以「建/删」这条路上 syncOverlays 照旧会跑
// (焦点陷阱、inert、Esc 关闭链全靠它)。

// 预设端点**由后端给**(/api/egress 的 presets),这儿一个地址都不许写死。
// 理由不是洁癖:能不能用的判据是「它认不认我们发的那一种请求」,而发请求的那一份
// 在 kb/feeds/resolve.py(`_read` + `JSON_API_PRESETS`)。前端自己列一份的下场实测
// 过 —— 照着「这家有 DoH」列进去的 OpenDNS 只认 RFC8484 wireformat,点一下保存、
// 下一次抓取当场 HTTP 400,面板上只剩一句「域名解析服务不可用」。
// 点一下只填进输入框,不直接保存 —— 保存是另一下,人得先看清填了什么。

function egressMarkup() {
  return `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="egress-title">
  <h2 id="egress-title">出口与解析</h2>
  <div class="row hint">本机 DNS 给错地址时,抓取会敲错门 —— 症状是超时或者「无法解析订阅
    服务器地址」。这里改成问一台可信的 DoH 解析器,并指定抓取走哪个代理。只存本机
    <code>data/secrets.json</code>,<b>保存后下一条抓取就用新值,不用重启</b>;留空的项回落
    到环境变量。</div>
  <div class="eg-row">
    <span class="eg-label">状态</span>
    <span id="egress-state" class="eg-state off">读取中…</span>
    <button type="button" class="ghost" id="egress-retry" hidden onclick="refreshEgress()">重试</button>
    <span class="eg-grow"></span>
    <button type="button" class="ghost" id="egress-probe-btn" onclick="probeEgress()" disabled>测一下</button>
  </div>
  <div class="eg-row">
    <span class="eg-label">DoH 端点</span>
    <input id="egress-endpoint" type="text" autocomplete="off" spellcheck="false"
           aria-label="DoH 端点" placeholder="留空 = 关(走系统解析)" disabled>
    <span class="eg-src" id="egress-endpoint-src"></span>
  </div>
  <div class="eg-row">
    <span class="eg-label"></span>
    <div class="eg-chips" id="egress-chips"></div>
  </div>
  <div class="field-err" id="egress-endpoint-err" role="alert"></div>
  <div class="eg-row">
    <span class="eg-label">单次上限</span>
    <input id="egress-seconds" type="number" min="1" max="60" step="1"
           aria-label="单次解析上限(秒)" placeholder="8" disabled>
    <span class="eg-hint">秒。整次解析还受这一次抓取自己的截止时间管,取小的那个。</span>
    <span class="eg-src" id="egress-seconds-src"></span>
  </div>
  <div class="field-err" id="egress-seconds-err" role="alert"></div>
  <div class="eg-row">
    <span class="eg-label">出口代理</span>
    <input id="egress-proxy" type="text" autocomplete="off" spellcheck="false"
           aria-label="出口代理" placeholder="留空 = 不指定(照旧读系统与环境变量)" disabled>
    <span class="eg-src" id="egress-proxy-src"></span>
  </div>
  <div class="field-err" id="egress-proxy-err" role="alert"></div>
  <div class="eg-row">
    <span class="eg-label">测哪个域名</span>
    <input id="egress-host" type="text" autocomplete="off" spellcheck="false"
           aria-label="测哪个域名" placeholder="mistral.ai" disabled>
    <span class="eg-hint">换成你抓不到的那个。</span>
  </div>
  <div class="eg-probe" id="egress-probe">点「测一下」:按上面这几项真解析一次(用的是抓取时的
同一条判据),报解析出来的地址和耗时 —— 不写盘、不改配置。</div>
  <div class="eg-foot">
    <span id="egress-msg" class="hint"></span>
    <button type="button" class="ghost" onclick="closeEgress()">关闭</button>
    <button type="button" id="egress-save" onclick="saveEgress()" disabled>保存</button>
  </div>
</div>`;
}

// **读到配置之前不许发保存。** 空表单发出去的后果不是「什么都没改」而是「清空」:
// 后端按「空串 = 清掉这一项」处理(那是「关掉」那一下的语义),三项都空就把
// secrets.json 里**整段 egress 删掉**。实测形态:接口慢、或者上一次读取失败,状态
// 还停在「读取中…」而表单是空的,这时点一下保存,toast 说「已保存,下一条抓取就用
// 新配置」,而存着的端点/秒数/代理连同整段一起没了,环境变量又没兜底时 DoH 当场变回
// 「关」。**后端分不开这两件事**(两边都是三个空串,而「清空」是真要支持的),所以
// 判据只能在这儿:**回填成功过一次才放行**。闸门挂在函数第一行,不只挂在按钮的
// disabled 上 —— 后者是给人看的,前者才是判据。
let egressLoaded = false;
// 第几次读取。**晚到的那一次不许回填**:人点了「重试」、或者关掉再开一次时,先发出
// 去的那一份可能在新的之后才回来,盖掉的正好是新的那一份(形态:刚看到的值被换成
// 旧的一份,而页面上零报错)。
let egressRun = 0;

function egressGate() {
  for (const id of ['egress-save', 'egress-probe-btn', 'egress-endpoint',
                    'egress-seconds', 'egress-proxy', 'egress-host']) {
    const e = $(id);
    if (e) e.disabled = !egressLoaded;
  }
}

function egressState(text, on) {
  const e = $('egress-state');
  if (!e) return;
  e.className = 'eg-state ' + (on ? 'on' : 'off');
  e.textContent = text;
}

// 读不到就**保持点不动**,外加一颗「重试」 —— 只飘一句 toast 的那一版把一个空表单
// 留在那儿可保存,而那一下就是「清空」。
function egressReadFailed(detail) {
  egressState('读不到当前配置', false);
  const retry = $('egress-retry');
  if (retry) retry.hidden = false;
  toast('出口配置读取失败', { detail });
}

function openEgress() {
  if (!$('egress-modal')) {
    const mask = document.createElement('div');
    mask.id = 'egress-modal';
    mask.className = 'modal-mask';
    mask.innerHTML = egressMarkup();
    mask.addEventListener('click', e => { if (e.target === mask) closeEgress(); });
    document.body.appendChild(mask);
    // Esc 关闭链认的是 id → 关函数这张表(workspace.js)。现建的弹窗得自己报名,
    // 否则 Esc 只会把焦点陷在里面出不来。
    overlayClosers['egress-modal'] = closeEgress;
  }
  egressLoaded = false;
  egressGate();
  refreshEgress();
}

function closeEgress() { $('egress-modal')?.remove(); }

function egressPick(url) {
  $('egress-endpoint').value = url;
  $('egress-endpoint').focus();
}

function egressClearErrors() {
  for (const f of ['endpoint', 'seconds', 'proxy']) $('egress-' + f + '-err').textContent = '';
}

function egressChips(presets) {
  const box = $('egress-chips');
  if (!box) return;
  box.textContent = '';
  for (const item of (presets || [])) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = item.name || item.url || '';
    // 地址进 DOM 走 value 赋值,不拼进 onclick 串:名单是后端给的,但拼串这条路上
    // 一个引号就能把整段标记拆了。
    b.addEventListener('click', () => egressPick(item.url || ''));
    box.appendChild(b);
  }
}

// **环境变量在兜底时,「留空」不是「关」** —— 它会回落到那一份,状态照旧是「开」。
// 这句话只在框空着的时候看得见,而那正好就是人刚点完「关掉」的那一下;不说的话那颗
// 预设在这种机器上是句假话(保存完状态又跳回「开」,看不出为什么)。
//
// **判据是「环境变量里有没有可回落的值」(后端给的 fallbacks),不是「现在生效的那一份
// 来自哪」(sources)。** 照 sources 判的那一版实测说反话:两层都有、设置那层生效时它
// 是 'settings',于是写「留空 = 关(走系统解析)」—— 人点完「关掉」保存,端点立刻被
// 环境变量那份回填、状态仍是「开」,而他刚看到的那句话写的是反面。
// 到底是哪个变量在兜底由旁边那行出处说(后端给的整句),这儿不复述变量名。
function egressPlaceholders(v) {
  const fb = v.fallbacks || {};
  $('egress-endpoint').placeholder = fb.endpoint
    ? '留空 = 回落到环境变量,不是关'
    : '留空 = 关(走系统解析)';
  $('egress-proxy').placeholder = fb.proxy
    ? '留空 = 回落到环境变量里那一份'
    : '留空 = 不指定(照旧读系统与环境变量)';
  // 秒数那一格同一个毛病(原先固定写默认 8,而环境变量里的 12 清空之后会接着生效),
  // 但它的框只放得下一个数 —— 所以这儿显示的就是**清空之后真正会用的那个数**,
  // 由后端算(口径同上:它知道环境变量里有没有)。
  $('egress-seconds').placeholder = String(v.seconds_when_cleared ?? v.default_seconds ?? 8);
}

function egressApply(v) {
  egressChips(v.presets);
  $('egress-endpoint').value = v.endpoint || '';
  $('egress-seconds').value = v.seconds || '';
  $('egress-proxy').value = v.proxy || '';
  // 出处那一句由后端给(含到底是哪个环境变量在兜底)—— 变量名在这儿再写一份
  // 就是第二份口径,而代理那一族有四个候选名。
  for (const f of ['endpoint', 'seconds', 'proxy']) {
    $(`egress-${f}-src`).textContent = (v.where || {})[f] || '';
  }
  const on = !!v.endpoint;
  egressState(on ? '开 · 问 DoH 解析器' : '关 · 走系统解析', on);
  egressPlaceholders(v);
}

async function refreshEgress() {
  egressClearErrors();
  const run = ++egressRun;
  egressLoaded = false;
  egressGate();
  const retry = $('egress-retry');
  if (retry) retry.hidden = true;
  egressState('读取中…', false);
  try {
    const r = await fetch('/api/egress');
    const detail = r.ok ? '' : await apiFail(r);
    const v = r.ok ? await r.json() : null;
    if (run !== egressRun) return;      // 这一次已经被后发的那次顶掉了
    if (!v) { egressReadFailed(detail); return; }
    egressApply(v);
    egressLoaded = true;
    egressGate();
  } catch (e) {
    if (run !== egressRun) return;
    egressReadFailed(String(e));
  }
}

function egressBody() {
  return {
    endpoint: $('egress-endpoint').value.trim(),
    seconds: $('egress-seconds').value.trim(),
    proxy: $('egress-proxy').value.trim(),
  };
}

// 坏值的 400 带 field:就地红字,不飘 toast(项目约定:字段级校验写在框下面)。
// 认不出字段的失败才走 toast —— 比如服务挂了,那不是「某个框填错了」。
async function egressFailed(r, fallback) {
  // **先 clone 再读**:apiFail 里那一下 r.text() 把 body 消费掉了,之后再 clone 拿到的
  // 是一个读不出东西的壳 —— field 永远是空,红字一条也落不到框下面(全飘成 toast)。
  const copy = r.clone();
  const detail = await apiFail(r, fallback);
  let field = '';
  try { field = (await copy.json()).field || ''; } catch {}
  const box = field && $('egress-' + field + '-err');
  if (box) { box.textContent = detail; $('egress-' + field).focus(); return; }
  toast(fallback, { detail });
}

async function saveEgress() {
  if (!egressLoaded) return;   // 没回填过:发出去的三个空串在后端是「清空」
  egressClearErrors();
  const btn = $('egress-save');
  btn.disabled = true;
  $('egress-msg').textContent = '保存中…';
  try {
    const r = await fetch('/api/egress', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(egressBody()),
    });
    if (!r.ok) { $('egress-msg').textContent = ''; await egressFailed(r, '保存失败'); return; }
    egressApply(await r.json());
    $('egress-msg').textContent = '';
    toast('已保存,下一条抓取就用新配置', { type: 'ok' });
  } catch (e) {
    $('egress-msg').textContent = '';
    toast('保存失败', { detail: String(e) });
  } finally {
    egressGate();
  }
}

async function probeEgress() {
  // 同一道闸:没回填过就测,测的是三个空串 —— 报出来的「走系统解析」跟这台机器上
  // 真正生效的配置无关,而这颗按钮存在的理由就是回答「到底哪一步给错了」。
  if (!egressLoaded) return;
  egressClearErrors();
  const btn = $('egress-probe-btn'), box = $('egress-probe');
  btn.disabled = true;
  box.className = 'eg-probe';
  box.textContent = '正在解析…';
  try {
    const r = await fetch('/api/egress/probe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...egressBody(), host: $('egress-host').value.trim() }),
    });
    if (!r.ok) { box.textContent = ''; await egressFailed(r, '测试失败'); return; }
    const d = await r.json();
    const via = d.via === 'doh' ? '问 DoH 解析器' : '走系统解析';
    if (d.ok) {
      box.className = 'eg-probe';
      box.innerHTML = `<b>${esc(d.host)}</b> · ${via} · ${d.ms} ms\n${d.addresses.map(esc).join('\n')}`;
    } else {
      box.className = 'eg-probe bad';
      box.innerHTML = `<b>${esc(d.host)}</b> · ${via} · ${d.ms} ms\n${esc(d.error)}`;
    }
  } catch (e) {
    box.className = 'eg-probe bad';
    box.textContent = String(e);
  } finally {
    egressGate();
  }
}
