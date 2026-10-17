import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { attachCdp } from './pipe_history_cdp.mjs';

const base = process.argv[2];
const { proc, ws, send, evalJs, until, pageErrors } = await attachCdp();
const passed = [];
const cases = [
  ['2026-10-09', '同条数假标题', 'https://example.org/real'],
  ['2026-10-10', '标题内来源形状', 'https://example.org/info'],
  ['2026-10-11', '标题链接后还有说明', 'https://example.org/info'],
  ['2026-10-12', '带链接多段作者', 'https://example.org/real'],
  ['2026-10-13', '无链接多段作者', null],
  ['2026-10-14', '无法独立确认的清单后注', 'https://example.org/real'],
];

try {
  await send('Page.navigate', { url: `${base}/#daily` });
  assert.ok(await until(`document.querySelectorAll('.daily-materials > li').length === 12`));
  for (const width of [1100, 390]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 844, deviceScaleFactor: 1, mobile: false });
    for (const [date, name, destination] of cases) {
      const report = await evalJs(`(async () => (await fetch('/api/daily/reports/${date}')).json())()`);
      assert.equal(report.material_view, null, `${name} API 安全回退`);
      await evalJs(`openDailyReport(${JSON.stringify(date)})`);
      assert.ok(await until(`$('daily-copy').dataset.body === ${JSON.stringify(report.body)}`));
      assert.ok(await evalJs(`(() => {
        const host = $('daily-reader-body');
        const current = host.innerHTML;
        const report = ${JSON.stringify(report)};
        renderDailyReportBody({ date: report.date, body: report.body });
        return !host.querySelector('.daily-materials') && current === host.innerHTML;
      })()`), `${name} 与未简化的原版 DOM 完全一致`);
      if (destination) {
        assert.ok(await evalJs(`[...$('daily-reader-body').querySelectorAll('a')]
          .some(link => link.getAttribute('href') === ${JSON.stringify(destination)})`),
        `${name} 原文链接仍可点`);
      }
      if (date === '2026-10-12' || date === '2026-10-13') {
        assert.ok(await evalJs(`$('daily-reader-body').innerText.includes('作者第一行')
          && $('daily-reader-body').innerText.includes('第二行')`), `${name} 作者没有被截断`);
      }
      if (date === '2026-10-14') {
        assert.ok(await evalJs(`$('daily-reader-body').innerText.includes('没列出的留在队列里')`),
          '队列说明仍完整可见');
      }
      passed.push(`${width}px ${name} 与原版完全一致`);
      const directory = process.env.KB_MATERIAL_SCREENSHOTS;
      if (directory) {
        mkdirSync(directory, { recursive: true });
        const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
        writeFileSync(`${directory}/daily-materials-ambiguity-${date}-${width}.png`,
          Buffer.from(shot.result.data, 'base64'));
      }
    }
  }
  assert.deepEqual(pageErrors, [], '回退页面没有 JavaScript 异常');
  console.log(JSON.stringify({ passed, errors: pageErrors }));
} finally {
  ws.close();
  proc.kill();
}
