// Run through Playwright CLI against tests/serve_workspace.py (headless).
// npx --yes --package @playwright/cli playwright-cli -s=sync-check run-code --filename tests/pipeline_probe.js
// Every API route is isolated. No collector, schedule or model is actually invoked.
async page => {
  const checks = [], errors = [], writes = [];
  const check = (name, value) => { if (!value) throw new Error(name); checks.push(name); };
  const onError = e => errors.push(e.message);
  page.on('pageerror', onError);
  const origin = page.url().match(/^https?:\/\/[^/]+/)[0];
  const keys = ['crawl','export','index','extract','projects','wiki'];
  const emptyStages = () => Object.fromEntries(keys.map(k=>[k,{status:'pending',detail:''}]));
  const retryStages = plan => Object.fromEntries(keys.map(k=>[k,{status:plan.includes(k)?'pending':'skipped',detail:plan.includes(k)?'':'本次无需重试'}]));
  let state = {running:false,stages:{},log:[],stop_supported:true};
  let holdSource = true, releaseSource = null, holdStatus = false, releaseStatus = null;
  let statusCount = 0, statusFail = false, holdStop = false, releaseStop = null;
  let sourceCount = 0, pendingCount = 0;
  let holdB = false, releaseB = null, accountFail = false, startFail = false;
  let holdAcct = false; const heldAcct = [], acctCalls = {};
  let holdStart = false, releaseStart = null, scheduleFail = false;
  let holdChannels = false, releaseChannels = null, channelFail = false;
  let clockInstalled = false;
  const accounts = {a:['公众号甲','公众号乙','公众号丙 "<测试>'],b:['另一通道甲','另一通道乙']};
  const reply = (route,data,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)}).catch(()=>{});
  const copy = value => JSON.parse(JSON.stringify(value));
  const settlePaint = () => page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  // 换视口后弹窗的 max-height 按新 vh 重算要多一帧,别拿中间态去断言底部按钮位置
  const settleSetup = () => page.waitForFunction(()=>{const el=document.querySelector('.pipe-setup');return !el || el.getBoundingClientRect().bottom<=innerHeight;},null,{timeout:5000});
  const handler = async route => {
    const request = route.request(), url = request.url();
    const path = url.slice(origin.length).split('?')[0];
    if (request.method() !== 'GET') writes.push({path,body:request.postDataJSON()});
    if (path === '/api/pipeline/status') {
      if (!url.includes('source=false')) {
        sourceCount++;
        if (holdSource) { releaseSource=()=>reply(route,{source:{online:true,label:'测试来源',message:'已连接'}}); return; }
        return reply(route,{source:{online:true,label:'测试来源',message:'已连接'}});
      }
      if (url.includes('pending=true')) { pendingCount++; return reply(route,{...copy(state),pending:{extract:0}}); }
      statusCount++;
      if (holdStatus) { releaseStatus=()=>reply(route,copy(state)); return; }
      return reply(route,statusFail?{detail:'测试网络中断'}:copy(state),statusFail?503:200);
    }
    if (path === '/api/pipeline/accounts') {
      const channel = url.match(/[?&]channel=([^&]*)/)?.[1];
      acctCalls[channel] = (acctCalls[channel] || 0) + 1;
      if (channel==='b' && holdB) { releaseB=()=>reply(route,{accounts:accounts.b}); return; }
      if (holdAcct) { heldAcct.push(route); return; }
      return reply(route,accountFail?{detail:'测试列表加载失败'}:{accounts:accounts[channel] || []},accountFail?503:200);
    }
    if (path === '/api/pipeline/stop') {
      if (holdStop) { releaseStop=()=>{state.stop_requested=true;return reply(route,{ok:true});};return; }
      state.stop_requested=true; return reply(route,{ok:true});
    }
    if (path === '/api/pipeline/run') {
      if (startFail) return reply(route,{detail:'测试启动失败，请检查采集器'},500);
      const start = () => {
        state={running:true,run_kind:'sync',planned_stages:[...keys],stop_requested:false,stop_supported:true,started_at:'2026-09-26 12:00:00',crawl_filter:request.postDataJSON(),stages:emptyStages(),log:['同步开始']};
        state.stages.crawl={status:'running',detail:'抓取公众号甲'};
        return reply(route,{ok:true});
      };
      if (holdStart) { releaseStart=start;return; }
      return start();
    }
    if (path === '/api/pipeline/retry-failed') {
      state={...state,running:true,run_kind:'retry',planned_stages:['extract'],finished_at:'',stages:retryStages(['extract'])};
      state.stages.extract={status:'running',detail:'重试提取'};return reply(route,{ok:true,plan:'extract'});
    }
    if (path === '/api/pipeline/schedule' && request.method()==='POST') return reply(route,scheduleFail?{detail:'测试保存失败'}:request.postDataJSON(),scheduleFail?500:200);
    if (path === '/api/pipeline/channels' && holdChannels) {
      releaseChannels = () => reply(route,{default:'a',channels:[{id:'a',label:'凭证通道'},{id:'b',label:'本机通道'}]});
      return;
    }
    if (path === '/api/pipeline/channels' && channelFail) return reply(route,{detail:'测试通道读取失败'},503);
    const fixtures = {
      '/api/pipeline/channels':{default:'a',channels:[{id:'a',label:'凭证通道'},{id:'b',label:'本机通道'}]},
      '/api/pipeline/schedule':{enabled:false,time:'07:30',channel:'a'},
      '/api/sources/targets':{manageable:false},
      '/api/stats':{documents:100,vectors:100,entities:50,extracted:90},
      '/api/facets':{sources:[],accounts:[],topics:[],entities:[]},
      '/api/projects':{tools:[],kinds:{},topics:[],total_entries:0},
      '/api/favorites':{doc_paths:[],favorites:[],counts:{inbox:0,done:0,archived:0},stale:0},
      '/api/favorites/settings':{model:'openai:test'}, '/api/favorites/triage':{running:false},
      '/api/models':{models:[],default:''}, '/api/keys':{keys:{}}, '/api/mcp/status':{config:'{}',tools:[]},
      '/api/conversations':{conversations:[]},
    };
    return reply(route,fixtures[path] || {});
  };
  await page.route('**/api/**',handler);
  // Finish a navigation-triggered read before requesting the next fixture state.
  const refresh = () => page.evaluate(async()=>{ await pipeRequest; await refreshPipe(); });
  const countWrites = path => writes.filter(w=>w.path===path).length;
  const finish = () => {
    state.running=false;state.finished_at='2026-09-26 12:06:00';
    state.stages=Object.fromEntries(keys.map(k=>[k, !state.planned_stages || state.planned_stages.includes(k) ? {status:'ok',detail:'完成'} : {status:'skipped',detail:state.run_kind==='retry'?'本次无需重试':'未选择'}]));
    state.stopped=false;state.stop_requested=false;
  };
  try {
    await page.setViewportSize({width:1440,height:1100});
    await page.emulateMedia({reducedMotion:'reduce'});
    await page.evaluate(()=>{localStorage.removeItem('kb-sync-channel');localStorage.removeItem('kb-sync-channels');});
    const channelBox = name => page.locator('#pipe-channel-list').getByRole('checkbox',{name,exact:true});
    const sorted = list => [...list].sort().join('|');
    state={running:true,stop_supported:true,stages:emptyStages(),log:[]};
    state.stages.crawl={status:'running',detail:'后台定时同步'};
    await page.goto(origin+'/?pipeline-test='+Date.now()+'#gallery');
    await page.locator('#nav-sync-dot').waitFor({state:'visible',timeout:10000});
    check('默认图鉴冷启动即可发现后台同步',await page.locator('#gallerypanel').isVisible() && !await page.locator('#pipepanel').isVisible());
    await page.reload();
    await page.locator('#nav-sync-dot').waitFor({state:'visible',timeout:10000});
    check('刷新后不进入同步页仍显示运行提示',await page.locator('#tab-gallery').getAttribute('aria-current')==='page');
    check('冷启动只读取轻量状态不触发采集器与积压扫描',sourceCount===0 && pendingCount===0);
    await settlePaint();
    await page.screenshot({path:'output/playwright/review-background-running.png'});
    statusFail=true;await page.reload();
    await page.waitForFunction(()=>!pipeRequest && !document.getElementById('pipe-connection').hidden);
    statusFail=false;
    await page.locator('#nav-sync-dot').waitFor({state:'visible',timeout:12000});
    check('首次状态读取失败会在图鉴自动恢复',await page.locator('#tab-gallery').getAttribute('aria-current')==='page');
    holdSource=false;finish();
    await page.locator('#nav-sync-dot').waitFor({state:'hidden',timeout:10000});
    check('后台运行结束自动清除图鉴侧栏提示',await page.locator('#gallerypanel').isVisible());
    state={running:true,stop_supported:true,stages:emptyStages(),log:[]};
    state.stages.crawl={status:'running',detail:'另一个标签页启动的同步'};
    await page.locator('#nav-sync-dot').waitFor({state:'visible',timeout:12000});
    check('留在图鉴也能发现后来启动的后台同步',await page.locator('#gallerypanel').isVisible());
    state={running:false,stages:{},log:[],stop_supported:true};
    holdSource=true;releaseSource=null;holdStatus=true;
    await page.goto(origin+'/?pipeline-test='+Date.now()+'#pipe');
    await page.locator('#pipe-run').click();await page.locator('input[data-acct]').first().waitFor();
    check('首次状态请求未返回也能打开新建同步',await page.locator('#pipe-modal').isVisible());
    check('读取状态时在配置内说明原因并防止实际启动',await page.locator('#pipe-setup-connection').isVisible() && await page.locator('#pipe-start').isDisabled());
    await page.getByRole('button',{name:'暂不执行',exact:true}).click();
    holdStatus=false;await releaseStatus();await page.waitForFunction(()=>pipeConnected && !pipeRequest);
    check('慢采集器探测不阻塞同步状态',holdSource && !!releaseSource && await page.locator('.pipe-stage').count()===6);
    holdSource=false;await releaseSource();
    state={running:true,stop_supported:true,started_at:'2026-09-26 12:00:00',crawl_filter:{channel:'a',accounts:['公众号甲']},stages:emptyStages(),log:Array.from({length:80},(_,i)=>`[12:00:${String(i).padStart(2,'0')}] 处理测试内容 ${i}`)};
    state.stages.crawl={status:'running',detail:'下载正文 12/40 篇',failures:[{title:'待补文章',reason:'等待来源恢复',pending:true}]};
    state.stages.extract={status:'running',detail:'正在处理存量文档'};
    await refresh();
    check('并行步骤都显示执行中',(await page.locator('#pipe-state-title').textContent()).includes('抓取内容 + 提取知识'));
    state.crawl_filter={channel:'a',accounts:['公众号甲'],channels:[{channel:'a',accounts:['公众号甲']},{channel:'b',accounts:[]}]};
    await refresh();
    check('运行中列出本次抓取的全部来源',(await page.locator('#pipe-running-channel').textContent()).includes('凭证通道 + 本机通道') && (await page.locator('#pipe-run-scope').textContent()).includes('凭证通道 1 个指定订阅 · 本机通道 全部订阅'));
    state.crawl_filter={channel:'a',accounts:['公众号甲']};await refresh();
    check('只抓一个来源时不重复标注来源名',(await page.locator('#pipe-run-scope').textContent()).startsWith('1 个指定订阅'));
    check('运行时禁用重复启动与切换查看来源',await page.locator('#pipe-run').isDisabled() && await page.locator('#pipe-channel-current').isDisabled());
    check('运行时侧栏有后台任务提示',await page.locator('#nav-sync-dot').isVisible());
    await page.locator('#pipe-stage-crawl summary').click();
    await page.locator('#pipe-log-box').evaluate(el=>{el.scrollTop=0;el.dispatchEvent(new Event('scroll'));});
    check('阅读旧日志自动暂停跟随',!await page.locator('#pipe-log-follow').isChecked());
    state.log.push('[12:02:00] 新进展');state.stages.crawl.detail='下载正文 13/40 篇';
    await refresh();
    check('刷新保留展开明细与键盘焦点',await page.locator('#pipe-stage-crawl details').evaluate(el=>el.open && el.contains(document.activeElement)));
    check('刷新不会抢走日志阅读位置',await page.locator('#pipe-log-box').evaluate(el=>el.scrollTop===0));
    await page.locator('#pipe-log-latest').click();
    check('可一键恢复跟随最新日志',await page.locator('#pipe-log-follow').isChecked() && await page.locator('#pipe-log-box').evaluate(el=>el.scrollHeight-el.scrollTop-el.clientHeight<3));
    await page.setViewportSize({width:1024,height:1100});
    await page.waitForFunction(()=>{const el=document.getElementById('pipe-log-box');return el.scrollHeight-el.scrollTop-el.clientHeight<3;});
    check('调整窗口后仍能跟随最新日志',await page.locator('#pipe-log-follow').isChecked());
    await page.setViewportSize({width:1440,height:1100});
    await page.screenshot({path:'output/playwright/sync-running-desktop.png'});
    statusFail=true;await refresh();
    check('断线保留现有结果并提示重连',await page.locator('#pipe-connection').isVisible() && (await page.locator('#pipe-stage-crawl .pipe-stage-detail').textContent()).includes('13/40'));
    check('状态不确定时禁用写操作',await page.locator('#pipe-stop').isDisabled());
    statusFail=false;await page.locator('#pipe-connection button').click();
    await page.waitForFunction(()=>document.getElementById('pipe-connection').hidden);
    check('重连后恢复操作',!await page.locator('#pipe-stop').isDisabled());
    holdStatus=true;const beforeStatus=statusCount;
    await page.evaluate(()=>{refreshPipe();refreshPipe();refreshPipe();});
    await page.waitForFunction(()=>!!pipeRequest);
    await page.waitForTimeout(100);
    check('慢状态请求不会堆积并发轮询',statusCount===beforeStatus+1);
    holdStatus=false;await releaseStatus();await page.waitForFunction(()=>!pipeRequest);
    holdStop=true;await page.locator('#pipe-stop').click();
    check('停止提交有明确等待反馈',await page.locator('#pipe-stop').isDisabled() && (await page.locator('#pipe-stop').textContent()).includes('等待'));
    await page.locator('#pipe-stop').evaluate(el=>el.click());
    check('停止请求防重复提交',countWrites('/api/pipeline/stop')===1);
    await releaseStop();await page.waitForFunction(()=>document.getElementById('pipe-state-title').textContent.includes('收尾'));
    check('停止不伪装成瞬间完成',await page.locator('#pipe-run').isDisabled());
    finish();state.stopped=true;
    for (const key of keys.slice(1)) state.stages[key]={status:'stopped',detail:'已停止，未继续执行'};
    await refresh();
    check('停止后说明结果保留且允许新建',(await page.locator('#pipe-state-hint').textContent()).includes('已保留') && !await page.locator('#pipe-run').isDisabled());
    await settlePaint();
    check('第一步后停止保留1/6完成且进度不跳满',await page.locator('#pipe-progress-label').textContent()==='1 / 6 完成' && await page.locator('#pipe-progress-fill').evaluate(el=>Math.abs(el.getBoundingClientRect().width/el.parentElement.getBoundingClientRect().width-.17)<.005) && await page.locator('.pipe-stage[data-status=stopped]').count()===5);
    await page.screenshot({path:'output/playwright/review-stopped-progress.png',fullPage:true});
    state={running:false,stop_supported:true,stages:{},log:[],last_run:copy(state)};
    await refresh();
    check('持久化的停止记录也保持实际完成进度',await page.locator('#pipe-progress-label').textContent()==='1 / 6 完成' && await page.locator('#pipe-progress-fill').evaluate(el=>el.style.width==='17%'));
    state={running:true,stop_supported:true,stages:Object.fromEntries(keys.map(k=>[k,{status:'skipped',detail:'本次不执行'}])),log:[]};
    state.stages.extract={status:'running',detail:'仅执行提取'};await refresh();
    check('跳过五步仅执行一步时不会预先填满进度',await page.locator('#pipe-progress-label').textContent()==='0 / 6 完成' && await page.locator('#pipe-progress-fill').evaluate(el=>el.style.width==='0%'));
    state.running=false;state.finished_at='2026-09-26 12:06:00';state.stages.extract={status:'ok',detail:'提取完成'};await refresh();
    check('部分步骤跳过时进度与完成计数一致',await page.locator('#pipe-progress-label').textContent()==='1 / 6 完成' && await page.locator('#pipe-progress-fill').evaluate(el=>el.style.width==='17%'));
    const retryPlan=['export','index','extract'];
    state={running:true,run_kind:'retry',planned_stages:retryPlan,stop_supported:true,stages:retryStages(retryPlan),log:[]};
    state.stages.export={status:'ok',detail:'回填完成'};state.stages.index={status:'ok',detail:'索引完成'};state.stages.extract={status:'running',detail:'重试提取'};await refresh();
    check('三步重试运行中按本次计划显示2/3和67%',await page.locator('#pipe-progress-label').textContent()==='2 / 3 完成' && await page.locator('#pipe-progress-fill').evaluate(el=>el.style.width==='67%'));
    check('运行标题与按钮明确正在重试',(await page.locator('#pipe-state-title').textContent()).includes('正在重试') && await page.locator('#pipe-run').textContent()==='重试进行中');
    state.running=false;state.finished_at='2026-09-28 08:00:00';state.stages.extract={status:'error',detail:'失败 1'};await refresh();
    check('重试仍有失败保持2/3并显示需处理',await page.locator('#pipe-progress-label').textContent()==='2 / 3 完成' && await page.locator('#pipe-state-title').textContent()==='重试结束，1 个步骤需处理');
    state.stages.extract={status:'ok',detail:'提取完成'};await refresh();await settlePaint();
    check('三步重试全部成功显示3/3及满进度',await page.locator('#pipe-progress-label').textContent()==='3 / 3 完成' && await page.locator('#pipe-progress-fill').evaluate(el=>el.style.width==='100%'));
    check('无需重试的步骤不误报部分跳过',await page.locator('#pipe-state-title').textContent()==='重试已完成' && (await page.locator('#pipe-state-hint').textContent()).includes('3 个步骤均已完成') && await page.locator('.pipe-stage[data-status=not_planned]').count()===3);
    const completedRetry=copy(state);
    state={running:false,stop_supported:true,stages:{},log:[],last_run:copy(completedRetry)};
    await page.reload();await page.waitForFunction(()=>document.getElementById('pipe-state-title').textContent==='重试已完成');
    check('刷新后历史重试计划仍显示3/3',await page.locator('#pipe-progress-label').textContent()==='3 / 3 完成');
    await settlePaint();await page.screenshot({path:'output/playwright/retry-completed-plan.png'});
    await page.setViewportSize({width:375,height:812});await settlePaint();
    check('手机重试完成态无横向溢出',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:'output/playwright/retry-completed-plan-mobile.png'});
    await page.setViewportSize({width:1440,height:1100});
    delete state.last_run.run_kind;delete state.last_run.planned_stages;await refresh();
    check('兼容旧重试记录按无需重试标记还原3/3',await page.locator('#pipe-progress-label').textContent()==='3 / 3 完成' && await page.locator('#pipe-state-title').textContent()==='重试已完成');
    state={...copy(completedRetry),stopped:true};
    state.stages.index={status:'stopped',detail:'停止'};state.stages.extract={status:'stopped',detail:'停止'};await refresh();
    check('重试中途停止仍以原计划三步为分母',await page.locator('#pipe-progress-label').textContent()==='1 / 3 完成' && await page.locator('#pipe-progress-fill').evaluate(el=>el.style.width==='33%') && await page.locator('#pipe-state-title').textContent()==='重试已停止');
    state={running:false,run_kind:'retry',planned_stages:['extract'],finished_at:'2026-09-28 08:00:00',stages:retryStages(['extract']),log:[]};state.stages.extract={status:'ok',detail:'完成'};await refresh();
    check('仅重试一步成功显示1/1满进度',await page.locator('#pipe-progress-label').textContent()==='1 / 1 完成' && await page.locator('#pipe-progress-fill').evaluate(el=>el.style.width==='100%'));
    state={running:false,run_kind:'retry',planned_stages:[...keys],finished_at:'2026-09-28 08:00:00',stages:Object.fromEntries(keys.map(k=>[k,{status:'ok',detail:'完成'}])),log:[]};await refresh();
    check('六步全纳入重试时仍识别重试完成',await page.locator('#pipe-progress-label').textContent()==='6 / 6 完成' && await page.locator('#pipe-state-title').textContent()==='重试已完成');
    state={running:false,stop_supported:true,stages:{},log:[]};
    finish();await refresh();
    check('六步全部成功才显示完整进度',await page.locator('#pipe-progress-label').textContent()==='6 / 6 完成' && await page.locator('#pipe-progress-fill').evaluate(el=>el.style.width==='100%'));
    statusFail=true;await refresh();
    check('空闲时断线不会禁用新建配置入口',!await page.locator('#pipe-run').isDisabled() && await page.locator('#pipe-run').textContent()==='新建同步');
    await page.locator('#pipe-run').click();await page.locator('input[data-acct]').first().waitFor();
    await page.waitForFunction(()=>!pipeRequest);
    check('断线仍能配置且在弹窗内提供重连',await page.locator('#pipe-setup-connection button').isEnabled() && (await page.locator('#pipe-est').textContent()).includes('恢复状态连接'));
    const disconnectedWrites=countWrites('/api/pipeline/run');
    await page.locator('#pipe-start').evaluate(el=>el.click());
    check('断线配置不会发出启动请求',await page.locator('#pipe-start').isDisabled() && countWrites('/api/pipeline/run')===disconnectedWrites);
    await page.getByRole('checkbox',{name:'公众号甲',exact:true}).uncheck();
    await page.locator('#pipe-range-btn').click();await page.getByRole('button',{name:'近 7 天',exact:true}).click();
    await page.locator('#pipe-setup-connection button').click();await page.waitForFunction(()=>!pipeRequest);
    check('重连失败后可继续重试且保留配置',!await page.getByRole('checkbox',{name:'公众号甲',exact:true}).isChecked() && await page.locator('#pipe-range-label').textContent()==='近 7 天' && await page.locator('#pipe-setup-connection button').isEnabled());
    await page.setViewportSize({width:375,height:812});
    await settlePaint();await settleSetup();
    check('手机断线配置仍显示提示及底部按钮且不横向溢出',await page.locator('#pipe-setup-connection').isVisible() && await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth && document.querySelector('.pipe-setup-footer').getBoundingClientRect().bottom<=innerHeight));
    await page.screenshot({path:'output/playwright/new-sync-disconnected-after.png'});
    await page.setViewportSize({width:1440,height:1100});
    statusFail=false;state.running=true;
    await page.locator('#pipe-setup-connection button').click();await page.waitForFunction(()=>pipeConnected && !pipeRequest);
    check('重连发现后台已有任务时明确提示并阻止重复启动',(await page.locator('#pipe-setup-connection').textContent()).includes('已有同步') && await page.locator('#pipe-start').isDisabled());
    state.running=false;await refresh();
    check('后台恢复空闲后可继续启动且配置未丢失',await page.locator('#pipe-start').isEnabled() && !await page.getByRole('checkbox',{name:'公众号甲',exact:true}).isChecked() && await page.locator('#pipe-range-label').textContent()==='近 7 天');
    await page.getByRole('checkbox',{name:'公众号甲',exact:true}).check();
    await page.locator('#pipe-range-btn').click();await page.getByRole('button',{name:'不限时间',exact:true}).click();
    await page.getByRole('button',{name:'暂不执行',exact:true}).click();
    await page.locator('#pipe-run').click();await page.locator('input[data-acct]').first().waitFor();
    check('公众号是可键盘操作的真实复选框',await page.getByRole('checkbox',{name:'公众号丙 "<测试>',exact:true}).isChecked());
    await page.getByRole('checkbox',{name:'公众号甲',exact:true}).uncheck();
    check('单选后不重建列表丢焦点',await page.getByRole('checkbox',{name:'公众号甲',exact:true}).evaluate(el=>el===document.activeElement));
    await page.locator('#pipe-range-btn').click();await page.getByRole('button',{name:'近 7 天',exact:true}).click();
    check('近7天包含今天共7天',await page.evaluate(()=>{const p=pipePresets()[2];return Math.round((Date.parse(p.to)-Date.parse(p.from))/86400000)===6;}));
    await page.locator('#pipe-acct-search').fill('公众号乙');await page.locator('#pipe-select-none').click();
    check('取消搜索结果不会清空其他已选账号',(await page.locator('#pipe-acct-count').textContent()).includes('已选 1'));
    await page.locator('#pipe-select-all').click();
    await page.getByRole('button',{name:'暂不执行',exact:true}).click();
    await page.locator('#pipe-run').click();await page.locator('input[data-acct]').first().waitFor();
    check('重开配置保留账号与日期草稿',!await page.getByRole('checkbox',{name:'公众号甲',exact:true}).isChecked() && (await page.locator('#pipe-range-label').textContent())==='近 7 天');
    holdB=true;await channelBox('本机通道').check();
    check('新勾来源读取期间禁止启动但保留已就绪来源',await page.locator('#pipe-start').isDisabled() && (await page.locator('#pipe-est').textContent()).includes('订阅读取完成') && await page.getByRole('checkbox',{name:'公众号乙',exact:true}).isVisible());
    holdB=false;await releaseB();await page.getByRole('checkbox',{name:'另一通道甲',exact:true}).waitFor();
    check('两个来源的订阅同时列出并各自计数',await page.locator('input[data-acct]').count()===5 && await page.locator('#pipe-acct-count').textContent()==='已选 4 / 5' && await page.locator('.pipe-acct-group').count()===2);
    await channelBox('凭证通道').uncheck();
    check('取消来源后它的订阅立即退出本次范围',await page.locator('input[data-acct]').count()===2 && await page.getByRole('checkbox',{name:'公众号乙',exact:true}).count()===0);
    await channelBox('凭证通道').check();await page.getByRole('checkbox',{name:'公众号乙',exact:true}).waitFor();
    check('重新勾回来源沿用上次的订阅选择',!await page.getByRole('checkbox',{name:'公众号甲',exact:true}).isChecked() && await page.getByRole('checkbox',{name:'公众号乙',exact:true}).isChecked());
    accountFail=true;await channelBox('凭证通道').uncheck();await channelBox('凭证通道').check();
    await page.getByRole('button',{name:'重新读取订阅',exact:true}).waitFor();
    check('一个来源读取失败只停掉它自己',await page.locator('.pipe-acct-group').count()===2 && await page.locator('input[data-acct]').count()===2 && await page.locator('#pipe-start').isEnabled() && (await page.locator('#pipe-est').textContent()).includes('1 个来源读取失败'));
    accountFail=false;await page.getByRole('button',{name:'重新读取订阅',exact:true}).click();await page.getByRole('checkbox',{name:'公众号乙',exact:true}).waitFor();
    check('就地重试恢复该来源的订阅',await page.locator('input[data-acct]').count()===5);
    startFail=true;await page.locator('#pipe-start').click();
    await page.waitForFunction(()=>document.getElementById('pipe-setup-error').textContent.includes('同步未启动') && !pipeAction);
    check('启动失败保留表单并给出具体原因',await page.locator('#pipe-modal').isVisible() && (await page.locator('#pipe-setup-error').textContent()).includes('测试启动失败'));
    startFail=false;holdStart=true;const beforeStart=countWrites('/api/pipeline/run');await page.locator('#pipe-start').click();
    check('启动期间锁定表单',await channelBox('凭证通道').isDisabled() && await page.locator('#pipe-start').isDisabled());
    await page.locator('#pipe-start').evaluate(el=>el.click());
    check('启动防重复提交',countWrites('/api/pipeline/run')===beforeStart+1);
    await releaseStart();await page.waitForFunction(()=>document.getElementById('pipe-modal').style.display==='none' && !pipeAction);
    check('启动后焦点转到运行状态',await page.locator('#pipe-state-title').evaluate(el=>el===document.activeElement));
    const submitted=writes.filter(w=>w.path==='/api/pipeline/run').at(-1).body;
    check('一次请求带上每个来源和它自己的订阅范围',submitted.channels.map(x=>x.channel).join('|')==='a|b' && sorted(submitted.channels[0].accounts)===sorted(accounts.a.slice(1)) && sorted(submitted.channels[1].accounts)===sorted(accounts.b));
    await page.locator('#tab-gallery').click();
    check('离开同步页仍保留后台运行提示',await page.locator('#nav-sync-dot').isVisible());
    finish();state.stages.extract={status:'error',detail:'失败 1',failures:[{title:'测试文档',reason:'模型暂不可用'}]};await refresh();
    await page.locator('#tab-pipe').click();await page.waitForFunction(()=>!document.getElementById('pipe-retry').hidden);
    check('部分失败不显示全成功并提供重试',(await page.locator('#pipe-state-title').textContent()).includes('需处理'));
    check('失败步骤不计为成功进度',await page.locator('#pipe-progress-label').textContent()==='5 / 6 完成' && await page.locator('#pipe-progress-fill').evaluate(el=>el.style.width==='83%'));
    const failedRun=copy(state);
    state.running=true;state.stages.wiki={status:'running',detail:'其余步骤仍在执行'};await refresh();
    check('本轮有失败但仍在执行时说明新建受阻原因',await page.locator('#pipe-run').isDisabled() && (await page.locator('#pipe-state-hint').textContent()).includes('本轮仍在执行'));
    await page.screenshot({path:'output/playwright/sync-failure-still-running.png'});
    state={running:false,stop_supported:true,stages:{},log:[],last_run:copy(failedRun)};await refresh();
    check('已结束的失败记录明确不阻止新建同步',await page.locator('#pipe-run').isEnabled() && (await page.locator('#pipe-state-hint').textContent()).includes('不影响新建同步'));
    await page.locator('#pipe-retry-help summary').click();
    check('可展开查看新建范围与自动重试边界',(await page.locator('#pipe-retry-help').textContent()).includes('下次完整同步再次尝试') && (await page.locator('#pipe-retry-help').textContent()).includes('上次的通道和范围'));
    await refresh();
    check('轮询保留重试说明展开状态',await page.locator('#pipe-retry-help').evaluate(el=>el.open));
    await page.screenshot({path:'output/playwright/sync-failure-guidance.png'});
    await page.setViewportSize({width:375,height:812});await settlePaint();
    check('手机失败提示与重试说明无横向溢出',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:'output/playwright/sync-failure-guidance-mobile.png',fullPage:true});
    await page.setViewportSize({width:1440,height:1100});
    await page.locator('#pipe-retry-help summary').click();
    await page.locator('#pipe-run').click();await page.locator('input[data-acct]').first().waitFor();
    check('上次失败后仍可正常配置并开始同步',await page.locator('#pipe-start').isEnabled());
    holdStart=false;const afterFailureStarts=countWrites('/api/pipeline/run');
    await page.locator('#pipe-start').click();await page.waitForFunction(()=>pipeSnapshot.running && !pipeAction);
    check('上次失败后新建同步完成提交并显示运行状态',countWrites('/api/pipeline/run')===afterFailureStarts+1 && !await page.locator('#pipe-modal').isVisible());
    state=copy(failedRun);await refresh();
    await page.locator('#pipe-retry').click();await page.waitForFunction(()=>!pipeAction && pipeSnapshot.running);
    check('重试有运行反馈且不重复启动',countWrites('/api/pipeline/retry-failed')===1 && await page.locator('#pipe-run').isDisabled());
    finish();await refresh();
    const beforeSchedule=countWrites('/api/pipeline/schedule');
    await page.locator('#sched-on').check();await page.locator('#sched-time').fill('08:45');
    await page.locator('#sched-channels').getByRole('checkbox',{name:'本机通道',exact:true}).check();
    check('修改定时字段不会隐式保存',countWrites('/api/pipeline/schedule')===beforeSchedule && (await page.locator('#sched-msg').textContent()).includes('未保存'));
    scheduleFail=true;await page.locator('#sched-save').click();await page.waitForFunction(()=>!schedSaving);
    check('定时保存失败保留编辑并允许重试',(await page.locator('#sched-msg').textContent()).includes('未保存') && await page.locator('#sched-time').inputValue()==='08:45' && !await page.locator('#sched-save').isDisabled());
    scheduleFail=false;await page.locator('#sched-save').click();await page.waitForFunction(()=>!schedSaving);
    check('定时整组设置一次保存并带上多个来源',JSON.stringify(writes.filter(w=>w.path==='/api/pipeline/schedule').at(-1).body)===JSON.stringify({enabled:true,time:'08:45',channels:['a','b']}) && await page.locator('#sched-save').isDisabled());
    await page.locator('#sched-channels').getByRole('checkbox',{name:'凭证通道',exact:true}).uncheck();
    await page.locator('#sched-channels').getByRole('checkbox',{name:'本机通道',exact:true}).uncheck();
    await page.locator('#sched-save').click();
    check('定时不允许一个来源都不选',(await page.locator('#sched-msg').textContent()).includes('至少选择一个') && countWrites('/api/pipeline/schedule')===beforeSchedule+2);
    await page.setViewportSize({width:375,height:812});
    check('手机同步页面无横向溢出',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    check('同一同步任务提示不堆叠',await page.locator('[data-toast-key=pipeline]').count()<=1);
    await page.locator('[data-toast-key=pipeline] .toast-x').click().catch(()=>{});
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:'output/playwright/sync-mobile.png'});
    await page.locator('#pipe-run').click();await page.locator('input[data-acct]').first().waitFor();await settleSetup();
    await page.locator('#pipe-range-btn').click();
    await page.locator('#pipe-custom-from').fill('2026-09-20');await page.locator('#pipe-custom-to').fill('2026-09-10');
    await page.getByRole('button',{name:'应用日期',exact:true}).click();
    check('日期倒置在表单内提示',(await page.locator('#pipe-custom-err').textContent()).includes('不能晚于'));
    check('未应用日期时禁用启动避免误用旧范围',await page.locator('#pipe-start').isDisabled());
    check('手机日期编辑无横向溢出',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    check('手机长表单标题和底部操作保持可见',await page.evaluate(()=>{const modal=document.querySelector('.pipe-setup').getBoundingClientRect(),title=document.querySelector('.pipe-setup h2').getBoundingClientRect(),footer=document.querySelector('.pipe-setup-footer').getBoundingClientRect();return title.top>=modal.top && footer.bottom<=modal.bottom+1;}));
    await page.screenshot({path:'output/playwright/sync-setup-mobile.png'});
    await page.keyboard.press('Escape');
    check('Escape先关闭日期选择并返回焦点',await page.locator('#pipe-range-btn').evaluate(el=>el===document.activeElement && el.getAttribute('aria-expanded')==='false'));
    await page.keyboard.press('Escape');
    check('关闭配置返回原入口焦点',await page.locator('#pipe-run').evaluate(el=>el===document.activeElement));
    channelFail=true;
    await page.goto(origin+'/?pipeline-test='+Date.now()+'#pipe');
    await page.waitForFunction(()=>pipeConnected && !pipeChannelsLoading);
    await page.locator('#pipe-run').click();
    await page.getByRole('button',{name:'重新读取来源',exact:true}).waitFor();
    check('通道请求失败显示具体原因和就地重试',await page.locator('#pipe-start').isDisabled() && (await page.locator('#pipe-setup-error').textContent()).includes('测试通道读取失败'));
    channelFail=false;
    await page.getByRole('button',{name:'重新读取来源',exact:true}).click();await page.locator('input[data-acct]').first().waitFor();
    check('通道重试恢复可用表单',await page.locator('#pipe-start').isEnabled());
    await page.getByRole('button',{name:'暂不执行',exact:true}).click();
    holdChannels=true;releaseChannels=null;
    await page.clock.install();clockInstalled=true;
    await page.goto(origin+'/?pipeline-test='+Date.now()+'#pipe');
    await page.waitForFunction(()=>pipeConnected);
    await page.locator('#pipe-run').click();
    await page.clock.fastForward(16000);
    await page.getByRole('button',{name:'重新读取来源',exact:true}).waitFor();
    check('通道请求挂起会超时退出等待并允许重试',(await page.locator('#pipe-setup-error').textContent()).includes('超时') && await page.locator('#pipe-start').isDisabled());
    holdChannels=false;if(releaseChannels)await releaseChannels();
    await page.getByRole('button',{name:'重新读取来源',exact:true}).click();await page.locator('input[data-acct]').first().waitFor();
    check('超时后重试不复用失效请求',await page.locator('#pipe-start').isEnabled());

    // 关闭窗口时被中止的那次读取不会再回来:重开必须重新发请求,不然永远停在
    // 「正在读取订阅…」上,开始按钮跟着一直禁用。
    await page.getByRole('button',{name:'暂不执行',exact:true}).click();
    const acctBefore = acctCalls.a;
    const waitAcctCalls = async n => { for (let i=0;i<60 && acctCalls.a<n;i++) await page.waitForTimeout(50); };
    holdAcct=true;
    await page.locator('#pipe-run').click();
    await waitAcctCalls(acctBefore+1);
    check('重开配置会重新读取订阅而不是沿用旧缓存',acctCalls.a===acctBefore+1 && (await page.locator('#pipe-acct-list').textContent()).includes('正在读取订阅'));
    await page.getByRole('button',{name:'暂不执行',exact:true}).click();
    holdAcct=false;for(const r of heldAcct.splice(0))r.abort().catch(()=>{});
    await page.locator('#pipe-run').click();await page.locator('input[data-acct]').first().waitFor();
    check('读取订阅途中关掉再打开不会卡在加载中',acctCalls.a===acctBefore+2 && await page.locator('#pipe-start').isEnabled());
    // 关着的这段时间里订阅被增删过(在别处添加/停用/移除了号):重开要照新的来,
    // 原来的勾选按新列表过滤——没了的不留,新来的也不替用户勾上。
    await page.getByRole('checkbox',{name:'公众号甲',exact:true}).uncheck();
    await page.getByRole('button',{name:'暂不执行',exact:true}).click();
    accounts.a=['公众号甲','公众号乙','公众号丁'];
    await page.locator('#pipe-run').click();
    await page.getByRole('checkbox',{name:'公众号丁',exact:true}).waitFor();
    check('重开反映订阅增删并按新列表过滤原勾选',
      await page.getByRole('checkbox',{name:'公众号丙 "<测试>',exact:true}).count()===0
      && !await page.getByRole('checkbox',{name:'公众号甲',exact:true}).isChecked()
      && await page.getByRole('checkbox',{name:'公众号乙',exact:true}).isChecked()
      && !await page.getByRole('checkbox',{name:'公众号丁',exact:true}).isChecked());
    check('无未处理前端异常',errors.length===0);
    return {passed:checks.length,checks,errors,isolatedWrites:writes};
  } catch (e) {
    // 超时之类的失败不带 check 名,报告里看不出跑到哪一步了
    e.message = `${e.message}\n已通过 ${checks.length} 项,最后通过:${checks.at(-1) || '(无)'}`
      + (errors.length ? `\n页面异常:${errors.join(' | ')}` : '');
    throw e;
  } finally {
    if(clockInstalled)await page.clock.resume();
    await page.evaluate(()=>{invalidatePipeStatus();});
    page.off('pageerror',onError);
    await page.unroute('**/api/**',handler);
  }
}
