// Isolated browser contract. Run via playwright-cli run-code --filename.
// All API routes are fixtures; acquisition is never invoked on the host.
async page => {
  const checks = [], errors = [], writes = [];
  const check = (name, ok) => { if (!ok) throw new Error(name); checks.push(name); };
  const onError = error => errors.push(error.message);
  page.on('pageerror', onError);
  let status = { state: 'unchecked', message: '检查本机绿泡泡，已有可用配置会直接复用。',
    running: false, provider_ready: true, models_ready: true };
  const base = page.url().match(/^https?:\/\/[^/]+/)[0];
  const respond = (route, data, code = 200) => route.fulfill({
    status: code, contentType: 'application/json', body: JSON.stringify(data),
  });
  // 公众号只剩一条通道了（原来那条逐号文章凭证的在 wechat-oa 分支上）。`aliases` 是
  // 退役通道名 → 接替它的那条：浏览器 localStorage 里存着的可能正是旧名字。
  const WECHAT = {id:'wechat_local', label:'公众号', hint:'读取本机已同步的公众号推送'};
  const channelsPayload = () => ({
    channels: [WECHAT, {id:'rss', label:'RSS', hint:'订阅源'}], default: WECHAT.id,
    labels: {wechat_local: WECHAT.label, wechat_oa: WECHAT.label, rss: 'RSS'},
    aliases: {wechat_oa: WECHAT.id},
  });
  const routeHandler = route => {
    const request = route.request(), path = request.url().slice(base.length).split('?')[0];
    if (path === '/api/pipeline/channels') return respond(route, channelsPayload());
    if (path === '/api/wechat/setup') {
      if (request.method() === 'POST') {
        const body = request.postDataJSON(); writes.push(body);
        if (body.action === 'acquire') {
          check('获取携带已确认令牌', body.confirmed && body.confirmation_token === 'test-token');
          check('获取请求带本机页面标记', request.headers()['x-kb-wechat-action'] === '1');
          status = { ...status, state: 'acquiring', running: true, message: '请在这台 Mac 上完成系统授权' };
        } else status = { ...status, state: 'authorization_required', confirmation_token: 'test-token',
          message: '工具已准备好。确认下方影响后，点击自动获取并验证。' };
      }
      return respond(route, status, request.method() === 'POST' ? 202 : 200);
    }
    const fixtures = {
      '/api/stats': {documents:0,vectors:0,entities:0,extracted:0},
      '/api/facets': {sources:[],accounts:[],topics:[],entities:[]},
      '/api/projects': {tools:[],kinds:{},total_entries:0,topics:[]},
      '/api/favorites': {doc_paths:[],favorites:[],counts:{inbox:0,done:0,archived:0},stale:0},
      '/api/models': {models:[],default:''},
      '/api/keys': {keys:{anthropic:{set:true},openai:{set:false}}},
      '/api/mcp/status': {config:'{}',command:'',tools:[],cli_available:false},
      '/api/pipeline/schedule': {enabled:false,time:'07:30'},
      '/api/pipeline/status': {running:false,stages:{},source:{online:true,ready:true},log:[]},
      '/api/pipeline/pending': {}, '/api/pipeline/accounts': {accounts:['测试公众号']},
      '/api/sources/targets': {manageable:true,label:'公众号',targets:[]},
      '/api/conversations': {conversations:[]}, '/api/stages': {stages:[],providers:[]},
    };
    return respond(route, fixtures[path] || {});
  };
  await page.route('**/api/**', routeHandler);
  try {
    await page.evaluate(() => localStorage.clear());
    await page.setViewportSize({width:1280,height:900});
    await page.goto(base);
    await page.getByRole('button',{name:'完成首次配置'}).click();
    await page.locator('#wechat-check').waitFor({state:'visible'});
    check('首次配置展示供应商和模型状态', await page.locator('#wechat-provider-state').textContent() === '已配置'
      && await page.locator('#wechat-model-state').textContent() === '已就绪');
    await page.locator('#wechat-check').click();
    await page.locator('#wechat-authorization').waitFor({state:'visible'});
    check('未确认影响不能获取', await page.locator('#wechat-acquire').isDisabled());
    await page.locator('#wechat-consent').check();
    await page.locator('#wechat-acquire').click();
    await page.getByText('请在这台 Mac 上完成系统授权', {exact:true}).first().waitFor();
    check('运行中阻止重复启动', await page.locator('#wechat-check').isDisabled());
    await page.reload();
    await page.getByRole('button',{name:'完成首次配置'}).click();
    check('刷新后恢复后台进度', (await page.locator('#wechat-status').textContent()).includes('系统授权'));
    status = { ...status, state:'ready', running:false, ready:true,
      confirmation_token:undefined, message:'绿泡泡数据库已验证可读，可以开始同步。' };
    await page.locator('#wechat-next').waitFor({state:'visible'});
    check('成功后首次提示收起', await page.locator('#wechat-welcome').isHidden());
    await page.locator('#wechat-next').click();
    await page.waitForFunction(() => document.getElementById('pipe-channel-current').value === 'wechat_local');
    // 接入完成后「去同步公众号」把要看/要管的来源定在公众号这条,并勾进这次同步的范围
    check('继续同步选中公众号这条', await page.locator('#pipe-channel-current').inputValue() === 'wechat_local');
    check('公众号也进了这次同步的范围',
      await page.evaluate(() => pipeRunSel.includes('wechat_local')));
    check('同步范围里没有退役的那条', await page.evaluate(() => !pipeChannels.some(c => c.id === 'wechat_oa')));
    check('打开同步中心', await page.locator('#pipepanel').isVisible());
    await page.getByRole('button',{name:'检查并接入绿泡泡',exact:true}).click();
    // 向导里点 API Key / 模型配置:新窗口叠在向导之上,关掉它向导必须还开着
    // (原来是先关向导再开新窗口,关掉新窗口就落回空白页面,得重新点开向导)
    await page.locator('#wechat-modal').waitFor({state:'visible'});
    await page.getByRole('button',{name:'API Key',exact:true}).click();
    await page.locator('#keys-modal').waitFor({state:'visible'});
    check('叠加的 API Key 弹窗接住键盘焦点', await page.evaluate(()=>overlayTop?.id==='keys-modal' && document.getElementById('keys-modal').contains(document.activeElement)));
    check('向导留在底下不关', await page.locator('#wechat-modal').isVisible());
    await page.keyboard.press('Escape');
    check('关掉 API Key 后回到向导', await page.locator('#keys-modal').isHidden() && await page.locator('#wechat-modal').isVisible() && await page.evaluate(()=>overlayTop?.id==='wechat-modal'));
    await page.getByRole('button',{name:'模型配置',exact:true}).click();
    await page.locator('#stages-modal').waitFor({state:'visible'});
    await page.keyboard.press('Escape');
    check('关掉模型配置后回到向导', await page.locator('#stages-modal').isHidden() && await page.locator('#wechat-modal').isVisible());
    check('叠加类用完就摘掉', await page.evaluate(()=>!document.getElementById('keys-modal').classList.contains('stacked') && !document.getElementById('stages-modal').classList.contains('stacked')));
    await page.setViewportSize({width:390,height:844});
    await page.locator('#wechat-modal summary').click();
    check('手机弹窗内容无横向溢出', await page.locator('.wechat-dialog').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    check('说明明确无需 skill', (await page.locator('.wechat-explanation').textContent()).includes('不需要安装个人情报库 skill'));
    await page.screenshot({path:'output/playwright/wechat-setup-mobile.png',fullPage:true});
    await page.setViewportSize({width:1280,height:900});
    await page.screenshot({path:'output/playwright/wechat-setup-desktop.png',fullPage:true});
    await page.keyboard.press('Escape');
    check('Esc 可关闭接入弹窗', await page.locator('#wechat-modal').isHidden());
    status = {state:'host_required',deployment:'docker',can_connect:false,ready:false,running:false,
      provider_ready:true,models_ready:true,message:'当前运行在 Docker 中，请在 Mac 本机获取 key。'};
    await page.evaluate(() => refreshWechatSetup());
    await page.getByRole('button',{name:'完成首次配置'}).click();
    check('Docker 禁止本机接入按钮', await page.locator('#wechat-check').isDisabled()
      && (await page.locator('#wechat-check').textContent()).includes('Mac 本机'));
    check('Docker 隐藏进程授权', await page.locator('#wechat-authorization').isHidden());
    check('Docker 首次说明提示挂载文章', (await page.locator('#wechat-welcome-status').textContent()).includes('挂载文章目录'));
    check('Docker 配置步骤指向文章挂载', (await page.locator('#wechat-title').textContent()).includes('Docker')
      && (await page.locator('#wechat-source-hint').textContent()).includes('挂载 Mac 导出的 vault'));
    await page.evaluate(() => startWechatSetup('connect'));
    check('只产生一次检查和一次明确获取', writes.length === 2);
    check('页面无脚本异常', errors.length === 0);
    return {passed:checks.length,checks,errors,writes:writes.map(x=>x.action)};
  } finally {
    await page.evaluate(() => { invalidatePipeStatus(); clearTimeout(wechatPoll); });
    await page.unroute('**/api/**', routeHandler);
    page.off('pageerror', onError);
  }
}
