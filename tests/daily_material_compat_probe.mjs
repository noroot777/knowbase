import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { attachCdp } from './pipe_history_cdp.mjs';

const base = process.argv[2];
const { proc, ws, send, evalJs, until, clickAt, pageErrors } = await attachCdp();
const passed = [];
const check = async (name, expression) => {
  assert.ok(await evalJs(expression), name);
  passed.push(name);
};
const screenshot = async name => {
  const directory = process.env.KB_MATERIAL_SCREENSHOTS;
  if (!directory) return;
  mkdirSync(directory, { recursive: true });
  const image = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  writeFileSync(`${directory}/daily-materials-compat-${name}.png`, Buffer.from(image.result.data, 'base64'));
};
const selectQuote = async (quote, selector = '#daily-reader-body') => evalJs(`(() => {
  const host = document.querySelector(${JSON.stringify(selector)});
  const index = snipTextIndex(host);
  const quote = ${JSON.stringify(quote)};
  const start = index.text.indexOf(quote);
  if (start < 0) throw new Error('找不到测试选区: ' + quote);
  const range = snipOffsetRange(index, start, start + quote.length);
  range.startContainer.parentElement.scrollIntoView({ block: 'center', behavior: 'instant' });
  const selection = getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  showSnipBtn();
  const saved = snipSpec({ el: host }, range, { root: host });
  return { q: saved.quote, p: saved.prefix, s: saved.suffix,
    u: saved.unique ? 1 : 0, e: saved.locatable ? 1 : 0 };
})()`);
const captureLegacy = async quote => {
  await evalJs(`(async () => {
    const report = await (await fetch('/api/daily/reports/2026-10-17')).json();
    const body = setDailyBody(mdHtml(report.body.replace(/^\\s*#\\s+[^\\n]*\\n+(?:>\\s*覆盖[^\\n]*\\n)?/, '')), report.date);
    enhanceDailyBody(body);
  })()`);
  const spec = await selectQuote(quote);
  assert.equal(spec.e, 1, '升级前采集锚点可严格定位');
  assert.ok(await until(`$('snipbtn') && !$('snipbtn').hidden`));
  assert.ok(await clickAt('#snipbtn'), '真实点击收藏按钮');
  const favoriteExpression = `(async () => (await (await fetch('/api/favorites')).json())
    .favorites.find(item => item.quote === ${JSON.stringify(quote)}))()`;
  assert.ok(await until(favoriteExpression));
  const favorite = await evalJs(favoriteExpression);
  assert.ok(await clickAt('#tab-fav'));
  const button = `#fav-list [data-fav-card="${favorite.id}"] .fav-jump`;
  assert.ok(await until(`document.querySelector(${JSON.stringify(button)})`));
  assert.ok(await clickAt(button), '真实点击旧收藏回跳');
  assert.ok(await until(`(() => {
    const highlight = CSS.highlights && CSS.highlights.get('kb-quote');
    return highlight && [...highlight].some(range => range.startContainer.isConnected
      && range.toString() === ${JSON.stringify(quote)})
      && document.querySelectorAll('.daily-materials > li').length === 12;
  })()`));
  return spec;
};

