import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { attachCdp } from './pipe_history_cdp.mjs';

const base = process.argv[2];
const { proc, ws, send, evalJs, until, clickAt, pageErrors } = await attachCdp();
const passed = [];

try {
  await send('Page.navigate', { url: `${base}/#daily` });
  assert.ok(await until(`document.querySelectorAll('#daily-list button').length > 0
    && document.querySelector('#daily-reader-body')?.dataset.snipRef === '2026-11-10'`));
  for (const width of [1100, 390]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 844, deviceScaleFactor: 1, mobile: false });
    for (let day = 1; day <= 10; day += 1) {
      const date = `2026-11-${String(day).padStart(2, '0')}`;
      const report = await evalJs(`(async () => (await fetch('/api/daily/reports/${date}')).json())()`);
      assert.ok(await clickAt(`.daily-row[data-report="${date}"]`), `${date} 真实点击日报`);
      assert.ok(await until(`$('daily-copy').dataset.body === ${JSON.stringify(report.body)}`));
      if (day === 3) {
        assert.equal(report.material_view.items.length, 2, '正常无尾注清单仍紧凑展示');
        assert.equal(await evalJs(`$('daily-reader-body').querySelectorAll('.daily-materials > li').length`), 2);
      } else {
        assert.equal(report.material_view, null, `${date} 快照不足以证明投影时回退`);
        assert.ok(await evalJs(`(() => {
          const host = $('daily-reader-body');
          const current = host.innerHTML;
          const report = ${JSON.stringify(report)};
          renderDailyReportBody({ date: report.date, body: report.body });
          return !host.querySelector('.daily-materials') && current === host.innerHTML;
        })()`), `${date} 与原版净化 DOM 完全一致`);
      }
      assert.ok(await evalJs(`(() => {
        const host = $('daily-reader-body');
        return host.innerText.includes('真实首条素材') && host.innerText.includes('真实补录素材')
          && host.innerText.includes('窗口作者') && host.innerText.includes('补录作者')
          && host.innerText.includes('真实要点')
          && [...host.querySelectorAll('a')].some(link =>
            link.getAttribute('href') === 'https://example.org/snapshot-first');
      })()`), `${date} 真实素材、作者、要点和原文链接未消失`);
      if (day === 2) {
        assert.ok(await evalJs(`$('daily-reader-body').innerText.includes('没列出的留在队列里')`),
          '回退保留完整队列说明');
      }
      passed.push(`${width}px ${date} ${day === 3 ? '紧凑展示' : '完整回退'}`);
      const directory = process.env.KB_MATERIAL_SCREENSHOTS;
      if (directory && [1, 3, 4].includes(day)) {
        mkdirSync(directory, { recursive: true });
        await evalJs(`document.querySelector('.daily-reader').scrollIntoView({ block: 'start', behavior: 'instant' })`);
        const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
        writeFileSync(`${directory}/daily-materials-snapshot-${date}-${width}.png`,
          Buffer.from(shot.result.data, 'base64'));
      }
    }
  }
  assert.deepEqual(pageErrors, [], '快照校验页面没有 JavaScript 异常');
  console.log(JSON.stringify({ passed, errors: pageErrors }));
} finally {
  ws.close();
  proc.kill();
}
