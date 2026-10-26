import assert from 'node:assert/strict';
import { attachCdp, sleep } from './pipe_history_cdp.mjs';

const base = process.argv[2];
const { proc, ws, send, evalJs, until, clickAt, pageErrors } = await attachCdp({
  profile:'kb-markdown-links-', extraArgs:['--disable-popup-blocking'],
});
const passed = [];
const check = async (name, expression) => {
  assert.ok(await evalJs(expression), name);
  passed.push(name);
};
const targets = async () => (await send('Target.getTargets')).result.targetInfos;
const clickNewTab = async (selector, name, { invalid = false } = {}) => {
  const before = new Set((await targets()).map(target => target.targetId));
  const expected = await evalJs(`document.querySelector(${JSON.stringify(selector)}).href`);
  assert.ok(await clickAt(selector), name + ': 真实鼠标命中');
  let opened;
  const deadline = Date.now() + 6000;
  while (!opened && Date.now() < deadline) {
    opened = (await targets()).find(target => target.type === 'page' && !before.has(target.targetId)
      && (invalid || target.url === expected));
    if (!opened) await sleep(100);
  }
  assert.ok(opened, name + ': 新标签到达目标');
  try {
    await check(name, `location.href === window.policyOriginalUrl
      && window.policyMemory === '问题、回答和筛选仍在'
      && $('policy-cases').isConnected`);
  } finally {
    await send('Target.closeTarget', { targetId: opened.targetId });
  }
};
const clickFragment = async (selector, name, targetSelector = null) => {
  const before = await evalJs(`({ url:location.href, filters:JSON.stringify(gState) })`);
  const pages = (await targets()).filter(target => target.type === 'page').length;
  assert.ok(await clickAt(selector), name + ': 真实鼠标命中');
  await sleep(100);
  const after = await evalJs(`({ url:location.href, filters:JSON.stringify(gState) })`);
  assert.deepEqual(after, before, name + ': 页签和筛选不变');
  assert.equal((await targets()).filter(target => target.type === 'page').length, pages);
  if (targetSelector) await check(name, `document.activeElement.matches(${JSON.stringify(targetSelector)})`);
  else passed.push(name);
};

