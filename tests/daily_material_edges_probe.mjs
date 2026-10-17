import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { attachCdp, sleep } from './pipe_history_cdp.mjs';

const base = process.argv[2];
const { proc, port, ws, send, evalJs, until, clickAt, pageErrors } = await attachCdp();
const passed = [];
const navigations = [];
const check = async (name, expression) => {
  assert.ok(await evalJs(expression), name);
  passed.push(name);
};
const screenshot = async name => {
  const directory = process.env.KB_MATERIAL_SCREENSHOTS;
  if (!directory) return;
  mkdirSync(directory, { recursive: true });
  const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  writeFileSync(`${directory}/daily-material-edges-${name}.png`, Buffer.from(shot.result.data, 'base64'));
};
const compareTargets = async name => {
  await check(name, `(() => {
    const host = $('daily-reader-body');
    const targets = [...host.querySelectorAll('.daily-materials > li')].map(row => {
      const link = row.querySelector('a[href]');
      return link ? [link.getAttribute('href'), link.href] : [null, null];
    });
    const refs = new Map([...host.querySelectorAll('a')].filter(link => !link.closest('ol, details'))
      .map(link => [link.textContent, [link.getAttribute('href'), link.href]]));
    return JSON.stringify(targets) === JSON.stringify(edgeExpected)
      && edgeExpected.every((target, index) => !target[0]
        || JSON.stringify(refs.get('[' + (index + 1) + ']')) === JSON.stringify(target));
  })()`);
};
const clickDestination = async selector => {
  const endpoint = `http://127.0.0.1:${port}`;
  const targets = async () => (await fetch(endpoint + '/json/list')).json();
  const before = new Set((await targets()).map(target => target.id));
  assert.ok(await clickAt(selector), '真实点击本地链接');
  let opened;
  const deadline = Date.now() + 6000;
  while (!opened && Date.now() < deadline) {
    opened = (await targets()).find(target => !before.has(target.id) && target.url.startsWith(base + '/?'));
    if (!opened) await sleep(100);
  }
  assert.ok(opened, '独立无头浏览器确实打开本地目标');
  try {
    return opened.url;
  } finally {
    await fetch(endpoint + '/json/close/' + opened.id);
  }
};

