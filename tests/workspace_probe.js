// Run against a local preview with Playwright CLI, always in a headless session:
// npx --yes --package @playwright/cli playwright-cli -s=workspace open http://127.0.0.1:18877
// npx --yes --package @playwright/cli playwright-cli -s=workspace run-code --filename tests/workspace_probe.js
// API and document routes are fully isolated; no production writes or LLM calls.
async page => {
  const checks = [], errors = [], writes = [];
  const check = (name, value) => { if (!value) throw new Error(name); checks.push(name); };
  const onError = e => errors.push(e.message);
  page.on('pageerror', onError);
  const origin = page.url().match(/^https?:\/\/[^/]+/)[0];
  const tools = Array.from({ length: 90 }, (_, i) => ({
    norm: 'tool-' + i, name: ['Alpha', 'Beta', 'Gamma'][i] || 'Tool ' + i,
    kind: i % 2 ? 'model' : 'project', summary: '用于探索知识的工具 ' + i,
    n: 90 - i, accounts: 2, first: '2026-01-01', latest: '2026-09-26',
    sources: [i % 2 ? 'dr-video-projects' : 'dr-gzh-projects'],
    topics: ['主题' + i % 12], url: 'https://example.com/tool-' + i,
  }));
  const gallery = { tools, kinds: { project: 45, model: 45 }, total_entries: 180,
    topics: Array.from({ length: 12 }, (_, i) => ({ value: '主题' + i, n: 8 })) };
  const hit = name => ({ path: 'test.md', title: name, vault: 'test', account: '测试来源',
    published: '2026-09-26', source: 'dr-gzh', matched_by: ['fts'], entities: ['Alpha'],
    summary: '检索回归验证', best_chunk: '测试正文', url: '' });
  // 来源目录真给数据,否则 initSourceModules 走的是错误分支,导轨上那几个
  // 动态入口(以及它们的 .nav-text)压根不会被建出来、测不到。
  const intakeModules = [
    {id:'social',label:'大V观点',description:'关注的人,此刻的想法。',note:'测试来源',manager:'feeds',icon:'chat'},
    {id:'rss',label:'RSS',description:'订阅的站点与博客。',note:'测试来源',manager:'feeds',icon:'rss'},
    {id:'gzh',label:'公众号',description:'不由这套面板管。',note:'测试来源',manager:'wechat',icon:'chat'},
  ];
  let galleryFail = false, searchFail = false, delayedSearch = null, holdModules = null;
  let askHandler = null, askCount = 0, favorite = false;
  const respond = (route, data, status = 200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  const handler = async route => {
    const request = route.request();
    const [path, query = ''] = request.url().slice(origin.length).split('?');
    const q = decodeURIComponent(query.match(/(?:^|&)q=([^&]*)/)?.[1] || '');
    if (request.method() !== 'GET') writes.push(path);
    if (path === '/api/projects') return respond(route, galleryFail ? {detail:'测试断线'} : gallery, galleryFail ? 503 : 200);
    if (path.startsWith('/api/projects/')) {
      const tool = tools.find(t => t.norm === decodeURIComponent(path.split('/').at(-1)));
      return respond(route, { ...tool, vault:'test', urls:['javascript:alert(1)'], entries: [
        { path:'test.md', article_title:'工具使用记录', account:'测试来源', published:'2026-09-26',
          source:'dr-gzh-projects', body:'## 测试正文\n\n**知识来源**与使用方法。<img src=x onerror="window.injected=true">' },
      ] });
    }
    if (path === '/api/search') {
      if (searchFail) return respond(route,{detail:'测试服务暂不可用'},503);
      if (q === 'slow') {
        delayedSearch = () => respond(route,{hits:[hit('过期结果')]}).catch(()=>{});
        return;
      }
      return respond(route, {hits: q === 'none' ? [] : [hit('最新结果')]});
    }
    if (path === '/api/ask/stream') {
      askCount++;
      if (askHandler) return askHandler(route);
      const frames = [{type:'meta',refs:[]},{type:'delta',text:'验证回答 **已完成**'},{type:'done',conversation_id:42,refs:[]}];
      return route.fulfill({contentType:'application/x-ndjson',body:frames.map(x=>JSON.stringify(x)).join('\n')+'\n'});
    }
    if (path === '/api/favorites/toggle') { favorite = !favorite; return respond(route,{favorited:favorite}); }
    if (path === '/api/intake/modules') {
      if (holdModules) await holdModules;   // 扣住它,验证名字浮层不跟着一起等
      return respond(route, {modules: intakeModules});
    }
    const fixtures = {
      '/api/stats': {documents:180,vectors:300,entities:90,extracted:180},
      '/api/facets': {sources:[{value:'dr-gzh',n:90}],accounts:[],topics:[{value:'topic/知识',n:5}],entities:[{value:'Alpha',n:5}]},
      '/api/favorites': {doc_paths:favorite?['test.md']:[],favorites:[],counts:{inbox:0,done:0,archived:0},stale:0},
      '/api/favorites/settings': {model:'openai:test'}, '/api/favorites/triage': {running:false},
      '/api/models': {models:[{key:'openai:test',label:'Test',enabled:true}],default:'openai:test'},
      '/api/keys': {keys:{anthropic:{set:false},openai:{set:false}}},
      '/api/mcp/status': {config:'{}',command:'test command',tools:[],cli_available:false},
      '/api/pipeline/channels': {default:'test',channels:[{id:'test',label:'测试来源'}]},
      '/api/pipeline/schedule': {enabled:false,time:'07:30'},
      '/api/pipeline/status': {running:false,stages:{},source:{online:false},log:[]},
      '/api/conversations': {conversations:[]}, '/api/stages': {stages:[],providers:[]},
    };
    return respond(route,fixtures[path] || {});
  };
  await page.route('**/api/**', handler);
  await page.route('**/doc/**', route => route.fulfill({contentType:'text/html',body:'<html><body><h1>测试文章</h1><p id="content">正文内容</p><button>阅读按钮</button></body></html>'}));
  try {
    await page.setViewportSize({width:1440,height:1000});
    await page.emulateMedia({reducedMotion:'reduce'});
    await page.goto(origin + '/?workspace-test=' + Date.now() + '#gallery');
    await page.locator('.gcard').first().waitFor();
    check('首屏增量渲染', await page.locator('.gcard').count() === 48);
    check('桌面侧栏宽度跟 --sidebar-width 一致', await page.locator('#sidebar').evaluate(el=>el.getBoundingClientRect().width===parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width'))));
    check('桌面不再显示顶部横条', !await page.locator('#topbar').isVisible());
    check('内容区域紧随窄侧栏', await page.locator('#main-content').evaluate(el=>el.getBoundingClientRect().left===parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width'))));
    check('三个图谱入口直接可达', await page.locator('.explore-links a:visible').count()===3);
    check('导轨上不再有那个只剩放大镜的查找框', await page.locator('.quick-search').count() === 0);
    await page.keyboard.press('Control+k');
    check('快捷键仍能定位搜索输入框', await page.locator('#g-q').evaluate(el=>el===document.activeElement));
    check('三组入口各有一道分组记号', await page.evaluate(() => {
      const marked = [...document.querySelectorAll('#sidebar .nav-section')]
        .filter(el => el.getClientRects().length && getComputedStyle(el, '::before').content !== 'none');
      return marked.length === 2;   // 第一组在品牌下面,不需要记号
    }));
    check('外链图谱带可见的新标签角标', await page.evaluate(() =>
      [...document.querySelectorAll('.explore-links a > .nav-ext')].every(el => el.getClientRects().length)));
    check('导轨上没有常驻文字标签', await page.evaluate(() =>
      [...document.querySelectorAll('#sidebar .nav-text')].every(el => el.getBoundingClientRect().width <= 1)));
    await page.locator('#tab-fav').hover();
    check('悬停把名字推到栏外且不被裁掉', await page.evaluate(() => {
      const tip = document.getElementById('nav-tip'), rail = document.getElementById('sidebar');
      const t = tip.getBoundingClientRect(), r = rail.getBoundingClientRect();
      return !tip.hidden && tip.textContent === '我的收藏' && t.left >= r.right
        && document.elementFromPoint(t.left + t.width / 2, t.top + t.height / 2) !== null;
    }));
    await page.locator('h1').first().hover();
    check('鼠标离开导轨后浮层收起', await page.evaluate(() => document.getElementById('nav-tip').hidden));
    await page.locator('.brand-lockup').focus();
    await page.keyboard.press('Tab');
    check('键盘聚焦同样给出名字', await page.evaluate(() =>
      !document.getElementById('nav-tip').hidden && document.getElementById('nav-tip').textContent === '工具图鉴'));
    await page.locator('#g-q').focus();
    check('焦点离开导轨后浮层收起', await page.evaluate(() => document.getElementById('nav-tip').hidden));
    await page.locator('#tab-gallery').click();   // 鼠标点完焦点也留在按钮上
    await page.locator('h1').first().hover();
    check('点过的入口在鼠标挪开后不留浮层', await page.evaluate(() => document.getElementById('nav-tip').hidden));
    await page.locator('.workspace-settings summary').click();
    // details 的 toggle 是排进任务队列才发的,所以这里等一拍,不当场读
    check('设置菜单展开后名字浮层让开', await page.waitForFunction(
      () => document.getElementById('nav-tip').hidden, null, {timeout:2000}).then(() => true, () => false));
    check('设置浮层未被窄侧栏裁切', await page.locator('#keys-btn').evaluate(el=>{
      const r=el.getBoundingClientRect();
      const rail=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width'));
      return r.left>=rail && document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===el;
    }));
    await page.keyboard.press('Escape');
    check('设置菜单 Escape 关闭并返回焦点', await page.locator('.workspace-settings').evaluate(el=>!el.open && document.activeElement===el.querySelector('summary')));
    await page.locator('.workspace-settings summary').click();
    await page.locator('h1').first().click();
    check('点击内容区收起菜单', await page.locator('.workspace-settings').evaluate(el=>!el.open));
    await page.evaluate(()=>window.scrollTo(0,1100));
    await page.locator('#tab-search').click();
    await page.locator('#tab-gallery').click();
    check('跨面板保留浏览位置', await page.evaluate(()=>window.scrollY>=1000));
    await page.locator('#g-kinds [data-kind=model]').click();
    check('类型筛选', await page.locator('.gcard').count() === 45);
    await page.locator('#g-src').selectOption('gzh');
    check('组合筛选空态可操作', await page.locator('#g-empty').isVisible());
    await page.locator('#g-empty button').click();
    await page.locator('#g-q').fill('Alpha');
    await page.waitForFunction(()=>document.querySelectorAll('.gcard').length===1);
    check('关键词写入地址', page.url().split('#')[1].includes('q=Alpha'));
    await page.reload();
    await page.locator('.gcard').first().waitFor();
    check('刷新还原筛选', await page.locator('#g-q').inputValue() === 'Alpha' && await page.locator('.gcard').count() === 1);
    await page.locator('#tab-search').click();
    await page.goBack();
    await page.locator('#g-q').waitFor();
    check('后退恢复所在面板', await page.locator('#tab-gallery').getAttribute('aria-current') === 'page');
    await page.locator('#g-reset').click();
    await page.locator('#g-topic-toggle').click();
    await page.locator('.gtopic[data-topic="主题10"]').click();
    await page.locator('#g-topic-toggle').click();
    check('收起后保留选中的主题', await page.locator('.gtopic[data-topic="主题10"]').isVisible());
    await page.locator('#g-reset').click();
    await page.locator('#g-view-list').click();
    await page.reload();
    await page.locator('.gcard').first().waitFor();
    check('列表偏好在刷新后保留', await page.locator('#g-grid').evaluate(el=>el.classList.contains('list-view')));
    await page.locator('#g-view-grid').click();
    await page.locator('#g-sort').selectOption('n');
    check('按提及数排序', (await page.locator('.gname').first().innerText()) === 'Alpha');
    await page.keyboard.press('/');
    check('斜杠聚焦图鉴搜索', await page.locator('#g-q').evaluate(el=>el===document.activeElement));
    await page.locator('#g-q').fill('zz-no-match');
    await page.locator('#g-empty').waitFor();
    await page.locator('#g-empty button').click();
    await page.locator('.gcard').first().click();
    await page.locator('.td-entry').waitFor();
    check('外部内容净化且链接安全', await page.evaluate(()=>!window.injected && !document.querySelector('#td-hero a[href^="javascript:"]')));
    await page.keyboard.press('Shift+Tab');
    check('抽屉键盘焦点循环', await page.locator('#td-chat-send').evaluate(el=>el===document.activeElement));
    await page.getByRole('link',{name:'在阅读器打开',exact:true}).click();
    await page.locator('#reader-frame').contentFrame().getByText('正文内容').click();
    await page.keyboard.press('Escape');
    check('阅读正文时 Escape 逐层关闭', !await page.locator('#reader').isVisible() && await page.locator('#tool-drawer').isVisible());
    await page.keyboard.press('Escape');
    check('关闭后返回原卡片焦点', await page.evaluate(()=>document.activeElement.classList.contains('gcard')));
    await page.locator('.gcard').first().click();
    await page.locator('#td-chat-input').fill('解释这个工具');
    await page.locator('#td-chat-input').press('Enter');
    await page.locator('#td-chat-msgs strong').waitFor();
    check('工具问答渲染流式 Markdown', await page.locator('#td-chat-msgs').innerText().then(s=>s.includes('已完成')));
    let delayedAsk = null;
    askHandler = route => { delayedAsk = route; };
    await page.locator('#td-chat-input').fill('旧工具问题');
    await Promise.all([page.waitForRequest(r=>r.url().includes('/api/ask/stream')),page.locator('#td-chat-send').click()]);
    await page.keyboard.press('Escape');
    await page.locator('.gcard').nth(1).click();
    await page.locator('#td-name').filter({hasText:'Beta'}).waitFor();
    askHandler = null;
    await page.locator('#td-chat-input').fill('新工具问题');
    await page.locator('#td-chat-send').click();
    await page.locator('#td-chat-msgs strong').waitFor();
    if (delayedAsk) await respond(delayedAsk,{}).catch(()=>{});
    check('切换工具隔离旧问答', await page.locator('#td-chat-msgs').innerText().then(s=>s.includes('新工具问题')&&!s.includes('旧工具问题')));
    await page.keyboard.press('Escape');
    await page.locator('#tab-search').click();
    await page.locator('#q').fill('slow');
    await Promise.all([page.waitForRequest(r=>r.url().includes('q=slow')),page.locator('#search-submit').click()]);
    check('慢搜索请求已发出', !!delayedSearch);
    await page.locator('#q').fill('fast');
    await page.locator('#search-submit').click();
    await page.getByRole('link',{name:'最新结果',exact:true}).waitFor();
    if (delayedSearch) await delayedSearch();
    check('旧搜索不会覆盖新搜索', !await page.getByRole('link',{name:'过期结果',exact:true}).count());
    await page.locator('.hit .favbtn').click();
    await page.getByRole('button',{name:'★ 已收藏',exact:true}).waitFor();
    check('收藏即时反馈', favorite);
    searchFail = true;
    await page.locator('#search-submit').click();
    await page.getByRole('button',{name:'重试搜索',exact:true}).waitFor();
    searchFail = false;
    await page.getByRole('button',{name:'重试搜索',exact:true}).click();
    await page.getByRole('link',{name:'最新结果',exact:true}).waitFor();
    check('搜索失败可恢复', await page.locator('#results').getAttribute('aria-busy') === 'false');
    await page.locator('#q').fill('');
    await page.locator('#f-source').selectOption('dr-gzh');
    await page.getByRole('link',{name:'最新结果',exact:true}).waitFor();
    check('仅来源也能检索', await page.locator('#results .hit').count()===1);
    await page.locator('#tab-ask').click();
    const before = askCount;
    await page.locator('#chat-input').fill('中文候选');
    await page.locator('#chat-input').dispatchEvent('keydown',{key:'Enter',isComposing:true,bubbles:true});
    check('中文输入确认不误发送', askCount===before && await page.locator('#chat-input').inputValue()==='中文候选');
    await page.locator('#chat-input').press('Shift+Enter');
    check('Shift Enter 保留换行', (await page.locator('#chat-input').inputValue()).includes('\n'));
    await page.locator('#chat-input').press('Enter');
    await page.locator('#chat-msgs strong').waitFor();
    check('主问答发送成功', askCount===before+1);
    await page.locator('#chat-new').click();
    askHandler = () => {};
    await page.locator('#chat-input').fill('停止测试');
    await page.locator('#chat-send').click();
    await page.getByRole('button',{name:'■ 停止',exact:true}).click();
    await page.getByRole('button',{name:'发送',exact:true}).waitFor();
    check('停止生成可恢复发送', !await page.locator('#chat-send').isDisabled());
    askHandler = null;
    await page.locator('.workspace-settings summary').click();
    await page.locator('#keys-btn').click();
    await page.keyboard.press('Escape');
    check('设置弹窗关闭返回入口', await page.locator('#keys-btn').evaluate(el=>el===document.activeElement));
    await page.setViewportSize({width:375,height:812});
    for (const tab of ['gallery','search','ask','fav','pipe']) {
      await page.getByRole('button',{name:'打开导航',exact:true}).click();
      await page.locator('#tab-' + tab).click();
      check('375px ' + tab + ' 无横向溢出', await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      check(tab + ' 导航关闭且不可聚焦', await page.locator('#sidebar').evaluate(el=>el.inert));
    }
    await page.getByRole('button',{name:'打开导航',exact:true}).click();
    await page.locator('.workspace-settings summary').click();
    await page.locator('#keys-btn').click();
    check('手机设置显示于导航上层', await page.locator('#keys-modal').evaluate(el=>!el.inert && getComputedStyle(el).display!=='none'));
    await page.keyboard.press('Escape');
    /* ── 名字只剩浮层这一条路了,它不许被任何一个和它无关的请求拖住 ──
       扣住来源目录(线上就是 pipeJson 的 15 秒超时那一段):工具卡片已经能翻了,
       导轨上每一个入口都得说得出自己叫什么;目录回来之后,原地一直没挪过的那次
       悬停也得还在——那时候不会再有第二个 pointerover 把名字带回来。 */
    await page.setViewportSize({width:1440,height:1000});
    let releaseModules = () => {};
    holdModules = new Promise(resolve => { releaseModules = resolve; });
    await page.goto(origin + '/?workspace-test=' + Date.now() + '#gallery');
    await page.locator('.gcard').first().waitFor();
    await page.locator('#tab-fav').hover();
    check('来源目录还没回来,导轨照样报得出名字', await page.evaluate(() => {
      const tip = document.getElementById('nav-tip');
      return !document.getElementById('tab-rss') && !tip.hidden && tip.textContent === '我的收藏';
    }));
    releaseModules(); holdModules = null;
    await page.locator('#tab-rss').waitFor();
    check('目录回来后原地未挪的悬停仍有名字且贴着那个入口', await page.evaluate(() => {
      const tip = document.getElementById('nav-tip');
      const t = tip.getBoundingClientRect(), item = document.getElementById('tab-fav').getBoundingClientRect();
      return !tip.hidden && tip.textContent === '我的收藏'
        && Math.abs((t.top + t.height / 2) - (item.top + item.height / 2)) <= 1;
    }));
    check('只有 feeds 那几个来源进导轨', await page.evaluate(() =>
      !!document.getElementById('tab-social') && !document.getElementById('tab-gzh')));
    await page.locator('#tab-rss').hover();
    check('接口回来后插进去的来源入口同样有名字', await page.evaluate(() => {
      const tip = document.getElementById('nav-tip');
      return !tip.hidden && tip.textContent === 'RSS'
        && document.querySelector('#tab-rss .nav-text').getBoundingClientRect().width <= 1;
    }));
    /* 矮窗口里 Tab 到露在视野外的入口,浏览器会自己滚侧栏——锚点没变、只是位置变了,
       那时候把名字清掉就再也没有第二个 focusin 能把它带回来。 */
    await page.locator('h1').first().hover();
    await page.setViewportSize({width:1440,height:540});
    await page.locator('.brand-lockup').focus();
    for (let i = 0; i < 24; i++) {
      if (await page.evaluate(() => !!document.activeElement?.closest?.('.nav-dropdown'))) break;
      await page.keyboard.press('Tab');
    }
    // 自动滚动是焦点落定之后才发生的,它那个 scroll 事件要等到下一次渲染更新才派发——
    // 按完键当场读的话,清掉名字的那一下还没发生,缺陷看不出来
    await page.waitForFunction(() => new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)))));
    check('矮窗口里键盘走到被滚进视野的入口仍有名字', await page.evaluate(() => {
      const tip = document.getElementById('nav-tip');
      return document.getElementById('sidebar').scrollTop > 0 && !tip.hidden && tip.textContent === '设置';
    }));
    await page.setViewportSize({width:1440,height:1000});
    galleryFail = true;
    await page.goto(origin + '/?workspace-test=' + Date.now() + '#gallery');
    await page.getByRole('button',{name:'重新加载',exact:true}).waitFor();
    galleryFail = false;
    await page.getByRole('button',{name:'重新加载',exact:true}).click();
    await page.locator('.gcard').first().waitFor();
    check('图鉴加载失败可重试', await page.locator('#g-grid').getAttribute('aria-busy')==='false');
    check('无未处理前端异常', errors.length===0);
    check('无同步或设置写入', writes.every(p=>['/api/ask/stream','/api/favorites/toggle'].includes(p)));
    return {passed:checks.length,checks,errors,isolatedWrites:writes};
  } finally {
    await page.evaluate(()=>invalidatePipeStatus());
    page.off('pageerror',onError);
    await page.unroute('**/api/**',handler);
    await page.unroute('**/doc/**');
  }
}
