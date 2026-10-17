import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { attachCdp, sleep } from './pipe_history_cdp.mjs';

const base = process.argv[2];
const { proc, ws, send, evalJs, until, clickAt, pageErrors } = await attachCdp();
const jumps = [];
const geometryChecks = [];
const cases = [];
const directory = process.env.KB_MATERIAL_SCREENSHOTS;
const screenshot = async name => {
  if (!directory) return;
  mkdirSync(directory, { recursive: true });
  const image = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(`${directory}/snip-scroll-${name}.png`, Buffer.from(image.result.data, 'base64'));
};
const viewport = async width => send('Emulation.setDeviceMetricsOverride', {
  width, height: 844, deviceScaleFactor: 1, mobile: false,
});
const openFavorites = async () => {
  if (await evalJs(`innerWidth <= 900 && !document.body.classList.contains('nav-open')`)) {
    assert.ok(await clickAt('.mobile-menu'), '打开手机导航');
    assert.ok(await until(`document.body.classList.contains('nav-open')`));
  }
  assert.ok(await clickAt('#tab-fav'), '打开真实收藏面板');
};
const measure = (expression = `[...CSS.highlights.get('kb-quote')][0]`) => evalJs(`(() => {
  const range = ${expression};
  if (!range) return null;
  const rects = [...range.getClientRects()].filter(rect => rect.width && rect.height);
  const clip = { top: 0, bottom: innerHeight, left: 0, right: innerWidth };
  const ancestors = [];
  for (let host = range.startContainer.parentElement; host; host = host.parentElement) {
    if (host === document.scrollingElement) continue;
    const style = getComputedStyle(host);
    const bounds = host.getBoundingClientRect();
    const clipsX = /^(auto|scroll|hidden|clip)$/.test(style.overflowX);
    const clipsY = /^(auto|scroll|hidden|clip)$/.test(style.overflowY);
    if (!clipsX && !clipsY) continue;
    const left = bounds.left + host.clientLeft, top = bounds.top + host.clientTop;
    if (clipsX) { clip.left = Math.max(clip.left, left); clip.right = Math.min(clip.right, left + host.clientWidth); }
    if (clipsY) { clip.top = Math.max(clip.top, top); clip.bottom = Math.min(clip.bottom, top + host.clientHeight); }
    ancestors.push({ tag: host.tagName, class: host.className, id: host.id,
      top, left, width: host.clientWidth, height: host.clientHeight,
      scrollTop: host.scrollTop, scrollLeft: host.scrollLeft });
  }
  return { quote: range.toString(), clip, rects: rects.map(rect => rect.toJSON()), ancestors,
    visible: rects.length > 0 && rects.every(rect => rect.top >= clip.top - 1
      && rect.bottom <= clip.bottom + 1 && rect.left >= clip.left - 1 && rect.right <= clip.right + 1) };
})()`);