try {
  await send('Page.navigate', { url: `${base}/#daily` });
  assert.ok(await until(`document.querySelectorAll('.daily-materials > li').length === 12`));
  await check('普通阅读没有收藏兼容正文', `!document.querySelector('.daily-legacy-context')`);
  await captureLegacy('Frontier Slides，超快 ;)');
  await check('升级前短标题收藏可高亮且没有错误提示', `(() => {
    const range = [...CSS.highlights.get('kb-quote')][0];
    return range.toString() === 'Frontier Slides，超快 ;)'
      && !document.body.innerText.includes('没找到这段收藏');
  })()`);
  await check('旧收藏回跳不替换简洁清单，兼容正文可以收起', `(() => {
    const context = document.querySelector('.daily-legacy-context');
    return document.querySelectorAll('.daily-materials > li').length === 12
      && context.open && context.querySelector('summary');
  })()`);
  assert.ok(await until(`(() => {
    const host = document.querySelector('.daily-legacy-context > div');
    const viewport = host.getBoundingClientRect();
    const range = [...CSS.highlights.get('kb-quote')][0].getBoundingClientRect();
    return viewport.height <= innerHeight * 0.4 + 1
      && range.top >= Math.max(0, viewport.top) - 1
      && range.bottom <= Math.min(innerHeight, viewport.bottom) + 1;
  })()`), '原版只在受限滚动区展示，且收藏落点确实可见');
  passed.push('旧收藏定位原文可见，不把全部历史正文重新平铺');
  await screenshot('favorite');
  await check('兼容正文中的新收藏仍使用原版坐标系', `(() => {
    const host = document.querySelector('.daily-legacy-context > div');
    const index = snipTextIndex(host);
    const start = index.text.indexOf('Frontier Slides，超快 ;)');
    const range = snipOffsetRange(index, start, start + 'Frontier Slides，超快 ;)'.length);
    const zone = snipZones.find(zone => zone.el === $('daily-reader-body'));
    const source = zone.describe(range);
    return source.root === host && source.origin_ref === '2026-10-17';
  })()`);
  await check('兼容界面文字和跨展示选区不能被冒充为日报收藏', `(() => {
    const zone = snipZones.find(zone => zone.el === $('daily-reader-body'));
    const range = document.createRange();
    range.selectNodeContents(document.querySelector('.daily-legacy-context > summary'));
    if (zone.describe(range)) return false;
    range.setStart(document.querySelector('.daily-material-title').firstChild, 0);
    range.setEnd(document.querySelector('.daily-legacy-context h2').firstChild, 2);
    return !zone.describe(range);
  })()`);
  await check('展开兼容正文不会污染简洁清单收藏上下文', `(() => {
    const title = document.querySelector('.daily-material-title');
    const range = document.createRange(); range.selectNodeContents(title);
    const zone = snipZones.find(zone => zone.el === $('daily-reader-body'));
    const source = zone.describe(range);
    const captured = snipSpec(zone, range, source);
    return captured.locatable && !captured.suffix.includes('收藏所在的原版正文')
      && source.skip === DAILY_LEGACY_SKIP;
  })()`);
  await evalJs(`openDailyReport('2026-10-17')`);
  await check('普通重新打开后恢复为纯清单', `!document.querySelector('.daily-legacy-context')`);
  await captureLegacy('把执行交给其他模型');
  await check('被收起的多段推文也能从旧收藏准确回跳', `(() => {
    const range = [...CSS.highlights.get('kb-quote')][0];
    return range.toString() === '把执行交给其他模型'
      && !!range.startContainer.parentElement.closest('.daily-legacy-context');
  })()`);
  await evalJs(`openDailyReport('2026-10-17')`);
  const compactSpec = await selectQuote('Frontier Slides，超快 ;)');
  await evalJs(`(async () => {
    dailyPendingSnip = { ref: '2026-10-17', spec: ${JSON.stringify(compactSpec)} };
    await openDailyReport('2026-10-17');
  })()`);
  await check('新清单收藏无需展开原版即可回跳', `CSS.highlights.has('kb-quote') && !document.querySelector('.daily-legacy-context')`);
  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
  await evalJs(`(async () => {
    dailyPendingSnip = { ref: '2026-10-17', spec: ${JSON.stringify(compactSpec)} };
    await openDailyReport('2026-10-17');
  })()`);
  await check('桌面采集的清单收藏在手机上仍可严格定位', `CSS.highlights.has('kb-quote') && !document.querySelector('.daily-legacy-context')`);
  await send('Emulation.clearDeviceMetricsOverride');
  await evalJs(`consumeDailySnipJump({ ref: '2026-10-17', spec: {
    ...${JSON.stringify(compactSpec)}, p: '已变化的前文' } }, '2026-10-17', $('daily-reader-body'))`);
  await check('仅引文本身唯一也不忽略已经变化的上下文', `!CSS.highlights.has('kb-quote') && !document.querySelector('.daily-legacy-context')`);
  await evalJs(`(() => {
    consumeDailySnipJump({ ref: '2026-10-17', spec: { q: 'Frontier Slides，超快 ;)',
      p: '', s: '', u: 0, e: 0 } }, '2026-10-17', $('daily-reader-body'));
  })()`);
  await check('不精确的原始锚点不会被兼容通道放宽', `!CSS.highlights.has('kb-quote') && !document.querySelector('.daily-legacy-context')`);
  await evalJs(`(() => {
    const host = $('daily-reader-body');
    const pending = { ref: '2026-10-16', spec: { q: '不存在的原文', p: '', s: '' } };
    window.materialUnconsumed = consumeDailySnipJump(pending, '2026-10-17', host) === pending;
    consumeDailySnipJump({ ...pending, ref: '2026-10-17' }, '2026-10-17', host);
  })()`);
  await check('错误日期不消费，变化或不存在的原文不伪造高亮', `materialUnconsumed && !CSS.highlights.has('kb-quote') && !document.querySelector('.daily-legacy-context')`);
  await evalJs(`openDailyReport('2026-10-16')`);
  await check('歧义无链接条目保留原文，编辑备注和作者没有被吞掉', `(() => {
    const host = $('daily-reader-body');
    return host.innerText.includes('这段补充说明必须显示。')
      && host.innerText.includes('没有原文链接的素材 — RSS·某号')
      && host.innerText.includes('资料整理 — RSS·编辑')
      && host.innerText.includes('后续说明。');
  })()`);
  await screenshot('postamble');
  await evalJs(`openDailyReport('2026-10-15')`);
  await check('整篇链接和图片定义在清单前后双向可见', `(() => {
    const host = $('daily-reader-body');
    const target = text => [...host.querySelectorAll('a')].find(link => link.textContent === text)?.getAttribute('href');
    const image = text => [...host.querySelectorAll('img')].find(image => image.alt === text)?.getAttribute('src');
    return target('原文') === '/doc/article' && target('上文定义') === '/doc/inside'
      && image('前图') === '/static/favicon.svg' && image('后图') === '/static/favicon.svg'
      && host.querySelectorAll('.daily-materials > li').length === 1;
  })()`);
  assert.ok(await until(`(() => {
    const images = [...$('daily-reader-body').querySelectorAll('img')];
    return images.length === 2 && images.every(image => image.complete && image.naturalWidth > 0);
  })()`), '双向引用式图片实际加载成功');
  passed.push('双向引用式图片实际加载成功');
  await check('改造前后非清单区链接和图片目标一致', `(async () => {
    const report = await (await fetch('/api/daily/reports/2026-10-15')).json();
    const collect = host => [...host.querySelectorAll('a, img')]
      .filter(node => !node.closest('ol'))
      .map(node => [node.tagName, node.textContent || node.alt, node.getAttribute('href') || node.getAttribute('src')]);
    const current = collect($('daily-reader-body'));
    const old = document.createElement('div'); old.innerHTML = mdHtml(report.body);
    return JSON.stringify(current) === JSON.stringify(collect(old));
  })()`);
  await screenshot('references');
  await evalJs(`renderDailyReportBody({ date: '2026-10-17', body: '', material_view: {
    before: '## 正文\\n\\n[1][2][3][4][5]', heading: '素材清单(5 条)', after: '',
    items: ['/doc/inside', '#section', 'mailto:hello@example.org', 'javascript:alert(1)', 'data:text/html,unsafe']
      .map((url, index) => ({ number: index + 1, title: '安全地址 ' + index,
        full_title: '安全地址 ' + index, source: 'RSS·作者', url }))
  } })`);
  await check('安全相对地址页内地址和邮件保留，危险协议仍被净化', `(() => {
    const links = [...document.querySelectorAll('.daily-materials a')].map(link => link.getAttribute('href'));
    const refs = [...document.querySelectorAll('#daily-reader-body a.daily-ref')].map(link => link.getAttribute('href'));
    const expected = ['/doc/inside', '#section', 'mailto:hello@example.org'];
    return JSON.stringify(links) === JSON.stringify(expected) && JSON.stringify(refs) === JSON.stringify(expected);
  })()`);
  await evalJs(`(() => { document.querySelector('.daily-material-source').textContent = 'RSS·' + '长作者'.repeat(500); })()`);
  for (const width of [1100, 780, 390]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 844, deviceScaleFactor: 1, mobile: false });
    await check(`${width}px 长作者不挤没标题或横向溢出`, `(() => {
      const title = document.querySelector('.daily-material-title').getBoundingClientRect();
      return title.width > 40 && document.documentElement.scrollWidth <= innerWidth;
    })()`);
  }
  await evalJs(`renderDailyReportBody({ date: '2026-10-17',
    body: '## 正文\\n\\n[危险链接][unsafe]\\n\\n[unsafe]: javascript:alert(1)', material_view: {
      before: '## 正文\\n\\n[危险链接][unsafe] <img src=x onerror="window.materialPayload=1">',
      heading: '素材清单(0 条)', after: '\\n[unsafe]: javascript:alert(1)', items: []
    } })`);
  await check('共享定义上下文后仍保留完整的 HTML 净化', `(() => {
    const host = $('daily-reader-body');
    return !host.querySelector('a[href^="javascript:"], [onerror], script') && !window.materialPayload;
  })()`);
  await evalJs(`(() => {
    const purifier = window.DOMPurify;
    window.DOMPurify = null;
    try {
      renderDailyReportBody({ date: '2026-10-17', body: '<script>window.materialPayload=1</script>',
        material_view: { before: '<script>window.materialPayload=1</script>',
          heading: '素材清单(0 条)', items: [], after: '' } });
    } finally { window.DOMPurify = purifier; }
  })()`);
  await check('净化器缺失时展示路径仍失败关闭', `!$('daily-reader-body').querySelector('script, a')
    && $('daily-reader-body').textContent.includes('<script>') && !window.materialPayload`);
  assert.deepEqual(pageErrors, [], '没有页面 JavaScript 异常');
  console.log(JSON.stringify({ passed, errors: pageErrors }));
} finally {
  ws.close();
  proc.kill();
}
