import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { attachCdp } from './pipe_history_cdp.mjs';

const base = process.argv[2];
const day = process.argv[3] || '2026-10-03';
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
  const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  writeFileSync(`${directory}/daily-queue-${day}-${name}.png`, Buffer.from(shot.result.data, 'base64'));
};

try {
  await send('Page.navigate', { url: `${base}/#daily` });
  const selector = `.daily-row[data-report="${day}"]`;
  assert.ok(await until(`!!document.querySelector(${JSON.stringify(selector)})`), '历史日报卡片已加载');
  assert.ok(await clickAt(selector), '真实点击含补录说明的日报卡片');
  assert.ok(await until(`document.querySelector('#daily-reader-body')?.dataset.snipRef === '${day}'
    && document.querySelectorAll('.daily-materials > li').length === 198`),
  '138 条窗口素材和 60 条补录显示为 198 个简洁条目');
  await evalJs(`(async () => {
    window.queueProbeReport = await (await fetch('/api/daily/reports/${day}')).json();
  })()`);
  await check('窗口与补录数量来自日报保存的快照', `queueProbeReport.docs === 138
    && queueProbeReport.total === 138 && queueProbeReport.late === 60
    && queueProbeReport.late_total === 791`);
  await check('条数、编号、短标题和来源逐条对应保存的日报', `(() => {
    const rows = [...document.querySelectorAll('.daily-materials > li')];
    return queueProbeReport.material_view.items.length === 198 && rows.every((row, index) => {
      const item = queueProbeReport.material_view.items[index];
      return item.number === index + 1 && [...item.title].length <= 72
        && row.querySelector('.daily-material-title').textContent === item.title
        && row.querySelector('.daily-material-source').textContent === ' — ' + item.source
        && !row.querySelector('p, ol, ul');
    });
  })()`);
  await check('素材原文链接仍保留，不把多段原文重新平铺', `(() => {
    const rows = [...document.querySelectorAll('.daily-materials > li')];
    return rows.every((row, index) => {
      const item = queueProbeReport.material_view.items[index];
      const link = row.querySelector('a');
      return item.url ? link?.href === dailyMarkdownLink(item.url)?.href
        && link.target === '_blank' : !link;
    });
  })()`);
  await check('补录说明完整保留在清单后，不混进最后一个作者', `(() => {
    const note = document.querySelector('.daily-materials').nextElementSibling;
    return note?.tagName === 'BLOCKQUOTE'
      && note.textContent.includes('补录共 791 条,以上列出 60 条。')
      && note.textContent.includes('没列出的留在队列里,下一份日报接着带。');
  })()`);
  await check('复制输入仍是完整原文，未被简洁标题替换', `document.getElementById('daily-copy').dataset.body
    === queueProbeReport.body && queueProbeReport.body.length
    > document.getElementById('daily-reader-body').textContent.length`);
  await evalJs(`document.querySelector('.daily-materials').previousElementSibling
    .scrollIntoView({ block: 'start', behavior: 'instant' })`);
  await screenshot('desktop');
  await send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: false,
  });
  await check('390px 窄屏下仍是一条条清单，标题和来源分行且无横向溢出', `(() => {
    const entry = document.querySelector('.daily-material-entry');
    return getComputedStyle(entry).display === 'block'
      && document.documentElement.scrollWidth <= innerWidth
      && document.querySelectorAll('.daily-materials > li').length === 198;
  })()`);
  await evalJs(`document.querySelector('.daily-materials').previousElementSibling
    .scrollIntoView({ block: 'start', behavior: 'instant' })`);
  await screenshot('mobile');
  await evalJs(`document.querySelector('.daily-materials').nextElementSibling
    .scrollIntoView({ block: 'end', behavior: 'instant' })`);
  await screenshot('footer');
  assert.deepEqual(pageErrors, [], '页面没有脚本错误');
  console.log(JSON.stringify({ passed, errors: pageErrors }));
} finally {
  ws.close();
  proc.kill();
}