try {
  await send('Page.navigate', { url: `${base}/#daily` });
  assert.ok(await until(`document.querySelectorAll('.daily-materials > li').length === 12`));
  for (const date of ['2026-10-08', '2026-10-07', '2026-10-06']) {
    const report = await evalJs(`(async () => {
      const response = await fetch('/api/daily/reports/${date}');
      return { status: response.status, data: await response.json() };
    })()`);
    assert.equal(report.status, 200, '数字异常不能阻断读取 API');
    assert.ok(await clickAt(`#daily-list [data-report="${date}"]`));
    assert.ok(await until(`$('daily-copy').dataset.body === ${JSON.stringify(report.data.body)}`));
    await check(`${date} 超长数字仍可真实打开、阅读和复制`, `(() => {
      const host = $('daily-reader-body');
      return host.textContent.includes('正常日报正文。')
        && !host.textContent.includes('读取失败') && $('daily-copy').dataset.body.includes('99999999');
    })()`);
    await screenshot(date);
  }
  await evalJs(`(async () => {
    window.edgeReport = await (await fetch('/api/daily/reports/2026-10-05')).json();
    const old = document.createElement('div');
    old.innerHTML = mdHtml(edgeReport.body);
    window.edgeExpected = [...old.querySelectorAll('ol > li')].map(row => {
      const link = row.querySelector('a[href]');
      return link ? [link.getAttribute('href'), link.href] : [null, null];
    });
    await openDailyReport('2026-10-05');
  })()`);
  const expected = await evalJs('edgeExpected');
  assert.equal(expected.length, 15);
  assert.equal(expected[0][0], 'https://example.org/paper?lang=en&download=1');
  assert.equal(expected[1][0], 'https://example.org/path_name');
  assert.equal(expected[2][0], 'https://example.org/paper?lang=en&amp;download=1');
  assert.equal(expected[8][0], 'https://example.org/encoded%2Fpath?x=%26amp%3B');
  assert.equal(expected[12][0], null);
  assert.equal(expected[13][0], null);
  await compareTargets('15 种目标：新标题、预链接正文编号与原版地址完全一致');
  await check('复制保留原 Markdown，不把目标解码写回文件', `$('daily-copy').dataset.body === edgeReport.body
    && edgeReport.body.includes('&amp;amp;download') && edgeReport.body.includes(${JSON.stringify('path\\_name')})`);
  await screenshot('links-desktop');
  for (const number of [5, 15]) {
    await evalJs(`(() => {
      const link = [...$('daily-reader-body').querySelectorAll('a')]
        .find(link => !link.closest('ol') && link.textContent === '[${number}]');
      link.dataset.edgeClick = 'reference';
    })()`);
    const reference = await clickDestination('#daily-reader-body [data-edge-click="reference"]');
    const title = await clickDestination(`.daily-materials > li:nth-child(${number}) a`);
    assert.equal(title, reference, '真实点击正文和标题的新标签导航相同');
    const query = new URL(title).searchParams;
    assert.ok(query.has(number === 5 ? 'download' : 'amp;download'));
    navigations.push({ number, reference, title });
    passed.push(`第 ${number} 条实体只解码一次，真实导航目标相同`);
    await evalJs(`document.querySelector('[data-edge-click]').removeAttribute('data-edge-click')`);
  }
  await evalJs(String.raw`renderDailyReportBody({ ...edgeReport, material_view: { ...edgeReport.material_view,
    before: '## 旧正文\n\n' + edgeExpected.map((_, index) => '[' + (index + 1) + ']').join(' ')
  } })`);
  await compareTargets('需补链接的旧正文编号复用已解码目标，不二次解释实体');
  await evalJs(`(() => {
    const host = setDailyBody(mdHtml(edgeReport.body), edgeReport.date);
    enhanceDailyBody(host);
    const quote = '旧正文链接兼容查找标记';
    const index = snipTextIndex(host), start = index.text.indexOf(quote);
    const range = snipOffsetRange(index, start, start + quote.length);
    const spec = snipSpec({ el: host }, range, { root: host });
    if (!spec.locatable) throw new Error('旧正文测试锚点不可定位');
    const current = renderDailyReportBody(edgeReport);
    consumeDailySnipJump({ ref: edgeReport.date, spec: { q: spec.quote, p: spec.prefix,
      s: spec.suffix, u: spec.unique ? 1 : 0, e: spec.locatable ? 1 : 0 } }, edgeReport.date, current);
  })()`);
  await check('收藏兼容原版正文和简洁列表的目标相同', `(() => {
    const host = document.querySelector('.daily-legacy-context > div');
    if (!host || !CSS.highlights.has('kb-quote')) return false;
    const targets = [...host.querySelectorAll('ol > li')].map(row => {
      const link = row.querySelector('a[href]');
      return link ? [link.getAttribute('href'), link.href] : [null, null];
    });
    return JSON.stringify(targets) === JSON.stringify(edgeExpected);
  })()`);
  await evalJs('renderDailyReportBody(edgeReport)');
  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
  await compareTargets('手机仍保持三种链接目标一致');
  await evalJs(`document.querySelector('.daily-materials').scrollIntoView({ block: 'start', behavior: 'instant' })`);
  await screenshot('links-mobile');
  for (const library of ['marked', 'DOMPurify']) {
    await evalJs(`(() => {
      const library = window.${library};
      window.${library} = null;
      try { renderDailyReportBody(edgeReport); } finally { window.${library} = library; }
    })()`);
    await check(`${library} 缺失仍安全降级，不生成未净化链接`, `!$('daily-reader-body').querySelector('a, script')
      && $('daily-reader-body').textContent.includes('链接样例')`);
  }
  assert.deepEqual(pageErrors, [], '页面无 JavaScript 异常');
  console.log(JSON.stringify({ passed, navigations, errors: pageErrors }));
} finally {
  ws.close();
  proc.kill();
}
