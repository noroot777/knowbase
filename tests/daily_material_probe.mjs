import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { attachCdp } from './pipe_history_cdp.mjs';

const base = process.argv[2];
const { proc, ws, send, evalJs, until, pageErrors } = await attachCdp();
const passed = [];
const check = async (name, expression) => {
  assert.ok(await evalJs(expression), name);
  passed.push(name);
};

try {
  await send('Page.navigate', { url: `${base}/#daily` });
  assert.ok(await until(`document.querySelectorAll('.daily-materials > li').length === 12`),
    '日报读取真实 API 后显示 12 条素材');
  await check('每条只有一个短标题和一个出处', `(() => {
    const rows = [...document.querySelectorAll('.daily-materials > li')];
    return rows.length === 12 && rows.every(row => row.querySelectorAll('.daily-material-title').length === 1
      && row.querySelectorAll('.daily-material-source').length === 1 && !row.querySelector('p, ol, ul'));
  })()`);
  await check('多段推文只显示首行，完整内容保留在提示中', `(() => {
    const title = document.querySelector('.daily-materials > li:nth-child(2) .daily-material-title');
    return title.textContent === 'Grok Bot 套餐不够用怎么办？'
      && title.title.includes('把执行交给其他模型');
  })()`);
  await check('编号与原文地址保持一一对应', `(() => {
    const link = document.querySelector('.daily-materials > li:nth-child(10) a');
    return link.href === 'https://example.org/material-9' && link.target === '_blank'
      && link.rel.includes('noopener');
  })()`);
  await check('没有链接的素材仍为普通文字', `!document.querySelector('.daily-materials > li:nth-child(4) a')`);
  await check('正文引用仍指向正确原文', `(() => {
    const links = [...document.querySelectorAll('#daily-reader-body a.daily-ref')];
    return links.some(link => link.textContent === '[2]' && link.href === 'https://example.org/material-1')
      && links.some(link => link.textContent === '[10]' && link.href === 'https://example.org/material-9');
  })()`);
  await check('复制内容仍然保留完整的原始推文', `document.getElementById('daily-copy').dataset.body.includes('1. 负责调度\\n2. 把执行交给其他模型')`);
  await check('素材清单后的队列说明仍可见', `document.querySelector('#daily-reader-body').textContent.includes('没列出的留在队列里')`);
  await check('桌面条目逐行排列且不粘连', `(() => {
    const rows = [...document.querySelectorAll('.daily-materials > li')];
    return rows.every((row, index) => row.getBoundingClientRect().height < 65
      && (!index || row.getBoundingClientRect().top >= rows[index - 1].getBoundingClientRect().bottom - 1));
  })()`);
  const artifacts = process.env.KB_MATERIAL_SCREENSHOTS;
  if (artifacts) {
    mkdirSync(artifacts, { recursive: true });
    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${artifacts}/daily-materials-desktop.png`, Buffer.from(shot.result.data, 'base64'));
  }
  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
  await check('手机标题与来源分行，没有横向溢出', `(() => {
    const entry = document.querySelector('.daily-material-entry');
    return getComputedStyle(entry).display === 'block' && document.documentElement.scrollWidth <= innerWidth;
  })()`);
  if (artifacts) {
    await evalJs(`document.querySelector('.daily-materials').previousElementSibling.scrollIntoView()`);
    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    writeFileSync(`${artifacts}/daily-materials-mobile.png`, Buffer.from(shot.result.data, 'base64'));
  }
  await evalJs(`(() => {
    const report = { date: '2026-10-17', body: '', material_view: {
      before: '## 素材清单(0 条)\\n\\n这不是引用表 [1]。\\n\\n## 正文\\n\\n正确引用 [1]',
      heading: '素材清单(2 条)', after: '', items: [
        { title: '<img src=x onerror=alert(1)>', full_title: '原始长标题',
          source: 'RSS·<script>alert(1)</script>', url: 'https://example.org/safe' },
        { title: '不安全地址', full_title: '原始标题', source: 'RSS·作者', url: 'javascript:alert(1)' }
      ] } };
    renderDailyReportBody(report);
  })()`);
  await check('标题和作者按文字渲染，危险地址不生成链接', `(() => {
    const host = document.getElementById('daily-reader-body');
    return !host.querySelector('img, script, a[href^="javascript:"]')
      && host.querySelector('.daily-material-title').textContent.includes('<img')
      && !host.querySelector('.daily-materials > li:nth-child(2) a');
  })()`);
  await check('正文里的假清单抬头不影响出处编号', `document.querySelector('#daily-reader-body a.daily-ref').href === 'https://example.org/safe'`);
  await evalJs(`renderDailyReportBody({ date: '2026-10-16', body:
    '## 今日要点\\n\\n旧正文 [1]\\n\\n## 素材清单(1 条)\\n\\n1. [旧标题](https://example.org/legacy) — RSS·作者' })`);
  await check('旧接口缺少展示模型时仍能阅读和跳转引用', `document.querySelector('#daily-reader-body a.daily-ref').href === 'https://example.org/legacy'`);
  assert.deepEqual(pageErrors, [], '页面没有 JavaScript 异常');
  console.log(JSON.stringify({ passed, errors: pageErrors }));
} finally {
  ws.close();
  proc.kill();
}