try {
  await viewport(390);
  await send('Page.navigate', { url: `${base}/#daily` });
  assert.ok(await until(`document.querySelectorAll('.daily-materials > li').length === 12`));
  for (const day of [14, 13]) {
    for (const edge of ['开头', '结尾']) {
      const quote = edge + '要找回的独特内容' + (edge === '开头' ? '正文说明。'.repeat(14) : '');
      const date = `2026-10-${day}`;
      await evalJs(`(async () => {
        await openDailyReport(${JSON.stringify(date)});
        const report = await (await fetch('/api/daily/reports/${date}')).json();
        const body = setDailyBody(mdHtml(report.body.replace(/^\\s*#\\s+[^\\n]*\\n+(?:>\\s*覆盖[^\\n]*\\n)?/, '')), report.date);
        enhanceDailyBody(body);
        const index = snipTextIndex(body), quote = ${JSON.stringify(quote)};
        const offset = index.text.indexOf(quote);
        if (offset < 0) throw new Error('missing legacy quote');
        const range = snipOffsetRange(index, offset, offset + quote.length);
        const spec = snipSpec({ el: body }, range, { root: body });
        if (!spec.locatable) throw new Error('inexact legacy anchor');
        const bounds = range.getBoundingClientRect();
        window.scrollBy({ top: bounds.top - innerHeight / 2, behavior: 'instant' });
        getSelection().removeAllRanges(); getSelection().addRange(range);
        showSnipBtn();
      })()`);
      assert.ok(await until(`$('snipbtn') && !$('snipbtn').hidden`));
      if (day === 14 && edge === '结尾') await screenshot('mobile-original');
      assert.ok(await clickAt('#snipbtn'), '真实保存长素材旧收藏');
      const expression = `(async () => (await (await fetch('/api/favorites')).json()).favorites
        .find(item => item.quote === ${JSON.stringify(quote)} && item.origin_ref === '${date}'))()`;
      assert.ok(await until(expression), '收藏已经落库');
      const favorite = await evalJs(expression);
      assert.equal(favorite.anchor_exact, 1);
      cases.push({ id: favorite.id, quote, date, linked: day === 14, edge });
    }
  }
  assert.ok(await until(`!document.querySelector('#toast-host .toast')`), '保存提示已自然消失');
  for (const width of [390, 1100]) {
    await viewport(width);
    for (const nested of [false, true]) {
      await evalJs(`(() => {
        const body = $('daily-reader-body');
        body.style.cssText = ${JSON.stringify(nested
          ? 'max-height:230px;overflow:auto;border:7px solid gray;padding:17px;scroll-behavior:smooth'
          : '')};
      })()`);
      for (const favorite of cases) {
        await openFavorites();
        const selector = `#fav-list [data-fav-card="${favorite.id}"] .fav-jump`;
        assert.ok(await until(`document.querySelector(${JSON.stringify(selector)})`));
        await evalJs(`getSelection().removeAllRanges()`);
        assert.ok(await clickAt(selector), '真实点击收藏回跳');
        assert.ok(await until(`(() => {
          const highlight = CSS.highlights.get('kb-quote');
          return highlight && [...highlight][0].toString() === ${JSON.stringify(favorite.quote)}
            && document.querySelector('.daily-legacy-context')?.open;
        })()`), '严格定位成功并展开原版');
        await sleep(800);
        const geometry = await measure();
        const name = `${width}-${nested ? 'nested' : 'normal'}-${favorite.linked ? 'link' : 'plain'}-${favorite.edge}`;
        await screenshot(name);
        assert.ok(geometry.visible, `${name}: 原句必须通过全部裁剪祖先后可见: ${JSON.stringify(geometry)}`);
        assert.ok(geometry.ancestors.length >= (nested ? 2 : 1));
        assert.ok(await evalJs(`!document.body.innerText.includes('没找到这段收藏')`));
        jumps.push({ name, ...geometry });
      }
    }
  }
  await viewport(390);
  await evalJs(`(() => {
    const stage = document.createElement('section');
    stage.id = 'snip-scroll-fixture';
    stage.style.cssText = 'margin:1100px 25px 1200px;width:310px;height:260px;overflow:auto;border:9px solid gray;scroll-behavior:smooth';
    stage.innerHTML = '<div style="height:360px"></div>'
      + '<div style="width:420px;height:210px;overflow:hidden;border:11px solid gray;padding:15px">'
      + '<div id="snip-scroll-inner" style="width:600px;height:150px;overflow:auto;border:7px solid gray;scroll-behavior:smooth">'
      + '<pre style="white-space:pre;margin:20px">' + '前面内容\\n'.repeat(40)
      + '横向内容'.repeat(100) + '跨越<strong>节点</strong>的原句'
      + '后面内容\\n'.repeat(40) + '</pre></div></div><div style="height:360px"></div>';
    document.body.append(stage);
    const index = snipTextIndex(stage), quote = '跨越节点的原句';
    const offset = index.text.indexOf(quote);
    const range = snipOffsetRange(index, offset, offset + quote.length);
    const spec = snipSpec({ el: stage }, range, { root: stage });
    window.snipScrollTest = { stage, range, spec: { q: spec.quote, p: spec.prefix,
      s: spec.suffix, u: +spec.unique, e: +spec.locatable } };
    if (!locateSnip(stage, snipScrollTest.spec)) throw new Error('nested range did not match');
  })()`);
  await sleep(800);
  const nested = await measure();
  assert.ok(nested.visible, `三层双向滚动必须露出跨节点原句: ${JSON.stringify(nested)}`);
  assert.equal(nested.ancestors.length, 3);
  assert.ok(nested.ancestors.some(host => host.scrollLeft > 0));
  assert.ok(nested.ancestors.filter(host => host.scrollTop > 0).length >= 2);
  geometryChecks.push({ name: 'three-level-both-axes-and-hidden-overflow', ...nested });
  await screenshot('nested-both-axes');
  const fallback = await evalJs(`(() => {
    const { stage, spec } = snipScrollTest;
    for (const host of stage.querySelectorAll('div')) host.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    stage.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    window.scrollTo({ top: 0, behavior: 'instant' });
    const html = stage.innerHTML, selection = getSelection().toString();
    const highlight = window.Highlight;
    try {
      window.Highlight = undefined;
      const found = locateSnip(stage, spec);
      return found && !CSS.highlights.has('kb-quote') && stage.innerHTML === html
        && getSelection().toString() === selection;
    } finally { window.Highlight = highlight; }
  })()`);
  assert.ok(fallback, '没有 Highlight API 时仍定位，不修改 DOM 或用户选区');
  assert.ok((await measure('snipScrollTest.range')).visible, '无高亮降级仍真正可见');
  geometryChecks.push({ name: 'without-highlight-api', visible: true });
  assert.ok(await evalJs(`(() => {
    const { stage, spec } = snipScrollTest;
    const positions = () => [scrollX, scrollY, ...[stage, ...stage.querySelectorAll('div')]
      .flatMap(host => [host.scrollLeft, host.scrollTop])];
    const before = JSON.stringify(positions());
    return !locateSnip(stage, { ...spec, e: 0 })
      && !locateSnip(stage, { ...spec, p: '失效的前文锚点', s: '失效的后文锚点' })
      && !locateSnip(stage, { ...spec, q: '完全不存在的原句' })
      && before === JSON.stringify(positions()) && !CSS.highlights.has('kb-quote');
  })()`), '不精确、不匹配的锚点不能成功或触发滚动');
  geometryChecks.push({ name: 'strict-anchor-rejection', unchanged: true });
  await evalJs(`(() => {
    const { stage } = snipScrollTest;
    stage.style.height = '120px';
    stage.innerHTML = '<p>' + '很长的一整段收藏原文。'.repeat(100) + '</p>';
    const range = document.createRange(); range.selectNodeContents(stage.querySelector('p'));
    const captured = snipSpec({ el: stage }, range, { root: stage });
    if (!locateSnip(stage, { q: captured.quote, p: captured.prefix, s: captured.suffix,
      e: +captured.locatable, u: +captured.unique })) throw new Error('oversized quote did not match');
    const index = snipTextIndex(stage);
    snipScrollTest.range = snipOffsetRange(index, 0, 1);
  })()`);
  assert.ok((await measure('snipScrollTest.range')).visible, '超出滚动区高度的整段收藏至少露出开头');
  geometryChecks.push({ name: 'oversized-quote-shows-start', visible: true });
  await screenshot('oversized-start');
  await evalJs(`(() => {
    const { stage } = snipScrollTest;
    stage.style.cssText = 'margin:1100px 25px 1200px;width:310px';
    stage.innerHTML = '<p>' + '原始长段落。'.repeat(120) + '视口末尾要找回的原句' + '</p>';
    const index = snipTextIndex(stage), quote = '视口末尾要找回的原句';
    const offset = index.text.indexOf(quote), range = snipOffsetRange(index, offset, offset + quote.length);
    const spec = snipSpec({ el: stage }, range, { root: stage });
    if (!locateSnip(stage, { q: spec.quote, p: spec.prefix, s: spec.suffix, e: +spec.locatable }))
      throw new Error('viewport quote did not match');
  })()`);
  assert.ok((await measure()).visible, '无内部滚动区时也按长段落中的原句滚动视口');
  geometryChecks.push({ name: 'viewport-only-long-paragraph', visible: true });
  assert.deepEqual(pageErrors, [], '没有页面 JavaScript 异常');
  const result = { jumps, geometryChecks, errors: pageErrors };
  if (directory) writeFileSync(`${directory}/snip-scroll-results.json`, JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} finally {
  ws.close();
  proc.kill();
}