try {
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: base + '/#ask' });
  assert.ok(await until(`typeof renderMsg === 'function' && typeof renderToolEntries === 'function'
    && typeof renderSourceBody === 'function' && typeof DOMPurify !== 'undefined'
    && typeof workspaceLocated !== 'undefined' && workspaceLocated`));
  await evalJs(`(() => {
    window.policyOriginalUrl = location.href;
    window.policyMemory = '问题、回答和筛选仍在';
    gState.q = 'agent'; gState.kind = 'project';
    window.policyMarkdown = '[打开目标](/static/favicon.svg?markdown-link-policy)\\n\\n'
      + '## 方法细节\\n\\n[页内](#方法细节) [同地址](' + location.origin + '/#方法细节) '
      + '[无落点](#missing) [编码错误](#%E0%A4%A)';
    const host = document.createElement('section');
    host.id = 'policy-cases';
    host.style.cssText = 'position:fixed;inset:0;z-index:99999;background:var(--bg);padding:30px;overflow:auto';
    document.body.append(host);
    const add = (id, html) => {
      const card = document.createElement('div');
      card.id = id;
      card.innerHTML = html;
      host.append(card);
    };
    add('policy-answer', renderMsg({ role:'assistant', content:policyMarkdown }));
    add('policy-restored', pendingHtml('fixture', policyMarkdown));
    add('policy-stream', pendingHtml('fixture', ''));
    $('policy-restored').querySelector('#chat-pending-body').id = 'policy-restored-body';
    inflight = { partial:policyMarkdown };
    paintPending();
    inflight = null;
    renderToolEntries({ name:'fixture', vault:'fixture', entries:[{
      body:policyMarkdown, path:'fixture.md', article_title:'fixture', source:'rss'
    }] });
    add('policy-entry', $('td-entries').innerHTML);
  })()`);
  for (const [id, name] of [
    ['policy-answer', '完整回答的正文链接保留知识库'],
    ['policy-restored', '恢复的进行中回答保留知识库'],
    ['policy-stream', '问答增量重绘保留知识库'],
    ['policy-entry', '工具条目正文保留知识库'],
  ]) await clickNewTab('#' + id + ' a', name);
  for (const id of ['policy-answer', 'policy-restored', 'policy-stream', 'policy-entry']) {
    await clickFragment('#' + id + ' a[href^="#"]', id + ': 片段只定位当前正文', '#' + id + ' h2');
  }
  await clickFragment('#policy-answer a[href^="http"]', '同地址片段不改工作台 hash', '#policy-answer h2');
  await clickFragment('#policy-answer a[href="#missing"]', '没有落点时安全停留');
  await clickFragment('#policy-answer a[href="#%E0%A4%A"]', '片段解码失败时安全停留');
  await evalJs(`(() => {
    const host = document.createElement('div');
    host.id = 'policy-invalid';
    host.innerHTML = renderMsg({ role:'assistant', content:'<a href="http://[">坏地址</a>' });
    $('policy-cases').append(host);
  })()`);
  await clickNewTab('#policy-invalid a', '坏地址也不能导航走知识库', { invalid:true });
  await evalJs(`renderDailyReportBody({ date:'fixture', body:policyMarkdown });
    $('policy-cases').append($('daily-reader-body'));`);
  await clickFragment('#daily-reader-body a[href^="#"]', '日报片段不改工作台 hash', '#daily-reader-body h2');

  await check('HTTP(S)、相对路径和协议相对路径共享打开策略', `(() => {
    const host = document.createElement('div');
    host.innerHTML = mdHtml('[外站](https://example.org/a) [同源](/doc/a.md) '
      + '[相对](static/favicon.svg) [无协议](//example.org/b) '
      + '<a href="/doc/b.md" target="_top" rel="nofollow">原始 HTML</a>');
    return host.querySelectorAll('a').length === 5 && [...host.querySelectorAll('a')].every(link =>
      link.target === '_blank' && link.relList.contains('noopener') && link.relList.contains('noreferrer'))
      && host.querySelector('a[rel~="nofollow"]');
  })()`);
  await check('页内锚点和已有协议入口不改写', `(() => {
    const host = document.createElement('div');
    host.innerHTML = mdHtml('[片段](#policy-anchor) [本页](' + location.origin + '/#policy-anchor) '
      + '[邮件](mailto:reader@example.org) [电话](tel:12345)');
    const obsidian = document.createElement('a');
    obsidian.href = 'obsidian://open?vault=fixture&file=note';
    host.append(obsidian);
    const before = [...host.querySelectorAll('a')].map(link => link.getAttribute('href'));
    prepareMarkdownLinks(host);
    return host.querySelectorAll('a').length === 5 && [...host.querySelectorAll('a')].every((link, index) =>
      !link.target && link.getAttribute('href') === before[index]);
  })()`);
  await check('日报补出的出处与订阅正文复用相同策略', `(() => {
    const host = document.createElement('div');
    host.innerHTML = mdHtml('出处 [1]。\\n\\n## 素材清单\\n\\n1. [文章](/doc/a.md)');
    enhanceDailyBody(host);
    renderSourceBody('[站内](/doc/a.md) [片段](#chapter) [邮件](mailto:reader@example.org)');
    const sourceLinks = [...$('source-reader-body').querySelectorAll('a')];
    return host.querySelector('a.daily-ref')?.target === '_blank'
      && sourceLinks.length === 3 && sourceLinks[0].target === '_blank'
      && !sourceLinks[1].target && !sourceLinks[2].target;
  })()`);
  await check('链接定义上下文和危险内容净化不变', `(() => {
    const host = document.createElement('div');
    host.innerHTML = mdHtml('[引用][ref]', '[ref]: /static/favicon.svg?x=1&y=2');
    const link = host.querySelector('a');
    if (link?.getAttribute('href') !== '/static/favicon.svg?x=1&y=2' || link.target !== '_blank') return false;
    host.innerHTML = mdHtml('<a href="javascript:window.policyPayload=1">危险</a>'
      + '<img onerror="window.policyPayload=1"><script>window.policyPayload=1</script>');
    return !host.querySelector('a[href], [onerror], script') && !window.policyPayload;
  })()`);
  await check('净化器缺失时仍失败关闭', `(() => {
    const purifier = window.DOMPurify;
    try {
      window.DOMPurify = null;
      const host = document.createElement('div');
      host.innerHTML = mdHtml('<a href="/doc/a.md">纯文本</a>');
      return !host.querySelector('a') && host.textContent.includes('<a');
    } finally { window.DOMPurify = purifier; }
  })()`);

  await evalJs(`(() => {
    window.policyFetch = window.fetch;
    window.fetch = (url, options) => url === '/api/ask/stream'
      ? Promise.resolve(new Response(new ReadableStream({ start(controller) {
        const encoder = new TextEncoder();
        controller.enqueue(encoder.encode(JSON.stringify({ type:'delta', text:policyMarkdown }) + '\\n'));
        window.policyFinishStream = () => {
          controller.enqueue(encoder.encode(JSON.stringify({ type:'done', conversation_id:1,
            user_message_id:1, message_id:2 }) + '\\n'));
          controller.close();
        };
      } }))) : policyFetch(url, options);
    toolMeta = { name:'fixture' };
    window.policyToolRun = doSendToolChat('展示链接');
  })()`);
  assert.ok(await until(`$('td-pending-body')?.querySelector('a')`));
  await check('工具流式回答在完成前就应用链接策略', `(() => {
    const link = $('td-pending-body').querySelector('a');
    return link.target === '_blank' && link.relList.contains('noopener');
  })()`);
  await evalJs(`$('policy-cases').append($('td-chat-msgs'))`);
  await clickNewTab('#td-pending-body a', '工具增量重绘保留知识库');
  await clickFragment('#td-pending-body a[href^="#"]', '图鉴流式片段在完成前安全定位', '#td-pending-body h2');
  await evalJs(`policyFinishStream(); policyToolRun.finally(() => { window.fetch = policyFetch; })`);
  assert.deepEqual(pageErrors, [], '没有页面 JavaScript 异常');
  console.log(JSON.stringify({ passed, errors:pageErrors }));
} finally {
  ws.close();
  proc.kill();
}
